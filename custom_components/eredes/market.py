"""OMIE price cache: fetch each market day once, keep it in ``.storage``.

Prices are public and identical for every user, so they are cached under one
Home Assistant storage key and only missing days are downloaded. Fetching
stops at the first failure: the market series must stay contiguous, and the
next sync retries from there (docs/adr/0007).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .omie import OmieError, async_fetch_day, parse_marginal_file

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.omie_prices"
STORAGE_VERSION = 1

# Pause between OMIE downloads, to stay polite.
OMIE_PAUSE_SECONDS = 0.5


@dataclass
class MarketPrices:
    """Quarter-hour prices (€/kWh, keyed by UTC start) and what is missing."""

    prices: dict[datetime, float] = field(default_factory=dict)
    days_fetched: int = 0
    first_missing_day: date | None = None


async def async_market_prices(hass: HomeAssistant, days: list[date]) -> MarketPrices:
    """Return the OMIE prices of ``days``, downloading the ones not cached."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    cached: dict[str, Any] = await store.async_load() or {}
    result = MarketPrices()

    session = async_get_clientsession(hass)
    for day in days:
        key = day.isoformat()
        if key not in cached:
            if result.days_fetched:
                await asyncio.sleep(OMIE_PAUSE_SECONDS)
            try:
                prices = parse_marginal_file(await async_fetch_day(session, day), day)
            except OmieError as err:
                _LOGGER.warning("OMIE prices unavailable from %s: %s", key, err)
                result.first_missing_day = day
                break
            cached[key] = [
                [start.timestamp(), price] for start, price in prices.items()
            ]
            result.days_fetched += 1
        for timestamp, price in cached[key]:
            result.prices[datetime.fromtimestamp(timestamp, tz=UTC)] = float(price)

    if result.days_fetched:
        await store.async_save(cached)
    return result
