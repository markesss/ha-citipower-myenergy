"""The Refresh data button: an immediate poll on demand.

The integration is set up for real (all platforms) with the portal client, the session store
and the polling replaced, so the button is the only thing under test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.citipower_myenergy.const import DOMAIN
from custom_components.citipower_myenergy.coordinator import MyEnergyCoordinator, MyEnergyData
from custom_components.citipower_myenergy.exceptions import MyEnergyAuthError, MyEnergyError
from custom_components.citipower_myenergy.models import AccountInfo

NMI = "6001234567"
PACKAGE = "custom_components.citipower_myenergy"
ACCOUNT = AccountInfo(
    company="Citipower", start=date(2026, 10, 1), end=date(2026, 10, 21), has_access=True
)


@pytest.fixture
def poll() -> Iterator[AsyncMock]:
    """Replace everything that talks to the portal; yield the mock standing in for one poll."""
    update = AsyncMock(return_value=MyEnergyData(account=ACCOUNT))
    with (
        patch(f"{PACKAGE}.MyEnergyApi"),
        patch(f"{PACKAGE}.MyEnergyAuraClient"),
        patch(f"{PACKAGE}.async_create_session", new=AsyncMock()),
        patch(f"{PACKAGE}.SessionStore") as store,
        patch.object(MyEnergyCoordinator, "_async_update_data", update),
        patch.object(MyEnergyCoordinator, "async_start_keepalive"),
    ):
        store.return_value.async_load = AsyncMock()
        store.return_value.cookies.return_value = {"sid": "not-a-real-session"}
        store.return_value.context.return_value = None
        yield update


@pytest.fixture
def portal() -> Iterator[MagicMock]:
    """Replace only the portal client, so the coordinator's own poll logic really runs.

    Yields the API mock. ``async_account_info`` is the first call of every poll, so making it
    raise is how a network failure or an ended session is staged.
    """
    with (
        patch(f"{PACKAGE}.MyEnergyApi") as api_class,
        patch(f"{PACKAGE}.MyEnergyAuraClient"),
        patch(f"{PACKAGE}.async_create_session", new=AsyncMock()),
        patch(f"{PACKAGE}.SessionStore") as store,
        patch.object(MyEnergyCoordinator, "async_start_keepalive"),
    ):
        api = api_class.return_value
        api.async_account_info = AsyncMock(return_value=ACCOUNT)
        api.async_meter_data = AsyncMock(return_value={})
        api.client = MagicMock(aura={}, cookies={})
        store.return_value.async_load = AsyncMock()
        store.return_value.async_save_suffixes = AsyncMock()
        store.return_value.async_save_session = AsyncMock()
        store.return_value.suffixes.return_value = []
        store.return_value.cookies.return_value = {"sid": "not-a-real-session"}
        store.return_value.context.return_value = None
        yield api


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NMI,
        data={"nmi": NMI, "cookies": {"sid": "not-a-real-session"}},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _button(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{NMI}_refresh_data")
    assert entity_id is not None
    return entity_id


async def _press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def test_the_button_is_on_the_device(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, poll
) -> None:
    await _setup(hass)
    entity = er.async_get(hass).async_get(_button(hass))
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, NMI)})
    assert device is not None
    assert entity.device_id == device.id
    assert hass.states.get(entity.entity_id).attributes["friendly_name"].endswith("Refresh data")


async def test_pressing_it_polls_once(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, poll
) -> None:
    await _setup(hass)
    before = poll.call_count
    await _press(hass, _button(hass))
    assert poll.call_count == before + 1


async def test_a_press_while_a_poll_is_running_does_not_start_another(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, poll
) -> None:
    await _setup(hass)
    entity_id = _button(hass)
    before = poll.call_count
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_poll() -> MyEnergyData:
        started.set()
        await release.wait()
        return MyEnergyData(account=ACCOUNT)

    poll.side_effect = slow_poll
    first = hass.async_create_task(_press(hass, entity_id))
    await started.wait()
    await _press(hass, entity_id)  # the poll from the first press is still running
    await _press(hass, entity_id)
    assert poll.call_count == before + 1
    release.set()
    await first
    assert poll.call_count == before + 1

    poll.side_effect = None  # and once it has finished, a press polls again
    await _press(hass, entity_id)
    assert poll.call_count == before + 2


async def test_a_failed_poll_shows_as_a_failed_press_and_can_be_retried(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, poll
) -> None:
    await _setup(hass)
    entity_id = _button(hass)
    poll.side_effect = UpdateFailed("the portal is down")
    with pytest.raises(HomeAssistantError, match="the portal is down.*try again"):
        await _press(hass, entity_id)
    # The sensors go unavailable with the failed poll; the button stays pressable.
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE

    poll.side_effect = None
    await _press(hass, entity_id)  # the retry works and raises nothing


async def test_a_network_failure_says_what_failed_without_leaking_the_url_query(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal
) -> None:
    entry = await _setup(hass)
    entity_id = _button(hass)
    portal.async_account_info.side_effect = MyEnergyError(
        "Cannot reach https://portal.example/s/page?sid=SECRET123&x=1 (timed out)"
    )
    with pytest.raises(HomeAssistantError) as raised:
        await _press(hass, entity_id)
    message = str(raised.value)
    assert "poll failed" in message
    assert "timed out" in message
    assert "SECRET123" not in message
    assert "try again" in message
    assert not entry.runtime_data.last_update_success
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE  # still pressable


async def test_an_ended_session_starts_sign_in_and_says_so(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal
) -> None:
    entry = await _setup(hass)
    entity_id = _button(hass)
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    portal.async_account_info.side_effect = MyEnergyAuthError("session ended")
    with pytest.raises(HomeAssistantError, match="session has ended.*Sign in again"):
        await _press(hass, entity_id)
    await hass.async_block_till_done()

    assert entry.runtime_data.session_dead
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
    # Never greyed out, even with the session dead.
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_a_press_after_a_failure_leaves_the_coordinator_healthy(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, portal
) -> None:
    entry = await _setup(hass)
    entity_id = _button(hass)
    coordinator = entry.runtime_data

    portal.async_account_info.side_effect = MyEnergyAuthError("session ended")
    with pytest.raises(HomeAssistantError):
        await _press(hass, entity_id)
    assert coordinator.session_dead
    assert not coordinator.last_update_success

    portal.async_account_info.side_effect = None  # the connection or session is back
    await _press(hass, entity_id)  # raises nothing
    assert coordinator.last_update_success
    assert not coordinator.session_dead  # cleared the way a scheduled poll clears it
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_unloading_removes_the_button(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, poll
) -> None:
    entry = await _setup(hass)
    entity_id = _button(hass)
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
