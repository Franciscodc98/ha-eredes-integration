"""Config and "Sync now" flows for the E-REDES integration.

The config flow asks for the CPE only; no token is ever stored. The options
flow is repurposed as a "Sync now" form: it takes a freshly pasted token, runs
the same sync as the ``eredes.sync`` action and shows the summary. Unlike an
action call, flow input is not fired as a ``call_service`` event, so the token
does not end up in the recorder database (docs/adr/0005).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    normalize_cpe,
)
from .sync import DEFAULT_HISTORY_DAYS, MAX_HISTORY_DAYS, async_sync

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
        """Return the "Sync now" flow."""
        return ERedesSyncFlow()


class ERedesSyncFlow(OptionsFlow):
    """Run a sync from the UI. Stores nothing in the entry's options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the token and sync options, then sync."""
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
                    self.config_entry.data[CONF_CPE],
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
            step_id="init",
            data_schema=SYNC_DATA_SCHEMA,
            errors=errors,
            description_placeholders=placeholders,
        )
