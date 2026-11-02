"""Sign in to myEnergy, including its multi-factor step.

The sequence the browser follows, and this replays:

1. ``LightningLoginFormController.communityLogin`` over Aura with the email
   (plus the network's ``.VPN`` suffix) and password. It returns a
   ``frontdoor.jsp`` URL carrying a one-time session id.
2. ``frontdoor.jsp`` trades that for session cookies and script-redirects to
   the Visualforce page ``/apex/MFA_Enabler``.
3. That page offers email or SMS. Choosing calls
   ``MFAEnablerController.initiateMFA`` by Visualforce remoting, which sends the
   code and returns a verification id.
4. ``MFAEnablerController.verifyMFA(code, id, isRegistration, method)`` checks
   the code, then the page posts its form back to ``/MFA_Enabler``, which
   completes the login flow and redirects to ``/s/``.

The password is used once and never stored. What comes out is the cookie jar
and an Aura context, which is all later requests need.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import aiohttp

from .aura import (
    MAX_CLIENT_REDIRECTS,
    REQUEST_TIMEOUT,
    async_load_page_context,
    async_post_aura,
    page_headers,
    read_cookie,
    session_cookies,
    xhr_headers,
)
from .const import (
    APEX_LOGIN_CLASS,
    APEX_LOGIN_METHOD,
    BASE_URL,
    HOME_PAGE,
    LOGIN_AURA_APP,
    LOGIN_PAGE,
    MFA_INITIATE,
    MFA_METHOD_EMAIL,
    MFA_VERIFY,
    REMOTING_ENDPOINT,
    USAGE_PAGE,
    USERNAME_SUFFIX,
)
from .exceptions import (
    MyEnergyApiError,
    MyEnergyAuthError,
    MyEnergyInvalidAuth,
    MyEnergyInvalidCode,
    cannot_connect,
)
from .portal import (
    AuraContext,
    MfaPage,
    build_remoting_call,
    describe_url,
    find_client_redirect,
    is_mfa_page,
    login_error,
    login_redirect,
    parse_mfa_page,
    parse_remoting_result,
)

_LOGGER = logging.getLogger(__name__)


def portal_username(email: str) -> str:
    """Return the username the portal expects for an email address."""
    email = email.strip()
    if email.lower().endswith(USERNAME_SUFFIX.lower()):
        return email
    return f"{email}{USERNAME_SUFFIX}"


@dataclass(slots=True)
class SignedIn:
    """The outcome of a completed sign-in."""

    cookies: dict[str, str]
    context: AuraContext


class MyEnergyLogin:
    """Drive the interactive sign-in and hand back a usable session."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Use this session's cookie jar to collect the login cookies."""
        self._session = session
        self._mfa_page: MfaPage | None = None
        self._mfa_url: str | None = None
        self._method: str = MFA_METHOD_EMAIL
        self._verification_id: str | None = None
        self._is_registration = False
        self._tid = 1

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the HTTP session holding the sign-in cookies."""
        return self._session

    @property
    def mfa_page(self) -> MfaPage | None:
        """Return the verification page reached after the password, if any."""
        return self._mfa_page

    @property
    def method(self) -> str:
        """Return the verification method last used."""
        return self._method

    async def async_submit_credentials(self, email: str, password: str) -> MfaPage | None:
        """Post the password; return the MFA page, or None if none was needed."""
        login_context = await async_load_page_context(self._session, LOGIN_PAGE, LOGIN_AURA_APP)
        try:
            result = await async_post_aura(
                self._session,
                login_context,
                APEX_LOGIN_CLASS,
                APEX_LOGIN_METHOD,
                {"username": portal_username(email), "password": password, "startUrl": "/s/"},
                page_uri="/s/login/",
                referer=LOGIN_PAGE,
            )
        except (MyEnergyApiError, MyEnergyAuthError) as err:
            raise MyEnergyInvalidAuth(str(err)) from err

        frontdoor = login_redirect(result)
        if frontdoor is None:
            raise MyEnergyInvalidAuth(
                login_error(result) or "The portal rejected that email address or password"
            )
        return await self._async_follow_to_mfa(frontdoor)

    async def async_send_code(self, method: str) -> None:
        """Ask the portal to send a verification code by email or SMS."""
        page = self._require_page()
        self._method = method
        result = await self._async_remote(page, MFA_INITIATE, [method])
        if not result.get("success"):
            raise MyEnergyApiError(
                result.get("message") or f"The portal could not send a code by {method}"
            )
        self._verification_id = result.get("verificationIdentifier")
        self._is_registration = bool(result.get("isRegistration"))
        if self._is_registration:
            # A first-time MFA enrolment uses a different method whose
            # arguments have not been seen; doing it once in a browser avoids
            # guessing.
            raise MyEnergyApiError(
                "This account has not finished setting up multi-factor "
                "authentication. Sign in once at myenergy.powercor.com.au, then try again."
            )

    async def async_verify_code(self, code: str) -> SignedIn:
        """Submit the code, complete the sign-in and return the session."""
        page = self._require_page()
        if self._verification_id is None:
            raise MyEnergyApiError("No verification code has been requested yet")

        result = await self._async_remote(
            page,
            MFA_VERIFY,
            [code.strip()[:6], self._verification_id, self._is_registration, self._method],
        )
        if not result.get("success"):
            raise MyEnergyInvalidCode(result.get("message") or "That code was not accepted")

        # verifyMFA only checks the code; posting the page's form is what
        # tells Salesforce the login flow is finished.
        target = urljoin(BASE_URL, page.final_form_action)
        try:
            async with self._session.post(
                target,
                data=page.final_form_fields,
                headers={
                    **page_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": BASE_URL,
                    "Referer": self._mfa_url or target,
                },
                timeout=REQUEST_TIMEOUT,
            ) as response:
                html = await response.text()
                landed = str(response.url)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise cannot_connect(err) from err

        _LOGGER.debug(
            "MFA form post landed on %s, redirecting to %s",
            describe_url(landed),
            describe_url(find_client_redirect(html)),
        )
        return await self.async_finish()

    async def async_finish(self) -> SignedIn:
        """Confirm the session reaches a signed-in page and return it."""
        try:
            context = await async_load_page_context(self._session, USAGE_PAGE)
        except MyEnergyAuthError as err:
            raise MyEnergyInvalidCode(
                "The portal did not complete the sign-in after the code was accepted"
            ) from err
        if not read_cookie(self._session, "sid"):
            raise MyEnergyApiError("Signed in, but the portal issued no session cookie")
        return SignedIn(cookies=session_cookies(self._session), context=context)

    def _require_page(self) -> MfaPage:
        if self._mfa_page is None:
            raise MyEnergyApiError("There is no verification step in progress")
        return self._mfa_page

    async def _async_follow_to_mfa(self, url: str) -> MfaPage | None:
        """Follow frontdoor.jsp's script redirects until the MFA page."""
        for hop in range(MAX_CLIENT_REDIRECTS):
            try:
                async with self._session.get(
                    url, headers=page_headers(), timeout=REQUEST_TIMEOUT, allow_redirects=True
                ) as response:
                    html = await response.text()
                    final_url = str(response.url)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise cannot_connect(err) from err

            _LOGGER.debug("Sign-in hop %s landed on %s", hop, describe_url(final_url))
            if is_mfa_page(html):
                self._mfa_page = parse_mfa_page(html)
                self._mfa_url = final_url
                return self._mfa_page

            redirect = find_client_redirect(html)
            if redirect is not None:
                url = urljoin(final_url, redirect)
                continue

            if final_url.startswith(HOME_PAGE) and read_cookie(self._session, "sid"):
                _LOGGER.debug("Signed in without a verification step")
                return None
            raise MyEnergyApiError(
                f"The sign-in stopped at {describe_url(final_url)} without reaching "
                "the verification page"
            )
        raise MyEnergyApiError("The sign-in redirected too many times to follow")

    async def _async_remote(self, page: MfaPage, method: str, data: list[Any]) -> dict[str, Any]:
        """Make a Visualforce remoting call from the MFA page."""
        self._tid += 1
        body = build_remoting_call(page, method, data, self._tid)
        headers = {
            **xhr_headers(referer=self._mfa_url or BASE_URL, content_type="application/json"),
            "X-Requested-With": "XMLHttpRequest",
            "X-User-Agent": "Visualforce-Remoting",
        }
        try:
            async with self._session.post(
                REMOTING_ENDPOINT, json=body, headers=headers, timeout=REQUEST_TIMEOUT
            ) as response:
                if response.status != 200:
                    raise MyEnergyApiError(f"{method} failed with HTTP {response.status}")
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise cannot_connect(err) from err
        except ValueError as err:
            raise MyEnergyApiError(f"Could not read the response to {method}") from err
        return parse_remoting_result(payload, method)
