"""Tests for E-REDES setup, the registered action and config entry migration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eredes import async_migrate_entry
from custom_components.eredes.const import CONF_CPE, DOMAIN, SERVICE_SYNC
from custom_components.eredes.diagnostics import async_get_config_entry_diagnostics

from .conftest import CPE, TOKEN

if TYPE_CHECKING:
    from homeassistant.components.recorder import Recorder
    from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_test_recorder: object) -> None:
    """Order the recorder fixtures before ``hass``."""


@pytest.mark.parametrize(
    ("version", "token_key"),
    [
        pytest.param(1, "session_cookie", id="v1-session_cookie"),
        pytest.param(1, "aat_token", id="v1-aat_token"),
        pytest.param(2, "access_token", id="v2-access_token"),
    ],
)
async def test_migrate_to_v3_deletes_token(
    hass: HomeAssistant, version: int, token_key: str
) -> None:
    """Any stored token key is deleted; only the CPE survives."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=version,
        data={token_key: TOKEN, CONF_CPE: CPE},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 3
    assert dict(entry.data) == {CONF_CPE: CPE}
    assert TOKEN not in repr(entry.as_dict())


async def test_migrate_removes_orphaned_sensor_entities_and_device(
    hass: HomeAssistant,
) -> None:
    """The v2 Daily Energy / Power sensors and their device are cleaned up."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={"access_token": TOKEN, CONF_CPE: CPE},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, CPE)}
    )
    ent_reg = er.async_get(hass)
    for key in ("energy", "power"):
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{CPE}_{key}",
            config_entry=entry,
            device_id=device.id,
        )

    assert await async_migrate_entry(hass, entry) is True

    assert er.async_entries_for_config_entry(ent_reg, entry.entry_id) == []
    assert dev_reg.async_get(device.id) is None


async def test_v3_is_noop(hass: HomeAssistant) -> None:
    """A current entry is left untouched."""
    entry = MockConfigEntry(domain=DOMAIN, version=3, data={CONF_CPE: CPE})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert dict(entry.data) == {CONF_CPE: CPE}


async def test_migrate_from_future_version_fails(hass: HomeAssistant) -> None:
    """A downgrade from a future version is refused."""
    entry = MockConfigEntry(domain=DOMAIN, version=4, data={CONF_CPE: CPE})
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is False


async def test_setup_registers_action_and_no_entities(
    recorder_mock: Recorder,  # noqa: ARG001
    hass: HomeAssistant,
) -> None:
    """Setting up registers eredes.sync and creates no entities at all."""
    entry = MockConfigEntry(domain=DOMAIN, version=3, data={CONF_CPE: CPE})
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, SERVICE_SYNC)
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []
    assert hass.states.async_entity_ids() == []
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_diagnostics_redact_cpe_and_hold_no_token(
    recorder_mock: Recorder,  # noqa: ARG001
    hass: HomeAssistant,
) -> None:
    """Diagnostics show the statistic, never the CPE or a token."""
    entry = MockConfigEntry(domain=DOMAIN, version=3, data={CONF_CPE: CPE})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry"]["data"] == {CONF_CPE: "**REDACTED**"}
    assert diagnostics["statistic"] == {
        "statistic_id": "eredes:energy_345678ab",
        "last": None,
    }
    assert CPE not in repr(diagnostics)
