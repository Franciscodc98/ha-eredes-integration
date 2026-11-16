"""Diagnostics support for E-REDES integration.

A config entry holds only the CPE, and it is redacted. There is no token to
redact: it is never stored (docs/adr/0005). Legacy token keys stay in the
redaction set as a belt-and-braces guard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.statistics import get_last_statistics

from .const import CONF_CPE, LEGACY_TOKEN_KEYS
from .sync import statistic_id

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import ERedesConfigEntry

TO_REDACT = {CONF_CPE, "aat", "token", "nif", "nif_requester", *LEGACY_TOKEN_KEYS}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    stat_id = statistic_id(entry.data[CONF_CPE])
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum", "state"}
    )
    last: dict[str, Any] | None = None
    if rows and rows.get(stat_id):
        row = rows[stat_id][0]
        last = {
            "start": datetime.fromtimestamp(row["start"], tz=UTC).isoformat(),
            "sum": row.get("sum"),
            "state": row.get("state"),
        }

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "statistic": {"statistic_id": stat_id, "last": last},
    }
