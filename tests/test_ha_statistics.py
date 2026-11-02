"""Tests for the statistics import (needs pytest-homeassistant-custom-component)."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.citipower_myenergy.const import MARKET_TZ
from custom_components.citipower_myenergy.nem12 import Channel
from custom_components.citipower_myenergy.statistics import (
    async_insert_channel,
    statistic_id_for,
)

NMI = "61020000001"
START = datetime(2026, 10, 20, 0, 0, tzinfo=MARKET_TZ)


def _channel(values: list[float], start: datetime = START, suffix: str = "E1") -> Channel:
    channel = Channel(
        nmi=NMI[:10], suffix=suffix, meter_serial="M1", unit="KWH", interval_minutes=5
    )
    for index, value in enumerate(values):
        channel.hours[start + timedelta(hours=index)] = value
    return channel


async def _stored(hass: HomeAssistant, suffix: str = "E1") -> list[dict]:
    await async_wait_recording_done(hass)
    statistic_id = statistic_id_for(NMI, suffix)
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


def test_statistic_id() -> None:
    assert statistic_id_for("61020000001", "E1") == "citipower_myenergy:61020000001_e1"


async def test_first_import(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    added = await async_insert_channel(hass, NMI, "Home", _channel([1.0, 2.0, 3.0]))
    assert added == 3
    rows = await _stored(hass)
    assert [row["state"] for row in rows] == [1.0, 2.0, 3.0]
    assert [row["sum"] for row in rows] == [1.0, 3.0, 6.0]


async def test_overlapping_import_appends_only_new_hours(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await async_insert_channel(hass, NMI, "Home", _channel([1.0, 2.0]))
    await async_wait_recording_done(hass)
    added = await async_insert_channel(hass, NMI, "Home", _channel([1.0, 2.0, 4.0]))
    assert added == 1
    rows = await _stored(hass)
    assert [row["sum"] for row in rows] == [1.0, 3.0, 7.0]


async def test_changed_reading_rewrites_from_that_hour(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An estimate replaced by an actual reading corrects every later sum."""
    await async_insert_channel(hass, NMI, "Home", _channel([1.0, 2.0, 3.0]))
    await async_wait_recording_done(hass)
    await async_insert_channel(hass, NMI, "Home", _channel([1.0, 5.0, 3.0]))
    rows = await _stored(hass)
    assert [row["state"] for row in rows] == [1.0, 5.0, 3.0]
    assert [row["sum"] for row in rows] == [1.0, 6.0, 9.0]


async def test_export_is_a_separate_statistic(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    await async_insert_channel(hass, NMI, "Home", _channel([1.0], suffix="E1"))
    await async_insert_channel(hass, NMI, "Home", _channel([0.5], suffix="B1"))
    assert [row["sum"] for row in await _stored(hass, "E1")] == [1.0]
    assert [row["sum"] for row in await _stored(hass, "B1")] == [0.5]
