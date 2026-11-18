"""Tests for the E-REDES config flow (CPE only) and the "Sync now" flow."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eredes.const import (
    CONF_CPE,
    DOMAIN,
    OPT_REDUCED_KWH,
    OPT_VAT_REDUCED,
    SUBENTRY_PLAN,
)
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


async def _open_sync_form(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Open the options menu and pick "Sync now"; return the flow id."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["sync", "taxes"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "sync"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sync"
    return result["flow_id"]


async def test_sync_flow_runs_sync_and_shows_summary(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_client: FakePortal
) -> None:
    """The form syncs with the pasted token and stores nothing."""
    flow_id = await _open_sync_form(hass, loaded_entry)

    result = await hass.config_entries.options.async_configure(
        flow_id, {"token": TOKEN, "dry_run": False, "history_days": 10}
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
    flow_id = await _open_sync_form(hass, loaded_entry)
    result = await hass.config_entries.options.async_configure(
        flow_id, {"token": TOKEN, "dry_run": True, "history_days": 10}
    )

    assert result["reason"] == "dry_run_complete"


async def test_sync_flow_shows_error_and_keeps_form(
    hass: HomeAssistant, loaded_entry: MockConfigEntry, mock_client: FakePortal
) -> None:
    """An expired token re-shows the form with a specific error."""
    mock_client.errors = {0: ERedesAuthenticationError("x")}
    flow_id = await _open_sync_form(hass, loaded_entry)

    result = await hass.config_entries.options.async_configure(
        flow_id, {"token": TOKEN, "dry_run": False, "history_days": 10}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "token_expired"}
    assert TOKEN not in repr(result)


# --- taxes -------------------------------------------------------------------


async def test_taxes_form_defaults_and_save(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """The taxes form suggests the defaults and saves to the entry options."""
    result = await hass.config_entries.options.async_init(loaded_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "taxes"}
    )
    assert result["step_id"] == "taxes"
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
    }
    assert suggested[OPT_VAT_REDUCED] == pytest.approx(6)
    assert suggested[OPT_REDUCED_KWH] == pytest.approx(200)

    values = {**suggested, OPT_REDUCED_KWH: 300}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded_entry.options[OPT_REDUCED_KWH] == 300


# --- tariff plans (subentries) -----------------------------------------------


async def _add_plan(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    structure: dict[str, Any],
    prices: dict[str, float],
) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PLAN), context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], structure
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] in {"prices", "indexed_prices"}
    assert {str(key) for key in result["data_schema"].schema} == set(prices)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], prices
    )
    await hass.async_block_till_done()
    return dict(result)


async def test_add_simple_plan(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A simple plan asks one energy price and stores no name in its data."""
    result = await _add_plan(
        hass,
        loaded_entry,
        {
            "name": "Current contract",
            "option": "simple",
            "cycle": "daily",
            "power_per_day": 0.5,
            "energy_discount_percent": 10,
        },
        {"price_simple": 0.16},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = loaded_entry.subentries.values()
    assert subentry.title == "Current contract"
    assert subentry.subentry_type == SUBENTRY_PLAN
    assert dict(subentry.data) == {
        "option": "simple",
        "cycle": "daily",
        "pricing": "fixed",
        "power_per_day": 0.5,
        "energy_discount_percent": 10,
        "price_simple": 0.16,
    }


async def test_add_tri_hourly_plan_asks_three_prices(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A tri-hourly plan asks vazio, cheias and ponta."""
    result = await _add_plan(
        hass,
        loaded_entry,
        {
            "name": "Tri weekly",
            "option": "tri_hourly",
            "cycle": "weekly",
            "power_per_day": 0.5,
            "energy_discount_percent": 0,
        },
        {"price_vazio": 0.1, "price_cheias": 0.17, "price_ponta": 0.25},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = loaded_entry.subentries.values()
    assert subentry.data["cycle"] == "weekly"
    assert subentry.data["price_ponta"] == 0.25


async def test_reconfigure_plan_switching_option_drops_old_prices(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Editing a plan into bi-hourly keeps only the bi-hourly prices."""
    await _add_plan(
        hass,
        loaded_entry,
        {
            "name": "Offer",
            "option": "simple",
            "cycle": "daily",
            "power_per_day": 0.5,
            "energy_discount_percent": 0,
        },
        {"price_simple": 0.16},
    )
    (subentry,) = loaded_entry.subentries.values()

    result = await loaded_entry.start_subentry_reconfigure_flow(
        hass, subentry.subentry_id
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "name": "Offer bi",
            "option": "bi_hourly",
            "cycle": "daily",
            "power_per_day": 0.5,
            "energy_discount_percent": 0,
        },
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"price_vazio": 0.1, "price_fora_vazio": 0.2}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    (subentry,) = loaded_entry.subentries.values()
    assert subentry.title == "Offer bi"
    assert "price_simple" not in subentry.data
    assert subentry.data["price_fora_vazio"] == 0.2


async def test_add_indexed_tri_hourly_plan(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """An indexed plan asks K1, K2, losses and the TAR energy per period."""
    result = await _add_plan(
        hass,
        loaded_entry,
        {
            "name": "Indexed tri",
            "option": "tri_hourly",
            "cycle": "daily",
            "pricing": "indexed",
            "power_per_day": 0.4,
            "energy_discount_percent": 0,
        },
        {
            "k1": 1.08,
            "k2": 0.0185,
            "losses_percent": 16.4,
            "tar_vazio": 0.01,
            "tar_cheias": 0.04,
            "tar_ponta": 0.24,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    (subentry,) = loaded_entry.subentries.values()
    assert subentry.data["pricing"] == "indexed"
    assert subentry.data["k1"] == 1.08
    assert "price_vazio" not in subentry.data
