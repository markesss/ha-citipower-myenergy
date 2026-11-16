"""Tests for the statistics import (needs pytest-homeassistant-custom-component)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

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

from custom_components.citipower_myenergy.const import (
    MARKET_TZ,
    SUPPLY_FIRST_HOUR,
    SUPPLY_NONE,
    SUPPLY_SPREAD,
)
from custom_components.citipower_myenergy.nem12 import Channel
from custom_components.citipower_myenergy.statistics import (
    async_insert_channel,
    async_insert_cost,
    async_insert_period_statistics,
    async_recalculate_costs,
    cost_statistic_id_for,
    period_statistic_ids,
    statistic_id_for,
)
from custom_components.citipower_myenergy.tariff import (
    Period,
    Tariff,
    TariffVersion,
    Tier,
    Window,
    daily_supply_dollars,
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


# --- Per-period statistics ------------------------------------------------------------------
#
# The same GLOHOME tariff split by period (slugs peak, off_peak, shoulder), 1 kWh in every
# hour of two AEST days. Per period, hand-worked (cents x 0.97 / 100):
#   peak      10 kWh (5 h x 2 days)  cost 10 x 0.305162 = $3.05162
#   off_peak  28 kWh (14 h x 2 days) cost 28 x 0.16005  = $4.48140
#   shoulder  10 kWh (5 h x 2 days)  cost 10 x 0.12804  = $1.28040
# No supply charge in any of them: that is 2 x $0.89628 = $1.79256, so the periods plus supply
# come to $10.60598 = 2 x DAY_COST, the combined statistic's total.

SLUGS = ("peak", "off_peak", "shoulder")
PERIOD_KWH = {"peak": 10.0, "off_peak": 28.0, "shoulder": 10.0}
PERIOD_COST = {"peak": 3.05162, "off_peak": 4.4814, "shoulder": 1.2804}
SUPPLY_DAY = 0.89628


async def _rows_of(hass: HomeAssistant, statistic_id: str) -> list[dict]:
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


async def _period_rows(hass: HomeAssistant, slugs=SLUGS) -> dict[str, list[dict]]:
    """Return {statistic id: rows} for every period statistic, kWh and cost."""
    out = {}
    for slug in slugs:
        for statistic_id in period_statistic_ids(NMI, slug):
            out[statistic_id] = await _rows_of(hass, statistic_id)
    return out


def _pair(rows: dict[str, list[dict]], slug: str) -> tuple[list[dict], list[dict]]:
    kwh_id, cost_id = period_statistic_ids(NMI, slug)
    return rows[kwh_id], rows[cost_id]


def test_period_statistic_ids() -> None:
    assert period_statistic_ids("61020000001", "peak") == (
        "citipower_myenergy:61020000001_e1_peak",
        "citipower_myenergy:61020000001_e1_peak_cost",
    )


async def test_period_first_import_splits_kwh_and_cost_by_period(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0] * 48)
    assert await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome()) == 48
    rows = await _period_rows(hass)
    for slug in SLUGS:
        kwh, cost = _pair(rows, slug)
        assert len(kwh) == len(cost) == 48  # a row for every hour
        assert kwh[-1]["sum"] == pytest.approx(PERIOD_KWH[slug], abs=1e-6)
        assert cost[-1]["sum"] == pytest.approx(PERIOD_COST[slug], abs=1e-6)
        _assert_consistent(kwh)
        _assert_consistent(cost)
    # Hour by hour: 17:00 is peak only, 12:00 shoulder only, midnight off-peak only.
    peak_kwh, peak_cost = _pair(rows, "peak")
    assert (peak_kwh[17]["state"], peak_cost[17]["state"]) == (1.0, pytest.approx(PEAK))
    assert (peak_kwh[12]["state"], peak_cost[12]["state"]) == (0.0, 0.0)
    off_kwh, off_cost = _pair(rows, "off_peak")
    assert (off_kwh[0]["state"], off_cost[0]["state"]) == (1.0, pytest.approx(OFFPEAK))
    # The supply charge is in no period's cost: midnight is plain off-peak here.
    assert off_cost[0]["state"] < MIDNIGHT


async def test_period_statistics_carry_names_unit_and_currency(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    hass.config.currency = "AUD"
    await async_insert_period_statistics(hass, NMI, "Home", _channel([1.0] * 24), _glohome())
    await async_wait_recording_done(hass)
    listed = {
        item["statistic_id"]: item
        for item in await hass.async_add_executor_job(list_statistic_ids, hass)
    }
    kwh_id, cost_id = period_statistic_ids(NMI, "off_peak")
    assert listed[kwh_id]["name"] == "Home grid import Off-peak"
    assert listed[kwh_id]["statistics_unit_of_measurement"] == "kWh"
    assert listed[kwh_id]["unit_class"] == "energy"
    assert listed[cost_id]["name"] == "Home grid import Off-peak cost"
    assert listed[cost_id]["statistics_unit_of_measurement"] == "AUD"
    assert listed[cost_id]["has_sum"] is True
    assert listed[cost_id]["unit_class"] is None


async def test_period_incremental_import_adds_only_new_days(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await async_insert_period_statistics(hass, NMI, "Home", _channel([1.0] * 24), _glohome())
    await async_wait_recording_done(hass)
    added = await async_insert_period_statistics(
        hass, NMI, "Home", _channel([1.0] * 48), _glohome()
    )
    assert added == 24
    rows = await _period_rows(hass)
    for slug in SLUGS:
        kwh, cost = _pair(rows, slug)
        assert len(kwh) == len(cost) == 48
        assert cost[-1]["sum"] == pytest.approx(PERIOD_COST[slug], abs=1e-6)
        _assert_consistent(cost)


async def test_period_rerun_changes_nothing(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    channel = _channel([1.0] * 48)
    await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome())
    before = await _period_rows(hass)
    assert await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome()) == 0
    assert await _period_rows(hass) == before


async def test_period_corrected_reading_rewrites_every_period_without_double_counting(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await async_insert_period_statistics(hass, NMI, "Home", _channel([1.0] * 48), _glohome())
    await async_wait_recording_done(hass)

    values = [1.0] * 48
    values[12] = 3.0  # 12:00 on day one is a shoulder hour: two extra kWh
    channel = _channel(values)
    written = await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome())
    assert written == 48  # all of day one is rewritten, then day two
    rows = await _period_rows(hass)
    for slug in SLUGS:
        kwh, cost = _pair(rows, slug)
        assert len(kwh) == len(cost) == 48  # rewritten in place, not appended
        _assert_consistent(kwh)
        _assert_consistent(cost)
    shoulder_kwh, shoulder_cost = _pair(rows, "shoulder")
    assert shoulder_kwh[12]["state"] == 3.0
    assert shoulder_kwh[-1]["sum"] == pytest.approx(PERIOD_KWH["shoulder"] + 2, abs=1e-6)
    assert shoulder_cost[-1]["sum"] == pytest.approx(
        PERIOD_COST["shoulder"] + 2 * SHOULDER, abs=1e-6
    )
    # The other periods hold what they held: only the shoulder changed value.
    off_kwh, _ = _pair(rows, "off_peak")
    assert off_kwh[-1]["sum"] == pytest.approx(PERIOD_KWH["off_peak"], abs=1e-6)


async def test_period_correction_on_day_two_leaves_day_one_alone(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await async_insert_period_statistics(hass, NMI, "Home", _channel([1.0] * 48), _glohome())
    before = await _period_rows(hass)
    values = [1.0] * 48
    values[24 + 17] = 2.0  # 17:00 on day two, a peak hour
    channel = _channel(values)
    assert await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome()) == 24
    rows = await _period_rows(hass)
    for statistic_id, stored in rows.items():
        assert stored[:24] == before[statistic_id][:24]
    _, peak_cost = _pair(rows, "peak")
    assert peak_cost[-1]["sum"] == pytest.approx(PERIOD_COST["peak"] + PEAK, abs=1e-6)


async def test_period_versions_with_different_periods_write_zero_for_the_missing_ones(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Day one is flat; day two is the three-period plan. Every statistic still has 48 rows."""
    old = flat_tariff(20.0, effective_from=D0).versions[0]  # one period, "All day"
    new = replace(_glohome().versions[0], effective_from=date(2026, 10, 21))  # day two onwards
    tariff = Tariff((old, new))
    channel = _channel([1.0] * 48)
    assert await async_insert_period_statistics(hass, NMI, "Home", channel, tariff) == 48
    every = ("all_day", *SLUGS)
    rows = await _period_rows(hass, every)
    all_kwh, all_cost = _pair(rows, "all_day")
    assert len(all_kwh) == 48
    assert [row["state"] for row in all_kwh[:24]] == [1.0] * 24  # day one: everything
    assert [row["state"] for row in all_kwh[24:]] == [0.0] * 24  # day two: nothing
    assert all_cost[23]["sum"] == pytest.approx(24 * 0.20, abs=1e-6)
    assert all_cost[-1]["sum"] == pytest.approx(24 * 0.20, abs=1e-6)  # flat for day one only
    for slug in SLUGS:
        kwh, cost = _pair(rows, slug)
        assert [row["state"] for row in kwh[:24]] == [0.0] * 24  # absent on day one
        assert kwh[-1]["sum"] == pytest.approx(PERIOD_KWH[slug] / 2, abs=1e-6)  # day two only
        assert cost[-1]["sum"] == pytest.approx(PERIOD_COST[slug] / 2, abs=1e-6)
    # Nothing is lost: each hour's kWh lands in exactly one period.
    for hour in range(48):
        assert sum(_pair(rows, slug)[0][hour]["state"] for slug in every) == 1.0


async def test_period_adding_a_period_backfills_its_history_and_orphans_the_old_one(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    await async_insert_channel(hass, NMI, "Home", _channel([1.0] * 48))
    await async_insert_period_statistics(hass, NMI, "Home", _channel([1.0] * 48), _glohome())
    await async_wait_recording_done(hass)
    old_off_peak = await _period_rows(hass, ("off_peak",))

    renamed = Tariff(
        (
            TariffVersion(
                effective_from=D0,
                supply_c_per_day=92.40,
                discount_pct=3.0,
                import_periods=(
                    Period("Peak", (Window(16, 21),), (Tier(None, 31.46),)),
                    Period("Night", (Window(21, 11),), (Tier(None, 16.50),)),  # was Off-peak
                    Period("Shoulder", (Window(11, 16),), (Tier(None, 13.20),)),
                ),
            ),
        )
    )
    # A poll that only carries the last day: the new "night" statistic is filled from the
    # stored kWh history anyway, and the others are rewritten together with it.
    recent = _channel([1.0] * 24, start=START + timedelta(days=1))
    assert await async_insert_period_statistics(hass, NMI, "Home", recent, renamed) == 48
    rows = await _period_rows(hass, ("night", "peak", "shoulder"))
    night_kwh, night_cost = _pair(rows, "night")
    assert len(night_kwh) == len(night_cost) == 48
    assert night_kwh[-1]["sum"] == pytest.approx(PERIOD_KWH["off_peak"], abs=1e-6)
    assert night_cost[-1]["sum"] == pytest.approx(PERIOD_COST["off_peak"], abs=1e-6)
    _assert_consistent(night_cost)
    # The renamed-away statistic is left alone: not deleted and not written to.
    assert await _period_rows(hass, ("off_peak",)) == old_off_peak


async def test_period_split_agrees_with_the_combined_statistics(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Periods' cost sums plus the supply charge equal the combined cost, per hour and in total."""
    values = [((i * 7) % 11) / 3 for i in range(96)]  # four days of uneven usage
    channel = _channel(values)
    await async_insert_channel(hass, NMI, "Home", channel)
    await async_insert_cost(hass, NMI, "Home", channel, _glohome())
    await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome())
    rows = await _period_rows(hass)
    combined_cost = await _stored_cost(hass)
    combined_kwh = await _stored(hass)

    days = 4
    period_cost_total = sum(_pair(rows, slug)[1][-1]["sum"] for slug in SLUGS)
    assert period_cost_total + days * SUPPLY_DAY == pytest.approx(
        combined_cost[-1]["sum"], abs=1e-4
    )
    period_kwh_total = sum(_pair(rows, slug)[0][-1]["sum"] for slug in SLUGS)
    assert period_kwh_total == pytest.approx(combined_kwh[-1]["sum"], abs=1e-4)

    for hour in range(96):
        supply = SUPPLY_DAY if hour % 24 == 0 else 0.0
        periods = sum(_pair(rows, slug)[1][hour]["state"] for slug in SLUGS)
        assert periods + supply == pytest.approx(combined_cost[hour]["state"], abs=2e-6)
        kwh = sum(_pair(rows, slug)[0][hour]["state"] for slug in SLUGS)
        assert kwh == pytest.approx(combined_kwh[hour]["state"], abs=1e-6)
    assert daily_supply_dollars(_glohome().versions[0]) == pytest.approx(SUPPLY_DAY)


async def test_period_writing_leaves_the_combined_statistics_alone(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0, 2.0, 3.0] * 16)
    await async_insert_channel(hass, NMI, "Home", channel)
    await async_insert_cost(hass, NMI, "Home", channel, _glohome())
    kwh_before, cost_before = await _stored(hass), await _stored_cost(hass)
    await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome())
    assert await _stored(hass) == kwh_before
    assert await _stored_cost(hass) == cost_before


async def test_period_first_run_prices_the_stored_kwh_history(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Switching the option on later backfills everything the dashboard already has."""
    await async_insert_channel(hass, NMI, "Home", _channel([1.0] * 48))
    await async_wait_recording_done(hass)
    recent = _channel([1.0] * 24, start=START + timedelta(days=1))
    assert await async_insert_period_statistics(hass, NMI, "Home", recent, _glohome()) == 48
    rows = await _period_rows(hass)
    for slug in SLUGS:
        assert _pair(rows, slug)[1][-1]["sum"] == pytest.approx(PERIOD_COST[slug], abs=1e-6)


async def test_period_recalculate_reprices_from_a_day_and_only_when_asked(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0] * 48)
    await async_insert_channel(hass, NMI, "Home", channel)
    await async_insert_cost(hass, NMI, "Home", channel, _glohome())
    await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome())
    await async_wait_recording_done(hass)
    before = await _period_rows(hass)

    # Same period names, new rate everywhere, no supply charge or discount.
    cheaper = Tariff(
        (
            TariffVersion(
                effective_from=D0,
                import_periods=(
                    Period("Peak", (Window(16, 21),), (Tier(None, 10.0),)),
                    Period("Off-peak", (Window(21, 11),), (Tier(None, 10.0),)),
                    Period("Shoulder", (Window(11, 16),), (Tier(None, 10.0),)),
                ),
            ),
        )
    )
    from_day = date(2026, 10, 21)  # day two
    # Without the flag the combined cost is repriced and the period statistics are not.
    await async_recalculate_costs(hass, NMI, "Home", "E1", cheaper, from_day)
    assert await _period_rows(hass) == before

    written = await async_recalculate_costs(
        hass, NMI, "Home", "E1", cheaper, from_day, period_statistics=True
    )
    assert written == 24  # the combined cost's hours
    rows = await _period_rows(hass)
    for slug in SLUGS:
        kwh, cost = _pair(rows, slug)
        assert kwh == _pair(before, slug)[0]  # readings did not change
        assert cost[:24] == _pair(before, slug)[1][:24]  # day one untouched
        _assert_consistent(cost)
    after = sum(_pair(rows, slug)[1][-1]["sum"] for slug in SLUGS)
    day_one = sum(_pair(before, slug)[1][23]["sum"] for slug in SLUGS)
    assert after - day_one == pytest.approx(24 * 0.10, abs=1e-6)  # no discount, no supply


async def test_period_only_splits_e1_and_never_a_partial_day(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    export = _channel([1.0] * 24, suffix="B1")
    assert await async_insert_period_statistics(hass, NMI, "Home", export, _glohome()) == 0
    partial = _channel([1.0] * 10)
    assert await async_insert_period_statistics(hass, NMI, "Home", partial, _glohome()) == 0
    assert all(not rows for rows in (await _period_rows(hass)).values())


# --- The tariff clock: daylight saving days, zone changes ------------------------------------
#
# Melbourne clocks go back on Sunday 5 April 2026 (that day has 25 hours: 02:00 happens twice)
# and forward on Sunday 4 October 2026 (23 hours: 02:00 does not exist). Costs are priced a
# whole local day at a time, so these days must have exactly the hours they really have.
#
# GLOHOME with 1 kWh in every hour, by local hour (see above): a 24-hour day is $5.30299.
#   25-hour day: one more off-peak hour (the repeated 2am)    5.30299 + 0.16005 = $5.46304
#   23-hour day: one fewer off-peak hour (the skipped 2am)    5.30299 - 0.16005 = $5.14294

MELBOURNE = ZoneInfo("Australia/Melbourne")
FALL_BACK = date(2026, 4, 5)
SPRING_FORWARD = date(2026, 10, 4)
LONG_DAY, SHORT_DAY = 5.46304, 5.14294


def _glohome_in(zone: str | None) -> Tariff:
    return Tariff((replace(_glohome().versions[0], timezone=zone),))


def _local_midnight(day: date, tz) -> datetime:
    """The UTC instant a local day starts (hour arithmetic must be done on UTC datetimes)."""
    return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)


def _expected_hour_cost(moment: datetime, tz) -> float:
    """GLOHOME's cost of 1 kWh in an hour, restated from the fact sheet (an independent check)."""
    local = moment.astimezone(tz)
    cents = 31.46 if 16 <= local.hour < 21 else 13.20 if 11 <= local.hour < 16 else 16.50
    if (moment - timedelta(hours=1)).astimezone(tz).date() != local.date():
        cents += 92.40  # the supply charge rides on the first hour of each local day
    return round(cents * 0.97 / 100, 6)


def _rows_by_hour(rows: list[dict]) -> dict[datetime, dict]:
    return {dt_util.utc_from_timestamp(row["start"]): row for row in rows}


async def test_cost_a_25_hour_day_is_priced_with_all_25_hours(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    # 4, 5 (25 hours) and 6 April: 24 + 25 + 24 real hours.
    start = _local_midnight(date(2026, 4, 4), MELBOURNE)
    channel = _channel([1.0] * 73, start)
    tariff = _glohome_in("Australia/Melbourne")
    assert await async_insert_cost(hass, NMI, "Home", channel, tariff) == 73
    rows = await _stored_cost(hass)
    assert len(rows) == 73
    for moment, row in _rows_by_hour(rows).items():
        assert row["state"] == pytest.approx(_expected_hour_cost(moment, MELBOURNE), abs=1e-9)
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST + LONG_DAY, abs=1e-5)
    # Both 02:00 hours (15:00Z AEDT, 16:00Z AEST) are off-peak; supply is charged once that day.
    by_hour = _rows_by_hour(rows)
    assert by_hour[datetime(2026, 4, 4, 15, tzinfo=UTC)]["state"] == pytest.approx(OFFPEAK)
    assert by_hour[datetime(2026, 4, 4, 16, tzinfo=UTC)]["state"] == pytest.approx(OFFPEAK)
    assert sum(1 for row in rows if row["state"] > 0.5) == 3  # one supply charge per day
    _assert_consistent(rows)


async def test_cost_a_23_hour_day_is_priced_with_its_23_hours(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    start = _local_midnight(date(2026, 10, 3), MELBOURNE)
    channel = _channel([1.0] * 71, start)  # 24 + 23 + 24
    tariff = _glohome_in("Australia/Melbourne")
    assert await async_insert_cost(hass, NMI, "Home", channel, tariff) == 71
    rows = await _stored_cost(hass)
    assert len(rows) == 71
    for moment, row in _rows_by_hour(rows).items():
        assert row["state"] == pytest.approx(_expected_hour_cost(moment, MELBOURNE), abs=1e-9)
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST + SHORT_DAY, abs=1e-5)
    _assert_consistent(rows)


async def test_cost_a_day_with_the_wrong_number_of_hours_is_not_written(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """24 readings do not make a whole 25-hour day, so nothing is written for it."""
    start = _local_midnight(FALL_BACK, MELBOURNE)
    channel = _channel([1.0] * 24, start)
    tariff = _glohome_in("Australia/Melbourne")
    assert await async_insert_cost(hass, NMI, "Home", channel, tariff) == 0
    assert await _stored_cost(hass) == []


async def test_cost_rerun_across_daylight_saving_changes_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    tariff = _glohome_in("Australia/Melbourne")
    channel = _channel([1.0] * 73, _local_midnight(date(2026, 4, 4), MELBOURNE))
    await async_insert_cost(hass, NMI, "Home", channel, tariff)
    before = await _stored_cost(hass)
    assert await async_insert_cost(hass, NMI, "Home", channel, tariff) == 0
    assert await _stored_cost(hass) == before


@pytest.mark.parametrize("zone", [None, "Australia/Brisbane"])
async def test_cost_fixed_aest_is_unaffected_by_daylight_saving(
    recorder_mock: Recorder, hass: HomeAssistant, zone: str | None
) -> None:
    """No zone and Brisbane both mean the old behaviour: every day is exactly 24 hours."""
    start = _local_midnight(date(2026, 4, 4), MARKET_TZ)
    channel = _channel([1.0] * 72, start)
    assert await async_insert_cost(hass, NMI, "Home", channel, _glohome_in(zone)) == 72
    rows = await _stored_cost(hass)
    assert [row["state"] for row in rows] == pytest.approx(_day_states() * 3, abs=1e-9)
    assert rows[-1]["sum"] == pytest.approx(3 * DAY_COST, abs=1e-5)


async def test_cost_gmt_prices_whole_utc_days(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    channel = _channel([1.0] * 48, datetime(2026, 11, 9, tzinfo=UTC))
    assert await async_insert_cost(hass, NMI, "Home", channel, _glohome_in("GMT")) == 48
    by_hour = _rows_by_hour(await _stored_cost(hass))
    assert by_hour[datetime(2026, 11, 9, tzinfo=UTC)]["state"] == pytest.approx(MIDNIGHT)
    assert by_hour[datetime(2026, 11, 9, 16, tzinfo=UTC)]["state"] == pytest.approx(PEAK)
    assert by_hour[datetime(2026, 11, 9, 15, tzinfo=UTC)]["state"] == pytest.approx(SHOULDER)


async def test_cost_changing_the_zone_reprices_and_keeps_the_running_total_consistent(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Fixed AEST to Melbourne in summer moves every day edge and window by an hour."""
    channel = _channel([1.0] * 48, datetime(2026, 11, 9, tzinfo=UTC))
    await async_insert_cost(hass, NMI, "Home", channel, _glohome_in(None))
    by_hour = _rows_by_hour(await _stored_cost(hass))
    at_0500 = datetime(2026, 11, 10, 5, tzinfo=UTC)
    assert by_hour[at_0500]["state"] == pytest.approx(SHOULDER)  # 3pm AEST
    await async_wait_recording_done(hass)

    await async_insert_cost(hass, NMI, "Home", channel, _glohome_in("Australia/Melbourne"))
    rows = await _stored_cost(hass)
    by_hour = _rows_by_hour(rows)
    assert by_hour[at_0500]["state"] == pytest.approx(PEAK)  # now 4pm AEDT
    # The Melbourne day 10 Nov is 13:00Z on the 9th to 12:00Z on the 10th: priced afresh.
    for hour in range(24):
        moment = datetime(2026, 11, 9, 13, tzinfo=UTC) + timedelta(hours=hour)
        assert by_hour[moment]["state"] == pytest.approx(
            _expected_hour_cost(moment, MELBOURNE), abs=1e-9
        )
    # The hour after it still holds its old price, but its running total is re-chained, so no
    # hour shows a spike or a dip while it waits for the next day to complete.
    tail = datetime(2026, 11, 10, 13, tzinfo=UTC)
    assert tail in by_hour and max(by_hour) == tail
    assert by_hour[tail]["state"] == pytest.approx(OFFPEAK)
    _assert_consistent(rows)

    # When the next Melbourne day completes, that hour is priced properly (it is its midnight).
    longer = _channel([1.0] * 72, datetime(2026, 11, 9, tzinfo=UTC))
    await async_wait_recording_done(hass)
    await async_insert_cost(hass, NMI, "Home", longer, _glohome_in("Australia/Melbourne"))
    rows = await _stored_cost(hass)
    assert len(rows) == 48
    assert _rows_by_hour(rows)[tail]["state"] == pytest.approx(MIDNIGHT)
    assert rows[-1]["sum"] == pytest.approx(2 * DAY_COST, abs=1e-5)
    _assert_consistent(rows)


async def test_cost_recalculate_moves_all_history_to_a_new_zone(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The explicit recalculation (what the options flow runs) reprices every stored day."""
    start = datetime(2026, 3, 30, tzinfo=UTC)  # spans the April fall-back
    channel = _channel([1.0] * (24 * 14), start)
    await async_insert_channel(hass, NMI, "Home", channel)
    await async_insert_cost(hass, NMI, "Home", channel, _glohome_in(None))
    await async_wait_recording_done(hass)

    melbourne = _glohome_in("Australia/Melbourne")
    far_past = date(2000, 1, 1)
    await async_recalculate_costs(hass, NMI, "Home", "E1", melbourne, far_past)
    rows = await _stored_cost(hass)
    by_hour = _rows_by_hour(rows)
    # Every row inside a complete Melbourne day is priced on the Melbourne clock.
    complete_start = _local_midnight(date(2026, 3, 31), MELBOURNE)
    complete_end = _local_midnight(date(2026, 4, 13), MELBOURNE)
    moment = complete_start
    while moment < complete_end:
        assert by_hour[moment]["state"] == pytest.approx(
            _expected_hour_cost(moment, MELBOURNE), abs=1e-9
        ), moment
        moment += timedelta(hours=1)
    _assert_consistent(rows)
    # The 25-hour day is in there, whole.
    day_start = _local_midnight(FALL_BACK, MELBOURNE)
    day_end = _local_midnight(date(2026, 4, 6), MELBOURNE)
    assert sum(1 for m in by_hour if day_start <= m < day_end) == 25

    # Running it again changes nothing.
    again = await async_recalculate_costs(hass, NMI, "Home", "E1", melbourne, far_past)
    assert again > 0
    assert await _stored_cost(hass) == rows

    # Following polls agree with it, so a normal import writes nothing.
    assert await async_insert_cost(hass, NMI, "Home", channel, melbourne) == 0
    assert await _stored_cost(hass) == rows


async def test_period_split_agrees_with_the_combined_statistics_across_daylight_saving(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Periods' costs plus the supply charge equal the combined cost on 23 and 25 hour days."""
    tariff = _glohome_in("Australia/Melbourne")
    for first_day, hours in ((date(2026, 4, 4), 73), (date(2026, 10, 3), 71)):
        values = [((i * 7) % 11) / 3 + 0.25 for i in range(hours)]
        channel = _channel(values, _local_midnight(first_day, MELBOURNE))
        await async_insert_channel(hass, NMI, "Home", channel)
        await async_insert_cost(hass, NMI, "Home", channel, tariff)
        await async_insert_period_statistics(hass, NMI, "Home", channel, tariff)
    rows = await _period_rows(hass)
    combined_cost = await _stored_cost(hass)
    combined_kwh = await _stored(hass)
    assert len(combined_cost) == 73 + 71

    days = 6
    period_cost_total = sum(_pair(rows, slug)[1][-1]["sum"] for slug in SLUGS)
    assert period_cost_total + days * SUPPLY_DAY == pytest.approx(
        combined_cost[-1]["sum"], abs=1e-4
    )
    assert sum(_pair(rows, slug)[0][-1]["sum"] for slug in SLUGS) == pytest.approx(
        combined_kwh[-1]["sum"], abs=1e-4
    )
    # Hour by hour: only the first hour of each local day carries the supply charge.
    first_hours = {_local_midnight(date(2026, 4, d), MELBOURNE) for d in (4, 5, 6)} | {
        _local_midnight(date(2026, 10, d), MELBOURNE) for d in (3, 4, 5)
    }
    for index, row in enumerate(combined_cost):
        moment = dt_util.utc_from_timestamp(row["start"])
        supply = SUPPLY_DAY if moment in first_hours else 0.0
        periods = sum(_pair(rows, slug)[1][index]["state"] for slug in SLUGS)
        assert periods + supply == pytest.approx(row["state"], abs=2e-6), moment


# --- Where the supply charge goes in the period costs -------------------------------------


async def _write_all(hass: HomeAssistant, channel: Channel, tariff: Tariff, mode: str) -> None:
    await async_insert_channel(hass, NMI, "Home", channel)
    await async_insert_cost(hass, NMI, "Home", channel, tariff)
    await async_insert_period_statistics(hass, NMI, "Home", channel, tariff, supply_mode=mode)


def _cost_state(rows: dict[str, list[dict]], hour: int) -> float:
    return sum(_pair(rows, slug)[1][hour]["state"] for slug in SLUGS)


@pytest.mark.parametrize("mode", [SUPPLY_SPREAD, SUPPLY_FIRST_HOUR])
async def test_supply_mode_period_costs_add_up_to_the_combined_cost_every_hour(
    recorder_mock: Recorder, hass: HomeAssistant, mode: str
) -> None:
    values = [((i * 7) % 11) / 3 for i in range(96)]  # four days of uneven usage
    channel = _channel(values)
    await _write_all(hass, channel, _glohome(), mode)
    rows = await _period_rows(hass)
    combined_cost = await _stored_cost(hass)
    for day in range(4):  # every day adds up, whichever hours carry the supply charge
        hours = range(day * 24, day * 24 + 24)
        assert sum(_cost_state(rows, h) for h in hours) == pytest.approx(
            sum(combined_cost[h]["state"] for h in hours), abs=1e-4
        )
    if mode == SUPPLY_FIRST_HOUR:  # and then it is the combined cost, hour for hour
        for hour in range(96):
            assert _cost_state(rows, hour) == pytest.approx(combined_cost[hour]["state"], abs=3e-6)
    assert sum(_pair(rows, slug)[1][-1]["sum"] for slug in SLUGS) == pytest.approx(
        combined_cost[-1]["sum"], abs=1e-4
    )
    for slug in SLUGS:
        _assert_consistent(_pair(rows, slug)[1])
    # kWh never depends on the mode.
    kwh = await _stored(hass)
    for hour in range(96):
        assert sum(_pair(rows, slug)[0][hour]["state"] for slug in SLUGS) == pytest.approx(
            kwh[hour]["state"], abs=1e-6
        )


async def test_supply_mode_spread_gives_every_hour_an_equal_share(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([0.0] * 48)  # no usage, so what is stored is only the supply charge
    await _write_all(hass, channel, _glohome(), SUPPLY_SPREAD)
    rows = await _period_rows(hass)
    for hour in range(48):
        assert _cost_state(rows, hour) == pytest.approx(SUPPLY_DAY / 24, abs=2e-6)
    # Each hour's share is in the period that covers it: off-peak has 14 hours of the day.
    off_peak = _pair(rows, "off_peak")[1]
    assert off_peak[-1]["sum"] == pytest.approx(2 * SUPPLY_DAY * 14 / 24, abs=1e-4)
    assert _pair(rows, "peak")[1][-1]["sum"] == pytest.approx(2 * SUPPLY_DAY * 5 / 24, abs=1e-4)


async def test_supply_mode_first_hour_puts_the_whole_day_on_hour_zero(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([0.0] * 48)
    await _write_all(hass, channel, _glohome(), SUPPLY_FIRST_HOUR)
    rows = await _period_rows(hass)
    for hour in range(48):
        expected = SUPPLY_DAY if hour % 24 == 0 else 0.0
        assert _cost_state(rows, hour) == pytest.approx(expected, abs=2e-6)
    assert _pair(rows, "off_peak")[1][0]["state"] == pytest.approx(SUPPLY_DAY, abs=2e-6)


async def test_supply_mode_none_is_what_period_costs_always_were(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0] * 48)
    await _write_all(hass, channel, _glohome(), SUPPLY_NONE)
    rows = await _period_rows(hass)
    for slug in SLUGS:
        assert _pair(rows, slug)[1][-1]["sum"] == pytest.approx(PERIOD_COST[slug], abs=1e-4)
    # Passing no mode at all changes nothing: it is the default.
    assert await async_insert_period_statistics(hass, NMI, "Home", channel, _glohome()) == 0
    assert await _period_rows(hass) == rows


@pytest.mark.parametrize("mode", [SUPPLY_SPREAD, SUPPLY_FIRST_HOUR])
async def test_supply_mode_rerun_changes_nothing(
    recorder_mock: Recorder, hass: HomeAssistant, mode: str
) -> None:
    channel = _channel([1.0] * 48)
    await _write_all(hass, channel, _glohome(), mode)
    before = await _period_rows(hass)
    written = await async_insert_period_statistics(
        hass, NMI, "Home", channel, _glohome(), supply_mode=mode
    )
    assert written == 0
    assert await _period_rows(hass) == before


async def test_supply_mode_switching_rewrites_without_double_counting(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A poll under a new mode repairs what it can see; the running totals stay exact."""
    channel = _channel([1.0] * 48)
    await _write_all(hass, channel, _glohome(), SUPPLY_NONE)
    for mode in (SUPPLY_SPREAD, SUPPLY_FIRST_HOUR, SUPPLY_NONE, SUPPLY_SPREAD):
        await async_insert_period_statistics(
            hass, NMI, "Home", channel, _glohome(), supply_mode=mode
        )
        rows = await _period_rows(hass)
        total = sum(_pair(rows, slug)[1][-1]["sum"] for slug in SLUGS)
        carried = 0.0 if mode == SUPPLY_NONE else 2 * SUPPLY_DAY
        assert total == pytest.approx(sum(PERIOD_COST.values()) + carried, abs=1e-4), mode
        for slug in SLUGS:
            kwh, cost = _pair(rows, slug)
            assert len(cost) == 48  # never extra rows
            _assert_consistent(cost)
            assert kwh[-1]["sum"] == pytest.approx(PERIOD_KWH[slug], abs=1e-6)


async def test_supply_mode_recalculate_moves_old_history_to_the_new_mode(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0] * 72)  # three days
    await _write_all(hass, channel, _glohome(), SUPPLY_NONE)
    written = await async_recalculate_costs(
        hass,
        NMI,
        "Home",
        "E1",
        _glohome(),
        date(2026, 10, 20),
        period_statistics=True,
        supply_mode=SUPPLY_SPREAD,
    )
    assert written == 72  # the combined cost's hours
    rows = await _period_rows(hass)
    combined = await _stored_cost(hass)
    for hour in range(72):  # a uniform-usage day: each hour is plain usage plus a 24th of supply
        plain = _day_states()[hour % 24] - (SUPPLY_DAY if hour % 24 == 0 else 0.0)
        assert _cost_state(rows, hour) == pytest.approx(plain + SUPPLY_DAY / 24, abs=3e-6)
    assert sum(_pair(rows, slug)[1][-1]["sum"] for slug in SLUGS) == pytest.approx(
        combined[-1]["sum"], abs=1e-4
    )
    for slug in SLUGS:
        _assert_consistent(_pair(rows, slug)[1])
    # And back, from a later day only: day one keeps the spread, the rest return to plain.
    await async_recalculate_costs(
        hass,
        NMI,
        "Home",
        "E1",
        _glohome(),
        date(2026, 10, 21),
        period_statistics=True,
        supply_mode=SUPPLY_NONE,
    )
    rows = await _period_rows(hass)
    for hour in range(24):
        plain = _day_states()[hour] - (SUPPLY_DAY if hour == 0 else 0.0)
        assert _cost_state(rows, hour) == pytest.approx(plain + SUPPLY_DAY / 24, abs=3e-6)
    for hour in range(24, 72):
        plain = _day_states()[hour % 24] - (SUPPLY_DAY if hour % 24 == 0 else 0.0)
        assert _cost_state(rows, hour) == pytest.approx(plain, abs=3e-6)
    for slug in SLUGS:
        _assert_consistent(_pair(rows, slug)[1])


async def test_supply_mode_recalculate_without_period_statistics_ignores_the_mode(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    channel = _channel([1.0] * 48)
    await _write_all(hass, channel, _glohome(), SUPPLY_NONE)
    before = await _period_rows(hass)
    await async_recalculate_costs(
        hass, NMI, "Home", "E1", _glohome(), date(2026, 10, 20), supply_mode=SUPPLY_SPREAD
    )
    assert await _period_rows(hass) == before


async def test_supply_mode_has_no_effect_on_a_flat_rate(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    tariff = flat_tariff(10.0, effective_from=D0, supply_c_per_day=100.0)
    channel = _channel([1.0] * 48)
    await _write_all(hass, channel, tariff, SUPPLY_SPREAD)
    cost = await _rows_of(hass, period_statistic_ids(NMI, "all_day")[1])
    assert cost[-1]["sum"] == pytest.approx(48 * 0.10, abs=1e-6)  # usage only, no supply


async def test_supply_mode_spread_across_daylight_saving_adds_up_on_23_and_25_hour_days(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    tariff = _glohome_in("Australia/Melbourne")
    for first_day, hours in ((date(2026, 4, 4), 73), (date(2026, 10, 3), 71)):
        values = [((i * 7) % 11) / 3 + 0.25 for i in range(hours)]
        channel = _channel(values, _local_midnight(first_day, MELBOURNE))
        await _write_all(hass, channel, tariff, SUPPLY_SPREAD)
    rows = await _period_rows(hass)
    combined = await _stored_cost(hass)
    assert len(combined) == 73 + 71
    # Each local day (23 or 25 hours long) adds up to the combined cost for it.
    starts = [_local_midnight(date(2026, 4, d), MELBOURNE) for d in (4, 5, 6)]
    starts += [_local_midnight(date(2026, 10, d), MELBOURNE) for d in (3, 4, 5)]
    starts += [starts[2] + timedelta(hours=24), starts[5] + timedelta(hours=24)]  # day ends
    stamps = [dt_util.utc_from_timestamp(row["start"]) for row in combined]
    days = ((0, 1, 24), (1, 2, 25), (3, 4, 24), (4, 5, 23))
    for a, b, hours in days:
        wanted = [i for i, m in enumerate(stamps) if starts[a] <= m < starts[b]]
        assert len(wanted) == hours
        assert sum(_cost_state(rows, i) for i in wanted) == pytest.approx(
            sum(combined[i]["state"] for i in wanted), abs=1e-4
        )
    assert sum(_pair(rows, slug)[1][-1]["sum"] for slug in SLUGS) == pytest.approx(
        combined[-1]["sum"], abs=1e-4
    )
    for slug in SLUGS:
        _assert_consistent(_pair(rows, slug)[1])
