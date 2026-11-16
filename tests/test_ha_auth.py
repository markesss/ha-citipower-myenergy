"""The full sign-in (password, then MFA) against a fake portal that replays the fixtures.

A local ``aiohttp`` server stands in for myenergy.powercor.com.au. Each route serves one of the
anonymised captures in ``tests/fixtures`` and, like the real portal, refuses a request that
arrives out of order (no session cookie, wrong remoting signature, a form that is not the one the
page carried). The portal's host in the captures is rewritten to the fake server's; nothing else
is changed.

What the fixtures honestly allow, and what they do not:

* Replayed for real: the login page, ``communityLogin``, ``frontdoor.jsp``, the MFA page, both
  remoting calls, the final form post and the signed-in page.
* Derived, not captured: the *login page* HTML was never saved, so it is the signed-in page with
  its ``eikoocnekot`` token announcement removed. That is what the live sign-in showed (a
  descriptor and no token, so the Aura token is the literal ``"null"``).
* Not available: the response to a wrong password (CLAUDE.md assumption 2). A wrong *code* is
  staged with the shape the page script reads (``success: false`` plus ``message``, assumption 3),
  so that test only proves the control flow, not the portal's wording.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.citipower_myenergy import aura, auth
from custom_components.citipower_myenergy.exceptions import (
    MyEnergyApiError,
    MyEnergyInvalidAuth,
    MyEnergyInvalidCode,
)
from custom_components.citipower_myenergy.portal import parse_mfa_page

FIXTURES = Path(__file__).parent / "fixtures"
REAL_ORIGIN = "https://myenergy.powercor.com.au"
EMAIL = "someone@example.invalid"
PASSWORD = "correct-horse-battery-staple"
CODE = "123456"
# The capture's frontdoor URL carries this placeholder in place of the one-time session id.
FRONTDOOR_SID = "REDACTED_SID"
SESSION_COOKIE = "sid"
SESSION_VALUE = "fake-session-value"
TOKEN_VALUE = "fake-csrf-token"


def _text(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakePortal:
    """The routes of the sign-in, with a record of what the client sent."""

    def __init__(self) -> None:
        self.origin = ""
        self.calls: list[str] = []
        self.aura_posts: list[dict[str, str]] = []
        self.remoting: list[dict] = []
        self.final_form: dict[str, str] | None = None
        self.accept_code = True
        self.reject_login = False
        self.mfa_done = False
        self._mfa_html = _text("mfa_enabler.html")
        self._page = parse_mfa_page(self._mfa_html)
        signed_in = _text("community_page.html")
        self._signed_in = signed_in
        # Derived (see module docstring): the signed-in page minus the token announcement.
        self._login_page = signed_in.replace("eikoocnekot", "not-a-token-announcement")
        assert "eikoocnekot" not in self._login_page

    def _body(self, text: str) -> str:
        return text.replace(REAL_ORIGIN, self.origin)

    def _token_cookie_name(self) -> str:
        marker = '"eikoocnekot":"'
        start = self._signed_in.index(marker) + len(marker)
        return self._signed_in[start : self._signed_in.index('"', start)]

    # --- routes ------------------------------------------------------------------

    async def login_page(self, request: web.Request) -> web.Response:
        self.calls.append("login_page")
        return web.Response(text=self._login_page, content_type="text/html")

    async def aura(self, request: web.Request) -> web.Response:
        self.calls.append("aura")
        form = {key: value for key, value in (await request.post()).items()}
        self.aura_posts.append(form)
        action = json.loads(form["message"])["actions"][0]
        assert action["descriptor"].endswith("ApexActionController/ACTION$execute")
        params = action["params"]
        assert params["classname"] == "LightningLoginFormController"
        assert params["method"] == "communityLogin"
        credentials = params["params"]
        if self.reject_login or credentials["password"] != PASSWORD:
            body = {"actions": [{"state": "ERROR", "error": [{"message": "no"}]}]}
            return web.json_response(body)
        # The browser's username carries the network suffix; a bare email is not accepted.
        assert credentials["username"] == f"{EMAIL}.VPN"
        return web.Response(
            text=self._body(_text("login_response.json")), content_type="application/json"
        )

    async def frontdoor(self, request: web.Request) -> web.Response:
        self.calls.append("frontdoor")
        assert request.query["sid"] == FRONTDOOR_SID
        response = web.Response(text=self._body(_text("frontdoor.html")), content_type="text/html")
        response.set_cookie(SESSION_COOKIE, SESSION_VALUE)
        return response

    async def mfa_page(self, request: web.Request) -> web.Response:
        self.calls.append("mfa_page")
        if request.cookies.get(SESSION_COOKIE) != SESSION_VALUE:
            raise web.HTTPFound("/s/login/")
        return web.Response(text=self._body(self._mfa_html), content_type="text/html")

    async def apexremote(self, request: web.Request) -> web.Response:
        self.calls.append("apexremote")
        assert request.headers["X-User-Agent"] == "Visualforce-Remoting"
        body = await request.json()
        self.remoting.append(body)
        method = self._page.methods[body["method"]]
        # The signing material must be what the page announced for that very method.
        assert body["action"] == self._page.controller
        assert body["ctx"]["vid"] == self._page.vid
        assert body["ctx"]["csrf"] == method.csrf
        assert body["ctx"].get("authorization") == method.authorization
        if body["method"] == "initiateMFA":
            payload = json.loads(_text("initiate_mfa.json"))
            assert body["data"] in (["sms"], ["email"])
        else:
            payload = json.loads(_text("verify_mfa.json"))
            code, verification_id, registration, chosen = body["data"]
            assert verification_id == "MFF0T1kwMDAwMFIwdWd6"  # from initiate_mfa.json
            assert registration is False
            assert chosen in ("sms", "email")
            if not (self.accept_code and code == CODE):
                payload[0]["result"] = {"success": False, "message": "That code is not right"}
        payload[0]["tid"] = body["tid"]
        return web.json_response(payload)

    async def mfa_post(self, request: web.Request) -> web.Response:
        self.calls.append("mfa_post")
        self.final_form = {key: value for key, value in (await request.post()).items()}
        assert self.final_form == self._page.final_form_fields
        assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        self.mfa_done = True
        return web.Response(text=_text("mfa_post.html"), content_type="text/html")

    async def usage_page(self, request: web.Request) -> web.Response:
        self.calls.append("usage_page")
        if not self.mfa_done:
            # Without the finished login flow the portal sends the user back to sign in.
            raise web.HTTPFound("/s/login/")
        response = web.Response(text=self._body(self._signed_in), content_type="text/html")
        response.set_cookie(self._token_cookie_name(), TOKEN_VALUE)
        return response


@pytest.fixture
async def portal(socket_enabled, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakePortal]:
    """Run the fake portal and point the client's URLs at it.

    ``socket_enabled`` lifts the test package's socket ban; the server only listens on loopback.
    """
    fake = FakePortal()
    app = web.Application()
    app.router.add_get("/s/login/", fake.login_page)
    app.router.add_post("/s/sfsites/aura", fake.aura)
    app.router.add_get("/secur/frontdoor.jsp", fake.frontdoor)
    app.router.add_get("/apex/MFA_Enabler", fake.mfa_page)
    app.router.add_post("/apexremote", fake.apexremote)
    app.router.add_post("/MFA_Enabler", fake.mfa_post)
    app.router.add_get("/s/energy-usage", fake.usage_page)
    server = TestServer(app)
    await server.start_server()
    origin = str(server.make_url("")).rstrip("/")
    fake.origin = origin

    for module in (aura, auth):
        for name, path in (
            ("BASE_URL", ""),
            ("AURA_ENDPOINT", "/s/sfsites/aura"),
            ("LOGIN_PAGE", "/s/login/"),
            ("HOME_PAGE", "/s/"),
            ("USAGE_PAGE", "/s/energy-usage"),
            ("REMOTING_ENDPOINT", "/apexremote"),
        ):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, f"{origin}{path}")
    try:
        yield fake
    finally:
        await server.close()


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    # unsafe=True: the default jar refuses cookies from an IP address such as the fake server's.
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
        yield client


async def test_password_then_code_signs_in(portal: FakePortal, session) -> None:
    login = auth.MyEnergyLogin(session)

    page = await login.async_submit_credentials(EMAIL, PASSWORD)
    assert page is not None
    assert page.options == ["sms", "email"]
    assert portal.calls == ["login_page", "aura", "frontdoor", "mfa_page"]

    await login.async_send_code("sms")
    signed_in = await login.async_verify_code(CODE)

    # The whole sequence, in the browser's order, ending on the signed-in page.
    assert portal.calls == [
        "login_page",
        "aura",
        "frontdoor",
        "mfa_page",
        "apexremote",
        "apexremote",
        "mfa_post",
        "usage_page",
    ]
    assert [call["method"] for call in portal.remoting] == ["initiateMFA", "verifyMFA"]
    # tids count up from 2, as in the capture.
    assert [call["tid"] for call in portal.remoting] == [2, 3]
    assert portal.remoting[0]["data"] == ["sms"]
    assert portal.remoting[1]["data"] == [CODE, "MFF0T1kwMDAwMFIwdWd6", False, "sms"]

    assert signed_in.cookies[SESSION_COOKIE] == SESSION_VALUE
    assert signed_in.context.token == TOKEN_VALUE
    assert signed_in.context.context["fwuid"]
    assert signed_in.context.context["app"] == "siteforce:communityApp"


async def test_the_login_page_context_has_the_literal_null_token(
    portal: FakePortal, session
) -> None:
    """communityLogin is posted with the page's context and the string "null" as its token."""
    await auth.MyEnergyLogin(session).async_submit_credentials(EMAIL, PASSWORD)

    post = portal.aura_posts[0]
    assert post["aura.token"] == "null"
    # The derived page's descriptor names the community app, so the context's app is not checked.
    assert json.loads(post["aura.context"])["fwuid"]
    assert post["aura.pageURI"] == "/s/login/"


async def test_email_can_be_chosen_instead_of_sms(portal: FakePortal, session) -> None:
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)
    await login.async_send_code("email")
    await login.async_verify_code(CODE)

    assert [call["data"][-1] for call in portal.remoting if call["method"] == "verifyMFA"] == [
        "email"
    ]
    assert portal.remoting[0]["data"] == ["email"]


async def test_the_final_form_post_is_what_completes_the_sign_in(
    portal: FakePortal, session
) -> None:
    """verifyMFA alone is not enough: the page's own form must be posted back."""
    login = auth.MyEnergyLogin(session)
    page = await login.async_submit_credentials(EMAIL, PASSWORD)
    await login.async_send_code("sms")
    await login.async_verify_code(CODE)

    assert portal.final_form == page.final_form_fields
    assert "j_id0:j_id29" in portal.final_form  # the form id doubles as a field, as jsfcljs does
    assert any(name.endswith("ViewStateMAC") for name in portal.final_form)


async def test_a_wrong_code_is_rejected_before_anything_else_is_posted(
    portal: FakePortal, session
) -> None:
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)
    await login.async_send_code("sms")

    with pytest.raises(MyEnergyInvalidCode, match="not right"):
        await login.async_verify_code("000000")
    assert "mfa_post" not in portal.calls
    assert "usage_page" not in portal.calls

    # The same verification id still works for a second attempt, so the user can retry.
    signed_in = await login.async_verify_code(CODE)
    assert signed_in.cookies[SESSION_COOKIE] == SESSION_VALUE


async def test_a_rejected_password_stops_before_the_mfa_page(portal: FakePortal, session) -> None:
    with pytest.raises(MyEnergyInvalidAuth):
        await auth.MyEnergyLogin(session).async_submit_credentials(EMAIL, "wrong")
    assert portal.calls == ["login_page", "aura"]


async def test_finishing_without_the_form_post_is_not_a_sign_in(
    portal: FakePortal, session
) -> None:
    """If the portal bounces us to the login page, the sign-in is reported as incomplete."""
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)

    with pytest.raises(MyEnergyInvalidCode, match="did not complete"):
        await login.async_finish()


async def test_a_code_cannot_be_checked_before_one_is_requested(
    portal: FakePortal, session
) -> None:
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)

    with pytest.raises(MyEnergyApiError, match="No verification code"):
        await login.async_verify_code(CODE)
    assert portal.remoting == []


async def test_the_password_is_not_kept(portal: FakePortal, session) -> None:
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)
    await login.async_send_code("sms")
    signed_in = await login.async_verify_code(CODE)

    assert PASSWORD not in repr(vars(login))
    assert PASSWORD not in json.dumps(signed_in.cookies)
    assert PASSWORD not in json.dumps(signed_in.context.as_dict())
