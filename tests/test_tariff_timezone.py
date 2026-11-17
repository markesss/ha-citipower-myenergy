"""The tariff's pricing clock: selectable Australian zones plus GMT, and 23/25-hour days.

Pure tests (no Home Assistant). They need a time zone database; on Windows install the
``tzdata`` package (it is in requirements-dev.txt).

Daylight saving dates used below (Australia/Melbourne, Sydney, Hobart and Canberra change on
the same dates). Clocks go back from 3am AEDT to 2am AEST on the FIRST SUNDAY OF APRIL, so
that day has 25 hours, and forward from 2am AEST to 3am AEDT on the FIRST SUNDAY OF OCTOBER,
so that day has 23 hours:

* 2025: Sunday 6 April (25 hours) and Sunday 5 October (23 hours)
* 2026: Sunday 5 April (25 hours) and Sunday 4 October (23 hours)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from custom_components.citipower_myenergy.const import MARKET_TZ
from custom_components.citipower_myenergy.exceptions import TariffError
from custom_components.citipower_myenergy.tariff import (
    ALLOWED_TIMEZONES,
    DEFAULT_TIMEZONE,
    KIND_IMPORT,
    LEGACY_TIMEZONE,
    Period,
    Tariff,
    TariffVersion,
    Tier,
    Window,
    cost_for_day,
    cost_for_day_by_period,
    daily_supply_dollars,
    flat_tariff,
    local_day,
    local_day_hours,
    zone_for,
)

try:
    ZoneInfo("Australia/Melbourne")
except ZoneInfoNotFoundError:  # pragma: no cover - only on a machine with no tz database
    pytest.skip("no time zone database; pip install tzdata", allow_module_level=True)

MELBOURNE = ZoneInfo("Australia/Melbourne")
D0 = date(2026, 1, 1)
DST_ZONES = ("Australia/Melbourne", "Australia/Sydney", "Australia/Hobart", "Australia/Canberra")

FALL_BACK_2025, SPRING_FORWARD_2025 = date(2025, 4, 6), date(2025, 10, 5)
FALL_BACK_2026, SPRING_FORWARD_2026 = date(2026, 4, 5), date(2026, 10, 4)


def tariff_in(zone: str | None, *periods: Period, **kwargs) -> Tariff:
    return Tariff(
        (TariffVersion(effective_from=D0, import_periods=periods, timezone=zone, **kwargs),)
    )


def rate(name: str, *windows: Window, cents: float, tiers: tuple[Tier, ...] | None = None):
    return Period(name, windows, tiers or (Tier(None, cents),))


def glohome(zone: str | None) -> Tariff:
    """GloBird GLOHOME Flexible Rate, CitiPower (rates inc. GST): supply 92.40, 3% discount."""
    return tariff_in(
        zone,
        rate("Peak", Window(16, 21), cents=31.46),
        rate("Off-peak", Window(21, 11), cents=16.50),
        rate("Shoulder", Window(11, 16), cents=13.20),
        supply_c_per_day=92.40,
        discount_pct=3.0,
    )


def whole_day(tariff: Tariff, day: date, kwh: float = 1.0) -> dict[datetime, float]:
    return dict.fromkeys(local_day_hours(day, tariff.tzinfo), kwh)


# --- The list of zones ------------------------------------------------------------------------


def test_the_allowed_zones_are_the_australian_whole_hour_zones_and_gmt() -> None:
    assert ALLOWED_TIMEZONES == (
        "Australia/Melbourne",
        "Australia/Sydney",
        "Australia/Hobart",
        "Australia/Canberra",
        "Australia/Brisbane",
        "Australia/Perth",
        "GMT",
    )
    assert DEFAULT_TIMEZONE == "Australia/Melbourne"
    assert LEGACY_TIMEZONE == "Australia/Brisbane"


@pytest.mark.parametrize("zone", ALLOWED_TIMEZONES)
def test_every_offered_zone_always_has_a_whole_hour_offset(zone: str) -> None:
    """Hourly statistics only line up with local whole hours if the offset is whole hours."""
    tz = zone_for(zone)
    moment = datetime(2024, 1, 1, tzinfo=UTC)
    while moment < datetime(2028, 1, 1, tzinfo=UTC):
        offset = moment.astimezone(tz).utcoffset()
        assert offset is not None and offset % timedelta(hours=1) == timedelta(0), (zone, moment)
        moment += timedelta(hours=3)


@pytest.mark.parametrize("zone", ["Australia/Adelaide", "Australia/Darwin", "Australia/Eucla"])
def test_half_hour_zones_exist_but_are_refused(zone: str) -> None:
    """They really do have fractional offsets, which is why they are not offered."""
    offset = datetime(2026, 7, 1, tzinfo=UTC).astimezone(ZoneInfo(zone)).utcoffset()
    assert offset is not None and offset % timedelta(hours=1) != timedelta(0)
    with pytest.raises(TariffError, match="not available"):
        glohome(zone).validate()


@pytest.mark.parametrize("zone", ["Australia/Lord_Howe", "Australia/Broken_Hill", "Asia/Tokyo"])
def test_other_zones_are_refused_with_the_choices_listed(zone: str) -> None:
    with pytest.raises(TariffError, match="Australia/Melbourne") as err:
        glohome(zone).validate()
    assert "GMT" in str(err.value)


def test_zone_for_a_missing_database_entry_explains_tzdata() -> None:
    with pytest.raises(TariffError, match="tzdata"):
        zone_for("Australia/Nowhere")


def test_no_zone_is_fixed_aest_and_gmt_is_utc() -> None:
    assert zone_for(None) is MARKET_TZ
    assert zone_for("GMT") is UTC
    assert glohome(None).tzinfo is MARKET_TZ
    assert glohome(None).zone_name == "Australia/Brisbane"
    assert glohome("Australia/Perth").zone_name == "Australia/Perth"


# --- Serialisation -----------------------------------------------------------------------------


def test_the_zone_round_trips_and_is_left_out_when_unset() -> None:
    with_zone = glohome("Australia/Melbourne").to_dict()
    assert with_zone["versions"][0]["timezone"] == "Australia/Melbourne"
    assert Tariff.from_dict(with_zone) == glohome("Australia/Melbourne")

    legacy = glohome(None).to_dict()
    assert "timezone" not in legacy["versions"][0]  # not written back
    assert Tariff.from_dict(legacy) == glohome(None)
    assert Tariff.from_dict(legacy).to_dict() == legacy


def test_utc_is_stored_as_gmt() -> None:
    data = glohome("GMT").to_dict()
    data["versions"][0]["timezone"] = "UTC"
    assert Tariff.from_dict(data).versions[0].timezone == "GMT"


def test_a_stored_timezone_mode_is_rejected_with_a_helpful_message() -> None:
    data = glohome(None).to_dict()
    data["versions"][0]["timezone_mode"] = "clock"
    with pytest.raises(TariffError, match="set 'timezone' to a zone name"):
        Tariff.from_dict(data)


@pytest.mark.parametrize("bad", [5, ["Australia/Melbourne"], {"zone": "x"}])
def test_a_zone_that_is_not_a_name_is_rejected(bad) -> None:
    data = glohome(None).to_dict()
    data["versions"][0]["timezone"] = bad
    with pytest.raises(TariffError, match="timezone must be a zone name"):
        Tariff.from_dict(data)


def test_every_version_must_share_one_clock() -> None:
    old = TariffVersion(
        effective_from=date(2026, 1, 1),
        import_periods=(rate("All day", Window(0, 24), cents=20.0),),
        timezone="Australia/Melbourne",
    )
    new = TariffVersion(
        effective_from=date(2026, 6, 1),
        import_periods=(rate("All day", Window(0, 24), cents=30.0),),
        timezone="Australia/Perth",
    )
    with pytest.raises(TariffError, match="same time zone"):
        Tariff((old, new)).validate()
    # No zone and Brisbane are the same clock (fixed AEST), so that mix is fine.
    legacy = TariffVersion(effective_from=date(2026, 1, 1), import_periods=old.import_periods)
    brisbane = TariffVersion(
        effective_from=date(2026, 6, 1),
        import_periods=new.import_periods,
        timezone="Australia/Brisbane",
    )
    Tariff((legacy, brisbane)).validate()


# --- The local day -----------------------------------------------------------------------------


@pytest.mark.parametrize("zone", DST_ZONES)
@pytest.mark.parametrize(
    ("day", "hours"),
    [
        (FALL_BACK_2025, 25),
        (SPRING_FORWARD_2025, 23),
        (FALL_BACK_2026, 25),
        (SPRING_FORWARD_2026, 23),
        (date(2026, 7, 15), 24),
        (date(2026, 11, 10), 24),
    ],
)
def test_daylight_saving_days_have_23_or_25_hours(zone: str, day: date, hours: int) -> None:
    got = local_day_hours(day, zone_for(zone))
    assert len(got) == hours
    # Contiguous UTC hours, starting at local midnight and ending where the next day starts.
    assert all(b - a == timedelta(hours=1) for a, b in zip(got, got[1:], strict=False))
    assert got[0] == datetime.combine(day, datetime.min.time(), tzinfo=zone_for(zone)).astimezone(
        UTC
    )
    assert (
        got[-1] + timedelta(hours=1) == local_day_hours(day + timedelta(days=1), zone_for(zone))[0]
    )
    assert local_day(got[0], zone_for(zone)) == day
    assert local_day(got[-1], zone_for(zone)) == day


def test_the_changeover_dates_are_the_first_sundays() -> None:
    for day in (FALL_BACK_2025, SPRING_FORWARD_2025, FALL_BACK_2026, SPRING_FORWARD_2026):
        assert day.weekday() == 6
        assert day.day <= 7  # the first Sunday of the month
    assert (FALL_BACK_2025.month, SPRING_FORWARD_2025.month) == (4, 10)
    assert (FALL_BACK_2026.month, SPRING_FORWARD_2026.month) == (4, 10)


@pytest.mark.parametrize("zone", ["Australia/Brisbane", "Australia/Perth", "GMT", None])
@pytest.mark.parametrize("day", [FALL_BACK_2026, SPRING_FORWARD_2026, date(2026, 11, 10)])
def test_zones_without_daylight_saving_always_have_24_hours(zone: str | None, day: date) -> None:
    assert len(local_day_hours(day, zone_for(zone))) == 24


def test_the_default_clock_is_still_aest() -> None:
    day = date(2026, 11, 10)
    assert local_day_hours(day)[0] == datetime(2026, 11, 9, 14, tzinfo=UTC)
    assert local_day(datetime(2026, 11, 9, 13, 30, tzinfo=UTC)) == date(2026, 11, 9)


# --- Pricing on the clock ------------------------------------------------------------------------
#
# GLOHOME, 1 kWh in every hour of one local day. By local hour (rates x 0.97, GST included):
#   4pm-9pm   5 h x 31.46 c = 157.30 c      11am-4pm  5 h x 13.20 c = 66.00 c
#   9pm-11am 14 h x 16.50 c = 231.00 c      supply 92.40 c on the first hour
#   (157.30 + 66.00 + 231.00 + 92.40) x 0.97 = 530.299 c = $5.30299 for a 24-hour day
# Hour costs: peak 0.305162, shoulder 0.128040, off-peak 0.160050.
PEAK, SHOULDER, OFFPEAK = 0.305162, 0.12804, 0.16005


def test_glohome_summer_day_is_shifted_one_hour_against_fixed_aest() -> None:
    """On the same UTC hour Melbourne (AEDT, UTC+11) and fixed AEST (UTC+10) are an hour apart."""
    day = date(2026, 11, 10)
    melbourne, fixed = glohome("Australia/Melbourne"), glohome(None)
    m_costs = cost_for_day(whole_day(melbourne, day), melbourne, KIND_IMPORT, day)
    f_costs = cost_for_day(whole_day(fixed, day), fixed, KIND_IMPORT, day)

    # Both days cost the same in total: only where each hour falls differs.
    assert sum(m_costs.values()) == pytest.approx(5.30299, abs=1e-9)
    assert sum(f_costs.values()) == pytest.approx(5.30299, abs=1e-9)

    # 05:00 UTC is 4pm in Melbourne (peak) but 3pm AEST (shoulder).
    at_0500 = datetime(2026, 11, 10, 5, tzinfo=UTC)
    assert m_costs[at_0500] == pytest.approx(PEAK, abs=1e-9)
    assert f_costs[at_0500] == pytest.approx(SHOULDER, abs=1e-9)
    # 10:00 UTC is 9pm in Melbourne (off-peak) but 8pm AEST (peak).
    at_1000 = datetime(2026, 11, 10, 10, tzinfo=UTC)
    assert m_costs[at_1000] == pytest.approx(OFFPEAK, abs=1e-9)
    assert f_costs[at_1000] == pytest.approx(PEAK, abs=1e-9)
    # The supply charge sits on the first hour of each clock's day: 13:00Z vs 14:00Z the day before.
    assert max(m_costs, key=m_costs.get) == datetime(2026, 11, 9, 13, tzinfo=UTC)
    assert m_costs[datetime(2026, 11, 9, 13, tzinfo=UTC)] == pytest.approx(
        (16.50 + 92.40) * 0.97 / 100, abs=1e-9
    )
    assert f_costs[datetime(2026, 11, 9, 14, tzinfo=UTC)] == pytest.approx(
        (16.50 + 92.40) * 0.97 / 100, abs=1e-9
    )


def test_in_winter_melbourne_and_fixed_aest_are_the_same_clock() -> None:
    day = date(2026, 7, 15)
    melbourne, fixed = glohome("Australia/Melbourne"), glohome(None)
    assert cost_for_day(whole_day(melbourne, day), melbourne, KIND_IMPORT, day) == cost_for_day(
        whole_day(fixed, day), fixed, KIND_IMPORT, day
    )


def test_brisbane_is_exactly_the_old_fixed_aest_pricing() -> None:
    for day in (date(2026, 11, 10), FALL_BACK_2026, SPRING_FORWARD_2026, date(2026, 7, 15)):
        a, b = glohome(None), glohome("Australia/Brisbane")
        assert cost_for_day(whole_day(a, day), a, KIND_IMPORT, day) == cost_for_day(
            whole_day(b, day), b, KIND_IMPORT, day
        )


def test_gmt_reads_windows_on_utc_with_whole_utc_days() -> None:
    tariff = glohome("GMT")
    day = date(2026, 11, 10)
    costs = cost_for_day(whole_day(tariff, day), tariff, KIND_IMPORT, day)
    assert len(costs) == 24
    assert costs[datetime(2026, 11, 10, 16, tzinfo=UTC)] == pytest.approx(PEAK, abs=1e-9)
    assert costs[datetime(2026, 11, 10, 15, tzinfo=UTC)] == pytest.approx(SHOULDER, abs=1e-9)
    assert costs[datetime(2026, 11, 10, 0, tzinfo=UTC)] == pytest.approx(
        (16.50 + 92.40) * 0.97 / 100, abs=1e-9
    )
    assert sum(costs.values()) == pytest.approx(5.30299, abs=1e-9)


def test_perth_is_whole_days_at_utc_plus_eight() -> None:
    tariff = glohome("Australia/Perth")
    day = date(2026, 11, 10)
    hours = local_day_hours(day, tariff.tzinfo)
    assert hours[0] == datetime(2026, 11, 9, 16, tzinfo=UTC)  # midnight Perth
    costs = cost_for_day(dict.fromkeys(hours, 1.0), tariff, KIND_IMPORT, day)
    assert costs[datetime(2026, 11, 10, 8, tzinfo=UTC)] == pytest.approx(PEAK, abs=1e-9)  # 4pm


# --- 23 and 25 hour days -------------------------------------------------------------------------
#
# Two periods and no GST or discount, so the sums are easy: Night 0-6 at 10 c and Day 6-24 at
# 20 c, supply 100 c, 1 kWh in every real hour.
#   25-hour day (clocks go back): local hours 0,1,2,2,3,4,5 = 7 night hours, 18 day hours
#       (7 x 10 + 18 x 20 + 100) c = $5.30
#   23-hour day (clocks go forward): local hours 0,1,3,4,5 = 5 night hours, 18 day hours
#       (5 x 10 + 18 x 20 + 100) c = $5.10


def night_day(zone: str) -> Tariff:
    return tariff_in(
        zone,
        rate("Night", Window(0, 6), cents=10.0),
        rate("Day", Window(6, 24), cents=20.0),
        supply_c_per_day=100.0,
    )


@pytest.mark.parametrize("zone", DST_ZONES)
def test_a_25_hour_day_prices_the_repeated_hour_twice_and_charges_supply_once(zone: str) -> None:
    tariff = night_day(zone)
    costs = cost_for_day(whole_day(tariff, FALL_BACK_2026), tariff, KIND_IMPORT, FALL_BACK_2026)
    assert len(costs) == 25
    assert sum(costs.values()) == pytest.approx(5.30, abs=1e-9)
    # Both 2:00s (02:00 AEDT then 02:00 AEST) are night hours at 10 c.
    assert costs[datetime(2026, 4, 4, 15, tzinfo=UTC)] == pytest.approx(0.10)
    assert costs[datetime(2026, 4, 4, 16, tzinfo=UTC)] == pytest.approx(0.10)
    # Supply on the first hour only: midnight AEDT = 13:00Z the day before.
    first = datetime(2026, 4, 4, 13, tzinfo=UTC)
    assert costs[first] == pytest.approx(0.10 + 1.00)
    assert sum(1 for cost in costs.values() if cost > 0.5) == 1


@pytest.mark.parametrize("zone", DST_ZONES)
def test_a_23_hour_day_has_no_skipped_hour_and_one_supply_charge(zone: str) -> None:
    tariff = night_day(zone)
    day = SPRING_FORWARD_2026
    costs = cost_for_day(whole_day(tariff, day), tariff, KIND_IMPORT, day)
    assert len(costs) == 23
    assert sum(costs.values()) == pytest.approx(5.10, abs=1e-9)
    local_hours = [moment.astimezone(zone_for(zone)).hour for moment in sorted(costs)]
    assert 2 not in local_hours  # 2am does not exist: the clock jumps from 1:59 to 3:00
    assert local_hours[:5] == [0, 1, 3, 4, 5]


def test_an_hour_from_the_neighbouring_day_is_rejected() -> None:
    tariff = night_day("Australia/Melbourne")
    stray = {**whole_day(tariff, SPRING_FORWARD_2026), datetime(2026, 10, 4, 13, tzinfo=UTC): 1.0}
    # 13:00Z on the 4th is midnight AEDT on the 5th: a different local day.
    with pytest.raises(TariffError, match="not in local day"):
        cost_for_day(stray, tariff, KIND_IMPORT, SPRING_FORWARD_2026)


@pytest.mark.parametrize("day", [FALL_BACK_2026, SPRING_FORWARD_2026, date(2026, 11, 10)])
def test_tiers_reset_at_local_midnight_on_changeover_days(day: date) -> None:
    """A 5 kWh/day free allowance applies to the local day however many hours it has."""
    tiers = (Tier(5.0, 0.0), Tier(None, 10.0))
    tariff = tariff_in(
        "Australia/Melbourne", rate("All day", Window(0, 24), cents=0.0, tiers=tiers)
    )
    hours = local_day_hours(day, tariff.tzinfo)
    this_day = cost_for_day(dict.fromkeys(hours, 1.0), tariff, KIND_IMPORT, day)
    assert sum(this_day.values()) == pytest.approx((len(hours) - 5) * 10 / 100, abs=1e-9)
    # The next day starts the allowance again from zero.
    next_day = day + timedelta(days=1)
    next_hours = local_day_hours(next_day, tariff.tzinfo)
    after = cost_for_day(dict.fromkeys(next_hours, 1.0), tariff, KIND_IMPORT, next_day)
    assert sum(after.values()) == pytest.approx((len(next_hours) - 5) * 10 / 100, abs=1e-9)


@pytest.mark.parametrize(
    "day", [FALL_BACK_2025, SPRING_FORWARD_2025, FALL_BACK_2026, SPRING_FORWARD_2026]
)
def test_period_breakdown_matches_the_total_on_changeover_days(day: date) -> None:
    tariff = glohome("Australia/Melbourne")
    kwh = {
        hour: ((i * 7) % 11) / 3 + 0.25
        for i, hour in enumerate(local_day_hours(day, tariff.tzinfo))
    }
    total = cost_for_day(kwh, tariff, KIND_IMPORT, day)
    split = cost_for_day_by_period(kwh, tariff, KIND_IMPORT, day)
    assert set(split) == set(total) == set(kwh)
    supply = daily_supply_dollars(tariff.versions[0])
    assert sum(item.cost for item in split.values()) + supply == pytest.approx(
        sum(total.values()), abs=1e-9
    )
    assert sum(item.kwh for item in split.values()) == pytest.approx(sum(kwh.values()))
    # Hour by hour too: only the first hour carries the supply charge.
    first = min(total)
    for moment, item in split.items():
        extra = supply if moment == first else 0.0
        assert item.cost + extra == pytest.approx(total[moment], abs=1e-9)


def test_flat_tariff_accepts_a_zone() -> None:
    tariff = flat_tariff(25.0, effective_from=D0, timezone="Australia/Sydney")
    assert tariff.versions[0].timezone == "Australia/Sydney"
    assert tariff.to_dict()["versions"][0]["timezone"] == "Australia/Sydney"


def test_prime_zones_fills_the_cache_for_every_allowed_zone() -> None:
    """After priming, ``zone_for`` is a cache hit, so it never reads the database on the loop."""
    from custom_components.citipower_myenergy.tariff import prime_zones, zone_for

    zone_for.cache_clear()
    prime_zones()
    assert zone_for.cache_info().currsize >= len(ALLOWED_TIMEZONES)
    misses = zone_for.cache_info().misses
    for name in ALLOWED_TIMEZONES:
        zone_for(name)
    assert zone_for.cache_info().misses == misses


def test_prime_zones_skips_a_zone_the_database_lacks(monkeypatch) -> None:
    from custom_components.citipower_myenergy import tariff as tariff_module

    def _no_database(key):
        raise ZoneInfoNotFoundError(key)

    tariff_module.zone_for.cache_clear()
    monkeypatch.setattr(tariff_module, "ZoneInfo", _no_database)
    tariff_module.prime_zones()  # must not raise; zone_for raises when a tariff needs the zone
    with pytest.raises(TariffError):
        tariff_module.zone_for("Australia/Sydney")
    tariff_module.zone_for.cache_clear()
