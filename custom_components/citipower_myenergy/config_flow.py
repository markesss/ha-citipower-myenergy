"""Config flow for CitiPower & Powercor myEnergy.

Mirrors the portal: email and password, then a choice of email or SMS for the
verification code, then the code. The password is used once and never stored;
the portal asks for a new code on every sign-in, so keeping it would buy
nothing.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import MyEnergyApi
from .aura import MyEnergyAuraClient
from .auth import MyEnergyLogin, SignedIn
from .const import (
    CONF_ADDRESS,
    CONF_CODE,
    CONF_COMPANY,
    CONF_COOKIES,
    CONF_DESCRIPTION,
    CONF_KEEPALIVE_MINUTES,
    CONF_MFA_METHOD,
    CONF_NMI,
    CONF_NMI_ASSIGN_ID,
    CONF_NMI_ID,
    CONF_RESEND,
    CONF_SIGNED_IN_AT,
    DEFAULT_KEEPALIVE_MINUTES,
    DOMAIN,
    MAX_KEEPALIVE_MINUTES,
    MFA_CODE_LENGTH,
    MFA_METHOD_EMAIL,
    MFA_METHOD_SMS,
    MIN_KEEPALIVE_MINUTES,
)
from .exceptions import (
    MyEnergyCannotConnect,
    MyEnergyError,
    MyEnergyInvalidAuth,
    MyEnergyInvalidCode,
)
from .models import NmiInfo
from .tls import async_create_session

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)

STEP_CODE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CODE, default=""): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
        ),
        vol.Optional(CONF_RESEND, default=False): bool,
    }
)


class MyEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the myEnergy config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Hold state that spans the sign-in steps."""
        self._login: MyEnergyLogin | None = None
        self._signed_in: SignedIn | None = None
        self._nmis: list[NmiInfo] = []
        self._code_error = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return MyEnergyOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the email and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._login = MyEnergyLogin(await async_create_session(self.hass))
            try:
                page = await self._login.async_submit_credentials(
                    user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                )
            except MyEnergyInvalidAuth:
                errors["base"] = "invalid_auth"
            except MyEnergyCannotConnect as err:
                _LOGGER.warning("Could not reach myEnergy: %s", err)
                errors["base"] = "cannot_connect"
            except MyEnergyError:
                _LOGGER.exception("Unexpected error signing in to myEnergy")
                errors["base"] = "unknown"
            else:
                if page is None:
                    return await self._async_signed_in()
                return await self.async_step_method()

        step_id = "reauth_confirm" if self.source == SOURCE_REAUTH else "user"
        return self.async_show_form(step_id=step_id, data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_method(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose where the verification code is sent."""
        assert self._login is not None and self._login.mfa_page is not None
        page = self._login.mfa_page
        errors: dict[str, str] = {}

        if user_input is not None or len(page.options) == 1:
            method = (user_input or {}).get(CONF_MFA_METHOD) or page.options[0]
            try:
                await self._login.async_send_code(method)
            except MyEnergyCannotConnect as err:
                _LOGGER.warning("Could not reach myEnergy: %s", err)
                errors["base"] = "cannot_connect"
            except MyEnergyError as err:
                _LOGGER.warning("myEnergy could not send a code by %s: %s", method, err)
                self._code_error = str(err)
                errors["base"] = "send_failed"
            else:
                return await self.async_step_code()

        labels = {
            MFA_METHOD_EMAIL: f"Email to {page.email_hint}" if page.email_hint else "Email",
            MFA_METHOD_SMS: f"SMS to {page.sms_hint}" if page.sms_hint else "SMS",
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_MFA_METHOD, default=page.options[0]): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=option, label=labels[option])
                            for option in page.options
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(
            step_id="method",
            data_schema=schema,
            errors=errors,
            description_placeholders={"detail": self._code_error},
        )

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the verification code."""
        assert self._login is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            code = "".join(ch for ch in user_input.get(CONF_CODE, "") if ch.isdigit())
            try:
                if user_input.get(CONF_RESEND):
                    await self._login.async_send_code(self._login.method)
                    errors["base"] = "code_resent"
                elif len(code) != MFA_CODE_LENGTH:
                    errors["base"] = "code_required"
                else:
                    self._signed_in = await self._login.async_verify_code(code)
                    return await self._async_signed_in()
            except MyEnergyInvalidCode as err:
                self._code_error = str(err)
                errors["base"] = "invalid_code"
            except MyEnergyCannotConnect as err:
                _LOGGER.warning("Could not reach myEnergy: %s", err)
                errors["base"] = "cannot_connect"
            except MyEnergyError:
                _LOGGER.exception("Unexpected error verifying the myEnergy code")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="code",
            data_schema=STEP_CODE_SCHEMA,
            errors=errors,
            description_placeholders={
                "method": "SMS" if self._login.method == MFA_METHOD_SMS else "email",
                "digits": str(MFA_CODE_LENGTH),
                "detail": self._code_error,
            },
        )

    async def _async_signed_in(self) -> ConfigFlowResult:
        """Find the NMIs behind the new session."""
        assert self._login is not None
        try:
            if self._signed_in is None:
                self._signed_in = await self._login.async_finish()
            self._nmis = [nmi for nmi in await self._api().async_list_nmis() if nmi.active]
        except MyEnergyCannotConnect as err:
            _LOGGER.warning("Could not reach myEnergy: %s", err)
            return self.async_abort(reason="cannot_connect")
        except MyEnergyError:
            _LOGGER.exception("Signed in to myEnergy but could not list the NMIs")
            return self.async_abort(reason="unknown")

        if not self._nmis:
            return self.async_abort(reason="no_nmis")

        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            match = next((n for n in self._nmis if n.nmi == entry.data[CONF_NMI]), None)
            if match is None:
                return self.async_abort(reason="wrong_account")
            return await self._async_create_or_update(match)

        if len(self._nmis) == 1:
            return await self._async_create_or_update(self._nmis[0])
        return await self.async_step_nmi()

    async def async_step_nmi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose which NMI to follow."""
        if user_input is not None:
            chosen = next(n for n in self._nmis if n.nmi == user_input[CONF_NMI])
            return await self._async_create_or_update(chosen)
        schema = vol.Schema(
            {
                vol.Required(CONF_NMI): SelectSelector(
                    SelectSelectorConfig(
                        options=[SelectOptionDict(value=n.nmi, label=n.label) for n in self._nmis],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="nmi", data_schema=schema)

    async def _async_create_or_update(self, nmi: NmiInfo) -> ConfigFlowResult:
        assert self._signed_in is not None
        try:
            account = await self._api().async_account_info(nmi.nmi)
        except MyEnergyError:
            _LOGGER.exception("Could not read the account details for the NMI")
            return self.async_abort(reason="unknown")

        data = {
            CONF_NMI: nmi.nmi,
            CONF_NMI_ID: nmi.nmi_id,
            CONF_NMI_ASSIGN_ID: nmi.assign_id,
            CONF_ADDRESS: nmi.address,
            CONF_DESCRIPTION: nmi.description,
            CONF_COMPANY: account.company,
            CONF_COOKIES: self._signed_in.cookies,
            CONF_SIGNED_IN_AT: dt_util.utcnow().isoformat(),
        }
        await self.async_set_unique_id(nmi.nmi)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)

        self._abort_if_unique_id_configured(updates=data)
        title = f"myEnergy {nmi.description or nmi.address or nmi.nmi}"
        return self.async_create_entry(title=title, data=data)

    def _api(self) -> MyEnergyApi:
        assert self._login is not None and self._signed_in is not None
        client = MyEnergyAuraClient(
            self._login.session, self._signed_in.cookies, context=self._signed_in.context
        )
        return MyEnergyApi(client)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start again when the session has ended."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=STEP_USER_SCHEMA)
        return await self.async_step_user(user_input)


class MyEnergyOptionsFlow(OptionsFlow):
    """Tune the keep-alive interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_KEEPALIVE_MINUTES: int(user_input[CONF_KEEPALIVE_MINUTES])}
            )
        current = self.config_entry.options.get(CONF_KEEPALIVE_MINUTES, DEFAULT_KEEPALIVE_MINUTES)
        schema = vol.Schema(
            {
                vol.Required(CONF_KEEPALIVE_MINUTES, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_KEEPALIVE_MINUTES,
                        max=MAX_KEEPALIVE_MINUTES,
                        step=1,
                        unit_of_measurement="min",
                        mode=NumberSelectorMode.BOX,
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
