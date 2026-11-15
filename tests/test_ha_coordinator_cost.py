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

from custom_components.citipower_myenergy.const import CONF_TARIFF, DOMAIN, MARKET_TZ
from custom_components.citipower_myenergy.coordinator import MyEnergyCoordinator
from custom_components.citipower_myenergy.models import AccountInfo
from custom_components.citipower_myenergy.nem12 import Channel
from custom_components.citipower_myenergy.statistics import cost_statistic_id_for, statistic_id_for
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
