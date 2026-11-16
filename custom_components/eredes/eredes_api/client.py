"""E-REDES API client."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import aiohttp

from .exceptions import (
    ERedesAccessDeniedError,
    ERedesAuthenticationError,
    ERedesCaptchaChallengeError,
    ERedesConnectionError,
    ERedesHTTPError,
    ERedesUnexpectedResponseError,
    ERedesUnsuccessfulResponseError,
)
from .models import ConsumptionData, ConsumptionReading

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://balcaodigital.e-redes.pt"
API_URL = f"{BASE_URL}/ms/reading/data-usage/edm/get"

# Load-curve timestamps are Lisbon wall-clock time despite their ``Z`` suffix.
LISBON = ZoneInfo("Europe/Lisbon")

# A 31-day load curve is ~3000 points and the request asks the server to wait.
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)


def _to_utc_series(timestamps: list[datetime]) -> list[datetime]:
    """Convert naive Europe/Lisbon wall-clock timestamps to aware UTC.

    E-REDES labels load-curve timestamps with a ``Z`` but reports local Lisbon
    time (see CONTEXT.md → Load curve), so the UTC offset changes across DST.

    The autumn transition repeats one wall-clock hour, and the API returns both
    copies. The first occurrence of a repeated value is read as the
    pre-transition (WEST, UTC+1) reading and the second as post-transition
    (WET, UTC+0) — correct as long as the series arrives in chronological
    order, which is the only ordering that makes the duplicates meaningful.
    Without this, both copies collapse onto the same instant and one hour of
    consumption is double-counted into the other.

    The spring transition needs no handling: the skipped hour simply has no
    readings, leaving a gap that closes naturally in UTC.
    """
    seen: set[datetime] = set()
    utc_timestamps: list[datetime] = []
    for naive in timestamps:
        local = naive.replace(tzinfo=LISBON, fold=1 if naive in seen else 0)
        seen.add(naive)
        utc_timestamps.append(local.astimezone(UTC))
    return utc_timestamps


def _is_recaptcha_challenge(headers: Any) -> bool:
    """Return True if a response carries the bot gate's ``recaptcha`` header.

    The portal answers ``recaptcha: true`` (docs/adr/0003); any value other
    than an explicit false-ish one is read as a challenge.
    """
    value = headers.get("recaptcha")
    if value is None:
        return False
    return str(value).strip().lower() not in {"false", "0", ""}


class ERedesClient:
    """Client for the E-REDES ``edm/get`` endpoint.

    One instance is built per sync from the pasted token and discarded
    afterwards; the token is never persisted.
    """

    def __init__(
        self,
        session: ClientSession,
        access_token: str,
    ) -> None:
        """Initialize the E-REDES client with an access token.

        Args:
            session: aiohttp ClientSession
            access_token: The pasted authentication value. Any of these
                shapes are accepted and treated identically:
                - a bare ``aat`` token value (``eyJ...``)
                - a prefixed pair (``aat=eyJ...``)
                - a full Cookie header
                  (``PHPSESSID=xxx; aat=xxx; SimpleSAML=xxx``)
        """
        self._session = session
        self._cookies = self._normalize_cookies(access_token)
        self._aat_token = self._cookies.get("aat", "")
        self._cookie_header = self._build_cookie_header(self._cookies)

    def __repr__(self) -> str:
        """Never expose the credential, even in a debugger or a log line."""
        return "ERedesClient(access_token=<redacted>)"

    @property
    def has_token(self) -> bool:
        """Return True if the pasted value yielded an ``aat`` token."""
        return bool(self._aat_token)

    def _normalize_cookies(self, access_token: str) -> dict[str, str]:
        """Normalize a pasted authentication value into a cookies dict.

        The input is trimmed and parsed as a Cookie header. A bare token value
        contains no ``key=value`` pair, so parsing yields no ``aat`` key; in
        that case the whole trimmed input is treated as the ``aat`` token.
        """
        stripped = access_token.strip()
        cookies = self._parse_cookies(stripped)
        if "aat" not in cookies:
            bare = stripped.rstrip(";").strip()
            if bare:
                cookies["aat"] = bare
        return cookies

    @staticmethod
    def _parse_cookies(cookie_string: str) -> dict[str, str]:
        """Parse a cookie string into a dictionary."""
        cookies: dict[str, str] = {}
        simple_cookie: SimpleCookie = SimpleCookie()
        try:
            simple_cookie.load(cookie_string)
        except CookieError:
            return cookies
        for key, morsel in simple_cookie.items():
            cookies[key] = morsel.value
        return cookies

    @staticmethod
    def _build_cookie_header(cookies: dict[str, str]) -> str:
        """Serialize a cookies dict into a Cookie request header."""
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    def _get_headers(self) -> dict[str, str]:
        """Get headers for API requests."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
            ),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/consumptions/history",
            "User-Agent-Context": "WEB",
            "Show-Loader": "true",
            "Cookie": self._cookie_header,
        }
        # Load-bearing despite the name: the gateway only checks this header is
        # present on /ms/* data endpoints, and refuses the request with a 403
        # without it. Do not remove — see docs/adr/0003.
        if self._aat_token:
            headers["Authorization-Request"] = self._aat_token
        return headers

    async def get_consumption(
        self,
        cpe: str,
        start_date: datetime,
        end_date: datetime,
    ) -> ConsumptionData:
        """Fetch the 15-minute load curve for the specified date range.

        Args:
            cpe: The CPE (delivery point) identifier
            start_date: Start of the range, naive Europe/Lisbon wall clock
            end_date: End of the range, naive Europe/Lisbon wall clock

        Returns:
            ConsumptionData with aware-UTC readings for the period

        Raises:
            ERedesAuthenticationError: HTTP 401, the token is dead
            ERedesCaptchaChallengeError: HTTP 403 with a ``recaptcha`` header
            ERedesAccessDeniedError: any other HTTP 403
            ERedesHTTPError: any other non-200 status
            ERedesConnectionError: network failure or timeout
            ERedesUnsuccessfulResponseError: ``Body.Success`` is false
            ERedesUnexpectedResponseError: the body could not be understood
        """
        payload = {
            "cpe": cpe,
            "request_type": "3",  # 15-minute interval readings
            "start_date": start_date.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end_date.strftime("%Y-%m-%d %H:%M:%S"),
            "wait": True,
            "formatted": False,
            "nif_requester": None,
            "serial_number": "",
            "nif": None,
        }

        try:
            async with self._session.post(
                API_URL,
                json=payload,
                headers=self._get_headers(),
                timeout=REQUEST_TIMEOUT,
            ) as response:
                self._refresh_session_from_response(response)

                if response.status == 401:
                    raise ERedesAuthenticationError("E-REDES refused the access token")

                if response.status == 403:
                    if _is_recaptcha_challenge(response.headers):
                        raise ERedesCaptchaChallengeError(
                            "E-REDES answered with a reCAPTCHA challenge"
                        )
                    raise ERedesAccessDeniedError("E-REDES denied access (HTTP 403)")

                if response.status != 200:
                    raise ERedesHTTPError(response.status)

                try:
                    data = await response.json(content_type=None)
                except ValueError as ex:
                    raise ERedesUnexpectedResponseError(
                        "E-REDES returned a body that is not JSON"
                    ) from ex

        except (aiohttp.ClientError, TimeoutError) as ex:
            # Only the exception type: aiohttp messages are safe today, but the
            # request carries the credential and nothing here should echo it.
            _LOGGER.debug("Request to E-REDES failed: %s", type(ex).__name__)
            raise ERedesConnectionError(
                f"Failed to reach E-REDES ({type(ex).__name__})"
            ) from ex

        return self._parse_consumption_response(cpe, data, start_date, end_date)

    def _refresh_session_from_response(self, response: Any) -> None:
        """Roll the secondary PHPSESSID cookie from a Set-Cookie header.

        Not a credential (CONTEXT.md → Server session); kept only so the
        requests of one sync look like one browser session.
        """
        set_cookie = response.headers.get("Set-Cookie", "")
        if "PHPSESSID=" in set_cookie:
            match = re.search(r"PHPSESSID=([^;]+)", set_cookie)
            if match:
                new_phpsessid = match.group(1)
                if new_phpsessid != self._cookies.get("PHPSESSID", ""):
                    self._cookies["PHPSESSID"] = new_phpsessid
                    self._cookie_header = self._build_cookie_header(self._cookies)

    def _parse_consumption_response(
        self,
        cpe: str,
        data: Any,
        start_date: datetime,
        end_date: datetime,
    ) -> ConsumptionData:
        """Parse the consumption API response.

        Response format:
        {
            "Body": {
                "Success": true,
                "Result": {
                    "utilitiesDevices": [{
                        "meterLoadCurves": [{
                            "register": "A+",
                            "loadCurves": [{
                                "loadCurveTimestamp": "2026-01-05T00:15:00Z",
                                "meterLoadCurve": 0.052,
                                "meterLoadCurveUnitMeasurement": "kwh"
                            }]
                        }]
                    }]
                }
            }
        }

        A structural surprise raises instead of returning a partial series: a
        silently short series would be written as a hole that is never
        re-fetched. Individual points without a value or with an unparseable
        timestamp are skipped; the resulting short hour is reported by the sync.
        """
        if not isinstance(data, dict) or not isinstance(data.get("Body"), dict):
            raise ERedesUnexpectedResponseError("E-REDES response has no Body")
        body = data["Body"]

        if not body.get("Success", False):
            raise ERedesUnsuccessfulResponseError(
                "E-REDES returned an unsuccessful response"
            )

        readings: list[ConsumptionReading] = []
        try:
            result = body.get("Result") or {}
            for device in result.get("utilitiesDevices") or []:
                for group in device.get("meterLoadCurves") or []:
                    # Only "A+" (active energy imported from the grid).
                    if group.get("register", "") != "A+":
                        continue

                    # Collect the group's series before converting: resolving
                    # the repeated autumn hour needs the readings in order,
                    # not one timestamp at a time.
                    local_timestamps: list[datetime] = []
                    values_wh: list[float] = []
                    for curve in group.get("loadCurves") or []:
                        timestamp_str = curve.get("loadCurveTimestamp")
                        value = curve.get("meterLoadCurve")
                        unit = str(
                            curve.get("meterLoadCurveUnitMeasurement") or ""
                        ).lower()

                        if not timestamp_str or value is None:
                            continue
                        # Naive Lisbon wall clock — the trailing Z is a lie
                        timestamp = self._parse_timestamp(str(timestamp_str))
                        if timestamp is None:
                            continue
                        # Value is in kWh, convert to Wh for internal use
                        values_wh.append(
                            float(value) * 1000 if unit == "kwh" else float(value)
                        )
                        local_timestamps.append(timestamp)

                    readings.extend(
                        ConsumptionReading(timestamp=timestamp, value_wh=value_wh)
                        for timestamp, value_wh in zip(
                            _to_utc_series(local_timestamps), values_wh, strict=True
                        )
                    )
        except (AttributeError, KeyError, TypeError, ValueError) as ex:
            raise ERedesUnexpectedResponseError(
                f"Could not parse the E-REDES load curve ({type(ex).__name__})"
            ) from ex

        readings.sort(key=lambda r: r.timestamp)

        return ConsumptionData(
            cpe=cpe,
            readings=readings,
            start_date=start_date,
            end_date=end_date,
        )

    @staticmethod
    def _parse_timestamp(timestamp_str: str) -> datetime | None:
        """Parse a load-curve timestamp into a naive Lisbon wall-clock time.

        The ``Z`` suffix is stripped by the format, not honoured: these are
        local times (see ``_to_utc_series``). The result stays naive; the
        caller converts the series to UTC once it has the readings in order.
        """
        formats = [
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ]
        for fmt in formats:
            try:
                return datetime.strptime(timestamp_str, fmt)
            except ValueError:
                continue
        return None
