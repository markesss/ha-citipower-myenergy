"""A button that polls the portal for new data straight away.

The poll is the same one that runs on the schedule (readings, cost and per-period statistics),
so it takes the same statistics write lock and nothing here writes anything itself. The portal
publishes each day's readings the following morning (about 1am), so pressing it earlier will
usually find nothing new. It is for testing, and for recovery: after a scheduled refresh fails
(no network, the portal down, the session ended) it is the way to try again from the UI.
"""

from __future__ import annotations

import asyncio
import re

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import MyEnergyConfigEntry, MyEnergyCoordinator
from .sensor import MyEnergyEntity

_QUERY = re.compile(r"\?\S*")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MyEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button."""
    async_add_entities([RefreshDataButton(entry.runtime_data)])


def _plain(reason: object) -> str:
    """Return an error's text without any URL query (they can carry session values)."""
    return _QUERY.sub("", str(reason) or "unknown error")


class RefreshDataButton(MyEnergyEntity, ButtonEntity):
    """Poll the portal for new data now."""

    _attr_translation_key = "refresh_data"

    def __init__(self, coordinator: MyEnergyCoordinator) -> None:
        """Create the button (the unique id follows the sensors': the NMI and a key)."""
        super().__init__(coordinator, "refresh_data")
        self._polling = asyncio.Lock()

    @property
    def available(self) -> bool:
        """Always available while the entry is loaded.

        ``CoordinatorEntity`` would grey the button out whenever the last poll failed, or the
        session had ended, which is exactly when someone needs it: after a failure the press is
        the retry, and after the session ends the press runs the poll that starts sign-in.
        """
        return True

    async def async_press(self) -> None:
        """Run a poll now. A failed poll raises, so the press shows as a failure in the UI."""
        if self._polling.locked():
            return  # a press while the poll it started is still running adds nothing
        async with self._polling:
            # ``async_refresh`` keeps the failure on the coordinator rather than raising it,
            # and for an ended session also starts the sign-in (reauth) flow.
            await self.coordinator.async_refresh()
            if self.coordinator.last_update_success:
                return
            reason = self.coordinator.last_exception
            if isinstance(reason, ConfigEntryAuthFailed):
                raise HomeAssistantError(
                    "The myEnergy session has ended. Sign in again from the notification or "
                    "from Settings > Devices & services, then press this button again."
                )
            raise HomeAssistantError(
                f"The myEnergy poll failed: {_plain(reason)}. Press the button to try again."
            )
