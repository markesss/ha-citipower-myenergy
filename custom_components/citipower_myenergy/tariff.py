"""Price hourly kWh from a user-entered tariff. PURE: no Home Assistant, no aiohttp.

The portal carries no prices (the retailer sets them, not the network), so the user
describes their plan and this module turns a day of hourly readings into a day of
hourly costs in dollars.

Model
-----
A ``Tariff`` is a list of ``TariffVersion`` objects, each effective from a date. A version
holds a supply charge, discount and GST setting, and two independent lists of ``Period``
(import and export; retailers use different windows for each). A ``Period`` is a rate
that applies in one or more ``Window`` objects (so an export rate for "9pm-10am and
2pm-4pm" is a single period) and is priced by ``Tier`` objects: a flat rate is one open-ended
tier, and a per-day allowance ("first 30 kWh/day free, then 11c") is two.

Decisions worth knowing
-----------------------
* **Local day.** Always midnight to midnight AEST (``const.MARKET_TZ``, UTC+10, no daylight
  saving), so a day is exactly 24 hours. That is NEM (market) time: the clock the NEM12
  meter data and the network tariffs use, and the clock window hours are read in. Retailer
  sheets that say "local time" are taken to mean it. ``cost_for_day`` is told which day it
  is pricing because the tariff version is chosen by that date.
* **Windows** use whole hours only (the data is hourly). ``end <= start`` crosses midnight
  and ``end == 24`` means midnight. A window's ``days`` are the days it *starts* on: a
  Friday 10pm-2am window covers Saturday 1am too.
* **Tiers** are cumulative per-day thresholds ("up to 30 kWh, then the remainder") that
  reset at local midnight and are tracked per period, in time order.
* **GST** (when prices are entered ex GST) is added to import charges only. Feed-in
  credits are paid as entered: retailers quote them "incl. GST if any" and residential
  feed-in normally carries none.
* **Discounts** reduce import supply and usage charges, never export credit, matching the
  retailers' fact sheets.
* The **supply charge** is added to the first hour of the local day, for import only.
  Callers price whole days; if the first hour is absent it lands on the earliest hour given.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from .const import MARKET_TZ
from .exceptions import TariffError

SCHEMA_VERSION = 1

KIND_IMPORT = "import"
KIND_EXPORT = "export"
Kind = Literal["import", "export"]

GST_FACTOR = 1.1

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# --- Model ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tier:
    """A price for energy up to a cumulative per-day threshold."""

    up_to_kwh_per_day: float | None  # None = everything beyond the previous tier
    c_per_kwh: float


@dataclass(frozen=True, slots=True)
class Window:
    """A span of whole hours on some days."""

    start: int  # 0-23
    end: int  # 0-24; end <= start crosses midnight
    days: frozenset[int] | None = None  # 0 = Monday; None = every day


@dataclass(frozen=True, slots=True)
class Period:
    """A named price, applying in its windows (or everywhere else if ``catch_all``)."""

    name: str
    windows: tuple[Window, ...]
    tiers: tuple[Tier, ...]
    catch_all: bool = False


@dataclass(frozen=True, slots=True)
class TariffVersion:
    """The prices in force from ``effective_from``."""

    effective_from: date
    import_periods: tuple[Period, ...]
    export_periods: tuple[Period, ...] = ()
    supply_c_per_day: float = 0.0
    discount_pct: float = 0.0
    prices_include_gst: bool = True

    def periods(self, kind: str) -> tuple[Period, ...]:
        """Return the import or export periods."""
        if kind == KIND_IMPORT:
            return self.import_periods
        if kind == KIND_EXPORT:
            return self.export_periods
        raise TariffError(f"Unknown kind {kind!r}; expected 'import' or 'export'")

    def period_for(self, local_dt: datetime, kind: str) -> Period | None:
        """Return the period priced at a wall-clock moment (only weekday and hour are used)."""
        periods = self.periods(kind)
        if not periods:
            return None
        owners, _ = _slot_owners(periods)
        slot = local_dt.weekday() * 24 + local_dt.hour
        return periods[owners[slot]] if owners[slot] >= 0 else None

    def validate(self) -> None:
        """Raise TariffError if this version is not a complete, consistent price list."""
        label = f"Tariff from {self.effective_from.isoformat()}"
        _check_number(self.supply_c_per_day, f"{label}: supply charge", minimum=0)
        _check_number(self.discount_pct, f"{label}: discount", minimum=0, maximum=100)
        if not self.import_periods:
            raise TariffError(f"{label}: add at least one import period")
        for kind in (KIND_IMPORT, KIND_EXPORT):
            periods = self.periods(kind)
            if periods:
                _validate_periods(periods, f"{label} ({kind})")


@dataclass(frozen=True, slots=True)
class Tariff:
    """Every version of a user's tariff, oldest first."""

    versions: tuple[TariffVersion, ...]

    def version_for(self, day: date) -> TariffVersion:
        """Return the version in force on a date; the first covers all earlier dates."""
        if not self.versions:
            raise TariffError("The tariff has no versions")
        chosen = self.versions[0]
        for version in self.versions:
            if version.effective_from <= day:
                chosen = version
            else:
                break
        return chosen

    def validate(self) -> None:
        """Raise TariffError if any version is invalid or the versions are out of order."""
        if not self.versions:
            raise TariffError("The tariff has no versions")
        dates = [version.effective_from for version in self.versions]
        if dates != sorted(set(dates)):
            raise TariffError("Tariff versions must have distinct effective dates, oldest first")
        for version in self.versions:
            version.validate()

    # --- Serialisation -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON/YAML-serialisable form for storage in config entry options."""
        return {"schema": SCHEMA_VERSION, "versions": [_version_to_dict(v) for v in self.versions]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Tariff:
        """Build and validate a tariff from user-supplied (or stored) data."""
        if not isinstance(data, Mapping):
            raise TariffError("The tariff must be a mapping")
        if data.get("schema") != SCHEMA_VERSION:
            raise TariffError(f"Unsupported tariff schema {data.get('schema')!r}")
        raw_versions = data.get("versions")
        if not isinstance(raw_versions, list | tuple) or not raw_versions:
            raise TariffError("The tariff needs a non-empty 'versions' list")
        versions = sorted(
            (_version_from_dict(raw) for raw in raw_versions), key=lambda v: v.effective_from
        )
        tariff = cls(tuple(versions))
        tariff.validate()
        return tariff


# --- Time helpers -------------------------------------------------------------------


def local_day(moment: datetime) -> date:
    """Return which AEST (market-time) day an aware moment falls in."""
    return moment.astimezone(MARKET_TZ).date()


def local_day_hours(day: date) -> list[datetime]:
    """Return the UTC start of each of the 24 hours of an AEST day."""
    start = datetime.combine(day, time(0), tzinfo=MARKET_TZ).astimezone(UTC)
    return [start + timedelta(hours=i) for i in range(24)]


# --- Slots and validation ---------------------------------------------------------


def _slot_label(slot: int) -> str:
    return f"{_DAY_NAMES[slot // 24]} {slot % 24:02d}:00"


def _window_slots(window: Window) -> set[int]:
    """Return the (weekday * 24 + hour) slots a window covers."""
    days = range(7) if window.days is None else window.days
    slots: set[int] = set()
    for day in days:
        if window.end > window.start:
            slots.update(day * 24 + hour for hour in range(window.start, window.end))
        else:
            slots.update(day * 24 + hour for hour in range(window.start, 24))
            # The after-midnight part belongs to the next day's slots.
            slots.update(((day + 1) % 7) * 24 + hour for hour in range(window.end))
    return slots


def _slot_owners(periods: tuple[Period, ...]) -> tuple[list[int], list[str]]:
    """Map each of the week's 168 hours to a period index; list any problems found."""
    owners = [-1] * 168
    problems: list[str] = []
    catch_all = -1
    for index, period in enumerate(periods):
        if period.catch_all:
            catch_all = index
            continue
        for slot in sorted(s for window in period.windows for s in _window_slots(window)):
            if owners[slot] not in (-1, index):
                other = periods[owners[slot]].name
                problems.append(f"'{other}' and '{period.name}' overlap on {_slot_label(slot)}")
                break
            owners[slot] = index
    gaps = [slot for slot, owner in enumerate(owners) if owner < 0]
    if catch_all >= 0:
        for slot in gaps:
            owners[slot] = catch_all
    elif gaps:
        extra = f" (and {len(gaps) - 1} more hours)" if len(gaps) > 1 else ""
        problems.append(f"no period covers {_slot_label(gaps[0])}{extra}")
    return owners, problems


def _check_number(
    value: Any, what: str, *, minimum: float | None = None, maximum: float | None = None
) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise TariffError(f"{what} must be a number")
    if minimum is not None and value < minimum:
        raise TariffError(f"{what} must be at least {minimum:g}")
    if maximum is not None and value > maximum:
        raise TariffError(f"{what} must be at most {maximum:g}")


def _validate_window(window: Window, label: str) -> None:
    for what, value, low, high in (
        ("start", window.start, 0, 23),
        ("end", window.end, 0, 24),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TariffError(f"{label}: window {what} must be a whole hour")
        if not low <= value <= high:
            raise TariffError(f"{label}: window {what} must be between {low} and {high}")
    if window.start == window.end:
        raise TariffError(f"{label}: window start and end are the same (use 0 to 24 for all day)")
    if window.days is not None:
        if not window.days:
            raise TariffError(f"{label}: a window's days cannot be empty (omit them for every day)")
        if any(
            isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in window.days
        ):
            raise TariffError(f"{label}: days must be 0 (Monday) to 6 (Sunday)")


def _validate_tiers(tiers: tuple[Tier, ...], label: str) -> None:
    if not tiers:
        raise TariffError(f"{label}: add at least one rate")
    previous = 0.0
    for index, tier in enumerate(tiers):
        _check_number(tier.c_per_kwh, f"{label}: rate")
        last = index == len(tiers) - 1
        if tier.up_to_kwh_per_day is None:
            if not last:
                raise TariffError(f"{label}: only the last rate can be open-ended")
            continue
        _check_number(tier.up_to_kwh_per_day, f"{label}: tier threshold", minimum=0)
        if tier.up_to_kwh_per_day <= previous:
            raise TariffError(f"{label}: tier thresholds must increase")
        if last:
            raise TariffError(f"{label}: the last rate must be open-ended (no threshold)")
        previous = tier.up_to_kwh_per_day


def _validate_periods(periods: tuple[Period, ...], label: str) -> None:
    names = [period.name for period in periods]
    if len(set(names)) != len(names):
        raise TariffError(f"{label}: period names must be unique")
    if sum(period.catch_all for period in periods) > 1:
        raise TariffError(f"{label}: only one period can cover 'all other times'")
    for period in periods:
        where = f"{label}, '{period.name}'"
        if not isinstance(period.name, str) or not period.name.strip():
            raise TariffError(f"{label}: every period needs a name")
        if period.catch_all and period.windows:
            raise TariffError(f"{where}: an 'all other times' period has no windows")
        if not period.catch_all and not period.windows:
            raise TariffError(f"{where}: add at least one window")
        for window in period.windows:
            _validate_window(window, where)
        _validate_tiers(period.tiers, where)
    _, problems = _slot_owners(periods)
    if problems:
        raise TariffError(f"{label}: {problems[0]}")


# --- Pricing -------------------------------------------------------------------------


def _tier_cents(tiers: tuple[Tier, ...], used: float, kwh: float) -> float:
    """Price ``kwh`` that follows ``used`` kWh already consumed in this period today."""
    cents = 0.0
    position = used
    remaining = kwh
    for tier in tiers:
        if remaining <= 0:
            break
        upper = math.inf if tier.up_to_kwh_per_day is None else tier.up_to_kwh_per_day
        if position >= upper:
            continue
        taken = min(remaining, upper - position)
        cents += taken * tier.c_per_kwh
        position += taken
        remaining -= taken
    return cents


def cost_for_day(
    hours: Mapping[datetime, float], tariff: Tariff, kind: str, day: date
) -> dict[datetime, float]:
    """Return each hour's cost in dollars for one local day of hourly kWh.

    ``hours`` is keyed by timezone-aware hour starts, all inside local day ``day``.
    The result is keyed by the same datetimes in UTC. Export costs are credits
    (positive money earned); callers decide how to present them.
    """
    version = tariff.version_for(day)
    periods = version.periods(kind)
    ordered = _normalise_hours(hours)
    if not ordered:
        return {}
    if not periods:
        return {moment: 0.0 for moment, _ in ordered}

    owners, problems = _slot_owners(periods)
    if problems:
        raise TariffError(f"Cannot price {kind} usage: {problems[0]}")

    is_import = kind == KIND_IMPORT
    # GST and the discount are charges-only; see the module docstring.
    factor = 1.0
    if is_import:
        if not version.prices_include_gst:
            factor *= GST_FACTOR
        factor *= 1 - version.discount_pct / 100

    used: dict[int, float] = {}
    costs: dict[datetime, float] = {}
    for position, (moment, kwh) in enumerate(ordered):
        if local_day(moment) != day:
            raise TariffError(f"{moment.isoformat()} is not in local day {day.isoformat()}")
        local = moment.astimezone(MARKET_TZ)
        index = owners[local.weekday() * 24 + local.hour]
        kwh = max(kwh, 0.0)
        cents = _tier_cents(periods[index].tiers, used.get(index, 0.0), kwh)
        used[index] = used.get(index, 0.0) + kwh
        if is_import and position == 0:
            cents += version.supply_c_per_day
        costs[moment] = cents * factor / 100
    return costs


def _normalise_hours(hours: Mapping[datetime, float]) -> list[tuple[datetime, float]]:
    """Return (UTC hour start, kWh) in time order, rejecting anything off the hour."""
    merged: dict[datetime, float] = {}
    for moment, kwh in hours.items():
        if moment.tzinfo is None:
            raise TariffError("Hour starts must be timezone-aware")
        utc = moment.astimezone(UTC)
        if utc.minute or utc.second or utc.microsecond:
            raise TariffError(f"{moment.isoformat()} is not on the hour")
        merged[utc] = merged.get(utc, 0.0) + float(kwh)
    return sorted(merged.items())


# --- Dict conversion -----------------------------------------------------------------


def _version_to_dict(version: TariffVersion) -> dict[str, Any]:
    return {
        "effective_from": version.effective_from.isoformat(),
        "prices_include_gst": version.prices_include_gst,
        "supply_c_per_day": version.supply_c_per_day,
        "discount_pct": version.discount_pct,
        "import_periods": [_period_to_dict(p) for p in version.import_periods],
        "export_periods": [_period_to_dict(p) for p in version.export_periods],
    }


def _period_to_dict(period: Period) -> dict[str, Any]:
    return {
        "name": period.name,
        "catch_all": period.catch_all,
        "windows": [
            {
                "start": window.start,
                "end": window.end,
                "days": None if window.days is None else sorted(window.days),
            }
            for window in period.windows
        ],
        "tiers": [
            {"up_to_kwh_per_day": tier.up_to_kwh_per_day, "c_per_kwh": tier.c_per_kwh}
            for tier in period.tiers
        ],
    }


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TariffError(f"{what} must be a mapping")
    return value


def _sequence(value: Any, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list | tuple):
        raise TariffError(f"{what} must be a list")
    return list(value)


def _number(value: Any, what: str) -> float:
    _check_number(value, what)
    return float(value)


def _whole(value: Any, what: str) -> int:
    """Accept ints, and floats that are whole (YAML and JSON editors produce both)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TariffError(f"{what} must be a whole number of hours")
    if isinstance(value, float) and not value.is_integer():
        raise TariffError(f"{what} must be a whole hour (the data is hourly), not {value:g}")
    return int(value)


def _version_from_dict(raw: Any) -> TariffVersion:
    data = _mapping(raw, "A tariff version")
    effective = data.get("effective_from")
    if isinstance(effective, datetime):
        effective = effective.date()
    elif isinstance(effective, str):
        try:
            effective = date.fromisoformat(effective)
        except ValueError as err:
            raise TariffError(f"effective_from {effective!r} is not a YYYY-MM-DD date") from err
    if not isinstance(effective, date):
        raise TariffError("Each tariff version needs an effective_from date")
    # Times are always AEST (NEM time). Refuse the key rather than quietly ignore it, so a
    # tariff written for a wall-clock mode cannot be priced on the wrong clock.
    if "timezone_mode" in data:
        raise TariffError(
            "timezone_mode is not supported: tariff times are always AEST (NEM time, UTC+10)"
        )
    gst = data.get("prices_include_gst", True)
    if not isinstance(gst, bool):
        raise TariffError("prices_include_gst must be true or false")
    return TariffVersion(
        effective_from=effective,
        prices_include_gst=gst,
        supply_c_per_day=_number(data.get("supply_c_per_day", 0.0), "supply_c_per_day"),
        discount_pct=_number(data.get("discount_pct", 0.0), "discount_pct"),
        import_periods=_periods_from(data.get("import_periods"), "import_periods"),
        export_periods=_periods_from(data.get("export_periods"), "export_periods"),
    )


def _periods_from(raw: Any, what: str) -> tuple[Period, ...]:
    return tuple(_period_from_dict(item, what) for item in _sequence(raw, what))


def _period_from_dict(raw: Any, what: str) -> Period:
    data = _mapping(raw, f"A period in {what}")
    name = data.get("name")
    if not isinstance(name, str):
        raise TariffError(f"Every period in {what} needs a name")
    catch_all = data.get("catch_all", False)
    if not isinstance(catch_all, bool):
        raise TariffError(f"{name}: catch_all must be true or false")
    windows = tuple(_window_from_dict(w, name) for w in _sequence(data.get("windows"), "windows"))
    tiers = tuple(_tier_from_dict(t, name) for t in _sequence(data.get("tiers"), "tiers"))
    return Period(name=name, windows=windows, tiers=tiers, catch_all=catch_all)


def _window_from_dict(raw: Any, name: str) -> Window:
    data = _mapping(raw, f"{name}: a window")
    days = data.get("days")
    day_set: frozenset[int] | None = None
    if days is not None:
        day_set = frozenset(_whole(d, f"{name}: day") for d in _sequence(days, "days"))
    return Window(
        start=_whole(data.get("start"), f"{name}: window start"),
        end=_whole(data.get("end"), f"{name}: window end"),
        days=day_set,
    )


def _tier_from_dict(raw: Any, name: str) -> Tier:
    data = _mapping(raw, f"{name}: a rate")
    limit = data.get("up_to_kwh_per_day")
    return Tier(
        up_to_kwh_per_day=None if limit is None else _number(limit, f"{name}: tier threshold"),
        c_per_kwh=_number(data.get("c_per_kwh"), f"{name}: rate"),
    )


def flat_tariff(
    c_per_kwh: float,
    *,
    effective_from: date,
    supply_c_per_day: float = 0.0,
    export_c_per_kwh: float | None = None,
    discount_pct: float = 0.0,
    prices_include_gst: bool = True,
) -> Tariff:
    """Build the simplest tariff: one all-day import rate and optionally one export rate."""

    def all_day(name: str, rate: float) -> tuple[Period, ...]:
        return (Period(name, (Window(0, 24),), (Tier(None, rate),)),)

    return Tariff(
        (
            TariffVersion(
                effective_from=effective_from,
                import_periods=all_day("All day", c_per_kwh),
                export_periods=()
                if export_c_per_kwh is None
                else all_day("All day", export_c_per_kwh),
                supply_c_per_day=supply_c_per_day,
                discount_pct=discount_pct,
                prices_include_gst=prices_include_gst,
            ),
        )
    )


__all__ = [
    "KIND_EXPORT",
    "KIND_IMPORT",
    "Kind",
    "Period",
    "Tariff",
    "TariffError",
    "TariffVersion",
    "Tier",
    "Window",
    "cost_for_day",
    "flat_tariff",
    "local_day",
    "local_day_hours",
]
