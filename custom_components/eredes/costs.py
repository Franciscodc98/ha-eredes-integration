"""Estimated cost series per tariff plan (docs/adr/0006).

Plans are config subentries; taxes are the entry's options. Costs are derived
from the stored energy series, never from the portal, so they can be
recomputed — whenever a plan or a tax changes, and after every sync — without
a token. Only rows whose value changed are rewritten.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_list_statistic_ids,
)

from .const import (
    CONF_CPE,
    OPT_CAV_PER_MONTH,
    OPT_DGEG_PER_MONTH,
    OPT_IEC_PER_KWH,
    OPT_REDUCED_KWH,
    OPT_VAT_CAV,
    OPT_VAT_NORMAL,
    OPT_VAT_POWER,
    OPT_VAT_REDUCED,
    PLAN_CYCLE,
    PLAN_DISCOUNT,
    PLAN_OPTION,
    PLAN_POWER_PER_DAY,
    PLAN_PRICE_KEYS,
    SUBENTRY_PLAN,
)
from .series import (
    async_read_rows,
    async_recorder_barrier,
    cost_statistic_id,
    cost_statistic_metadata,
    cost_statistic_prefix,
    period_statistic_id,
    statistic_id,
)
from .tariffs import (
    Cycle,
    HourEnergy,
    Period,
    Plan,
    TariffOption,
    Taxes,
    estimate_costs,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Below this a cost difference is float noise, not a change worth rewriting.
COST_EPSILON = 1e-6


def taxes_from_options(options: dict[str, Any]) -> Taxes:
    """Build the tax settings from entry options (percentages in the UI)."""
    default = Taxes()

    def pct(key: str, fallback: float) -> float:
        value = options.get(key)
        return fallback if value is None else float(value) / 100

    def num(key: str, fallback: float) -> float:
        value = options.get(key)
        return fallback if value is None else float(value)

    return Taxes(
        vat_reduced=pct(OPT_VAT_REDUCED, default.vat_reduced),
        vat_normal=pct(OPT_VAT_NORMAL, default.vat_normal),
        reduced_kwh_per_30_days=num(OPT_REDUCED_KWH, default.reduced_kwh_per_30_days),
        vat_power=pct(OPT_VAT_POWER, default.vat_power),
        iec_per_kwh=num(OPT_IEC_PER_KWH, default.iec_per_kwh),
        dgeg_per_month=num(OPT_DGEG_PER_MONTH, default.dgeg_per_month),
        cav_per_month=num(OPT_CAV_PER_MONTH, default.cav_per_month),
        vat_cav=pct(OPT_VAT_CAV, default.vat_cav),
    )


def plan_from_data(data: dict[str, Any]) -> Plan:
    """Build a plan from a subentry's data."""
    return Plan(
        option=TariffOption(data[PLAN_OPTION]),
        cycle=Cycle(data.get(PLAN_CYCLE) or Cycle.DAILY),
        power_per_day=float(data[PLAN_POWER_PER_DAY]),
        energy={
            key: float(data[field])
            for key, field in PLAN_PRICE_KEYS.items()
            if data.get(field) is not None
        },
        energy_discount=float(data.get(PLAN_DISCOUNT) or 0) / 100,
    )


def build_hours(rows: dict[str, list[dict[str, Any]]], cpe: str) -> list[HourEnergy]:
    """Assemble per-hour energy (total and per period) from stored series."""
    hours: dict[float, HourEnergy] = {}
    for row in rows.get(statistic_id(cpe), []):
        start = float(row["start"])
        hours[start] = HourEnergy(
            start=datetime.fromtimestamp(start, tz=UTC),
            total=float(row.get("state") or 0.0),
        )
    for cycle in Cycle:
        for period in Period:
            for row in rows.get(period_statistic_id(cpe, cycle, period), []):
                hour = hours.get(float(row["start"]))
                if hour is not None:
                    hour.periods.setdefault(cycle, {})[period] = float(
                        row.get("state") or 0.0
                    )
    return [hours[key] for key in sorted(hours)]


def changed_rows(
    computed: list[tuple[datetime, float]], stored: list[dict[str, Any]]
) -> list[StatisticData]:
    """Return the cumulative cost rows that differ from what is stored."""
    existing = {
        float(row["start"]): (row.get("state"), row.get("sum")) for row in stored
    }
    out: list[StatisticData] = []
    running = 0.0
    for start, cost in computed:
        running += cost
        state, total = existing.get(start.timestamp(), (None, None))
        if (
            state is None
            or total is None
            or abs(float(state) - cost) > COST_EPSILON
            or abs(float(total) - running) > COST_EPSILON
        ):
            out.append(StatisticData(start=start, state=cost, sum=running))
    return out


async def async_recompute_costs(hass: HomeAssistant, entry: ConfigEntry) -> int:
    """Rewrite every plan's cost series where it changed; drop removed plans.

    Returns the number of rows written. Callers hold the CPE's sync lock.
    """
    cpe = entry.data[CONF_CPE]
    plans = {
        subentry_id: subentry
        for subentry_id, subentry in entry.subentries.items()
        if subentry.subentry_type == SUBENTRY_PLAN
    }
    wanted = {cost_statistic_id(cpe, plan_id) for plan_id in plans}

    # Drop the series of plans that no longer exist.
    prefix = cost_statistic_prefix(cpe)
    listed = await async_list_statistic_ids(hass, statistic_type="sum")
    orphans = [
        item["statistic_id"]
        for item in listed
        if item["statistic_id"].startswith(prefix)
        and item["statistic_id"] not in wanted
    ]
    if orphans:
        get_instance(hass).async_clear_statistics(orphans)

    written = 0
    if plans:
        energy_ids = {statistic_id(cpe)} | {
            period_statistic_id(cpe, cycle, period)
            for cycle in Cycle
            for period in Period
        }
        rows = await async_read_rows(hass, energy_ids | wanted, {"state", "sum"})
        hours = build_hours(rows, cpe)
        taxes = taxes_from_options(dict(entry.options))
        currency = hass.config.currency

        for plan_id, subentry in plans.items():
            try:
                plan = plan_from_data(dict(subentry.data))
                computed, _ = estimate_costs(hours, plan, taxes)
            except (KeyError, ValueError) as err:
                _LOGGER.warning(
                    "Skipping plan %s: incomplete settings (%s)",
                    subentry.title,
                    type(err).__name__,
                )
                continue
            stat_id = cost_statistic_id(cpe, plan_id)
            changes = changed_rows(computed, rows.get(stat_id, []))
            metadata = cost_statistic_metadata(cpe, plan_id, subentry.title, currency)
            # Always written, so a renamed plan updates the series name.
            async_add_external_statistics(hass, metadata, changes)
            written += len(changes)

    if orphans or plans:
        await async_recorder_barrier(hass)
    _LOGGER.debug(
        "Costs for %s: %d plan(s), %d row(s) written, %d removed",
        cpe[-8:],
        len(plans),
        written,
        len(orphans),
    )
    return written
