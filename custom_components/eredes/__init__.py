"""The E-REDES integration: manual sync of consumption into the Energy dashboard.

There is no polling and there are no entities. A config entry holds one CPE;
the ``eredes.sync`` action (or the entry's "Sync now" options flow) takes a
freshly pasted access token and imports the missing range into the
``eredes:energy_<cpe suffix>`` external statistic. See docs/adr/0005.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_CPE,
    ATTR_DRY_RUN,
    ATTR_HISTORY_DAYS,
    ATTR_TOKEN,
    CONF_CPE,
    DOMAIN,
    LEGACY_TOKEN_KEYS,
    SERVICE_SYNC,
    normalize_cpe,
)
from .sync import DEFAULT_HISTORY_DAYS, MAX_HISTORY_DAYS, async_sync

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SYNC_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_TOKEN): cv.string,
        vol.Optional(ATTR_CPE): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
        vol.Optional(ATTR_HISTORY_DAYS, default=DEFAULT_HISTORY_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=MAX_HISTORY_DAYS)
        ),
    }
)

type ERedesConfigEntry = ConfigEntry[None]


def _resolve_entry(hass: HomeAssistant, cpe: str | None) -> ConfigEntry:
    """Pick the loaded entry for ``cpe``, or the only one when it is omitted."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if cpe is not None:
        wanted = normalize_cpe(cpe)
        for entry in entries:
            if normalize_cpe(entry.data[CONF_CPE]) == wanted:
                return entry
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="cpe_not_configured"
        )
    if len(entries) == 1:
        return entries[0]
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="no_entries" if not entries else "cpe_required",
    )


async def _async_handle_sync(call: ServiceCall) -> ServiceResponse:
    """Handle ``eredes.sync``. The token is read from the call and dropped."""
    entry = _resolve_entry(call.hass, call.data.get(ATTR_CPE))
    result = await async_sync(
        call.hass,
        entry.data[CONF_CPE],
        call.data[ATTR_TOKEN],
        dry_run=call.data[ATTR_DRY_RUN],
        history_days=call.data[ATTR_HISTORY_DAYS],
    )
    return result.as_response()


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Register the domain-level ``eredes.sync`` action."""
    hass.services.async_register(
        DOMAIN,
        SERVICE_SYNC,
        _async_handle_sync,
        schema=SYNC_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(_hass: HomeAssistant, _entry: ERedesConfigEntry) -> bool:
    """Set up a CPE. Nothing to start: syncing is on demand."""
    return True


async def async_unload_entry(_hass: HomeAssistant, _entry: ERedesConfigEntry) -> bool:
    """Unload a config entry."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to version 3.

    Versions 1 and 2 stored the access token (under ``session_cookie`` /
    ``aat_token`` in v1, ``access_token`` in v2) and exposed polling sensors.
    Version 3 keeps only the CPE: every token key is deleted and the orphaned
    sensor entities and their device are removed from the registries. The
    ``eredes:energy_…`` statistic is untouched, so the next sync resumes it.
    """
    if entry.version > 3:
        # Downgrade from a future version is not supported.
        return False

    if entry.version < 3:
        data = {k: v for k, v in entry.data.items() if k not in LEGACY_TOKEN_KEYS}

        ent_reg = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            ent_reg.async_remove(entity.entity_id)
        dev_reg = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            dev_reg.async_remove_device(device.id)

        hass.config_entries.async_update_entry(
            entry, data=data, options={}, version=3, minor_version=1
        )
        _LOGGER.debug("Migrated E-REDES config entry to version 3")

    return True
