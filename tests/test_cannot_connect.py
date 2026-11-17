"""cannot_connect never echoes aiohttp's own message: it carries the URL with its sid."""

from __future__ import annotations

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.citipower_myenergy.exceptions import (
    MyEnergyCannotConnect,
    cannot_connect,
)

URL_WITH_SECRET = (
    "https://portal.example/secur/frontdoor.jsp?sid=SECRET123&retURL=%2Fs%2F&c=TOKEN456"
)
SECRETS = ("SECRET123", "TOKEN456", "sid=", "retURL")


def _info() -> aiohttp.RequestInfo:
    url = URL(URL_WITH_SECRET)
    return aiohttp.RequestInfo(url, "GET", CIMultiDictProxy(CIMultiDict()), url)


def _errors() -> list[BaseException]:
    return [
        aiohttp.TooManyRedirects(_info(), ()),
        aiohttp.ClientResponseError(_info(), (), status=503, message="Service Unavailable"),
    ]


@pytest.mark.parametrize("err", _errors(), ids=lambda e: type(e).__name__)
def test_the_message_has_no_sid_or_query(err: BaseException) -> None:
    # Prove the premise: aiohttp's own text really does carry the secret.
    assert "SECRET123" in str(err)
    result = cannot_connect(err)
    assert isinstance(result, MyEnergyCannotConnect)
    for secret in SECRETS:
        assert secret not in str(result)
    assert type(err).__name__ in str(result)
    assert "portal.example/secur/frontdoor.jsp" in str(result)


def test_a_response_error_reports_its_status() -> None:
    assert "HTTP 503" in str(cannot_connect(_errors()[1]))


def test_a_plain_timeout_still_says_it_timed_out() -> None:
    assert str(cannot_connect(TimeoutError())) == (
        "Could not reach the myEnergy portal: the request timed out"
    )


def test_an_error_without_a_url_names_its_type() -> None:
    assert "ClientConnectionError" in str(cannot_connect(aiohttp.ClientConnectionError("x")))
