"""Tests for tariff period classification and cost estimates (no HA needed)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.eredes.eredes_api.client import LISBON
from custom_components.eredes.tariffs import (
    Cycle,
    HourEnergy,
    Period,
    Plan,
    TariffOption,
    Taxes,
    classify,
    estimate_costs,
)


def _local(*args: int) -> datetime:
    """An aware instant from a Lisbon wall clock."""
    return datetime(*args, tzinfo=LISBON)  # type: ignore[misc]


# --- classification ----------------------------------------------------------

# 2026-01-14 is a Wednesday in winter; 2026-07-15 a Wednesday in summer.
WINTER_WED = (2026, 1, 14)
SUMMER_WED = (2026, 7, 15)


@pytest.mark.parametrize(
    ("hm", "expected"),
    [
        ((7, 45), Period.VAZIO),
        ((8, 0), Period.CHEIAS),
        ((9, 0), Period.PONTA),
        ((10, 15), Period.PONTA),
        ((10, 30), Period.CHEIAS),
        ((18, 0), Period.PONTA),
        ((20, 15), Period.PONTA),
        ((20, 30), Period.CHEIAS),
        ((21, 45), Period.CHEIAS),
        ((22, 0), Period.VAZIO),
    ],
)
def test_daily_cycle_winter(hm: tuple[int, int], expected: Period) -> None:
    """Daily cycle, winter: ponta 09:00-10:30 and 18:00-20:30."""
    assert classify(_local(*WINTER_WED, *hm), Cycle.DAILY) is expected


@pytest.mark.parametrize(
    ("hm", "expected"),
    [
        ((10, 15), Period.CHEIAS),
        ((10, 30), Period.PONTA),
        ((12, 45), Period.PONTA),
        ((13, 0), Period.CHEIAS),
        ((19, 30), Period.PONTA),
        ((21, 0), Period.CHEIAS),
        ((22, 0), Period.VAZIO),
    ],
)
def test_daily_cycle_summer(hm: tuple[int, int], expected: Period) -> None:
    """Daily cycle, summer: ponta 10:30-13:00 and 19:30-21:00."""
    assert classify(_local(*SUMMER_WED, *hm), Cycle.DAILY) is expected


def test_daily_cycle_ignores_weekends() -> None:
    """The daily cycle is the same on a Sunday."""
    sunday = (2026, 1, 18)
    assert classify(_local(*sunday, 9, 0), Cycle.DAILY) is Period.PONTA


@pytest.mark.parametrize(
    ("day", "hm", "expected"),
    [
        (WINTER_WED, (6, 45), Period.VAZIO),
        (WINTER_WED, (7, 0), Period.CHEIAS),
        (WINTER_WED, (9, 30), Period.PONTA),
        (WINTER_WED, (12, 0), Period.CHEIAS),
        (WINTER_WED, (18, 30), Period.PONTA),
        (WINTER_WED, (21, 0), Period.CHEIAS),
        (WINTER_WED, (23, 45), Period.CHEIAS),
        (SUMMER_WED, (9, 0), Period.CHEIAS),
        (SUMMER_WED, (9, 15), Period.PONTA),
        (SUMMER_WED, (12, 15), Period.CHEIAS),
        (SUMMER_WED, (19, 0), Period.CHEIAS),
        ((2026, 1, 17), (9, 15), Period.VAZIO),  # winter Saturday
        ((2026, 1, 17), (9, 30), Period.CHEIAS),
        ((2026, 1, 17), (13, 0), Period.VAZIO),
        ((2026, 1, 17), (18, 30), Period.CHEIAS),
        ((2026, 7, 18), (9, 0), Period.CHEIAS),  # summer Saturday
        ((2026, 7, 18), (14, 0), Period.VAZIO),
        ((2026, 7, 18), (20, 0), Period.CHEIAS),
        ((2026, 7, 19), (12, 0), Period.VAZIO),  # Sunday
    ],
)
def test_weekly_cycle(
    day: tuple[int, int, int], hm: tuple[int, int], expected: Period
) -> None:
    """Weekly cycle: weekdays, Saturdays and Sundays differ, as do seasons."""
    assert classify(_local(*day, *hm), Cycle.WEEKLY) is expected


def test_season_follows_legal_time_on_transition_day() -> None:
    """2026-03-29: before the 01:00 UTC switch it is winter, after it summer."""
    winter_10h = datetime(2026, 3, 28, 10, 0, tzinfo=UTC)  # 10:00 WET
    summer_10h = datetime(2026, 3, 30, 9, 0, tzinfo=UTC)  # 10:00 WEST
    assert classify(winter_10h, Cycle.DAILY) is Period.PONTA
    assert classify(summer_10h, Cycle.DAILY) is Period.CHEIAS


def test_classification_uses_lisbon_time_not_utc() -> None:
    """09:30 UTC in summer is 10:30 Lisbon (ponta); read as UTC it is cheias."""
    assert classify(datetime(2026, 7, 15, 9, 30, tzinfo=UTC), Cycle.DAILY) is (
        Period.PONTA
    )  # 10:30 WEST


# --- cost estimates ----------------------------------------------------------

NO_LEVIES = Taxes(
    vat_reduced=0.0,
    vat_normal=0.0,
    reduced_kwh_per_30_days=0,
    vat_power=0.0,
    iec_per_kwh=0.0,
    dgeg_per_month=0.0,
    cav_per_month=0.0,
    vat_cav=0.0,
)


def _month_of_hours(
    first: datetime, days: int, kwh_per_hour: float
) -> list[HourEnergy]:
    hours = []
    moment = first
    while moment < first + timedelta(days=days):
        hours.append(HourEnergy(start=moment.astimezone(UTC), total=kwh_per_hour))
        moment += timedelta(hours=1)
    return hours


def _simple(price: float = 0.2, power: float = 0.5, discount: float = 0.0) -> Plan:
    return Plan(
        option=TariffOption.SIMPLE,
        cycle=Cycle.DAILY,
        power_per_day=power,
        energy={"simple": price},
        energy_discount=discount,
    )


def test_simple_plan_without_taxes() -> None:
    """Energy x price plus power x days, and nothing else."""
    hours = _month_of_hours(_local(2026, 2, 1), 28, 0.5)  # 336 kWh

    _, (month,) = estimate_costs(hours, _simple(), NO_LEVIES)

    assert month.days == 28
    assert month.kwh == pytest.approx(336)
    assert month.energy == pytest.approx(336 * 0.2)
    assert month.power == pytest.approx(28 * 0.5)
    assert month.total == pytest.approx(336 * 0.2 + 14)


def test_discount_applies_to_energy_only() -> None:
    """A 10 % discount reduces energy, not the power charge."""
    hours = _month_of_hours(_local(2026, 2, 1), 28, 0.5)

    _, (month,) = estimate_costs(hours, _simple(discount=0.10), NO_LEVIES)

    assert month.energy == pytest.approx(336 * 0.2 * 0.9)
    assert month.power == pytest.approx(14)


def test_reduced_vat_on_first_kwh_scaled_by_days_and_rounded() -> None:
    """300 kWh in 31 days: round(200 x 31/30) = 207 kWh at 6 %, 93 at 23 %."""
    hours = _month_of_hours(_local(2026, 1, 1), 31, 300 / (31 * 24))
    taxes = Taxes(iec_per_kwh=0.0, dgeg_per_month=0.0, cav_per_month=0.0, vat_power=0.0)

    _, (month,) = estimate_costs(hours, _simple(power=0.0), taxes)

    energy = 300 * 0.2
    assert month.vat == pytest.approx(energy * (207 / 300 * 0.06 + 93 / 300 * 0.23))


def test_small_month_is_all_at_reduced_vat() -> None:
    """Below the threshold every kWh gets the reduced rate."""
    hours = _month_of_hours(_local(2026, 1, 1), 31, 0.1)  # 74.4 kWh
    taxes = Taxes(iec_per_kwh=0.0, dgeg_per_month=0.0, cav_per_month=0.0, vat_power=0.0)

    _, (month,) = estimate_costs(hours, _simple(power=0.0), taxes)

    assert month.vat == pytest.approx(month.energy * 0.06)


def test_monthly_levies_and_their_vat() -> None:
    """DGEG and IEC pay the normal rate; CAV its own; power its own."""
    hours = _month_of_hours(_local(2026, 1, 1), 31, 0.1)  # 74.4 kWh
    taxes = Taxes(
        vat_reduced=0.0,
        iec_per_kwh=0.01,
        dgeg_per_month=1.0,
        cav_per_month=2.0,
        vat_cav=0.06,
        vat_power=0.23,
    )

    _, (month,) = estimate_costs(hours, _simple(price=0.0, power=1.0), taxes)

    iec = 74.4 * 0.01
    assert month.levies == pytest.approx(iec + 1.0 + 2.0)
    assert month.vat == pytest.approx((iec + 1.0) * 0.23 + 2.0 * 0.06 + 31 * 1.0 * 0.23)


def test_hourly_costs_add_up_to_monthly_totals() -> None:
    """The hourly series sums to the months, across a month boundary."""
    hours = _month_of_hours(_local(2026, 1, 20), 20, 0.4)

    rows, months = estimate_costs(hours, _simple(), Taxes())

    assert len(months) == 2
    assert sum(cost for _, cost in rows) == pytest.approx(sum(m.total for m in months))


def test_power_charge_counts_only_days_with_data() -> None:
    """A contract starting mid-month pays power for the days it has."""
    hours = _month_of_hours(_local(2026, 3, 11), 21, 0.2)

    _, (month,) = estimate_costs(hours, _simple(), NO_LEVIES)

    assert month.days == 21
    assert month.power == pytest.approx(21 * 0.5)


def test_tri_hourly_prices_each_period() -> None:
    """Each period's kWh is priced at its own rate under the plan's cycle."""
    hour = HourEnergy(
        start=datetime(2026, 1, 14, 9, tzinfo=UTC),
        total=3.0,
        periods={
            Cycle.WEEKLY: {Period.VAZIO: 1.0, Period.CHEIAS: 1.5, Period.PONTA: 0.5}
        },
    )
    plan = Plan(
        option=TariffOption.TRI_HOURLY,
        cycle=Cycle.WEEKLY,
        power_per_day=0.0,
        energy={"vazio": 0.1, "cheias": 0.2, "ponta": 0.4},
    )

    _, (month,) = estimate_costs([hour], plan, NO_LEVIES)

    assert month.energy == pytest.approx(0.1 + 0.3 + 0.2)


def test_bi_hourly_fora_de_vazio_is_cheias_plus_ponta() -> None:
    """Bi-hourly prices cheias and ponta alike, as fora de vazio."""
    hour = HourEnergy(
        start=datetime(2026, 1, 14, 9, tzinfo=UTC),
        total=3.0,
        periods={
            Cycle.DAILY: {Period.VAZIO: 1.0, Period.CHEIAS: 1.5, Period.PONTA: 0.5}
        },
    )
    plan = Plan(
        option=TariffOption.BI_HOURLY,
        cycle=Cycle.DAILY,
        power_per_day=0.0,
        energy={"vazio": 0.1, "fora_vazio": 0.2},
    )

    _, (month,) = estimate_costs([hour], plan, NO_LEVIES)

    assert month.energy == pytest.approx(0.1 + 2.0 * 0.2)
