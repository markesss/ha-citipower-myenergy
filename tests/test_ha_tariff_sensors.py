"""The live tariff sensors: current period, current rate, when the period next changes.

The integration is set up for real (sensor platform and all) with the portal client, the
session store and the polling replaced, so the sensors are the only thing under test. Time is
frozen; a period boundary is crossed by moving the clock and firing the timers.

16 November 2026 is a Monday; Melbourne is on daylight saving (AEDT, UTC+11), so 11:30 local
is 00:30 UTC and Peak (16:00 local) starts at 05:00 UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.citipower_myenergy.const import CONF_TARIFF, DOMAIN
from custom_components.citipower_myenergy.coordinator import MyEnergyCoordinator, MyEnergyData
from custom_components.citipower_myenergy.models import AccountInfo
from custom_components.citipower_myenergy.sensor import TariffTimeline
from custom_components.citipower_myenergy.tariff import SCAN_HOURS, Tariff, flat_tariff

NMI = "6001234567"
PACKAGE = "custom_components.citipower_myenergy"
ACCOUNT = AccountInfo(
    company="Citipower", start=date(2026, 10, 1), end=date(2026, 10, 21), has_access=True
)


def _period(name: str, start: int, end: int, rate: float) -> dict:
    return {
        "name": name,
        "windows": [{"start": start, "end": end}],
        "tiers": [{"up_to_kwh_per_day": None, "c_per_kwh": rate}],
    }


def _glohome() -> dict:
    return Tariff.from_dict(
        {
            "schema": 1,
            "versions": [
                {
                    "effective_from": "2026-01-01",
                    "timezone": "Australia/Melbourne",
                    "supply_c_per_day": 92.4,
                    "discount_pct": 3,
                    "import_periods": [
                        _period("Peak", 16, 21, 31.46),
                        _period("Solar Soak", 11, 16, 13.2),
                        _period("Off Peak", 21, 11, 16.5),
                    ],
                }
            ],
        }
    ).to_dict()


@pytest.fixture
def portal() -> Iterator[None]:
    """Replace everything that talks to the portal, and the polling itself."""
    with (
        patch(f"{PACKAGE}.MyEnergyApi"),
        patch(f"{PACKAGE}.MyEnergyAuraClient"),
        patch(f"{PACKAGE}.async_create_session", new=AsyncMock()),
        patch(f"{PACKAGE}.SessionStore") as store,
        patch.object(
            MyEnergyCoordinator,
            "_async_update_data",
            AsyncMock(return_value=MyEnergyData(account=ACCOUNT)),
        ),
        patch.object(MyEnergyCoordinator, "async_start_keepalive"),
    ):
        store.return_value.async_load = AsyncMock()
        store.return_value.cookies.return_value = {"sid": "not-a-real-session"}
        store.return_value.context.return_value = None
        yield


async def _setup(hass: HomeAssistant, options: dict) -> MockConfigEntry:
    hass.config.currency = "AUD"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NMI,
        data={"nmi": NMI, "cookies": {"sid": "not-a-real-session"}},
        options=options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _id(hass: HomeAssistant, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{NMI}_{key}")


async def _move_to(hass: HomeAssistant, freezer, text: str) -> None:
    moment = datetime.fromisoformat(text).replace(tzinfo=UTC)
    freezer.move_to(moment)
    async_fire_time_changed(hass, moment)
    await hass.async_block_till_done()


async def test_no_tariff_means_no_tariff_sensors(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal, freezer
) -> None:
    freezer.move_to("2026-11-16T00:30:00+00:00")
    await _setup(hass, {})
    for key in ("tariff_period", "tariff_rate", "tariff_next_change"):
        assert _id(hass, key) is None
    assert _id(hass, "latest_data") is not None  # the existing sensors are unaffected


async def test_the_sensors_show_the_period_and_rate_now(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal, freezer
) -> None:
    freezer.move_to("2026-11-16T00:30:00+00:00")  # 11:30 AEDT
    await _setup(hass, {CONF_TARIFF: _glohome()})

    period = hass.states.get(_id(hass, "tariff_period"))
    assert period.state == "Solar Soak"
    assert period.attributes["options"] == ["Peak", "Solar Soak", "Off Peak"]
    assert period.attributes["rate_c_per_kwh"] == 13.2
    assert period.attributes["rate_after_discount_c_per_kwh"] == pytest.approx(13.2 * 0.97)
    assert period.attributes["tiered"] is False
    assert period.attributes["period_slug"] == "solar_soak"
    assert period.attributes["next_period"] == "Peak"
    assert period.attributes["next_rate_c_per_kwh"] == 31.46
    assert datetime.fromisoformat(period.attributes["next_period_starts"]) == datetime(
        2026, 11, 16, 5, 0, tzinfo=UTC
    )

    rate = hass.states.get(_id(hass, "tariff_rate"))
    assert float(rate.state) == pytest.approx(0.132)
    assert rate.attributes["unit_of_measurement"] == "AUD/kWh"
    assert rate.attributes["state_class"] == "measurement"
    assert rate.attributes["period"] == "Solar Soak"
    assert rate.attributes["rate_after_discount"] == pytest.approx(0.132 * 0.97)

    change = hass.states.get(_id(hass, "tariff_next_change"))
    assert datetime.fromisoformat(change.state) == datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
    assert change.attributes["next_period"] == "Peak"


async def test_the_sensors_update_at_the_boundary(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal, freezer
) -> None:
    freezer.move_to("2026-11-16T00:30:00+00:00")
    await _setup(hass, {CONF_TARIFF: _glohome()})

    await _move_to(hass, freezer, "2026-11-16T05:00:00")  # 16:00 AEDT
    assert hass.states.get(_id(hass, "tariff_period")).state == "Peak"
    assert float(hass.states.get(_id(hass, "tariff_rate")).state) == pytest.approx(0.3146)
    assert datetime.fromisoformat(
        hass.states.get(_id(hass, "tariff_next_change")).state
    ) == datetime(2026, 11, 16, 10, 0, tzinfo=UTC)

    # The next boundary was set up by the first, so it keeps going without help.
    await _move_to(hass, freezer, "2026-11-16T10:00:00")  # 21:00 AEDT
    period = hass.states.get(_id(hass, "tariff_period"))
    assert period.state == "Off Peak"
    assert period.attributes["next_period"] == "Solar Soak"
    assert datetime.fromisoformat(period.attributes["next_period_starts"]) == datetime(
        2026, 11, 17, 0, 0, tzinfo=UTC
    )  # 11:00 AEDT tomorrow: midnight does not end Off Peak


async def test_saving_a_new_tariff_changes_the_sensors(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal, freezer
) -> None:
    freezer.move_to("2026-11-16T00:30:00+00:00")
    entry = await _setup(hass, {CONF_TARIFF: _glohome()})
    flat = flat_tariff(25.0, effective_from=date(2026, 1, 1), timezone="Australia/Melbourne")
    hass.config_entries.async_update_entry(entry, options={CONF_TARIFF: flat.to_dict()})
    await hass.async_block_till_done()  # the options listener reloads the entry

    assert hass.states.get(_id(hass, "tariff_period")).state == "All day"
    assert float(hass.states.get(_id(hass, "tariff_rate")).state) == pytest.approx(0.25)
    # A flat tariff never changes.
    assert hass.states.get(_id(hass, "tariff_next_change")).state == STATE_UNKNOWN


async def test_removing_the_tariff_removes_the_sensors(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal, freezer
) -> None:
    freezer.move_to("2026-11-16T00:30:00+00:00")
    entry = await _setup(hass, {CONF_TARIFF: _glohome()})
    hass.config_entries.async_update_entry(entry, options={})
    await hass.async_block_till_done()
    for key in ("tariff_period", "tariff_rate", "tariff_next_change"):
        assert hass.states.get(_id(hass, key)).state == STATE_UNAVAILABLE


async def test_unloading_cancels_the_timer(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal, freezer
) -> None:
    freezer.move_to("2026-11-16T00:30:00+00:00")
    cancel = MagicMock()
    with patch(f"{PACKAGE}.sensor.async_track_point_in_utc_time", return_value=cancel) as track:
        entry = await _setup(hass, {CONF_TARIFF: _glohome()})
        assert track.call_count == 1  # one timer however many entities listen
        assert track.call_args.args[2] == datetime(2026, 11, 16, 5, 0, tzinfo=UTC)
        cancel.assert_not_called()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    cancel.assert_called_once()


async def test_a_flat_tariff_looks_again_before_the_scan_horizon(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal, freezer
) -> None:
    """With nothing to wait for, a later tariff version must still be noticed in time."""
    freezer.move_to("2026-11-16T00:30:00+00:00")
    flat = flat_tariff(25.0, effective_from=date(2026, 1, 1), timezone="Australia/Melbourne")
    with patch(
        f"{PACKAGE}.sensor.async_track_point_in_utc_time", return_value=MagicMock()
    ) as track:
        await _setup(hass, {CONF_TARIFF: flat.to_dict()})
    assert track.call_args.args[2] == datetime(2026, 11, 16, 0, 30, tzinfo=UTC) + timedelta(
        hours=SCAN_HOURS - 24
    )


async def test_a_later_version_is_picked_up_when_the_timer_looks_again(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal, freezer
) -> None:
    freezer.move_to("2026-12-10T00:00:00+00:00")
    first = flat_tariff(25.0, effective_from=date(2026, 1, 1), timezone="Australia/Brisbane")
    later = flat_tariff(26.0, effective_from=date(2026, 12, 25), timezone="Australia/Brisbane")
    tariff = Tariff((first.versions[0], later.versions[0]))
    await _setup(hass, {CONF_TARIFF: tariff.to_dict()})
    assert hass.states.get(_id(hass, "tariff_next_change")).state == STATE_UNKNOWN  # 15 days away

    await _move_to(hass, freezer, "2026-12-17T00:00:00")  # 7 days on: the change is in view
    change = hass.states.get(_id(hass, "tariff_next_change"))
    assert datetime.fromisoformat(change.state) == datetime(2026, 12, 24, 14, 0, tzinfo=UTC)


async def test_the_timeline_stops_cleanly(hass: HomeAssistant) -> None:
    """Stopping twice, or listening after a stop, schedules nothing."""
    tariff = flat_tariff(25.0, effective_from=date(2026, 1, 1))
    timeline = TariffTimeline(hass, tariff)
    timeline.async_stop()
    timeline.async_stop()
    with patch(f"{PACKAGE}.sensor.async_track_point_in_utc_time") as track:
        remove = timeline.async_add_listener(lambda: None)
        remove()
    track.assert_not_called()
