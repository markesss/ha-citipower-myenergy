"""Pure tests for the tariff options flow helpers (no Home Assistant needed)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from custom_components.citipower_myenergy import tariff_form as form
from custom_components.citipower_myenergy.tariff import Tariff

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "citipower_myenergy"

# Steps whose descriptions show or take times, and so must carry the AEST / NEM-time note.
TIME_STEPS = (
    "tariff_basics",
    "tariff_flat_rate",
    "tariff_import_periods",
    "tariff_export_periods",
    "period_add",
    "period_edit",
    "period_edit_form",
    "period_delete",
    "tariff_review",
    "tariff_advanced",
)


def glohome() -> dict:
    """GloBird GLOHOME Flexible Rate, CitiPower (rates inc. GST), import only."""
    version = form.new_version_dict(date(2026, 10, 28))
    version.update(supply_c_per_day=92.40, discount_pct=3.0)
    version["import_periods"] = [
        form.form_to_period("Peak", 31.46, "16-21", None),
        form.form_to_period("Off-peak", 16.50, "21-11", None),
        form.form_to_period("Shoulder", 13.20, "11-16", None),
    ]
    return version


def four4free_free_window() -> dict:
    """A period with a per-day allowance: first 30 kWh/day free, then 11c."""
    return {
        "name": "Free window",
        "catch_all": False,
        "windows": [{"start": 11, "end": 15, "days": None}],
        "tiers": [
            {"up_to_kwh_per_day": 30.0, "c_per_kwh": 0.0},
            {"up_to_kwh_per_day": None, "c_per_kwh": 11.0},
        ],
    }


# --- Windows as text --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("16-21", [(16, 21)]),
        ("4pm-9pm", [(16, 21)]),
        ("4pm - 9pm", [(16, 21)]),
        ("16:00-21:00", [(16, 21)]),
        ("9pm to 11am", [(21, 11)]),
        ("12am-6am", [(0, 6)]),
        ("12pm-1pm", [(12, 13)]),
        ("0-24", [(0, 24)]),
        ("21-10, 14-16", [(21, 10), (14, 16)]),
        ("9pm-10am; 2pm-4pm", [(21, 10), (14, 16)]),
        ("21-0", [(21, 0)]),
    ],
)
def test_parse_windows(text: str, expected: list[tuple[int, int]]) -> None:
    assert form.parse_windows(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "   ", "16", "16-17-18", "abc", "16:30-17:00", "25-3", "0-25", "13pm-2pm", "5-5", "24-3"],
)
def test_parse_windows_rejects(text: str) -> None:
    with pytest.raises(form.FormError):
        form.parse_windows(text)


def test_format_windows_round_trips() -> None:
    windows = [{"start": 21, "end": 10}, {"start": 14, "end": 16}]
    assert form.format_windows(windows) == "21-10, 14-16"
    assert form.parse_windows(form.format_windows(windows)) == [(21, 10), (14, 16)]


# --- Periods ----------------------------------------------------------------------------


def test_form_to_period_normalises_days() -> None:
    assert form.form_to_period(" Peak ", 30, "16-21", [])["windows"] == [
        {"start": 16, "end": 21, "days": None}
    ]
    every = form.form_to_period("Peak", 30, "16-21", [str(d) for d in range(7)])
    assert every["windows"][0]["days"] is None
    weekend = form.form_to_period("Peak", 30, "16-21", ["6", "5"])
    assert weekend["windows"][0]["days"] == [5, 6]
    assert weekend["name"] == "Peak"
    assert weekend["tiers"] == [{"up_to_kwh_per_day": None, "c_per_kwh": 30.0}]


def test_period_form_round_trip() -> None:
    period = form.form_to_period("Export off-peak", 0.1, "21-10, 14-16", ["5", "6"])
    values = form.period_to_form(period)
    assert values == {
        "name": "Export off-peak",
        "rate": 0.1,
        "windows": "21-10, 14-16",
        "days": ["5", "6"],
    }
    assert form.form_to_period(
        values["name"], values["rate"], values["windows"], values["days"]
    ) == (period)


def test_flat_period_is_all_day() -> None:
    period = form.flat_period(30)
    assert period["name"] == "All day"
    assert period["windows"] == [{"start": 0, "end": 24, "days": None}]
    assert form.period_is_guided(period)


def test_which_periods_the_guided_editor_may_touch() -> None:
    assert form.period_is_guided(form.form_to_period("Peak", 30, "16-21", None))
    assert not form.period_is_guided(four4free_free_window())  # tiers
    catch_all = {
        "name": "Other",
        "catch_all": True,
        "windows": [],
        "tiers": [{"up_to_kwh_per_day": None, "c_per_kwh": 20.0}],
    }
    assert not form.period_is_guided(catch_all)
    mixed_days = {
        "name": "Mixed",
        "catch_all": False,
        "windows": [
            {"start": 9, "end": 12, "days": [0, 1, 2, 3, 4]},
            {"start": 9, "end": 17, "days": [5, 6]},
        ],
        "tiers": [{"up_to_kwh_per_day": None, "c_per_kwh": 20.0}],
    }
    assert not form.period_is_guided(mixed_days)
    assert not form.period_is_guided({**mixed_days, "windows": []})


def test_describe_periods_marks_advanced_ones() -> None:
    text = form.describe_periods([*glohome()["import_periods"], four4free_free_window()])
    assert "- **Peak**: 16-21 every day, 31.46 c/kWh" in text
    assert "- **Off-peak**: 21-11 every day, 16.50 c/kWh" in text
    assert "first 30 kWh/day 0.00 c/kWh, then 11.00 c/kWh" in text
    assert text.count("advanced editor") == 1
    assert form.describe_periods([]) == "_No periods yet._"


def test_describe_versions() -> None:
    text = form.describe_versions([glohome()])
    assert text == "- From **2026-10-28**: 3 import periods, supply 92.40 c/day"


# --- Hour map -----------------------------------------------------------------------------


def test_the_reference_monday_is_a_monday() -> None:
    assert form._A_MONDAY.weekday() == 0


def test_glohome_hour_map() -> None:
    text = form.review_text(glohome())
    assert text.splitlines() == [
        "**Import hours (AEST, NEM time, UTC+10)**",
        "Every day:",
        "- 00:00–11:00 Off-peak (16.50 c/kWh)",
        "- 11:00–16:00 Shoulder (13.20 c/kWh)",
        "- 16:00–21:00 Peak (31.46 c/kWh)",
        "- 21:00–24:00 Off-peak (16.50 c/kWh)",
    ]


def test_hour_map_groups_days_that_differ() -> None:
    version = form.new_version_dict(date(2026, 1, 1))
    version["import_periods"] = [
        form.form_to_period("Weekday peak", 40, "9-17", ["0", "1", "2", "3", "4"]),
        form.form_to_period("Weekend day", 25, "9-17", ["5", "6"]),
        form.form_to_period("Off-peak", 15, "17-9", None),
    ]
    assert form.draft_problem([version]) is None
    lines = form.review_text(version).splitlines()
    assert lines == [
        "**Import hours (AEST, NEM time, UTC+10)**",
        "Mon–Fri:",
        "- 00:00–09:00 Off-peak (15.00 c/kWh)",
        "- 09:00–17:00 Weekday peak (40.00 c/kWh)",
        "- 17:00–24:00 Off-peak (15.00 c/kWh)",
        "Sat–Sun:",
        "- 00:00–09:00 Off-peak (15.00 c/kWh)",
        "- 09:00–17:00 Weekend day (25.00 c/kWh)",
        "- 17:00–24:00 Off-peak (15.00 c/kWh)",
    ]


def test_hour_map_shows_gaps_without_failing() -> None:
    version = form.new_version_dict(date(2026, 1, 1))
    version["import_periods"] = [form.form_to_period("Peak", 31.46, "16-21", None)]
    text = form.review_text(version)
    assert "00:00–16:00 **not covered**" in text
    assert "16:00–21:00 Peak" in text


def test_hour_map_includes_export_only_when_present() -> None:
    version = glohome()
    assert "Export" not in form.review_text(version)
    version["export_periods"] = [
        form.form_to_period("Evening", 3.0, "16-21", None),
        form.form_to_period("Other", 0.1, "21-10, 14-16", None),
        form.form_to_period("Midday", 0.0, "10-14", None),
    ]
    text = form.review_text(version)
    assert "**Export hours (AEST, NEM time, UTC+10)**" in text
    assert "- 10:00–14:00 Midday (0.00 c/kWh)" in text


# --- Validation ----------------------------------------------------------------------------


def test_a_complete_draft_is_valid() -> None:
    assert form.draft_problem([glohome()]) is None
    tariff = form.build_tariff([glohome()])
    assert isinstance(tariff, Tariff)
    assert tariff.to_dict()["versions"][0]["supply_c_per_day"] == 92.40


def test_gap_is_reported() -> None:
    version = glohome()
    del version["import_periods"][2]
    assert "no period covers Monday 11:00" in (form.draft_problem([version]) or "")


def test_overlap_is_reported() -> None:
    version = glohome()
    version["import_periods"].append(form.form_to_period("Oops", 10, "12-14", None))
    assert "overlap" in (form.draft_problem([version]) or "")


def test_no_periods_is_reported() -> None:
    version = form.new_version_dict(date(2026, 1, 1))
    assert "at least one import period" in (form.draft_problem([version]) or "")


def test_duplicate_version_dates_are_reported() -> None:
    assert "distinct" in (form.draft_problem([glohome(), glohome()]) or "")


# --- Strings ---------------------------------------------------------------------------------


def _strings() -> dict:
    return json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("step", TIME_STEPS)
def test_every_step_that_takes_times_repeats_the_timezone_note(step: str) -> None:
    description = _strings()["options"]["step"][step]["description"]
    assert form.TIMEZONE_NOTE in description


def test_timezone_note_says_what_it_must() -> None:
    note = form.TIMEZONE_NOTE
    for phrase in ("AEST", "UTC+10", "no daylight saving", "NEM", "not Home Assistant"):
        assert phrase in note


def test_english_translation_matches_strings() -> None:
    translation = (PACKAGE / "translations" / "en.json").read_text(encoding="utf-8")
    assert json.loads(translation) == _strings()


def test_every_menu_option_and_step_has_a_label() -> None:
    steps = _strings()["options"]["step"]
    for step in (
        "init",
        "tariff",
        "tariff_import_periods",
        "tariff_export_periods",
        "tariff_review",
    ):
        assert steps[step]["menu_options"], step
    for step in (
        "keepalive",
        "tariff_basics",
        "tariff_flat_rate",
        "period_add",
        "period_edit_form",
    ):
        assert steps[step]["data"], step


# --- Statistics per tariff period --------------------------------------------------------------


def _ids(slug: str) -> tuple[str, str]:
    return f"stat:{slug}", f"stat:{slug}_cost"


def test_period_statistics_text_gives_the_daily_adjustment_and_the_statistics() -> None:
    tariff = form.build_tariff([glohome()])
    text = form.period_statistics_text(tariff, _ids, "AUD")
    # 92.40 c x 0.97 (3% discount, prices already include GST) = $0.89628.
    assert "AUD 0.89628" in text
    assert "92.40 c/day supply charge less the 3% discount" in text
    assert "GST" not in text
    assert "rates starting 2026-10-28" in text
    assert "- **Peak**: `stat:peak` and `stat:peak_cost`" in text
    assert "- **Off-peak**: `stat:off_peak` and `stat:off_peak_cost`" in text
    assert "- **Shoulder**: `stat:shoulder` and `stat:shoulder_cost`" in text
    assert "older rates" not in text


def test_period_statistics_text_explains_gst_and_a_missing_discount() -> None:
    version = glohome()
    version.update(prices_include_gst=False, discount_pct=0.0, supply_c_per_day=100.0)
    text = form.period_statistics_text(form.build_tariff([version]), _ids, "AUD")
    assert "AUD 1.10000" in text  # 100 c plus 10% GST
    assert "100.00 c/day supply charge plus 10% GST" in text
    assert "discount" not in text


def test_period_statistics_text_uses_the_newest_version_and_flags_old_periods() -> None:
    old = glohome()
    new = glohome()
    new.update(effective_from="2026-12-01", supply_c_per_day=110.0, discount_pct=0.0)
    new["import_periods"] = [form.form_to_period("All day", 20.0, "0-24", None)]
    text = form.period_statistics_text(form.build_tariff([old, new]), _ids, "AUD")
    assert "AUD 1.10000" in text  # the 2026-12-01 supply charge, no discount
    assert "rates starting 2026-12-01" in text
    assert "- **All day**: `stat:all_day`" in text
    assert "- **Peak** _(only in older rates, so zero from now on)_: `stat:peak`" in text


def test_period_statistics_step_has_its_strings() -> None:
    steps = _strings()["options"]["step"]
    step = steps["tariff_period_stats"]
    assert "{details}" in step["description"]
    assert step["data"]["enabled"]
    assert "tariff_period_stats" in steps["tariff"]["menu_options"]
    # It must warn about the double count and say the adjustment is a fixed number.
    for phrase in ("twice", "daily cost adjustment", "one grid source only", "fixed number"):
        assert phrase in step["description"]
