"""Domain-level calls against the myEnergy portal."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from .aura import MyEnergyAuraClient
from .const import (
    APEX_ACCOUNT_INFO_METHOD,
    APEX_ENERGY_INFO_CLASS,
    APEX_METER_DATA_METHOD,
    APEX_NMI_LIST_METHOD,
    APEX_USAGE_PAGE_CLASS,
    METER_DATA_FORMAT,
    SESSION_TIME_URL,
)
from .models import (
    AccountInfo,
    NmiInfo,
    date_chunks,
    parse_account_info,
    parse_meter_data,
    parse_nmi_list,
)
from .nem12 import Channel, merge_channels
from .portal import session_seconds_remaining

_LOGGER = logging.getLogger(__name__)


class MyEnergyApi:
    """The handful of portal calls the integration needs."""

    def __init__(self, client: MyEnergyAuraClient) -> None:
        """Wrap an Aura client."""
        self._client = client

    @property
    def client(self) -> MyEnergyAuraClient:
        """Return the underlying client."""
        return self._client

    async def async_list_nmis(self, nmi_assign_id: str = "") -> list[NmiInfo]:
        """Return the NMIs this login can see."""
        params: dict[str, Any] = {"noOfLimit": 50}
        if nmi_assign_id:
            params["nmiAssignId"] = nmi_assign_id
        return parse_nmi_list(
            await self._client.async_invoke(APEX_USAGE_PAGE_CLASS, APEX_NMI_LIST_METHOD, params)
        )

    async def async_account_info(self, nmi: str) -> AccountInfo:
        """Return the distributor and the range of data available for an NMI."""
        return parse_account_info(
            await self._client.async_invoke(
                APEX_ENERGY_INFO_CLASS, APEX_ACCOUNT_INFO_METHOD, {"nmi": nmi}
            )
        )

    async def async_meter_data(
        self, nmi: str, company: str, start: date, end: date
    ) -> dict[str, Channel]:
        """Return interval data for an inclusive date range, fetched in chunks."""
        channels: dict[str, Channel] = {}
        # Oldest first, so newer chunks overwrite any overlapping day.
        for job_id, (chunk_start, chunk_end) in enumerate(reversed(date_chunks(start, end))):
            _LOGGER.debug("Fetching meter data %s to %s", chunk_start, chunk_end)
            payload = await self._client.async_invoke(
                APEX_ENERGY_INFO_CLASS,
                APEX_METER_DATA_METHOD,
                {
                    "nmi": nmi,
                    "formatType": METER_DATA_FORMAT,
                    "startDate": chunk_start.isoformat(),
                    "endDate": chunk_end.isoformat(),
                    "company": company,
                    "jobId": job_id,
                },
            )
            merge_channels(channels, parse_meter_data(payload))
        return channels

    async def async_session_seconds(self) -> int | None:
        """Return how long the portal says the session has left, if it says."""
        return session_seconds_remaining(await self._client.async_get_text(SESSION_TIME_URL))
