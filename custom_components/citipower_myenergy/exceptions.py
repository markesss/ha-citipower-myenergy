"""Exceptions raised by the myEnergy integration.

These deliberately do not depend on Home Assistant, so the portal client can be
exercised on its own.
"""

from __future__ import annotations


class MyEnergyError(Exception):
    """Base class for myEnergy errors."""


class MyEnergyCannotConnect(MyEnergyError):
    """The portal could not be reached."""


class MyEnergyAuthError(MyEnergyError):
    """The session is no longer valid and the user must sign in again."""


class MyEnergyInvalidAuth(MyEnergyError):
    """The email address or password was rejected."""


class MyEnergyInvalidCode(MyEnergyError):
    """The verification code was rejected."""


class MyEnergyApiError(MyEnergyError):
    """The portal returned something unexpected."""


class TariffError(MyEnergyError):
    """A tariff is malformed or cannot be applied to the data given."""


def _masked_url(err: BaseException) -> str | None:
    """Return the failed request's URL without its query, if the error carries one.

    aiohttp's own message embeds the full URL, and frontdoor.jsp URLs carry a live session id
    in the query string, so the message must never be used. Duck-typed to keep this module free
    of aiohttp imports.
    """
    from .portal import describe_url  # portal imports this module; import late to stay acyclic

    info = getattr(err, "request_info", None)
    url = getattr(info, "real_url", None) or getattr(info, "url", None)
    # Drop the query altogether: not even its parameter names belong in a message.
    return describe_url(str(url).split("?", 1)[0].split("#", 1)[0]) if url else None


def cannot_connect(err: BaseException) -> MyEnergyCannotConnect:
    """Describe a transport failure without ever echoing the error's own text.

    Raise the result ``from None``: the original error's text carries the URL (and its
    ``sid`` query value), so it must not reach a chained traceback in the logs.
    """
    if isinstance(err, TimeoutError):
        reason = "the request timed out"
    else:
        reason = type(err).__name__
        status = getattr(err, "status", None)
        if isinstance(status, int):
            reason += f" (HTTP {status})"
        url = _masked_url(err)
        if url:
            reason += f" for {url}"
    return MyEnergyCannotConnect(f"Could not reach the myEnergy portal: {reason}")
