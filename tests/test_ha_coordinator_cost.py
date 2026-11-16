"""The coordinator records cost only when a tariff is saved, and never breaks the kWh import."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.citipower_myenergy.const import (
    CONF_PERIOD_STATS,
    CONF_SUPPLY_MODE,
    CONF_TARIFF,
    DOMAIN,
    MARKET_TZ,
)
from custom_components.citipower_myenergy.coordinator import MyEnergyCoordinator
from custom_components.citipower_myenergy.exceptions import TariffError
from custom_components.citipower_myenergy.models import AccountInfo
from custom_components.citipower_myenergy.nem12 import Channel
from custom_components.citipower_myenergy.statistics import (
    cost_statistic_id_for,
    period_statistic_ids,
    statistic_id_for,
)
from custom_components.citipower_myenergy.tariff import flat_tariff

NMI = "61020000001"
START = datetime(2026, 10, 20, 0, 0, tzinfo=MARKET_TZ)


def _channels() -> dict[str, Channel]:
    channel = Channel(nmi=NMI[:10], suffix="E1", meter_serial="M1", unit="KWH", interval_minutes=5)
    for index in range(48):  # two whole days, 1 kWh an hour
        channel.hours[START + timedelta(hours=index)] = 1.0
    channel.days = {date(2026, 10, 20), date(2026, 10, 21)}
    return {"E1": channel}


def _coordinator(hass: HomeAssistant, options: dict) -> MyEnergyCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"nmi": NMI}, options=options, unique_id=NMI)
    entry.add_to_hass(hass)
    api = MagicMock()
    api.async_account_info = AsyncMock(
        return_value=AccountInfo(
            company="Citipower", start=date(2026, 10, 1), end=date(2026, 10, 21), has_access=True
        )
    )
    api.async_meter_data = AsyncMock(return_value=_channels())
    api.client = MagicMock(aura=None, cookies={})
    store = MagicMock()
    store.suffixes.return_value = ["E1"]
    store.async_save_suffixes = AsyncMock()
    store.async_save_session = AsyncMock()
    return MyEnergyCoordinator(hass, entry, api, store, nmi=NMI, name="Home", signed_in_at=None)


async def _rows(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return rows.get(statistic_id, [])


async def test_no_tariff_means_no_cost_statistics(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass, options={})
    data = await coordinator._async_update_data()
    assert data.hours_added == 48
    assert len(await _rows(hass, statistic_id_for(NMI, "E1"))) == 48
    assert await _rows(hass, cost_statistic_id_for(NMI, "E1")) == []


async def test_a_saved_tariff_records_cost_alongside_kwh(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    tariff = flat_tariff(10.0, effective_from=date(2026, 1, 1)).to_dict()
    coordinator = _coordinator(hass, options={CONF_TARIFF: tariff})
    data = await coordinator._async_update_data()
    assert data.hours_added == 48  # still counts kWh hours
    assert len(await _rows(hass, statistic_id_for(NMI, "E1"))) == 48
    cost = await _rows(hass, cost_statistic_id_for(NMI, "E1"))
    assert len(cost) == 48
    assert cost[-1]["sum"] == pytest.approx(48 * 0.10, abs=1e-6)


async def test_a_second_poll_changes_nothing(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    tariff = flat_tariff(10.0, effective_from=date(2026, 1, 1)).to_dict()
    coordinator = _coordinator(hass, options={CONF_TARIFF: tariff})
    await coordinator._async_update_data()
    before = await _rows(hass, cost_statistic_id_for(NMI, "E1"))
    coordinator.api.async_meter_data = AsyncMock(return_value=_channels())
    await coordinator._async_update_data()
    assert await _rows(hass, cost_statistic_id_for(NMI, "E1")) == before


async def test_a_broken_saved_tariff_is_ignored_but_the_import_continues(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _coordinator(hass, options={CONF_TARIFF: {"schema": 99, "versions": []}})
    with caplog.at_level(logging.WARNING):
        data = await coordinator._async_update_data()
    assert data.hours_added == 48
    assert "Ignoring the saved tariff" in caplog.text
    assert await _rows(hass, cost_statistic_id_for(NMI, "E1")) == []


async def test_a_failure_while_pricing_does_not_stop_the_kwh_import(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    tariff = flat_tariff(10.0, effective_from=date(2026, 1, 1)).to_dict()
    coordinator = _coordinator(hass, options={CONF_TARIFF: tariff})
    boom = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch("custom_components.citipower_myenergy.coordinator.async_insert_cost", boom),
        caplog.at_level(logging.ERROR),
    ):
        data = await coordinator._async_update_data()
    assert data.hours_added == 48
    assert len(await _rows(hass, statistic_id_for(NMI, "E1"))) == 48
    # Logged with the traceback, not swallowed silently.
    assert "Unexpected error recording E1 cost" in caplog.text
    assert "RuntimeError: boom" in caplog.text


# --- Per-period statistics ------------------------------------------------------------------
#
# A flat tariff has one period, "All day", whose slug is all_day: 10 c/kWh on 1 kWh an hour
# for 48 hours is 48 kWh and $4.80, with no supply charge or discount.


def _period_ids() -> tuple[str, str]:
    return period_statistic_ids(NMI, "all_day")


def _flat_options(**extra: object) -> dict:
    return {CONF_TARIFF: flat_tariff(10.0, effective_from=date(2026, 1, 1)).to_dict(), **extra}


async def test_period_statistics_are_off_by_default(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass, options=_flat_options())
    await coordinator._async_update_data()
    kwh_id, cost_id = _period_ids()
    assert await _rows(hass, kwh_id) == []
    assert await _rows(hass, cost_id) == []
    assert len(await _rows(hass, cost_statistic_id_for(NMI, "E1"))) == 48  # combined cost is on


async def test_period_statistics_are_written_when_the_option_is_on(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass, options=_flat_options(**{CONF_PERIOD_STATS: True}))
    data = await coordinator._async_update_data()
    assert data.hours_added == 48  # still counts the kWh hours only
    kwh_id, cost_id = _period_ids()
    kwh, cost = await _rows(hass, kwh_id), await _rows(hass, cost_id)
    assert len(kwh) == len(cost) == 48
    assert kwh[-1]["sum"] == pytest.approx(48.0)
    assert cost[-1]["sum"] == pytest.approx(48 * 0.10, abs=1e-6)


async def test_the_option_without_a_tariff_writes_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass, options={CONF_PERIOD_STATS: True})
    data = await coordinator._async_update_data()
    assert data.hours_added == 48
    assert await _rows(hass, _period_ids()[0]) == []


async def test_turning_the_option_on_later_backfills_and_leaves_combined_alone(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass, options=_flat_options())
    await coordinator._async_update_data()
    combined_cost = await _rows(hass, cost_statistic_id_for(NMI, "E1"))
    combined_kwh = await _rows(hass, statistic_id_for(NMI, "E1"))
    assert await _rows(hass, _period_ids()[1]) == []

    hass.config_entries.async_update_entry(
        coordinator.config_entry, options=_flat_options(**{CONF_PERIOD_STATS: True})
    )
    coordinator.api.async_meter_data = AsyncMock(return_value=_channels())
    await coordinator._async_update_data()
    assert len(await _rows(hass, _period_ids()[1])) == 48  # the whole history, not just a window
    assert await _rows(hass, cost_statistic_id_for(NMI, "E1")) == combined_cost
    assert await _rows(hass, statistic_id_for(NMI, "E1")) == combined_kwh


async def test_a_second_poll_with_the_option_on_changes_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    coordinator = _coordinator(hass, options=_flat_options(**{CONF_PERIOD_STATS: True}))
    await coordinator._async_update_data()
    before = [await _rows(hass, statistic_id) for statistic_id in _period_ids()]
    coordinator.api.async_meter_data = AsyncMock(return_value=_channels())
    await coordinator._async_update_data()
    assert [await _rows(hass, statistic_id) for statistic_id in _period_ids()] == before


async def test_a_failure_in_period_statistics_does_not_stop_the_import_or_the_cost(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _coordinator(hass, options=_flat_options(**{CONF_PERIOD_STATS: True}))
    boom = AsyncMock(side_effect=RuntimeError("boom"))
    with (
        patch(
            "custom_components.citipower_myenergy.coordinator.async_insert_period_statistics", boom
        ),
        caplog.at_level(logging.ERROR),
    ):
        data = await coordinator._async_update_data()
    assert data.hours_added == 48
    assert len(await _rows(hass, statistic_id_for(NMI, "E1"))) == 48
    assert len(await _rows(hass, cost_statistic_id_for(NMI, "E1"))) == 48
    assert "Unexpected error recording E1 per-period statistics" in caplog.text
    assert "RuntimeError: boom" in caplog.text  # logged with the traceback


async def test_a_pricing_problem_in_period_statistics_is_a_warning(
    recorder_mock: Recorder, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _coordinator(hass, options=_flat_options(**{CONF_PERIOD_STATS: True}))
    bad = AsyncMock(side_effect=TariffError("no period covers Monday 00:00"))
    with (
        patch(
            "custom_components.citipower_myenergy.coordinator.async_insert_period_statistics", bad
        ),
        caplog.at_level(logging.WARNING),
    ):
        data = await coordinator._async_update_data()
    assert data.hours_added == 48
    assert "Could not price E1 per period" in caplog.text
    assert "no period covers Monday 00:00" in caplog.text


async def test_the_coordinator_passes_the_supply_mode_to_the_period_statistics(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    for stored_mode, expected in ((None, "none"), ("spread", "spread"), ("bogus", "none")):
        extra: dict = {CONF_PERIOD_STATS: True}
        if stored_mode is not None:
            extra[CONF_SUPPLY_MODE] = stored_mode
        coordinator = _coordinator(hass, options=_flat_options(**extra))
        spy = AsyncMock(return_value=0)
        with patch(
            "custom_components.citipower_myenergy.coordinator.async_insert_period_statistics", spy
        ):
            await coordinator._async_update_data()
        assert spy.await_args.kwargs == {"supply_mode": expected}, stored_mode
