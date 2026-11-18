"""The coordinator's polling window, its handling of an ended session, and the keep-alive loop.

Only the portal (``MyEnergyApi``) and the session store are mocked. The recorder, the statistics
writer, the timers and the reauth flow are real, so the window really is read from stored
statistics and the keep-alive really is driven by Home Assistant's clock.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.citipower_myenergy.const import (
    CONF_KEEPALIVE_MINUTES,
    DEFAULT_KEEPALIVE_MINUTES,
    DOMAIN,
    KEEPALIVE_RETRY,
    MARKET_TZ,
    MAX_HISTORY_DAYS,
    REFRESH_WINDOW_DAYS,
)
from custom_components.citipower_myenergy.coordinator import MyEnergyCoordinator
from custom_components.citipower_myenergy.exceptions import MyEnergyAuthError, MyEnergyError
from custom_components.citipower_myenergy.models import AccountInfo
from custom_components.citipower_myenergy.nem12 import Channel
from custom_components.citipower_myenergy.schedule import CATCHUP_RETRY, FAILURE_RETRY

NMI = "61020000001"
START = datetime(2026, 10, 20, 0, 0, tzinfo=MARKET_TZ)
ACCOUNT_START = date(2026, 10, 1)
ACCOUNT_END = date(2026, 10, 21)
KEEPALIVE = timedelta(minutes=DEFAULT_KEEPALIVE_MINUTES)


def _channel(suffix: str, first: datetime, hours: int) -> Channel:
    channel = Channel(
        nmi=NMI[:10], suffix=suffix, meter_serial="M1", unit="KWH", interval_minutes=5
    )
    for index in range(hours):
        channel.hours[first + timedelta(hours=index)] = 1.0
    channel.days = {hour.date() for hour in channel.hours}
    return channel


def _coordinator(
    hass: HomeAssistant, *, start: date | None = ACCOUNT_START, options: dict | None = None
) -> MyEnergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"nmi": NMI}, options=options or {}, unique_id=NMI)
    entry.add_to_hass(hass)
    api = MagicMock()
    api.async_account_info = AsyncMock(
        return_value=AccountInfo(company="Citipower", start=start, end=ACCOUNT_END, has_access=True)
    )
    api.async_meter_data = AsyncMock(return_value={"E1": _channel("E1", START, 48)})
    api.async_session_seconds = AsyncMock(return_value=7000)
    api.client = MagicMock(aura=None, cookies={})
    store = MagicMock()
    store.suffixes.return_value = ["E1"]
    store.async_save_suffixes = AsyncMock()
    store.async_save_session = AsyncMock()
    return MyEnergyCoordinator(hass, entry, api, store, nmi=NMI, name="Home", signed_in_at=None)


def _requested(coordinator: MyEnergyCoordinator) -> tuple[date, date]:
    """Return the (start, end) the last meter-data request asked for."""
    nmi, company, start, end = coordinator.api.async_meter_data.call_args.args
    assert (nmi, company) == (NMI, "Citipower")
    return start, end


# --- The window each poll asks the portal for ---------------------------------------------


async def test_the_first_poll_backfills_from_the_start_of_the_account(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass)
    await coordinator._async_update_data()
    assert _requested(coordinator) == (ACCOUNT_START, ACCOUNT_END)


async def test_a_backfill_never_reaches_past_the_portal_limit(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass, start=date(2015, 1, 1))
    await coordinator._async_update_data()
    assert _requested(coordinator) == (ACCOUNT_END - timedelta(days=MAX_HISTORY_DAYS), ACCOUNT_END)


async def test_a_second_poll_rereads_a_week_before_the_newest_stored_hour(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass)
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    # The newest stored hour is 2026-10-21 23:00 AEST.
    assert coordinator.api.async_meter_data.await_count == 2
    assert REFRESH_WINDOW_DAYS == 7
    assert _requested(coordinator) == (date(2026, 10, 14), ACCOUNT_END)


async def test_the_window_is_counted_in_market_time_not_utc(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """05:00 AEST on the 21st is still the 20th in UTC; the window must start from the 21st."""
    coordinator = _coordinator(hass)
    coordinator.api.async_meter_data.return_value = {"E1": _channel("E1", START, 30)}
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert _requested(coordinator) == (date(2026, 10, 14), ACCOUNT_END)


async def test_the_window_never_starts_before_the_account_does(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass, start=date(2026, 10, 18))
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert _requested(coordinator) == (date(2026, 10, 18), ACCOUNT_END)


async def test_the_channel_with_the_oldest_data_sets_the_window(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass)
    coordinator._store.suffixes.return_value = ["E1", "B1"]
    coordinator.api.async_meter_data.return_value = {
        "E1": _channel("E1", START, 48),
        "B1": _channel("B1", datetime(2026, 10, 11, tzinfo=MARKET_TZ), 48),  # ends 2026-10-12
    }
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert _requested(coordinator) == (date(2026, 10, 5), ACCOUNT_END)


async def test_a_channel_with_nothing_stored_forces_a_full_backfill(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A newly seen channel must not be read from only the last week."""
    coordinator = _coordinator(hass)
    await coordinator._async_update_data()
    coordinator._store.suffixes.return_value = ["E1", "B1"]
    await coordinator._async_update_data()

    assert _requested(coordinator) == (ACCOUNT_START, ACCOUNT_END)


# --- An ended session ----------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["async_account_info", "async_meter_data"])
async def test_an_ended_session_asks_for_reauth(
    recorder_mock: Recorder, hass: HomeAssistant, failing: str
) -> None:
    coordinator = _coordinator(hass)
    getattr(coordinator.api, failing).side_effect = MyEnergyAuthError("session ended")

    with pytest.raises(ConfigEntryAuthFailed, match="session ended"):
        await coordinator._async_update_data()
    assert coordinator.session_dead
    assert coordinator.session_dead_since is not None


async def test_an_ended_session_stops_the_keepalive_loop(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass)
    coordinator.async_start_keepalive()
    assert coordinator._cancel_keepalive is not None

    coordinator.api.async_account_info.side_effect = MyEnergyAuthError("session ended")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert coordinator._cancel_keepalive is None


async def test_other_portal_errors_are_a_failed_poll_not_an_ended_session(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass)
    coordinator.api.async_meter_data.side_effect = MyEnergyError("the portal is down")

    with pytest.raises(UpdateFailed, match="the portal is down"):
        await coordinator._async_update_data()
    assert not coordinator.session_dead


# --- When the next poll is due (the catch-up window) ---------------------------------------


async def _poll_at(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    local: str,
    *,
    error: MyEnergyError | None = None,
) -> MyEnergyCoordinator:
    """Poll at a Melbourne wall-clock time; the fake portal's newest data is 2026-10-21.

    The coordinator's clock is replaced rather than frozen: the recorder is running, and a
    frozen clock in the past would stall the statistics write.
    """
    local_now = datetime.fromisoformat(f"{local}+11:00")
    monkeypatch.setattr(
        MyEnergyCoordinator, "_now", staticmethod(lambda tz=None: local_now if tz is None else local_now.astimezone(tz))
    )
    coordinator = _coordinator(hass)
    if error:
        coordinator.api.async_meter_data.side_effect = error
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    else:
        await coordinator._async_update_data()
    return coordinator


async def test_a_complete_day_sleeps_until_the_next_catch_up_window(
    recorder_mock: Recorder, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-22 03:00: yesterday (the 21st) is in, so nothing is due until 02:30 tomorrow.
    coordinator = await _poll_at(hass, monkeypatch, "2026-10-22T03:00:00")
    assert coordinator.update_interval == timedelta(hours=23, minutes=30)


async def test_a_missing_day_is_retried_every_ten_minutes_in_the_window(
    recorder_mock: Recorder, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-23 03:00: the 22nd has not arrived (the newest data is the 21st).
    coordinator = await _poll_at(hass, monkeypatch, "2026-10-23T03:00:00")
    assert coordinator.update_interval == CATCHUP_RETRY


async def test_a_missing_day_outside_the_window_waits_for_the_next_one(
    recorder_mock: Recorder, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-23 12:00: the window closed at 08:30, so wait for 02:30 tomorrow.
    coordinator = await _poll_at(hass, monkeypatch, "2026-10-23T12:00:00")
    assert coordinator.update_interval == timedelta(hours=14, minutes=30)


async def test_a_failed_poll_retries_soon_and_a_good_one_resets_the_backoff(
    recorder_mock: Recorder, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = await _poll_at(
        hass, monkeypatch, "2026-10-23T12:00:00", error=MyEnergyError("the portal is down")
    )
    assert coordinator.update_interval == FAILURE_RETRY
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == FAILURE_RETRY * 2

    coordinator.api.async_meter_data.side_effect = None
    await coordinator._async_update_data()
    assert coordinator._failures == 0
    assert coordinator.update_interval == timedelta(hours=14, minutes=30)


async def test_an_ended_session_schedules_no_further_poll(
    recorder_mock: Recorder, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _coordinator(hass)
    before = coordinator.update_interval
    coordinator.api.async_account_info.side_effect = MyEnergyAuthError("session ended")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert coordinator.update_interval == before  # untouched; reauth restarts the entry


# --- Keep-alive ----------------------------------------------------------------------------


async def _advance(hass: HomeAssistant, freezer, delta: timedelta) -> None:
    """Move the clock on and fire whatever timers are then due."""
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_a_keepalive_pings_the_portal_and_records_the_session(
    recorder_mock: Recorder, hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-11-16T00:00:00+00:00")
    coordinator = _coordinator(hass)
    coordinator.async_start_keepalive()

    await _advance(hass, freezer, KEEPALIVE + timedelta(seconds=1))

    coordinator.api.async_account_info.assert_awaited_once_with(NMI)
    assert coordinator.session_seconds == 7000
    assert coordinator.last_keepalive is not None
    coordinator._store.async_save_session.assert_awaited()
    assert not coordinator.session_dead

    # It keeps going: a second interval means a second ping.
    await _advance(hass, freezer, KEEPALIVE + timedelta(seconds=1))
    assert coordinator.api.async_account_info.await_count == 2


async def test_the_interval_comes_from_the_options(
    recorder_mock: Recorder, hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-11-16T00:00:00+00:00")
    coordinator = _coordinator(hass, options={CONF_KEEPALIVE_MINUTES: 10})
    coordinator.async_start_keepalive()

    await _advance(hass, freezer, timedelta(minutes=5))
    coordinator.api.async_account_info.assert_not_awaited()
    await _advance(hass, freezer, timedelta(minutes=6))
    coordinator.api.async_account_info.assert_awaited_once()


async def test_a_ping_is_skipped_when_a_poll_just_did_the_job(
    recorder_mock: Recorder, hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-11-16T00:00:00+00:00")
    coordinator = _coordinator(hass)
    coordinator.async_start_keepalive()

    # A poll 30 minutes in touches the portal, so the idle clock restarts.
    await _advance(hass, freezer, timedelta(minutes=30))
    await coordinator.async_refresh()
    coordinator.api.async_account_info.reset_mock()

    # The keep-alive timer falls due (by 45 minutes at the latest) with the portal 15 idle.
    await _advance(hass, freezer, timedelta(minutes=15, seconds=1))
    coordinator.api.async_account_info.assert_not_awaited()
    assert coordinator.last_keepalive is None

    # It waits out only what is owed (about 30 minutes), then pings.
    await _advance(hass, freezer, timedelta(minutes=25))
    coordinator.api.async_account_info.assert_not_awaited()
    await _advance(hass, freezer, timedelta(minutes=6))
    coordinator.api.async_account_info.assert_awaited_once_with(NMI)
    assert coordinator.last_keepalive is not None


async def test_an_ended_session_found_by_a_ping_starts_reauth_and_stops_the_loop(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    custom_integration,
    freezer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    freezer.move_to("2026-11-16T00:00:00+00:00")
    coordinator = _coordinator(hass)
    coordinator.api.async_account_info.side_effect = MyEnergyAuthError("session ended")
    coordinator.async_start_keepalive()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    with caplog.at_level(logging.WARNING):
        await _advance(hass, freezer, KEEPALIVE + timedelta(seconds=1))

    assert coordinator.session_dead
    assert coordinator._cancel_keepalive is None
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    assert "has ended; sign in again" in caplog.text

    # Hours later it has not pinged again: the loop is really stopped. (Stop short of the 4-hourly
    # poll, which is not the keep-alive and would call the portal itself.)
    await _advance(hass, freezer, timedelta(hours=3))
    assert coordinator.api.async_account_info.await_count == 1
    coordinator.async_start_keepalive()  # and a dead session cannot restart it
    assert coordinator._cancel_keepalive is None


@pytest.mark.parametrize(
    "error", [MyEnergyError("a blip"), RuntimeError("something odd")], ids=["portal", "unexpected"]
)
async def test_other_ping_failures_retry_after_the_short_delay(
    recorder_mock: Recorder, hass: HomeAssistant, freezer, error: Exception
) -> None:
    freezer.move_to("2026-11-16T00:00:00+00:00")
    coordinator = _coordinator(hass)
    coordinator.api.async_account_info.side_effect = error
    coordinator.async_start_keepalive()

    await _advance(hass, freezer, KEEPALIVE + timedelta(seconds=1))
    assert coordinator.api.async_account_info.await_count == 1
    assert not coordinator.session_dead
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    # Not before KEEPALIVE_RETRY (2 minutes), and then again, rather than a whole interval later.
    assert KEEPALIVE_RETRY == timedelta(minutes=2)
    await _advance(hass, freezer, timedelta(minutes=1))
    assert coordinator.api.async_account_info.await_count == 1
    await _advance(hass, freezer, timedelta(minutes=1, seconds=5))
    assert coordinator.api.async_account_info.await_count == 2

    # Once the portal answers again the normal interval resumes.
    coordinator.api.async_account_info.side_effect = None
    await _advance(hass, freezer, KEEPALIVE_RETRY + timedelta(seconds=5))
    assert coordinator.api.async_account_info.await_count == 3
    assert coordinator.last_keepalive is not None
    await _advance(hass, freezer, timedelta(minutes=30))
    assert coordinator.api.async_account_info.await_count == 3


async def test_stopping_the_keepalive_cancels_the_timer(
    recorder_mock: Recorder, hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-11-16T00:00:00+00:00")
    coordinator = _coordinator(hass)
    coordinator.async_start_keepalive()
    coordinator.async_stop_keepalive()

    await _advance(hass, freezer, timedelta(hours=3))
    coordinator.api.async_account_info.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["ok", "ended", "blip"])
async def test_halting_during_a_ping_leaves_nothing_running(
    recorder_mock: Recorder, hass: HomeAssistant, freezer, outcome: str
) -> None:
    """An unload while the ping is in flight must not reschedule, save or start a reauth."""
    freezer.move_to("2026-11-16T00:00:00+00:00")
    coordinator = _coordinator(hass)
    gate = asyncio.Event()

    async def _slow(_nmi):
        await gate.wait()
        if outcome == "ended":
            raise MyEnergyAuthError("session ended")
        if outcome == "blip":
            raise MyEnergyError("a blip")
        return MagicMock()

    coordinator.api.async_account_info = AsyncMock(side_effect=_slow)
    coordinator.async_start_keepalive()
    freezer.tick(KEEPALIVE + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await asyncio.sleep(0)  # the ping is now waiting on the gate
    assert coordinator.api.async_account_info.await_count == 1

    coordinator.async_halt_keepalive()
    gate.set()
    await hass.async_block_till_done()

    assert coordinator._cancel_keepalive is None
    coordinator._store.async_save_session.assert_not_awaited()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    await _advance(hass, freezer, timedelta(hours=3))
    assert coordinator.api.async_account_info.await_count == 1


async def test_a_halted_keepalive_cannot_be_started_again(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass)
    coordinator.async_halt_keepalive()
    coordinator.async_start_keepalive()
    assert coordinator._cancel_keepalive is None


async def test_a_recovered_session_restarts_the_keepalive(
    recorder_mock: Recorder, hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-11-16T00:00:00+00:00")
    coordinator = _coordinator(hass)
    coordinator.async_start_keepalive()
    coordinator.api.async_account_info.side_effect = MyEnergyAuthError("401")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert coordinator.session_dead and coordinator._cancel_keepalive is None

    coordinator.api.async_account_info.side_effect = None
    await coordinator._async_update_data()
    assert not coordinator.session_dead
    assert coordinator._cancel_keepalive is not None  # pinging again

    await _advance(hass, freezer, KEEPALIVE + timedelta(seconds=1))
    assert coordinator.last_keepalive is not None


async def test_a_recovery_after_unload_does_not_restart_the_keepalive(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass)
    coordinator._mark_dead()
    coordinator.async_halt_keepalive()
    coordinator._touched()
    assert coordinator._cancel_keepalive is None


@pytest.mark.parametrize("name", ["Home", NMI], ids=["named", "named-by-nmi"])
async def test_a_failed_cost_write_logs_no_nmi(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    """Unexpected and pricing failures name the site (or 'this meter'), never the NMI."""
    from unittest.mock import patch

    from custom_components.citipower_myenergy.exceptions import TariffError
    from custom_components.citipower_myenergy.tariff import flat_tariff

    caplog.set_level("DEBUG")
    coordinator = _coordinator(hass)
    coordinator.site_name = name
    channel = _channel("E1", START, 2)
    tariff = flat_tariff(30.0, effective_from=date(2026, 1, 1))
    target = "custom_components.citipower_myenergy.coordinator.async_insert_cost"
    for error in (RuntimeError("boom"), TariffError("bad price")):
        with patch(target, AsyncMock(side_effect=error)):
            await coordinator._async_insert_cost(channel, tariff)
    period = "custom_components.citipower_myenergy.coordinator.async_insert_period_statistics"
    for error in (RuntimeError("boom"), TariffError("bad price")):
        with patch(period, AsyncMock(side_effect=error)):
            await coordinator._async_insert_period_statistics(channel, tariff)

    assert "Unexpected error recording" in caplog.text
    assert len(caplog.records) >= 4
    for record in caplog.records:
        assert NMI not in record.getMessage() + (record.exc_text or "")
