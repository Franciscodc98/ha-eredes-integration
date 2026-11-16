"""Tests for the pure sync planning functions (no Home Assistant needed)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.eredes.eredes_api.client import ERedesClient
from custom_components.eredes.eredes_api.models import ConsumptionReading
from custom_components.eredes.planning import (
    BlockAbortReason,
    BlockOutcome,
    BlockSequence,
    BlockSequenceError,
    HourBucket,
    LastStatistic,
    aggregate_hours,
    build_import_plan,
    dedupe_readings,
    plan_window,
    split_into_blocks,
    utc_to_lisbon_naive,
)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


def _quarters(start: datetime, count: int, kwh: float = 0.25) -> list:
    return [
        ConsumptionReading(
            timestamp=start + timedelta(minutes=15 * i), value_wh=kwh * 1000
        )
        for i in range(count)
    ]


def _bucket(hour: datetime, kwh: float = 1.0, quarters: int = 4) -> HourBucket:
    return HourBucket(start=hour, kwh=kwh, quarters=quarters)


# --- window ------------------------------------------------------------------


def test_window_without_statistics_goes_back_history_days_to_midnight() -> None:
    """A first import starts history_days back, floored to Lisbon midnight."""
    now = _utc(2026, 8, 10, 14, 37, 12)  # 15:37 WEST

    start, end = plan_window(now, None, 365)

    assert end == datetime(2026, 8, 10, 15, 37, 12)  # naive Lisbon
    assert start == datetime(2025, 8, 10, 0, 0)


def test_window_with_statistics_starts_two_days_before_last_hour() -> None:
    """A resume re-requests a buffer before the last stored hour."""
    last = LastStatistic(start=_utc(2026, 8, 8, 22), sum=10.0, state=1.0)

    start, _ = plan_window(_utc(2026, 8, 10, 12), last, 365)

    # 22:00 UTC is 23:00 WEST on the 8th; minus two days, floored.
    assert start == datetime(2026, 8, 6, 0, 0)


def test_utc_to_lisbon_naive_applies_dst() -> None:
    """Summer instants are an hour ahead in Lisbon; winter ones are not."""
    assert utc_to_lisbon_naive(_utc(2026, 7, 1, 12)) == datetime(2026, 7, 1, 13)
    assert utc_to_lisbon_naive(_utc(2026, 1, 1, 12)) == datetime(2026, 1, 1, 12)


# --- blocks ------------------------------------------------------------------


def test_blocks_cover_the_window_without_gaps_or_overlap() -> None:
    """Blocks are contiguous, oldest first, and at most 31 days each."""
    start = datetime(2025, 8, 10)
    end = datetime(2026, 8, 10, 15, 37, 12)

    blocks = split_into_blocks(start, end)

    assert blocks[0][0] == start
    assert blocks[-1][1] == end
    for (_, prev_end), (next_start, _) in itertools.pairwise(blocks):
        assert prev_end == next_start
    assert all(e - s <= timedelta(days=31) for s, e in blocks)
    assert len(blocks) == 12


def test_newest_block_is_full_and_short_remainder_is_oldest() -> None:
    """The newest block is never a sliver; interior edges sit on midnight."""
    start = datetime(2026, 1, 1)
    end = datetime(2026, 3, 5, 10, 0)

    blocks = split_into_blocks(start, end)

    newest_start, newest_end = blocks[-1]
    assert newest_end - newest_start > timedelta(days=30)
    for block_start, _ in blocks[1:]:
        assert block_start.time() == datetime.min.time()


def test_short_window_is_one_block() -> None:
    """A window shorter than 31 days is a single request."""
    start, end = datetime(2026, 3, 1), datetime(2026, 3, 4, 9, 30)

    assert split_into_blocks(start, end) == [(start, end)]


# --- block sequence policy ---------------------------------------------------


@pytest.mark.parametrize(
    "outcomes",
    [
        [BlockOutcome.UNSUCCESSFUL, BlockOutcome.EMPTY, BlockOutcome.DATA],
        [BlockOutcome.DATA, BlockOutcome.DATA],
        [BlockOutcome.DATA, BlockOutcome.EMPTY, BlockOutcome.EMPTY],
        [BlockOutcome.EMPTY],
    ],
)
def test_tolerated_sequences(outcomes: list[BlockOutcome]) -> None:
    """Leading empty/unsuccessful and trailing empty blocks are fine."""
    sequence = BlockSequence()
    for outcome in outcomes:
        sequence.record(outcome)


def test_unsuccessful_after_data_aborts() -> None:
    """``Success: false`` once data has been seen aborts."""
    sequence = BlockSequence()
    sequence.record(BlockOutcome.DATA)

    with pytest.raises(BlockSequenceError) as err:
        sequence.record(BlockOutcome.UNSUCCESSFUL)

    assert err.value.reason is BlockAbortReason.UNSUCCESSFUL_AFTER_DATA
    assert err.value.block_index == 1


def test_empty_block_between_data_aborts() -> None:
    """Data resuming after an empty block means a block-sized hole."""
    sequence = BlockSequence()
    sequence.record(BlockOutcome.DATA)
    sequence.record(BlockOutcome.EMPTY)

    with pytest.raises(BlockSequenceError) as err:
        sequence.record(BlockOutcome.DATA)

    assert err.value.reason is BlockAbortReason.EMPTY_BLOCK_BETWEEN_DATA


# --- aggregation -------------------------------------------------------------


def test_dedupe_drops_boundary_overlap() -> None:
    """The same instant returned by two adjacent blocks is counted once."""
    first = _quarters(_utc(2026, 1, 1, 0), 4)
    second = _quarters(_utc(2026, 1, 1, 0, 45), 2)

    unique, duplicates = dedupe_readings(first + second)

    assert duplicates == 1
    assert len(unique) == 5


def test_aggregate_counts_quarters_per_utc_hour() -> None:
    """15-minute readings roll up into UTC hours with their quarter count."""
    buckets = aggregate_hours(_quarters(_utc(2026, 1, 1, 0), 6))

    assert buckets == [
        HourBucket(_utc(2026, 1, 1, 0), pytest.approx(1.0), 4),  # type: ignore[arg-type]
        HourBucket(_utc(2026, 1, 1, 1), pytest.approx(0.5), 2),  # type: ignore[arg-type]
    ]


@pytest.mark.parametrize(
    ("day", "points"),
    [
        pytest.param("2026-03-29", 92, id="spring-forward"),
        pytest.param("2025-10-26", 100, id="fall-back"),
    ],
)
def test_dst_days_give_complete_utc_hours(day: str, points: int) -> None:
    """After the Lisbon→UTC conversion every hour of a DST day has 4 quarters.

    Builds the wire shape the portal returns (docs/adr/0004) and runs it
    through the real client parser, then the aggregator.
    """
    local = datetime.fromisoformat(day)
    wall_clocks: list[datetime] = []
    moment = local
    while moment < local + timedelta(days=1):
        if not (day == "2026-03-29" and moment.hour == 1):
            wall_clocks.append(moment)
            if day == "2025-10-26" and moment.hour == 1 and moment.minute == 45:
                wall_clocks.extend(
                    local.replace(hour=1, minute=m) for m in (0, 15, 30, 45)
                )
        moment += timedelta(minutes=15)
    assert len(wall_clocks) == points

    response = {
        "Body": {
            "Success": True,
            "Result": {
                "utilitiesDevices": [
                    {
                        "meterLoadCurves": [
                            {
                                "register": "A+",
                                "loadCurves": [
                                    {
                                        "loadCurveTimestamp": wc.strftime(
                                            "%Y-%m-%dT%H:%M:%SZ"
                                        ),
                                        "meterLoadCurve": 0.25,
                                        "meterLoadCurveUnitMeasurement": "kWh",
                                    }
                                    for wc in wall_clocks
                                ],
                            }
                        ]
                    }
                ]
            },
        }
    }
    client = ERedesClient(object(), "aat=synthetic")  # type: ignore[arg-type]
    readings = client._parse_consumption_response(
        "CPE", response, local, local
    ).readings

    buckets = aggregate_hours(readings)

    assert all(b.quarters == 4 for b in buckets)
    assert len(buckets) == points // 4


# --- import plan -------------------------------------------------------------


def test_first_import_drops_partial_final_hour() -> None:
    """The newest hour with fewer than 4 quarters is left for the next sync."""
    buckets = [
        _bucket(_utc(2026, 1, 1, 0)),
        _bucket(_utc(2026, 1, 1, 1)),
        _bucket(_utc(2026, 1, 1, 2), kwh=0.5, quarters=2),
    ]

    plan = build_import_plan(buckets, None)

    assert [r.start for r in plan.rows] == [_utc(2026, 1, 1, 0), _utc(2026, 1, 1, 1)]
    assert [r.sum for r in plan.rows] == [1.0, 2.0]
    assert plan.dropped_partial_final_hour == _utc(2026, 1, 1, 2)
    assert plan.kwh_added == 2.0
    assert plan.final_sum == 2.0


def test_resume_skips_stored_hours_and_continues_sum() -> None:
    """Hours at or before the last stored one are not re-counted."""
    last = LastStatistic(start=_utc(2026, 1, 1, 1), sum=10.0, state=1.0)
    buckets = [_bucket(_utc(2026, 1, 1, h)) for h in range(4)]

    plan = build_import_plan(buckets, last)

    assert [r.start for r in plan.rows] == [_utc(2026, 1, 1, 2), _utc(2026, 1, 1, 3)]
    assert [r.sum for r in plan.rows] == [11.0, 12.0]
    assert plan.kwh_added == 2.0
    assert not plan.reopened_last_hour


def test_resume_with_nothing_new_is_a_noop() -> None:
    """Idempotence: re-running after a sync adds no hours and keeps the sum."""
    last = LastStatistic(start=_utc(2026, 1, 1, 3), sum=12.0, state=1.0)
    buckets = [_bucket(_utc(2026, 1, 1, h)) for h in range(4)]
    buckets.append(_bucket(_utc(2026, 1, 1, 4), kwh=0.25, quarters=1))

    plan = build_import_plan(buckets, last)

    assert plan.rows == []
    assert plan.kwh_added == 0.0
    assert plan.final_sum == 12.0
    assert plan.dropped_partial_final_hour == _utc(2026, 1, 1, 4)


def test_interior_incomplete_hours_are_imported_and_reported() -> None:
    """A short hour followed by later data is real, kept, and flagged."""
    buckets = [
        _bucket(_utc(2026, 1, 1, 0)),
        _bucket(_utc(2026, 1, 1, 1), kwh=0.75, quarters=3),
        _bucket(_utc(2026, 1, 1, 2)),
    ]

    plan = build_import_plan(buckets, None)

    assert len(plan.rows) == 3
    assert plan.incomplete_interior_hours == [_utc(2026, 1, 1, 1)]
    assert plan.dropped_partial_final_hour is None


def test_missing_hours_are_counted() -> None:
    """Whole hours with no reading (including after the cutoff) are counted."""
    last = LastStatistic(start=_utc(2026, 1, 1, 0), sum=1.0, state=1.0)
    buckets = [_bucket(_utc(2026, 1, 1, 2)), _bucket(_utc(2026, 1, 1, 5))]

    plan = build_import_plan(buckets, last)

    assert plan.missing_hours == 1 + 2


def test_partial_last_stored_hour_is_reopened_when_now_complete() -> None:
    """A partial hour left by an earlier version is rewritten, sum stays monotonic."""
    last = LastStatistic(start=_utc(2026, 1, 1, 1), sum=1.5, state=0.5)
    buckets = [
        _bucket(_utc(2026, 1, 1, 0)),
        _bucket(_utc(2026, 1, 1, 1)),
        _bucket(_utc(2026, 1, 1, 2)),
    ]

    plan = build_import_plan(buckets, last)

    assert plan.reopened_last_hour
    assert [(r.start, r.state, r.sum) for r in plan.rows] == [
        (_utc(2026, 1, 1, 1), 1.0, 2.0),
        (_utc(2026, 1, 1, 2), 1.0, 3.0),
    ]
    assert plan.kwh_added == pytest.approx(1.5)


@pytest.mark.parametrize(
    "stored_bucket",
    [
        pytest.param(_bucket(_utc(2026, 1, 1, 1), kwh=1.0), id="same-energy"),
        pytest.param(
            _bucket(_utc(2026, 1, 1, 1), kwh=0.75, quarters=3), id="still-short"
        ),
    ],
)
def test_last_stored_hour_not_reopened_otherwise(stored_bucket: HourBucket) -> None:
    """Only a now-complete hour carrying more energy is reopened."""
    last = LastStatistic(start=_utc(2026, 1, 1, 1), sum=1.0, state=1.0)

    plan = build_import_plan([stored_bucket, _bucket(_utc(2026, 1, 1, 2))], last)

    assert not plan.reopened_last_hour
    assert [r.start for r in plan.rows] == [_utc(2026, 1, 1, 2)]
