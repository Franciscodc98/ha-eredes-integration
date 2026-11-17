"""Pure sync planning: window, request blocks, hourly aggregation, gap detection.

Nothing in this module imports Home Assistant or does I/O, so every rule of
the manual sync (docs/adr/0005) can be tested with plain values.

Two clocks meet here and must not be confused (docs/adr/0004):

- **Request windows** are naive Europe/Lisbon wall-clock datetimes, because
  that is the clock the ``edm/get`` endpoint speaks.
- **Readings and statistics** are timezone-aware UTC, converted by the client
  at parse time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from .eredes_api.client import LISBON
from .tariffs import Cycle, Period, classify

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .eredes_api.models import ConsumptionReading

# Longest span requested in one call.
MAX_DAYS_PER_REQUEST = 31

# On resume, re-request a couple of days before the last stored hour so the
# window comfortably covers it; aggregation drops what is already stored.
REFETCH_BUFFER_DAYS = 2

# A complete UTC hour holds four 15-minute readings. The conversion from Lisbon
# local time makes this true on DST days too (docs/adr/0004).
QUARTERS_PER_HOUR = 4

# Tolerance when comparing kWh floats.
KWH_EPSILON = 1e-9

HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class LastStatistic:
    """The newest stored row of the ``eredes:energy_…`` statistic."""

    start: datetime  # aware UTC, top of the hour
    sum: float
    state: float | None  # that hour's own kWh, if stored


@dataclass(frozen=True)
class HourBucket:
    """15-minute readings rolled up into one UTC hour."""

    start: datetime  # aware UTC, top of the hour
    kwh: float
    quarters: int

    @property
    def complete(self) -> bool:
        """Return True if all four quarter-hours are present."""
        return self.quarters >= QUARTERS_PER_HOUR


@dataclass(frozen=True)
class StatisticRow:
    """One hourly row to write (mirrors the recorder's StatisticData)."""

    start: datetime
    state: float
    sum: float


@dataclass(frozen=True)
class ImportPlan:
    """What a sync would write, and what it noticed along the way."""

    rows: list[StatisticRow]
    kwh_added: float
    final_sum: float
    incomplete_interior_hours: list[datetime] = field(default_factory=list)
    missing_hours: int = 0
    dropped_partial_final_hour: datetime | None = None
    reopened_last_hour: bool = False


# --- window ------------------------------------------------------------------


def utc_to_lisbon_naive(moment: datetime) -> datetime:
    """Convert an aware instant to the naive Lisbon wall clock the API speaks."""
    return moment.astimezone(LISBON).replace(tzinfo=None)


def _floor_to_midnight(naive: datetime) -> datetime:
    return naive.replace(hour=0, minute=0, second=0, microsecond=0)


def resume_point(lasts: Iterable[LastStatistic | None]) -> datetime | None:
    """Return the earliest last-stored hour, or None if any series is empty.

    Every statistic a sync feeds must be covered by the window: a series that
    does not exist yet (e.g. added by an upgrade) needs the full history.
    """
    starts: list[datetime] = []
    for last in lasts:
        if last is None:
            return None
        starts.append(last.start)
    return min(starts) if starts else None


def plan_window(
    now: datetime,
    resume_after: datetime | None,
    history_days: int,
) -> tuple[datetime, datetime]:
    """Return the ``(start, end)`` request window as naive Lisbon datetimes.

    With stored statistics the window opens ``REFETCH_BUFFER_DAYS`` before
    ``resume_after`` (the earliest last stored hour); otherwise
    ``history_days`` before now. The start is floored to local midnight so the
    first imported hour is not cut short by an arbitrary minute.
    """
    end = utc_to_lisbon_naive(now)
    if resume_after is not None:
        start = utc_to_lisbon_naive(resume_after) - timedelta(days=REFETCH_BUFFER_DAYS)
    else:
        start = end - timedelta(days=history_days)
    return _floor_to_midnight(start), end


def split_into_blocks(
    start: datetime,
    end: datetime,
    max_days: int = MAX_DAYS_PER_REQUEST,
) -> list[tuple[datetime, datetime]]:
    """Split a window into request blocks of at most ``max_days``, oldest first.

    Blocks are cut backwards from ``end``, so the newest block is always a full
    span (never a sliver that is empty only because the portal has not
    published yet) and any short remainder lands at the oldest end, where an
    empty block is tolerated. Interior boundaries fall on local midnight so a
    block edge never splits an hour, whatever the endpoint's bound semantics.
    """
    span = timedelta(days=max_days)
    blocks: list[tuple[datetime, datetime]] = []
    block_end = end
    while block_end > start:
        candidate = block_end - span
        boundary = _floor_to_midnight(candidate)
        if boundary < candidate:
            boundary += timedelta(days=1)  # ceil, so the span stays <= max_days
        block_start = max(start, boundary)
        blocks.append((block_start, block_end))
        block_end = block_start
    blocks.reverse()
    return blocks


# --- block outcomes ----------------------------------------------------------


class BlockOutcome(Enum):
    """How one request block came back."""

    DATA = "data"  # Success: true with at least one reading
    EMPTY = "empty"  # Success: true with no readings
    UNSUCCESSFUL = "unsuccessful"  # Success: false


class BlockAbortReason(Enum):
    """Why a block sequence is unacceptable."""

    UNSUCCESSFUL_AFTER_DATA = "unsuccessful_after_data"
    EMPTY_BLOCK_BETWEEN_DATA = "empty_block_between_data"


class BlockSequenceError(Exception):
    """A block outcome that must abort the sync before anything is written."""

    def __init__(self, reason: BlockAbortReason, block_index: int) -> None:
        """Store the reason and the zero-based index of the offending block."""
        super().__init__(f"{reason.value} at block {block_index}")
        self.reason = reason
        self.block_index = block_index


class BlockSequence:
    """Decide, block by block, whether the fetched sequence is acceptable.

    The portal's behaviour for ranges before the contract start is unknown
    (it may answer empty or ``Success: false``), so the prudent rule is:

    - empty or unsuccessful blocks are tolerated only while no block has
      returned data yet (the leading edge of the window);
    - once data has been seen, ``Success: false`` aborts;
    - an empty block after data is tolerated only if every later block is
      empty too (the trailing edge); data reappearing after it means a whole
      block-sized hole inside the series, which aborts.

    Transport errors, HTTP errors and malformed bodies never reach this class:
    they abort the sync anywhere in the window.
    """

    def __init__(self) -> None:
        """Start with no blocks seen."""
        self._index = -1
        self._seen_data = False
        self._empty_after_data = False
        self._seen_unsuccessful = False

    @property
    def seen_data(self) -> bool:
        """Return True once any block has returned readings."""
        return self._seen_data

    @property
    def seen_unsuccessful(self) -> bool:
        """Return True if any (tolerated) block was ``Success: false``."""
        return self._seen_unsuccessful

    def record(self, outcome: BlockOutcome) -> None:
        """Record the next block's outcome, raising if it must abort the sync."""
        self._index += 1
        if outcome is BlockOutcome.UNSUCCESSFUL:
            if self._seen_data:
                raise BlockSequenceError(
                    BlockAbortReason.UNSUCCESSFUL_AFTER_DATA, self._index
                )
            self._seen_unsuccessful = True
        elif outcome is BlockOutcome.EMPTY:
            if self._seen_data:
                self._empty_after_data = True
        else:
            if self._empty_after_data:
                raise BlockSequenceError(
                    BlockAbortReason.EMPTY_BLOCK_BETWEEN_DATA, self._index
                )
            self._seen_data = True


# --- aggregation -------------------------------------------------------------


def dedupe_readings(
    readings: Iterable[ConsumptionReading],
) -> tuple[list[ConsumptionReading], int]:
    """Sort readings and drop repeats of the same UTC instant.

    Adjacent blocks may both return the reading at their shared boundary.
    The autumn repeated hour is already resolved to distinct UTC instants by
    the client, so equal instants here are genuine overlaps. Returns the
    readings and how many duplicates were dropped (the first copy wins).
    """
    unique: dict[datetime, ConsumptionReading] = {}
    duplicates = 0
    for reading in readings:
        if reading.timestamp in unique:
            duplicates += 1
            continue
        unique[reading.timestamp] = reading
    return [unique[ts] for ts in sorted(unique)], duplicates


def aggregate_hours(readings: Iterable[ConsumptionReading]) -> list[HourBucket]:
    """Roll aware-UTC 15-minute readings up into sorted UTC hour buckets.

    Timestamps are already UTC, so this only truncates; re-stamping tzinfo
    here would reintroduce the summer one-hour shift (docs/adr/0004).
    """
    kwh: dict[datetime, float] = {}
    quarters: dict[datetime, int] = {}
    for reading in readings:
        hour = reading.timestamp.astimezone(UTC).replace(
            minute=0, second=0, microsecond=0
        )
        kwh[hour] = kwh.get(hour, 0.0) + reading.value_kwh
        quarters[hour] = quarters.get(hour, 0) + 1
    return [HourBucket(hour, kwh[hour], quarters[hour]) for hour in sorted(kwh)]


def _count_missing_hours(previous: datetime | None, hours: list[datetime]) -> int:
    """Count whole hours with no bucket between consecutive hour starts."""
    missing = 0
    for hour in hours:
        if previous is not None:
            missing += max(0, round((hour - previous) / HOUR) - 1)
        previous = hour
    return missing


def build_import_plan(
    buckets: list[HourBucket],
    last: LastStatistic | None,
) -> ImportPlan:
    """Turn hour buckets into the rows to append after the last stored hour.

    - Hours at or before the last stored hour are skipped — except that the
      last stored hour itself is *reopened* when it now comes back complete
      with more energy than was stored (a partial final hour left behind by
      an earlier version). Its row is rewritten from the sum before it, so
      the cumulative series stays monotonic.
    - The final hour is dropped if it is incomplete: the portal has not
      published all of it yet, and the next sync picks it up whole.
    - Incomplete hours before the final one are imported, but reported.
    """
    base_sum = last.sum if last is not None else 0.0
    cutoff = last.start if last is not None else None
    reopened = False

    new = [b for b in buckets if cutoff is None or b.start > cutoff]

    if last is not None and last.state is not None:
        stored = next((b for b in buckets if b.start == last.start), None)
        if (
            stored is not None
            and stored.complete
            and stored.kwh > last.state + KWH_EPSILON
        ):
            new.insert(0, stored)
            base_sum = last.sum - last.state
            reopened = True

    dropped: datetime | None = None
    if new and not new[-1].complete:
        dropped = new.pop().start

    rows: list[StatisticRow] = []
    running = base_sum
    for bucket in new:
        running += bucket.kwh
        rows.append(StatisticRow(start=bucket.start, state=bucket.kwh, sum=running))

    incomplete = [b.start for b in new if not b.complete]
    missing = _count_missing_hours(None if reopened else cutoff, [b.start for b in new])

    previous_sum = last.sum if last is not None else 0.0
    final_sum = rows[-1].sum if rows else previous_sum
    return ImportPlan(
        rows=rows,
        kwh_added=final_sum - previous_sum,
        final_sum=final_sum,
        incomplete_interior_hours=incomplete,
        missing_hours=missing,
        dropped_partial_final_hour=dropped,
        reopened_last_hour=reopened,
    )


# --- derived series ----------------------------------------------------------


@dataclass(frozen=True)
class PowerRow:
    """Average power of the 15-minute intervals of one UTC hour, in kW."""

    start: datetime
    mean: float
    min: float
    max: float


def _hour_of(reading: ConsumptionReading) -> datetime:
    return reading.timestamp.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def aggregate_period_hours(
    readings: Iterable[ConsumptionReading], cycle: Cycle
) -> dict[Period, list[HourBucket]]:
    """Split readings by tariff period under ``cycle``, then roll up per hour.

    Classification happens per 15-minute reading, before aggregation, because
    some period boundaries fall inside an hour (e.g. 10:30).
    """
    split: dict[Period, list[ConsumptionReading]] = {period: [] for period in Period}
    for reading in readings:
        split[classify(reading.timestamp, cycle)].append(reading)
    return {period: aggregate_hours(items) for period, items in split.items()}


def aggregate_power_hours(readings: Iterable[ConsumptionReading]) -> list[PowerRow]:
    """Return the mean, min and max 15-minute average power of each UTC hour.

    A quarter-hour's average power is its energy x 4 (kWh over 0.25 h). The
    portal's "potência máxima tomada" is understood to be the monthly maximum
    of this value — unverified, see docs/adr/0006.
    """
    by_hour: dict[datetime, list[float]] = {}
    for reading in readings:
        by_hour.setdefault(_hour_of(reading), []).append(reading.value_kwh * 4)
    return [
        PowerRow(
            start=hour,
            mean=sum(values) / len(values),
            min=min(values),
            max=max(values),
        )
        for hour, values in sorted(by_hour.items())
    ]


def import_cutoff(plan: ImportPlan, last: LastStatistic | None) -> datetime | None:
    """Return the newest hour every series may be written up to.

    That is the total statistic's newest written hour — after the partial
    final hour was dropped — or its last stored hour if nothing new was
    written. Derived series never run ahead of the total.
    """
    if plan.rows:
        return plan.rows[-1].start
    return last.start if last is not None else None


def series_rows(
    buckets: list[HourBucket],
    last: LastStatistic | None,
    cutoff: datetime | None,
) -> list[StatisticRow]:
    """Rows to append to a derived energy series: after its last hour, up to cutoff.

    A derived series may legitimately hold fewer than four quarters in an hour
    (an hour split between two periods), so completeness is judged on the
    total statistic, through ``cutoff``, not here.
    """
    if cutoff is None:
        return []
    running = last.sum if last is not None else 0.0
    rows: list[StatisticRow] = []
    for bucket in buckets:
        if last is not None and bucket.start <= last.start:
            continue
        if bucket.start > cutoff:
            break
        running += bucket.kwh
        rows.append(StatisticRow(start=bucket.start, state=bucket.kwh, sum=running))
    return rows


def power_rows(
    rows: list[PowerRow], last_start: datetime | None, cutoff: datetime | None
) -> list[PowerRow]:
    """Power rows after ``last_start``, up to ``cutoff``."""
    if cutoff is None:
        return []
    return [
        row
        for row in rows
        if (last_start is None or row.start > last_start) and row.start <= cutoff
    ]
