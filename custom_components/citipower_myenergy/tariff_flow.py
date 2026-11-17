"""The tariff half of the options flow: set prices so cost statistics can be written.

Home Assistant has no table editor, so this is a menu-driven flow over a *draft* tariff
version held as plain dicts (see ``tariff_form``). Nothing is saved until the review step
shows the resolved hours and ``Tariff.from_dict`` accepts the result, so a half-finished
draft can never replace a working tariff.

Guided editing handles one flat rate per period. A stored period with tiers, an "all other
times" rule, or windows on different days is never rewritten by the guided steps: it is
listed as advanced, refused if you try to edit it, and left exactly as stored when other
parts of the tariff change. The advanced step edits the whole tariff as YAML for those.
"""

from __future__ import annotations

import copy
import logging
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from . import tariff_form as form
from .const import (
    CONF_NMI,
    CONF_PERIOD_STATS,
    CONF_SUPPLY_MODE,
    CONF_TARIFF,
    MARKET_TZ,
    SUPPLY_MODES,
)
from .exceptions import MyEnergyError, TariffError
from .reprice import FAR_PAST as _FAR_PAST
from .reprice import async_reprice_entry
from .session_store import get_session_store
from .statistics import async_recalculate_costs, period_statistic_ids
from .tariff import (
    DEFAULT_TIMEZONE,
    KIND_EXPORT,
    KIND_IMPORT,
    Tariff,
    daily_supply_dollars,
    flat_tariff,
    supply_mode_of,
    zone_for,
)

_LOGGER = logging.getLogger(__name__)

TYPE_FLAT = "flat"
TYPE_TIME_OF_USE = "tou"

# Reprice "all of history" when the clock changes: from before any stored reading.

# Errors from reading hand-edited YAML: TariffError covers the model, the rest are shapes
# the model never sees (a list where a mapping should be, and so on).
_BAD_INPUT = (TariffError, TypeError, ValueError, KeyError, AttributeError)


def _rate_selector() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=0, max=1000, step="any", unit_of_measurement="c/kWh", mode=NumberSelectorMode.BOX
        )
    )


def _suggest(name: str, value: Any = None, *, required: bool = True) -> vol.Marker:
    marker = vol.Required if required else vol.Optional
    return marker(name, description={"suggested_value": value})


class TariffOptionsFlow(OptionsFlow):
    """Steps that edit the electricity tariff; the menu that leads here is in config_flow."""

    def __init__(self) -> None:
        """Hold the draft that spans the steps."""
        super().__init__()
        self._others: list[dict[str, Any]] = []  # stored versions not being edited
        self._draft: dict[str, Any] = {}
        self._kind = KIND_IMPORT
        self._flat_setup = False
        self._export_applicable = False
        self._period_index: int | None = None
        self._pending: Tariff | None = None  # saved once the user answers the clock-change step
        # Options awaiting the supply-charge answer on the per-period statistics path.
        self._pending_options: dict[str, Any] | None = None

    # --- Helpers ---------------------------------------------------------------------

    def _stored_raw(self) -> Any:
        return self.config_entry.options.get(CONF_TARIFF)

    def _stored_tariff(self) -> Tariff | None:
        raw = self._stored_raw()
        if not raw:
            return None
        try:
            return Tariff.from_dict(raw)
        except _BAD_INPUT:
            return None

    def _today(self) -> date:
        """Return today's date on the clock the draft prices on (new tariffs: the default)."""
        try:
            zone = zone_for(self._draft.get("timezone") or DEFAULT_TIMEZONE)
        except TariffError:
            zone = MARKET_TZ
        return dt_util.now(zone).date()

    def _clock_note(self) -> str:
        """Return the sentence saying which clock the times on this step are on."""
        if self._draft:
            return form.timezone_note(form.draft_zone(self._draft))
        tariff = self._stored_tariff()
        return form.timezone_note(tariff.zone_name if tariff else DEFAULT_TIMEZONE)

    async def _async_prepare(self) -> None:
        """Decide whether export prices make sense: only for an account that exports."""
        store = get_session_store(self.hass)
        await store.async_load()
        suffixes = store.suffixes(self.config_entry.entry_id)
        stored = self._stored_tariff()
        had_export = stored is not None and any(v.export_periods for v in stored.versions)
        self._export_applicable = had_export or any(s.upper().startswith("B") for s in suffixes)

    def _all_versions(self) -> list[dict[str, Any]]:
        return sorted([*self._others, self._draft], key=lambda version: version["effective_from"])

    def _periods(self) -> list[dict[str, Any]]:
        return self._draft[form.periods_key(self._kind)]

    async def _async_finish(self, tariff: Tariff) -> ConfigFlowResult:
        """Save a tariff, first asking about history if it changed the pricing clock."""
        previous = self._stored_tariff()
        if previous is not None and previous.zone_name != tariff.zone_name:
            self._pending = tariff
            return await self.async_step_tariff_timezone_changed()
        return self._save(tariff)

    def _save(self, tariff: Tariff | None) -> ConfigFlowResult:
        """Store the tariff beside the other options (keep-alive must survive)."""
        options = dict(self.config_entry.options)
        if tariff is None:
            options.pop(CONF_TARIFF, None)
        else:
            options[CONF_TARIFF] = tariff.to_dict()
        return self.async_create_entry(title="", data=options)

    # --- Tariff menu -------------------------------------------------------------------

    async def async_step_tariff(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show what is set and what can be done about it."""
        self._draft = {}  # a stale draft from an earlier visit must not set the clock note
        tariff = self._stored_tariff()
        if tariff is not None:
            versions = tariff.to_dict()["versions"]
            summary = form.describe_versions(versions)
            options = [
                "tariff_edit_latest",
                "tariff_add_version",
                "tariff_recalculate",
                "tariff_period_stats",
                "tariff_advanced",
                "tariff_remove",
            ]
        else:
            summary = (
                "The saved tariff is not valid, so no costs are being recorded. Use the "
                "advanced editor to see what is wrong, or start again."
                if self._stored_raw()
                else "No tariff is set, so no costs are being recorded."
            )
            options = ["tariff_setup", "tariff_advanced"]
        return self.async_show_menu(
            step_id="tariff",
            menu_options=options,
            description_placeholders={"summary": summary},
        )

    async def async_step_tariff_setup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start a brand new tariff."""
        await self._async_prepare()
        self._others = []
        self._draft = form.new_version_dict(self._today())
        return await self.async_step_tariff_type()

    async def async_step_tariff_edit_latest(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the newest version in place."""
        tariff = self._stored_tariff()
        if tariff is None:
            return await self.async_step_tariff()
        await self._async_prepare()
        versions = tariff.to_dict()["versions"]
        self._others, self._draft = versions[:-1], versions[-1]
        self._flat_setup = False
        return await self.async_step_tariff_basics()

    async def async_step_tariff_add_version(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a version from a date, starting from a copy of the newest one."""
        tariff = self._stored_tariff()
        if tariff is None:
            return await self.async_step_tariff()
        await self._async_prepare()
        versions = tariff.to_dict()["versions"]
        self._others = versions
        self._draft = copy.deepcopy(versions[-1])
        self._draft["effective_from"] = self._today().isoformat()
        self._flat_setup = False
        return await self.async_step_tariff_basics()

    async def async_step_tariff_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Turn cost tracking off. Cost statistics already written are kept."""
        if user_input is not None:
            return self._save(None) if user_input["confirm"] else await self.async_step_tariff()
        return self.async_show_form(
            step_id="tariff_remove",
            data_schema=vol.Schema({vol.Required("confirm", default=False): BooleanSelector()}),
        )

    # --- New tariff: type, basics, flat rate -----------------------------------------

    async def async_step_tariff_type(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between a flat rate and time-of-use rates."""
        if user_input is not None:
            self._flat_setup = user_input["tariff_type"] == TYPE_FLAT
            return await self.async_step_tariff_basics()
        schema = vol.Schema(
            {
                vol.Required("tariff_type", default=TYPE_TIME_OF_USE): SelectSelector(
                    SelectSelectorConfig(
                        options=[TYPE_FLAT, TYPE_TIME_OF_USE],
                        mode=SelectSelectorMode.LIST,
                        translation_key="tariff_type",
                    )
                )
            }
        )
        return self.async_show_form(step_id="tariff_type", data_schema=schema)

    async def async_step_tariff_basics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect what applies to the whole version rather than one period."""
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = user_input["effective_from"]
            if any(version["effective_from"] == chosen for version in self._others):
                errors["effective_from"] = "date_in_use"
            else:
                self._draft.update(
                    effective_from=chosen,
                    prices_include_gst=bool(user_input["prices_include_gst"]),
                    supply_c_per_day=float(user_input["supply_c_per_day"]),
                    discount_pct=float(user_input["discount_pct"]),
                )
                # One clock for the whole tariff: a retailer's clock does not change with prices.
                form.apply_timezone([*self._others, self._draft], user_input["timezone"])
                if self._flat_setup:
                    return await self.async_step_tariff_flat_rate()
                return await self.async_step_tariff_import_periods()
        values = user_input or self._draft
        schema = vol.Schema(
            {
                vol.Required("effective_from", default=values["effective_from"]): DateSelector(),
                vol.Required("timezone", default=form.draft_zone(values)): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=value, label=label)
                            for value, label in form.timezone_options()
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    "prices_include_gst", default=values.get("prices_include_gst", True)
                ): BooleanSelector(),
                vol.Required(
                    "supply_c_per_day", default=values.get("supply_c_per_day", 0.0)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=10000,
                        step="any",
                        unit_of_measurement="c/day",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    "discount_pct", default=values.get("discount_pct", 0.0)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=100,
                        step="any",
                        unit_of_measurement="%",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        legacy = "timezone" not in self._draft and (self._others or self._stored_tariff())
        return self.async_show_form(
            step_id="tariff_basics",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "timezone_help": form.TIMEZONE_HELP,
                "timezone_state": (
                    "These rates have no clock chosen, so they use fixed AEST (the Brisbane "
                    "option). Pick your retailer's clock to change that."
                    if legacy
                    else ""
                ),
            },
        )

    async def async_step_tariff_flat_rate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the one rate of a flat tariff, stored as a single all-day period."""
        if user_input is not None:
            self._draft["import_periods"] = [form.flat_period(float(user_input["rate"]))]
            return await self._async_after_import()
        schema = vol.Schema({vol.Required("rate"): _rate_selector()})
        return self.async_show_form(
            step_id="tariff_flat_rate",
            data_schema=schema,
            description_placeholders={"timezone": self._clock_note()},
        )

    # --- Periods ---------------------------------------------------------------------------

    async def _async_after_import(self) -> ConfigFlowResult:
        if self._export_applicable:
            return await self.async_step_tariff_export_periods()
        return await self.async_step_tariff_review()

    async def _async_back_to_periods(self) -> ConfigFlowResult:
        if self._kind == KIND_EXPORT:
            return await self.async_step_tariff_export_periods()
        return await self.async_step_tariff_import_periods()

    def _periods_menu(self, step_id: str) -> ConfigFlowResult:
        periods = self._periods()
        options = ["period_add"]
        if periods:
            options += ["period_edit", "period_delete"]
        options.append("periods_done")
        return self.async_show_menu(
            step_id=step_id,
            menu_options=options,
            description_placeholders={
                "periods": form.describe_periods(periods),
                "timezone": self._clock_note(),
            },
        )

    async def async_step_tariff_import_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List the import periods and offer to change them."""
        self._kind = KIND_IMPORT
        return self._periods_menu("tariff_import_periods")

    async def async_step_tariff_export_periods(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List the export periods and offer to change them (only for accounts that export)."""
        self._kind = KIND_EXPORT
        return self._periods_menu("tariff_export_periods")

    async def async_step_periods_done(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Move on from a list of periods."""
        if self._kind == KIND_IMPORT:
            return await self._async_after_import()
        return await self.async_step_tariff_review()

    async def async_step_period_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a period."""
        return await self._async_period_form("period_add", user_input, None)

    async def async_step_period_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a period to edit."""
        return await self._async_period_select("period_edit", user_input)

    async def async_step_period_edit_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the chosen period."""
        return await self._async_period_form("period_edit_form", user_input, self._period_index)

    async def async_step_period_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a period to delete."""
        return await self._async_period_select("period_delete", user_input)

    async def _async_period_select(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        periods = self._periods()
        if not periods:
            return await self._async_back_to_periods()
        names = [period["name"] for period in periods]
        errors: dict[str, str] = {}
        if user_input is not None:
            index = names.index(user_input["period"])
            if step_id == "period_delete":
                del periods[index]
                return await self._async_back_to_periods()
            if form.period_is_guided(periods[index]):
                self._period_index = index
                return await self.async_step_period_edit_form()
            # Rewriting it here would drop its tiers, so send the user to the YAML editor.
            errors["period"] = "period_not_guided"
        schema = vol.Schema(
            {
                vol.Required("period"): SelectSelector(
                    SelectSelectorConfig(options=names, mode=SelectSelectorMode.DROPDOWN)
                )
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "periods": form.describe_periods(periods),
                "timezone": self._clock_note(),
            },
        )

    def _parse_period_form(
        self, user_input: dict[str, Any], index: int | None
    ) -> tuple[dict[str, Any] | None, dict[str, str], str]:
        errors: dict[str, str] = {}
        detail = ""
        name = str(user_input.get("name") or "").strip()
        taken = {p["name"] for i, p in enumerate(self._periods()) if i != index}
        if not name:
            errors["name"] = "name_required"
        elif name in taken:
            errors["name"] = "name_in_use"
        period: dict[str, Any] | None = None
        try:
            period = form.form_to_period(
                name,
                float(user_input["rate"]),
                user_input.get("windows", ""),
                user_input.get("days"),
            )
        except form.FormError as err:
            errors["windows"] = "invalid_windows"
            detail = str(err)
        return (None if errors else period), errors, detail

    async def _async_period_form(
        self, step_id: str, user_input: dict[str, Any] | None, index: int | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            period, errors, detail = self._parse_period_form(user_input, index)
            if period is not None:
                periods = self._periods()
                if index is None:
                    periods.append(period)
                else:
                    periods[index] = period
                return await self._async_back_to_periods()
            values: dict[str, Any] = dict(user_input)
        elif index is not None:
            values = form.period_to_form(self._periods()[index])
        else:
            values = {}
        schema = vol.Schema(
            {
                _suggest("name", values.get("name")): TextSelector(),
                _suggest("rate", values.get("rate")): _rate_selector(),
                _suggest("windows", values.get("windows")): TextSelector(),
                _suggest("days", values.get("days"), required=False): SelectSelector(
                    SelectSelectorConfig(
                        options=[str(day) for day in range(7)],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key="weekday",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "detail": detail,
                "kind": self._kind,
                "timezone": self._clock_note(),
            },
        )

    # --- Review and save ---------------------------------------------------------------

    async def async_step_tariff_review(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the resolved hours and any problem; only a valid tariff can be saved."""
        problem = form.draft_problem(self._all_versions())
        notice = f"**Fix this before saving:** {problem}\n\n" if problem else ""
        options = ["review_back"] if problem else ["review_save", "review_back"]
        return self.async_show_menu(
            step_id="tariff_review",
            menu_options=options,
            description_placeholders={
                "problem": notice,
                "hour_map": form.review_text(self._draft),
                "timezone": self._clock_note(),
            },
        )

    async def async_step_review_back(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Go back to the periods."""
        return await self.async_step_tariff_import_periods()

    async def async_step_review_save(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate once more and store the tariff."""
        try:
            tariff = form.build_tariff(self._all_versions())
        except TariffError:
            return await self.async_step_tariff_review()
        return await self._async_finish(tariff)

    # --- Advanced: the whole tariff as YAML -------------------------------------------

    async def async_step_tariff_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the whole tariff as YAML, for copy-paste and for tiers."""
        errors: dict[str, str] = {}
        detail = ""
        if user_input is not None:
            try:
                return await self._async_finish(Tariff.from_dict(user_input["tariff"]))
            except _BAD_INPUT as err:
                errors["tariff"] = "invalid_tariff"
                detail = str(err)
            initial = user_input["tariff"]
        else:
            initial = (
                self._stored_raw()
                or flat_tariff(30.0, effective_from=self._today(), supply_c_per_day=100.0).to_dict()
            )
        schema = vol.Schema({_suggest("tariff", initial): ObjectSelector()})
        return self.async_show_form(
            step_id="tariff_advanced",
            data_schema=schema,
            errors=errors,
            description_placeholders={"detail": detail, "timezone": self._clock_note()},
        )

    # --- Recalculate history ---------------------------------------------------------------

    async def _async_reprice(
        self,
        tariff: Tariff,
        from_day: date,
        *,
        period_statistics: bool | None = None,
        supply_mode: str | None = None,
    ) -> int:
        """Reprice every priced channel's stored history from a day; return combined-cost hours.

        The per-period switch and supply mode default to the saved options; the supply-charge
        step passes the values it is about to save instead, so history follows the new choice.
        """
        return await async_reprice_entry(
            self.hass,
            self.config_entry,
            tariff,
            from_day,
            period_statistics=period_statistics,
            supply_mode=supply_mode,
            # Looked up here, not bound in reprice.py, so the flow's own name is the one used.
            recalculate=async_recalculate_costs,
        )

    async def async_step_tariff_timezone_changed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Say the pricing clock changed and reprice history on it, unless the user declines.

        The clock moves every window and every day edge, so recorded costs are wrong until they
        are recalculated. Doing it here, from the stored readings and before the options are
        saved, means the reload that follows finds nothing left to change. Declining saves the
        tariff anyway; "Recalculate cost history" can be run later.
        """
        tariff = self._pending
        if tariff is None:
            return await self.async_step_tariff()
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input["reprice_history"]:
                try:
                    await self._async_reprice(tariff, _FAR_PAST)
                except (TariffError, MyEnergyError) as err:
                    _LOGGER.warning("Could not recalculate the cost history: %s", err)
                    errors["base"] = "recalculate_failed"
            if not errors:
                self._pending = None
                return self._save(tariff)
        previous = self._stored_tariff()
        schema = vol.Schema({vol.Required("reprice_history", default=True): BooleanSelector()})
        return self.async_show_form(
            step_id="tariff_timezone_changed",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "old_clock": form.timezone_label(previous.zone_name if previous else None),
                "new_clock": form.timezone_label(tariff.zone_name),
            },
        )

    async def async_step_tariff_recalculate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reprice stored cost history from a date, after a rate change that applies to it."""
        tariff = self._stored_tariff()
        if tariff is None:
            return await self.async_step_tariff()
        errors: dict[str, str] = {}
        if user_input is not None:
            from_day = date.fromisoformat(user_input["from_date"])
            try:
                rows = await self._async_reprice(tariff, from_day)
            except (TariffError, MyEnergyError) as err:
                _LOGGER.warning("Could not recalculate the cost history: %s", err)
                errors["base"] = "recalculate_failed"
            else:
                return self.async_abort(
                    reason="recalculated",
                    description_placeholders={"rows": str(rows), "from_date": from_day.isoformat()},
                )
        default = tariff.versions[-1].effective_from.isoformat()
        schema = vol.Schema({vol.Required("from_date", default=default): DateSelector()})
        return self.async_show_form(step_id="tariff_recalculate", data_schema=schema, errors=errors)

    # --- Statistics per tariff period ------------------------------------------------------

    async def async_step_tariff_period_stats(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Turn the per-period statistics on or off (the coloured bars in the Energy dashboard)."""
        tariff = self._stored_tariff()
        if tariff is None:
            return await self.async_step_tariff()
        if user_input is not None:
            # Saving reloads the entry, and the first poll after the reload backfills history.
            options = dict(self.config_entry.options)
            options[CONF_PERIOD_STATS] = bool(user_input["enabled"])
            if options[CONF_PERIOD_STATS] and form.is_time_of_use(tariff):
                # Only a time-of-use tariff has a supply charge to place; a flat rate keeps it
                # out of the one period, as the dashboard applies its own adjustment there.
                self._pending_options = options
                return await self.async_step_tariff_supply_mode()
            return self.async_create_entry(title="", data=options)
        nmi = self.config_entry.data[CONF_NMI]
        details = form.period_statistics_text(
            tariff,
            lambda slug: period_statistic_ids(nmi, slug),
            self.hass.config.currency,
            supply_mode_of(self.config_entry.options),
        )
        current = bool(self.config_entry.options.get(CONF_PERIOD_STATS))
        schema = vol.Schema({vol.Required("enabled", default=current): BooleanSelector()})
        return self.async_show_form(
            step_id="tariff_period_stats",
            data_schema=schema,
            description_placeholders={"details": details},
        )

    async def async_step_tariff_supply_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose where the daily supply charge appears in the per-period costs.

        Reached from the per-period step with the new options held back, so nothing is saved until
        this is answered. Changing the choice offers to rewrite the recorded period costs now,
        before saving; if that fails nothing is saved. Unticking it saves the choice anyway and
        leaves older days as they were until "Recalculate cost history" is run.
        """
        tariff = self._stored_tariff()
        options = self._pending_options
        if tariff is None or options is None:
            return await self.async_step_tariff()
        previous = supply_mode_of(self.config_entry.options)
        errors: dict[str, str] = {}
        if user_input is not None:
            mode = user_input["supply_mode"]
            if mode != previous and user_input["reprice_history"]:
                try:
                    await self._async_reprice(
                        tariff, _FAR_PAST, period_statistics=True, supply_mode=mode
                    )
                except (TariffError, MyEnergyError) as err:
                    _LOGGER.warning("Could not recalculate the cost history: %s", err)
                    errors["base"] = "recalculate_failed"
            if not errors:
                self._pending_options = None
                options[CONF_SUPPLY_MODE] = mode
                return self.async_create_entry(title="", data=options)
        schema = vol.Schema(
            {
                vol.Required("supply_mode", default=previous): SelectSelector(
                    SelectSelectorConfig(
                        options=list(SUPPLY_MODES),
                        mode=SelectSelectorMode.LIST,
                        translation_key="supply_mode",
                    )
                ),
                vol.Required("reprice_history", default=True): BooleanSelector(),
            }
        )
        amount = daily_supply_dollars(tariff.versions[-1])
        return self.async_show_form(
            step_id="tariff_supply_mode",
            data_schema=schema,
            errors=errors,
            description_placeholders={"daily_supply": f"{self.hass.config.currency} {amount:.5f}"},
        )
