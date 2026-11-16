"""Which period is it now, and when does it next change (period_at / next_period_change).

Pure tests. Dates used (all 2026, Victorian clocks):

* Daylight saving starts Sunday 4 October (a 23-hour day: 2am AEST becomes 3am AEDT) and ends
  Sunday 5 April (a 25-hour day: 3am AEDT becomes 2am AEST).
* 16 November 2026 is a Monday, so 20 November is a Friday and 23 November a Monday.

GLOHOME-shaped tariff: Peak 16-21, Solar Soak 11-16, Off Peak 21-11 (crosses midnight).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from custom_components.citipower_myenergy.exceptions import TariffError
from custom_components.citipower_myenergy.tariff import (
    SCAN_HOURS,
    Tariff,
    flat_tariff,
    next_period_change,
    period_at,
    rate_at,
)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _period(name: str, start: int, end: int, rate: float, days=None) -> dict:
    return {
        "name": name,
        "windows": [{"start": start, "end": end, "days": days}],
        "tiers": [{"up_to_kwh_per_day": None, "c_per_kwh": rate}],
    }


def _glohome(zone: str | None = "Australia/Melbourne", **extra) -> Tariff:
    version = {
        "effective_from": "2026-01-01",
        "supply_c_per_day": 92.4,
        "discount_pct": 3,
        "import_periods": [
            _period("Peak", 16, 21, 31.46),
            _period("Solar Soak", 11, 16, 13.2),
            _period("Off Peak", 21, 11, 16.5),
        ],
        **extra,
    }
    if zone is not None:
        version["timezone"] = zone
    return Tariff.from_dict({"schema": 1, "versions": [version]})


def _names(tariff: Tariff, text: str) -> tuple[str | None, str | None]:
    """Return the period now and the one the next change brings."""
    when = _utc(text)
    now = period_at(tariff, when)
    change = next_period_change(tariff, when)
    return (now.name if now else None, change[1].name if change and change[1] else None)


# --- period_at -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        ("2026-11-15T22:30", "Off Peak"),  # 09:30 AEDT Monday
        ("2026-11-16T00:00", "Solar Soak"),  # 11:00 AEDT, the first hour of it
        ("2026-11-16T04:59", "Solar Soak"),  # 15:59
        ("2026-11-16T05:00", "Peak"),  # 16:00
        ("2026-11-16T09:59", "Peak"),  # 20:59
        ("2026-11-16T10:00", "Off Peak"),  # 21:00
        ("2026-11-16T13:30", "Off Peak"),  # 00:30 next day, past midnight
    ],
)
def test_period_at_on_the_melbourne_clock(utc: str, expected: str) -> None:
    assert period_at(_glohome(), _utc(utc)).name == expected


def test_a_tariff_with_no_zone_prices_on_fixed_aest() -> None:
    """05:00 UTC is 16:00 in Melbourne in summer (Peak) but 15:00 AEST (Solar Soak)."""
    moment = _utc("2026-11-16T05:00")
    assert period_at(_glohome("Australia/Melbourne"), moment).name == "Peak"
    assert period_at(_glohome(None), moment).name == "Solar Soak"


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(TariffError, match="timezone-aware"):
        period_at(_glohome(), datetime(2026, 11, 16, 12, 0))


def test_the_repeated_hour_on_the_25_hour_day_is_the_same_period_twice() -> None:
    tariff = _glohome()
    # 02:00 AEDT then 02:00 AEST on Sunday 5 April.
    assert period_at(tariff, _utc("2026-04-04T15:30")).name == "Off Peak"
    assert period_at(tariff, _utc("2026-04-04T16:30")).name == "Off Peak"


# --- next_period_change ------------------------------------------------------------------


def test_next_change_is_the_next_different_period() -> None:
    tariff = _glohome()
    assert _names(tariff, "2026-11-16T00:30") == ("Solar Soak", "Peak")
    assert _names(tariff, "2026-11-16T06:00") == ("Peak", "Off Peak")
    when, period = next_period_change(tariff, _utc("2026-11-16T00:30"))
    assert when == _utc("2026-11-16T05:00")  # 16:00 AEDT
    assert period.name == "Peak"


def test_a_stretch_that_crosses_midnight_is_one_period() -> None:
    """From 10pm the next change is 11am the next day, not midnight."""
    tariff = _glohome()
    now = _utc("2026-11-16T11:00")  # 22:00 AEDT
    when, period = next_period_change(tariff, now)
    assert period.name == "Solar Soak"
    assert when == _utc("2026-11-16T00:00").replace(day=17)  # 11:00 AEDT on the 17th


def test_a_moment_inside_an_hour_changes_at_the_next_hour_boundary() -> None:
    when, _ = next_period_change(_glohome(), _utc("2026-11-16T04:59:30"))
    assert when == _utc("2026-11-16T05:00")


def test_the_23_hour_day_moves_the_boundary_with_the_wall_clock() -> None:
    """Off Peak ends at 11:00 local: 01:00 UTC before the clocks go forward, 00:00 UTC after."""
    tariff = _glohome()
    # Before the change (Saturday 3 October, AEST, +10): 11:00 is 01:00 UTC.
    before, _ = next_period_change(tariff, _utc("2026-10-02T12:00"))  # 22:00 AEST
    assert before == _utc("2026-10-03T01:00")
    # The night the clocks go forward: 11:00 AEDT on the 4th is 00:00 UTC.
    over, _ = next_period_change(tariff, _utc("2026-10-03T12:00"))  # 22:00 AEST on the 3rd
    assert over == _utc("2026-10-04T00:00")


def test_the_25_hour_day_moves_the_boundary_with_the_wall_clock() -> None:
    tariff = _glohome()
    # 02:30 AEDT on 5 April; Off Peak runs to 11:00 AEST, which is 01:00 UTC.
    when, period = next_period_change(tariff, _utc("2026-04-04T15:30"))
    assert when == _utc("2026-04-05T01:00")
    assert period.name == "Solar Soak"


def _weekday_tariff() -> Tariff:
    version = {
        "effective_from": "2026-01-01",
        "timezone": "Australia/Brisbane",
        "import_periods": [
            _period("Weekday Peak", 15, 21, 40.0, days=[0, 1, 2, 3, 4]),
            {
                "name": "Other",
                "catch_all": True,
                "tiers": [{"up_to_kwh_per_day": None, "c_per_kwh": 22.0}],
            },
        ],
    }
    return Tariff.from_dict({"schema": 1, "versions": [version]})


def test_a_weekday_only_period_skips_the_weekend() -> None:
    tariff = _weekday_tariff()
    assert _names(tariff, "2026-11-20T06:00") == ("Weekday Peak", "Other")  # Friday 16:00
    # Friday 22:00: the next Weekday Peak is Monday 15:00 AEST (05:00 UTC), 3 days away.
    when, period = next_period_change(tariff, _utc("2026-11-20T12:00"))
    assert period.name == "Weekday Peak"
    assert when == _utc("2026-11-23T05:00")


def test_a_flat_tariff_never_changes() -> None:
    tariff = flat_tariff(25.0, effective_from=date(2026, 1, 1))
    assert next_period_change(tariff, _utc("2026-11-16T05:00")) is None


def _two_versions(second: dict) -> Tariff:
    first = flat_tariff(25.0, effective_from=date(2026, 1, 1), timezone="Australia/Brisbane")
    later = flat_tariff(**second, timezone="Australia/Brisbane")
    return Tariff((first.versions[0], later.versions[0]))


def test_a_new_version_with_different_rates_is_a_change_at_its_first_midnight() -> None:
    tariff = _two_versions({"c_per_kwh": 26.0, "effective_from": date(2026, 7, 1)})
    when, period = next_period_change(tariff, _utc("2026-06-29T00:00"))
    assert when == _utc("2026-06-30T14:00")  # 00:00 AEST on 1 July
    assert period.tiers[0].c_per_kwh == 26.0
    assert rate_at(tariff, when).c_per_kwh == 26.0


def test_a_new_version_that_only_changes_the_supply_charge_is_not_a_change() -> None:
    tariff = _two_versions(
        {"c_per_kwh": 25.0, "effective_from": date(2026, 7, 1), "supply_c_per_day": 120.0}
    )
    assert next_period_change(tariff, _utc("2026-06-29T00:00")) is None


def test_a_version_beyond_the_scan_is_not_seen_yet() -> None:
    """Callers look again after the horizon; the pure function reports only what it scanned."""
    tariff = _two_versions({"c_per_kwh": 26.0, "effective_from": date(2026, 12, 25)})
    assert SCAN_HOURS == 192
    assert next_period_change(tariff, _utc("2026-11-16T00:00")) is None
    seen = next_period_change(tariff, _utc("2026-12-20T00:00"))
    assert seen is not None
    assert seen[0] == _utc("2026-12-24T14:00")


# --- rate_at -----------------------------------------------------------------------------


def test_rate_at_gives_the_headline_and_discounted_rates() -> None:
    peak = rate_at(_glohome(), _utc("2026-11-16T06:00"))
    assert peak.period.name == "Peak"
    assert peak.c_per_kwh == 31.46
    assert peak.discounted_c_per_kwh == pytest.approx(31.46 * 0.97)
    assert peak.tiered is False


def test_prices_entered_ex_gst_are_shown_with_gst() -> None:
    tariff = flat_tariff(10.0, effective_from=date(2026, 1, 1), prices_include_gst=False)
    rate = rate_at(tariff, _utc("2026-11-16T06:00"))
    assert rate.c_per_kwh == pytest.approx(11.0)
    assert rate.discounted_c_per_kwh == pytest.approx(11.0)


def test_an_allowance_period_reports_its_first_rate_and_is_marked_tiered() -> None:
    version = {
        "effective_from": "2026-01-01",
        "timezone": "Australia/Sydney",
        "import_periods": [
            {
                "name": "Free Hours",
                "windows": [{"start": 11, "end": 15}],
                "tiers": [
                    {"up_to_kwh_per_day": 30, "c_per_kwh": 0},
                    {"up_to_kwh_per_day": None, "c_per_kwh": 11.0},
                ],
            },
            {
                "name": "Everything Else",
                "catch_all": True,
                "tiers": [{"up_to_kwh_per_day": None, "c_per_kwh": 30.0}],
            },
        ],
    }
    tariff = Tariff.from_dict({"schema": 1, "versions": [version]})
    rate = rate_at(tariff, _utc("2026-11-16T02:00"))  # 13:00 AEDT
    assert rate.period.name == "Free Hours"
    assert rate.c_per_kwh == 0
    assert rate.tiered is True
