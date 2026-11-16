"""E-REDES API client library (no Home Assistant imports)."""

from .client import LISBON, ERedesClient
from .exceptions import (
    ERedesAccessDeniedError,
    ERedesAuthenticationError,
    ERedesCaptchaChallengeError,
    ERedesConnectionError,
    ERedesError,
    ERedesHTTPError,
    ERedesUnexpectedResponseError,
    ERedesUnsuccessfulResponseError,
)
from .models import ConsumptionData, ConsumptionReading

__all__ = [
    "LISBON",
    "ConsumptionData",
    "ConsumptionReading",
    "ERedesAccessDeniedError",
    "ERedesAuthenticationError",
    "ERedesCaptchaChallengeError",
    "ERedesClient",
    "ERedesConnectionError",
    "ERedesError",
    "ERedesHTTPError",
    "ERedesUnexpectedResponseError",
    "ERedesUnsuccessfulResponseError",
]
