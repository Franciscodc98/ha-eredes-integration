"""Tests for the E-REDES config flow (CPE only) and the "Sync now" flow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eredes.const import CONF_CPE, DOMAIN
from custom_components.eredes.eredes_api import ERedesAuthenticationError

from .conftest import CPE, TOKEN, FakePortal

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder import Recorder
    from homeassistant.core import HomeAssistant


# The integration depends on the recorder, so every flow needs one.
pytestmark = pytest.mark.usefixtures("recorder_mock")


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_test_recorder: object) -> None:
    """Order the recorder fixtures before ``hass``."""


# --- config flow -------------------------------------------------------------


async def test_user_flow_creates_cpe_only_entry(hass: HomeAssistant) -> None:
    """Only the (normalized) CPE is stored — no token is asked for."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert list(result["data_schema"].schema) == [CONF_CPE]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CPE: " pt0002 0000 1234 5678ab "}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "E-REDES (345678AB)"
    assert result["data"] == {CONF_CPE: CPE}
    assert result["result"].version == 3


@pytest.mark.parametrize("cpe", ["", "12345", "PT123", "XX0002000012345678AB"])
async def test_user_flow_rejects_malformed_cpe(hass: HomeAssistant, cpe: str) -> None:
    """Something that is not a CPE shows a field error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CPE: cpe}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CPE: "invalid_cpe"}


async def test_user_flow_aborts_if_already_configured(hass: HomeAssistant) -> None:
    """The same CPE cannot be added twice."""
    MockConfigEntry(
        domain=DOMAIN, version=3, data={CONF_CPE: CPE}, unique_id=CPE
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CPE: CPE}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# --- "Sync now" options flow -------------------------------------------------


@pytest.fixture
async def loaded_entry(
    recorder_mock: Recorder,  # noqa: ARG001
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> MockConfigEntry:
    """A loaded entry with the clock frozen after the fake portal's data."""
    freezer.move_to(datetime(2026, 3, 10, 9, 0, tzinfo=UTC))
    entry = MockConfigEntry(
        domain=DOMAIN, version=3, data={CONF_CPE: CPE}, unique_id=CPE
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_sync_flow_runs_sync_and_shows_summary(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_client: FakePortal
) -> None:
    """The form syncs with the pasted token and stores nothing."""
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"token": TOKEN, "dry_run": False, "history_days": 10}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "sync_complete"
    placeholders = result["description_placeholders"]
    assert int(placeholders["hours_written"]) > 0
    assert TOKEN not in repr(placeholders)
    assert mock_client.tokens == [TOKEN]
    assert dict(loaded_entry.options) == {}
    assert dict(loaded_entry.data) == {CONF_CPE: CPE}


@pytest.mark.usefixtures("mock_client")
async def test_sync_flow_dry_run(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A dry run from the UI reports without writing."""
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"token": TOKEN, "dry_run": True, "history_days": 10}
    )

    assert result["reason"] == "dry_run_complete"


async def test_sync_flow_shows_error_and_keeps_form(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_client: FakePortal
) -> None:
    """An expired token re-shows the form with a specific error."""
    mock_client.errors = {0: ERedesAuthenticationError("x")}
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"token": TOKEN, "dry_run": False, "history_days": 10}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "token_expired"}
    assert TOKEN not in repr(result)
