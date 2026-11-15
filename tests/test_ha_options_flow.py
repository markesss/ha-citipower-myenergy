"""Options flow: the keep-alive setting and the tariff editor."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.citipower_myenergy import tariff_form as form
from custom_components.citipower_myenergy.const import (
    CONF_COOKIES,
    CONF_KEEPALIVE_MINUTES,
    CONF_NMI,
    CONF_PERIOD_STATS,
    CONF_SIGNED_IN_AT,
    CONF_TARIFF,
    DOMAIN,
)
from custom_components.citipower_myenergy.session_store import SessionStore
from custom_components.citipower_myenergy.tariff import Tariff, flat_tariff

NMI = "61020000001"

BASICS = {
    "effective_from": "2026-10-28",
    "prices_include_gst": True,
    "supply_c_per_day": 92.40,
    "discount_pct": 3.0,
}


def glohome_version(effective_from: str = "2026-10-28") -> dict[str, Any]:
    """GloBird GLOHOME Flexible Rate, CitiPower: import only, rates inc. GST."""
    version = form.new_version_dict(date.fromisoformat(effective_from))
    version.update(supply_c_per_day=92.40, discount_pct=3.0, prices_include_gst=True)
    version["import_periods"] = [
        form.form_to_period("Peak", 31.46, "16-21", None),
        form.form_to_period("Off-peak", 16.50, "21-11", None),
        form.form_to_period("Shoulder", 13.20, "11-16", None),
    ]
    return version


def stored(*versions: dict[str, Any]) -> dict[str, Any]:
    """Return a stored tariff dict for the given version dicts."""
    return form.build_tariff(list(versions)).to_dict()


def tiered_version() -> dict[str, Any]:
    """FOUR4FREE-style tariff: the middle period has a per-day allowance (two tiers)."""
    version = form.new_version_dict(date(2026, 10, 28))
    version.update(supply_c_per_day=129.80)
    version["import_periods"] = [
        form.form_to_period("Peak", 36.63, "16-23", None),
        {
            "name": "Free window",
            "catch_all": False,
            "windows": [{"start": 11, "end": 15, "days": None}],
            "tiers": [
                {"up_to_kwh_per_day": 30.0, "c_per_kwh": 0.0},
                {"up_to_kwh_per_day": None, "c_per_kwh": 11.0},
            ],
        },
        form.form_to_period("Other", 22.0, "15-16, 23-11", None),
    ]
    return version


def make_entry(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NMI,
        data={CONF_NMI: NMI, CONF_COOKIES: {"sid": "x"}, CONF_SIGNED_IN_AT: "x"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def go(hass: HomeAssistant, result: dict[str, Any], user_input: dict[str, Any]):
    """Continue a flow: a form takes its fields, a menu takes ``{"next_step_id": ...}``."""
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def pick(hass: HomeAssistant, result: dict[str, Any], option: str):
    assert result["type"] is FlowResultType.MENU, result
    assert option in result["menu_options"], result["menu_options"]
    return await go(hass, result, {"next_step_id": option})


async def open_tariff_menu(hass: HomeAssistant, entry: MockConfigEntry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["keepalive", "tariff"]
    result = await pick(hass, result, "tariff")
    assert result["step_id"] == "tariff"
    return result


async def add_period(hass: HomeAssistant, result: dict[str, Any], **fields: Any):
    """From a periods menu, add one period and return to the menu."""
    result = await pick(hass, result, "period_add")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "period_add"
    return await go(hass, result, fields)


async def start_time_of_use(hass: HomeAssistant, entry: MockConfigEntry):
    """Walk a new time-of-use tariff to the import periods menu."""
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_setup")
    assert result["step_id"] == "tariff_type"
    result = await go(hass, result, {"tariff_type": "tou"})
    assert result["step_id"] == "tariff_basics"
    result = await go(hass, result, BASICS)
    assert result["step_id"] == "tariff_import_periods"
    return result


async def add_glohome_periods(hass: HomeAssistant, result: dict[str, Any]):
    for name, rate, windows in (
        ("Peak", 31.46, "16-21"),
        ("Off-peak", 16.50, "21-11"),
        ("Shoulder", 13.20, "11-16"),
    ):
        result = await add_period(hass, result, name=name, rate=rate, windows=windows)
        assert result["step_id"] == "tariff_import_periods"
    return result


# --- Keep-alive ---------------------------------------------------------------------------


async def test_keepalive_still_works_and_keeps_the_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    tariff = flat_tariff(30.0, effective_from=date(2026, 10, 28)).to_dict()
    entry = make_entry(hass, {CONF_TARIFF: tariff})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await pick(hass, result, "keepalive")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "keepalive"
    result = await go(hass, result, {CONF_KEEPALIVE_MINUTES: 30})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_KEEPALIVE_MINUTES] == 30
    assert entry.options[CONF_TARIFF] == tariff


# --- Flat rate and time of use --------------------------------------------------------------


async def test_flat_rate_flow_saves_a_valid_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass, {CONF_KEEPALIVE_MINUTES: 20})
    result = await open_tariff_menu(hass, entry)
    assert result["menu_options"] == ["tariff_setup", "tariff_advanced"]
    result = await pick(hass, result, "tariff_setup")
    result = await go(hass, result, {"tariff_type": "flat"})
    result = await go(hass, result, {**BASICS, "effective_from": "2026-11-01"})
    assert result["step_id"] == "tariff_flat_rate"
    result = await go(hass, result, {"rate": 30.0})
    # No B1 channel, so there is no export step.
    assert result["step_id"] == "tariff_review"
    assert result["menu_options"] == ["review_save", "review_back"]
    result = await pick(hass, result, "review_save")
    assert result["type"] is FlowResultType.CREATE_ENTRY

    expected = flat_tariff(
        30.0, effective_from=date(2026, 11, 1), supply_c_per_day=92.40, discount_pct=3.0
    )
    assert entry.options[CONF_TARIFF] == expected.to_dict()
    assert entry.options[CONF_KEEPALIVE_MINUTES] == 20  # untouched
    Tariff.from_dict(entry.options[CONF_TARIFF])  # and it validates


async def test_glohome_time_of_use_flow_saves_the_expected_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    result = await start_time_of_use(hass, entry)
    result = await add_glohome_periods(hass, result)
    assert "Shoulder" in result["description_placeholders"]["periods"]
    result = await pick(hass, result, "periods_done")
    assert result["step_id"] == "tariff_review"
    assert result["description_placeholders"]["problem"] == ""
    assert "16:00–21:00 Peak (31.46 c/kWh)" in result["description_placeholders"]["hour_map"]
    result = await pick(hass, result, "review_save")
    assert result["type"] is FlowResultType.CREATE_ENTRY

    assert entry.options[CONF_TARIFF] == stored(glohome_version())
    version = entry.options[CONF_TARIFF]["versions"][0]
    assert version["supply_c_per_day"] == 92.40
    assert version["discount_pct"] == 3.0
    assert version["prices_include_gst"] is True
    assert version["export_periods"] == []


async def test_period_with_several_windows_and_days(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    result = await start_time_of_use(hass, entry)
    result = await add_period(
        hass, result, name="Weekend", rate=10.0, windows="9pm-10am, 2pm-4pm", days=["5", "6"]
    )
    assert result["step_id"] == "tariff_import_periods"
    result = await pick(hass, result, "period_edit")
    result = await go(hass, result, {"period": "Weekend"})
    assert result["step_id"] == "period_edit_form"
    # The edit form is prefilled from the stored period.
    suggested = {
        str(key): key.description["suggested_value"] for key in result["data_schema"].schema
    }
    assert suggested["windows"] == "21-10, 14-16"
    assert suggested["days"] == ["5", "6"]


# --- Problems at review -----------------------------------------------------------------------


async def test_a_gap_is_shown_at_review_and_cannot_be_saved(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    result = await start_time_of_use(hass, entry)
    result = await add_period(hass, result, name="Peak", rate=31.46, windows="16-21")
    result = await pick(hass, result, "periods_done")
    assert result["step_id"] == "tariff_review"
    assert result["menu_options"] == ["review_back"]
    placeholders = result["description_placeholders"]
    assert "Fix this before saving" in placeholders["problem"]
    assert "no period covers" in placeholders["problem"]
    assert "**not covered**" in placeholders["hour_map"]

    # Going back lets the user fix it; nothing was saved.
    assert CONF_TARIFF not in entry.options
    result = await pick(hass, result, "review_back")
    assert result["step_id"] == "tariff_import_periods"
    assert CONF_TARIFF not in entry.options


async def test_an_overlap_is_shown_at_review(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    result = await start_time_of_use(hass, entry)
    result = await add_period(hass, result, name="All day", rate=20.0, windows="0-24")
    result = await add_period(hass, result, name="Peak", rate=30.0, windows="16-21")
    result = await pick(hass, result, "periods_done")
    assert result["menu_options"] == ["review_back"]
    assert "overlap" in result["description_placeholders"]["problem"]
    assert CONF_TARIFF not in entry.options


async def test_period_form_errors(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    result = await start_time_of_use(hass, entry)
    result = await add_period(hass, result, name="Peak", rate=31.46, windows="16-21")
    result = await pick(hass, result, "period_add")

    bad = await go(hass, result, {"name": "Peak", "rate": 1.0, "windows": "banana"})
    assert bad["type"] is FlowResultType.FORM
    assert bad["errors"] == {"name": "name_in_use", "windows": "invalid_windows"}
    assert "banana" in bad["description_placeholders"]["detail"]

    blank = await go(hass, bad, {"name": " ", "rate": 1.0, "windows": "0-24"})
    assert blank["errors"] == {"name": "name_required"}

    ok = await go(hass, blank, {"name": "Rest", "rate": 1.0, "windows": "21-16"})
    assert ok["step_id"] == "tariff_import_periods"


# --- Export is for accounts that export ------------------------------------------------------


async def test_export_step_is_hidden_without_a_b1_channel(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    await SessionStore(hass).async_save_suffixes(entry.entry_id, ["E1"])
    result = await start_time_of_use(hass, entry)
    result = await add_glohome_periods(hass, result)
    result = await pick(hass, result, "periods_done")
    assert result["step_id"] == "tariff_review"


async def test_export_step_is_offered_with_a_b1_channel(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    await SessionStore(hass).async_save_suffixes(entry.entry_id, ["E1", "B1"])
    result = await start_time_of_use(hass, entry)
    result = await add_glohome_periods(hass, result)
    result = await pick(hass, result, "periods_done")
    assert result["step_id"] == "tariff_export_periods"
    for name, rate, windows in (
        ("Evening", 3.0, "16-21"),
        ("Off-peak", 0.1, "21-10, 14-16"),
        ("Midday", 0.0, "10-14"),
    ):
        result = await add_period(hass, result, name=name, rate=rate, windows=windows)
        assert result["step_id"] == "tariff_export_periods"
    result = await pick(hass, result, "periods_done")
    assert result["step_id"] == "tariff_review"
    assert "Export hours" in result["description_placeholders"]["hour_map"]
    result = await pick(hass, result, "review_save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    exports = entry.options[CONF_TARIFF]["versions"][0]["export_periods"]
    assert [period["name"] for period in exports] == ["Evening", "Off-peak", "Midday"]


async def test_export_step_is_offered_when_the_stored_tariff_already_exports(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    version = glohome_version()
    version["export_periods"] = [form.flat_period(5.0)]
    entry = make_entry(hass, {CONF_TARIFF: stored(version)})
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_edit_latest")
    result = await go(hass, result, BASICS)
    result = await pick(hass, result, "periods_done")
    assert result["step_id"] == "tariff_export_periods"


# --- Versions and editing ---------------------------------------------------------------------


async def test_add_a_new_version_copies_the_latest(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    first = glohome_version("2026-10-28")
    entry = make_entry(hass, {CONF_TARIFF: stored(first)})
    result = await open_tariff_menu(hass, entry)
    assert result["description_placeholders"]["summary"] == (
        "- From **2026-10-28**: 3 import periods, supply 92.40 c/day"
    )
    result = await pick(hass, result, "tariff_add_version")
    assert result["step_id"] == "tariff_basics"

    # The date of an existing version is refused.
    clash = await go(hass, result, BASICS)
    assert clash["errors"] == {"effective_from": "date_in_use"}

    result = await go(
        hass,
        clash,
        {**BASICS, "effective_from": "2026-12-01", "supply_c_per_day": 100.0},
    )
    assert result["step_id"] == "tariff_import_periods"
    assert "Peak" in result["description_placeholders"]["periods"]  # copied
    result = await pick(hass, result, "periods_done")
    result = await pick(hass, result, "review_save")
    assert result["type"] is FlowResultType.CREATE_ENTRY

    versions = entry.options[CONF_TARIFF]["versions"]
    assert [v["effective_from"] for v in versions] == ["2026-10-28", "2026-12-01"]
    assert versions[0] == stored(first)["versions"][0]  # the old rates are untouched
    assert versions[1]["supply_c_per_day"] == 100.0
    assert versions[1]["import_periods"] == versions[0]["import_periods"]


async def test_deleting_a_period_leaves_a_gap_that_review_catches(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass, {CONF_TARIFF: stored(glohome_version())})
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_edit_latest")
    result = await go(hass, result, BASICS)
    result = await pick(hass, result, "period_delete")
    assert result["step_id"] == "period_delete"
    result = await go(hass, result, {"period": "Shoulder"})
    assert result["step_id"] == "tariff_import_periods"
    assert "Shoulder" not in result["description_placeholders"]["periods"]
    result = await pick(hass, result, "periods_done")
    assert result["menu_options"] == ["review_back"]
    assert "no period covers" in result["description_placeholders"]["problem"]

    # Put it back and the tariff saves again.
    result = await pick(hass, result, "review_back")
    result = await add_period(hass, result, name="Shoulder", rate=13.2, windows="11-16")
    result = await pick(hass, result, "periods_done")
    result = await pick(hass, result, "review_save")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (
        entry.options[CONF_TARIFF]["versions"][0]["import_periods"]
        == (stored(glohome_version())["versions"][0]["import_periods"])
    )


async def test_a_tiered_period_is_refused_and_preserved(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    original = stored(tiered_version())
    entry = make_entry(hass, {CONF_TARIFF: original})
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_edit_latest")
    result = await go(
        hass,
        result,
        {**BASICS, "effective_from": "2026-10-28", "supply_c_per_day": 135.0},
    )
    assert "advanced editor" in result["description_placeholders"]["periods"]

    # The guided editor will not rewrite it.
    result = await pick(hass, result, "period_edit")
    refused = await go(hass, result, {"period": "Free window"})
    assert refused["type"] is FlowResultType.FORM
    assert refused["step_id"] == "period_edit"
    assert refused["errors"] == {"period": "period_not_guided"}

    # Editing a simple period beside it leaves the tiers alone.
    ok = await go(hass, refused, {"period": "Peak"})
    assert ok["step_id"] == "period_edit_form"
    back = await go(hass, ok, {"name": "Peak", "rate": 37.0, "windows": "16-23"})
    assert back["step_id"] == "tariff_import_periods"
    result = await pick(hass, back, "periods_done")
    result = await pick(hass, result, "review_save")
    assert result["type"] is FlowResultType.CREATE_ENTRY

    saved = entry.options[CONF_TARIFF]["versions"][0]
    assert saved["supply_c_per_day"] == 135.0
    assert saved["import_periods"][0]["tiers"] == [{"up_to_kwh_per_day": None, "c_per_kwh": 37.0}]
    assert saved["import_periods"][1] == original["versions"][0]["import_periods"][1]
    assert saved["import_periods"][1]["tiers"][0] == {"up_to_kwh_per_day": 30.0, "c_per_kwh": 0.0}


# --- Advanced ---------------------------------------------------------------------------------


async def test_advanced_yaml_saves_a_valid_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_advanced")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tariff_advanced"
    tariff = stored(tiered_version())
    result = await go(hass, result, {"tariff": tariff})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TARIFF] == tariff


async def test_advanced_yaml_rejects_a_bad_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_advanced")
    broken = stored(glohome_version())
    broken["versions"][0]["import_periods"][0]["windows"][0]["end"] = 22  # overlaps Off-peak
    result = await go(hass, result, {"tariff": broken})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"tariff": "invalid_tariff"}
    assert "overlap" in result["description_placeholders"]["detail"]
    assert CONF_TARIFF not in entry.options

    for junk in ({"schema": 99, "versions": []}, {"versions": "nope"}, {}):
        result = await go(hass, result, {"tariff": junk})
        assert result["errors"] == {"tariff": "invalid_tariff"}


async def test_advanced_editor_is_prefilled_with_the_stored_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    tariff = stored(glohome_version())
    entry = make_entry(hass, {CONF_TARIFF: tariff})
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_advanced")
    (marker,) = result["data_schema"].schema
    assert marker.description["suggested_value"] == tariff


# --- Remove and recalculate ---------------------------------------------------------------------


async def test_remove_the_tariff_keeps_other_options(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass, {CONF_KEEPALIVE_MINUTES: 25, CONF_TARIFF: stored(glohome_version())})
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_remove")
    result = await go(hass, result, {"confirm": False})
    assert result["step_id"] == "tariff"  # declined: back to the menu
    assert CONF_TARIFF in entry.options
    result = await pick(hass, result, "tariff_remove")
    result = await go(hass, result, {"confirm": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_TARIFF not in entry.options
    assert entry.options[CONF_KEEPALIVE_MINUTES] == 25


async def test_recalculate_reprices_from_a_date(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass, {CONF_TARIFF: stored(glohome_version())})
    await SessionStore(hass).async_save_suffixes(entry.entry_id, ["E1"])
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_recalculate")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tariff_recalculate"
    recalc = AsyncMock(return_value=48)
    with patch("custom_components.citipower_myenergy.tariff_flow.async_recalculate_costs", recalc):
        result = await go(hass, result, {"from_date": "2026-11-01"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "recalculated"
    assert result["description_placeholders"] == {"rows": "48", "from_date": "2026-11-01"}
    recalc.assert_awaited_once()
    args = recalc.await_args.args
    assert args[1:3] == (NMI, NMI)  # nmi, and the name falls back to it
    assert args[3] == "E1"
    assert isinstance(args[4], Tariff)
    assert args[5] == date(2026, 11, 1)
    assert entry.options[CONF_TARIFF] == stored(glohome_version())  # nothing else changed


async def test_recalculate_is_only_offered_with_a_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass)
    result = await open_tariff_menu(hass, entry)
    assert "tariff_recalculate" not in result["menu_options"]
    assert "tariff_remove" not in result["menu_options"]


@pytest.mark.parametrize("raw", [{"schema": 1, "versions": []}, {"junk": True}])
async def test_an_invalid_stored_tariff_offers_a_fresh_start(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    custom_integration,
    hass_storage,
    raw: dict[str, Any],
) -> None:
    entry = make_entry(hass, {CONF_TARIFF: raw})
    result = await open_tariff_menu(hass, entry)
    assert result["menu_options"] == ["tariff_setup", "tariff_advanced"]
    assert "not valid" in result["description_placeholders"]["summary"]


# --- Statistics per tariff period ---------------------------------------------------------------


async def test_period_statistics_is_not_offered_without_a_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    result = await open_tariff_menu(hass, make_entry(hass))
    assert "tariff_period_stats" not in result["menu_options"]


async def test_period_statistics_is_offered_with_a_tariff(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass, {CONF_TARIFF: stored(glohome_version())})
    result = await open_tariff_menu(hass, entry)
    assert "tariff_period_stats" in result["menu_options"]


async def test_period_statistics_step_shows_the_adjustment_and_the_ids(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass, {CONF_TARIFF: stored(glohome_version())})
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_period_stats")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tariff_period_stats"
    details = result["description_placeholders"]["details"]
    assert f"{hass.config.currency} 0.89628" in details
    for slug in ("peak", "off_peak", "shoulder"):
        assert f"citipower_myenergy:{NMI}_e1_{slug}`" in details
        assert f"citipower_myenergy:{NMI}_e1_{slug}_cost`" in details
    # Off until the user turns it on.
    (marker,) = result["data_schema"].schema
    assert marker.default() is False


async def test_period_statistics_can_be_turned_on_and_off_keeping_the_other_options(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass, {CONF_KEEPALIVE_MINUTES: 25, CONF_TARIFF: stored(glohome_version())})
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_period_stats")
    result = await go(hass, result, {"enabled": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PERIOD_STATS] is True
    assert entry.options[CONF_KEEPALIVE_MINUTES] == 25
    assert entry.options[CONF_TARIFF] == stored(glohome_version())

    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_period_stats")
    (marker,) = result["data_schema"].schema
    assert marker.default() is True  # remembers the current choice
    result = await go(hass, result, {"enabled": False})
    assert entry.options[CONF_PERIOD_STATS] is False
    assert entry.options[CONF_TARIFF] == stored(glohome_version())


async def test_the_keepalive_step_keeps_the_period_statistics_option(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, hass_storage
) -> None:
    entry = make_entry(hass, {CONF_PERIOD_STATS: True, CONF_TARIFF: stored(glohome_version())})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await pick(hass, result, "keepalive")
    result = await go(hass, result, {CONF_KEEPALIVE_MINUTES: 30})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PERIOD_STATS] is True
    assert entry.options[CONF_KEEPALIVE_MINUTES] == 30


@pytest.mark.parametrize("enabled", [True, False])
async def test_recalculate_rewrites_period_statistics_only_when_the_option_is_on(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    custom_integration,
    hass_storage,
    enabled: bool,
) -> None:
    entry = make_entry(hass, {CONF_TARIFF: stored(glohome_version()), CONF_PERIOD_STATS: enabled})
    await SessionStore(hass).async_save_suffixes(entry.entry_id, ["E1"])
    result = await open_tariff_menu(hass, entry)
    result = await pick(hass, result, "tariff_recalculate")
    recalc = AsyncMock(return_value=24)
    with patch("custom_components.citipower_myenergy.tariff_flow.async_recalculate_costs", recalc):
        await go(hass, result, {"from_date": "2026-11-01"})
    assert recalc.await_args.kwargs == {"period_statistics": enabled}
