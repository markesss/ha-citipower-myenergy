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
    MyEnergyCannotConnect,
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
        # Knobs for the failure tests. The defaults replay the captures untouched.
        self.login_result: object = None  # replaces communityLogin's returnValue
        self.initiate_result: dict | None = None  # replaces initiateMFA's result
        self.code_message: str | None = "That code is not right"  # the wrong-code message
        self.remoting_mode: str | None = None  # "http500", "garbage" or "drop"
        self.frontdoor_mode: str | None = None  # "drop", "home", "stuck" or "loop"
        self.mfa_post_mode: str | None = None  # "drop"
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
        if self.login_result is not None:
            body = {
                "actions": [{"state": "SUCCESS", "returnValue": {"returnValue": self.login_result}}]
            }
            return web.json_response(body)
        return web.Response(
            text=self._body(_text("login_response.json")), content_type="application/json"
        )

    async def frontdoor(self, request: web.Request) -> web.Response:
        self.calls.append("frontdoor")
        assert request.query["sid"] == FRONTDOOR_SID
        if self.frontdoor_mode == "drop":
            request.transport.close()
            return web.Response()
        if self.frontdoor_mode == "stuck":
            return web.Response(text="<html>nothing to do here</html>", content_type="text/html")
        if self.frontdoor_mode == "loop":
            script = (
                "<script>window.location.replace('/secur/frontdoor.jsp?sid=REDACTED_SID')</script>"
            )
            return web.Response(text=script, content_type="text/html")
        if self.frontdoor_mode == "home":
            redirect = web.HTTPFound("/s/")
            redirect.set_cookie(SESSION_COOKIE, SESSION_VALUE)
            raise redirect
        response = web.Response(text=self._body(_text("frontdoor.html")), content_type="text/html")
        response.set_cookie(SESSION_COOKIE, SESSION_VALUE)
        return response

    async def home(self, request: web.Request) -> web.Response:
        self.calls.append("home")
        return web.Response(text="<html>home</html>", content_type="text/html")

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
        if self.remoting_mode == "http500":
            return web.Response(status=500, text="oops")
        if self.remoting_mode == "garbage":
            return web.Response(text="<html>not json</html>", content_type="application/json")
        if self.remoting_mode == "drop":
            request.transport.close()
            return web.Response()
        method = self._page.methods[body["method"]]
        # The signing material must be what the page announced for that very method.
        assert body["action"] == self._page.controller
        assert body["ctx"]["vid"] == self._page.vid
        assert body["ctx"]["csrf"] == method.csrf
        assert body["ctx"].get("authorization") == method.authorization
        if body["method"] == "initiateMFA":
            payload = json.loads(_text("initiate_mfa.json"))
            assert body["data"] in (["sms"], ["email"])
            if self.initiate_result is not None:
                payload[0]["result"] = self.initiate_result
        else:
            payload = json.loads(_text("verify_mfa.json"))
            code, verification_id, registration, chosen = body["data"]
            assert verification_id == "MFF0T1kwMDAwMFIwdWd6"  # from initiate_mfa.json
            assert registration is False
            assert chosen in ("sms", "email")
            if not (self.accept_code and code == CODE):
                payload[0]["result"] = {"success": False}
                if self.code_message:
                    payload[0]["result"]["message"] = self.code_message
        payload[0]["tid"] = body["tid"]
        return web.json_response(payload)

    async def mfa_post(self, request: web.Request) -> web.Response:
        self.calls.append("mfa_post")
        if self.mfa_post_mode == "drop":
            request.transport.close()
            return web.Response()
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
    app.router.add_get("/s/", fake.home)
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


# --- the rest of the sign-in's error paths ---------------------------------------------------
#
# None of these shapes was captured from the portal (CLAUDE.md assumptions 2 and 3): they are
# staged so the control flow and the exception each one maps to are pinned, not the wording.


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("someone@example.invalid", "someone@example.invalid.VPN"),
        ("  someone@example.invalid  ", "someone@example.invalid.VPN"),
        ("someone@example.invalid.VPN", "someone@example.invalid.VPN"),
        # The suffix check ignores case, so a pasted lower-case suffix is not doubled.
        ("someone@example.invalid.vpn", "someone@example.invalid.vpn"),
    ],
)
def test_the_username_gets_the_network_suffix_once(typed: str, expected: str) -> None:
    assert auth.portal_username(typed) == expected


async def test_the_login_object_exposes_its_state_as_the_sign_in_advances(
    portal: FakePortal, session
) -> None:
    login = auth.MyEnergyLogin(session)
    assert login.session is session
    assert login.mfa_page is None
    assert login.method == "email"  # the default until the user chooses

    page = await login.async_submit_credentials(EMAIL, PASSWORD)
    assert login.mfa_page is page

    await login.async_send_code("sms")
    assert login.method == "sms"


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"isSuccess": False, "errorMessage": "Your login attempt has failed."}, "login attempt"),
        ({"isSuccess": True, "data": {"message": "Try again later"}}, "Try again later"),
        ({"isSuccess": True, "data": {}}, "rejected that email address or password"),
        ("Plain string refusal", "Plain string refusal"),
    ],
)
async def test_a_login_result_without_a_frontdoor_url_is_a_rejection(
    portal: FakePortal, session, result, message
) -> None:
    portal.login_result = result

    with pytest.raises(MyEnergyInvalidAuth, match=message):
        await auth.MyEnergyLogin(session).async_submit_credentials(EMAIL, PASSWORD)
    assert portal.calls == ["login_page", "aura"]


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"success": False, "message": "Too many codes"}, "Too many codes"),
        ({"success": False}, "could not send a code by sms"),
        ({"success": True, "isRegistration": True}, "not finished setting up"),
    ],
)
async def test_a_code_that_cannot_be_sent_is_an_api_error(
    portal: FakePortal, session, result, message
) -> None:
    portal.initiate_result = result
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)

    with pytest.raises(MyEnergyApiError, match=message):
        await login.async_send_code("sms")
    assert portal.remoting[0]["method"] == "initiateMFA"


async def test_nothing_can_be_sent_before_the_password_step(portal: FakePortal, session) -> None:
    login = auth.MyEnergyLogin(session)

    with pytest.raises(MyEnergyApiError, match="no verification step"):
        await login.async_send_code("sms")
    with pytest.raises(MyEnergyApiError, match="no verification step"):
        await login.async_verify_code(CODE)
    assert portal.calls == []


async def test_a_rejected_code_without_a_message_gets_a_default(
    portal: FakePortal, session
) -> None:
    portal.code_message = None
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)
    await login.async_send_code("sms")

    with pytest.raises(MyEnergyInvalidCode, match="was not accepted"):
        await login.async_verify_code("000000")


async def test_a_pasted_code_is_trimmed_and_cut_to_six_digits(portal: FakePortal, session) -> None:
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)
    await login.async_send_code("sms")

    await login.async_verify_code(f"  {CODE}999  ")
    assert portal.remoting[-1]["data"][0] == CODE


@pytest.mark.parametrize(
    ("mode", "error", "match"),
    [
        ("http500", MyEnergyApiError, "initiateMFA failed with HTTP 500"),
        ("garbage", MyEnergyApiError, "Could not read the response to initiateMFA"),
        ("drop", MyEnergyCannotConnect, "Could not reach"),
    ],
)
async def test_remoting_failures_map_onto_the_exception_hierarchy(
    portal: FakePortal, session, mode, error, match
) -> None:
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)
    portal.remoting_mode = mode

    with pytest.raises(error, match=match):
        await login.async_send_code("sms")


async def test_a_dropped_connection_on_the_final_form_post_is_cannot_connect(
    portal: FakePortal, session
) -> None:
    login = auth.MyEnergyLogin(session)
    await login.async_submit_credentials(EMAIL, PASSWORD)
    await login.async_send_code("sms")
    portal.mfa_post_mode = "drop"

    with pytest.raises(MyEnergyCannotConnect):
        await login.async_verify_code(CODE)
    assert "usage_page" not in portal.calls


async def test_a_signed_in_page_without_a_session_cookie_is_an_api_error(
    portal: FakePortal, session
) -> None:
    """The usage page loads, but the jar has no ``sid``: the sign-in cannot be called complete."""
    portal.mfa_done = True

    with pytest.raises(MyEnergyApiError, match="no session cookie"):
        await auth.MyEnergyLogin(session).async_finish()


async def test_a_dropped_connection_at_frontdoor_is_cannot_connect(
    portal: FakePortal, session
) -> None:
    portal.frontdoor_mode = "drop"

    with pytest.raises(MyEnergyCannotConnect):
        await auth.MyEnergyLogin(session).async_submit_credentials(EMAIL, PASSWORD)


async def test_an_account_that_lands_on_the_home_page_needs_no_verification_step(
    portal: FakePortal, session
) -> None:
    portal.frontdoor_mode = "home"
    login = auth.MyEnergyLogin(session)

    assert await login.async_submit_credentials(EMAIL, PASSWORD) is None
    assert login.mfa_page is None
    assert portal.calls == ["login_page", "aura", "frontdoor", "home"]


async def test_a_sign_in_that_stops_somewhere_else_is_an_api_error(
    portal: FakePortal, session
) -> None:
    portal.frontdoor_mode = "stuck"

    with pytest.raises(MyEnergyApiError, match="stopped at .*frontdoor.jsp.*verification page"):
        await auth.MyEnergyLogin(session).async_submit_credentials(EMAIL, PASSWORD)


async def test_a_sign_in_that_redirects_forever_gives_up(portal: FakePortal, session) -> None:
    portal.frontdoor_mode = "loop"

    with pytest.raises(MyEnergyApiError, match="too many times"):
        await auth.MyEnergyLogin(session).async_submit_credentials(EMAIL, PASSWORD)
    assert portal.calls.count("frontdoor") == aura.MAX_CLIENT_REDIRECTS
