"""Where the daily supply charge goes in the per-period cost breakdown (pure, no Home Assistant).

Three modes: ``none`` leaves it out (what per-period costs always did), ``spread`` divides it
over every hour of the local day, ``first_hour`` puts all of it on the day's first hour. The two
that carry it make the periods' costs add up to the combined cost for the day.

Needs a time zone database for the daylight saving cases (``tzdata`` is in requirements-dev.txt).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from custom_components.citipower_myenergy import tariff_form as form
from custom_components.citipower_myenergy.const import (
    CONF_SUPPLY_MODE,
    SUPPLY_FIRST_HOUR,
    SUPPLY_MODES,
    SUPPLY_NONE,
    SUPPLY_SPREAD,
)
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
    cost_for_day_by_period,
    daily_supply_dollars,
    flat_tariff,
    local_day_hours,
    supply_mode_of,
)

try:
    MELBOURNE = ZoneInfo("Australia/Melbourne")
except ZoneInfoNotFoundError:  # pragma: no cover - only on a machine with no tz database
    pytest.skip("no time zone database; pip install tzdata", allow_module_level=True)

D0 = date(2026, 1, 1)
SUPPLY = 0.89628  # 92.40 c x 0.97 (3% discount, GST already in the prices)
FALL_BACK_2026, SPRING_FORWARD_2026 = date(2026, 4, 5), date(2026, 10, 4)  # 25 h and 23 h


def glohome(zone: str | None = None) -> Tariff:
    """GloBird GLOHOME Flexible Rate, CitiPower: supply 92.40 c/day, 3% discount, inc. GST."""
    return Tariff(
        (
            TariffVersion(
                effective_from=D0,
                supply_c_per_day=92.40,
                discount_pct=3.0,
                timezone=zone,
                import_periods=(
                    Period("Peak", (Window(16, 21),), (Tier(None, 31.46),)),
                    Period("Off-peak", (Window(21, 11),), (Tier(None, 16.50),)),
                    Period("Shoulder", (Window(11, 16),), (Tier(None, 13.20),)),
                ),
            ),
        )
    )


def day_of(tariff: Tariff, day: date, kwh: float = 1.0) -> dict[datetime, float]:
    """Uneven usage for every real hour of a local day, so a mix-up cannot cancel out."""
    return {
        moment: kwh + (index % 5) * 0.37
        for index, moment in enumerate(local_day_hours(day, tariff.tzinfo))
    }


def total(costs: dict[datetime, object]) -> float:
    return sum(item.cost for item in costs.values())  # type: ignore[attr-defined]


DAYS = [
    (D0, None, 24),
    (FALL_BACK_2026, "Australia/Melbourne", 25),
    (SPRING_FORWARD_2026, "Australia/Melbourne", 23),
]


def test_the_modes_are_the_three_the_options_store() -> None:
    assert SUPPLY_MODES == (SUPPLY_NONE, SUPPLY_SPREAD, SUPPLY_FIRST_HOUR)
    assert (SUPPLY_NONE, SUPPLY_SPREAD, SUPPLY_FIRST_HOUR) == ("none", "spread", "first_hour")


def test_none_is_the_default_and_leaves_the_supply_charge_out() -> None:
    tariff = glohome()
    hours = day_of(tariff, D0)
    default = cost_for_day_by_period(hours, tariff, KIND_IMPORT, D0)
    assert default == cost_for_day_by_period(hours, tariff, KIND_IMPORT, D0, SUPPLY_NONE)
    combined = cost_for_day(hours, tariff, KIND_IMPORT, D0)
    assert total(default) + SUPPLY == pytest.approx(sum(combined.values()), abs=1e-9)
    assert daily_supply_dollars(tariff.versions[0]) == pytest.approx(SUPPLY)


@pytest.mark.parametrize(("day", "zone", "hours_in_day"), DAYS)
@pytest.mark.parametrize("mode", [SUPPLY_SPREAD, SUPPLY_FIRST_HOUR])
def test_the_periods_add_up_to_the_combined_cost_for_the_whole_day(
    mode: str, day: date, zone: str | None, hours_in_day: int
) -> None:
    tariff = glohome(zone)
    hours = day_of(tariff, day)
    assert len(hours) == hours_in_day
    by_period = cost_for_day_by_period(hours, tariff, KIND_IMPORT, day, mode)
    combined = cost_for_day(hours, tariff, KIND_IMPORT, day)
    assert total(by_period) == pytest.approx(sum(combined.values()), abs=1e-9)


@pytest.mark.parametrize(("day", "zone", "hours_in_day"), DAYS)
def test_spread_adds_an_equal_share_to_every_hour(
    day: date, zone: str | None, hours_in_day: int
) -> None:
    tariff = glohome(zone)
    hours = day_of(tariff, day)
    plain = cost_for_day_by_period(hours, tariff, KIND_IMPORT, day)
    spread = cost_for_day_by_period(hours, tariff, KIND_IMPORT, day, SUPPLY_SPREAD)
    assert spread.keys() == plain.keys()
    for moment, item in spread.items():
        # The divisor is the hours the day really has: 24, 25 on fall-back, 23 on spring-forward.
        assert item.cost - plain[moment].cost == pytest.approx(SUPPLY / hours_in_day, abs=1e-12)
        assert (item.period, item.kwh) == (plain[moment].period, plain[moment].kwh)


@pytest.mark.parametrize(("day", "zone", "hours_in_day"), DAYS)
def test_first_hour_puts_everything_on_the_first_real_hour_of_the_day(
    day: date, zone: str | None, hours_in_day: int
) -> None:
    tariff = glohome(zone)
    hours = day_of(tariff, day)
    plain = cost_for_day_by_period(hours, tariff, KIND_IMPORT, day)
    first = cost_for_day_by_period(hours, tariff, KIND_IMPORT, day, SUPPLY_FIRST_HOUR)
    midnight = local_day_hours(day, tariff.tzinfo)[0]
    assert midnight.astimezone(tariff.tzinfo).hour == 0
    for moment, item in first.items():
        extra = item.cost - plain[moment].cost
        assert extra == pytest.approx(SUPPLY if moment == midnight else 0.0, abs=1e-12)
    # It is the very hour the combined cost carries it on, hour for hour.
    combined = cost_for_day(hours, tariff, KIND_IMPORT, day)
    for moment, item in first.items():
        assert item.cost == pytest.approx(combined[moment], abs=1e-12)


def test_the_supply_goes_to_whichever_period_covers_the_hour() -> None:
    tariff = glohome()
    hours = day_of(tariff, D0)
    midnight = local_day_hours(D0, tariff.tzinfo)[0]
    first = cost_for_day_by_period(hours, tariff, KIND_IMPORT, D0, SUPPLY_FIRST_HOUR)
    assert first[midnight].period == "Off-peak"  # 21-11 crosses midnight
    spread = cost_for_day_by_period(hours, tariff, KIND_IMPORT, D0, SUPPLY_SPREAD)
    by_name: dict[str, float] = {}
    for item in spread.values():
        by_name[item.period] = by_name.get(item.period, 0.0) + item.cost
    plain: dict[str, float] = {}
    for item in cost_for_day_by_period(hours, tariff, KIND_IMPORT, D0).values():
        plain[item.period] = plain.get(item.period, 0.0) + item.cost
    # Off-peak has 14 of the 24 hours, peak and shoulder 5 each.
    for name, count in (("Off-peak", 14), ("Peak", 5), ("Shoulder", 5)):
        assert by_name[name] - plain[name] == pytest.approx(SUPPLY * count / 24, abs=1e-12)


def test_a_flat_rate_never_moves_the_supply_charge_into_its_one_period() -> None:
    tariff = flat_tariff(30.0, effective_from=D0, supply_c_per_day=100.0)
    hours = day_of(tariff, D0)
    plain = cost_for_day_by_period(hours, tariff, KIND_IMPORT, D0)
    for mode in SUPPLY_MODES:
        assert cost_for_day_by_period(hours, tariff, KIND_IMPORT, D0, mode) == plain


def test_export_has_no_supply_charge_in_any_mode() -> None:
    version = TariffVersion(
        effective_from=D0,
        supply_c_per_day=92.40,
        import_periods=(
            Period("Peak", (Window(16, 21),), (Tier(None, 31.0),)),
            Period("Other", (Window(21, 16),), (Tier(None, 15.0),)),
        ),
        export_periods=(Period("Feed-in", (Window(0, 24),), (Tier(None, 5.0),)),),
    )
    tariff = Tariff((version,))
    hours = day_of(tariff, D0)
    plain = cost_for_day_by_period(hours, tariff, KIND_EXPORT, D0)
    for mode in SUPPLY_MODES:
        assert cost_for_day_by_period(hours, tariff, KIND_EXPORT, D0, mode) == plain


def test_an_unknown_mode_is_refused() -> None:
    tariff = glohome()
    with pytest.raises(TariffError, match="supply charge mode"):
        cost_for_day_by_period(day_of(tariff, D0), tariff, KIND_IMPORT, D0, "everywhere")


def test_an_empty_day_is_empty_in_every_mode() -> None:
    for mode in SUPPLY_MODES:
        assert cost_for_day_by_period({}, glohome(), KIND_IMPORT, D0, mode) == {}


def test_the_discount_and_gst_shape_the_supply_that_is_spread() -> None:
    version = TariffVersion(
        effective_from=D0,
        supply_c_per_day=100.0,
        prices_include_gst=False,
        discount_pct=10.0,
        import_periods=(
            Period("Peak", (Window(16, 21),), (Tier(None, 30.0),)),
            Period("Other", (Window(21, 16),), (Tier(None, 20.0),)),
        ),
    )
    tariff = Tariff((version,))
    assert daily_supply_dollars(version) == pytest.approx(0.99)  # 1.00 x 1.1 GST x 0.9
    hours = {moment: 0.0 for moment in local_day_hours(D0, tariff.tzinfo)}  # no usage at all
    spread = cost_for_day_by_period(hours, tariff, KIND_IMPORT, D0, SUPPLY_SPREAD)
    assert all(item.cost == pytest.approx(0.99 / 24, abs=1e-12) for item in spread.values())
    assert total(spread) == pytest.approx(0.99, abs=1e-12)


# --- Reading the stored option ----------------------------------------------------------------


def test_a_missing_or_unknown_stored_mode_means_none() -> None:
    assert supply_mode_of({}) == SUPPLY_NONE
    assert supply_mode_of({CONF_SUPPLY_MODE: "nonsense"}) == SUPPLY_NONE
    assert supply_mode_of({CONF_SUPPLY_MODE: None}) == SUPPLY_NONE
    for mode in SUPPLY_MODES:
        assert supply_mode_of({CONF_SUPPLY_MODE: mode}) == mode


# --- The text the options step shows ----------------------------------------------------------


def _ids(slug: str) -> tuple[str, str]:
    return f"stat:{slug}", f"stat:{slug}_cost"


def test_the_step_text_says_where_the_supply_charge_is_for_each_mode() -> None:
    tariff = glohome()
    none = form.period_statistics_text(tariff, _ids, "AUD")
    assert none == form.period_statistics_text(tariff, _ids, "AUD", SUPPLY_NONE)
    assert "not included in these period costs" in none
    spread = form.period_statistics_text(tariff, _ids, "AUD", SUPPLY_SPREAD)
    assert "included in these period costs, spread equally over every hour" in spread
    assert "not included" not in spread
    first = form.period_statistics_text(tariff, _ids, "AUD", SUPPLY_FIRST_HOUR)
    assert "included in these period costs, all on the first hour" in first
    for text in (none, spread, first):
        assert "AUD 0.89628" in text


def test_a_flat_rate_always_reads_as_left_out() -> None:
    tariff = flat_tariff(30.0, effective_from=D0, supply_c_per_day=100.0)
    text = form.period_statistics_text(tariff, _ids, "AUD", SUPPLY_SPREAD)
    assert "not included in these period costs" in text


def test_only_a_tariff_with_several_periods_is_time_of_use() -> None:
    assert form.is_time_of_use(glohome()) is True
    assert form.is_time_of_use(flat_tariff(30.0, effective_from=D0)) is False
