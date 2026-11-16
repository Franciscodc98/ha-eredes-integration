"""Tests for the E-REDES API client: token normalization and HTTP error mapping."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.eredes.eredes_api import (
    ERedesAccessDeniedError,
    ERedesAuthenticationError,
    ERedesCaptchaChallengeError,
    ERedesConnectionError,
    ERedesError,
    ERedesHTTPError,
    ERedesUnexpectedResponseError,
    ERedesUnsuccessfulResponseError,
)
from custom_components.eredes.eredes_api.client import API_URL, ERedesClient

from .conftest import TOKEN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.test_util.aiohttp import (
        AiohttpClientMocker,
    )

START = datetime(2026, 1, 1)
END = datetime(2026, 1, 2)

OK_BODY = {
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
                                    "loadCurveTimestamp": "2026-01-01T00:00:00Z",
                                    "meterLoadCurve": 0.1,
                                    "meterLoadCurveUnitMeasurement": "kWh",
                                }
                            ],
                        }
                    ]
                }
            ]
        },
    }
}


def _make_client(access_token: str) -> ERedesClient:
    """Build a client with a stub session (the session is unused by headers)."""
    return ERedesClient(MagicMock(), access_token)


# --- token normalization -----------------------------------------------------


@pytest.mark.parametrize(
    "access_token",
    [
        pytest.param(TOKEN, id="bare-value"),
        pytest.param(f"aat={TOKEN}", id="prefixed"),
        pytest.param(f"  {TOKEN}  ", id="bare-with-whitespace"),
        pytest.param(f"aat={TOKEN};", id="prefixed-trailing-semicolon"),
        pytest.param(f"  aat={TOKEN} ; ", id="prefixed-whitespace-and-semicolon"),
    ],
)
def test_aat_token_normalized_from_various_shapes(access_token: str) -> None:
    """Bare, prefixed and whitespace/semicolon-padded inputs all yield the token."""
    client = _make_client(access_token)
    headers = client._get_headers()

    assert client._aat_token == TOKEN
    assert headers["Authorization-Request"] == TOKEN
    assert headers["Cookie"] == f"aat={TOKEN}"


def test_full_cookie_header_extracts_aat_and_forwards_all_cookies() -> None:
    """A full Cookie header keeps every cookie and extracts the aat token."""
    client = _make_client(f"  PHPSESSID=abc123; aat={TOKEN}; SimpleSAML=xyz789 ; ")
    headers = client._get_headers()

    assert client._aat_token == TOKEN
    assert headers["Authorization-Request"] == TOKEN
    assert "PHPSESSID=abc123" in headers["Cookie"]
    assert f"aat={TOKEN}" in headers["Cookie"]
    assert "SimpleSAML=xyz789" in headers["Cookie"]


def test_blank_input_yields_no_token() -> None:
    """A blank input produces no aat token and omits the gate header."""
    client = _make_client("   ")

    assert not client.has_token
    assert "Authorization-Request" not in client._get_headers()


def test_repr_redacts_token() -> None:
    """The client never shows its credential."""
    client = _make_client(TOKEN)

    assert TOKEN not in repr(client)
    assert TOKEN not in str(client)


# --- HTTP ---------------------------------------------------------------------


async def _fetch(hass: HomeAssistant) -> object:
    client = ERedesClient(async_get_clientsession(hass), f"aat={TOKEN}")
    return await client.get_consumption("PT0002000012345678AB", START, END)


async def test_request_sends_token_in_cookie_and_gate_header(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The aat travels in Cookie *and* Authorization-Request (docs/adr/0003)."""
    aioclient_mock.post(API_URL, json=OK_BODY)

    await _fetch(hass)

    _, url, payload, headers = aioclient_mock.mock_calls[0]
    assert str(url) == API_URL
    assert headers["Authorization-Request"] == TOKEN
    assert headers["Cookie"] == f"aat={TOKEN}"
    assert payload["start_date"] == "2026-01-01 00:00:00"
    assert payload["end_date"] == "2026-01-02 00:00:00"


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        pytest.param(401, {}, ERedesAuthenticationError, id="401"),
        pytest.param(
            403, {"recaptcha": "true"}, ERedesCaptchaChallengeError, id="403-recaptcha"
        ),
        pytest.param(403, {}, ERedesAccessDeniedError, id="403-plain"),
        pytest.param(500, {}, ERedesHTTPError, id="500"),
        pytest.param(400, {}, ERedesHTTPError, id="400"),
    ],
)
async def test_http_status_mapping(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    headers: dict[str, str],
    expected: type[ERedesError],
) -> None:
    """401 and 403-with-recaptcha are distinct; neither carries the token."""
    aioclient_mock.post(API_URL, status=status, headers=headers)

    with pytest.raises(expected) as err:
        await _fetch(hass)

    assert TOKEN not in str(err.value)


async def test_captcha_is_not_an_authentication_error() -> None:
    """A bot challenge must never be read as a dead token (docs/adr/0003)."""
    assert not issubclass(ERedesCaptchaChallengeError, ERedesAuthenticationError)


async def test_network_error_maps_to_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Transport failures become ERedesConnectionError."""
    aioclient_mock.post(API_URL, exc=aiohttp.ClientConnectionError("boom"))

    with pytest.raises(ERedesConnectionError):
        await _fetch(hass)


async def test_timeout_maps_to_connection_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A timeout is a connection error too."""
    aioclient_mock.post(API_URL, exc=TimeoutError())

    with pytest.raises(ERedesConnectionError):
        await _fetch(hass)


async def test_non_json_body_is_unexpected(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTML page with 200 (e.g. a login redirect) is not silently empty."""
    aioclient_mock.post(API_URL, text="<html>login</html>")

    with pytest.raises(ERedesUnexpectedResponseError):
        await _fetch(hass)


async def test_unsuccessful_body_is_distinct(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """``Success: false`` raises its own error, so the sync can tolerate it."""
    aioclient_mock.post(API_URL, json={"Body": {"Success": False}})

    with pytest.raises(ERedesUnsuccessfulResponseError):
        await _fetch(hass)


async def test_ok_body_parses(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 200 with a load curve yields readings."""
    aioclient_mock.post(API_URL, json=OK_BODY)

    data = await _fetch(hass)

    assert len(data.readings) == 1  # type: ignore[attr-defined]
