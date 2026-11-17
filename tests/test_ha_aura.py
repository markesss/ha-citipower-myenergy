"""HTTP-level tests for ``aura.py``: page loads, Aura posts and the client's stale-context retry.

A local ``aiohttp`` server (the ``serve`` fixture in ``conftest.py``) stands in for the portal
and replays the anonymised captures. As in ``test_ha_auth.py``, the signed-in page is the
``community_page.html`` capture.

What the fixtures do not give us: the portal's real error bodies. The Aura error shapes staged
here (``invalidSession`` event, ``state: ERROR`` with a message, a non-JSON body) follow what
``portal.extract_return_value`` reads, so they prove the control flow and the exception mapping,
not the portal's wording.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from custom_components.citipower_myenergy import aura
from custom_components.citipower_myenergy.exceptions import (
    MyEnergyApiError,
    MyEnergyAuthError,
    MyEnergyCannotConnect,
)
from custom_components.citipower_myenergy.portal import AuraContext

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN_VALUE = "fake-csrf-token"
OLD_CONTEXT = AuraContext(context={"fwuid": "stale", "app": "siteforce:communityApp"}, token="old")


def ok(return_value: Any) -> str:
    """An Aura body for a successful Apex call (the portal double-wraps the return value)."""
    return json.dumps(
        {
            "actions": [
                {"id": "1;a", "state": "SUCCESS", "returnValue": {"returnValue": return_value}}
            ]
        }
    )


INVALID_SESSION = json.dumps({"event": {"descriptor": "markup://aura:invalidSession"}})
OUT_OF_SYNC = json.dumps(
    {"actions": [{"state": "ERROR", "error": [{"message": "clientOutOfSync: refresh the page"}]}]}
)


class FakePortal:
    """The usage page and the Aura endpoint, with a record of what the client sent."""

    def __init__(self) -> None:
        self.page = (FIXTURES / "community_page.html").read_text()
        marker = '"eikoocnekot":"'
        start = self.page.index(marker) + len(marker)
        self.token_cookie = self.page[start : self.page.index('"', start)]
        self.usage_hits = 0
        self.usage_redirects_to_login = False
        self.set_token_cookie = True
        self.forms: list[dict[str, str]] = []
        self.queries: list[dict[str, str]] = []
        self.headers: list[dict[str, str]] = []
        # Each entry is (status, body); the last one repeats.
        self.replies: list[tuple[int, str]] = [(200, ok({"data": "fine"}))]

    async def usage(self, request: web.Request) -> web.Response:
        self.usage_hits += 1
        if self.usage_redirects_to_login:
            raise web.HTTPFound("/s/login/")
        response = web.Response(text=self.page, content_type="text/html")
        if self.set_token_cookie:
            response.set_cookie(self.token_cookie, TOKEN_VALUE)
        return response

    async def login(self, request: web.Request) -> web.Response:
        return web.Response(text="<html>sign in</html>", content_type="text/html")

    async def aura(self, request: web.Request) -> web.Response:
        self.forms.append({key: value for key, value in (await request.post()).items()})
        self.queries.append(dict(request.query))
        self.headers.append(dict(request.headers))
        status, body = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return web.Response(status=status, text=body, content_type="application/json")


@pytest.fixture
async def portal(serve) -> FakePortal:
    fake = FakePortal()
    await serve(
        [
            ("GET", "/s/energy-usage", fake.usage),
            ("GET", "/s/login/", fake.login),
            ("POST", "/s/sfsites/aura", fake.aura),
        ]
    )
    return fake


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    # unsafe=True: the default jar refuses cookies from an IP address such as the fake server's.
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        yield client


class BrokenSession:
    """A session whose every request fails the way a dead network does."""

    def __init__(self, real: aiohttp.ClientSession, error: BaseException) -> None:
        self.cookie_jar = real.cookie_jar
        self._error = error

    def get(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error

    def post(self, *args: Any, **kwargs: Any) -> Any:
        raise self._error


def _client(session, on_change=None, context=OLD_CONTEXT, cookies=None):
    return aura.MyEnergyAuraClient(session, cookies or {}, context, on_change)


# --- headers and cookies -------------------------------------------------------------


def test_page_and_xhr_headers_differ_the_way_a_navigation_and_a_background_call_do() -> None:
    page = aura.page_headers()
    assert page["Sec-Fetch-Mode"] == "navigate"
    assert page["Upgrade-Insecure-Requests"] == "1"
    assert page["Accept"].startswith("text/html")

    xhr = aura.xhr_headers(referer="https://example.invalid/r", content_type="application/json")
    assert xhr["Sec-Fetch-Mode"] == "cors"
    assert xhr["Referer"] == "https://example.invalid/r"
    assert xhr["Content-Type"] == "application/json"
    assert "Upgrade-Insecure-Requests" not in xhr
    # The browser's form encoding is the default.
    assert aura.xhr_headers()["Content-Type"] == "application/x-www-form-urlencoded; charset=UTF-8"


async def test_client_seeds_the_jar_and_keeps_only_cookies_worth_restoring(
    portal: FakePortal, session
) -> None:
    client = _client(
        session,
        cookies={"sid": "abc", "_ga": "analytics", "__Host-ERIC_x": "csrf", "other": "keep"},
    )

    assert aura.read_cookie(session, "sid") == "abc"
    assert aura.read_cookie(session, "missing") is None
    # Analytics and the CSRF cookie are not stored: the token is re-read from the page.
    assert client.cookies == {"sid": "abc", "other": "keep"}
    assert client.session is session
    assert client.aura is OLD_CONTEXT


# --- loading pages -------------------------------------------------------------------


async def test_a_page_load_follows_script_redirects(serve, session) -> None:
    async def first(request):
        return web.Response(
            text="<script>window.location.replace('/b?sid=secret')</script>",
            content_type="text/html",
        )

    async def second(request):
        assert request.query["sid"] == "secret"
        return web.Response(text="<html>arrived</html>", content_type="text/html")

    origin = await serve([("GET", "/a", first), ("GET", "/b", second)])

    html, final = await aura.async_get_page(session, f"{origin}/a")
    assert "arrived" in html
    assert final == f"{origin}/b?sid=secret"


async def test_a_page_that_redirects_forever_by_script_is_an_api_error(serve, session) -> None:
    hits = 0

    async def loop(request):
        nonlocal hits
        hits += 1
        return web.Response(
            text="<script>window.location='/loop'</script>", content_type="text/html"
        )

    origin = await serve([("GET", "/loop", loop)])

    with pytest.raises(MyEnergyApiError, match="too many times"):
        await aura.async_get_page(session, f"{origin}/loop")
    assert hits == aura.MAX_CLIENT_REDIRECTS


async def test_an_http_redirect_loop_means_the_session_has_ended(serve, session) -> None:
    async def loop(request):
        raise web.HTTPFound("/loop")

    origin = await serve([("GET", "/loop", loop)])

    with pytest.raises(MyEnergyAuthError, match="keeps redirecting"):
        await aura.async_get_page(session, f"{origin}/loop")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (aiohttp.ClientConnectionError("connection refused"), "ClientConnectionError"),
        (TimeoutError(), "the request timed out"),
    ],
)
async def test_a_dead_network_is_cannot_connect(session, error, message) -> None:
    with pytest.raises(MyEnergyCannotConnect, match=message):
        await aura.async_get_page(BrokenSession(session, error), "https://example.invalid/")


async def test_the_context_comes_from_the_page_and_the_token_from_its_cookie(
    portal: FakePortal, session
) -> None:
    context = await aura.async_load_page_context(session, aura.USAGE_PAGE)

    assert context.token == TOKEN_VALUE
    assert context.context["fwuid"]
    assert context.context["app"] == "siteforce:communityApp"
    assert context.context["uad"] is True


async def test_a_page_without_a_token_announcement_gets_the_literal_null(
    portal: FakePortal, session
) -> None:
    portal.page = portal.page.replace("eikoocnekot", "not-a-token-announcement")

    context = await aura.async_load_page_context(session, aura.USAGE_PAGE)
    assert context.token == "null"


async def test_a_named_but_unset_token_cookie_is_an_api_error(portal: FakePortal, session) -> None:
    portal.set_token_cookie = False  # the page names the cookie but the server never sends it

    with pytest.raises(MyEnergyApiError, match="did not set it"):
        await aura.async_load_page_context(session, aura.USAGE_PAGE)


async def test_a_page_without_a_descriptor_is_an_api_error_that_names_the_page(
    serve, session
) -> None:
    async def bare(request):
        return web.Response(text="<html>nothing here</html>", content_type="text/html")

    origin = await serve([("GET", "/s/energy-usage", bare)])

    with pytest.raises(MyEnergyApiError, match=r"descriptor.*loaded .*/s/energy-usage"):
        await aura.async_load_page_context(session, f"{origin}/s/energy-usage")


@pytest.mark.parametrize("landing", ["/s/login/", "/secur/login", "/CommunitiesLanding"])
async def test_being_sent_to_a_login_page_means_the_session_expired(
    serve, session, landing
) -> None:
    async def bounce(request):
        raise web.HTTPFound(landing)

    async def landing_page(request):
        return web.Response(text="<html>sign in</html>", content_type="text/html")

    origin = await serve([("GET", "/s/energy-usage", bounce), ("GET", landing, landing_page)])

    with pytest.raises(MyEnergyAuthError, match="Session expired"):
        await aura.async_load_page_context(session, f"{origin}/s/energy-usage")


# --- posting to Aura -----------------------------------------------------------------


async def test_an_aura_post_carries_what_the_browser_sends(portal: FakePortal, session) -> None:
    portal.replies = [(200, ok({"data": [1, 2]}))]
    context = AuraContext(context={"fwuid": "fw", "app": "siteforce:communityApp"}, token="tok")

    result = await aura.async_post_aura(
        session, context, "SomeClass", "someMethod", {"a": 1}, "/s/energy-usage", aura.USAGE_PAGE, 7
    )

    assert result == {"data": [1, 2]}
    assert portal.queries == [{"r": "7", "aura.ApexAction.execute": "1"}]
    form = portal.forms[0]
    assert form["aura.token"] == "tok"
    assert form["aura.pageURI"] == "/s/energy-usage"
    assert json.loads(form["aura.context"]) == context.context
    params = json.loads(form["message"])["actions"][0]["params"]
    assert (params["classname"], params["method"], params["params"]) == (
        "SomeClass",
        "someMethod",
        {"a": 1},
    )
    headers = portal.headers[0]
    assert headers["Referer"] == aura.USAGE_PAGE
    assert headers["Content-Type"].startswith("application/x-www-form-urlencoded")


async def test_the_anti_hijacking_prefix_is_stripped(portal: FakePortal, session) -> None:
    portal.replies = [(200, "while(1);\n" + ok("payload"))]

    result = await aura.async_post_aura(
        session, OLD_CONTEXT, "C", "m", None, "/s/energy-usage", aura.USAGE_PAGE
    )
    assert result == "payload"


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_request_is_an_auth_error(portal: FakePortal, session, status) -> None:
    portal.replies = [(status, "denied")]

    with pytest.raises(MyEnergyAuthError, match=f"HTTP {status}"):
        await aura.async_post_aura(
            session, OLD_CONTEXT, "C", "m", None, "/s/energy-usage", aura.USAGE_PAGE
        )


async def test_an_empty_server_error_is_cannot_connect_but_one_with_a_body_is_read(
    portal: FakePortal, session
) -> None:
    portal.replies = [(502, "   ")]
    with pytest.raises(MyEnergyCannotConnect, match="HTTP 502"):
        await aura.async_post_aura(
            session, OLD_CONTEXT, "C", "m", None, "/s/energy-usage", aura.USAGE_PAGE
        )

    # A 500 that still carries an Aura body is parsed like any other (here, an error action).
    portal.replies = [(500, OUT_OF_SYNC)]
    with pytest.raises(MyEnergyApiError, match="clientOutOfSync"):
        await aura.async_post_aura(
            session, OLD_CONTEXT, "C", "m", None, "/s/energy-usage", aura.USAGE_PAGE
        )


@pytest.mark.parametrize(
    ("body", "error", "match"),
    [
        (INVALID_SESSION, MyEnergyAuthError, "no longer valid"),
        (
            json.dumps({"exceptionMessage": "Guest user access is not allowed"}),
            MyEnergyAuthError,
            "no longer valid",
        ),
        (json.dumps({"actions": [], "exceptionMessage": "boom"}), MyEnergyApiError, "boom"),
        (json.dumps({"actions": []}), MyEnergyApiError, "no action result"),
        (
            json.dumps({"actions": [{"state": "ERROR", "error": [{"message": "bad apex"}]}]}),
            MyEnergyApiError,
            "rejected the request: bad apex",
        ),
        (
            json.dumps({"actions": [{"state": "INCOMPLETE", "error": []}]}),
            MyEnergyApiError,
            "rejected the request: INCOMPLETE",
        ),
        ("<html>maintenance</html>", MyEnergyApiError, "Could not parse"),
    ],
)
async def test_aura_failures_map_onto_the_exception_hierarchy(
    portal: FakePortal, session, body, error, match
) -> None:
    portal.replies = [(200, body)]

    with pytest.raises(error, match=match):
        await aura.async_post_aura(
            session, OLD_CONTEXT, "C", "m", None, "/s/energy-usage", aura.USAGE_PAGE
        )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (aiohttp.ServerDisconnectedError(), "ServerDisconnectedError"),
        (TimeoutError(), "timed out"),
    ],
)
async def test_a_failed_aura_post_is_cannot_connect(session, error, message) -> None:
    with pytest.raises(MyEnergyCannotConnect, match=message):
        await aura.async_post_aura(
            BrokenSession(session, error),
            OLD_CONTEXT,
            "C",
            "m",
            None,
            "/s/energy-usage",
            aura.USAGE_PAGE,
        )


# --- the client ----------------------------------------------------------------------


async def test_the_first_call_loads_a_context_and_reports_it(portal: FakePortal, session) -> None:
    changes: list[tuple[AuraContext | None, dict[str, str]]] = []

    async def on_change(context, cookies):
        changes.append((context, cookies))

    client = _client(session, on_change, context=None)
    assert client.aura is None

    assert await client.async_invoke("C", "m", {"x": 1}) == {"data": "fine"}
    assert await client.async_invoke("C", "m") == {"data": "fine"}

    # One page load for the two calls, and the caller was told about the new context once.
    assert portal.usage_hits == 1
    assert [form["aura.token"] for form in portal.forms] == [TOKEN_VALUE, TOKEN_VALUE]
    assert len(changes) == 1
    assert changes[0][0] is client.aura
    assert changes[0][0].token == TOKEN_VALUE
    # The CSRF cookie is not among the cookies handed over for storage.
    assert not any(name.startswith("__Host-ERIC") for name in changes[0][1])
    # Request numbers count up across calls, as the browser's do.
    assert [query["r"] for query in portal.queries] == ["1", "2"]


async def test_refreshing_without_a_change_callback_still_works(
    portal: FakePortal, session
) -> None:
    client = _client(session, on_change=None)
    context = await client.async_refresh()
    assert context.token == TOKEN_VALUE
    assert client.aura is context


@pytest.mark.parametrize("stale", [OUT_OF_SYNC, INVALID_SESSION])
async def test_a_stale_context_is_refreshed_once_and_the_call_retried(
    portal: FakePortal, session, stale
) -> None:
    portal.replies = [(200, stale), (200, ok("second time lucky"))]
    notified: list[str] = []

    async def on_change(context, cookies):
        notified.append(context.token)

    client = _client(session, on_change)

    assert await client.async_invoke("C", "m") == "second time lucky"
    assert [form["aura.token"] for form in portal.forms] == ["old", TOKEN_VALUE]
    assert portal.usage_hits == 1
    assert notified == [TOKEN_VALUE]


async def test_an_unrelated_api_error_is_not_retried(portal: FakePortal, session) -> None:
    portal.replies = [
        (200, json.dumps({"actions": [{"state": "ERROR", "error": [{"message": "bad input"}]}]}))
    ]
    client = _client(session)

    with pytest.raises(MyEnergyApiError, match="bad input"):
        await client.async_invoke("C", "m")
    assert portal.usage_hits == 0
    assert len(portal.forms) == 1


async def test_a_second_failure_after_the_refresh_is_raised(portal: FakePortal, session) -> None:
    portal.replies = [(200, INVALID_SESSION)]  # repeats for every call
    client = _client(session)

    with pytest.raises(MyEnergyAuthError):
        await client.async_invoke("C", "m")
    # One try, one refresh, one retry: no loop.
    assert len(portal.forms) == 2
    assert portal.usage_hits == 1


async def test_when_the_page_load_fails_the_context_in_hand_gets_a_second_chance(
    portal: FakePortal, session
) -> None:
    """Loading a page can fail where the endpoint would still accept the held context."""
    portal.usage_redirects_to_login = True
    portal.replies = [(200, INVALID_SESSION), (200, ok("accepted"))]
    client = _client(session)

    assert await client.async_invoke("C", "m") == "accepted"
    assert [form["aura.token"] for form in portal.forms] == ["old", "old"]
    assert client.aura is OLD_CONTEXT


async def test_if_nothing_is_held_a_failed_refresh_is_raised(
    portal: FakePortal, session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive branch: the context goes missing between the failed call and the refresh."""
    portal.usage_redirects_to_login = True
    client = _client(session)

    async def fail_and_forget(classname, method, params):
        client._aura = None
        raise MyEnergyAuthError("gone")

    monkeypatch.setattr(client, "_async_invoke_once", fail_and_forget)

    with pytest.raises(MyEnergyAuthError, match="Session expired"):
        await client.async_invoke("C", "m")


async def test_small_resources_are_read_up_to_500_characters(serve, session) -> None:
    async def long_text(request):
        assert request.headers["Referer"].endswith("/s/energy-usage")
        return web.Response(text="x" * 800)

    async def gone(request):
        return web.Response(status=404, text="nope")

    async def moved(request):
        raise web.HTTPFound("/elsewhere")

    origin = await serve(
        [("GET", "/long", long_text), ("GET", "/gone", gone), ("GET", "/moved", moved)]
    )
    client = _client(session)

    assert await client.async_get_text(f"{origin}/long") == "x" * 500
    assert await client.async_get_text(f"{origin}/gone") is None
    # Redirects are not followed: a bounce to a login page is "unavailable", not a page of text.
    assert await client.async_get_text(f"{origin}/moved") is None


@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError("down"), TimeoutError()])
async def test_a_failed_small_read_is_none_not_an_exception(session, error) -> None:
    client = _client(BrokenSession(session, error))
    assert await client.async_get_text("https://example.invalid/x") is None
