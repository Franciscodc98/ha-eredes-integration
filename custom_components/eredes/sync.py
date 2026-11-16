"""Manual sync of E-REDES consumption into long-term statistics.

One entry point, ``async_sync``, shared by the ``eredes.sync`` action and the
"Sync now" options flow. The pasted token lives only in the local
``ERedesClient`` for the duration of the call: it is never stored, logged,
put in an exception or returned (docs/adr/0005).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.const import UnitOfEnergy
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .eredes_api import (
    ERedesAccessDeniedError,
    ERedesAuthenticationError,
    ERedesCaptchaChallengeError,
    ERedesClient,
    ERedesConnectionError,
    ERedesError,
    ERedesHTTPError,
    ERedesUnsuccessfulResponseError,
)
from .planning import (
    BlockOutcome,
    BlockSequence,
    BlockSequenceError,
    ImportPlan,
    LastStatistic,
    aggregate_hours,
    build_import_plan,
    dedupe_readings,
    plan_window,
    split_into_blocks,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .eredes_api.models import ConsumptionReading

_LOGGER = logging.getLogger(__name__)

DEFAULT_HISTORY_DAYS = 365
MAX_HISTORY_DAYS = 1095

# Pause between consecutive requests, to stay polite to the portal.
REQUEST_PAUSE_SECONDS = 1.0

# Cap on the incomplete-hour timestamps echoed back in the response.
MAX_REPORTED_HOURS = 48


def statistic_id(cpe: str) -> str:
    """Return the external long-term-statistics id for a CPE's energy history.

    External statistics must be ``<source>:<object_id>`` (colon-separated), not
    an entity id — see CONTEXT.md and docs/adr/0002.
    """
    return f"{DOMAIN}:energy_{cpe[-8:].lower()}"


def statistic_metadata(cpe: str) -> StatisticMetaData:
    """Return the metadata of the external energy statistic (unchanged format)."""
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"E-REDES Energy ({cpe[-8:]})",
        source=DOMAIN,
        statistic_id=statistic_id(cpe),
        unit_class=None,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one sync, safe to show the user (no token, no full CPE)."""

    statistic_id: str
    dry_run: bool
    window_start: datetime  # naive Lisbon
    window_end: datetime  # naive Lisbon
    blocks_requested: int
    readings_fetched: int
    duplicate_readings: int
    first_reading: datetime | None
    last_reading: datetime | None
    plan: ImportPlan

    @property
    def hours_written(self) -> int:
        """Rows that were (or, in a dry run, would be) written."""
        return len(self.plan.rows)

    def as_response(self) -> dict[str, Any]:
        """Return the service response payload."""
        plan = self.plan

        def iso(moment: datetime | None) -> str | None:
            return moment.isoformat() if moment is not None else None

        return {
            "statistic_id": self.statistic_id,
            "dry_run": self.dry_run,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "blocks_requested": self.blocks_requested,
            "readings_fetched": self.readings_fetched,
            "duplicate_readings": self.duplicate_readings,
            "first_reading": iso(self.first_reading),
            "last_reading": iso(self.last_reading),
            "hours_written": self.hours_written,
            "first_hour_written": iso(plan.rows[0].start) if plan.rows else None,
            "last_hour_written": iso(plan.rows[-1].start) if plan.rows else None,
            "kwh_added": round(plan.kwh_added, 3),
            "final_sum_kwh": round(plan.final_sum, 3),
            "incomplete_interior_hours": len(plan.incomplete_interior_hours),
            "incomplete_interior_hour_starts": [
                h.isoformat()
                for h in plan.incomplete_interior_hours[:MAX_REPORTED_HOURS]
            ],
            "missing_hours": plan.missing_hours,
            "dropped_partial_final_hour": plan.dropped_partial_final_hour is not None,
            "dropped_partial_final_hour_start": iso(plan.dropped_partial_final_hour),
            "reopened_last_hour": plan.reopened_last_hour,
        }


def _locks(hass: HomeAssistant) -> dict[str, asyncio.Lock]:
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(DOMAIN, {}).setdefault(
        "sync_locks", {}
    )
    return locks


async def _async_last_statistic(
    hass: HomeAssistant, stat_id: str
) -> LastStatistic | None:
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum", "state"}
    )
    if not rows or stat_id not in rows or not rows[stat_id]:
        return None
    row = rows[stat_id][0]
    state = row.get("state")
    return LastStatistic(
        # The recorder returns start as a UTC epoch.
        start=datetime.fromtimestamp(row["start"], tz=UTC),
        sum=float(row.get("sum") or 0.0),
        state=float(state) if state is not None else None,
    )


async def _async_recorder_barrier(hass: HomeAssistant) -> None:
    """Wait until every task queued on the recorder so far has run.

    ``Recorder.async_block_till_done`` is not enough: it returns at once when
    the queue is empty, which is also the case while the recorder thread is
    still running the import it just dequeued. A ``SynchronizeTask`` queued
    behind the import resolves only after the import has finished, because
    the recorder runs its queue one task at a time, in order.
    """
    future: asyncio.Future[None] = hass.loop.create_future()
    get_instance(hass).queue_task(SynchronizeTask(future))
    await future


def _translate_api_error(err: ERedesError, *, at_first_block: bool) -> Exception:
    """Map an API error to the Home Assistant error shown to the user.

    Token and captcha problems are the user's to fix, so they are
    ServiceValidationError with distinct messages (docs/adr/0003). Everything
    else is a HomeAssistantError. No message carries the token.
    """
    if isinstance(err, ERedesAuthenticationError):
        return ServiceValidationError(
            translation_domain=DOMAIN, translation_key="token_expired"
        )
    if isinstance(err, ERedesCaptchaChallengeError):
        return ServiceValidationError(
            translation_domain=DOMAIN, translation_key="captcha_challenge"
        )
    if isinstance(err, ERedesAccessDeniedError):
        return HomeAssistantError(
            translation_domain=DOMAIN, translation_key="access_denied"
        )
    if isinstance(err, ERedesHTTPError):
        return HomeAssistantError(
            translation_domain=DOMAIN,
            # The portal's answer to ranges before the contract start is
            # unknown; if the very first block fails, hint at history_days.
            translation_key="http_error_first_block"
            if at_first_block
            else "http_error",
            translation_placeholders={"status": str(err.status)},
        )
    if isinstance(err, ERedesConnectionError):
        return HomeAssistantError(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        )
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key="unexpected_response"
    )


async def async_sync(
    hass: HomeAssistant,
    cpe: str,
    token: str,
    *,
    dry_run: bool = False,
    history_days: int = DEFAULT_HISTORY_DAYS,
) -> SyncResult:
    """Fetch the missing range for ``cpe`` and append it to its statistic.

    Every block is fetched before anything is aggregated or written; any
    error aborts with nothing written (see ``BlockSequence`` for the one
    tolerated case). The write is a single ``async_add_external_statistics``
    call, awaited through the recorder queue so a sync run right after sees it.
    """
    lock = _locks(hass).setdefault(cpe, asyncio.Lock())
    if lock.locked():
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="sync_in_progress"
        )

    async with lock:
        client = ERedesClient(async_get_clientsession(hass), token)
        if not client.has_token:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="token_missing"
            )

        stat_id = statistic_id(cpe)
        last = await _async_last_statistic(hass, stat_id)
        window_start, window_end = plan_window(dt_util.utcnow(), last, history_days)
        blocks = split_into_blocks(window_start, window_end)
        _LOGGER.debug(
            "Sync %s: window %s → %s in %d blocks (resume: %s, dry run: %s)",
            stat_id,
            window_start.isoformat(),
            window_end.isoformat(),
            len(blocks),
            last is not None,
            dry_run,
        )

        readings: list[ConsumptionReading] = []
        sequence = BlockSequence()
        for index, (block_start, block_end) in enumerate(blocks):
            if index:
                await asyncio.sleep(REQUEST_PAUSE_SECONDS)
            try:
                consumption = await client.get_consumption(cpe, block_start, block_end)
            except ERedesUnsuccessfulResponseError:
                outcome = BlockOutcome.UNSUCCESSFUL
            except ERedesError as err:
                _LOGGER.debug(
                    "Sync %s aborted at block %d/%d: %s",
                    stat_id,
                    index + 1,
                    len(blocks),
                    type(err).__name__,
                )
                raise _translate_api_error(err, at_first_block=index == 0) from None
            else:
                readings.extend(consumption.readings)
                outcome = (
                    BlockOutcome.DATA if consumption.readings else BlockOutcome.EMPTY
                )
            _LOGGER.debug(
                "Sync %s: block %d/%d %s → %s: %s",
                stat_id,
                index + 1,
                len(blocks),
                block_start.isoformat(),
                block_end.isoformat(),
                outcome.value,
            )
            try:
                sequence.record(outcome)
            except BlockSequenceError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key=err.reason.value,
                    translation_placeholders={"block": str(err.block_index + 1)},
                ) from None

        if not sequence.seen_data and sequence.seen_unsuccessful:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="no_successful_response"
            )

        unique, duplicates = dedupe_readings(readings)
        plan = build_import_plan(aggregate_hours(unique), last)
        result = SyncResult(
            statistic_id=stat_id,
            dry_run=dry_run,
            window_start=window_start,
            window_end=window_end,
            blocks_requested=len(blocks),
            readings_fetched=len(unique),
            duplicate_readings=duplicates,
            first_reading=unique[0].timestamp if unique else None,
            last_reading=unique[-1].timestamp if unique else None,
            plan=plan,
        )

        if plan.incomplete_interior_hours:
            _LOGGER.warning(
                "Sync %s: %d imported hour(s) have fewer than 4 readings, first at %s",
                stat_id,
                len(plan.incomplete_interior_hours),
                plan.incomplete_interior_hours[0].isoformat(),
            )

        if dry_run or not plan.rows:
            return result

        async_add_external_statistics(
            hass,
            statistic_metadata(cpe),
            [
                StatisticData(start=row.start, state=row.state, sum=row.sum)
                for row in plan.rows
            ],
        )
        # The call above only queues the import; wait for the recorder so the
        # next sync reads this one's last hour and appends nothing twice.
        await _async_recorder_barrier(hass)
        _LOGGER.info(
            "Sync %s: wrote %d hour(s), %.3f kWh added, sum now %.3f kWh",
            stat_id,
            len(plan.rows),
            plan.kwh_added,
            plan.final_sum,
        )
        return result
