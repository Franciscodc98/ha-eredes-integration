"""Manual sync of E-REDES consumption into long-term statistics.

One entry point, ``async_sync``, shared by the ``eredes.sync`` action and the
"Sync now" options flow. The pasted token lives only in the local
``ERedesClient`` for the duration of the call: it is never stored, logged,
put in an exception or returned (docs/adr/0005).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import CONF_CPE, DOMAIN
from .costs import async_recompute_costs
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
    aggregate_period_hours,
    aggregate_power_hours,
    build_import_plan,
    dedupe_readings,
    import_cutoff,
    plan_window,
    power_rows,
    resume_point,
    series_rows,
    split_into_blocks,
)
from .series import (
    async_last_statistic,
    async_recorder_barrier,
    period_statistic_id,
    period_statistic_metadata,
    power_statistic_id,
    power_statistic_metadata,
    statistic_id,
    statistic_metadata,
)
from .tariffs import Cycle, Period

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .eredes_api.models import ConsumptionReading

_LOGGER = logging.getLogger(__name__)

DEFAULT_HISTORY_DAYS = 365
MAX_HISTORY_DAYS = 1095

# Pause between consecutive requests, to stay polite to the portal.
REQUEST_PAUSE_SECONDS = 1.0

# Cap on the incomplete-hour timestamps echoed back in the response.
MAX_REPORTED_HOURS = 48


__all__ = ["SyncResult", "async_sync", "statistic_id", "statistic_metadata"]


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
    derived_rows_written: int = 0
    cost_rows_written: int = 0

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
            "derived_rows_written": self.derived_rows_written,
            "cost_rows_written": self.cost_rows_written,
        }


def sync_lock(hass: HomeAssistant, cpe: str) -> asyncio.Lock:
    """Return the lock serialising syncs and cost recomputes of one CPE."""
    locks: dict[str, asyncio.Lock] = hass.data.setdefault(DOMAIN, {}).setdefault(
        "sync_locks", {}
    )
    return locks.setdefault(cpe, asyncio.Lock())


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


async def _async_series_lasts(
    hass: HomeAssistant, cpe: str
) -> tuple[
    LastStatistic | None,
    dict[tuple[Cycle, Period], LastStatistic | None],
    LastStatistic | None,
]:
    """Read the newest stored hour of the total, period and power series."""
    total = await async_last_statistic(hass, statistic_id(cpe))
    periods = {
        (cycle, period): await async_last_statistic(
            hass, period_statistic_id(cpe, cycle, period)
        )
        for cycle in Cycle
        for period in Period
    }
    power = await async_last_statistic(hass, power_statistic_id(cpe), mean=True)
    return total, periods, power


def _write_derived_series(
    hass: HomeAssistant,
    cpe: str,
    *,
    readings: list[ConsumptionReading],
    cutoff: datetime | None,
    periods_last: dict[tuple[Cycle, Period], LastStatistic | None],
    power_last: LastStatistic | None,
) -> int:
    """Queue the period-energy and power rows up to ``cutoff``; return the count."""
    written = 0
    for cycle in Cycle:
        buckets = aggregate_period_hours(readings, cycle)
        for period in Period:
            rows = series_rows(buckets[period], periods_last[(cycle, period)], cutoff)
            if rows:
                async_add_external_statistics(
                    hass,
                    period_statistic_metadata(cpe, cycle, period),
                    [
                        StatisticData(start=r.start, state=r.state, sum=r.sum)
                        for r in rows
                    ],
                )
                written += len(rows)
    power = power_rows(
        aggregate_power_hours(readings),
        power_last.start if power_last is not None else None,
        cutoff,
    )
    if power:
        async_add_external_statistics(
            hass,
            power_statistic_metadata(cpe),
            [
                StatisticData(start=r.start, mean=r.mean, min=r.min, max=r.max)
                for r in power
            ],
        )
        written += len(power)
    return written


async def async_sync(
    hass: HomeAssistant,
    entry: ConfigEntry,
    token: str,
    *,
    dry_run: bool = False,
    history_days: int = DEFAULT_HISTORY_DAYS,
) -> SyncResult:
    """Fetch the missing range for the entry's CPE and append it to its series.

    Every block is fetched before anything is aggregated or written; any
    error aborts with nothing written (see ``BlockSequence`` for the one
    tolerated case). Then the total, per-period and power series are queued
    together, the recorder is awaited so a sync run right after sees them, and
    the plans' cost series are recomputed from what is now stored.
    """
    cpe: str = entry.data[CONF_CPE]
    lock = sync_lock(hass, cpe)
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
        last, periods_last, power_last = await _async_series_lasts(hass, cpe)
        # A series added by an upgrade has no rows yet: widen the window so it
        # is backfilled; series already ahead simply skip what they have.
        resume = resume_point([last, *periods_last.values(), power_last])
        window_start, window_end = plan_window(dt_util.utcnow(), resume, history_days)
        blocks = split_into_blocks(window_start, window_end)
        _LOGGER.debug(
            "Sync %s: window %s -> %s in %d blocks (resume: %s, dry run: %s)",
            stat_id,
            window_start.isoformat(),
            window_end.isoformat(),
            len(blocks),
            resume is not None,
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
                "Sync %s: block %d/%d %s -> %s: %s",
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

        if dry_run or (not plan.rows and last is None):
            return result

        # Written even with no new rows when the statistic already exists: the
        # import also upserts the metadata, which repairs statistics created by
        # earlier versions without a unit class (hidden from the Energy picker).
        async_add_external_statistics(
            hass,
            statistic_metadata(cpe),
            [
                StatisticData(start=row.start, state=row.state, sum=row.sum)
                for row in plan.rows
            ],
        )
        derived = _write_derived_series(
            hass,
            cpe,
            readings=unique,
            cutoff=import_cutoff(plan, last),
            periods_last=periods_last,
            power_last=power_last,
        )
        # The calls above only queue the imports; wait for the recorder so the
        # next sync reads this one's last hours and appends nothing twice.
        await async_recorder_barrier(hass)
        costs = await async_recompute_costs(hass, entry)
        _LOGGER.info(
            "Sync %s: wrote %d hour(s), %.3f kWh added, sum now %.3f kWh; "
            "%d derived row(s), %d cost row(s)",
            stat_id,
            len(plan.rows),
            plan.kwh_added,
            plan.final_sum,
            derived,
            costs,
        )
        return replace(result, derived_rows_written=derived, cost_rows_written=costs)
