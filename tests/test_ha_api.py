"""``MyEnergyApi`` over HTTP: each portal call, the meter-data chunking and the error mapping.

The Aura endpoint of a local server answers by Apex class and method with the anonymised captures
(``nmi_assignments.json``, ``account_info.json``, ``meter_data.json``, ``session_time.txt``).
The client is given a context up front, as it is after a restart, so only the call under test
reaches the server.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from custom_components.citipower_myenergy.api import MyEnergyApi
from custom_components.citipower_myenergy.aura import MyEnergyAuraClient
from custom_components.citipower_myenergy.exceptions import (
    MyEnergyApiError,
    MyEnergyAuthError,
)
from custom_components.citipower_myenergy.portal import AuraContext

FIXTURES = Path(__file__).parent / "fixtures"
CONTEXT = AuraContext(context={"fwuid": "fw", "app": "siteforce:communityApp"}, token="tok")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _nem12(day: str, value: float, suffix: str = "E1") -> str:
    """One day of 30-minute data, every interval the same, so a day's total is 48 * value."""
    values = ",".join([f"{value:.4f}"] * 48)
    return (
        "100,NEM12,202601010000,XXX1,\r\n"
        f"200,6102000000,{suffix},{suffix},{suffix},,SER1,KWH,30,\r\n"
        f"300,{day},{values},A,,,20260102000000\r\n"
        "900\r\n"
    )


def _meter_reply(nmi_data: str, status: str | None = None) -> str:
    body: dict[str, Any] = {"nmiData": nmi_data}
    if status is not None:
        body["status"] = status
    return json.dumps(
        {
            "actions": [
                {
                    "state": "SUCCESS",
                    "returnValue": {"returnValue": {"data": {"body": body}, "isSuccess": True}},
                }
            ]
        }
    )


class FakePortal:
    """Answers Aura calls by (class, method) and the session clock."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.replies: dict[str, list[tuple[int, str]]] = {}
        self.session_reply: tuple[int, str] = (200, _fixture("session_time.txt"))

    def reply_for(self, key: str) -> tuple[int, str]:
        queue = self.replies[key]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def aura(self, request: web.Request) -> web.Response:
        form = await request.post()
        params = json.loads(form["message"])["actions"][0]["params"]
        self.calls.append(params)
        status, body = self.reply_for(f"{params['classname']}.{params['method']}")
        return web.Response(status=status, text=body, content_type="application/json")

    async def usage_page(self, request: web.Request) -> web.Response:
        """Only reached when the client refreshes its context after a refusal."""
        page = _fixture("community_page.html")
        marker = '"eikoocnekot":"'
        start = page.index(marker) + len(marker)
        response = web.Response(text=page, content_type="text/html")
        response.set_cookie(page[start : page.index('"', start)], "fresh-token")
        return response

    async def session_time(self, request: web.Request) -> web.Response:
        status, body = self.session_reply
        return web.Response(status=status, text=body)

    def meter_calls(self) -> list[dict[str, Any]]:
        return [call["params"] for call in self.calls if call["method"] == "getMeterData"]


@pytest.fixture
async def portal(serve) -> FakePortal:
    fake = FakePortal()
    await serve(
        [
            ("POST", "/s/sfsites/aura", fake.aura),
            ("GET", "/s/energy-usage", fake.usage_page),
            ("GET", "/session", fake.session_time),
        ]
    )
    return fake


@pytest.fixture
async def api() -> AsyncIterator[MyEnergyApi]:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
        yield MyEnergyApi(MyEnergyAuraClient(session, {}, CONTEXT))


async def test_the_api_exposes_its_client(api: MyEnergyApi) -> None:
    assert isinstance(api.client, MyEnergyAuraClient)
    assert api.client.aura is CONTEXT


async def test_listing_nmis_asks_for_up_to_fifty_and_parses_the_capture(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    portal.replies["EnergyUsageDetailPageController.getEnergyUsageDetailWrapper"] = [
        (200, _fixture("nmi_assignments.json"))
    ]

    nmis = await api.async_list_nmis()

    assert portal.calls[0]["params"] == {"noOfLimit": 50}
    assert [nmi.nmi for nmi in nmis] == ["61020000001"]
    assert nmis[0].active


async def test_listing_nmis_can_be_narrowed_to_one_assignment(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    portal.replies["EnergyUsageDetailPageController.getEnergyUsageDetailWrapper"] = [
        (200, _fixture("nmi_assignments.json"))
    ]

    await api.async_list_nmis("a1BOY000001uGj32AE")

    assert portal.calls[0]["params"] == {"noOfLimit": 50, "nmiAssignId": "a1BOY000001uGj32AE"}


async def test_account_info_sends_the_nmi_and_reads_the_date_range(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    portal.replies["EnergyInformationController.retrieveAccountHolderInfo"] = [
        (200, _fixture("account_info.json"))
    ]

    info = await api.async_account_info("61020000001")

    assert portal.calls[0]["params"] == {"nmi": "61020000001"}
    assert info.company == "Citipower"
    assert (info.start, info.end) == (date(2025, 6, 18), date(2026, 10, 31))
    assert info.has_access


async def test_a_short_range_is_one_request_with_the_browsers_parameters(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    portal.replies["EnergyInformationController.getMeterData"] = [
        (200, _fixture("meter_data.json"))
    ]

    channels = await api.async_meter_data(
        "61020000001", "Citipower", date(2026, 10, 27), date(2026, 10, 31)
    )

    assert portal.meter_calls() == [
        {
            "nmi": "61020000001",
            "formatType": "Customer Access NEM12",
            "startDate": "2026-10-27",
            "endDate": "2026-10-31",
            "company": "Citipower",
            "jobId": 0,
        }
    ]
    assert list(channels) == ["E1"]
    assert len(channels["E1"].days) == 5


async def test_a_long_range_is_fetched_oldest_chunk_first_and_newer_data_wins(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    """91 days is two chunks (1 day and 90 days); the day both cover takes the newer reply."""
    portal.replies["EnergyInformationController.getMeterData"] = [
        (200, _meter_reply(_nem12("20260101", 1.0))),
        (200, _meter_reply(_nem12("20260101", 2.0) + _nem12("20260401", 3.0))),
    ]

    channels = await api.async_meter_data(
        "61020000001", "Citipower", date(2026, 1, 1), date(2026, 4, 1)
    )

    assert [(c["startDate"], c["endDate"], c["jobId"]) for c in portal.meter_calls()] == [
        ("2026-01-01", "2026-01-01", 0),
        ("2026-01-02", "2026-04-01", 1),
    ]
    channel = channels["E1"]
    assert channel.days == {date(2026, 1, 1), date(2026, 4, 1)}
    # The first reply said 1.0 per interval (48 kWh); the second overwrote it with 2.0 (96 kWh).
    day_total = sum(v for h, v in channel.hours.items() if h.date() == date(2026, 1, 1))
    assert day_total == pytest.approx(96.0)


async def test_channels_from_different_chunks_are_merged(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    portal.replies["EnergyInformationController.getMeterData"] = [
        (200, _meter_reply(_nem12("20260101", 1.0, "E1"))),
        (200, _meter_reply(_nem12("20260102", 1.0, "B1"))),
    ]

    channels = await api.async_meter_data(
        "61020000001", "Citipower", date(2026, 1, 1), date(2026, 4, 1)
    )

    assert sorted(channels) == ["B1", "E1"]
    assert channels["B1"].is_export


@pytest.mark.parametrize("status", ["Failed", "Error"])
async def test_a_meter_data_request_that_did_not_succeed_is_an_api_error(
    portal: FakePortal, api: MyEnergyApi, status: str
) -> None:
    portal.replies["EnergyInformationController.getMeterData"] = [
        (200, _meter_reply("", status=status))
    ]

    with pytest.raises(MyEnergyApiError, match=f"status {status}"):
        await api.async_meter_data("61020000001", "Citipower", date(2026, 1, 1), date(2026, 1, 2))


async def test_a_portal_that_reports_isSuccess_false_is_an_api_error(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    body = json.dumps(
        {
            "actions": [
                {
                    "state": "SUCCESS",
                    "returnValue": {
                        "returnValue": {"isSuccess": False, "errorMessage": "No access"}
                    },
                }
            ]
        }
    )
    portal.replies["EnergyInformationController.retrieveAccountHolderInfo"] = [(200, body)]

    with pytest.raises(MyEnergyApiError, match="No access"):
        await api.async_account_info("61020000001")


async def test_a_dead_session_surfaces_as_an_auth_error_after_one_refresh(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    portal.replies["EnergyUsageDetailPageController.getEnergyUsageDetailWrapper"] = [
        (403, "forbidden")
    ]

    with pytest.raises(MyEnergyAuthError, match="HTTP 403"):
        await api.async_list_nmis()
    # The refusal was tried, then retried once with a refreshed context; no more.
    assert len(portal.calls) == 2


async def test_the_session_clock_is_read_from_the_servlet(
    portal: FakePortal, api: MyEnergyApi
) -> None:
    assert await api.async_session_seconds() == 7172


@pytest.mark.parametrize(
    "reply", [(404, "not found"), (200, "{}"), (200, "")], ids=["http-error", "no-sr", "empty"]
)
async def test_an_unreadable_session_clock_is_none(
    portal: FakePortal, api: MyEnergyApi, reply: tuple[int, str]
) -> None:
    portal.session_reply = reply
    assert await api.async_session_seconds() is None
