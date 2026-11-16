"""Fixtures for E-REDES tests. All identifiers and values are synthetic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from custom_components.eredes.eredes_api import (
    LISBON,
    ConsumptionData,
    ConsumptionReading,
    ERedesError,
    ERedesUnsuccessfulResponseError,
)

if TYPE_CHECKING:
    from collections.abc import Generator

# Synthetic delivery point and token — not real.
CPE = "PT0002000012345678AB"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.c3ludGhldGljLXNpZw"

QUARTER = timedelta(minutes=15)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations for all tests."""


@pytest.fixture(autouse=True)
def expected_lingering_threads() -> bool:
    """Allow lingering threads from aiohttp's safe shutdown loop."""
    return True


@pytest.fixture(autouse=True)
def no_request_pause() -> Generator[None]:
    """Don't sleep between request blocks in tests."""
    with patch("custom_components.eredes.sync.REQUEST_PAUSE_SECONDS", 0):
        yield


def lisbon_to_utc(naive: datetime) -> datetime:
    """Interpret a naive Lisbon wall clock as an aware UTC instant."""
    return naive.replace(tzinfo=LISBON).astimezone(UTC)


@dataclass
class FakePortal:
    """A synthetic ``edm/get``: one reading per UTC quarter-hour in range.

    - Readings exist from ``contract_start`` up to (excluding)
      ``published_until``; each quarter is ``kwh_per_quarter``.
    - A block entirely before ``contract_start`` answers ``Success: false``
      when ``unsuccessful_before_contract`` is set, otherwise empty.
    - ``errors`` maps a zero-based call index to an exception to raise.
    - ``missing`` lists UTC quarter instants to leave out.
    """

    contract_start: datetime
    published_until: datetime
    kwh_per_quarter: float = 0.25
    unsuccessful_before_contract: bool = False
    errors: dict[int, ERedesError] = field(default_factory=dict)
    missing: set[datetime] = field(default_factory=set)
    calls: list[tuple[datetime, datetime]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    async def get_consumption(
        self, cpe: str, start_date: datetime, end_date: datetime
    ) -> ConsumptionData:
        """Answer one request block."""
        index = len(self.calls)
        self.calls.append((start_date, end_date))
        if index in self.errors:
            raise self.errors[index]

        start = lisbon_to_utc(start_date)
        end = lisbon_to_utc(end_date)
        if end <= self.contract_start and self.unsuccessful_before_contract:
            raise ERedesUnsuccessfulResponseError("synthetic")

        moment = max(start, self.contract_start)
        # Align to the quarter grid.
        moment = moment.replace(second=0, microsecond=0)
        moment -= timedelta(minutes=moment.minute % 15)
        if moment < start:
            moment += QUARTER
        readings: list[ConsumptionReading] = []
        while moment < min(end, self.published_until):
            if moment not in self.missing:
                readings.append(
                    ConsumptionReading(
                        timestamp=moment, value_wh=self.kwh_per_quarter * 1000
                    )
                )
            moment += QUARTER
        return ConsumptionData(
            cpe=cpe, readings=readings, start_date=start_date, end_date=end_date
        )


@pytest.fixture
def portal() -> FakePortal:
    """A portal with a year of contract and data published up to 10:30 UTC."""
    return FakePortal(
        contract_start=datetime(2025, 1, 1, tzinfo=UTC),
        published_until=datetime(2026, 3, 9, 10, 30, tzinfo=UTC),
    )


@pytest.fixture
def mock_client(portal: FakePortal) -> Generator[FakePortal]:
    """Route the sync's ERedesClient to the fake portal, recording tokens."""

    class _Client:
        def __init__(self, _session: object, access_token: str) -> None:
            portal.tokens.append(access_token)
            self.has_token = bool(access_token.strip())

        async def get_consumption(
            self, cpe: str, start_date: datetime, end_date: datetime
        ) -> ConsumptionData:
            return await portal.get_consumption(cpe, start_date, end_date)

    with patch("custom_components.eredes.sync.ERedesClient", _Client):
        yield portal
