"""Diagnostics summarise the tariff without exposing the NMI or the session."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.citipower_myenergy.const import (
    CONF_PERIOD_STATS,
    CONF_TARIFF,
    DOMAIN,
)
from custom_components.citipower_myenergy.diagnostics import async_get_config_entry_diagnostics
from custom_components.citipower_myenergy.tariff import flat_tariff

NMI = "61020000001"


def _entry(options: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NMI,
        data={"nmi": NMI, "cookies": {"sid": "secret-session"}, "address": "1 Example St"},
        options=options,
    )
    coordinator = MagicMock()
    coordinator.data = None
    coordinator.api.client.cookies = {"sid": "secret-session"}
    coordinator.session_dead = False
    entry.runtime_data = coordinator
    return entry


async def test_without_a_tariff_nothing_is_summarised(hass: HomeAssistant) -> None:
    result = await async_get_config_entry_diagnostics(hass, _entry({}))
    assert result["cost_tracking"] == {"tariff_configured": False, "period_statistics": False}


async def test_the_tariff_structure_is_summarised(hass: HomeAssistant) -> None:
    tariff = flat_tariff(
        25.0, effective_from=date(2026, 1, 1), supply_c_per_day=90.0, discount_pct=3.0
    )
    entry = _entry({CONF_TARIFF: tariff.to_dict(), CONF_PERIOD_STATS: True})
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["cost_tracking"] == {
        "tariff_configured": True,
        "period_statistics": True,
        "timezone": "Australia/Brisbane",  # no zone stored: fixed AEST
        "timezone_set": False,
        "supply_mode": "none",
        "versions": [
            {
                "effective_from": "2026-01-01",
                "supply_c_per_day": 90.0,
                "discount_pct": 3.0,
                "prices_include_gst": True,
                "import_periods": ["All day"],
                "export_periods": [],
            }
        ],
        "period_slugs": ["all_day"],
    }


async def test_a_chosen_zone_is_reported(hass: HomeAssistant) -> None:
    tariff = flat_tariff(25.0, effective_from=date(2026, 1, 1), timezone="Australia/Melbourne")
    entry = _entry({CONF_TARIFF: tariff.to_dict()})
    summary = (await async_get_config_entry_diagnostics(hass, entry))["cost_tracking"]
    assert summary["timezone"] == "Australia/Melbourne"
    assert summary["timezone_set"] is True


async def test_a_bad_tariff_is_reported_not_raised(hass: HomeAssistant) -> None:
    entry = _entry({CONF_TARIFF: {"schema": 99, "versions": []}})
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["cost_tracking"]["tariff_configured"] is True
    assert "Unsupported tariff schema" in result["cost_tracking"]["tariff_error"]


async def test_the_summary_leaks_neither_the_nmi_nor_the_session(hass: HomeAssistant) -> None:
    tariff = flat_tariff(25.0, effective_from=date(2026, 1, 1))
    entry = _entry({CONF_TARIFF: tariff.to_dict()})
    summary = json.dumps((await async_get_config_entry_diagnostics(hass, entry))["cost_tracking"])
    assert NMI not in summary
    assert "secret-session" not in summary


async def test_the_write_trail_is_shown_and_never_names_the_nmi(hass: HomeAssistant) -> None:
    from custom_components.citipower_myenergy.chain_guard import guard_for
    from custom_components.citipower_myenergy.statistics import statistic_id_for

    guard = guard_for(hass, NMI)
    guard.record(
        who="poll",
        statistic_id=statistic_id_for(NMI, "E1") + "_peak_cost",  # the id carries the NMI
        repair_from="2026-11-09",
        supply_mode="spread",
        start_total=499.766,
        rows=48,
        join_repaired=True,
        verified=True,
    )
    guard.broken["e1_peak_cost"] = "09 Nov 2026 00:00"
    tariff = flat_tariff(25.0, effective_from=date(2026, 1, 1))
    result = await async_get_config_entry_diagnostics(hass, _entry({CONF_TARIFF: tariff.to_dict()}))

    writes = result["statistics_writes"]
    assert writes["recent_writes"][-1]["statistic"] == "e1_peak_cost"
    assert writes["recent_writes"][-1]["who"] == "poll"
    assert writes["recent_writes"][-1]["join_repaired"] is True
    assert writes["not_joined"] == {"e1_peak_cost": "09 Nov 2026 00:00"}
    assert NMI not in json.dumps(result, default=str)  # nowhere in the whole download
