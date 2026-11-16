"""Reprice an entry's stored history from its stored kWh statistics.

Shared by the options flow (a tariff, clock or supply-mode change) and the Repairs fix for a
cost statistic whose running total does not join. Each ``async_recalculate_costs`` call takes
the NMI's write lock, so a poll that is writing finishes first (see ``chain_guard.py``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS, CONF_DESCRIPTION, CONF_NMI, CONF_PERIOD_STATS
from .session_store import SessionStore
from .statistics import async_recalculate_costs
from .tariff import Tariff, supply_mode_of

# Before any data the portal could hold, so a recalculation reaches all of an entry's history.
FAR_PAST = date(2000, 1, 1)


async def async_reprice_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[Any],
    tariff: Tariff,
    from_day: date,
    *,
    period_statistics: bool | None = None,
    supply_mode: str | None = None,
    recalculate: Callable[..., Awaitable[int]] = async_recalculate_costs,
) -> int:
    """Reprice every priced channel's stored history from a day; return combined-cost hours.

    The per-period switch and supply mode default to the entry's saved options; the options
    flow passes the values it is about to save instead, so history follows the new choice.
    ``recalculate`` is a parameter so the options flow can keep its own reference to the
    function (its tests replace that).
    """
    data = entry.data
    name = data.get(CONF_DESCRIPTION) or data.get(CONF_ADDRESS) or data[CONF_NMI]
    store = SessionStore(hass)
    await store.async_load()
    suffixes = store.suffixes(entry.entry_id) or ["E1"]
    if period_statistics is None:
        period_statistics = bool(entry.options.get(CONF_PERIOD_STATS))
    if supply_mode is None:
        supply_mode = supply_mode_of(entry.options)
    rows = 0
    for suffix in suffixes:
        rows += await recalculate(
            hass,
            data[CONF_NMI],
            name,
            suffix,
            tariff,
            from_day,
            period_statistics=period_statistics,
            supply_mode=supply_mode,
        )
    return rows
