"""PURE: when the next poll is due, built around the portal's overnight publication.

The portal publishes a day's readings the following morning (about 01:00 AEST) but at no time it
states. A fixed interval counted from start-up therefore lands anywhere relative to that, and a
poll that finds nothing leaves the data a whole interval late. Instead each poll sets the delay
to the next one: sleep until a daily catch-up window, retry briskly inside it until yesterday's
readings arrive, then sleep until the next day's window. The shape follows gheydon/ha-yvw.

The window is wall-clock time in Home Assistant's own time zone (the caller passes ``now`` in
it), so it stays at the same hour across daylight saving changes. Differences are taken in UTC,
because subtracting two aware datetimes that share a tzinfo ignores a DST change in between.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

# 02:30 local is half an hour after the portal's 01:00 AEST publication in summer time
# (02:00 AEDT), and 01:30 AEST in winter, so the first poll should already find the day.
CATCHUP_START = time(2, 30)
# Give up on a day that never arrives rather than polling all day for it.
CATCHUP_HOURS = 6
CATCHUP_RETRY = timedelta(minutes=10)

# A failure outside the window waits longer, doubling to a cap, but never past the next window.
FAILURE_RETRY = timedelta(minutes=30)
MAX_FAILURE_RETRY = timedelta(hours=2)


def _elapsed(earlier: datetime, later: datetime) -> timedelta:
    """Return the real time between two aware datetimes, whatever their zone's DST did."""
    return later.astimezone(UTC) - earlier.astimezone(UTC)


def _opens_on(now: datetime, days: int = 0) -> datetime:
    """Return when the window opens on ``now``'s date, shifted by ``days``, in ``now``'s zone."""
    day = now + timedelta(days=days)
    return day.replace(
        hour=CATCHUP_START.hour, minute=CATCHUP_START.minute, second=0, microsecond=0
    )


def in_window(now: datetime) -> bool:
    """Return whether ``now`` falls in today's catch-up window."""
    opened = _opens_on(now)
    return timedelta(0) <= _elapsed(opened, now) < timedelta(hours=CATCHUP_HOURS)


def until_next_window(now: datetime) -> timedelta:
    """Return the time until the next window opens (today's if it has not yet, else tomorrow's)."""
    opens = _opens_on(now)
    if _elapsed(now, opens) <= timedelta(0):
        opens = _opens_on(now, 1)
    return _elapsed(now, opens)


def after_success(now: datetime, *, yesterday_complete: bool) -> timedelta:
    """Return the delay to the next poll after one that worked.

    Once yesterday's readings are in, nothing new can appear until tomorrow's window. Until then,
    inside the window keep trying; outside it (the day never came, or it has not opened yet) wait
    for the next window.
    """
    if yesterday_complete:
        return _elapsed(now, _opens_on(now, 1))
    if in_window(now):
        return CATCHUP_RETRY
    return until_next_window(now)


def after_failure(now: datetime, *, yesterday_complete: bool, failures: int) -> timedelta:
    """Return the delay to the next poll after one that failed (``failures`` counts in a row)."""
    if not yesterday_complete and in_window(now):
        return CATCHUP_RETRY
    backoff = min(FAILURE_RETRY * 2 ** max(failures - 1, 0), MAX_FAILURE_RETRY)
    # A failure just after yesterday completed has nothing owed until tomorrow's window.
    owed = _elapsed(now, _opens_on(now, 1)) if yesterday_complete else until_next_window(now)
    return min(backoff, owed)
