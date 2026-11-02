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
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

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

_LOGGER = logging.getLogger(__name__)

# Widening look-back windows when searching for the total before an hour, so
# the common case (yesterday) does not read two years of rows.
_LOOKBACK_DAYS = (3, 45, 800)


def statistic_id_for(nmi: str, suffix: str) -> str:
    """Return the external statistic id for an NMI's channel."""
    return f"{DOMAIN}:{nmi.lower()}_{suffix.lower()}"


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
