"""OMIE day-ahead market prices for Portugal (no Home Assistant imports).

OMIE publishes one public file per day with the marginal price of every
market period, no account or key needed (docs/adr/0007)::

    MARGINALPDBCPT;
    2026;11;16;1;145;145;
    2026;11;16;2;129.93;129.93;
    ...

Columns: year; month; day; period; price A; price B — in €/MWh. Since the
Iberian market moved to 15-minute periods there are 96 per day (92 / 100 on
DST days); older files have 24 (23 / 25) hourly periods. The fifth column is
read as the Portuguese price, an assumption that only matters on the rare days
the two zones split (docs/adr/0007).

Periods count from midnight **Spanish** time (Europe/Madrid), an hour ahead of
Lisbon. They are consecutive real intervals, so the n-th period starts at
Madrid midnight plus n-1 period lengths, in absolute time — which handles the
short and long DST days without special cases.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import aiohttp

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)

MADRID = ZoneInfo("Europe/Madrid")

FILE_URL = (
    "https://www.omie.es/pt/file-download"
    "?parents=marginalpdbcpt&filename=marginalpdbcpt_{day:%Y%m%d}.1"
)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

QUARTER = timedelta(minutes=15)
HOUR = timedelta(hours=1)

# A day with at least this many periods uses 15-minute periods.
MIN_QUARTER_PERIODS = 90


class OmieError(Exception):
    """An OMIE file could not be fetched or understood."""


def parse_marginal_file(text: str, day: date) -> dict[datetime, float]:
    """Return the price of every 15-minute interval of ``day``, in €/kWh.

    Keys are aware UTC interval starts. An hourly file is expanded to four
    quarter-hours per period, so callers always work at 15-minute resolution.
    """
    rows: dict[int, float] = {}
    for line in text.splitlines():
        fields = [field.strip() for field in line.split(";")]
        if len(fields) < 5 or not fields[0].isdigit():
            continue
        try:
            row_day = date(int(fields[0]), int(fields[1]), int(fields[2]))
            period = int(fields[3])
            price = float(fields[4].replace(",", "."))
        except ValueError as err:
            raise OmieError(f"Unreadable OMIE row for {day}") from err
        if row_day != day:
            raise OmieError(f"OMIE file for {day} contains {row_day}")
        rows[period] = price / 1000  # €/MWh -> €/kWh

    if not rows:
        raise OmieError(f"OMIE file for {day} has no prices")
    if sorted(rows) != list(range(1, len(rows) + 1)):
        raise OmieError(f"OMIE file for {day} skips periods")

    length = QUARTER if len(rows) >= MIN_QUARTER_PERIODS else HOUR
    midnight = datetime(day.year, day.month, day.day, tzinfo=MADRID).astimezone(UTC)
    prices: dict[datetime, float] = {}
    for period, price in rows.items():
        start = midnight + (period - 1) * length
        for offset in range(int(length / QUARTER)):
            prices[start + offset * QUARTER] = price
    return prices


def market_days(start: datetime, end: datetime) -> list[date]:
    """Return the Madrid market days covering the UTC instants [start, end]."""
    first = start.astimezone(MADRID).date()
    last = end.astimezone(MADRID).date()
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


async def async_fetch_day(session: ClientSession, day: date) -> str:
    """Download the OMIE marginal price file of one market day."""
    url = FILE_URL.format(day=day)
    try:
        async with session.get(url, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                raise OmieError(f"OMIE answered HTTP {response.status} for {day}")
            return await response.text(encoding="latin-1")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise OmieError(f"Could not reach OMIE for {day}") from err
