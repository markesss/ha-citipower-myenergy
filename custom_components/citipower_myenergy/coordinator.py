"""Poll myEnergy, import readings as statistics, and keep the session alive."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import MyEnergyApi
from .const import (
    CONF_KEEPALIVE_MINUTES,
    DEFAULT_KEEPALIVE_MINUTES,
    DOMAIN,
    KEEPALIVE_RETRY,
    MARKET_TZ,
    MAX_HISTORY_DAYS,
    REFRESH_WINDOW_DAYS,
    UPDATE_INTERVAL,
)
from .exceptions import MyEnergyAuthError, MyEnergyError
from .models import AccountInfo
from .nem12 import daily_total
from .session_store import SessionStore
from .statistics import async_insert_channel, async_last_stat_start, statistic_id_for

_LOGGER = logging.getLogger(__name__)

type MyEnergyConfigEntry = ConfigEntry[MyEnergyCoordinator]


@dataclass(slots=True)
class ChannelSummary:
    """The latest day's figures for one channel."""

    suffix: str
    meter_serial: str
    last_day: date | None
    last_day_kwh: float | None
    statistic_id: str


@dataclass(slots=True)
class MyEnergyData:
    """What the sensors show."""

    account: AccountInfo
    channels: dict[str, ChannelSummary] = field(default_factory=dict)
    hours_added: int = 0
    last_fetch: datetime | None = None


class MyEnergyCoordinator(DataUpdateCoordinator[MyEnergyData]):
    """Fetch interval data, record it as statistics, and hold the session open."""

    config_entry: MyEnergyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: MyEnergyConfigEntry,
        api: MyEnergyApi,
        store: SessionStore,
        nmi: str,
        name: str,
        signed_in_at: str | None,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )
        self.api = api
        self.nmi = nmi
        self.site_name = name
        self._store = store
        self._signed_in_at = signed_in_at
        self._cancel_keepalive: Callable[[], None] | None = None
        self._last_contact: datetime | None = None
        self.last_keepalive: datetime | None = None
        self.session_seconds: int | None = None
        self.session_dead = False
        self.session_dead_since: datetime | None = None

        # The statistics import is the point of the integration, so polling
        # must continue even when no entity is listening.
        self.async_add_listener(lambda: None)

    @property
    def signed_in_at(self) -> datetime | None:
        """Return when the current session was signed in."""
        return dt_util.parse_datetime(self._signed_in_at or "")

    # --- Polling ----------------------------------------------------------------

    async def _async_update_data(self) -> MyEnergyData:
        try:
            account = await self.api.async_account_info(self.nmi)
            self._touched()
            if not account.has_access:
                raise UpdateFailed("The portal says this login has no access to the NMI's data")

            end = account.end or (dt_util.now(MARKET_TZ).date() - timedelta(days=1))
            start = await self._async_fetch_start(account, end)
            data = MyEnergyData(account=account, last_fetch=dt_util.utcnow())
            if start > end:
                data.channels = self.data.channels if self.data else {}
                return data

            channels = await self.api.async_meter_data(self.nmi, account.company, start, end)
            self._touched()
        except MyEnergyAuthError as err:
            self._mark_dead()
            raise ConfigEntryAuthFailed(str(err)) from err
        except MyEnergyError as err:
            raise UpdateFailed(str(err)) from err

        for suffix, channel in channels.items():
            data.hours_added += await async_insert_channel(
                self.hass, self.nmi, self.site_name, channel
            )
            last_day = channel.last_day
            data.channels[suffix] = ChannelSummary(
                suffix=suffix,
                meter_serial=channel.meter_serial,
                last_day=last_day,
                last_day_kwh=daily_total(channel, last_day) if last_day else None,
                statistic_id=statistic_id_for(self.nmi, suffix),
            )
        await self._store.async_save_suffixes(self.config_entry.entry_id, list(channels))
        await self._async_save_session()
        return data

    async def _async_fetch_start(self, account: AccountInfo, end: date) -> date:
        """Decide how far back this poll needs to read."""
        earliest = end - timedelta(days=MAX_HISTORY_DAYS)
        if account.start and account.start > earliest:
            earliest = account.start

        suffixes = self._store.suffixes(self.config_entry.entry_id) or ["E1"]
        newest_per_channel = [
            await async_last_stat_start(self.hass, statistic_id_for(self.nmi, suffix))
            for suffix in suffixes
        ]
        if any(stamp is None for stamp in newest_per_channel):
            _LOGGER.info(
                "Importing myEnergy history for %s from %s to %s", self.site_name, earliest, end
            )
            return earliest
        oldest = min(stamp for stamp in newest_per_channel if stamp is not None)
        start = oldest.astimezone(MARKET_TZ).date() - timedelta(days=REFRESH_WINDOW_DAYS)
        return max(start, earliest)

    # --- Session ----------------------------------------------------------------

    def _touched(self) -> None:
        """Note a successful request; any request resets the idle clock."""
        self._last_contact = dt_util.utcnow()
        if self.session_dead:
            self.session_dead = False
            self.session_dead_since = None

    def _mark_dead(self) -> None:
        if not self.session_dead:
            self.session_dead = True
            self.session_dead_since = dt_util.utcnow()
        self.async_stop_keepalive()

    async def _async_save_session(self) -> None:
        client = self.api.client
        await self._store.async_save_session(
            self.config_entry.entry_id, self._signed_in_at, client.aura, client.cookies
        )

    @property
    def keepalive_interval(self) -> timedelta:
        """Return the configured gap between keep-alive requests."""
        minutes = self.config_entry.options.get(CONF_KEEPALIVE_MINUTES, DEFAULT_KEEPALIVE_MINUTES)
        return timedelta(minutes=int(minutes))

    @callback
    def async_start_keepalive(self) -> None:
        """Start pinging the portal so the session never idles out."""
        if self._cancel_keepalive is None and not self.session_dead:
            self._async_schedule_keepalive(None)

    @callback
    def async_stop_keepalive(self) -> None:
        """Stop the keep-alive loop."""
        if self._cancel_keepalive is not None:
            self._cancel_keepalive()
            self._cancel_keepalive = None

    @callback
    def _async_schedule_keepalive(self, delay: timedelta | None) -> None:
        if delay is None:
            # A little jitter keeps the request pattern from looking robotic.
            base = self.keepalive_interval.total_seconds()
            seconds = base * random.uniform(0.85, 1.0)
        else:
            seconds = max(delay.total_seconds(), 1)
        self._cancel_keepalive = async_call_later(self.hass, seconds, self._async_keepalive)

    async def _async_keepalive(self, _now: datetime) -> None:
        """Touch the portal; on a dead session, ask the user to sign in again."""
        self._cancel_keepalive = None
        reschedule: timedelta | None = None
        try:
            idle = dt_util.utcnow() - (self._last_contact or dt_util.utcnow())
            if self._last_contact is not None and idle < self.keepalive_interval * 0.8:
                # A poll just did this job; wait out only what is still owed.
                reschedule = self.keepalive_interval - idle
                return
            try:
                await self.api.async_account_info(self.nmi)
            except MyEnergyAuthError:
                _LOGGER.warning(
                    "The myEnergy session for %s has ended; sign in again to resume",
                    self.site_name,
                )
                self._mark_dead()
                self.config_entry.async_start_reauth(self.hass)
                self.async_update_listeners()
                return
            except MyEnergyError as err:
                _LOGGER.debug("Keep-alive request failed, retrying shortly: %s", err)
                reschedule = KEEPALIVE_RETRY
                return

            self._touched()
            self.last_keepalive = dt_util.utcnow()
            self.session_seconds = await self.api.async_session_seconds()
            _LOGGER.debug("Keep-alive ok; portal reports %ss left", self.session_seconds)
            await self._async_save_session()
            self.async_update_listeners()
        except Exception:
            _LOGGER.exception("Keep-alive failed unexpectedly; retrying shortly")
            reschedule = KEEPALIVE_RETRY
        finally:
            if not self.session_dead:
                self._async_schedule_keepalive(reschedule)
