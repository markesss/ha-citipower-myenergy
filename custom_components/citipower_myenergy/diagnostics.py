"""Diagnostics for myEnergy, with the session and personal details redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADDRESS,
    CONF_COOKIES,
    CONF_NMI,
    CONF_NMI_ASSIGN_ID,
    CONF_NMI_ID,
)
from .coordinator import MyEnergyConfigEntry

TO_REDACT = {CONF_COOKIES, CONF_ADDRESS, CONF_NMI, CONF_NMI_ID, CONF_NMI_ASSIGN_ID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MyEnergyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "session": {
            "dead": coordinator.session_dead,
            "signed_in_at": str(coordinator.signed_in_at),
            "last_keepalive": str(coordinator.last_keepalive),
            "seconds_remaining": coordinator.session_seconds,
            "cookie_names": sorted(coordinator.api.client.cookies),
            "has_context": coordinator.api.client.aura is not None,
        },
        "account": {
            "company": data.account.company if data else None,
            "start": str(data.account.start) if data else None,
            "end": str(data.account.end) if data else None,
        },
        "channels": {
            suffix: {
                "last_day": str(channel.last_day),
                "last_day_kwh": channel.last_day_kwh,
            }
            for suffix, channel in (data.channels.items() if data else [])
        },
        "hours_added_last_fetch": data.hours_added if data else None,
    }
