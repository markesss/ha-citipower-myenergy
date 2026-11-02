"""The CitiPower & Powercor myEnergy integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import MyEnergyApi
from .aura import MyEnergyAuraClient
from .const import (
    CONF_ADDRESS,
    CONF_COOKIES,
    CONF_DESCRIPTION,
    CONF_NMI,
    CONF_SIGNED_IN_AT,
)
from .coordinator import MyEnergyConfigEntry, MyEnergyCoordinator
from .session_store import SessionStore

PLATFORMS: list[Platform] = [Platform.SENSOR]


def site_name(data: dict) -> str:
    """Return the name used for statistics and the device."""
    return data.get(CONF_DESCRIPTION) or data.get(CONF_ADDRESS) or data[CONF_NMI]


async def async_setup_entry(hass: HomeAssistant, entry: MyEnergyConfigEntry) -> bool:
    """Set up myEnergy from a config entry."""
    store = SessionStore(hass)
    await store.async_load()
    signed_in_at = entry.data.get(CONF_SIGNED_IN_AT)

    async def _remember(context, cookies) -> None:
        await store.async_save_session(entry.entry_id, signed_in_at, context, cookies)

    # A dedicated session keeps the portal's cookies out of the shared jar.
    client = MyEnergyAuraClient(
        async_create_clientsession(hass),
        store.cookies(entry.entry_id, signed_in_at) or entry.data[CONF_COOKIES],
        context=store.context(entry.entry_id, signed_in_at),
        on_change=_remember,
    )
    coordinator = MyEnergyCoordinator(
        hass,
        entry,
        MyEnergyApi(client),
        store,
        nmi=entry.data[CONF_NMI],
        name=site_name(entry.data),
        signed_in_at=signed_in_at,
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_stop_keepalive)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    coordinator.async_start_keepalive()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MyEnergyConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: MyEnergyConfigEntry) -> None:
    """Reload when options change, so a new keep-alive interval applies."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: MyEnergyConfigEntry) -> None:
    """Forget the kept session when an entry is removed."""
    store = SessionStore(hass)
    await store.async_load()
    await store.async_forget(entry.entry_id)
