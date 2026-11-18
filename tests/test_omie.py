"""Tests for the OMIE price file parser and the market valuation (no HA needed)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from custom_components.eredes.eredes_api.models import ConsumptionReading
from custom_components.eredes.omie import OmieError, market_days, parse_marginal_file
from custom_components.eredes.planning import aggregate_market_hours
from custom_components.eredes.tariffs import (
    Cycle,
    HourEnergy,
    Indexation,
    Plan,
    TariffOption,
    Taxes,
    estimate_costs,
)

if TYPE_CHECKING:
    from .conftest import FakeOmie

QUARTER = timedelta(minutes=15)


def _file(day: date, prices: list[float]) -> str:
    rows = [
        f"{day.year};{day.month};{day.day};{n};{p};{p};"
        for n, p in enumerate(prices, 1)
    ]
    return "MARGINALPDBCPT;\n" + "\n".join(rows) + "\n"


def test_quarter_hour_file_is_keyed_from_madrid_midnight() -> None:
    """Period 1 starts at 00:00 Madrid, i.e. 23:00 UTC the day before in winter."""
    day = date(2026, 11, 16)
    prices = parse_marginal_file(_file(day, [100.0 + n for n in range(96)]), day)

    starts = sorted(prices)
    assert len(starts) == 96
    assert starts[0] == datetime(2026, 11, 15, 23, 0, tzinfo=UTC)
    assert starts[-1] == datetime(2026, 11, 16, 22, 45, tzinfo=UTC)
    assert prices[starts[0]] == pytest.approx(0.100)  # €/MWh -> €/kWh
    assert prices[starts[1]] == pytest.approx(0.101)


def test_summer_file_starts_two_hours_before_midnight_utc() -> None:
    """In CEST, Madrid midnight is 22:00 UTC."""
    day = date(2026, 7, 1)
    prices = parse_marginal_file(_file(day, [50.0] * 96), day)

    assert min(prices) == datetime(2026, 6, 30, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("day", "periods"),
    [
        pytest.param(date(2026, 3, 29), 92, id="spring-forward"),
        pytest.param(date(2026, 10, 25), 100, id="fall-back"),
    ],
)
def test_dst_days_are_contiguous_with_the_next_day(day: date, periods: int) -> None:
    """92 / 100 periods tile the short / long day exactly, with no gap or overlap."""
    today = parse_marginal_file(_file(day, [1.0] * periods), day)
    tomorrow_day = day + timedelta(days=1)
    tomorrow = parse_marginal_file(_file(tomorrow_day, [1.0] * 96), tomorrow_day)

    assert max(today) + QUARTER == min(tomorrow)
    assert len(today) == periods


def test_hourly_file_expands_to_quarters() -> None:
    """Pre-2025 files have 24 hourly periods; each covers four quarter-hours."""
    day = date(2025, 1, 15)
    prices = parse_marginal_file(_file(day, [60.0] * 24), day)

    assert len(prices) == 96
    assert all(price == pytest.approx(0.06) for price in prices.values())


def test_fake_omie_files_parse(omie: FakeOmie) -> None:
    """The test fixture's files are valid, DST days included."""
    for day in (date(2026, 3, 29), date(2026, 10, 25), date(2026, 11, 1)):
        assert parse_marginal_file(omie.file_for(day), day)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("MARGINALPDBCPT;\n", id="empty"),
        pytest.param("2026;11;17;1;10;10;\n", id="wrong-day"),
        pytest.param("2026;11;16;1;10;10;\n2026;11;16;3;10;10;\n", id="gap"),
        pytest.param("2026;11;16;1;abc;abc;\n", id="garbage"),
    ],
)
def test_bad_files_raise(text: str) -> None:
    """Anything unexpected is an OmieError, never a silently short day."""
    with pytest.raises(OmieError):
        parse_marginal_file(text, date(2026, 11, 16))


def test_market_days_follow_madrid_dates() -> None:
    """23:30 UTC is already the next day in Madrid."""
    days = market_days(
        datetime(2026, 11, 15, 23, 30, tzinfo=UTC),
        datetime(2026, 11, 16, 12, 0, tzinfo=UTC),
    )

    assert days == [date(2026, 11, 16)]


# --- market valuation ---------------------------------------------------------


def _readings(
    start: datetime, count: int, kwh: float = 0.25
) -> list[ConsumptionReading]:
    return [
        ConsumptionReading(timestamp=start + n * QUARTER, value_wh=kwh * 1000)
        for n in range(count)
    ]


def test_market_value_is_consumption_times_price() -> None:
    """Each quarter is valued at its own price, then summed per hour."""
    start = datetime(2026, 11, 16, 0, tzinfo=UTC)
    readings = _readings(start, 4)
    prices = {start + n * QUARTER: 0.1 * (n + 1) for n in range(4)}

    buckets, until = aggregate_market_hours(readings, prices)

    assert until == start
    assert buckets[0].kwh == pytest.approx(0.25 * (0.1 + 0.2 + 0.3 + 0.4))


def test_market_series_stops_before_the_first_unpriced_hour() -> None:
    """An hour with any unpriced quarter, and everything after, is left out."""
    start = datetime(2026, 11, 16, 0, tzinfo=UTC)
    readings = _readings(start, 12)  # three hours
    prices = {start + n * QUARTER: 0.1 for n in range(6)}  # 1.5 hours priced

    buckets, until = aggregate_market_hours(readings, prices)

    assert until == start
    assert [b.start for b in buckets] == [start]


def test_nothing_priced() -> None:
    """No price for the first reading means no market rows at all."""
    buckets, until = aggregate_market_hours(
        _readings(datetime(2026, 11, 16, tzinfo=UTC), 4), {}
    )

    assert (buckets, until) == ([], None)


# --- indexed plan --------------------------------------------------------------

NO_TAXES = Taxes(
    vat_reduced=0.0,
    vat_normal=0.0,
    reduced_kwh_per_30_days=0,
    vat_power=0.0,
    iec_per_kwh=0.0,
    dgeg_per_month=0.0,
    cav_per_month=0.0,
    vat_cav=0.0,
)


def test_indexed_energy_formula() -> None:
    """(OMIE x (1 + losses) x K1 + K2 + TAR) x kWh, summed over the hour."""
    hour = HourEnergy(
        start=datetime(2026, 1, 14, 9, tzinfo=UTC), total=2.0, market=0.2
    )  # 2 kWh valued at 0.2 € at market price
    plan = Plan(
        option=TariffOption.SIMPLE,
        cycle=Cycle.DAILY,
        power_per_day=0.0,
        energy={"simple": 0.05},  # TAR energy
        indexed=Indexation(k1=1.1, k2=0.02, losses=0.15),
    )

    _, (month,) = estimate_costs([hour], plan, NO_TAXES)

    assert month.energy == pytest.approx(0.2 * 1.15 * 1.1 + 2.0 * 0.02 + 2.0 * 0.05)


def test_indexed_plan_skips_hours_without_market_price() -> None:
    """Hours not yet priced are not costed (rather than costed as free)."""
    priced = HourEnergy(
        start=datetime(2026, 1, 14, 9, tzinfo=UTC), total=1.0, market=0.1
    )
    unpriced = HourEnergy(start=datetime(2026, 1, 14, 10, tzinfo=UTC), total=1.0)
    plan = Plan(
        option=TariffOption.SIMPLE,
        cycle=Cycle.DAILY,
        power_per_day=0.0,
        energy={"simple": 0.0},
        indexed=Indexation(k1=1.0),
    )

    rows, (month,) = estimate_costs([priced, unpriced], plan, NO_TAXES)

    assert [start for start, _ in rows] == [priced.start]
    assert month.kwh == pytest.approx(1.0)
