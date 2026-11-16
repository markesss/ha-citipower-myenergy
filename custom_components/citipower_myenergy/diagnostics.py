"""Diagnostics for myEnergy, with the session and personal details redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .chain_guard import guard_for
from .const import (
    CONF_ADDRESS,
    CONF_COOKIES,
    CONF_NMI,
    CONF_NMI_ASSIGN_ID,
    CONF_NMI_ID,
    CONF_PERIOD_STATS,
    CONF_TARIFF,
)
from .coordinator import MyEnergyConfigEntry
from .exceptions import TariffError
from .tariff import Tariff, period_slugs, supply_mode_of

TO_REDACT = {CONF_COOKIES, CONF_ADDRESS, CONF_NMI, CONF_NMI_ID, CONF_NMI_ASSIGN_ID}


def _cost_tracking(entry: MyEnergyConfigEntry) -> dict[str, Any]:
    """Summarise the tariff setup. Prices and period names are not secret; the NMI is not here."""
    raw = entry.options.get(CONF_TARIFF)
    summary: dict[str, Any] = {
        "tariff_configured": bool(raw),
        "period_statistics": bool(entry.options.get(CONF_PERIOD_STATS)),
    }
    if not raw:
        return summary
    try:
        tariff = Tariff.from_dict(raw)
    except TariffError as err:
        summary["tariff_error"] = str(err)
        return summary
    # A zone is a setting, not an identifier. Unset (fixed AEST) reads as Australia/Brisbane.
    summary["timezone"] = tariff.zone_name
    summary["timezone_set"] = any(version.timezone for version in tariff.versions)
    summary["supply_mode"] = supply_mode_of(entry.options)
    summary["versions"] = [
        {
            "effective_from": version.effective_from.isoformat(),
            "supply_c_per_day": version.supply_c_per_day,
            "discount_pct": version.discount_pct,
            "prices_include_gst": version.prices_include_gst,
            "import_periods": [period.name for period in version.import_periods],
            "export_periods": [period.name for period in version.export_periods],
        }
        for version in tariff.versions
    ]
    summary["period_slugs"] = list(period_slugs(tariff).values())
    return summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MyEnergyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "cost_tracking": _cost_tracking(entry),
        # The last writes and any statistic whose running total does not join. Statistics are
        # named without the NMI, so nothing here identifies the account.
        "statistics_writes": guard_for(hass, entry.data[CONF_NMI]).as_diagnostics(),
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
