"""End-to-end tests of ``eredes.sync`` against a real (in-memory) recorder."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.eredes.const import CONF_CPE, DOMAIN, SERVICE_SYNC
from custom_components.eredes.eredes_api import (
    ERedesAuthenticationError,
    ERedesCaptchaChallengeError,
    ERedesConnectionError,
    ERedesHTTPError,
    ERedesUnexpectedResponseError,
    ERedesUnsuccessfulResponseError,
)
from custom_components.eredes.sync import statistic_id, statistic_metadata

from .conftest import CPE, TOKEN, FakePortal

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.components.recorder import Recorder
    from homeassistant.core import HomeAssistant

# Every test runs against a loaded entry and the fake portal.
pytestmark = pytest.mark.usefixtures("entry", "mock_client")

STAT_ID = statistic_id(CPE)
NOW = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def mock_recorder_before_hass(async_test_recorder: object) -> None:
    """Order the recorder fixtures before ``hass``, which recorder_mock needs."""


@pytest.fixture
async def entry(
    recorder_mock: Recorder,  # noqa: ARG001
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> MockConfigEntry:
    """A loaded v3 entry, with the clock frozen at NOW."""
    freezer.move_to(NOW)
    config_entry = MockConfigEntry(
        domain=DOMAIN, version=3, data={CONF_CPE: CPE}, unique_id=CPE
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def _sync(hass: HomeAssistant, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_SYNC,
        {"token": TOKEN, **data},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    return dict(response)


async def _rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    await async_wait_recording_done(hass)
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2020, 1, 1, tzinfo=UTC),
        None,
        {STAT_ID},
        "hour",
        None,
        {"state", "sum"},
    )
    return list(stats.get(STAT_ID, []))


def _assert_monotonic(rows: list[dict[str, Any]]) -> None:
    sums = [row["sum"] for row in rows]
    assert sums == sorted(sums)
    starts = [row["start"] for row in rows]
    assert len(starts) == len(set(starts))


# --- happy path and idempotence ---------------------------------------------


async def test_first_sync_imports_history_and_drops_partial_final_hour(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """The first sync imports the window; the 10:00 hour (2 quarters) waits."""
    response = await _sync(hass, history_days=10)

    rows = await _rows(hass)
    assert response["hours_written"] == len(rows) > 0
    assert response["dropped_partial_final_hour"] is True
    assert response["dropped_partial_final_hour_start"] == "2026-03-09T10:00:00+00:00"
    assert response["last_hour_written"] == "2026-03-09T09:00:00+00:00"
    assert response["dry_run"] is False
    assert response["incomplete_interior_hours"] == 0
    assert response["final_sum_kwh"] == pytest.approx(len(rows) * 1.0)
    assert response["kwh_added"] == response["final_sum_kwh"]
    assert response["readings_fetched"] == len(rows) * 4 + 2
    assert datetime.fromtimestamp(rows[-1]["start"], tz=UTC) == datetime(
        2026, 3, 9, 9, tzinfo=UTC
    )
    _assert_monotonic(rows)
    # The whole window went out as one block, before any write.
    assert len(mock_client.calls) == response["blocks_requested"] == 1


async def test_second_sync_in_a_row_adds_nothing(
    hass: HomeAssistant,
) -> None:
    """Idempotence: an immediate re-run writes 0 hours and keeps the sum."""
    first = await _sync(hass, history_days=10)
    second = await _sync(hass)

    rows = await _rows(hass)
    assert second["hours_written"] == 0
    assert second["kwh_added"] == 0
    assert second["final_sum_kwh"] == first["final_sum_kwh"]
    assert len(rows) == first["hours_written"]
    _assert_monotonic(rows)


async def test_later_sync_completes_the_dropped_hour_and_appends(
    hass: HomeAssistant,
    mock_client: FakePortal,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Days later only the missing range is added, starting at the dropped hour."""
    first = await _sync(hass, history_days=10)

    mock_client.published_until = datetime(2026, 3, 12, 0, 0, tzinfo=UTC)
    freezer.move_to(NOW + timedelta(days=3))
    later = await _sync(hass)

    rows = await _rows(hass)
    assert later["first_hour_written"] == "2026-03-09T10:00:00+00:00"
    assert later["last_hour_written"] == "2026-03-11T23:00:00+00:00"
    assert later["hours_written"] == 14 + 24 + 24
    assert later["dropped_partial_final_hour"] is False
    assert later["final_sum_kwh"] == pytest.approx(
        first["final_sum_kwh"] + later["hours_written"]
    )
    # The resume window started two days before the last stored hour.
    assert later["window_start"] == "2026-03-07T00:00:00"
    _assert_monotonic(rows)


async def test_long_first_import_uses_31_day_blocks(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """365 days go out as 12 sequential blocks and land in one write."""
    response = await _sync(hass)

    assert response["blocks_requested"] == len(mock_client.calls) == 12
    assert all(end - start <= timedelta(days=31) for start, end in mock_client.calls)
    rows = await _rows(hass)
    assert len(rows) == response["hours_written"]
    _assert_monotonic(rows)


async def test_dry_run_writes_nothing(
    hass: HomeAssistant,
) -> None:
    """A dry run computes the same plan but leaves the statistic untouched."""
    response = await _sync(hass, history_days=10, dry_run=True)

    assert response["dry_run"] is True
    assert response["hours_written"] > 0
    assert await _rows(hass) == []


# --- reporting ---------------------------------------------------------------


async def test_interior_incomplete_hour_is_imported_and_reported(
    hass: HomeAssistant,
    mock_client: FakePortal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing quarter inside the series is counted and warned about."""
    hole = datetime(2026, 3, 5, 12, 15, tzinfo=UTC)
    mock_client.missing = {hole}

    response = await _sync(hass, history_days=10)

    assert response["incomplete_interior_hours"] == 1
    assert response["incomplete_interior_hour_starts"] == ["2026-03-05T12:00:00+00:00"]
    assert "fewer than 4 readings" in caplog.text


async def test_reopens_partial_hour_left_by_an_older_version(
    hass: HomeAssistant,
) -> None:
    """A stored partial last hour is completed and the sum stays monotonic."""
    async_add_external_statistics(
        hass,
        statistic_metadata(CPE),
        [
            {"start": datetime(2026, 3, 8, 0, tzinfo=UTC), "state": 1.0, "sum": 100.0},
            {"start": datetime(2026, 3, 8, 1, tzinfo=UTC), "state": 0.5, "sum": 100.5},
        ],
    )
    await async_wait_recording_done(hass)

    response = await _sync(hass)

    rows = await _rows(hass)
    assert response["reopened_last_hour"] is True
    assert response["first_hour_written"] == "2026-03-08T01:00:00+00:00"
    by_start = {datetime.fromtimestamp(r["start"], tz=UTC): r for r in rows}
    assert by_start[datetime(2026, 3, 8, 1, tzinfo=UTC)]["state"] == pytest.approx(1.0)
    assert by_start[datetime(2026, 3, 8, 1, tzinfo=UTC)]["sum"] == pytest.approx(101.0)
    _assert_monotonic(rows)


# --- error policy ------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "exception", "key"),
    [
        pytest.param(
            ERedesAuthenticationError("x"),
            ServiceValidationError,
            "token_expired",
            id="401",
        ),
        pytest.param(
            ERedesCaptchaChallengeError("x"),
            ServiceValidationError,
            "captcha_challenge",
            id="403-recaptcha",
        ),
        pytest.param(
            ERedesConnectionError("x"), HomeAssistantError, "cannot_connect", id="net"
        ),
        pytest.param(
            ERedesUnexpectedResponseError("x"),
            HomeAssistantError,
            "unexpected_response",
            id="garbage",
        ),
        pytest.param(ERedesHTTPError(502), HomeAssistantError, "http_error", id="502"),
    ],
)
async def test_any_error_after_data_aborts_without_writing(
    hass: HomeAssistant,
    mock_client: FakePortal,
    error: Exception,
    exception: type[Exception],
    key: str,
) -> None:
    """Block 3 of 12 fails: nothing at all is written, with a distinct error."""
    mock_client.errors = {2: error}  # type: ignore[dict-item]

    with pytest.raises(exception) as err:
        await _sync(hass)

    assert err.value.translation_key == key  # type: ignore[attr-defined]
    assert len(mock_client.calls) == 3  # stopped at the failing block
    assert await _rows(hass) == []


async def test_token_and_captcha_errors_are_distinct_and_validation_errors(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """401 and 403-recaptcha raise ServiceValidationError with different keys."""
    keys = set()
    for error in (ERedesAuthenticationError("x"), ERedesCaptchaChallengeError("x")):
        mock_client.calls.clear()
        mock_client.errors = {0: error}
        with pytest.raises(ServiceValidationError) as err:
            await _sync(hass, history_days=10)
        keys.add(err.value.translation_key)
    assert keys == {"token_expired", "captcha_challenge"}


async def test_http_error_on_first_block_hints_at_history_days(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """Prudent assumption: an HTTP error even at the window start aborts."""
    mock_client.errors = {0: ERedesHTTPError(400)}

    with pytest.raises(HomeAssistantError) as err:
        await _sync(hass)

    assert err.value.translation_key == "http_error_first_block"
    assert await _rows(hass) == []


async def test_leading_unsuccessful_blocks_before_contract_are_tolerated(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """History may start at the contract date: early blocks may fail softly."""
    mock_client.contract_start = datetime(2026, 1, 20, tzinfo=UTC)
    mock_client.unsuccessful_before_contract = True

    response = await _sync(hass)

    assert response["first_hour_written"] == "2026-01-20T00:00:00+00:00"
    assert response["hours_written"] > 0


async def test_leading_empty_blocks_are_tolerated(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """Empty (successful) blocks before the contract are fine too."""
    mock_client.contract_start = datetime(2026, 1, 20, tzinfo=UTC)

    response = await _sync(hass)

    assert response["first_hour_written"] == "2026-01-20T00:00:00+00:00"


async def test_unsuccessful_block_after_data_aborts(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """Once data has been seen, ``Success: false`` aborts the whole sync."""
    mock_client.errors = {5: ERedesUnsuccessfulResponseError("x")}

    with pytest.raises(HomeAssistantError) as err:
        await _sync(hass)

    assert err.value.translation_key == "unsuccessful_after_data"
    assert await _rows(hass) == []


async def test_only_unsuccessful_blocks_is_an_error(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """If no block ever succeeds (e.g. wrong CPE), the sync fails clearly."""
    mock_client.contract_start = datetime(2030, 1, 1, tzinfo=UTC)
    mock_client.unsuccessful_before_contract = True

    with pytest.raises(HomeAssistantError) as err:
        await _sync(hass, history_days=10)

    assert err.value.translation_key == "no_successful_response"


# --- token hygiene -----------------------------------------------------------


async def test_token_never_appears_in_response_logs_or_entry(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_client: FakePortal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The token reaches the client and goes nowhere else."""
    caplog.set_level(logging.DEBUG, logger="custom_components.eredes")

    response = await _sync(hass, history_days=10)
    mock_client.errors = {len(mock_client.calls): ERedesAuthenticationError("x")}
    with pytest.raises(ServiceValidationError) as err:
        await _sync(hass)

    assert mock_client.tokens == [TOKEN, TOKEN]
    assert TOKEN not in repr(response)
    assert TOKEN not in str(err.value)
    # Only this integration's records: with debug logging on, Home Assistant
    # core itself logs the call_service event data, token included — the
    # reason the README recommends the "Sync now" form (docs/adr/0005).
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.eredes")
    )
    assert "Sync eredes:energy_345678ab" in ours
    assert TOKEN not in ours
    assert TOKEN not in repr(dict(entry.data))
    assert TOKEN not in repr(dict(entry.options))


async def test_blank_token_is_rejected(
    hass: HomeAssistant,
    mock_client: FakePortal,
) -> None:
    """Whitespace is not a token."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, SERVICE_SYNC, {"token": "   "}, blocking=True, return_response=True
        )

    assert err.value.translation_key == "token_missing"
    assert mock_client.calls == []


# --- CPE resolution ----------------------------------------------------------


async def test_cpe_selects_the_entry(
    hass: HomeAssistant,
) -> None:
    """An explicit CPE (any case/spacing) selects the matching entry."""
    response = await _sync(hass, cpe="pt0002 0000 1234 5678ab", history_days=10)

    assert response["statistic_id"] == STAT_ID


async def test_unknown_cpe_is_rejected(
    hass: HomeAssistant,
) -> None:
    """A CPE with no loaded entry is a validation error."""
    with pytest.raises(ServiceValidationError) as err:
        await _sync(hass, cpe="PT0002000099999999ZZ")

    assert err.value.translation_key == "cpe_not_configured"


async def test_cpe_required_with_several_entries(
    hass: HomeAssistant,
) -> None:
    """With two entries, the CPE must be given."""
    other = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={CONF_CPE: "PT0002000099999999ZZ"},
        unique_id="PT0002000099999999ZZ",
    )
    other.add_to_hass(hass)
    assert await hass.config_entries.async_setup(other.entry_id)

    with pytest.raises(ServiceValidationError) as err:
        await _sync(hass)

    assert err.value.translation_key == "cpe_required"


async def test_statistic_format_is_unchanged(
    hass: HomeAssistant,
) -> None:
    """Same external id, source and unit as before (docs/adr/0002)."""
    await _sync(hass, history_days=10)
    await async_wait_recording_done(hass)

    last = await hass.async_add_executor_job(
        get_last_statistics, hass, 1, STAT_ID, True, {"sum"}
    )

    assert STAT_ID == "eredes:energy_345678ab"
    assert last[STAT_ID]
