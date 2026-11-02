"""Sensors for myEnergy.

The Energy dashboard reads the imported statistics directly; these sensors
are for seeing at a glance how fresh the data is and whether the session is
still alive.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADDRESS, CONF_COMPANY, DOMAIN, MARKET_TZ
from .coordinator import MyEnergyConfigEntry, MyEnergyCoordinator
from .statistics import channel_label


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

    async_add_entities(entities)
    _add_channels()
    entry.async_on_unload(coordinator.async_add_listener(_add_channels))


class MyEnergyEntity(CoordinatorEntity[MyEnergyCoordinator]):
    """Common device information."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MyEnergyCoordinator, key: str) -> None:
        """Attach to the NMI's device."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{coordinator.nmi}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.nmi)},
            name=f"myEnergy {coordinator.site_name}",
            manufacturer=entry.data.get(CONF_COMPANY) or "CitiPower / Powercor",
            model="Smart meter",
            serial_number=coordinator.nmi,
            suggested_area=None,
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://myenergy.powercor.com.au/s/energy-usage",
        )
        self._address = entry.data.get(CONF_ADDRESS)


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
