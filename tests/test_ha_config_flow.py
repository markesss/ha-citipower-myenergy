"""Tests for the config flow (needs pytest-homeassistant-custom-component)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.citipower_myenergy.auth import SignedIn
from custom_components.citipower_myenergy.const import (
    CONF_COMPANY,
    CONF_COOKIES,
    CONF_NMI,
    CONF_SIGNED_IN_AT,
    DOMAIN,
)
from custom_components.citipower_myenergy.exceptions import (
    MyEnergyInvalidAuth,
    MyEnergyInvalidCode,
)
from custom_components.citipower_myenergy.models import AccountInfo, NmiInfo
from custom_components.citipower_myenergy.portal import AuraContext, parse_mfa_page

MFA_HTML = (Path(__file__).parent / "fixtures" / "mfa_enabler.html").read_text()
EMAIL = "someone@example.invalid"
PASSWORD = "correct-horse-battery-staple"
NMI = NmiInfo(
    assign_id="a1B000000000001",
    nmi_id="a1C000000000001",
    nmi="61020000001",
    description="Home",
    address="1 EXAMPLE STREET MELBOURNE VIC 3000",
    active=True,
)
SIGNED_IN = SignedIn(
    cookies={"sid": "session-value", "sid_Client": "paired"},
    context=AuraContext(context={"fwuid": "x"}, token="t"),
)


def _login(**kwargs) -> MagicMock:
    login = MagicMock()
    login.mfa_page = parse_mfa_page(MFA_HTML)
    login.method = "sms"
    login.async_submit_credentials = AsyncMock(return_value=login.mfa_page)
    login.async_send_code = AsyncMock()
    login.async_verify_code = AsyncMock(return_value=SIGNED_IN)
    login.async_finish = AsyncMock(return_value=SIGNED_IN)
    for key, value in kwargs.items():
        setattr(login, key, value)
    return login


def _api(nmis: list[NmiInfo] | None = None) -> MagicMock:
    api = MagicMock()
    api.async_list_nmis = AsyncMock(return_value=[NMI] if nmis is None else nmis)
    api.async_account_info = AsyncMock(
        return_value=AccountInfo(
            company="Citipower", start=date(2025, 6, 18), end=date(2026, 10, 31), has_access=True
        )
    )
    return api


def _patches(login: MagicMock, api: MagicMock):
    return (
        patch("custom_components.citipower_myenergy.config_flow.MyEnergyLogin", return_value=login),
        patch("custom_components.citipower_myenergy.config_flow.MyEnergyApi", return_value=api),
        patch("custom_components.citipower_myenergy.config_flow.MyEnergyAuraClient"),
        patch("custom_components.citipower_myenergy.async_setup_entry", return_value=True),
    )


async def _sign_in(hass: HomeAssistant, flow_id: str, code: str = "123456"):
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "method"
    result = await hass.config_entries.flow.async_configure(flow_id, {"mfa_method": "sms"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "code"
    return await hass.config_entries.flow.async_configure(flow_id, {"code": code, "resend": False})


async def test_full_sign_in_creates_entry(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Password, SMS, code: the whole path creates an entry for the NMI."""
    login, api = _login(), _api()
    p1, p2, p3, p4 = _patches(login, api)
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await _sign_in(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "myEnergy Home"
    assert result["data"][CONF_NMI] == "61020000001"
    assert result["data"][CONF_COMPANY] == "Citipower"
    assert result["data"][CONF_COOKIES] == SIGNED_IN.cookies
    assert result["data"][CONF_SIGNED_IN_AT]
    login.async_send_code.assert_awaited_once_with("sms")
    login.async_verify_code.assert_awaited_once_with("123456")
    # The password is never kept.
    assert PASSWORD not in repr(result["data"])


async def test_bad_password(recorder_mock: Recorder, hass: HomeAssistant, custom_integration):
    """A refused password stays on the first step with an error."""
    login = _login(async_submit_credentials=AsyncMock(side_effect=MyEnergyInvalidAuth("no")))
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: "wrong"}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_bad_code_can_be_retried(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """A wrong code shows an error and accepts another attempt."""
    login = _login(
        async_verify_code=AsyncMock(side_effect=[MyEnergyInvalidCode("Invalid code"), SIGNED_IN])
    )
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await _sign_in(hass, result["flow_id"], code="000000")
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_code"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456", "resend": False}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_resend(recorder_mock: Recorder, hass: HomeAssistant, custom_integration):
    """Ticking resend asks for a new code with the same method."""
    login = _login()
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        flow_id = result["flow_id"]
        await hass.config_entries.flow.async_configure(
            flow_id, {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
        )
        await hass.config_entries.flow.async_configure(flow_id, {"mfa_method": "sms"})
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"code": "", "resend": True}
        )
    assert result["errors"] == {"base": "code_resent"}
    assert login.async_send_code.await_count == 2


async def test_multiple_nmis_ask_which(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """With more than one NMI, the user picks one."""
    other = NmiInfo(
        assign_id="a1B2",
        nmi_id="a1C2",
        nmi="61029999999",
        description="Shack",
        address="2 OTHER ST",
        active=True,
    )
    p1, p2, p3, p4 = _patches(_login(), _api([NMI, other]))
    with p1, p2, p3, p4:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await _sign_in(hass, result["flow_id"])
        assert result["step_id"] == "nmi"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NMI: "61029999999"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_NMI] == "61029999999"


async def test_reauth_updates_cookies(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Signing in again refreshes the session on the existing entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="61020000001",
        data={CONF_NMI: "61020000001", CONF_COOKIES: {"sid": "old"}, CONF_SIGNED_IN_AT: "x"},
    )
    entry.add_to_hass(hass)
    p1, p2, p3, p4 = _patches(_login(), _api())
    with p1, p2, p3, p4:
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await _sign_in(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_COOKIES] == SIGNED_IN.cookies
