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


def cannot_connect(err: BaseException) -> MyEnergyCannotConnect:
    """Describe a transport failure, including timeouts that carry no message."""
    reason = str(err) or (
        "the request timed out" if isinstance(err, TimeoutError) else type(err).__name__
    )
    return MyEnergyCannotConnect(f"Could not reach the myEnergy portal: {reason}")
