"""Entry lifecycle: zones load off the event loop; the HTTP session closes on unload."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.citipower_myenergy import tariff as tariff_module
from custom_components.citipower_myenergy.const import DOMAIN
from custom_components.citipower_myenergy.coordinator import MyEnergyCoordinator, MyEnergyData
from custom_components.citipower_myenergy.models import AccountInfo
from custom_components.citipower_myenergy.tls import async_create_session

NMI = "6001234567"
PACKAGE = "custom_components.citipower_myenergy"
ACCOUNT = AccountInfo(
    company="Citipower", start=date(2026, 10, 1), end=date(2026, 10, 21), has_access=True
)


@pytest.fixture
def session() -> Iterator[MagicMock]:
    """Stand in for the portal; yield the session the entry is given."""
    fake = MagicMock(close=AsyncMock())
    with (
        patch(f"{PACKAGE}.MyEnergyApi"),
        patch(f"{PACKAGE}.MyEnergyAuraClient"),
        patch(f"{PACKAGE}.async_create_session", new=AsyncMock(return_value=fake)),
        patch(f"{PACKAGE}.get_session_store") as store,
        patch.object(
            MyEnergyCoordinator,
            "_async_update_data",
            AsyncMock(return_value=MyEnergyData(account=ACCOUNT)),
        ),
        patch.object(MyEnergyCoordinator, "async_start_keepalive"),
    ):
        store.return_value.async_load = AsyncMock()
        store.return_value.cookies.return_value = {"sid": "not-a-real-session"}
        store.return_value.context.return_value = None
        yield fake


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=NMI, data={"nmi": NMI, "cookies": {"s": "x"}})
    entry.add_to_hass(hass)
    return entry


async def test_setup_primes_the_zones_in_an_executor_before_anything_uses_them(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, session
) -> None:
    tariff_module.zone_for.cache_clear()
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Every allowed zone is cached, so no later zone_for call reads the database on the loop.
    misses = tariff_module.zone_for.cache_info().misses
    for name in tariff_module.ALLOWED_TIMEZONES:
        tariff_module.zone_for(name)
    assert tariff_module.zone_for.cache_info().misses == misses


async def test_unloading_closes_the_session(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, session
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    session.close.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    session.close.assert_awaited_once()


async def test_each_reload_closes_the_session_it_opened(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, session
) -> None:
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert session.close.await_count == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert session.close.await_count == 2


async def test_shutdown_closes_open_sessions_with_one_listener(hass: HomeAssistant) -> None:
    before = hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_CLOSE, 0)
    sessions = [await async_create_session(hass) for _ in range(3)]
    # One shutdown listener in total, however many sessions are created.
    assert hass.bus.async_listeners().get(EVENT_HOMEASSISTANT_CLOSE, 0) <= before + 1
    await sessions[0].close()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_CLOSE)
    await hass.async_block_till_done()
    assert all(s.closed for s in sessions)
