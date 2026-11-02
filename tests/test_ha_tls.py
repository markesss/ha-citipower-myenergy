"""The portal omits its TLS intermediate; we ship it so verification can succeed."""

from __future__ import annotations

import ssl

from custom_components.citipower_myenergy.tls import (
    INTERMEDIATE_PEM,
    async_create_session,
    build_ssl_context,
)

INTERMEDIATE_CN = "DigiCert Global G2 TLS RSA SHA256 2020 CA1"


def _common_names(context: ssl.SSLContext) -> set[str]:
    return {
        value
        for cert in context.get_ca_certs()
        for rdn in cert["subject"]
        for key, value in rdn
        if key == "commonName"
    }


def test_context_trusts_the_shipped_intermediate_and_keeps_verification() -> None:
    context = build_ssl_context()
    assert INTERMEDIATE_PEM.is_file()
    assert INTERMEDIATE_CN in _common_names(context)
    # The default trust store is still there, so the chain must end at a real root.
    assert "DigiCert Global Root G2" in _common_names(context)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_sessions_share_a_connector_but_not_cookies(hass) -> None:
    first = await async_create_session(hass)
    second = await async_create_session(hass)
    assert first.connector is second.connector
    assert first.cookie_jar is not second.cookie_jar
    # A session drops its connector reference on close, so keep ours.
    connector = first.connector
    await first.close()
    await second.close()
    # Closing a session must not close the connector the others still use.
    assert not connector.closed
    await connector.close()
