"""Parse NEM12 interval data and roll it up into hourly totals.

myEnergy hands meter data back as a NEM12 file (the AEMO "Meter Data File
Format") embedded in a JSON response. Only the parts needed here are read:

* 200 — a data stream: NMI, suffix (E1 = import, B1 = export, ...), unit and
  interval length;
* 300 — one day of interval values for the current stream, followed by a
  quality flag;
* 400 — per-interval quality overrides when a day's flag is "V" (variable).

Times are market time (AEST, UTC+10 all year). Hourly buckets are therefore
whole hours in UTC too, with no daylight-saving ambiguity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .const import MARKET_TZ

# Multipliers to kWh for the units NEM12 permits for energy.
_TO_KWH = {"WH": 0.001, "KWH": 1.0, "MWH": 1000.0}

# Quality flags, best first. A bucket takes the worst flag of its intervals.
_QUALITY_RANK = {"A": 0, "F": 1, "S": 2, "E": 3, "N": 4}


@dataclass(slots=True)
class Channel:
    """One NEM12 data stream, rolled up into hourly kWh."""

    nmi: str
    suffix: str
    meter_serial: str
    unit: str
    interval_minutes: int
    hours: dict[datetime, float] = field(default_factory=dict)
    quality: dict[datetime, str] = field(default_factory=dict)
    days: set[date] = field(default_factory=set)
    updated: dict[date, datetime] = field(default_factory=dict)

    @property
    def is_export(self) -> bool:
        """Return whether this stream measures energy sent to the grid."""
        return self.suffix[:1].upper() == "B"

    @property
    def last_day(self) -> date | None:
        """Return the latest day with readings."""
        return max(self.days) if self.days else None


def _parse_day(text: str) -> date:
    return datetime.strptime(text, "%Y%m%d").date()


def _parse_timestamp(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=MARKET_TZ)
    except ValueError:
        return None


def _worse(current: str | None, new: str) -> str:
    if current is None:
        return new
    return new if _QUALITY_RANK.get(new, 9) > _QUALITY_RANK.get(current, 9) else current


def parse_nem12(text: str) -> dict[str, Channel]:
    """Parse a NEM12 file into channels keyed by NMI suffix.

    Days present in more than one 300 record (overlapping requests stitched
    together) take the last one seen.
    """
    channels: dict[str, Channel] = {}
    current: Channel | None = None
    # Raw interval values for the current day, kept so 400 records can be
    # applied before rolling up.
    pending: tuple[date, list[float | None], str] | None = None
    overrides: list[tuple[int, int, str]] = []

    def flush() -> None:
        nonlocal pending, overrides
        if current is None or pending is None:
            pending, overrides = None, []
            return
        day, values, day_quality = pending
        flags = [day_quality[:1] or "A"] * len(values)
        for start, end, flag in overrides:
            for index in range(max(start, 1) - 1, min(end, len(values))):
                flags[index] = flag[:1] or flags[index]

        factor = _TO_KWH.get(current.unit.upper(), 1.0)
        midnight = datetime.combine(day, datetime.min.time(), MARKET_TZ)
        buckets: dict[datetime, float] = defaultdict(float)
        bucket_quality: dict[datetime, str] = {}
        for index, value in enumerate(values):
            start = midnight + timedelta(minutes=index * current.interval_minutes)
            hour = start.replace(minute=0, second=0, microsecond=0)
            if value is not None:
                buckets[hour] += value * factor
            bucket_quality[hour] = _worse(bucket_quality.get(hour), flags[index])
        for hour in bucket_quality:
            current.hours[hour] = round(buckets.get(hour, 0.0), 6)
            current.quality[hour] = bucket_quality[hour]
        current.days.add(day)
        pending, overrides = None, []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = [part.strip() for part in line.split(",")]
        record = fields[0]

        if record == "200":
            flush()
            nmi, suffix = fields[1], fields[4] or fields[3]
            interval = int(fields[8]) if len(fields) > 8 and fields[8] else 30
            current = channels.get(suffix)
            if current is None:
                current = Channel(
                    nmi=nmi,
                    suffix=suffix,
                    meter_serial=fields[6] if len(fields) > 6 else "",
                    unit=fields[7] if len(fields) > 7 else "KWH",
                    interval_minutes=interval,
                )
                channels[suffix] = current
            else:
                # A stream can reappear when files are stitched together, or
                # after a meter exchange; the latest description wins.
                current.interval_minutes = interval
                current.unit = fields[7] if len(fields) > 7 else current.unit
                current.meter_serial = fields[6] if len(fields) > 6 else current.meter_serial
        elif record == "300" and current is not None:
            flush()
            per_day = 1440 // current.interval_minutes
            day = _parse_day(fields[1])
            values: list[float | None] = []
            for part in fields[2 : 2 + per_day]:
                try:
                    values.append(float(part))
                except ValueError:
                    values.append(None)
            tail = fields[2 + per_day :]
            quality = tail[0] if tail else "A"
            pending = (day, values, quality)
            if len(tail) > 3:
                updated = _parse_timestamp(tail[3])
                if updated is not None:
                    current.updated[day] = updated
        elif record == "400" and pending is not None:
            try:
                overrides.append((int(fields[1]), int(fields[2]), fields[3]))
            except (IndexError, ValueError):
                continue
        elif record in ("500", "900", "100"):
            flush()

    flush()
    return channels


def merge_channels(target: dict[str, Channel], source: dict[str, Channel]) -> None:
    """Merge one parsed file into another, later data winning."""
    for suffix, channel in source.items():
        existing = target.get(suffix)
        if existing is None:
            target[suffix] = channel
            continue
        existing.hours.update(channel.hours)
        existing.quality.update(channel.quality)
        existing.days |= channel.days
        existing.updated.update(channel.updated)
        existing.meter_serial = channel.meter_serial or existing.meter_serial


def daily_total(channel: Channel, day: date) -> float | None:
    """Return a channel's total for one market day, if it has that day."""
    if day not in channel.days:
        return None
    start = datetime.combine(day, datetime.min.time(), MARKET_TZ)
    end = start + timedelta(days=1)
    return round(sum(value for hour, value in channel.hours.items() if start <= hour < end), 3)
