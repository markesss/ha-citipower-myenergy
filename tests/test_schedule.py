"""The catch-up window: when the next poll is due. Pure, so it runs without Home Assistant."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.citipower_myenergy.schedule import (
    CATCHUP_RETRY,
    FAILURE_RETRY,
    MAX_FAILURE_RETRY,
    after_failure,
    after_success,
    in_window,
    until_next_window,
)

TZ = ZoneInfo("Australia/Melbourne")


def at(day: int, hour: int, minute: int = 0, *, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


def test_window_is_0230_for_six_hours():
    assert not in_window(at(18, 2, 29))
    assert in_window(at(18, 2, 30))
    assert in_window(at(18, 8, 29))
    assert not in_window(at(18, 8, 30))


def test_a_complete_day_sleeps_until_tomorrows_window():
    assert after_success(at(18, 2, 40), yesterday_complete=True) == timedelta(hours=23, minutes=50)
    # Even before today's window: yesterday is in, so nothing is owed until tomorrow.
    assert after_success(at(18, 1, 0), yesterday_complete=True) == timedelta(hours=25, minutes=30)


def test_an_incomplete_day_retries_every_ten_minutes_in_the_window():
    assert after_success(at(18, 3), yesterday_complete=False) == CATCHUP_RETRY
    assert after_success(at(18, 8, 29), yesterday_complete=False) == CATCHUP_RETRY


def test_an_incomplete_day_waits_for_the_window_outside_it():
    # Before it opens (a restart overnight): wait for today's window.
    assert after_success(at(18, 1), yesterday_complete=False) == timedelta(hours=1, minutes=30)
    # After it closes (the day never came): give up until tomorrow's.
    assert after_success(at(18, 9), yesterday_complete=False) == timedelta(hours=17, minutes=30)


def test_failures_in_the_window_keep_the_short_retry():
    assert after_failure(at(18, 3), yesterday_complete=False, failures=5) == CATCHUP_RETRY


def test_failures_outside_the_window_back_off_to_a_cap():
    now = at(18, 12)
    delays = [after_failure(now, yesterday_complete=True, failures=n) for n in (1, 2, 3, 4, 9)]
    assert delays == [
        FAILURE_RETRY,
        FAILURE_RETRY * 2,
        MAX_FAILURE_RETRY,
        MAX_FAILURE_RETRY,
        MAX_FAILURE_RETRY,
    ]


def test_backoff_never_skips_the_next_window():
    # 01:50: the window is 40 minutes away, so a 2 hour backoff must not pass it.
    assert after_failure(at(18, 1, 50), yesterday_complete=False, failures=4) == timedelta(
        minutes=40
    )


def test_the_window_survives_the_nights_the_clocks_change():
    # Clocks go forward at 02:00 on 2026-10-04, so 02:30 does not exist that night: the window
    # opens at the same instant as 02:30 AEST (03:30 AEDT), 4.5 hours after 22:00 AEST.
    assert until_next_window(at(3, 22, month=10)) == timedelta(hours=4, minutes=30)
    # Clocks go back at 03:00 on 2026-04-05; 02:30 happens twice and the first one counts.
    assert until_next_window(at(4, 22, month=4)) == timedelta(hours=4, minutes=30)
    # The next night is back to a plain 02:30 wall clock, whatever the offset.
    assert until_next_window(at(5, 22, month=4)) == timedelta(hours=4, minutes=30)
    assert until_next_window(at(4, 22, month=10)) == timedelta(hours=4, minutes=30)


@pytest.mark.parametrize("hour", range(24))
def test_the_delay_is_always_positive(hour):
    now = at(18, hour, 15)
    assert after_success(now, yesterday_complete=True) > timedelta(0)
    assert after_success(now, yesterday_complete=False) > timedelta(0)
    assert after_failure(now, yesterday_complete=False, failures=1) > timedelta(0)
