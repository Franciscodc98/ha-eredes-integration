"""The external statistics this integration writes, and recorder helpers.

All series live under the ``eredes`` source (docs/adr/0002, 0006):

| Statistic id                                 | What                             |
|----------------------------------------------|----------------------------------|
| ``eredes:energy_<sfx>``                      | Total energy, kWh (unchanged)    |
| ``eredes:energy_<sfx>_<cycle>_<period>``     | Energy per tariff period, kWh    |
| ``eredes:power_<sfx>``                       | 15-min average power, kW (max)   |
| ``eredes:cost_<sfx>_<plan id>``              | Estimated cost of a plan, €      |

``<sfx>`` is the last 8 characters of the CPE, lower-cased.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import SynchronizeTask
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import DOMAIN
from .planning import LastStatistic
from .tariffs import Cycle, Period

if TYPE_CHECKING:
    import asyncio

    from homeassistant.core import HomeAssistant

# The beginning of time for "read the whole series".
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)

CYCLE_LABELS = {Cycle.DAILY: "ciclo diário", Cycle.WEEKLY: "ciclo semanal"}
PERIOD_LABELS = {Period.VAZIO: "Vazio", Period.CHEIAS: "Cheias", Period.PONTA: "Ponta"}


def suffix(cpe: str) -> str:
    """Return the CPE suffix used in statistic ids and names."""
    return cpe[-8:]


def statistic_id(cpe: str) -> str:
    """Return the external long-term-statistics id for a CPE's energy history.

    External statistics must be ``<source>:<object_id>`` (colon-separated), not
    an entity id — see CONTEXT.md and docs/adr/0002.
    """
    return f"{DOMAIN}:energy_{suffix(cpe).lower()}"


def period_statistic_id(cpe: str, cycle: Cycle, period: Period) -> str:
    """Return the id of the energy series of one tariff period."""
    return f"{statistic_id(cpe)}_{cycle.value}_{period.value}"


def power_statistic_id(cpe: str) -> str:
    """Return the id of the 15-minute average power series."""
    return f"{DOMAIN}:power_{suffix(cpe).lower()}"


def cost_statistic_prefix(cpe: str) -> str:
    """Return the id prefix shared by every cost series of a CPE."""
    return f"{DOMAIN}:cost_{suffix(cpe).lower()}_"


def cost_statistic_id(cpe: str, plan_id: str) -> str:
    """Return the id of a plan's estimated cost series."""
    return f"{cost_statistic_prefix(cpe)}{plan_id.lower()}"


def _energy_metadata(stat_id: str, name: str) -> StatisticMetaData:
    # unit_class must be "energy": the Energy dashboard's statistic picker
    # only offers statistics of that class, and one with no class is hidden.
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=name,
        source=DOMAIN,
        statistic_id=stat_id,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def statistic_metadata(cpe: str) -> StatisticMetaData:
    """Return the metadata of the total energy statistic."""
    return _energy_metadata(statistic_id(cpe), f"E-REDES Energy ({suffix(cpe)})")


def period_statistic_metadata(
    cpe: str, cycle: Cycle, period: Period
) -> StatisticMetaData:
    """Return the metadata of one tariff period's energy series."""
    return _energy_metadata(
        period_statistic_id(cpe, cycle, period),
        f"E-REDES {PERIOD_LABELS[period]}, {CYCLE_LABELS[cycle]} ({suffix(cpe)})",
    )


def power_statistic_metadata(cpe: str) -> StatisticMetaData:
    """Return the metadata of the 15-minute average power series."""
    return StatisticMetaData(
        has_mean=True,
        has_sum=False,
        mean_type=StatisticMeanType.ARITHMETIC,
        name=f"E-REDES Power, 15-min average ({suffix(cpe)})",
        source=DOMAIN,
        statistic_id=power_statistic_id(cpe),
        unit_class=PowerConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfPower.KILO_WATT,
    )


def cost_statistic_metadata(
    cpe: str, plan_id: str, plan_name: str, currency: str
) -> StatisticMetaData:
    """Return the metadata of a plan's estimated cost series."""
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"E-REDES cost: {plan_name} ({suffix(cpe)})",
        source=DOMAIN,
        statistic_id=cost_statistic_id(cpe, plan_id),
        unit_class=None,
        unit_of_measurement=currency,
    )


async def async_last_statistic(
    hass: HomeAssistant, stat_id: str, *, mean: bool = False
) -> LastStatistic | None:
    """Return the newest row of a series (``sum``/``state``, or just its start)."""
    types: set[Any] = {"max"} if mean else {"sum", "state"}
    rows = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, types
    )
    if not rows or not rows.get(stat_id):
        return None
    row = rows[stat_id][0]
    state = row.get("state")
    return LastStatistic(
        # The recorder returns start as a UTC epoch.
        start=datetime.fromtimestamp(row["start"], tz=UTC),
        sum=float(row.get("sum") or 0.0),
        state=float(state) if state is not None else None,
    )


async def async_read_rows(
    hass: HomeAssistant, stat_ids: set[str], types: set[Any]
) -> dict[str, list[dict[str, Any]]]:
    """Return every hourly row of ``stat_ids`` (missing ids map to [])."""
    found = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, EPOCH, None, stat_ids, "hour", None, types
    )
    return {
        stat_id: [dict(row) for row in found.get(stat_id, [])] for stat_id in stat_ids
    }


async def async_recorder_barrier(hass: HomeAssistant) -> None:
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
