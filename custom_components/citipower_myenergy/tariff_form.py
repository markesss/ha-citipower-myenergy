"""Pure helpers for the tariff options flow. PURE: no Home Assistant, no aiohttp.

Home Assistant forms are flat, so the guided editor works on plain dicts shaped like
``Tariff.to_dict()["versions"]`` entries and this module turns form input into those dicts,
and them into text the user can check: the period list and the resolved hour-by-hour map.

The guided editor only handles the simple case: a period with one flat rate in windows that
share the same days. Anything richer (tiers, per-day allowances, "all other times", windows
on different days) is left untouched and edited as YAML in the advanced step.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from .exceptions import TariffError
from .tariff import (
    KIND_EXPORT,
    KIND_IMPORT,
    SCHEMA_VERSION,
    Period,
    Tariff,
    TariffVersion,
    Tier,
    Window,
)

# The same sentence is repeated, as plain text, in every step description in strings.json
# (translations cannot take placeholders for constant text); a test keeps them identical.
TIMEZONE_NOTE = (
    "All times are AEST (UTC+10) with no daylight saving. This is NEM (National Electricity "
    "Market) time, the clock your meter data and network tariffs use. It is a pricing clock, "
    "not Home Assistant's own timezone."
)

DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_DAY_ABBR = tuple(name[:3] for name in DAY_NAMES)

# 5 Jan 2026 is a Monday. Only the weekday and hour of the moment are used to look up a period.
_A_MONDAY = date(2026, 1, 5)

ALL_DAY_NAME = "All day"


class FormError(ValueError):
    """Form input the user needs to correct; the message says how."""


# --- Windows as text ------------------------------------------------------------------

_HOUR = re.compile(r"^(\d{1,2})(?::00)?\s*(am|pm)?$", re.IGNORECASE)
_RANGE_SPLIT = re.compile(r"\s*(?:-|–|—|\bto\b)\s*", re.IGNORECASE)


def _parse_hour(token: str) -> int:
    match = _HOUR.match(token.strip())
    if match is None:
        raise FormError(f"'{token.strip()}' is not a whole hour")
    hour = int(match.group(1))
    suffix = (match.group(2) or "").lower()
    if suffix:
        if not 1 <= hour <= 12:
            raise FormError(f"'{token.strip()}' is not a valid am/pm hour")
        return hour % 12 + (12 if suffix == "pm" else 0)
    if hour > 24:
        raise FormError(f"'{token.strip()}' is not between 0 and 24")
    return hour


def parse_windows(text: str) -> list[tuple[int, int]]:
    """Read "16-21, 9pm-10am" into (start, end) whole-hour pairs.

    Hours are 0-24 on a 24-hour clock, or 1-12 with am/pm. An end at or before the start
    crosses midnight, and ``0-24`` is all day.
    """
    chunks = [chunk for chunk in re.split(r"[,;]", text or "") if chunk.strip()]
    if not chunks:
        raise FormError("enter at least one window, such as 16-21")
    windows: list[tuple[int, int]] = []
    for chunk in chunks:
        parts = _RANGE_SPLIT.split(chunk.strip())
        if len(parts) != 2:
            raise FormError(f"'{chunk.strip()}' should look like 16-21")
        start, end = _parse_hour(parts[0]), _parse_hour(parts[1])
        if start > 23:
            raise FormError(f"'{chunk.strip()}' starts at 24; use 0 for midnight")
        if start == end:
            raise FormError(f"'{chunk.strip()}' starts and ends at the same hour (use 0-24)")
        windows.append((start, end))
    return windows


def format_windows(windows: Iterable[Mapping[str, Any]]) -> str:
    """Write stored windows back as the text ``parse_windows`` reads."""
    return ", ".join(f"{window['start']}-{window['end']}" for window in windows)


# --- Periods ------------------------------------------------------------------------------


def _days_key(window: Mapping[str, Any]) -> tuple[int, ...] | None:
    days = window.get("days")
    return None if days is None else tuple(sorted(days))


def simple_rate(period: Mapping[str, Any]) -> float | None:
    """Return the rate of a period with one open-ended tier, else None."""
    tiers = period.get("tiers") or []
    if len(tiers) == 1 and tiers[0].get("up_to_kwh_per_day") is None:
        return float(tiers[0]["c_per_kwh"])
    return None


def period_is_guided(period: Mapping[str, Any]) -> bool:
    """Say whether the guided editor can edit a stored period without losing anything."""
    if period.get("catch_all") or simple_rate(period) is None:
        return False
    windows = period.get("windows") or []
    if not windows:
        return False
    first = _days_key(windows[0])
    return all(_days_key(window) == first for window in windows)


def period_to_form(period: Mapping[str, Any]) -> dict[str, Any]:
    """Return the form values for a guided period."""
    windows = period["windows"]
    days = windows[0].get("days")
    return {
        "name": period["name"],
        "rate": simple_rate(period),
        "windows": format_windows(windows),
        "days": [str(day) for day in days] if days else [],
    }


def form_to_period(
    name: str, rate: float, windows_text: str, days: Iterable[str] | None
) -> dict[str, Any]:
    """Build a period dict from form input; raises FormError on bad windows."""
    chosen = sorted({int(day) for day in days or []})
    day_list = None if len(chosen) in (0, 7) else chosen
    return {
        "name": name.strip(),
        "catch_all": False,
        "windows": [
            {"start": start, "end": end, "days": day_list}
            for start, end in parse_windows(windows_text)
        ],
        "tiers": [{"up_to_kwh_per_day": None, "c_per_kwh": float(rate)}],
    }


def flat_period(rate: float) -> dict[str, Any]:
    """Return the single all-day period that makes up a flat-rate tariff."""
    return form_to_period(ALL_DAY_NAME, rate, "0-24", None)


def new_version_dict(effective_from: date) -> dict[str, Any]:
    """Return an empty tariff version, ready to be filled in."""
    return {
        "effective_from": effective_from.isoformat(),
        "prices_include_gst": True,
        "supply_c_per_day": 0.0,
        "discount_pct": 0.0,
        "import_periods": [],
        "export_periods": [],
    }


def periods_key(kind: str) -> str:
    """Return the version-dict key holding a kind's periods."""
    if kind == KIND_IMPORT:
        return "import_periods"
    if kind == KIND_EXPORT:
        return "export_periods"
    raise TariffError(f"Unknown kind {kind!r}")


# --- Describing things -------------------------------------------------------------------


def _days_label(days: Sequence[int] | None) -> str:
    if days is None or len(days) == 7:
        return "every day"
    ordered = sorted(days)
    if ordered == [0, 1, 2, 3, 4]:
        return "Mon–Fri"
    if ordered == [5, 6]:
        return "Sat–Sun"
    return ", ".join(_DAY_ABBR[day] for day in ordered)


def _rate_text(tiers: Iterable[Mapping[str, Any] | Tier]) -> str:
    parts: list[str] = []
    for tier in tiers:
        if isinstance(tier, Tier):
            limit, rate = tier.up_to_kwh_per_day, tier.c_per_kwh
        else:
            limit, rate = tier.get("up_to_kwh_per_day"), tier["c_per_kwh"]
        if limit is None:
            parts.append(f"{rate:.2f} c/kWh" if not parts else f"then {rate:.2f} c/kWh")
        else:
            parts.append(f"first {limit:g} kWh/day {rate:.2f} c/kWh")
    return ", ".join(parts)


def describe_periods(periods: Sequence[Mapping[str, Any]]) -> str:
    """Return a markdown list of periods for a step description."""
    if not periods:
        return "_No periods yet._"
    lines = []
    for period in periods:
        if period.get("catch_all"):
            when = "all other times"
        else:
            when = "; ".join(
                f"{window['start']}-{window['end']} {_days_label(window.get('days'))}"
                for window in period.get("windows") or []
            )
        line = f"- **{period['name']}**: {when}, {_rate_text(period.get('tiers') or [])}"
        if not period_is_guided(period):
            line += " _(tiers or special rules: edit it in the advanced editor)_"
        lines.append(line)
    return "\n".join(lines)


def describe_versions(versions: Sequence[Mapping[str, Any]]) -> str:
    """Return a markdown list of tariff versions for a step description."""
    lines = []
    for version in versions:
        imports = len(version.get("import_periods") or [])
        exports = len(version.get("export_periods") or [])
        line = (
            f"- From **{version['effective_from']}**: {imports} import "
            f"period{'s' if imports != 1 else ''}"
        )
        if exports:
            line += f", {exports} export period{'s' if exports != 1 else ''}"
        line += f", supply {float(version.get('supply_c_per_day') or 0):.2f} c/day"
        lines.append(line)
    return "\n".join(lines)


def version_from_draft(draft: Mapping[str, Any]) -> TariffVersion:
    """Build a TariffVersion from a draft dict without validating it.

    A half-finished draft can have gaps; the hour map shows them rather than refusing.
    """

    def periods(key: str) -> tuple[Period, ...]:
        return tuple(
            Period(
                name=period["name"],
                windows=tuple(
                    Window(
                        window["start"],
                        window["end"],
                        None if window.get("days") is None else frozenset(window["days"]),
                    )
                    for window in period.get("windows") or []
                ),
                tiers=tuple(
                    Tier(tier.get("up_to_kwh_per_day"), tier["c_per_kwh"])
                    for tier in period.get("tiers") or []
                ),
                catch_all=bool(period.get("catch_all")),
            )
            for period in draft.get(key) or []
        )

    return TariffVersion(
        effective_from=date.fromisoformat(draft["effective_from"]),
        import_periods=periods("import_periods"),
        export_periods=periods("export_periods"),
        supply_c_per_day=float(draft.get("supply_c_per_day") or 0),
        discount_pct=float(draft.get("discount_pct") or 0),
        prices_include_gst=bool(draft.get("prices_include_gst", True)),
    )


def _runs(pattern: Sequence[str | None]) -> list[tuple[int, int, str | None]]:
    runs: list[tuple[int, int, str | None]] = []
    start = 0
    for hour in range(1, 25):
        if hour == 24 or pattern[hour] != pattern[start]:
            runs.append((start, hour, pattern[start]))
            start = hour
    return runs


def hour_map_text(version: TariffVersion, kind: str) -> str:
    """Return the resolved hour-by-hour map for one kind as markdown, grouped by day pattern."""
    periods = version.periods(kind)
    if not periods:
        return ""
    rates = {period.name: _rate_text(period.tiers) for period in periods}
    groups: dict[tuple[str | None, ...], list[int]] = {}
    for day in range(7):
        pattern = []
        for hour in range(24):
            owner = version.period_for(datetime(2026, 1, _A_MONDAY.day + day, hour), kind)
            pattern.append(owner.name if owner else None)
        groups.setdefault(tuple(pattern), []).append(day)

    label = "Import" if kind == KIND_IMPORT else "Export"
    lines = [f"**{label} hours (AEST, NEM time, UTC+10)**"]
    for pattern, days in groups.items():
        label_days = _days_label(days)
        lines.append(f"{label_days[:1].upper()}{label_days[1:]}:")
        for start, end, name in _runs(pattern):
            who = f"{name} ({rates[name]})" if name else "**not covered**"
            lines.append(f"- {start:02d}:00–{end:02d}:00 {who}")
    return "\n".join(lines)


def review_text(draft: Mapping[str, Any]) -> str:
    """Return the maps for every kind the draft prices, ready for the review step."""
    version = version_from_draft(draft)
    blocks = [hour_map_text(version, kind) for kind in (KIND_IMPORT, KIND_EXPORT)]
    return "\n\n".join(block for block in blocks if block) or "_No periods yet._"


# --- Validation ------------------------------------------------------------------------------


def build_tariff(versions: Sequence[Mapping[str, Any]]) -> Tariff:
    """Build and validate a tariff from version dicts; raises TariffError."""
    return Tariff.from_dict({"schema": SCHEMA_VERSION, "versions": list(versions)})


def draft_problem(versions: Sequence[Mapping[str, Any]]) -> str | None:
    """Return what is wrong with the tariff as it stands, or None if it can be saved."""
    try:
        build_tariff(versions)
    except TariffError as err:
        return str(err)
    return None
