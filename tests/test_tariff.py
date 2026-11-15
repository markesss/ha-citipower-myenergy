"""Tariff pricing: pure, needs nothing but the standard library."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.citipower_myenergy.const import MARKET_TZ
from custom_components.citipower_myenergy.exceptions import TariffError
from custom_components.citipower_myenergy.tariff import (
    KIND_EXPORT,
    KIND_IMPORT,
    Period,
    Tariff,
    TariffVersion,
    Tier,
    Window,
    cost_for_day,
    flat_tariff,
    local_day,
    local_day_hours,
)

TUESDAY = date(2026, 11, 10)
D0 = date(2026, 1, 1)


def flat(rate: float) -> tuple[Period, ...]:
    return (Period("All day", (Window(0, 24),), (Tier(None, rate),)),)


def one(version: TariffVersion) -> Tariff:
    return Tariff((version,))


def market_day(day: date, per_hour: float | dict[int, float]) -> dict[datetime, float]:
    """A full AEST day of readings: one value for every hour, or per-hour overrides."""
    out = {}
    for hour in range(24):
        value = per_hour if isinstance(per_hour, int | float) else per_hour.get(hour, 0.0)
        out[datetime(day.year, day.month, day.day, hour, tzinfo=MARKET_TZ)] = value
    return out


def at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=MARKET_TZ).astimezone(UTC)


def total(costs: dict[datetime, float]) -> float:
    return sum(costs.values())


# --- Real plans (CitiPower, inc. GST) ---------------------------------------------------

GLOHOME = TariffVersion(
    effective_from=D0,
    supply_c_per_day=92.40,
    discount_pct=3.0,
    import_periods=(
        Period("Peak", (Window(16, 21),), (Tier(None, 31.46),)),
        # 9pm to 11am crosses midnight.
        Period("Off-peak", (Window(21, 11),), (Tier(None, 16.50),)),
        Period("Shoulder", (Window(11, 16),), (Tier(None, 13.20),)),
    ),
    export_periods=(
        Period("Peak", (Window(16, 21),), (Tier(None, 3.00),)),
        # One rate, two disjoint windows.
        Period("Low", (Window(21, 10), Window(14, 16)), (Tier(None, 0.10),)),
        Period("Zero", (Window(10, 14),), (Tier(None, 0.00),)),
    ),
)

FOUR4FREE = TariffVersion(
    effective_from=D0,
    supply_c_per_day=129.80,
    import_periods=(
        Period("Peak", (Window(16, 23),), (Tier(None, 36.63),)),
        Period("Free window", (Window(11, 15),), (Tier(30, 0.00), Tier(None, 11.00))),
        Period("Other", (Window(15, 16), Window(23, 11)), (Tier(None, 22.00),)),
    ),
    export_periods=(
        Period("Peak", (Window(16, 23),), (Tier(30, 5.00), Tier(None, 3.00))),
        Period("Other", (Window(23, 16),), (Tier(None, 0.00),)),
    ),
)


def test_flat_rate_with_supply_charge():
    tariff = flat_tariff(25.0, effective_from=D0, supply_c_per_day=100.0)
    costs = cost_for_day(market_day(TUESDAY, 0.5), tariff, KIND_IMPORT, TUESDAY)
    # 24 x 0.5 kWh x 25c = 300c, plus 100c supply = $4.00.
    assert total(costs) == pytest.approx(4.00)
    first = at(TUESDAY, 0)
    assert costs[first] == pytest.approx((0.5 * 25 + 100) / 100)
    assert costs[at(TUESDAY, 1)] == pytest.approx(0.125)


def test_gst_exclusive_prices_are_uplifted():
    tariff = flat_tariff(25.0, effective_from=D0, supply_c_per_day=100.0, prices_include_gst=False)
    costs = cost_for_day(market_day(TUESDAY, 0.5), tariff, KIND_IMPORT, TUESDAY)
    assert total(costs) == pytest.approx(4.40)


def test_discount_applies_to_supply_and_usage():
    tariff = flat_tariff(25.0, effective_from=D0, supply_c_per_day=100.0, discount_pct=10.0)
    costs = cost_for_day(market_day(TUESDAY, 0.5), tariff, KIND_IMPORT, TUESDAY)
    assert total(costs) == pytest.approx(3.60)


def test_export_gets_neither_gst_nor_discount_nor_supply_charge():
    tariff = flat_tariff(
        25.0,
        effective_from=D0,
        supply_c_per_day=100.0,
        export_c_per_kwh=5.0,
        discount_pct=10.0,
        prices_include_gst=False,
    )
    costs = cost_for_day(market_day(TUESDAY, 1.0), tariff, KIND_EXPORT, TUESDAY)
    assert total(costs) == pytest.approx(24 * 5.0 / 100)


def test_no_export_periods_means_zero_credit():
    tariff = flat_tariff(25.0, effective_from=D0)
    costs = cost_for_day(market_day(TUESDAY, 1.0), tariff, KIND_EXPORT, TUESDAY)
    assert set(costs.values()) == {0.0}
    assert len(costs) == 24


def test_glohome_import_day():
    costs = cost_for_day(market_day(TUESDAY, 1.0), one(GLOHOME), KIND_IMPORT, TUESDAY)
    # 1 kWh every hour. Peak 16-20 = 5h, off-peak 21-23 + 0-10 = 14h, shoulder 11-15 = 5h:
    # 5 x 31.46 + 14 x 16.50 + 5 x 13.20 = 454.30c; supply 92.40c; sum 546.70c; less 3%.
    assert total(costs) == pytest.approx(546.70 * 0.97 / 100)
    # The supply charge sits on the first hour: (16.50 + 92.40) x 0.97 = 105.633c.
    assert costs[at(TUESDAY, 0)] == pytest.approx(1.05633)
    assert costs[at(TUESDAY, 17)] == pytest.approx(31.46 * 0.97 / 100)


def test_glohome_export_day():
    costs = cost_for_day(market_day(TUESDAY, 1.0), one(GLOHOME), KIND_EXPORT, TUESDAY)
    # Peak 5h x 3.00 + zero 4h x 0.00 + low (13h + 2h) x 0.10 = 16.50c. No discount on export.
    assert total(costs) == pytest.approx(0.165)
    assert costs[at(TUESDAY, 15)] == pytest.approx(0.001)  # 2pm-4pm window
    assert costs[at(TUESDAY, 11)] == 0.0


def test_four4free_import_allowance_spans_hours():
    # 12 kWh in each of 11, 12, 13, 14: the 30 kWh/day free allowance is used up part way
    # through hour 13 (24 kWh used, 6 kWh free left, then 6 kWh at 11c).
    costs = cost_for_day(
        market_day(TUESDAY, {11: 12, 12: 12, 13: 12, 14: 12}),
        one(FOUR4FREE),
        KIND_IMPORT,
        TUESDAY,
    )
    assert costs[at(TUESDAY, 11)] == 0.0
    assert costs[at(TUESDAY, 12)] == 0.0
    assert costs[at(TUESDAY, 13)] == pytest.approx(6 * 11.00 / 100)
    assert costs[at(TUESDAY, 14)] == pytest.approx(12 * 11.00 / 100)
    # Supply charge on hour 0 (which used nothing).
    assert costs[at(TUESDAY, 0)] == pytest.approx(1.298)
    assert total(costs) == pytest.approx(1.298 + 0.66 + 1.32)


def test_four4free_import_peak_and_other_rates():
    costs = cost_for_day(
        market_day(TUESDAY, {15: 2, 16: 2, 22: 1, 23: 1, 3: 4}),
        one(FOUR4FREE),
        KIND_IMPORT,
        TUESDAY,
    )
    assert costs[at(TUESDAY, 15)] == pytest.approx(2 * 22.00 / 100)  # 3pm-4pm "other"
    assert costs[at(TUESDAY, 16)] == pytest.approx(2 * 36.63 / 100)
    assert costs[at(TUESDAY, 22)] == pytest.approx(36.63 / 100)
    assert costs[at(TUESDAY, 23)] == pytest.approx(22.00 / 100)  # 11pm onwards "other"
    assert costs[at(TUESDAY, 3)] == pytest.approx(4 * 22.00 / 100)
    assert costs[at(TUESDAY, 0)] == pytest.approx(1.298)  # supply charge, no usage


def test_four4free_export_allowance():
    costs = cost_for_day(
        market_day(TUESDAY, {16: 20, 17: 20, 8: 5}), one(FOUR4FREE), KIND_EXPORT, TUESDAY
    )
    assert costs[at(TUESDAY, 16)] == pytest.approx(20 * 5.00 / 100)
    # 10 kWh of allowance left, then 10 kWh at 3c.
    assert costs[at(TUESDAY, 17)] == pytest.approx((10 * 5.00 + 10 * 3.00) / 100)
    assert costs[at(TUESDAY, 8)] == 0.0
    assert total(costs) == pytest.approx(1.80)


def test_tier_allowance_resets_each_day():
    tariff = one(FOUR4FREE)
    day_one = cost_for_day(market_day(TUESDAY, {11: 40}), tariff, KIND_IMPORT, TUESDAY)
    next_day = TUESDAY + timedelta(days=1)
    day_two = cost_for_day(market_day(next_day, {11: 40}), tariff, KIND_IMPORT, next_day)
    assert day_one[at(TUESDAY, 11)] == pytest.approx(10 * 11.00 / 100)
    assert day_two[at(next_day, 11)] == pytest.approx(10 * 11.00 / 100)


def test_hours_are_priced_in_time_order_whatever_the_dict_order():
    hours = market_day(TUESDAY, {11: 20, 12: 20})
    reversed_hours = dict(reversed(list(hours.items())))
    costs = cost_for_day(reversed_hours, one(FOUR4FREE), KIND_IMPORT, TUESDAY)
    assert costs[at(TUESDAY, 11)] == 0.0
    assert costs[at(TUESDAY, 12)] == pytest.approx(10 * 11.00 / 100)


# --- Windows and periods -----------------------------------------------------------------


def two_rate(window: Window) -> TariffVersion:
    return TariffVersion(
        effective_from=D0,
        import_periods=(
            Period("Special", (window,), (Tier(None, 50.0),)),
            Period("Rest", (), (Tier(None, 10.0),), catch_all=True),
        ),
    )


def test_midnight_crossing_window():
    version = two_rate(Window(22, 6))
    version.validate()
    special = {22, 23, 0, 1, 2, 3, 4, 5}
    for hour in range(24):
        found = version.period_for(datetime(2026, 11, 10, hour), KIND_IMPORT)
        assert found is not None
        assert found.name == ("Special" if hour in special else "Rest"), hour


def test_end_of_24_means_midnight():
    version = two_rate(Window(20, 24))
    assert version.period_for(datetime(2026, 11, 10, 23), KIND_IMPORT).name == "Special"
    assert version.period_for(datetime(2026, 11, 10, 0), KIND_IMPORT).name == "Rest"


def test_day_of_week_window():
    version = two_rate(Window(16, 20, frozenset({0, 1, 2, 3, 4})))
    version.validate()
    monday, saturday = datetime(2026, 11, 9, 17), datetime(2026, 11, 14, 17)
    assert monday.weekday() == 0 and saturday.weekday() == 5
    assert version.period_for(monday, KIND_IMPORT).name == "Special"
    assert version.period_for(saturday, KIND_IMPORT).name == "Rest"
    costs = cost_for_day(
        market_day(date(2026, 11, 14), 1.0), one(version), KIND_IMPORT, date(2026, 11, 14)
    )
    assert total(costs) == pytest.approx(24 * 10.0 / 100)


def test_window_days_are_the_days_it_starts_on():
    # A Friday 10pm-2am window covers Saturday 1am, but not Friday 1am or Saturday 11pm.
    version = two_rate(Window(22, 2, frozenset({4})))
    version.validate()

    def name(weekday_date: int, hour: int) -> str:
        return version.period_for(datetime(2026, 11, weekday_date, hour), KIND_IMPORT).name

    assert datetime(2026, 11, 13).weekday() == 4  # Friday
    assert name(13, 23) == "Special"
    assert name(14, 1) == "Special"
    assert name(13, 1) == "Rest"
    assert name(14, 23) == "Rest"


# --- Validation ---------------------------------------------------------------------------


def import_only(*periods: Period) -> Tariff:
    return one(TariffVersion(effective_from=D0, import_periods=periods))


def rate(name: str, *windows: Window, catch_all: bool = False) -> Period:
    return Period(name, windows, (Tier(None, 10.0),), catch_all=catch_all)


def test_gap_is_rejected_with_the_uncovered_hour():
    with pytest.raises(TariffError, match=r"no period covers Monday 12:00"):
        import_only(rate("A", Window(0, 12)), rate("B", Window(13, 24))).validate()


def test_overlap_is_rejected():
    with pytest.raises(TariffError, match=r"'A' and 'B' overlap on Monday 11:00"):
        import_only(rate("A", Window(0, 12)), rate("B", Window(11, 24))).validate()


def test_overlap_across_midnight_is_rejected():
    with pytest.raises(TariffError, match="overlap"):
        import_only(rate("A", Window(20, 4)), rate("B", Window(2, 20))).validate()


def test_a_day_of_week_gap_is_rejected():
    with pytest.raises(TariffError, match="Saturday"):
        import_only(rate("Weekdays", Window(0, 24, frozenset(range(5))))).validate()


def test_catch_all_fills_the_gaps():
    import_only(rate("A", Window(0, 12)), rate("Rest", catch_all=True)).validate()


def test_only_one_catch_all_and_it_has_no_windows():
    with pytest.raises(TariffError, match="only one"):
        import_only(rate("A", catch_all=True), rate("B", catch_all=True)).validate()
    with pytest.raises(TariffError, match="no windows"):
        import_only(rate("A", Window(0, 5), catch_all=True)).validate()


def test_a_period_needs_a_window_unless_it_is_catch_all():
    with pytest.raises(TariffError, match="at least one window"):
        import_only(rate("A")).validate()


def test_names_must_be_unique():
    with pytest.raises(TariffError, match="unique"):
        import_only(rate("A", Window(0, 12)), rate("A", Window(12, 24))).validate()


@pytest.mark.parametrize(
    "window",
    [Window(16.5, 20), Window(16, 20.5), Window(5, 5), Window(24, 6), Window(0, 25), Window(-1, 5)],
)
def test_bad_window_edges_are_rejected(window):
    with pytest.raises(TariffError):
        import_only(rate("A", window), rate("Rest", catch_all=True)).validate()


def test_bad_day_numbers_are_rejected():
    with pytest.raises(TariffError, match="days"):
        import_only(rate("A", Window(1, 2, frozenset({7}))), rate("R", catch_all=True)).validate()


@pytest.mark.parametrize(
    "tiers, message",
    [
        ((Tier(30, 0.0), Tier(20, 5.0), Tier(None, 9.0)), "increase"),
        ((Tier(None, 1.0), Tier(None, 2.0)), "only the last"),
        ((Tier(10, 1.0),), "last rate must be open-ended"),
        ((), "at least one rate"),
    ],
)
def test_bad_tiers_are_rejected(tiers, message):
    period = Period("A", (Window(0, 24),), tiers)
    with pytest.raises(TariffError, match=message):
        import_only(period).validate()


def test_other_field_ranges():
    for kwargs in ({"discount_pct": 101.0}, {"supply_c_per_day": -1.0}):
        version = TariffVersion(effective_from=D0, import_periods=flat(1.0), **kwargs)
        with pytest.raises(TariffError):
            version.validate()
    with pytest.raises(TariffError, match="import period"):
        TariffVersion(effective_from=D0, import_periods=()).validate()


def test_versions_must_be_ordered_and_distinct():
    a = TariffVersion(effective_from=date(2026, 7, 1), import_periods=flat(1.0))
    b = TariffVersion(effective_from=date(2026, 1, 1), import_periods=flat(1.0))
    with pytest.raises(TariffError, match="distinct"):
        Tariff((a, b)).validate()
    with pytest.raises(TariffError, match="distinct"):
        Tariff((a, a)).validate()


def test_hours_outside_the_day_are_rejected():
    tariff = flat_tariff(10.0, effective_from=D0)
    stray = {at(TUESDAY + timedelta(days=1), 0): 1.0}
    with pytest.raises(TariffError, match="not in local day"):
        cost_for_day(stray, tariff, KIND_IMPORT, TUESDAY)


def test_naive_and_off_the_hour_keys_are_rejected():
    tariff = flat_tariff(10.0, effective_from=D0)
    with pytest.raises(TariffError, match="timezone-aware"):
        cost_for_day({datetime(2026, 11, 10, 0): 1.0}, tariff, KIND_IMPORT, TUESDAY)
    with pytest.raises(TariffError, match="on the hour"):
        cost_for_day({at(TUESDAY, 0) + timedelta(minutes=30): 1.0}, tariff, KIND_IMPORT, TUESDAY)


def test_empty_day_costs_nothing():
    assert (
        cost_for_day(
            {}, flat_tariff(10.0, effective_from=D0, supply_c_per_day=50), "import", TUESDAY
        )
        == {}
    )


# --- Versions ------------------------------------------------------------------------------


def test_version_boundary():
    old = TariffVersion(effective_from=date(2026, 1, 1), import_periods=flat(20.0))
    new = TariffVersion(effective_from=date(2026, 7, 1), import_periods=flat(30.0))
    tariff = Tariff((old, new))
    tariff.validate()
    assert tariff.version_for(date(2025, 6, 1)) is old  # the first covers earlier dates
    assert tariff.version_for(date(2026, 6, 30)) is old
    assert tariff.version_for(date(2026, 7, 1)) is new
    before = cost_for_day(
        market_day(date(2026, 6, 30), 1.0), tariff, KIND_IMPORT, date(2026, 6, 30)
    )
    after = cost_for_day(market_day(date(2026, 7, 1), 1.0), tariff, KIND_IMPORT, date(2026, 7, 1))
    assert total(before) == pytest.approx(24 * 20.0 / 100)
    assert total(after) == pytest.approx(24 * 30.0 / 100)


# --- Market time (fixed AEST) ---------------------------------------------------------------


def peak_tariff() -> Tariff:
    return one(
        TariffVersion(
            effective_from=D0,
            import_periods=(
                Period("Peak", (Window(16, 21),), (Tier(None, 40.0),)),
                Period("Rest", (), (Tier(None, 10.0),), catch_all=True),
            ),
        )
    )


def test_windows_are_read_in_aest_all_year():
    # 06:00 UTC is 4pm AEST in June and November alike; Melbourne's clock would say 5pm in
    # November, which must not move the peak.
    for day in (date(2026, 6, 10), date(2026, 11, 10)):
        instant = datetime(day.year, day.month, day.day, 6, tzinfo=UTC)
        costs = cost_for_day({instant: 1.0}, peak_tariff(), KIND_IMPORT, day)
        assert costs[instant] == pytest.approx(0.40)
    # 05:00 UTC is 3pm AEST: outside the peak, even though it is 4pm in Melbourne in summer.
    instant = datetime(2026, 11, 10, 5, tzinfo=UTC)
    assert cost_for_day({instant: 1.0}, peak_tariff(), KIND_IMPORT, TUESDAY)[instant] == (
        pytest.approx(0.10)
    )


def test_every_day_has_24_hours_including_melbourne_dst_changes():
    for day in (date(2026, 10, 4), date(2027, 4, 4), TUESDAY):
        hours = local_day_hours(day)
        assert len(hours) == 24
        assert hours[0] == datetime(day.year, day.month, day.day, tzinfo=MARKET_TZ).astimezone(UTC)
    assert local_day_hours(TUESDAY)[0] == datetime(2026, 11, 9, 14, tzinfo=UTC)


def test_local_day_of_a_moment():
    # 13:30 UTC is 23:30 AEST on the 9th (it would be 00:30 on the 10th in Melbourne).
    assert local_day(datetime(2026, 11, 9, 13, 30, tzinfo=UTC)) == date(2026, 11, 9)
    assert local_day(datetime(2026, 11, 9, 14, 0, tzinfo=UTC)) == TUESDAY


@pytest.mark.parametrize("day", [date(2026, 10, 4), date(2027, 4, 4)])
def test_supply_charge_once_on_melbourne_dst_changeover_dates(day):
    tariff = one(
        TariffVersion(effective_from=D0, supply_c_per_day=100.0, import_periods=flat(10.0))
    )
    costs = cost_for_day(dict.fromkeys(local_day_hours(day), 1.0), tariff, KIND_IMPORT, day)
    assert len(costs) == 24
    assert total(costs) == pytest.approx((24 * 10 + 100) / 100)
    assert costs[min(costs)] == pytest.approx(1.10)


# --- Serialisation ------------------------------------------------------------------------------


def full_tariff() -> Tariff:
    old = TariffVersion(
        effective_from=date(2026, 1, 1),
        import_periods=flat(20.0),
        supply_c_per_day=80.0,
    )
    return Tariff(
        (
            old,
            TariffVersion(
                effective_from=date(2026, 7, 1),
                prices_include_gst=False,
                supply_c_per_day=92.4,
                discount_pct=3.0,
                import_periods=(
                    Period(
                        "Weekday peak",
                        (Window(16, 20, frozenset({0, 1, 2, 3, 4})),),
                        (Tier(None, 40.0),),
                    ),
                    Period("Free", (Window(11, 15),), (Tier(30, 0.0), Tier(None, 11.0))),
                    Period("Rest", (), (Tier(None, 15.0),), catch_all=True),
                ),
                export_periods=(
                    Period("Low", (Window(21, 10), Window(14, 16)), (Tier(None, 0.1),)),
                    Period("Rest", (), (Tier(None, 3.0),), catch_all=True),
                ),
            ),
        )
    )


def test_round_trip_through_json():
    tariff = full_tariff()
    tariff.validate()
    stored = json.loads(json.dumps(tariff.to_dict()))
    assert stored["schema"] == 1
    assert Tariff.from_dict(stored) == tariff


def test_from_dict_sorts_versions_and_accepts_yaml_style_numbers():
    data = full_tariff().to_dict()
    data["versions"].reverse()
    data["versions"][0]["import_periods"][0]["windows"][0]["start"] = 16.0  # YAML floats
    assert Tariff.from_dict(data) == full_tariff()


def test_from_dict_defaults_and_minimal_form():
    tariff = Tariff.from_dict(
        {
            "schema": 1,
            "versions": [
                {
                    "effective_from": "2026-11-01",
                    "import_periods": [
                        {
                            "name": "All day",
                            "windows": [{"start": 0, "end": 24}],
                            "tiers": [{"c_per_kwh": 25}],
                        }
                    ],
                }
            ],
        }
    )
    version = tariff.versions[0]
    assert version.prices_include_gst is True
    assert version.supply_c_per_day == 0 and version.export_periods == ()


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(schema=2), "schema"),
        (lambda d: d.update(versions=[]), "versions"),
        (lambda d: d["versions"][0].update(effective_from="01/02/2026"), "YYYY-MM-DD"),
        (lambda d: d["versions"][0].pop("effective_from"), "effective_from"),
        (lambda d: d["versions"][0].update(prices_include_gst="yes"), "true or false"),
        (
            lambda d: d["versions"][0]["import_periods"][0]["windows"][0].update(start=16.5),
            "whole hour",
        ),
        (lambda d: d["versions"][0]["import_periods"][0]["windows"][0].update(start=True), "whole"),
        (
            lambda d: d["versions"][0]["import_periods"][0]["tiers"][0].update(c_per_kwh="cheap"),
            "number",
        ),
        (lambda d: d["versions"][0].update(timezone_mode="market"), "AEST"),
        (lambda d: d["versions"][0].update(timezone_mode="clock"), "AEST"),
        (lambda d: d["versions"][0]["import_periods"][0].pop("name"), "name"),
    ],
)
def test_from_dict_rejects_malformed_input(mutate, message):
    data = flat_tariff(10.0, effective_from=D0).to_dict()
    mutate(data)
    with pytest.raises(TariffError, match=message):
        Tariff.from_dict(data)


def test_from_dict_rejects_non_mappings():
    with pytest.raises(TariffError):
        Tariff.from_dict([])  # type: ignore[arg-type]
