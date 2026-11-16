"""Per-period pricing and period slugs: pure, needs nothing but the standard library.

Expected figures are worked by hand from the retailers' fact sheets (CitiPower, inc. GST).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

import pytest

from custom_components.citipower_myenergy.const import MARKET_TZ
from custom_components.citipower_myenergy.exceptions import TariffError
from custom_components.citipower_myenergy.tariff import (
    KIND_EXPORT,
    KIND_IMPORT,
    HourCost,
    Period,
    Tariff,
    TariffVersion,
    Tier,
    Window,
    cost_for_day,
    cost_for_day_by_period,
    daily_supply_dollars,
    flat_tariff,
    period_slug,
    period_slugs,
)

TUESDAY = date(2026, 11, 10)
D0 = date(2026, 1, 1)

# GLOHOME Flexible Rate: supply 92.40 c/day, 3% discount, rates inc. GST.
GLOHOME = TariffVersion(
    effective_from=D0,
    supply_c_per_day=92.40,
    discount_pct=3.0,
    import_periods=(
        Period("Peak", (Window(16, 21),), (Tier(None, 31.46),)),
        Period("Off Peak", (Window(21, 11),), (Tier(None, 16.50),)),  # crosses midnight
        Period("Solar Soak", (Window(11, 16),), (Tier(None, 13.20),)),
    ),
    export_periods=(
        Period("Peak", (Window(16, 21),), (Tier(None, 3.00),)),
        Period("Low", (Window(21, 10), Window(14, 16)), (Tier(None, 0.10),)),
        Period("Zero", (Window(10, 14),), (Tier(None, 0.00),)),
    ),
)

# FOUR4FREE: the first 30 kWh/day inside the free window cost nothing, then 11 c.
FOUR4FREE = TariffVersion(
    effective_from=D0,
    supply_c_per_day=129.80,
    import_periods=(
        Period("Peak", (Window(16, 23),), (Tier(None, 36.63),)),
        Period("Free window", (Window(11, 15),), (Tier(30, 0.00), Tier(None, 11.00))),
        Period("Other", (Window(15, 16), Window(23, 11)), (Tier(None, 22.00),)),
    ),
)


def day_of(day: date, per_hour: float | dict[int, float]) -> dict[datetime, float]:
    out = {}
    for hour in range(24):
        value = per_hour if isinstance(per_hour, int | float) else per_hour.get(hour, 0.0)
        out[datetime(day.year, day.month, day.day, hour, tzinfo=MARKET_TZ)] = value
    return out


def at(day: date, hour: int) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=MARKET_TZ).astimezone(UTC)


def by_period(breakdown: dict[datetime, HourCost]) -> dict[str, tuple[float, float]]:
    """Sum a breakdown into {period: (kWh, dollars)}."""
    out: dict[str, tuple[float, float]] = {}
    for item in breakdown.values():
        kwh, cost = out.get(item.period, (0.0, 0.0))
        out[item.period] = (kwh + item.kwh, cost + item.cost)
    return out


# --- Breakdown ----------------------------------------------------------------------------


def test_glohome_one_kwh_every_hour_splits_by_period():
    breakdown = cost_for_day_by_period(
        day_of(TUESDAY, 1.0), Tariff((GLOHOME,)), KIND_IMPORT, TUESDAY
    )
    totals = by_period(breakdown)
    # Peak 16-20 is 5 hours at 31.46 c, off-peak 21-23 and 0-10 is 14 hours at 16.50 c and
    # solar soak 11-15 is 5 hours at 13.20 c; every c/kWh is taken down by the 3% discount.
    assert totals["Peak"] == (5.0, pytest.approx(5 * 31.46 * 0.97 / 100))  # $1.52581
    assert totals["Off Peak"] == (14.0, pytest.approx(14 * 16.50 * 0.97 / 100))  # $2.24070
    assert totals["Solar Soak"] == (5.0, pytest.approx(5 * 13.20 * 0.97 / 100))  # $0.64020
    assert totals["Peak"][1] == pytest.approx(1.52581)
    assert totals["Off Peak"][1] == pytest.approx(2.2407)
    assert totals["Solar Soak"][1] == pytest.approx(0.6402)


def test_the_supply_charge_belongs_to_no_period():
    tariff = Tariff((GLOHOME,))
    breakdown = cost_for_day_by_period(day_of(TUESDAY, 1.0), tariff, KIND_IMPORT, TUESDAY)
    # The first hour (midnight, off-peak) costs only its usage here: 16.50 x 0.97 = 16.005 c.
    assert breakdown[at(TUESDAY, 0)] == HourCost("Off Peak", 1.0, pytest.approx(0.16005))
    # 92.40 x 0.97 = 89.628 c, once a day, supplied separately.
    assert daily_supply_dollars(GLOHOME) == pytest.approx(0.89628)
    total = sum(item.cost for item in breakdown.values()) + daily_supply_dollars(GLOHOME)
    assert total == pytest.approx(5.30299)


def test_period_costs_plus_supply_equal_cost_for_day_per_hour_and_per_day():
    tariff = Tariff((GLOHOME,))
    readings = day_of(TUESDAY, {h: 0.25 + h / 10 for h in range(24)})
    breakdown = cost_for_day_by_period(readings, tariff, KIND_IMPORT, TUESDAY)
    combined = cost_for_day(readings, tariff, KIND_IMPORT, TUESDAY)
    first = at(TUESDAY, 0)
    for moment, cost in combined.items():
        supply = daily_supply_dollars(GLOHOME) if moment == first else 0.0
        assert breakdown[moment].cost + supply == pytest.approx(cost, abs=1e-12)
    assert sum(item.cost for item in breakdown.values()) + daily_supply_dollars(
        GLOHOME
    ) == pytest.approx(sum(combined.values()), abs=1e-12)


def test_period_kwh_sums_to_the_days_kwh():
    readings = day_of(TUESDAY, {h: h * 0.123 for h in range(24)})
    breakdown = cost_for_day_by_period(readings, Tariff((GLOHOME,)), KIND_IMPORT, TUESDAY)
    assert sum(kwh for kwh, _ in by_period(breakdown).values()) == pytest.approx(
        sum(readings.values())
    )
    assert {item.kwh for item in breakdown.values()} <= set(readings.values())


def test_four4free_allowance_is_spent_inside_its_own_period():
    tariff = Tariff((FOUR4FREE,))
    # 10 kWh in each of 11:00-14:00 uses the 30 kWh allowance; 14:00's 10 kWh is all at 11 c.
    # Peak, off-peak and shoulder usage elsewhere does not touch the free window's allowance.
    readings = day_of(TUESDAY, {11: 10, 12: 10, 13: 10, 14: 10, 17: 5, 2: 5})
    breakdown = cost_for_day_by_period(readings, tariff, KIND_IMPORT, TUESDAY)
    totals = by_period(breakdown)
    assert totals["Free window"] == (40.0, pytest.approx(1.10))  # 10 kWh x 11 c, no discount
    assert totals["Peak"] == (5.0, pytest.approx(5 * 36.63 / 100))
    assert totals["Other"] == (5.0, pytest.approx(5 * 22.00 / 100))
    assert breakdown[at(TUESDAY, 13)].cost == 0.0  # the last free hour
    assert breakdown[at(TUESDAY, 14)].cost == pytest.approx(1.10)  # all beyond the allowance
    total = sum(item.cost for item in breakdown.values()) + daily_supply_dollars(FOUR4FREE)
    assert total == pytest.approx(
        sum(cost_for_day(readings, tariff, KIND_IMPORT, TUESDAY).values())
    )
    assert daily_supply_dollars(FOUR4FREE) == pytest.approx(1.298)


def test_a_period_with_two_windows_is_one_period_across_midnight():
    readings = day_of(TUESDAY, {23: 2.0, 3: 1.0, 22: 1.0, 9: 1.0})
    breakdown = cost_for_day_by_period(readings, Tariff((GLOHOME,)), KIND_IMPORT, TUESDAY)
    totals = by_period(breakdown)
    # 23:00, 22:00, 03:00 and 09:00 all fall in "Off Peak" (9pm to 11am): 5 kWh.
    assert totals["Off Peak"] == (5.0, pytest.approx(5 * 16.50 * 0.97 / 100))
    assert totals["Peak"] == (0.0, 0.0)


def test_export_breakdown_uses_the_export_periods_and_has_no_supply_charge():
    tariff = Tariff((GLOHOME,))
    readings = day_of(TUESDAY, {17: 2.0, 11: 4.0, 22: 3.0, 15: 1.0})
    breakdown = cost_for_day_by_period(readings, tariff, KIND_EXPORT, TUESDAY)
    totals = by_period(breakdown)
    assert totals["Peak"] == (2.0, pytest.approx(2 * 3.00 / 100))  # no discount, no GST
    assert totals["Zero"] == (4.0, 0.0)
    assert totals["Low"] == (4.0, pytest.approx(4 * 0.10 / 100))  # 22:00 and 15:00
    combined = cost_for_day(readings, tariff, KIND_EXPORT, TUESDAY)
    assert sum(item.cost for item in breakdown.values()) == pytest.approx(sum(combined.values()))


@pytest.mark.parametrize(
    "tariff",
    [
        Tariff((GLOHOME,)),
        Tariff((FOUR4FREE,)),
        flat_tariff(25.0, effective_from=D0, supply_c_per_day=100.0, discount_pct=10.0),
        flat_tariff(25.0, effective_from=D0, supply_c_per_day=100.0, prices_include_gst=False),
    ],
)
def test_invariant_holds_for_every_kind_of_tariff(tariff):
    readings = day_of(TUESDAY, {h: ((h * 7) % 11) / 3 for h in range(24)})
    breakdown = cost_for_day_by_period(readings, tariff, KIND_IMPORT, TUESDAY)
    version = tariff.version_for(TUESDAY)
    total = sum(item.cost for item in breakdown.values()) + daily_supply_dollars(version)
    assert total == pytest.approx(
        sum(cost_for_day(readings, tariff, KIND_IMPORT, TUESDAY).values())
    )
    assert sum(item.kwh for item in breakdown.values()) == pytest.approx(sum(readings.values()))


def test_version_boundary_uses_the_version_for_each_day():
    old = flat_tariff(20.0, effective_from=D0, supply_c_per_day=100.0).versions[0]
    new = flat_tariff(30.0, effective_from=date(2026, 11, 11), supply_c_per_day=200.0).versions[0]
    tariff = Tariff((old, new))
    before = cost_for_day_by_period(day_of(TUESDAY, 1.0), tariff, KIND_IMPORT, TUESDAY)
    after_day = date(2026, 11, 11)
    after = cost_for_day_by_period(day_of(after_day, 1.0), tariff, KIND_IMPORT, after_day)
    assert sum(item.cost for item in before.values()) == pytest.approx(24 * 0.20)
    assert sum(item.cost for item in after.values()) == pytest.approx(24 * 0.30)
    assert daily_supply_dollars(tariff.version_for(TUESDAY)) == pytest.approx(1.00)
    assert daily_supply_dollars(tariff.version_for(after_day)) == pytest.approx(2.00)


def test_gst_exclusive_supply_and_discount():
    version = flat_tariff(
        10.0, effective_from=D0, supply_c_per_day=100.0, discount_pct=10.0, prices_include_gst=False
    ).versions[0]
    # 100 c x 1.1 (GST) x 0.9 (discount) = 99 c.
    assert daily_supply_dollars(version) == pytest.approx(0.99)


def test_empty_day_and_a_kind_with_no_periods():
    tariff = flat_tariff(25.0, effective_from=D0)
    assert cost_for_day_by_period({}, tariff, KIND_IMPORT, TUESDAY) == {}
    breakdown = cost_for_day_by_period(day_of(TUESDAY, 1.0), tariff, KIND_EXPORT, TUESDAY)
    assert len(breakdown) == 24
    assert set(breakdown.values()) == {HourCost("", 1.0, 0.0)}


def test_breakdown_rejects_hours_outside_the_day():
    with pytest.raises(TariffError, match="not in local day"):
        cost_for_day_by_period(
            day_of(date(2026, 11, 11), 1.0), Tariff((GLOHOME,)), KIND_IMPORT, TUESDAY
        )


def test_cost_for_day_results_are_unchanged_by_the_refactor():
    """Pins the day total of the GLOHOME plan: $5.30299 for 1 kWh every hour."""
    costs = cost_for_day(day_of(TUESDAY, 1.0), Tariff((GLOHOME,)), KIND_IMPORT, TUESDAY)
    assert sum(costs.values()) == pytest.approx(5.30299)
    assert costs[at(TUESDAY, 0)] == pytest.approx(1.05633)


# --- Slugs --------------------------------------------------------------------------------

STATISTIC_OBJECT_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)$")


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Peak", "peak"),
        ("Off Peak", "off_peak"),
        ("Off-peak", "off_peak"),
        ("  Solar Soak!  ", "solar_soak"),
        ("4pm–9pm (weekday)", "4pm_9pm_weekday"),
        ("A__B", "a_b"),
        ("⚡", ""),
        ("", ""),
    ],
)
def test_period_slug(name, slug):
    assert period_slug(name) == slug


def test_period_slug_is_capped_without_a_trailing_underscore():
    slug = period_slug("a" * 39 + " bbbbbbbbbb")
    assert slug == "a" * 39
    assert len(period_slug("x" * 100)) == 40


def one_version(*names: str) -> TariffVersion:
    periods = tuple(
        Period(name, (Window(i * 4 % 24, (i * 4 + 4) % 24 or 24),), (Tier(None, 10.0),))
        for i, name in enumerate(names)
    )
    return TariffVersion(effective_from=D0, import_periods=periods)


def test_slugs_follow_the_names():
    assert period_slugs(Tariff((GLOHOME,))) == {
        "Peak": "peak",
        "Off Peak": "off_peak",
        "Solar Soak": "solar_soak",
    }


def test_clashing_and_empty_slugs_are_made_unique():
    tariff = Tariff((one_version("Off-peak", "Off peak", "⚡", "off_peak"),))
    slugs = period_slugs(tariff)
    assert slugs == {
        "Off-peak": "off_peak",
        "Off peak": "off_peak_2",
        "⚡": "period_3",
        "off_peak": "off_peak_3",
    }
    assert len(set(slugs.values())) == len(slugs)


def test_a_slug_never_makes_one_periods_kwh_id_another_periods_cost_id():
    names = ("Cost", "Peak", "Peak Cost", "peak_cost", "Cost Cost")
    slugs = period_slugs(Tariff((one_version(*names),)))
    kwh_ids = {f"e1_{slug}" for slug in slugs.values()}
    cost_ids = {f"e1_{slug}_cost" for slug in slugs.values()}
    assert len(kwh_ids) == len(names)
    assert not kwh_ids & cost_ids
    assert "e1_cost" not in kwh_ids  # the combined cost statistic's own id


@pytest.mark.parametrize(
    "name",
    ["Peak", "Off-peak", "4pm-9pm", "A  B", "Cost", "x_cost", "⚡", "a" * 80, "_lead", "trail_"],
)
def test_every_slug_makes_valid_statistic_ids(name):
    slug = period_slugs(Tariff((one_version(name),)))[name]
    for object_id in (f"6001234567_e1_{slug}", f"6001234567_e1_{slug}_cost"):
        assert STATISTIC_OBJECT_ID.match(object_id), object_id


def test_versions_with_different_period_sets_share_slugs_by_name():
    old = TariffVersion(
        effective_from=D0,
        import_periods=(
            Period("Peak", (Window(16, 21),), (Tier(None, 30.0),)),
            Period("Rest", (Window(21, 16),), (Tier(None, 15.0),)),
        ),
    )
    new = TariffVersion(
        effective_from=date(2026, 6, 1),
        import_periods=(
            Period("Peak", (Window(16, 21),), (Tier(None, 31.0),)),
            Period("Night", (Window(21, 11),), (Tier(None, 16.0),)),
            Period("Soak", (Window(11, 16),), (Tier(None, 13.0),)),
        ),
    )
    only_old = period_slugs(Tariff((old,)))
    both = period_slugs(Tariff((old, new)))
    # Adding a newer version never changes an existing slug, and the set is the union.
    assert {name: both[name] for name in only_old} == only_old
    assert list(both) == ["Peak", "Rest", "Night", "Soak"]
    assert both["Night"] == "night"
