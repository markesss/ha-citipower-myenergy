"""Tests for the config flow (needs pytest-homeassistant-custom-component)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    CONF_KEEPALIVE_MINUTES,
    CONF_NMI,
    CONF_SIGNED_IN_AT,
    DOMAIN,
)
from custom_components.citipower_myenergy.exceptions import (
    MyEnergyCannotConnect,
    MyEnergyError,
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


# --- Error paths, each followed by the recovery -------------------------------------------


def _mfa_page():
    return parse_mfa_page(MFA_HTML)


async def _start(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return result["flow_id"]


async def _credentials(hass: HomeAssistant, flow_id: str):
    return await hass.config_entries.flow.async_configure(
        flow_id, {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    )


async def _to_code(hass: HomeAssistant, flow_id: str):
    """Walk credentials and method so the next step is the code form."""
    await _credentials(hass, flow_id)
    return await hass.config_entries.flow.async_configure(flow_id, {"mfa_method": "sms"})


async def _code(hass: HomeAssistant, flow_id: str, code: str = "123456", resend: bool = False):
    return await hass.config_entries.flow.async_configure(flow_id, {"code": code, "resend": resend})


def _entry(**kwargs) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=kwargs.pop("unique_id", NMI.nmi),
        data={CONF_NMI: NMI.nmi, CONF_COOKIES: {"sid": "old"}, CONF_SIGNED_IN_AT: "x"},
        **kwargs,
    )


@pytest.mark.parametrize(
    ("error", "key"),
    [(MyEnergyCannotConnect("down"), "cannot_connect"), (MyEnergyError("odd"), "unknown")],
)
async def test_credentials_error_then_recovery(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, error, key
) -> None:
    """A network or unexpected failure on the password step shows an error; retrying works."""
    login = _login(async_submit_credentials=AsyncMock(side_effect=[error, _mfa_page()]))
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        flow_id = await _start(hass)
        result = await _credentials(hass, flow_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["errors"] == {"base": key}
        result = await _credentials(hass, flow_id)
        assert result["step_id"] == "method"
        await hass.config_entries.flow.async_configure(flow_id, {"mfa_method": "sms"})
        result = await _code(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_bad_password_then_recovery(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """After a refused password the user can enter the right one and finish."""
    login = _login(
        async_submit_credentials=AsyncMock(side_effect=[MyEnergyInvalidAuth("no"), _mfa_page()])
    )
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        flow_id = await _start(hass)
        result = await _credentials(hass, flow_id)
        assert result["errors"] == {"base": "invalid_auth"}
        await _credentials(hass, flow_id)
        await hass.config_entries.flow.async_configure(flow_id, {"mfa_method": "sms"})
        result = await _code(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_mfa_page_finishes_the_session_itself(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """If the portal signs in without asking for a code, the flow finishes the session."""
    login = _login(async_submit_credentials=AsyncMock(return_value=None))
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        result = await _credentials(hass, await _start(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    login.async_finish.assert_awaited_once()
    login.async_send_code.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (MyEnergyCannotConnect("down"), "cannot_connect"),
        (MyEnergyError("could not send"), "send_failed"),
    ],
)
async def test_send_code_error_then_recovery(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, error, key
) -> None:
    """A failed code request shows an error on the method step; choosing again succeeds."""
    login = _login(async_send_code=AsyncMock(side_effect=[error, None]))
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        flow_id = await _start(hass)
        await _credentials(hass, flow_id)
        result = await hass.config_entries.flow.async_configure(flow_id, {"mfa_method": "sms"})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "method"
        assert result["errors"] == {"base": key}
        result = await hass.config_entries.flow.async_configure(flow_id, {"mfa_method": "email"})
        assert result["step_id"] == "code"
        result = await _code(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert login.async_send_code.await_args_list[-1].args == ("email",)


async def test_single_method_is_sent_without_asking(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """With one way to receive a code the choice is skipped; a failure shows the method form."""
    page = replace(_mfa_page(), has_sms=False)
    assert page.options == ["email"]
    login = _login(
        mfa_page=page,
        async_submit_credentials=AsyncMock(return_value=page),
        async_send_code=AsyncMock(side_effect=[MyEnergyError("nope"), None]),
    )
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        flow_id = await _start(hass)
        result = await _credentials(hass, flow_id)
        assert result["step_id"] == "method"
        assert result["errors"] == {"base": "send_failed"}
        result = await hass.config_entries.flow.async_configure(flow_id, {"mfa_method": "email"})
        assert result["step_id"] == "code"
        result = await _code(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_short_code_is_refused_without_calling_the_portal(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """A code of the wrong length asks again; the portal is only asked once it is complete."""
    login = _login()
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        flow_id = await _start(hass)
        await _to_code(hass, flow_id)
        result = await _code(hass, flow_id, code="12")
        assert result["step_id"] == "code"
        assert result["errors"] == {"base": "code_required"}
        login.async_verify_code.assert_not_awaited()
        result = await _code(hass, flow_id, code="123 456")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    login.async_verify_code.assert_awaited_once_with("123456")


@pytest.mark.parametrize(
    ("error", "key"),
    [(MyEnergyCannotConnect("down"), "cannot_connect"), (MyEnergyError("odd"), "unknown")],
)
async def test_verify_code_error_then_recovery(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, error, key
) -> None:
    """A network or unexpected failure while verifying keeps the code form; retry works."""
    login = _login(async_verify_code=AsyncMock(side_effect=[error, SIGNED_IN]))
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        flow_id = await _start(hass)
        await _to_code(hass, flow_id)
        result = await _code(hass, flow_id)
        assert result["step_id"] == "code"
        assert result["errors"] == {"base": key}
        result = await _code(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    ("error", "key"),
    [(MyEnergyCannotConnect("down"), "cannot_connect"), (MyEnergyError("odd"), "unknown")],
)
async def test_resend_error_then_recovery(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, error, key
) -> None:
    """A failed resend is reported on the code form and does not end the flow."""
    login = _login(async_send_code=AsyncMock(side_effect=[None, error, None]))
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        flow_id = await _start(hass)
        await _to_code(hass, flow_id)
        result = await _code(hass, flow_id, code="", resend=True)
        assert result["errors"] == {"base": key}
        result = await _code(hass, flow_id, code="", resend=True)
        assert result["errors"] == {"base": "code_resent"}
        result = await _code(hass, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_email_code_wording(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The code form says where the code went (email versus SMS)."""
    login = _login(method="email")
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        result = await _to_code(hass, await _start(hass))
    assert result["description_placeholders"]["method"] == "email"


@pytest.mark.parametrize(
    ("error", "reason"),
    [(MyEnergyCannotConnect("down"), "cannot_connect"), (MyEnergyError("odd"), "unknown")],
)
async def test_listing_nmis_fails_then_a_new_flow_succeeds(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration, error, reason
) -> None:
    """If the NMI list cannot be read the flow aborts; starting again completes."""
    failing = _api()
    failing.async_list_nmis = AsyncMock(side_effect=error)
    p1, p2, p3, p4 = _patches(_login(), failing)
    with p1, p2, p3, p4:
        result = await _sign_in(hass, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason

    p1, p2, p3, p4 = _patches(_login(), _api())
    with p1, p2, p3, p4:
        result = await _sign_in(hass, await _start(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_finish_failure_aborts_then_a_new_flow_succeeds(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """With no code step, a failure completing the sign-in aborts; a fresh attempt works."""
    login = _login(
        async_submit_credentials=AsyncMock(return_value=None),
        async_finish=AsyncMock(side_effect=MyEnergyCannotConnect("down")),
    )
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        result = await _credentials(hass, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"

    p1, p2, p3, p4 = _patches(_login(), _api())
    with p1, p2, p3, p4:
        result = await _sign_in(hass, await _start(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_no_active_nmis_aborts_then_new_flow_succeeds(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Inactive NMIs are ignored; with none left the flow aborts, and a later one works."""
    inactive = replace(NMI, active=False)
    p1, p2, p3, p4 = _patches(_login(), _api([inactive]))
    with p1, p2, p3, p4:
        result = await _sign_in(hass, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_nmis"

    p1, p2, p3, p4 = _patches(_login(), _api([inactive, NMI]))
    with p1, p2, p3, p4:
        result = await _sign_in(hass, await _start(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_NMI] == NMI.nmi


async def test_account_info_failure_aborts_then_new_flow_succeeds(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Failing to read the account details aborts; a fresh attempt completes."""
    failing = _api()
    failing.async_account_info = AsyncMock(side_effect=MyEnergyError("odd"))
    p1, p2, p3, p4 = _patches(_login(), failing)
    with p1, p2, p3, p4:
        result = await _sign_in(hass, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown"

    p1, p2, p3, p4 = _patches(_login(), _api())
    with p1, p2, p3, p4:
        result = await _sign_in(hass, await _start(hass))
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_already_configured_updates_the_entry(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Signing in for an NMI that already has an entry aborts and refreshes its session."""
    entry = _entry()
    entry.add_to_hass(hass)
    p1, p2, p3, p4 = _patches(_login(), _api())
    with p1, p2, p3, p4:
        result = await _sign_in(hass, await _start(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_COOKIES] == SIGNED_IN.cookies


async def test_reauth_for_a_different_account_aborts_then_right_account_works(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Signing in as someone else must not overwrite the entry; the right login still can."""
    entry = _entry()
    entry.add_to_hass(hass)
    stranger = replace(NMI, nmi="61020000002")
    p1, p2, p3, p4 = _patches(_login(), _api([stranger]))
    with p1, p2, p3, p4:
        result = await entry.start_reauth_flow(hass)
        result = await _sign_in(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_COOKIES] == {"sid": "old"}

    p1, p2, p3, p4 = _patches(_login(), _api())
    with p1, p2, p3, p4:
        result = await entry.start_reauth_flow(hass)
        result = await _sign_in(hass, result["flow_id"])
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_COOKIES] == SIGNED_IN.cookies


async def test_reauth_unique_id_mismatch_aborts(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The entry's unique id is checked as well as its stored NMI before it is rewritten."""
    entry = _entry(unique_id="61020000002")
    entry.add_to_hass(hass)
    p1, p2, p3, p4 = _patches(_login(), _api())
    with p1, p2, p3, p4:
        result = await entry.start_reauth_flow(hass)
        result = await _sign_in(hass, result["flow_id"])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_COOKIES] == {"sid": "old"}


async def test_reauth_bad_password_then_recovery(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """On re-sign-in a wrong password re-shows the reauth form; the right one completes."""
    entry = _entry()
    entry.add_to_hass(hass)
    login = _login(
        async_submit_credentials=AsyncMock(side_effect=[MyEnergyInvalidAuth("no"), _mfa_page()])
    )
    p1, p2, p3, p4 = _patches(login, _api())
    with p1, p2, p3, p4:
        result = await entry.start_reauth_flow(hass)
        flow_id = result["flow_id"]
        result = await _credentials(hass, flow_id)
        assert result["step_id"] == "reauth_confirm"
        assert result["errors"] == {"base": "invalid_auth"}
        result = await _sign_in(hass, flow_id)
    assert result["reason"] == "reauth_successful"


# --- Options flow -------------------------------------------------------------------------


async def test_options_keepalive_is_saved_and_keeps_other_options(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The keep-alive step shows the current value and merges into the existing options."""
    entry = _entry(options={"tariff": {"kept": True}, CONF_KEEPALIVE_MINUTES: 30})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "keepalive" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "keepalive"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "keepalive"
    key = next(k for k in result["data_schema"].schema if k == CONF_KEEPALIVE_MINUTES)
    assert key.default() == 30
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_KEEPALIVE_MINUTES: 20}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_KEEPALIVE_MINUTES] == 20
    assert entry.options["tariff"] == {"kept": True}


async def test_options_keepalive_default_when_unset(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """With nothing saved the form offers the default interval."""
    entry = _entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "keepalive"}
    )
    key = next(k for k in result["data_schema"].schema if k == CONF_KEEPALIVE_MINUTES)
    assert key.default() == 45
