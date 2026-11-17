"""Sensors for myEnergy.

The Energy dashboard reads the imported statistics directly; these sensors
are for seeing at a glance how fresh the data is and whether the session is
still alive. When a tariff is set up, three more follow the retailer's clock for automations:
the current period, its rate, and when the period next changes. They are not polled; a timer
wakes them at each boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import MARKET_TZ
from .coordinator import MyEnergyConfigEntry, MyEnergyCoordinator
from .entity import MyEnergyEntity, device_info
from .exceptions import TariffError
from .statistics import channel_label
from .tariff import (
    KIND_IMPORT,
    SCAN_HOURS,
    PeriodRate,
    Tariff,
    next_period_change,
    period_slugs,
    rate_at,
)

# Every sensor reads data the coordinator (or the tariff timeline) already holds and makes no
# request of its own, so there is nothing to limit.
PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [LatestDataSensor(coordinator), SessionSensor(coordinator)]
    known: set[str] = set()

    @callback
    def _add_channels() -> None:
        new = [
            LatestDayEnergySensor(coordinator, suffix)
            for suffix in (coordinator.data.channels if coordinator.data else {})
            if suffix not in known
        ]
        known.update(sensor.suffix for sensor in new)
        if new:
            async_add_entities(new)

    tariff = coordinator.load_tariff()
    timeline: TariffTimeline | None = None
    if tariff is not None:
        try:
            timeline = TariffTimeline(hass, tariff)
        except TariffError as err:
            # No time zone database for the tariff's zone (install the tzdata package). Only the
            # tariff sensors depend on it, so the rest still load.
            _LOGGER.warning("Skipping the tariff sensors for %s: %s", coordinator.log_name, err)
    if tariff is not None and timeline is not None:
        entry.async_on_unload(timeline.async_stop)
        entities += [
            TariffPeriodSensor(coordinator, timeline),
            TariffRateSensor(coordinator, timeline),
            TariffNextChangeSensor(coordinator, timeline),
        ]

    async_add_entities(entities)
    _add_channels()
    entry.async_on_unload(coordinator.async_add_listener(_add_channels))


class LatestDataSensor(MyEnergyEntity, SensorEntity):
    """When the newest reading the portal holds ends."""

    _attr_translation_key = "latest_data"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MyEnergyCoordinator) -> None:
        """Create the sensor."""
        super().__init__(coordinator, "latest_data")

    @property
    def native_value(self) -> datetime | None:
        """Return the end of the latest day with readings."""
        days = [c.last_day for c in (self.coordinator.data.channels.values()) if c.last_day]
        if not days:
            return None
        return datetime.combine(max(days) + timedelta(days=1), time.min, MARKET_TZ)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe what the portal has."""
        data = self.coordinator.data
        return {
            "history_starts": data.account.start.isoformat() if data.account.start else None,
            "last_fetch": data.last_fetch.isoformat() if data.last_fetch else None,
            "hours_added_last_fetch": data.hours_added,
            "statistic_ids": [c.statistic_id for c in data.channels.values()],
        }


class LatestDayEnergySensor(MyEnergyEntity, SensorEntity):
    """The total for the latest complete day, per channel.

    No state class: the Energy dashboard should use the imported statistics,
    which carry the true hourly timing, not this day-late figure.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: MyEnergyCoordinator, suffix: str) -> None:
        """Create the sensor for one channel."""
        super().__init__(coordinator, f"latest_day_{suffix.lower()}")
        self.suffix = suffix
        self._attr_name = f"Latest day {channel_label(suffix)}"

    @property
    def available(self) -> bool:
        """Only while the channel is still reported."""
        return super().available and self.suffix in self.coordinator.data.channels

    @property
    def native_value(self) -> float | None:
        """Return the latest day's total."""
        channel = self.coordinator.data.channels.get(self.suffix)
        return channel.last_day_kwh if channel else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say which day the total is for."""
        channel = self.coordinator.data.channels.get(self.suffix)
        if channel is None:
            return {}
        return {
            "day": channel.last_day.isoformat() if channel.last_day else None,
            "meter_serial": channel.meter_serial,
            "statistic_id": channel.statistic_id,
        }


class SessionSensor(MyEnergyEntity, SensorEntity):
    """Whether the portal session is alive."""

    _attr_translation_key = "session"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["active", "expired"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: MyEnergyCoordinator) -> None:
        """Create the sensor."""
        super().__init__(coordinator, "session")

    @property
    def available(self) -> bool:
        """Stay available so an expired session is visible."""
        return True

    @property
    def native_value(self) -> str:
        """Return the session state."""
        return "expired" if self.coordinator.session_dead else "active"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Describe the session."""
        c = self.coordinator
        return {
            "signed_in_at": c.signed_in_at.isoformat() if c.signed_in_at else None,
            "last_keepalive": c.last_keepalive.isoformat() if c.last_keepalive else None,
            "seconds_remaining": c.session_seconds,
            "keepalive_minutes": round(c.keepalive_interval.total_seconds() / 60),
            "expired_at": c.session_dead_since.isoformat() if c.session_dead_since else None,
        }


# --- Live tariff sensors ---------------------------------------------------------------


class TariffTimeline:
    """Knows which tariff period it is and wakes its sensors exactly when that changes.

    One timer, set for the next boundary, so nothing polls. Entities read the answer as of the
    last boundary; the timeline rewrites them after each one. When nothing changes within the
    scan horizon (a flat tariff, or the next version is further away) it looks again before
    the horizon runs out, so a tariff version that starts later is still picked up.
    """

    def __init__(self, hass: HomeAssistant, tariff: Tariff) -> None:
        """Work out the current period; the first listener starts the timer."""
        self._hass = hass
        self.tariff = tariff
        self.currency = hass.config.currency
        self.slugs = period_slugs(tariff)
        self.period_names = list(self.slugs)
        self._listeners: list[Callable[[], None]] = []
        self._cancel: CALLBACK_TYPE | None = None
        self._stopped = False
        self._boundary: datetime | None = None
        self._now = dt_util.utcnow()
        self.current: PeriodRate | None = None
        self.upcoming: tuple[datetime, PeriodRate | None] | None = None
        self._refresh(self._now)

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        """Call ``listener`` after every boundary; the first listener starts the timer."""
        self._listeners.append(listener)
        if self._cancel is None and not self._stopped:
            self._schedule()

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def async_stop(self) -> None:
        """Cancel the timer (the entry is unloading)."""
        self._stopped = True
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    def _refresh(self, moment: datetime) -> None:
        self._now = moment
        self.current = rate_at(self.tariff, moment, KIND_IMPORT)
        change = next_period_change(self.tariff, moment, KIND_IMPORT)
        self.upcoming = (
            None if change is None else (change[0], rate_at(self.tariff, change[0], KIND_IMPORT))
        )

    def _schedule(self) -> None:
        if self.upcoming is not None:
            self._boundary = self.upcoming[0]
        else:
            self._boundary = self._now + timedelta(hours=SCAN_HOURS - 24)
        self._cancel = async_track_point_in_utc_time(self._hass, self._fire, self._boundary)

    @callback
    def _fire(self, _when: datetime) -> None:
        self._cancel = None
        if self._stopped:
            return
        # The boundary itself, unless the timer ran late (the system slept).
        try:
            self._refresh(max(self._boundary or dt_util.utcnow(), dt_util.utcnow()))
        except TariffError as err:
            _LOGGER.warning("The tariff sensors have stopped updating: %s", err)
            return
        self._schedule()
        for listener in list(self._listeners):
            listener()


class TariffEntity(SensorEntity):
    """Common base: attached to the NMI's device and refreshed by the timeline."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: MyEnergyCoordinator, timeline: TariffTimeline, key: str
    ) -> None:
        """Attach to the NMI's device."""
        self._timeline = timeline
        self._attr_unique_id = f"{coordinator.nmi}_{key}"
        self._attr_device_info = device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        """Rewrite the state at each period boundary."""
        self.async_on_remove(self._timeline.async_add_listener(self.async_write_ha_state))


class TariffPeriodSensor(TariffEntity):
    """The tariff period in force now, for automations that react to it changing."""

    _attr_translation_key = "tariff_period"
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(self, coordinator: MyEnergyCoordinator, timeline: TariffTimeline) -> None:
        """Create the sensor."""
        super().__init__(coordinator, timeline, "tariff_period")
        self._attr_options = list(timeline.period_names)

    @property
    def native_value(self) -> str | None:
        """Return the current period's name."""
        current = self._timeline.current
        return None if current is None else current.period.name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Give the rates now and for the next period."""
        current = self._timeline.current
        upcoming = self._timeline.upcoming
        following = upcoming[1] if upcoming else None
        return {
            "rate_c_per_kwh": None if current is None else current.c_per_kwh,
            "rate_after_discount_c_per_kwh": (
                None if current is None else current.discounted_c_per_kwh
            ),
            "tiered": None if current is None else current.tiered,
            "period_slug": (
                None if current is None else self._timeline.slugs.get(current.period.name)
            ),
            "next_period": None if following is None else following.period.name,
            "next_rate_c_per_kwh": None if following is None else following.c_per_kwh,
            "next_period_starts": (dt_util.as_local(upcoming[0]).isoformat() if upcoming else None),
        }


class TariffRateSensor(TariffEntity):
    """The headline import rate now, in currency per kWh (GST included, before any discount)."""

    _attr_translation_key = "tariff_rate"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: MyEnergyCoordinator, timeline: TariffTimeline) -> None:
        """Create the sensor; the unit follows Home Assistant's currency."""
        super().__init__(coordinator, timeline, "tariff_rate")
        self._attr_native_unit_of_measurement = f"{timeline.currency}/kWh"

    @property
    def native_value(self) -> float | None:
        """Return the rate in dollars per kWh."""
        current = self._timeline.current
        return None if current is None else round(current.c_per_kwh / 100, 6)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Say which period it is and what the rate is after the discount."""
        current = self._timeline.current
        if current is None:
            return {}
        return {
            "period": current.period.name,
            "rate_after_discount": round(current.discounted_c_per_kwh / 100, 6),
        }


class TariffNextChangeSensor(TariffEntity):
    """When the period next changes, for a trigger a few minutes ahead of it."""

    _attr_translation_key = "tariff_next_change"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: MyEnergyCoordinator, timeline: TariffTimeline) -> None:
        """Create the sensor."""
        super().__init__(coordinator, timeline, "tariff_next_change")

    @property
    def native_value(self) -> datetime | None:
        """Return the start of the next different period."""
        upcoming = self._timeline.upcoming
        return None if upcoming is None else upcoming[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Name the period that starts then."""
        upcoming = self._timeline.upcoming
        following = upcoming[1] if upcoming else None
        return {"next_period": None if following is None else following.period.name}
