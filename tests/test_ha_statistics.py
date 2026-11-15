"""Tests for the statistics import (needs pytest-homeassistant-custom-component)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.citipower_myenergy.const import MARKET_TZ
from custom_components.citipower_myenergy.nem12 import Channel
from custom_components.citipower_myenergy.statistics import (
    async_insert_channel,
    async_insert_cost,
    async_recalculate_costs,
    cost_statistic_id_for,
    statistic_id_for,
)
from custom_components.citipower_myenergy.tariff import (
    Period,
    Tariff,
    TariffVersion,
    Tier,
    Window,
    flat_tariff,
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


# --- Cost statistics --------------------------------------------------------------------
#
# The GLOHOME Flexible Rate (CitiPower, inc. GST): supply 92.40 c/day, 3% discount on supply
# and usage, import 4pm-9pm 31.46 c, 9pm-11am 16.50 c, 11am-4pm 13.20 c. One kWh in every
# hour of an AEST day, worked by hand (cents x 0.97 / 100, to a millionth of a dollar):
#   peak hour    31.46 x 0.97 = 30.5162 c  -> $0.305162   (16:00-21:00, 5 hours)
#   off-peak     16.50 x 0.97 = 16.005  c  -> $0.16005    (21:00-11:00, 14 hours)
#   shoulder     13.20 x 0.97 = 12.804  c  -> $0.12804    (11:00-16:00, 5 hours)
#   midnight hour also carries supply: (16.50 + 92.40) x 0.97 = 105.633 c -> $1.05633
#   a whole day = 1.05633 + 13 x 0.16005 + 5 x 0.305162 + 5 x 0.12804 = $5.30299

PEAK, OFFPEAK, SHOULDER, MIDNIGHT = 0.305162, 0.16005, 0.12804, 1.05633
DAY_COST = 5.30299
D0 = date(2026, 1, 1)


def _glohome() -> Tariff:
    return Tariff(
        (
            TariffVersion(
                effective_from=D0,
                supply_c_per_day=92.40,
                discount_pct=3.0,
                import_periods=(
                    Period("Peak", (Window(16, 21),), (Tier(None, 31.46),)),
                    Period("Off-peak", (Window(21, 11),), (Tier(None, 16.50),)),
                    Period("Shoulder", (Window(11, 16),), (Tier(None, 13.20),)),
                ),
            ),
        )
    )


def _day_states() -> list[float]:
    """The 24 hourly costs of a day with 1 kWh in every hour."""
    out = []
    for hour in range(24):
        if hour == 0:
            out.append(MIDNIGHT)
        elif 16 <= hour < 21:
            out.append(PEAK)
        elif 11 <= hour < 16:
            out.append(SHOULDER)
        else:
            out.append(OFFPEAK)
    return out


async def _stored_cost(hass: HomeAssistant, suffix: str = "E1") -> list[dict]:
    await async_wait_recording_done(hass)
    statistic_id = cost_statistic_id_for(NMI, suffix)
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


def _assert_consistent(rows: list[dict]) -> None:
    """Every sum must be exactly the running total of the states: nothing double-counted."""
    running = 0.0
    for row in rows:
        running += row["state"]
        assert row["sum"] == pytest.approx(running, abs=1e-5)


def test_cost_statistic_ids() -> None:
    assert cost_statistic_id_for("61020000001", "E1") == "citipower_myenergy:61020000001_e1_cost"
    assert (
        cost_statistic_id_for("61020000001", "B1")
        == "citipower_myenergy:61020000001_b1_compensation"
    )


async def test_cost_first_import_prices_every_whole_day(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0] * 48)
    assert await async_insert_cost(hass, NMI, "Home", channel, _glohome()) == 48
    rows = await _stored_cost(hass)
    assert [row["state"] for row in rows] == pytest.approx(_day_states() * 2, abs=1e-9)
    assert rows[23]["sum"] == pytest.approx(DAY_COST, abs=1e-6)
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST, abs=1e-6)
    _assert_consistent(rows)


async def test_cost_incremental_import_adds_only_new_days(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 24), _glohome())
    await async_wait_recording_done(hass)
    added = await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 48), _glohome())
    assert added == 24
    rows = await _stored_cost(hass)
    assert len(rows) == 48
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST, abs=1e-6)
    _assert_consistent(rows)


async def test_cost_rerun_changes_nothing(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    channel = _channel([1.0] * 48)
    await async_insert_cost(hass, NMI, "Home", channel, _glohome())
    before = await _stored_cost(hass)
    assert await async_insert_cost(hass, NMI, "Home", channel, _glohome()) == 0
    assert await _stored_cost(hass) == before


async def test_cost_follows_a_corrected_reading_without_double_counting(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """An estimate replaced by an actual reprices that day and everything after it."""
    await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 48), _glohome())
    await async_wait_recording_done(hass)

    values = [1.0] * 48
    values[12] = 3.0  # 12:00 on day one, a shoulder hour: two extra kWh
    assert await async_insert_cost(hass, NMI, "Home", _channel(values), _glohome()) == 48
    rows = await _stored_cost(hass)
    assert len(rows) == 48  # rewritten in place, not appended
    assert rows[12]["state"] == pytest.approx(3 * SHOULDER, abs=1e-9)
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST + 2 * SHOULDER, abs=1e-6)
    _assert_consistent(rows)


async def test_cost_correction_on_day_two_leaves_day_one_alone(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 48), _glohome())
    day_one = (await _stored_cost(hass))[:24]
    values = [1.0] * 48
    values[24 + 17] = 2.0  # 17:00 on day two, a peak hour
    assert await async_insert_cost(hass, NMI, "Home", _channel(values), _glohome()) == 24
    rows = await _stored_cost(hass)
    assert rows[:24] == day_one
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST + PEAK, abs=1e-6)
    _assert_consistent(rows)


async def test_cost_tariff_change_reprices_the_window(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0] * 48)
    await async_insert_cost(hass, NMI, "Home", channel, _glohome())
    await async_wait_recording_done(hass)
    cheaper = flat_tariff(10.0, effective_from=D0)  # 10 c in every hour, no supply or discount
    assert await async_insert_cost(hass, NMI, "Home", channel, cheaper) == 48
    rows = await _stored_cost(hass)
    assert len(rows) == 48
    assert rows[-1]["sum"] == pytest.approx(48 * 0.10, abs=1e-6)
    _assert_consistent(rows)


async def test_cost_tariff_version_boundary_prices_each_day_with_its_own_version(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    old = flat_tariff(20.0, effective_from=D0).versions[0]
    new = flat_tariff(30.0, effective_from=date(2026, 10, 21)).versions[0]  # day two onwards
    await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 48), Tariff((old, new)))
    rows = await _stored_cost(hass)
    assert rows[23]["sum"] == pytest.approx(24 * 0.20, abs=1e-6)
    assert rows[-1]["sum"] == pytest.approx(24 * 0.20 + 24 * 0.30, abs=1e-6)
    assert rows[24]["state"] == pytest.approx(0.30)


async def test_cost_partial_days_write_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    # Ten hours of a past day: the rest of it is missing.
    assert await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 10), _glohome()) == 0
    # A day still in progress must not be priced either, however many hours it has.
    today = datetime.combine(dt_util.now(MARKET_TZ).date(), time(0), tzinfo=MARKET_TZ)
    in_progress = _channel([1.0] * 24, start=today)
    assert await async_insert_cost(hass, NMI, "Home", in_progress, _glohome()) == 0
    assert await _stored_cost(hass) == []


async def test_cost_skipped_day_is_filled_in_later(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A day that arrives after the one following it is slotted in with sums recalculated."""
    only_day_two = _channel([1.0] * 24, start=START + timedelta(days=1))
    await async_insert_cost(hass, NMI, "Home", only_day_two, _glohome())
    await async_wait_recording_done(hass)

    assert await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 48), _glohome()) == 48
    rows = await _stored_cost(hass)
    assert len(rows) == 48
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST, abs=1e-6)
    _assert_consistent(rows)


async def test_cost_first_run_prices_the_stored_kwh_history(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Switching a tariff on later backfills cost for kWh already stored, not just this poll."""
    await async_insert_channel(hass, NMI, "Home", _channel([1.0] * 48))
    await async_wait_recording_done(hass)
    recent = _channel([1.0] * 24, start=START + timedelta(days=1))
    assert await async_insert_cost(hass, NMI, "Home", recent, _glohome()) == 48
    rows = await _stored_cost(hass)
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST, abs=1e-6)


async def test_cost_recalculate_reprices_from_a_chosen_day(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await async_insert_channel(hass, NMI, "Home", _channel([1.0] * 48))
    await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 48), _glohome())
    await async_wait_recording_done(hass)
    day_one = (await _stored_cost(hass))[:24]

    cheaper = flat_tariff(10.0, effective_from=D0)
    written = await async_recalculate_costs(hass, NMI, "Home", "E1", cheaper, date(2026, 10, 21))
    assert written == 24
    rows = await _stored_cost(hass)
    assert rows[:24] == day_one  # before the chosen day nothing moved
    assert rows[-1]["sum"] == pytest.approx(DAY_COST + 24 * 0.10, abs=1e-6)
    _assert_consistent(rows)

    # Repricing from the first day starts the running total again from zero.
    written = await async_recalculate_costs(hass, NMI, "Home", "E1", cheaper, date(2026, 10, 20))
    assert written == 48
    rows = await _stored_cost(hass)
    assert len(rows) == 48
    assert rows[-1]["sum"] == pytest.approx(48 * 0.10, abs=1e-6)


async def test_cost_unit_is_the_configured_currency(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    hass.config.currency = "AUD"
    await async_insert_cost(hass, NMI, "Home", _channel([1.0] * 24), _glohome())
    await async_wait_recording_done(hass)
    listed = await hass.async_add_executor_job(list_statistic_ids, hass)
    wanted = cost_statistic_id_for(NMI, "E1")
    entry = next(item for item in listed if item["statistic_id"] == wanted)
    assert entry["statistics_unit_of_measurement"] == "AUD"
    assert entry["has_sum"] is True
    assert entry["name"] == "Home grid import cost"


async def test_cost_only_prices_e1_and_b1(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    controlled_load = _channel([1.0] * 24, suffix="E2")
    assert await async_insert_cost(hass, NMI, "Home", controlled_load, _glohome()) == 0
    # No export periods in the tariff: export is not priced either.
    export = _channel([1.0] * 24, suffix="B1")
    assert await async_insert_cost(hass, NMI, "Home", export, _glohome()) == 0
    assert await _stored_cost(hass, "E2") == []
    assert await _stored_cost(hass, "B1") == []


async def test_cost_export_is_positive_compensation(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    tariff = flat_tariff(10.0, effective_from=D0, export_c_per_kwh=5.0)
    export = _channel([0.5] * 24, suffix="B1")
    assert await async_insert_cost(hass, NMI, "Home", export, tariff) == 24
    rows = await _stored_cost(hass, "B1")
    assert rows[0]["state"] == pytest.approx(0.5 * 0.05)  # 0.5 kWh at 5 c, no supply charge
    assert rows[-1]["sum"] == pytest.approx(24 * 0.025, abs=1e-6)  # positive: money earned


async def test_cost_leaves_the_kwh_statistic_alone(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0, 2.0, 3.0] * 16)
    await async_insert_channel(hass, NMI, "Home", channel)
    kwh_before = await _stored(hass)
    await async_insert_cost(hass, NMI, "Home", channel, _glohome())
    assert await _stored(hass) == kwh_before
