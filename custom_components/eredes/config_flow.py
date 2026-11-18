"""Config and "Sync now" flows for the E-REDES integration.

The config flow asks for the CPE only; no token is ever stored. The options
flow is a menu. "Sync now" takes a freshly pasted token, runs the same sync as
the ``eredes.sync`` action and shows the summary; unlike an action call, flow
input is not fired as a ``call_service`` event, so the token does not end up
in the recorder database (docs/adr/0005). "Taxes" edits the levies used by
cost estimates. Tariff plans are config subentries (docs/adr/0006).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    ATTR_DRY_RUN,
    ATTR_HISTORY_DAYS,
    ATTR_TOKEN,
    BASE_URL,
    CONF_CPE,
    CPE_PATTERN,
    DOMAIN,
    OPT_CAV_PER_MONTH,
    OPT_DGEG_PER_MONTH,
    OPT_IEC_PER_KWH,
    OPT_REDUCED_KWH,
    OPT_VAT_CAV,
    OPT_VAT_NORMAL,
    OPT_VAT_POWER,
    OPT_VAT_REDUCED,
    PLAN_CYCLE,
    PLAN_DISCOUNT,
    PLAN_K1,
    PLAN_K2,
    PLAN_LOSSES,
    PLAN_OPTION,
    PLAN_POWER_PER_DAY,
    PLAN_PRICE_KEYS,
    PLAN_PRICING,
    PLAN_TAR_KEYS,
    PRICING_FIXED,
    PRICING_INDEXED,
    SUBENTRY_PLAN,
    normalize_cpe,
)
from .sync import DEFAULT_HISTORY_DAYS, MAX_HISTORY_DAYS, async_sync
from .tariffs import Cycle, TariffOption, Taxes

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema({vol.Required(CONF_CPE): str})

SYNC_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(ATTR_DRY_RUN, default=False): BooleanSelector(),
        vol.Optional(ATTR_HISTORY_DAYS, default=DEFAULT_HISTORY_DAYS): NumberSelector(
            NumberSelectorConfig(
                min=1, max=MAX_HISTORY_DAYS, step=1, mode=NumberSelectorMode.BOX
            )
        ),
    }
)


PLAN_NAME = "name"


def _number(
    minimum: float, maximum: float, step: float | str, unit: str | None = None
) -> NumberSelector:
    config = NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


PRICE_SELECTOR = _number(0, 10, "any", "€/kWh")

PLAN_STRUCTURE_SCHEMA = vol.Schema(
    {
        vol.Required(PLAN_NAME): TextSelector(),
        vol.Required(PLAN_OPTION): SelectSelector(
            SelectSelectorConfig(
                options=[option.value for option in TariffOption],
                translation_key=PLAN_OPTION,
                mode=SelectSelectorMode.LIST,
            )
        ),
        vol.Required(PLAN_CYCLE, default=Cycle.DAILY.value): SelectSelector(
            SelectSelectorConfig(
                options=[cycle.value for cycle in Cycle],
                translation_key=PLAN_CYCLE,
                mode=SelectSelectorMode.LIST,
            )
        ),
        vol.Required(PLAN_PRICING, default=PRICING_FIXED): SelectSelector(
            SelectSelectorConfig(
                options=[PRICING_FIXED, PRICING_INDEXED],
                translation_key=PLAN_PRICING,
                mode=SelectSelectorMode.LIST,
            )
        ),
        vol.Required(PLAN_POWER_PER_DAY): _number(0, 10, "any", "€/day"),
        vol.Required(PLAN_DISCOUNT, default=0): _number(0, 100, 0.01, "%"),
    }
)

# Energy price fields asked for each option (subentry data keys).
PRICE_FIELDS: dict[TariffOption, tuple[str, ...]] = {
    TariffOption.SIMPLE: (PLAN_PRICE_KEYS["simple"],),
    TariffOption.BI_HOURLY: (
        PLAN_PRICE_KEYS["vazio"],
        PLAN_PRICE_KEYS["fora_vazio"],
    ),
    TariffOption.TRI_HOURLY: (
        PLAN_PRICE_KEYS["vazio"],
        PLAN_PRICE_KEYS["cheias"],
        PLAN_PRICE_KEYS["ponta"],
    ),
}

# Indexed plans: the formula's coefficients, then TAR energy per period.
INDEXED_FIELDS: tuple[str, ...] = (PLAN_K1, PLAN_K2, PLAN_LOSSES)
INDEXED_SELECTORS = {
    PLAN_K1: _number(0, 10, "any"),
    PLAN_K2: _number(-1, 10, "any", "€/kWh"),
    PLAN_LOSSES: _number(0, 100, "any", "%"),
}


def _tar_fields(option: TariffOption) -> tuple[str, ...]:
    """The TAR energy fields of an indexed plan, mirroring PRICE_FIELDS."""
    by_price = {price: key for key, price in PLAN_PRICE_KEYS.items()}
    return tuple(PLAN_TAR_KEYS[by_price[field]] for field in PRICE_FIELDS[option])


_DEFAULT_TAXES = Taxes()
TAX_DEFAULTS: dict[str, float] = {
    OPT_VAT_REDUCED: _DEFAULT_TAXES.vat_reduced * 100,
    OPT_VAT_NORMAL: _DEFAULT_TAXES.vat_normal * 100,
    OPT_REDUCED_KWH: _DEFAULT_TAXES.reduced_kwh_per_30_days,
    OPT_VAT_POWER: _DEFAULT_TAXES.vat_power * 100,
    OPT_IEC_PER_KWH: _DEFAULT_TAXES.iec_per_kwh,
    OPT_DGEG_PER_MONTH: _DEFAULT_TAXES.dgeg_per_month,
    OPT_CAV_PER_MONTH: _DEFAULT_TAXES.cav_per_month,
    OPT_VAT_CAV: _DEFAULT_TAXES.vat_cav * 100,
}

TAXES_SCHEMA = vol.Schema(
    {
        vol.Required(OPT_VAT_REDUCED): _number(0, 100, 0.1, "%"),
        vol.Required(OPT_VAT_NORMAL): _number(0, 100, 0.1, "%"),
        vol.Required(OPT_REDUCED_KWH): _number(0, 10000, 1, "kWh"),
        vol.Required(OPT_VAT_POWER): _number(0, 100, 0.1, "%"),
        vol.Required(OPT_IEC_PER_KWH): _number(0, 1, "any", "€/kWh"),
        vol.Required(OPT_DGEG_PER_MONTH): _number(0, 100, 0.01, "€"),
        vol.Required(OPT_CAV_PER_MONTH): _number(0, 100, 0.01, "€"),
        vol.Required(OPT_VAT_CAV): _number(0, 100, 0.1, "%"),
    }
)


# Every field that holds a price or coefficient, dropped on re-entry.
ALL_PRICE_FIELDS = {*PLAN_PRICE_KEYS.values(), *PLAN_TAR_KEYS.values(), *INDEXED_FIELDS}


class ERedesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a CPE. Nothing is fetched here, so no token is asked for."""

    VERSION = 3
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            cpe = normalize_cpe(user_input[CONF_CPE])
            if not CPE_PATTERN.match(cpe):
                errors[CONF_CPE] = "invalid_cpe"
            else:
                await self.async_set_unique_id(cpe)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"E-REDES ({cpe[-8:]})",
                    data={CONF_CPE: cpe},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"setup_url": BASE_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow: "Sync now" and the tax settings."""
        return ERedesOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, _config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Tariff plans to estimate costs for (docs/adr/0006)."""
        return {SUBENTRY_PLAN: PlanSubentryFlow}


class ERedesOptionsFlow(OptionsFlow):
    """A menu: run a sync, or edit the taxes used by cost estimates."""

    async def async_step_init(
        self, _user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the menu."""
        return self.async_show_menu(step_id="init", menu_options=["sync", "taxes"])

    async def async_step_sync(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the token and sync options, then sync. Stores nothing."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {
            "setup_url": BASE_URL,
            "status": "",
            "block": "",
        }

        if user_input is not None:
            try:
                result = await async_sync(
                    self.hass,
                    self.config_entry,
                    user_input[ATTR_TOKEN],
                    dry_run=bool(user_input.get(ATTR_DRY_RUN, False)),
                    history_days=int(
                        user_input.get(ATTR_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
                    ),
                )
            except HomeAssistantError as err:
                errors["base"] = err.translation_key or "unknown"
                placeholders.update(err.translation_placeholders or {})
            else:
                response = result.as_response()
                return self.async_abort(
                    reason="dry_run_complete" if result.dry_run else "sync_complete",
                    description_placeholders={
                        key: "" if value is None else str(value)
                        for key, value in response.items()
                        if not isinstance(value, list)
                    },
                )

        # Never echo the token back into the form.
        return self.async_show_form(
            step_id="sync",
            data_schema=SYNC_DATA_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_taxes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the taxes; saving triggers a cost recompute (update listener)."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="taxes",
            data_schema=self.add_suggested_values_to_schema(
                TAXES_SCHEMA, {**TAX_DEFAULTS, **self.config_entry.options}
            ),
        )


class PlanSubentryFlow(ConfigSubentryFlow):
    """Add or edit a tariff plan: name and structure first, then prices."""

    def __init__(self) -> None:
        """Start with no answers."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Name, option, cycle, power price and discount of a new plan."""
        return await self._async_step_structure(user_input, {})

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing plan, starting from its current values."""
        subentry = self._get_reconfigure_subentry()
        current = {**subentry.data, PLAN_NAME: subentry.title}
        return await self._async_step_structure(user_input, current)

    async def _async_step_structure(
        self, user_input: dict[str, Any] | None, current: dict[str, Any]
    ) -> SubentryFlowResult:
        if user_input is not None:
            self._data = {**current, **user_input}
            return await self.async_step_prices()
        step_id = "user" if self.source == SOURCE_USER else "reconfigure"
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                PLAN_STRUCTURE_SCHEMA, {PLAN_DISCOUNT: 0, **current}
            ),
        )

    async def async_step_indexed_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Coefficients and TAR energy of a market-indexed plan."""
        return await self.async_step_prices(user_input)

    async def async_step_prices(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Energy prices for the periods the chosen option distinguishes."""
        option = TariffOption(self._data[PLAN_OPTION])
        indexed = self._data.get(PLAN_PRICING) == PRICING_INDEXED
        fields = (
            (*INDEXED_FIELDS, *_tar_fields(option)) if indexed else PRICE_FIELDS[option]
        )
        if user_input is not None:
            data = {
                key: value
                for key, value in self._data.items()
                if key != PLAN_NAME and key not in ALL_PRICE_FIELDS
            }
            data.update({field: user_input[field] for field in fields})
            title = str(self._data[PLAN_NAME]).strip() or option.value
            if self.source == SOURCE_USER:
                return self.async_create_entry(title=title, data=data)
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                title=title,
                data=data,
            )
        schema = vol.Schema(
            {
                vol.Required(field): INDEXED_SELECTORS.get(field, PRICE_SELECTOR)
                for field in fields
            }
        )
        return self.async_show_form(
            step_id="indexed_prices" if indexed else "prices",
            data_schema=self.add_suggested_values_to_schema(schema, self._data),
        )
