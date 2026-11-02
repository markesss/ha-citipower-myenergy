"""Keep the portal session across Home Assistant restarts.

A restart builds a fresh cookie jar. The cookies from sign-in are in the config
entry, but Salesforce can rotate some of them while the session is alive, and
the Aura context (framework id and CSRF token) is only obtainable by loading a
page. Keeping the latest of both here means a restart carries on without
loading a page first, which is the request most likely to be turned away.

Each record is tied to the sign-in it came from, so a re-authentication never
resumes from a session that has already ended.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import SESSION_STORAGE_KEY, STORAGE_VERSION
from .portal import AuraContext


class SessionStore:
    """Persist cookies, Aura context and known channels per config entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Create the store."""
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, SESSION_STORAGE_KEY)
        self._data: dict[str, Any] = {}

    async def async_load(self) -> None:
        """Read the store from disk."""
        self._data = await self._store.async_load() or {}

    def _record(self, entry_id: str, signed_in_at: str | None) -> dict[str, Any]:
        record = self._data.get(entry_id) or {}
        if record.get("signed_in_at") != signed_in_at:
            return {}
        return record

    def cookies(self, entry_id: str, signed_in_at: str | None) -> dict[str, str] | None:
        """Return the newest cookies kept for this sign-in."""
        cookies = self._record(entry_id, signed_in_at).get("cookies")
        return cookies if isinstance(cookies, dict) and cookies else None

    def context(self, entry_id: str, signed_in_at: str | None) -> AuraContext | None:
        """Return the newest Aura context kept for this sign-in."""
        return AuraContext.from_dict(self._record(entry_id, signed_in_at).get("context"))

    def suffixes(self, entry_id: str) -> list[str]:
        """Return the NEM12 channels seen for this entry."""
        return list((self._data.get(entry_id) or {}).get("suffixes") or [])

    async def async_save_session(
        self,
        entry_id: str,
        signed_in_at: str | None,
        context: AuraContext | None,
        cookies: dict[str, str],
    ) -> None:
        """Remember the session as it is now."""
        record = dict(self._data.get(entry_id) or {})
        record.update(
            signed_in_at=signed_in_at,
            context=context.as_dict() if context else None,
            cookies=cookies,
        )
        if record != self._data.get(entry_id):
            self._data[entry_id] = record
            await self._store.async_save(self._data)

    async def async_save_suffixes(self, entry_id: str, suffixes: list[str]) -> None:
        """Remember which channels this NMI has."""
        record = dict(self._data.get(entry_id) or {})
        merged = sorted(set(record.get("suffixes") or []) | set(suffixes))
        if merged != record.get("suffixes"):
            record["suffixes"] = merged
            self._data[entry_id] = record
            await self._store.async_save(self._data)

    async def async_forget(self, entry_id: str) -> None:
        """Drop everything kept for an entry."""
        if self._data.pop(entry_id, None) is not None:
            await self._store.async_save(self._data)
