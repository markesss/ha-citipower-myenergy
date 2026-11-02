"""HTTP client for the myEnergy Aura endpoint.

myEnergy has no public API. The browser talks to Salesforce's Aura endpoint,
calling @AuraEnabled Apex methods, and each call needs the framework id
(``fwuid``) and a CSRF token on top of the session cookies. Both are read from
a signed-in page: the token sits in a ``__Host-ERIC_*`` cookie whose name the
page announces as ``eikoocnekot``.

Adapted from gheydon/ha-yvw (MIT).
"""

from __future__ import annotations

import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin

import aiohttp
from yarl import URL

from .const import (
    AURA_APP,
    AURA_ENDPOINT,
    BASE_URL,
    BROWSER_HEADERS,
    IGNORED_COOKIE_PREFIXES,
    USAGE_PAGE,
)
from .exceptions import (
    MyEnergyApiError,
    MyEnergyAuthError,
    MyEnergyCannotConnect,
    cannot_connect,
)
from .portal import (
    AuraContext,
    build_context,
    build_message,
    describe_url,
    extract_descriptor,
    extract_return_value,
    find_client_redirect,
    parse_aura_body,
    token_cookie_name,
)

_LOGGER = logging.getLogger(__name__)

MAX_CLIENT_REDIRECTS = 6
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Errors that mean the context is stale rather than the request being wrong.
_STALE_CONTEXT_HINTS = ("clientoutofsync", "refresh the page", "process your request")


def page_headers() -> dict[str, str]:
    """Headers for a page navigation."""
    return {
        **BROWSER_HEADERS,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }


def xhr_headers(referer: str = USAGE_PAGE, content_type: str | None = None) -> dict[str, str]:
    """Headers for a background request from a page."""
    return {
        **BROWSER_HEADERS,
        "Accept": "*/*",
        "Content-Type": content_type or "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": BASE_URL,
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def read_cookie(session: aiohttp.ClientSession, name: str) -> str | None:
    """Return a cookie's value from the session's jar."""
    for cookie in session.cookie_jar:
        if cookie.key == name:
            return cookie.value
    return None


def session_cookies(session: aiohttp.ClientSession) -> dict[str, str]:
    """Return the cookies worth keeping to restore this session later."""
    return {
        cookie.key: cookie.value
        for cookie in session.cookie_jar
        if not cookie.key.startswith(IGNORED_COOKIE_PREFIXES)
    }


def _is_login_url(url: str) -> bool:
    return "/s/login" in url or "/secur/login" in url or "CommunitiesLanding" in url


async def async_get_page(session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
    """Load a page, following script redirects. Returns (html, final url)."""
    html, final_url = "", url
    for _ in range(MAX_CLIENT_REDIRECTS):
        try:
            async with session.get(
                url, headers=page_headers(), timeout=REQUEST_TIMEOUT, allow_redirects=True
            ) as response:
                html = await response.text()
                final_url = str(response.url)
        except aiohttp.TooManyRedirects as err:
            raise MyEnergyAuthError("The portal keeps redirecting; the session has ended") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise cannot_connect(err) from err

        redirect = find_client_redirect(html)
        if redirect is None:
            return html, final_url
        _LOGGER.debug("Following a client-side redirect to %s", describe_url(redirect))
        url = urljoin(final_url, redirect)
    raise MyEnergyApiError("The portal redirected too many times to follow")


async def async_load_page_context(
    session: aiohttp.ClientSession, url: str, app: str = AURA_APP
) -> AuraContext:
    """Load a community page and take the Aura context and CSRF token from it."""
    html, final_url = await async_get_page(session, url)
    if _is_login_url(final_url) and not _is_login_url(url):
        raise MyEnergyAuthError("Session expired: the portal sent us to the login page")

    cookie_name = token_cookie_name(html)
    if cookie_name is None:
        # The login page has no token; the browser sends the literal "null".
        token = "null"
    else:
        token = read_cookie(session, cookie_name) or ""
        if not token:
            raise MyEnergyApiError("The portal named a CSRF token cookie but did not set it")

    try:
        descriptor = extract_descriptor(html)
    except MyEnergyApiError as err:
        raise MyEnergyApiError(f"{err} (loaded {describe_url(final_url)})") from err
    return AuraContext(context=build_context(descriptor, app), token=token)


async def async_post_aura(
    session: aiohttp.ClientSession,
    aura: AuraContext,
    classname: str,
    method: str,
    params: dict[str, Any] | None,
    page_uri: str,
    referer: str,
    request_number: int = 1,
) -> Any:
    """Post one Apex action to the Aura endpoint and return its result."""
    payload = {
        "message": build_message(classname, method, params),
        "aura.context": json.dumps(aura.context),
        "aura.pageURI": page_uri,
        "aura.token": aura.token,
    }
    try:
        async with session.post(
            AURA_ENDPOINT,
            params={"r": str(request_number), "aura.ApexAction.execute": "1"},
            data=payload,
            headers=xhr_headers(referer=referer),
            timeout=REQUEST_TIMEOUT,
        ) as response:
            text = await response.text()
            status = response.status
    except (aiohttp.ClientError, TimeoutError) as err:
        raise cannot_connect(err) from err

    if status in (401, 403):
        raise MyEnergyAuthError(f"The portal refused the request (HTTP {status})")
    if status >= 500 and not text.strip():
        raise MyEnergyCannotConnect(f"The portal returned HTTP {status}")
    return extract_return_value(parse_aura_body(text))


class MyEnergyAuraClient:
    """Issue Apex calls against the myEnergy Aura endpoint with a kept session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cookies: dict[str, str],
        context: AuraContext | None = None,
        on_change: Callable[[AuraContext | None, dict[str, str]], Awaitable[None]] | None = None,
    ) -> None:
        """Seed the jar with the cookies kept from sign-in."""
        self._session = session
        self._session.cookie_jar.update_cookies(cookies, response_url=URL(BASE_URL))
        self._aura = context
        self._on_change = on_change
        self._counter = itertools.count(1)

    @property
    def session(self) -> aiohttp.ClientSession:
        """Return the underlying HTTP session."""
        return self._session

    @property
    def aura(self) -> AuraContext | None:
        """Return the Aura context in use, if one has been loaded."""
        return self._aura

    @property
    def cookies(self) -> dict[str, str]:
        """Return the session's current cookies, minus analytics."""
        return session_cookies(self._session)

    async def async_refresh(self) -> AuraContext:
        """Reload the usage page for a fresh framework id and token."""
        _LOGGER.debug("Loading a portal page for a fresh Aura context")
        self._aura = await async_load_page_context(self._session, USAGE_PAGE)
        await self._async_notify()
        return self._aura

    async def _async_notify(self) -> None:
        if self._on_change is not None:
            await self._on_change(self._aura, self.cookies)

    async def async_get_text(self, url: str) -> str | None:
        """Fetch a small authenticated resource, or None if unavailable."""
        try:
            async with self._session.get(
                url,
                headers={**BROWSER_HEADERS, "Accept": "*/*", "Referer": USAGE_PAGE},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return None
                return (await response.text())[:500]
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def async_invoke(
        self, classname: str, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Call an @AuraEnabled Apex method, refreshing the context once if stale."""
        if self._aura is None:
            await self.async_refresh()
        try:
            return await self._async_invoke_once(classname, method, params)
        except (MyEnergyApiError, MyEnergyAuthError) as err:
            if not isinstance(err, MyEnergyAuthError) and not any(
                hint in str(err).casefold() for hint in _STALE_CONTEXT_HINTS
            ):
                raise
            held = self._aura
            _LOGGER.debug("Retrying %s.%s after refreshing the Aura context", classname, method)
            try:
                await self.async_refresh()
            except MyEnergyAuthError:
                # Loading a page can fail where the endpoint would still accept
                # the context in hand; only give up if that fails too.
                if held is None:
                    raise
                self._aura = held
            return await self._async_invoke_once(classname, method, params)

    async def _async_invoke_once(
        self, classname: str, method: str, params: dict[str, Any] | None
    ) -> Any:
        assert self._aura is not None
        return await async_post_aura(
            self._session,
            self._aura,
            classname,
            method,
            params,
            page_uri="/s/energy-usage",
            referer=USAGE_PAGE,
            request_number=next(self._counter),
        )
