"""Plain data models and response parsers for the myEnergy portal.

No I/O and no Home Assistant imports, so these can be tested on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from .const import MAX_CHUNK_DAYS
from .exceptions import MyEnergyApiError
from .nem12 import Channel, parse_nem12
from .portal import unwrap_result


@dataclass(frozen=True, slots=True)
class NmiInfo:
    """An NMI the account can see."""

    assign_id: str
    nmi_id: str
    nmi: str
    description: str
    address: str
    active: bool

    @property
    def label(self) -> str:
        """Return a human-readable label for choosing between NMIs."""
        name = self.description or "NMI"
        return f"{name} – {self.address or self.nmi} ({self.nmi})"


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """What the portal says about an NMI's available data."""

    company: str
    start: date | None
    end: date | None
    has_access: bool


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def parse_nmi_list(payload: Any) -> list[NmiInfo]:
    """Turn getEnergyUsageDetailWrapper's result into NMIs."""
    data = unwrap_result(payload, "the NMI list")
    if not isinstance(data, dict):
        raise MyEnergyApiError("Unexpected NMI list from the portal")
    found: list[NmiInfo] = []
    for item in data.get("nmiAssignmentsForEnergyUsage") or []:
        nmi = item.get("NMI__r") or {}
        if not nmi.get("Name"):
            continue
        found.append(
            NmiInfo(
                assign_id=item.get("Id", ""),
                nmi_id=nmi.get("Id") or item.get("NMI__c", ""),
                nmi=nmi["Name"],
                description=item.get("Description__c") or "",
                address=nmi.get("Display_Address__c") or "",
                active=(item.get("Status__c") or "Active") == "Active",
            )
        )
    return found


def parse_account_info(payload: Any) -> AccountInfo:
    """Turn retrieveAccountHolderInfo's result into AccountInfo."""
    data = unwrap_result(payload, "the account details")
    if not isinstance(data, dict):
        raise MyEnergyApiError("Unexpected account details from the portal")
    if data.get("errorMessage"):
        raise MyEnergyApiError(f"The portal reported: {data['errorMessage']}")
    return AccountInfo(
        company=data.get("companyName") or "",
        start=_parse_date(data.get("startDate")),
        end=_parse_date(data.get("endDate")),
        has_access=bool(data.get("hasReportAccess", True)),
    )


def parse_meter_data(payload: Any) -> dict[str, Channel]:
    """Turn getMeterData's result into parsed NEM12 channels."""
    data = unwrap_result(payload, "meter data")
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        raise MyEnergyApiError("Unexpected meter data from the portal")
    status = str(body.get("status") or "Success")
    if status.lower() not in ("success", "completed", "complete"):
        raise MyEnergyApiError(f"The portal's meter data request ended with status {status}")
    return parse_nem12(body.get("nmiData") or "")


def date_chunks(start: date, end: date, days: int = MAX_CHUNK_DAYS) -> list[tuple[date, date]]:
    """Split an inclusive date range into inclusive chunks, newest first."""
    chunks: list[tuple[date, date]] = []
    chunk_end = end
    while chunk_end >= start:
        chunk_start = max(start, chunk_end - timedelta(days=days - 1))
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start - timedelta(days=1)
    return chunks
