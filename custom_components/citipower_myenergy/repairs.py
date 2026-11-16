"""Repair a cost statistic whose running total does not join.

``chain_guard.py`` raises the notice when a check after a write finds a row whose running total
is not the row before it plus its own state. The fix reprices the entry's whole history from
its stored kWh readings, exactly as *Recalculate cost history* does, which rebuilds every
cost statistic's chain from the first row.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir

from .chain_guard import guard_for
from .const import CONF_NMI, CONF_TARIFF, DOMAIN
from .exceptions import TariffError
from .reprice import FAR_PAST, async_reprice_entry
from .tariff import Tariff

_LOGGER = logging.getLogger(__name__)


class ChainBreakRepairFlow(RepairsFlow):
    """Ask once, then reprice the history."""

    def __init__(self, entry_id: str | None) -> None:
        """Remember which entry the notice belongs to."""
        self._entry_id = entry_id

    def _placeholders(self) -> dict[str, str]:
        """Return the notice's {statistic} and {hour} for the confirmation text.

        Home Assistant's own confirm-only flow passes the issue's placeholders to its form, but a
        custom fix flow has to do it itself. The text lives in the fix flow's step, not at the top
        of the issue, because an issue with a fix flow cannot also have its own description
        (hassfest rejects both together).
        """
        issue_id = getattr(self, "issue_id", None)
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id) if issue_id else None
        if issue is not None and issue.translation_placeholders:
            return dict(issue.translation_placeholders)
        return {"statistic": "a cost statistic", "hour": "an hour in its history"}

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Start with the confirmation."""
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Reprice on confirmation."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id or "")
        if entry is None:
            return self.async_abort(reason="entry_missing")
        stored = entry.options.get(CONF_TARIFF)
        try:
            tariff = Tariff.from_dict(stored) if stored else None
        except TariffError:
            tariff = None
        if tariff is None:
            return self.async_abort(reason="no_tariff")

        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=self._placeholders(),
            )

        guard = guard_for(self.hass, entry.data[CONF_NMI])
        # Forget the old failures first: the rewrite checks every statistic again and raises the
        # notice straight back if one still does not join.
        guard.reset()
        try:
            await async_reprice_entry(self.hass, entry, tariff, FAR_PAST)
        except Exception:
            _LOGGER.exception("Repairing the cost statistics failed")
            return self.async_abort(reason="repair_failed")
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the flow for the broken-chain notice."""
    entry_id = (data or {}).get("entry_id")
    return ChainBreakRepairFlow(entry_id if isinstance(entry_id, str) else None)
