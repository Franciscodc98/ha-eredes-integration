"""Tariff periods and cost estimates for BTN (pure, no Home Assistant imports).

Two concerns live here:

- **Classification** of each 15-minute reading into a tariff period (vazio,
  cheias, ponta) for the two BTN cycles, using the ERSE period tables in force
  before the Directive 3/2026 reform (see docs/adr/0006).
- **Cost estimation** of an hourly consumption series under a user-defined
  tariff plan, applying the same structure a Portuguese invoice does: energy
  with an optional discount, a daily power charge, IEC, DGEG, CAV, and VAT
  with the reduced rate on the first kWh of each month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from .eredes_api.client import LISBON

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


class Period(StrEnum):
    """A BTN tariff period. Bi-hourly "fora de vazio" is cheias + ponta."""

    VAZIO = "vazio"
    CHEIAS = "cheias"
    PONTA = "ponta"


class Cycle(StrEnum):
    """The BTN time-of-use cycle (ciclo horário)."""

    DAILY = "daily"  # ciclo diário: the same periods every day
    WEEKLY = "weekly"  # ciclo semanal: weekdays, Saturdays and Sundays differ


class TariffOption(StrEnum):
    """How many periods the energy price distinguishes."""

    SIMPLE = "simple"
    BI_HOURLY = "bi_hourly"
    TRI_HOURLY = "tri_hourly"


# --- period tables -----------------------------------------------------------
#
# Intervals are [start, end) in minutes after local midnight; anything not
# listed is vazio. "Winter" and "summer" follow the legal time (hora legal de
# inverno / verão), i.e. whether Lisbon is on WET or WEST at that instant.
# Source: ERSE BTN period tables before Directive 3/2026, as published by
# suppliers (docs/adr/0006). BTN holidays are not special-cased.

type Intervals = tuple[tuple[int, int], ...]
type DayTable = dict[Period, Intervals]


def _hm(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def _iv(*pairs: tuple[str, str]) -> Intervals:
    return tuple((_hm(start), _hm(end)) for start, end in pairs)


_DAILY: dict[bool, DayTable] = {
    False: {  # winter
        Period.PONTA: _iv(("09:00", "10:30"), ("18:00", "20:30")),
        Period.CHEIAS: _iv(("08:00", "09:00"), ("10:30", "18:00"), ("20:30", "22:00")),
    },
    True: {  # summer
        Period.PONTA: _iv(("10:30", "13:00"), ("19:30", "21:00")),
        Period.CHEIAS: _iv(("08:00", "10:30"), ("13:00", "19:30"), ("21:00", "22:00")),
    },
}

_WEEKLY_WEEKDAY: dict[bool, DayTable] = {
    False: {
        Period.PONTA: _iv(("09:30", "12:00"), ("18:30", "21:00")),
        Period.CHEIAS: _iv(("07:00", "09:30"), ("12:00", "18:30"), ("21:00", "24:00")),
    },
    True: {
        Period.PONTA: _iv(("09:15", "12:15")),
        Period.CHEIAS: _iv(("07:00", "09:15"), ("12:15", "24:00")),
    },
}

_WEEKLY_SATURDAY: dict[bool, DayTable] = {
    False: {Period.CHEIAS: _iv(("09:30", "13:00"), ("18:30", "22:00"))},
    True: {Period.CHEIAS: _iv(("09:00", "14:00"), ("20:00", "22:00"))},
}

_SUNDAY: DayTable = {}


def _is_summer(local: datetime) -> bool:
    offset = local.dst()
    return offset is not None and offset != timedelta(0)


def classify(moment: datetime, cycle: Cycle) -> Period:
    """Return the tariff period of the quarter-hour starting at ``moment``.

    ``moment`` is an aware instant (readings are UTC); it is classified on the
    Lisbon wall clock, where the period tables are defined.
    """
    local = moment.astimezone(LISBON)
    summer = _is_summer(local)
    if cycle is Cycle.DAILY:
        table = _DAILY[summer]
    elif local.weekday() == 6:
        table = _SUNDAY
    elif local.weekday() == 5:
        table = _WEEKLY_SATURDAY[summer]
    else:
        table = _WEEKLY_WEEKDAY[summer]

    minute = local.hour * 60 + local.minute
    for period, intervals in table.items():
        if any(start <= minute < end for start, end in intervals):
            return period
    return Period.VAZIO


# --- plans and taxes ---------------------------------------------------------


@dataclass(frozen=True)
class Indexation:
    """Market-indexed energy (docs/adr/0007).

    Per 15-minute interval: ``OMIE x (1 + losses) x k1 + k2 + TAR energy``.
    The plan's ``energy`` prices then hold the TAR energy component per period.
    """

    k1: float = 1.0  # multiplier on the loss-adjusted market price
    k2: float = 0.0  # € per kWh
    losses: float = 0.0  # fraction, e.g. 0.164 for 16.4 %


@dataclass(frozen=True)
class Plan:
    """A tariff offer to estimate. Prices exclude VAT; € per kWh / per day."""

    option: TariffOption
    cycle: Cycle
    power_per_day: float
    energy: Mapping[str, float]  # "simple" | "vazio" | "fora_vazio" | period
    energy_discount: float = 0.0  # fraction, e.g. 0.15 for 15 %
    indexed: Indexation | None = None

    def price(self, period: Period) -> float:
        """Return the pre-discount energy price of ``period`` under this plan."""
        if self.option is TariffOption.SIMPLE:
            return self.energy["simple"]
        if self.option is TariffOption.BI_HOURLY:
            key = "vazio" if period is Period.VAZIO else "fora_vazio"
            return self.energy[key]
        return self.energy[period.value]


@dataclass(frozen=True)
class Taxes:
    """Taxes and levies common to every plan (rates as fractions)."""

    vat_reduced: float = 0.06
    vat_normal: float = 0.23
    reduced_kwh_per_30_days: float = 200.0
    vat_power: float = 0.23  # reduced rate only applies up to 3.45 kVA
    iec_per_kwh: float = 0.001
    dgeg_per_month: float = 0.07
    cav_per_month: float = 2.85
    vat_cav: float = 0.06


@dataclass
class HourEnergy:
    """One UTC hour of consumption, total and split per cycle and period."""

    start: datetime  # aware UTC, top of the hour
    total: float
    periods: dict[Cycle, dict[Period, float]] = field(default_factory=dict)
    market: float | None = None  # sum of kWh x OMIE price (€), if known


@dataclass(frozen=True)
class MonthCost:
    """The estimated invoice for one calendar month (for checks and display)."""

    month: date  # first day of the month
    days: int
    kwh: float
    energy: float
    power: float
    levies: float  # IEC + DGEG + CAV, before VAT
    vat: float

    @property
    def total(self) -> float:
        """Return the total including VAT."""
        return self.energy + self.power + self.levies + self.vat


def _local_day(moment: datetime) -> date:
    return moment.astimezone(LISBON).date()


def _energy_net(hour: HourEnergy, plan: Plan) -> float:
    """Energy cost of one hour before VAT, after the discount.

    For an indexed plan the per-period prices are the TAR energy component,
    and the market part is added from the hour's OMIE-valued consumption.
    The formula is linear in consumption, so summing per hour is exact.
    """
    if plan.option is TariffOption.SIMPLE:
        gross = hour.total * plan.energy["simple"]
    else:
        split = hour.periods.get(plan.cycle, {})
        gross = sum(kwh * plan.price(period) for period, kwh in split.items())
    if plan.indexed is not None:
        index = plan.indexed
        gross += (hour.market or 0.0) * (1 + index.losses) * index.k1
        gross += hour.total * index.k2
    return gross * (1 - plan.energy_discount)


def estimate_costs(
    hours: Iterable[HourEnergy], plan: Plan, taxes: Taxes
) -> tuple[list[tuple[datetime, float]], list[MonthCost]]:
    """Estimate the cost of each hour, and of each month, under ``plan``.

    Per calendar month (Lisbon time), as on an invoice:

    - energy: kWh x price x (1 - discount);
    - power: price per day x days with data;
    - IEC per kWh, DGEG and CAV once per month;
    - VAT: the reduced rate on the first ``reduced_kwh_per_30_days x days/30``
      kWh (rounded to whole kWh, as invoices do) and the normal rate on the
      rest. With several periods the reduced share is applied in proportion
      to each period's consumption — an assumption, see docs/adr/0006.

    Hourly costs carry their own energy, IEC and VAT; the daily power charge
    lands on the first hour of each local day and the monthly levies on the
    first hour of the month, so hourly costs add up to the monthly totals.
    """
    by_month: dict[date, list[HourEnergy]] = {}
    for hour in sorted(hours, key=lambda h: h.start):
        if plan.indexed is not None and hour.market is None:
            # No market price yet: an indexed plan cannot be priced there.
            continue
        day = _local_day(hour.start)
        by_month.setdefault(day.replace(day=1), []).append(hour)

    rows: list[tuple[datetime, float]] = []
    months: list[MonthCost] = []
    for month, month_hours in by_month.items():
        days = len({_local_day(h.start) for h in month_hours})
        kwh = sum(h.total for h in month_hours)
        reduced_kwh = round(taxes.reduced_kwh_per_30_days * days / 30)
        share = min(1.0, reduced_kwh / kwh) if kwh > 0 else 1.0
        energy_vat = share * taxes.vat_reduced + (1 - share) * taxes.vat_normal

        totals = {"energy": 0.0, "power": 0.0, "levies": 0.0, "vat": 0.0}
        seen_days: set[date] = set()
        for index, hour in enumerate(month_hours):
            energy = _energy_net(hour, plan)
            iec = hour.total * taxes.iec_per_kwh
            vat = energy * energy_vat + iec * taxes.vat_normal
            power = 0.0
            levies = iec
            day = _local_day(hour.start)
            if day not in seen_days:
                seen_days.add(day)
                power = plan.power_per_day
                vat += power * taxes.vat_power
            if index == 0:
                levies += taxes.dgeg_per_month + taxes.cav_per_month
                vat += (
                    taxes.dgeg_per_month * taxes.vat_normal
                    + taxes.cav_per_month * taxes.vat_cav
                )
            totals["energy"] += energy
            totals["power"] += power
            totals["levies"] += levies
            totals["vat"] += vat
            rows.append((hour.start, energy + power + levies + vat))

        months.append(
            MonthCost(
                month=month,
                days=days,
                kwh=kwh,
                energy=totals["energy"],
                power=totals["power"],
                levies=totals["levies"],
                vat=totals["vat"],
            )
        )
    return rows, months


def utc_hour(moment: datetime) -> datetime:
    """Truncate an aware instant to its UTC hour."""
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
