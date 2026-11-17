"""The session store, and that setting up an entry loads, uses and forgets it correctly.

Setup runs for real with the portal client, API and polling replaced, so what is under test is
which cookies and Aura context the client is built with, and which sensors the entry creates.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.citipower_myenergy.const import (
    DOMAIN,
    MARKET_TZ,
    SESSION_STORAGE_KEY,
    STORAGE_VERSION,
)
from custom_components.citipower_myenergy.models import AccountInfo
from custom_components.citipower_myenergy.nem12 import Channel
from custom_components.citipower_myenergy.portal import AuraContext
from custom_components.citipower_myenergy.session_store import SessionStore

NMI = "6001234567"
PACKAGE = "custom_components.citipower_myenergy"
SIGNED_IN = "2026-11-16T01:00:00+00:00"
LATER_SIGN_IN = "2026-11-16T09:00:00+00:00"
CONTEXT = AuraContext(context={"fwuid": "stored-fwuid", "app": "siteforce:communityApp"}, token="t")
COOKIES = {"sid": "stored-session", "sid_Client": "paired"}
ENTRY_COOKIES = {"sid": "entry-session"}


# --- SessionStore on its own ---------------------------------------------------------------


async def test_cookies_and_context_round_trip_through_storage(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    store = SessionStore(hass)
    await store.async_save_session("entry-1", SIGNED_IN, CONTEXT, COOKIES)

    # A restart: a brand new store reads what the first one wrote.
    reloaded = SessionStore(hass)
    await reloaded.async_load()
    assert reloaded.cookies("entry-1", SIGNED_IN) == COOKIES
    assert reloaded.context("entry-1", SIGNED_IN) == CONTEXT

    saved = hass_storage[SESSION_STORAGE_KEY]
    assert saved["version"] == STORAGE_VERSION
    assert saved["data"]["entry-1"]["signed_in_at"] == SIGNED_IN


async def test_records_from_a_different_sign_in_are_ignored(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """After a reauth the old session is dead; it must never be resumed."""
    store = SessionStore(hass)
    await store.async_save_session("entry-1", SIGNED_IN, CONTEXT, COOKIES)

    assert store.cookies("entry-1", LATER_SIGN_IN) is None
    assert store.context("entry-1", LATER_SIGN_IN) is None
    assert store.cookies("entry-1", None) is None  # an entry with no sign-in time either
    assert store.cookies("another-entry", SIGNED_IN) is None
    assert store.cookies("entry-1", SIGNED_IN) == COOKIES  # the matching one is untouched


async def test_a_new_sign_in_replaces_the_session_but_keeps_the_channels(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    store = SessionStore(hass)
    await store.async_save_suffixes("entry-1", ["E1"])
    await store.async_save_session("entry-1", SIGNED_IN, CONTEXT, COOKIES)
    new_context = AuraContext(context={"fwuid": "new"}, token="n")
    await store.async_save_session("entry-1", LATER_SIGN_IN, new_context, {"sid": "new-session"})

    assert store.cookies("entry-1", SIGNED_IN) is None
    assert store.cookies("entry-1", LATER_SIGN_IN) == {"sid": "new-session"}
    assert store.context("entry-1", LATER_SIGN_IN) == new_context
    assert store.suffixes("entry-1") == ["E1"]  # channels belong to the NMI, not the sign-in


async def test_channels_accumulate_and_survive_a_restart(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    store = SessionStore(hass)
    await store.async_save_suffixes("entry-1", ["E1"])
    await store.async_save_suffixes("entry-1", ["B1", "E1"])

    reloaded = SessionStore(hass)
    await reloaded.async_load()
    assert reloaded.suffixes("entry-1") == ["B1", "E1"]
    assert reloaded.suffixes("another-entry") == []


@pytest.mark.parametrize(
    ("stored", "cookies"),
    [
        ({"signed_in_at": SIGNED_IN, "cookies": {}, "context": None}, None),
        ({"signed_in_at": SIGNED_IN, "cookies": "no", "context": "no"}, None),
        (
            {
                "signed_in_at": SIGNED_IN,
                "cookies": COOKIES,
                "context": {"context": {}, "token": ""},
            },
            COOKIES,
        ),
    ],
    ids=["empty", "wrong-types", "empty-token"],
)
async def test_unusable_records_read_as_nothing(
    hass: HomeAssistant, hass_storage: dict, stored: dict, cookies: dict | None
) -> None:
    hass_storage[SESSION_STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": SESSION_STORAGE_KEY,
        "data": {"entry-1": stored},
    }
    store = SessionStore(hass)
    await store.async_load()

    assert store.cookies("entry-1", SIGNED_IN) == cookies
    assert store.context("entry-1", SIGNED_IN) is None


async def test_forgetting_an_entry_leaves_the_others(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    store = SessionStore(hass)
    await store.async_save_session("entry-1", SIGNED_IN, CONTEXT, COOKIES)
    await store.async_save_session("entry-2", SIGNED_IN, CONTEXT, COOKIES)
    await store.async_forget("entry-1")

    assert store.cookies("entry-1", SIGNED_IN) is None
    assert store.cookies("entry-2", SIGNED_IN) == COOKIES
    assert "entry-1" not in hass_storage[SESSION_STORAGE_KEY]["data"]


# --- Setting up an entry -------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[MagicMock]:
    """Replace the portal; yield the client class so its constructor arguments can be read."""
    channel = Channel(nmi=NMI, suffix="E1", meter_serial="M1", unit="KWH", interval_minutes=5)
    start = datetime(2026, 10, 20, tzinfo=MARKET_TZ)
    for index in range(48):
        channel.hours[start + timedelta(hours=index)] = 1.0
    channel.days = {date(2026, 10, 20), date(2026, 10, 21)}
    with (
        patch(f"{PACKAGE}.MyEnergyApi") as api_class,
        patch(f"{PACKAGE}.MyEnergyAuraClient") as client_class,
        patch(f"{PACKAGE}.async_create_session", new=AsyncMock(return_value=AsyncMock())),
        patch(f"{PACKAGE}.coordinator.MyEnergyCoordinator.async_start_keepalive"),
    ):
        api = api_class.return_value
        api.async_account_info = AsyncMock(
            return_value=AccountInfo(
                company="Citipower",
                start=date(2026, 10, 1),
                end=date(2026, 10, 21),
                has_access=True,
            )
        )
        api.async_meter_data = AsyncMock(return_value={"E1": channel})
        client_class.return_value.aura = CONTEXT
        client_class.return_value.cookies = COOKIES
        api.client = client_class.return_value
        yield client_class


def _entry(hass: HomeAssistant, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=NMI,
        data={"nmi": NMI, "description": "Home", "cookies": ENTRY_COOKIES, **data},
    )
    entry.add_to_hass(hass)
    return entry


def _seed(hass_storage: dict, entry: MockConfigEntry, signed_in_at: str | None) -> None:
    hass_storage[SESSION_STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "key": SESSION_STORAGE_KEY,
        "data": {
            entry.entry_id: {
                "signed_in_at": signed_in_at,
                "cookies": COOKIES,
                "context": CONTEXT.as_dict(),
            }
        },
    }


async def test_an_entry_loads_and_its_sensors_appear(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, client, hass_storage
) -> None:
    entry = _entry(hass, signed_in_at=SIGNED_IN)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    for key in ("latest_data", "session", "latest_day_e1"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{NMI}_{key}")
        assert entity_id is not None, key
        assert hass.states.get(entity_id).state not in ("unavailable", "unknown"), key

    session = hass.states.get(registry.async_get_entity_id("sensor", DOMAIN, f"{NMI}_session"))
    assert session.state == "active"
    assert session.attributes["signed_in_at"] == SIGNED_IN
    latest_day = hass.states.get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{NMI}_latest_day_e1")
    )
    assert float(latest_day.state) == pytest.approx(24.0)


async def test_setup_resumes_from_the_kept_session_for_the_same_sign_in(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, client, hass_storage
) -> None:
    entry = _entry(hass, signed_in_at=SIGNED_IN)
    _seed(hass_storage, entry, SIGNED_IN)
    assert await hass.config_entries.async_setup(entry.entry_id)

    args, kwargs = client.call_args
    assert args[1] == COOKIES  # the rotated cookies, not the ones saved at sign-in
    assert kwargs["context"] == CONTEXT  # so no page needs loading before the first call


async def test_setup_ignores_a_session_kept_for_a_different_sign_in(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, client, hass_storage
) -> None:
    entry = _entry(hass, signed_in_at=LATER_SIGN_IN)  # the user has signed in again since
    _seed(hass_storage, entry, SIGNED_IN)
    assert await hass.config_entries.async_setup(entry.entry_id)

    args, kwargs = client.call_args
    assert args[1] == ENTRY_COOKIES
    assert kwargs["context"] is None


async def test_setup_without_a_kept_session_uses_the_entry_cookies(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, client, hass_storage
) -> None:
    entry = _entry(hass, signed_in_at=SIGNED_IN)
    assert await hass.config_entries.async_setup(entry.entry_id)

    args, kwargs = client.call_args
    assert args[1] == ENTRY_COOKIES
    assert kwargs["context"] is None


async def test_the_first_poll_keeps_the_session_for_the_next_restart(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, client, hass_storage
) -> None:
    entry = _entry(hass, signed_in_at=SIGNED_IN)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    record = hass_storage[SESSION_STORAGE_KEY]["data"][entry.entry_id]
    assert record["signed_in_at"] == SIGNED_IN
    assert record["cookies"] == COOKIES
    assert record["context"] == CONTEXT.as_dict()
    assert record["suffixes"] == ["E1"]
    assert "stored-session" in json.dumps(record)  # it is a real record, not an empty shell


async def test_removing_the_entry_forgets_its_session(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, client, hass_storage
) -> None:
    entry = _entry(hass, signed_in_at=SIGNED_IN)
    other = MockConfigEntry(domain=DOMAIN, unique_id="other", data={"nmi": "other"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    store = SessionStore(hass)
    await store.async_load()
    await store.async_save_session(other.entry_id, SIGNED_IN, CONTEXT, COOKIES)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    kept = hass_storage[SESSION_STORAGE_KEY]["data"]
    assert entry.entry_id not in kept
    assert other.entry_id in kept
