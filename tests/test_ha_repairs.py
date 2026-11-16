"""The Repairs fix flow for a broken running total, apart from the repricing itself.

``test_ha_statistics.py`` runs the flow end to end against a real recorder. These tests pin the
flow's own decisions (which issue's text it shows, when it aborts and with what reason, what it
does when the repricing fails) with the repricing patched out, so no recorder is needed.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.citipower_myenergy.chain_guard import guard_for
from custom_components.citipower_myenergy.const import CONF_TARIFF, DOMAIN
from custom_components.citipower_myenergy.repairs import async_create_fix_flow
from custom_components.citipower_myenergy.tariff import Period, Tariff, TariffVersion, Tier, Window

NMI = "61020000001"
FIX = "custom_components.citipower_myenergy.repairs.async_reprice_entry"


def _tariff() -> Tariff:
    return Tariff(
        (
            TariffVersion(
                effective_from=date(2026, 1, 1),
                supply_c_per_day=90.0,
                import_periods=(Period("Flat", (Window(0, 24),), (Tier(None, 30.0),)),),
            ),
        )
    )


def _entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NMI,
        data={"nmi": NMI},
        options={CONF_TARIFF: _tariff().to_dict()} if options is None else options,
    )
    entry.add_to_hass(hass)
    return entry


async def _flow(hass: HomeAssistant, entry_id: str, issue_id: str | None = None):
    flow = await async_create_fix_flow(hass, "chain_break_x", {"entry_id": entry_id})
    flow.hass = hass
    if issue_id is not None:
        flow.issue_id = issue_id
    return flow


def _raise_issue(hass: HomeAssistant, issue_id: str, placeholders: dict | None) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="chain_break",
        translation_placeholders=placeholders,
    )


@pytest.mark.parametrize("data", [None, {}, {"entry_id": None}, {"entry_id": 7}])
async def test_a_notice_without_a_usable_entry_id_aborts_as_entry_missing(
    hass: HomeAssistant, data
) -> None:
    flow = await async_create_fix_flow(hass, "chain_break_x", data)
    flow.hass = hass

    assert (await flow.async_step_init())["reason"] == "entry_missing"


async def test_the_confirmation_names_the_statistic_and_hour_of_the_notice(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    _raise_issue(hass, "issue_a", {"statistic": "cost_import", "hour": "01 Jan 2026 04:00"})

    form = await (await _flow(hass, entry.entry_id, "issue_a")).async_step_init()

    assert form["type"] == "form" and form["step_id"] == "confirm"
    assert dict(form["description_placeholders"]) == {
        "statistic": "cost_import",
        "hour": "01 Jan 2026 04:00",
    }


@pytest.mark.parametrize(
    ("issue_id", "placeholders"),
    [
        (None, None),  # the manager has not set an issue id
        ("never_raised", None),  # the id names a notice that has since been cleared
        ("issue_b", None),  # a notice that carries no placeholders
    ],
)
async def test_the_confirmation_falls_back_to_generic_wording(
    hass: HomeAssistant, issue_id, placeholders
) -> None:
    entry = _entry(hass)
    _raise_issue(hass, "issue_b", placeholders)

    form = await (await _flow(hass, entry.entry_id, issue_id)).async_step_confirm()

    assert dict(form["description_placeholders"]) == {
        "statistic": "a cost statistic",
        "hour": "an hour in its history",
    }


@pytest.mark.parametrize(
    "options",
    [
        {},  # no tariff saved
        {CONF_TARIFF: {"versions": "not a list"}},  # a tariff that no longer parses
    ],
    ids=["no-tariff", "malformed-tariff"],
)
async def test_without_a_usable_tariff_the_flow_aborts_and_reprices_nothing(
    hass: HomeAssistant, options
) -> None:
    entry = _entry(hass, options)

    with patch(FIX, new=AsyncMock()) as reprice:
        flow = await _flow(hass, entry.entry_id)
        result = await flow.async_step_confirm({})

    assert result["reason"] == "no_tariff"
    reprice.assert_not_awaited()


async def test_confirming_reprices_all_history_and_forgets_old_failures_first(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    guard = guard_for(hass, NMI)
    guard.broken["cost_import"] = "01 Jan 2026 04:00"
    seen_before_repricing: list[dict[str, str]] = []

    async def reprice(*args, **kwargs) -> int:
        # The old failures are gone by the time the rewrite checks the chain again.
        seen_before_repricing.append(dict(guard.broken))
        return 24

    with patch(FIX, new=AsyncMock(side_effect=reprice)) as mock:
        result = await (await _flow(hass, entry.entry_id)).async_step_confirm({})

    assert result["type"] == "create_entry"
    assert seen_before_repricing == [{}]
    _, args, _ = mock.mock_calls[0]
    assert args[1] is entry
    assert args[3] == date(2000, 1, 1)  # from the first day anything could exist


async def test_a_failed_repricing_aborts_and_the_notice_is_left_to_the_next_check(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Current behaviour, pinned: the failures are forgotten before the rewrite is attempted.

    So when the rewrite itself fails the notice is already gone. It comes back if the next write
    or check finds the row still does not join; the flow does not restore it.
    """
    entry = _entry(hass)
    guard = guard_for(hass, NMI)
    guard.entry_id = entry.entry_id
    guard.broken["cost_import"] = "01 Jan 2026 04:00"
    ir.async_create_issue(
        hass,
        DOMAIN,
        guard.issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="chain_break",
    )

    with patch(FIX, new=AsyncMock(side_effect=RuntimeError("recorder unavailable"))):
        result = await (await _flow(hass, entry.entry_id)).async_step_confirm({})

    assert result["reason"] == "repair_failed"
    assert "Repairing the cost statistics failed" in caplog.text
    assert guard.broken == {}
    assert ir.async_get(hass).async_get_issue(DOMAIN, guard.issue_id) is None
