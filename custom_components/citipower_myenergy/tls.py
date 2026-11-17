"""HTTP sessions that can verify the myEnergy portal's certificate chain.

The portal serves only its leaf certificate, signed by the DigiCert
intermediate "DigiCert Global G2 TLS RSA SHA256 2020 CA1", and leaves out the
intermediate. Browsers and Windows fetch it on demand (AIA); Python/OpenSSL on
Linux does not, so verification fails with "unable to get local issuer
certificate". We ship the public intermediate and add it to the normal trust
store. Verification stays on: the chain must still end at a trusted root.
"""

from __future__ import annotations

import ssl
import weakref
from pathlib import Path

import aiohttp
import certifi
from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import HomeAssistant

# Valid until 2031-03-29. If DigiCert rotates it, sign-in fails with the same
# "unable to get local issuer certificate" message; replace this file.
INTERMEDIATE_PEM = (
    Path(__file__).parent / "certs" / "digicert_global_g2_tls_rsa_sha256_2020_ca1.pem"
)

_CONNECTOR_KEY = "citipower_myenergy_connector"
_SESSIONS_KEY = "citipower_myenergy_sessions"


def build_ssl_context() -> ssl.SSLContext:
    """Return the default trust store plus the portal's intermediate (blocking I/O)."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cafile=str(INTERMEDIATE_PEM))
    return context


async def async_create_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    """Create a session with its own cookie jar and the extended trust store.

    The connector (and so the SSL context) is shared across sessions; each
    session still has a separate cookie jar. Callers close their session when
    they are done (entry unload, end of the config flow). One shutdown listener
    closes any still open, so reloads do not pile up listeners or sessions.
    """
    connector = hass.data.get(_CONNECTOR_KEY)
    if connector is None or connector.closed:
        context = await hass.async_add_executor_job(build_ssl_context)
        connector = aiohttp.TCPConnector(ssl=context)
        hass.data[_CONNECTOR_KEY] = connector

    open_sessions: weakref.WeakSet[aiohttp.ClientSession] | None = hass.data.get(_SESSIONS_KEY)
    if open_sessions is None:
        open_sessions = hass.data[_SESSIONS_KEY] = weakref.WeakSet()

        async def _close_all(_event) -> None:
            for open_session in list(open_sessions):
                await open_session.close()
            await hass.data[_CONNECTOR_KEY].close()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, _close_all)

    session = aiohttp.ClientSession(connector=connector, connector_owner=False)
    open_sessions.add(session)
    return session
