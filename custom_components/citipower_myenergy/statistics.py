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

* Costs are priced a **whole local day** at a time, because tier allowances and the daily
  supply charge are per day. The day is midnight to midnight on the tariff's clock (see
  ``tariff.py``): 24 hours, or 23 or 25 on a daylight saving changeover day for zones that
  observe it. Only days with every hour of readings that have ended are written; a partial
  day writes nothing, so a running total never has to be undone.
* The same repair rule as the kWh statistic applies: if a day's expected cost differs from
  what is stored (new readings, an estimate replaced by an actual, a changed tariff) or a
  day was skipped in the middle of stored history, everything from that day on is rewritten
  with the running total recalculated from the row before it. An unchanged re-run writes
  nothing.
* Each hour's ``state`` is rounded to a millionth of a dollar and ``sum`` accumulates those
  rounded states, so the total is exactly what the rows add up to. Rounding to cents per
  hour would drift by dollars over a year; micro-dollars cannot.
* A poll only re-reads a recent window, so a tariff edit reprices just those days.
  ``async_recalculate_costs`` reprices older history from the stored kWh statistic. That
  includes a changed tariff timezone, which moves every day's edges and every window: the
  stored cost rows then disagree from the first affected day and are rewritten from there.

Per-period statistics (opt-in)
------------------------------
With the "statistics per tariff period" option on, each import period also gets its own
kWh statistic (``<nmi>_e1_<slug>``) and cost statistic (``<nmi>_e1_<slug>_cost``), so the
Energy dashboard can draw each period as its own grid source in its own colour. The combined
``_e1`` and ``_e1_cost`` statistics are untouched and keep including the supply charge. The
per-period costs leave it out by default, because the dashboard's fixed daily cost adjustment
is not applied to per-period sources, so it would not be shown. A **supply mode** option can
instead put it in the period costs (see ``tariff.cost_for_day_by_period``): spread equally over
every hour of the day, or all on the day's first hour, so the period costs add up to the day's
combined cost. The mode only changes cost rows; kWh rows never depend on it. Changing it
disagrees with the stored cost rows from the first day in range, so the usual repair rule
rewrites them (``async_recalculate_costs`` reaches older history).

* The set of periods is the union of every tariff version's period names, keyed by slug (see
  ``tariff.period_slugs``). A day priced under a version that lacks a period writes zero for
  it, so every statistic has a row for every hour and the totals always add up.
* All of a channel's period statistics are written together by one repair rule: if any of them
  disagrees with the tariff and readings for a day (or has no rows while another does, as
  when a period is added), everything from that day on is rewritten for all of them, with each
  running total recalculated from its own row before. A re-run changes nothing.
* A period that is renamed or removed from the latest version stops being written; its old
  statistics stay in the database until the user clears them (Developer Tools > Statistics).
  Statistics are never deleted automatically.
* Only import periods are split. Export keeps its single ``_b1_compensation`` statistic.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

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

from .chain_guard import ChainGuard, guard_for, local_hour_text
from .const import DOMAIN
from .nem12 import Channel
from .tariff import (
    KIND_EXPORT,
    KIND_IMPORT,
    SUPPLY_NONE,
    Tariff,
    cost_for_day,
    cost_for_day_by_period,
    local_day,
    local_day_hours,
    period_slugs,
)

_LOGGER = logging.getLogger(__name__)

# Widening look-back windows when searching for the total before an hour, so
# the common case (yesterday) does not read two years of rows.
_LOOKBACK_DAYS = (3, 45, 800)

# Cost statistics are in dollars, kept to a millionth so rounding cannot accumulate.
_COST_DECIMALS = 6
_COST_TOLERANCE = 5e-7

# How long to wait for rows just queued to be stored: this many looks, this far apart.
_STORED_WAIT_ATTEMPTS = 200
_STORED_WAIT_STEP = 0.05

# How far a row's running total may differ from the row before it plus its own state.
_CHAIN_TOLERANCE = 1e-5

# The only channels a tariff prices; see the module docstring.
_PRICED_CHANNELS = {"E1": KIND_IMPORT, "B1": KIND_EXPORT}


def statistic_id_for(nmi: str, suffix: str) -> str:
    """Return the external statistic id for an NMI's channel."""
    return f"{DOMAIN}:{nmi.lower()}_{suffix.lower()}"


def cost_statistic_id_for(nmi: str, suffix: str) -> str:
    """Return the external statistic id holding a channel's cost (or export compensation)."""
    kind = "compensation" if suffix[:1].upper() == "B" else "cost"
    return f"{DOMAIN}:{nmi.lower()}_{suffix.lower()}_{kind}"


def period_statistic_ids(nmi: str, slug: str) -> tuple[str, str]:
    """Return a period's (kWh, cost) external statistic ids; ``slug`` is from tariff.period_slugs.

    ``period_slugs`` never yields ``cost`` or a slug ending in ``_cost``, so these cannot clash
    with each other or with the combined ``<nmi>_e1_cost`` statistic.
    """
    kwh = f"{DOMAIN}:{nmi.lower()}_e1_{slug}"
    return kwh, f"{kwh}_cost"


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


def _period_metadata(
    hass: HomeAssistant, nmi: str, site: str, period_name: str, slug: str
) -> tuple[StatisticMetaData, StatisticMetaData]:
    """Return the (kWh, cost) metadata of one period's statistics."""
    kwh_id, cost_id = period_statistic_ids(nmi, slug)
    label = f"{site} grid import {period_name}"
    kwh = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=label,
        source=DOMAIN,
        statistic_id=kwh_id,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    cost = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{label} cost",
        source=DOMAIN,
        statistic_id=cost_id,
        unit_class=None,
        unit_of_measurement=hass.config.currency,
    )
    return kwh, cost


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
    async with guard_for(hass, nmi).lock:
        await _async_settled(hass)
        return await _async_insert_channel(hass, nmi, name, channel)


async def _async_insert_channel(hass: HomeAssistant, nmi: str, name: str, channel: Channel) -> int:
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
        await _async_wait_until_stored(hass, statistic_id, statistics)
        # The kWh total is rebuilt from the portal, not by a cost recalculation, so a break here
        # is logged and kept in the trail but does not raise the repair notice.
        guard = guard_for(hass, nmi)
        bad = await _async_chain_problem(hass, statistic_id, statistics[0]["start"])
        if bad is not None:
            _warn_not_joined(guard, statistic_id, bad, found="After writing")
        guard.record(
            who="poll",
            statistic_id=statistic_id,
            repair_from=None if repair_from is None else repair_from.isoformat(),
            supply_mode=None,
            start_total=running_sum - sum(row["state"] for row in statistics),
            rows=len(statistics),
            join_repaired=False,
            verified=bad is None,
        )
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


async def _async_settled(hass: HomeAssistant) -> None:
    """Wait until every statistics write queued so far has been stored.

    ``async_add_external_statistics`` only queues the rows. A running total read before they
    land is stale, and chaining new rows from it breaks the join (see ``chain_guard.py``).

    ``async_block_till_done`` returns at once when the queue is empty, which is also the case
    while the recorder is part-way through the task it just took off the queue, so rows we
    have just written are confirmed with ``_async_wait_until_stored`` instead.
    """
    await get_instance(hass).async_block_till_done()


async def _async_wait_until_stored(
    hass: HomeAssistant, statistic_id: str, statistics: list[StatisticData]
) -> bool:
    """Wait until the last row just queued can be read back; return whether it could.

    The rows of one write are stored together, so the last one being readable means all are.
    """
    await _async_settled(hass)
    last = statistics[-1]
    for _ in range(_STORED_WAIT_ATTEMPTS):
        rows = await _async_rows(
            hass, statistic_id, last["start"], last["start"] + timedelta(hours=1)
        )
        if rows and abs(float(rows[0].get("sum") or 0.0) - float(last["sum"])) <= _CHAIN_TOLERANCE:
            return True
        await asyncio.sleep(_STORED_WAIT_STEP)
    _LOGGER.debug("Rows written to a statistic were not readable back in time")
    return False


async def _async_chain_problem(
    hass: HomeAssistant, statistic_id: str, since: datetime
) -> float | None:
    """Return the start of the first row from ``since`` whose total does not join, or None.

    A row joins when its running total is the previous row's total plus its own state. The
    first row is checked against the stored row before ``since``, which is exactly where a
    rewrite that started from a stale total goes wrong.
    """
    rows = await _async_rows(hass, statistic_id, since, dt_util.utcnow() + timedelta(hours=1))
    rows = sorted((row for row in rows if row.get("sum") is not None), key=lambda r: r["start"])
    if not rows:
        return None
    previous, before_start = await _async_total_before(hass, statistic_id, since)
    running: float | None = previous if before_start is not None else None
    for row in rows:
        total, state = float(row["sum"]), row.get("state")
        if (
            running is not None
            and state is not None
            and abs(total - running - float(state)) > _CHAIN_TOLERANCE
        ):
            return float(row["start"])
        running = total
    return None


def _warn_not_joined(guard: ChainGuard, statistic_id: str, bad_start: float, *, found: str) -> None:
    """Log a chain problem by the statistic's short label and the local hour (never the NMI)."""
    _LOGGER.warning(
        "%s: the running total of %s does not join the row before it at %s",
        found,
        guard.label(statistic_id),
        local_hour_text(dt_util.utc_from_timestamp(bad_start)),
    )


# --- Cost ---------------------------------------------------------------------------


def _prices_channel(suffix: str, tariff: Tariff) -> str | None:
    """Return the kind a channel is priced as, or None if the tariff does not cover it."""
    kind = _PRICED_CHANNELS.get(suffix.upper())
    if kind == KIND_EXPORT and not any(v.export_periods for v in tariff.versions):
        return None
    return kind


def _complete_days(
    hours: dict[datetime, float], now: datetime, tz: tzinfo
) -> dict[date, dict[datetime, float]]:
    """Group readings by day on the tariff's clock, keeping only days that are whole and over.

    A whole day has every hour that day really has: 24, or 23/25 on a daylight saving change.
    """
    by_day: dict[date, dict[datetime, float]] = {}
    for moment, kwh in hours.items():
        utc = moment.astimezone(UTC)
        by_day.setdefault(local_day(utc, tz), {})[utc] = kwh
    complete: dict[date, dict[datetime, float]] = {}
    for day, readings in by_day.items():
        expected = local_day_hours(day, tz)
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
    who: str = "poll",
) -> int:
    """Price whole days of readings and store the cost rows; return how many were written."""
    kind = _prices_channel(suffix, tariff)
    if kind is None:
        return 0
    days = _complete_days(hours, dt_util.utcnow(), tariff.tzinfo)
    if not days:
        return 0

    expected: dict[date, dict[datetime, float]] = {
        day: {
            moment: _micro(cost)
            for moment, cost in cost_for_day(readings, tariff, kind, day).items()
        }
        for day, readings in days.items()
    }

    return await _async_write_series(
        hass,
        nmi,
        [_Series(_cost_metadata(hass, nmi, suffix, name), expected)],
        tariff.tzinfo,
        reprice_from=reprice_from,
        who=who,
    )


@dataclass(slots=True)
class _Series:
    """One statistic to write: its metadata and the value it should hold for every hour."""

    metadata: StatisticMetaData
    expected: dict[date, dict[datetime, float]]  # local day -> hour start (UTC) -> rounded state


async def _async_write_series(
    hass: HomeAssistant,
    nmi: str,
    series: list[_Series],
    tz: tzinfo,
    *,
    reprice_from: date | None = None,
    who: str = "poll",
    supply_mode: str | None = None,
) -> int:
    """Store whole days of expected values for statistics that are repaired together.

    The statistics share the same days and hours. If ANY of them disagrees with what is
    expected for a day, or has no rows in range while another does (a period just added),
    every statistic is rewritten from that day on, each running total recalculated from its
    own row before it. With nothing in disagreement only rows newer than each statistic's
    newest are added, so an unchanged re-run writes nothing. Returns the most hours written
    to any one statistic.

    The caller holds the NMI's guard lock (``chain_guard.py``). Two further protections:

    * A stored row whose running total does not join the row before it (the first stored row
      of the window is checked against the row just before the window) counts as a
      disagreement from that row's day, so a bad join is rewritten even though every state in
      the window is right. Without this the poll would never look at the join at its window's
      start, which is where a stale starting total leaves its mark.
    * After a statistic is written the recorder is awaited and the rows are checked again
      from the first written row. A failure is logged and keeps the repair notice raised
      until a later check passes.
    """
    guard = guard_for(hass, nmi)
    await _async_settled(hass)
    reference = series[0].expected
    first_hour = min(min(rows) for rows in reference.values())
    last_hour = max(max(rows) for rows in reference.values()) + timedelta(hours=1)
    stored: dict[str, dict[float, dict]] = {}
    for item in series:
        statistic_id = item.metadata["statistic_id"]
        rows = await _async_rows(hass, statistic_id, first_hour, last_hour)
        stored[statistic_id] = {row["start"]: row for row in rows}
    any_stored = any(stored.values())

    repair_day = reprice_from
    if repair_day is None:
        for item in series:
            by_start = stored[item.metadata["statistic_id"]]
            if by_start:
                day = _first_cost_disagreement(item.expected, by_start, max(by_start))
            elif any_stored:
                day = next(iter(item.expected))  # nothing yet here, history elsewhere
            else:
                day = None
            if day is not None and (repair_day is None or day < repair_day):
                repair_day = day

    # A stored row that does not join the one before it. Not looked for when the caller asked
    # for a rewrite from a day: that rebuilds the chain from the row before the day anyway.
    broken_join: set[str] = set()
    if reprice_from is None:
        for item in series:
            statistic_id = item.metadata["statistic_id"]
            by_start = stored[statistic_id]
            if not by_start:
                continue
            first_stored = dt_util.utc_from_timestamp(min(by_start))
            bad = await _async_chain_problem(hass, statistic_id, first_stored)
            if bad is not None:
                broken_join.add(statistic_id)
                _warn_not_joined(guard, statistic_id, bad, found="Found")
                day = local_day(dt_util.utc_from_timestamp(bad), tz)
                if repair_day is None or day < repair_day:
                    repair_day = day

    written = 0
    for item in series:
        statistic_id = item.metadata["statistic_id"]
        if repair_day is not None:
            start_of_day = local_day_hours(repair_day, tz)[0]
            running_sum, last_start = await _async_total_before(hass, statistic_id, start_of_day)
            _LOGGER.info("Rewriting %s from %s", guard.label(statistic_id), repair_day)
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

        start_total = running_sum
        statistics: list[StatisticData] = []
        for day, rows in item.expected.items():
            if repair_day is not None and day < repair_day:
                continue
            for moment in sorted(rows):
                if last_start is not None and moment.timestamp() <= last_start:
                    continue
                running_sum = _micro(running_sum + rows[moment])
                statistics.append(StatisticData(start=moment, state=rows[moment], sum=running_sum))

        if repair_day is not None and statistics:
            running_sum = await _async_keep_the_tail(hass, statistic_id, statistics, running_sum)

        verified: bool | None = None
        if statistics:
            _LOGGER.debug(
                "Adding %s hours to %s (through %s)",
                len(statistics),
                guard.label(statistic_id),
                statistics[-1]["start"],
            )
            async_add_external_statistics(hass, item.metadata, statistics)
            await _async_wait_until_stored(hass, statistic_id, statistics)
            bad = await _async_chain_problem(hass, statistic_id, statistics[0]["start"])
            verified = bad is None
            if bad is not None:
                _warn_not_joined(guard, statistic_id, bad, found="After writing")
            guard.report(statistic_id, bad)
        elif statistic_id not in broken_join:
            guard.report(statistic_id, None)  # a clean check clears an earlier failure
        guard.record(
            who=who,
            statistic_id=statistic_id,
            repair_from=None if repair_day is None else repair_day.isoformat(),
            supply_mode=supply_mode,
            start_total=start_total,
            rows=len(statistics),
            join_repaired=statistic_id in broken_join,
            verified=verified,
        )
        written = max(written, len(statistics))
    return written


async def _async_keep_the_tail(
    hass: HomeAssistant, statistic_id: str, statistics: list[StatisticData], running_sum: float
) -> float:
    """Re-chain any stored rows after a rewrite so no running total is left pointing backwards.

    A rewrite covers whole days on the tariff's clock. If the clock changed, the last complete
    day now ends at a different instant, and an hour that was priced under the old clock can sit
    just beyond the rewritten rows with an old running total: the dashboard would show that hour
    as a spike or a dip until the next day completes and overwrites it. Keeping its own state
    but recalculating its sum from the rows before it makes the chain consistent meanwhile.
    The next complete day replaces it with the right price.
    """
    after = statistics[-1]["start"] + timedelta(hours=1)
    for row in await _async_rows(hass, statistic_id, after, dt_util.utcnow() + timedelta(hours=1)):
        state = row.get("state")
        if state is None:
            continue
        running_sum = _micro(running_sum + float(state))
        statistics.append(
            StatisticData(
                start=dt_util.utc_from_timestamp(row["start"]), state=float(state), sum=running_sum
            )
        )
    return running_sum


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
    async with guard_for(hass, nmi).lock:
        return await _async_insert_cost(hass, nmi, name, channel, tariff)


async def _async_insert_cost(
    hass: HomeAssistant, nmi: str, name: str, channel: Channel, tariff: Tariff
) -> int:
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
    hass: HomeAssistant,
    nmi: str,
    name: str,
    suffix: str,
    tariff: Tariff,
    from_day: date,
    *,
    period_statistics: bool = False,
    supply_mode: str = SUPPLY_NONE,
) -> int:
    """Reprice a channel's history from ``from_day`` on, using the stored kWh statistic.

    Used when the tariff changed and older days should follow it. Rewrites every cost row
    from that day with the running total recalculated; nothing is read from the portal.
    With ``period_statistics`` the import channel's per-period statistics are rewritten too
    (from the current period list) with ``supply_mode`` deciding where the supply charge goes
    in their costs. The return value counts the combined cost's hours only.

    Takes the NMI's write lock, so it waits for a poll that is writing and the poll waits for it.
    """
    async with guard_for(hass, nmi).lock:
        await _async_settled(hass)  # the stored kWh read below must include every queued row
        start = local_day_hours(from_day, tariff.tzinfo)[0]
        hours = await _async_stored_hours(hass, nmi, suffix, start)
        written = await _async_write_costs(
            hass, nmi, name, suffix, tariff, hours, reprice_from=from_day, who="recalculate"
        )
        if period_statistics and suffix.upper() == "E1":
            await _async_write_periods(
                hass,
                nmi,
                name,
                tariff,
                hours,
                reprice_from=from_day,
                supply_mode=supply_mode,
                who="recalculate",
            )
        return written


# --- Per-period statistics ----------------------------------------------------------


def _period_series(
    hass: HomeAssistant,
    nmi: str,
    site: str,
    tariff: Tariff,
    days: dict[date, dict[datetime, float]],
    supply_mode: str = SUPPLY_NONE,
) -> list[_Series]:
    """Build every period's kWh and cost series: a row per hour, zero outside the period."""
    slugs = period_slugs(tariff, KIND_IMPORT)
    kwh_rows: dict[str, dict[date, dict[datetime, float]]] = {s: {} for s in slugs.values()}
    cost_rows: dict[str, dict[date, dict[datetime, float]]] = {s: {} for s in slugs.values()}
    for day, readings in days.items():
        for slug in slugs.values():
            kwh_rows[slug][day] = {}
            cost_rows[slug][day] = {}
        priced = cost_for_day_by_period(readings, tariff, KIND_IMPORT, day, supply_mode)
        for moment, item in priced.items():
            owner = slugs[item.period]
            for slug in slugs.values():
                mine = slug == owner
                kwh_rows[slug][day][moment] = _micro(item.kwh) if mine else 0.0
                cost_rows[slug][day][moment] = _micro(item.cost) if mine else 0.0

    series: list[_Series] = []
    for period_name, slug in slugs.items():
        kwh_meta, cost_meta = _period_metadata(hass, nmi, site, period_name, slug)
        series.append(_Series(kwh_meta, kwh_rows[slug]))
        series.append(_Series(cost_meta, cost_rows[slug]))
    return series


async def _async_write_periods(
    hass: HomeAssistant,
    nmi: str,
    name: str,
    tariff: Tariff,
    hours: dict[datetime, float],
    *,
    reprice_from: date | None = None,
    supply_mode: str = SUPPLY_NONE,
    who: str = "poll",
) -> int:
    """Price whole days into every period's statistics together; return hours written."""
    days = _complete_days(hours, dt_util.utcnow(), tariff.tzinfo)
    if not days:
        return 0
    series = _period_series(hass, nmi, name, tariff, days, supply_mode)
    return await _async_write_series(
        hass,
        nmi,
        series,
        tariff.tzinfo,
        reprice_from=reprice_from,
        who=who,
        supply_mode=supply_mode,
    )


async def async_insert_period_statistics(
    hass: HomeAssistant,
    nmi: str,
    name: str,
    channel: Channel,
    tariff: Tariff,
    supply_mode: str = SUPPLY_NONE,
) -> int:
    """Record the import channel's per-period kWh and cost; return hours written.

    Only E1 is split. The first time (or when a period has been added, so one statistic has
    no rows yet) the stored kWh history is priced too, not just this poll's window: turning
    the option on backfills everything the dashboard already has.
    """
    async with guard_for(hass, nmi).lock:
        return await _async_insert_period_statistics(hass, nmi, name, channel, tariff, supply_mode)


async def _async_insert_period_statistics(
    hass: HomeAssistant,
    nmi: str,
    name: str,
    channel: Channel,
    tariff: Tariff,
    supply_mode: str,
) -> int:
    if channel.suffix.upper() != "E1":
        return 0
    hours: dict[datetime, float] = {m.astimezone(UTC): kwh for m, kwh in channel.hours.items()}
    new_statistic = False
    for slug in period_slugs(tariff, KIND_IMPORT).values():
        for statistic_id in period_statistic_ids(nmi, slug):
            last = await get_instance(hass).async_add_executor_job(
                get_last_statistics, hass, 1, statistic_id, True, {"sum"}
            )
            if not last or not last.get(statistic_id):
                new_statistic = True
    if new_statistic:
        stored = await _async_stored_hours(hass, nmi, channel.suffix, dt_util.utc_from_timestamp(0))
        hours = {**stored, **hours}
    return await _async_write_periods(hass, nmi, name, tariff, hours, supply_mode=supply_mode)
