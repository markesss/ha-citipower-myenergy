"""Write meter data into Home Assistant's long-term statistics.

The portal publishes a day's readings the morning after, so they can never be
recorded as they happen. They are written as external statistics stamped with
the hour they belong to instead, which is what the Energy dashboard reads. One
statistic per NEM12 channel: E1 is energy taken from the grid, B1 energy sent
back to it.

Adapted from gheydon/ha-yvw (MIT): each poll re-reads a window of recent days,
and if any stored hour disagrees with the portal (an estimate replaced by an
actual reading, say), everything from that hour on is rewritten with the
running total recalculated.

Cost statistics
---------------
When the user has entered a tariff, a companion statistic per priced channel holds
dollars: ``<nmi>_e1_cost`` (grid import cost) and ``<nmi>_b1_compensation`` (money earned
for export, positive; the Energy dashboard subtracts it, verified live). Only E1 and B1 are
priced: other channels (E2 is usually controlled load) follow rules a single tariff does not
describe.

* Costs are priced a **whole local (AEST) day** at a time, because tier allowances and the
  daily supply charge are per day. Only days with all 24 hours of readings that have ended
  are written; a partial day writes nothing, so a running total never has to be undone.
* The same repair rule as the kWh statistic applies: if a day's expected cost differs from
  what is stored (new readings, an estimate replaced by an actual, a changed tariff) or a
  day was skipped in the middle of stored history, everything from that day on is rewritten
  with the running total recalculated from the row before it. An unchanged re-run writes
  nothing.
* Each hour's ``state`` is rounded to a millionth of a dollar and ``sum`` accumulates those
  rounded states, so the total is exactly what the rows add up to. Rounding to cents per
  hour would drift by dollars over a year; micro-dollars cannot.
* A poll only re-reads a recent window, so a tariff edit reprices just those days.
  ``async_recalculate_costs`` reprices older history from the stored kWh statistic.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN
from .nem12 import Channel
from .tariff import KIND_EXPORT, KIND_IMPORT, Tariff, cost_for_day, local_day, local_day_hours

_LOGGER = logging.getLogger(__name__)

# Widening look-back windows when searching for the total before an hour, so
# the common case (yesterday) does not read two years of rows.
_LOOKBACK_DAYS = (3, 45, 800)

# Cost statistics are in dollars, kept to a millionth so rounding cannot accumulate.
_COST_DECIMALS = 6
_COST_TOLERANCE = 5e-7

# The only channels a tariff prices; see the module docstring.
_PRICED_CHANNELS = {"E1": KIND_IMPORT, "B1": KIND_EXPORT}


def statistic_id_for(nmi: str, suffix: str) -> str:
    """Return the external statistic id for an NMI's channel."""
    return f"{DOMAIN}:{nmi.lower()}_{suffix.lower()}"


def cost_statistic_id_for(nmi: str, suffix: str) -> str:
    """Return the external statistic id holding a channel's cost (or export compensation)."""
    kind = "compensation" if suffix[:1].upper() == "B" else "cost"
    return f"{DOMAIN}:{nmi.lower()}_{suffix.lower()}_{kind}"


def channel_label(suffix: str) -> str:
    """Describe a NEM12 channel in plain words."""
    kind = suffix[:1].upper()
    if kind == "B":
        return "grid export"
    if kind == "E":
        return "grid import" if suffix.upper() == "E1" else f"grid import ({suffix})"
    return f"channel {suffix}"


def _metadata(nmi: str, suffix: str, name: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{name} {channel_label(suffix)}",
        source=DOMAIN,
        statistic_id=statistic_id_for(nmi, suffix),
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def _cost_metadata(hass: HomeAssistant, nmi: str, suffix: str, name: str) -> StatisticMetaData:
    what = "grid export compensation" if suffix[:1].upper() == "B" else "grid import cost"
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{name} {what}",
        source=DOMAIN,
        statistic_id=cost_statistic_id_for(nmi, suffix),
        unit_class=None,
        # The user's own currency, so the Energy dashboard's pickers keep listing it.
        unit_of_measurement=hass.config.currency,
    )


async def async_last_stat_start(hass: HomeAssistant, statistic_id: str) -> datetime | None:
    """Return the start of the newest stored hour for a statistic."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    rows = last.get(statistic_id) if last else None
    if not rows:
        return None
    return dt_util.utc_from_timestamp(rows[0]["start"])


async def async_insert_channel(hass: HomeAssistant, nmi: str, name: str, channel: Channel) -> int:
    """Record a channel's hourly readings; return how many hours were written."""
    now = dt_util.utcnow()
    readings = sorted(
        (hour, kwh) for hour, kwh in channel.hours.items() if hour + timedelta(hours=1) <= now
    )
    if not readings:
        return 0

    statistic_id = statistic_id_for(nmi, channel.suffix)
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    if last and last.get(statistic_id):
        row = last[statistic_id][0]
        running_sum = float(row.get("sum") or 0.0)
        last_start: float | None = row["start"]
    else:
        running_sum, last_start = 0.0, None

    repair_from = await _async_first_disagreement(hass, statistic_id, readings)
    if repair_from is not None:
        running_sum, last_start = await _async_total_before(hass, statistic_id, repair_from)
        _LOGGER.info(
            "Rewriting %s from %s: the portal's readings changed there", statistic_id, repair_from
        )

    statistics: list[StatisticData] = []
    for hour, kwh in readings:
        if last_start is not None and hour.timestamp() <= last_start:
            continue
        running_sum += kwh
        statistics.append(StatisticData(start=hour, state=kwh, sum=round(running_sum, 6)))

    if statistics:
        _LOGGER.debug(
            "Adding %s hours to %s (through %s)",
            len(statistics),
            statistic_id,
            statistics[-1]["start"],
        )
        async_add_external_statistics(hass, _metadata(nmi, channel.suffix, name), statistics)
    return len(statistics)


async def _async_rows(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> list[dict]:
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return rows.get(statistic_id, [])


async def _async_first_disagreement(
    hass: HomeAssistant, statistic_id: str, readings: list[tuple[datetime, float]]
) -> datetime | None:
    """Return the first hour where what is stored differs from the portal."""
    first, last = readings[0][0], readings[-1][0] + timedelta(hours=1)
    stored = await _async_rows(hass, statistic_id, first, last)
    if not stored:
        return None
    by_start = {row["start"]: row for row in stored}
    newest = max(by_start)
    for hour, kwh in readings:
        row = by_start.get(hour.timestamp())
        if row is None:
            # Missing in the middle of stored history means it was skipped.
            if hour.timestamp() < newest:
                return hour
            continue
        state = row.get("state")
        if state is None or abs(float(state) - kwh) > 0.0005:
            return hour
    return None


async def _async_total_before(
    hass: HomeAssistant, statistic_id: str, moment: datetime
) -> tuple[float, float | None]:
    """Return the running total and start of the hour just before a moment."""
    for days in _LOOKBACK_DAYS:
        rows = await _async_rows(hass, statistic_id, moment - timedelta(days=days), moment)
        earlier = [row for row in rows if row["start"] < moment.timestamp()]
        if earlier:
            row = max(earlier, key=lambda item: item["start"])
            return float(row.get("sum") or 0.0), row["start"]
    return 0.0, None


# --- Cost ---------------------------------------------------------------------------


def _prices_channel(suffix: str, tariff: Tariff) -> str | None:
    """Return the kind a channel is priced as, or None if the tariff does not cover it."""
    kind = _PRICED_CHANNELS.get(suffix.upper())
    if kind == KIND_EXPORT and not any(v.export_periods for v in tariff.versions):
        return None
    return kind


def _complete_days(
    hours: dict[datetime, float], now: datetime
) -> dict[date, dict[datetime, float]]:
    """Group readings by AEST day, keeping only days that are whole and over."""
    by_day: dict[date, dict[datetime, float]] = {}
    for moment, kwh in hours.items():
        utc = moment.astimezone(UTC)
        by_day.setdefault(local_day(utc), {})[utc] = kwh
    complete: dict[date, dict[datetime, float]] = {}
    for day, readings in by_day.items():
        expected = local_day_hours(day)
        if set(readings) == set(expected) and expected[-1] + timedelta(hours=1) <= now:
            complete[day] = readings
    return dict(sorted(complete.items()))


def _micro(value: float) -> float:
    return round(value, _COST_DECIMALS)


async def _async_write_costs(
    hass: HomeAssistant,
    nmi: str,
    name: str,
    suffix: str,
    tariff: Tariff,
    hours: dict[datetime, float],
    *,
    reprice_from: date | None = None,
) -> int:
    """Price whole days of readings and store the cost rows; return how many were written."""
    kind = _prices_channel(suffix, tariff)
    if kind is None:
        return 0
    days = _complete_days(hours, dt_util.utcnow())
    if not days:
        return 0

    expected: dict[date, dict[datetime, float]] = {
        day: {
            moment: _micro(cost)
            for moment, cost in cost_for_day(readings, tariff, kind, day).items()
        }
        for day, readings in days.items()
    }

    statistic_id = cost_statistic_id_for(nmi, suffix)
    first_hour = min(min(rows) for rows in expected.values())
    last_hour = max(max(rows) for rows in expected.values()) + timedelta(hours=1)
    stored = await _async_rows(hass, statistic_id, first_hour, last_hour)
    by_start = {row["start"]: row for row in stored}
    newest = max(by_start) if by_start else None

    repair_day = reprice_from
    if repair_day is None and newest is not None:
        repair_day = _first_cost_disagreement(expected, by_start, newest)

    if repair_day is not None:
        start_of_day = local_day_hours(repair_day)[0]
        running_sum, last_start = await _async_total_before(hass, statistic_id, start_of_day)
        _LOGGER.info("Rewriting %s from %s", statistic_id, repair_day)
    else:
        last = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, statistic_id, True, {"sum"}
        )
        if last and last.get(statistic_id):
            # Every stored hour in range agrees; add only what is newer.
            row = last[statistic_id][0]
            running_sum, last_start = float(row.get("sum") or 0.0), row["start"]
        else:
            running_sum, last_start = 0.0, None

    statistics: list[StatisticData] = []
    for day, rows in expected.items():
        if repair_day is not None and day < repair_day:
            continue
        for moment in sorted(rows):
            if last_start is not None and moment.timestamp() <= last_start:
                continue
            running_sum = _micro(running_sum + rows[moment])
            statistics.append(StatisticData(start=moment, state=rows[moment], sum=running_sum))

    if statistics:
        _LOGGER.debug(
            "Adding %s hours to %s (through %s)",
            len(statistics),
            statistic_id,
            statistics[-1]["start"],
        )
        async_add_external_statistics(hass, _cost_metadata(hass, nmi, suffix, name), statistics)
    return len(statistics)


def _first_cost_disagreement(
    expected: dict[date, dict[datetime, float]], by_start: dict[float, dict], newest: float
) -> date | None:
    """Return the first day whose stored cost differs from what the tariff now gives."""
    for day, rows in expected.items():
        for moment in sorted(rows):
            row = by_start.get(moment.timestamp())
            if row is None:
                # Missing in the middle of stored history means a day was skipped.
                if moment.timestamp() < newest:
                    return day
                continue
            state = row.get("state")
            if state is None or abs(float(state) - rows[moment]) > _COST_TOLERANCE:
                return day
    return None


async def _async_stored_hours(
    hass: HomeAssistant, nmi: str, suffix: str, start: datetime
) -> dict[datetime, float]:
    """Return the hourly kWh already stored for a channel, from ``start`` on."""
    rows = await _async_rows(hass, statistic_id_for(nmi, suffix), start, dt_util.utcnow())
    return {
        dt_util.utc_from_timestamp(row["start"]): float(row["state"])
        for row in rows
        if row.get("state") is not None
    }


async def async_insert_cost(
    hass: HomeAssistant, nmi: str, name: str, channel: Channel, tariff: Tariff
) -> int:
    """Price a channel's readings and record the cost; return how many hours were written."""
    suffix = channel.suffix
    if _prices_channel(suffix, tariff) is None:
        return 0
    statistic_id = cost_statistic_id_for(nmi, suffix)
    hours: dict[datetime, float] = {m.astimezone(UTC): kwh for m, kwh in channel.hours.items()}
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    if not last or not last.get(statistic_id):
        # First time with a tariff: price the stored kWh history too, not just this poll's
        # window. The readings just fetched win over what is stored.
        stored = await _async_stored_hours(hass, nmi, suffix, dt_util.utc_from_timestamp(0))
        hours = {**stored, **hours}
    return await _async_write_costs(hass, nmi, name, suffix, tariff, hours)


async def async_recalculate_costs(
    hass: HomeAssistant, nmi: str, name: str, suffix: str, tariff: Tariff, from_day: date
) -> int:
    """Reprice a channel's history from ``from_day`` on, using the stored kWh statistic.

    Used when the tariff changed and older days should follow it. Rewrites every cost row
    from that day with the running total recalculated; nothing is read from the portal.
    """
    start = local_day_hours(from_day)[0]
    hours = await _async_stored_hours(hass, nmi, suffix, start)
    return await _async_write_costs(hass, nmi, name, suffix, tariff, hours, reprice_from=from_day)
