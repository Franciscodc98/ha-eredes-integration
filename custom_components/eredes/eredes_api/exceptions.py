"""Exceptions for the E-REDES API client.

None of these ever carries the access token: messages are built from status
codes and exception type names only, never from request headers or payloads.
"""


class ERedesError(Exception):
    """Base exception for E-REDES API errors."""


class ERedesAuthenticationError(ERedesError):
    """The credential was refused (HTTP 401): the access token is dead.

    See docs/adr/0003 — this is the only response that says anything about
    the token itself.
    """


class ERedesCaptchaChallengeError(ERedesError):
    """The bot gate wants a challenge solved (HTTP 403 + ``recaptcha`` header).

    Says nothing about the token, which is very likely still valid
    (docs/adr/0003). Deliberately *not* an ``ERedesAuthenticationError``.
    """


class ERedesAccessDeniedError(ERedesError):
    """HTTP 403 without the ``recaptcha`` response header."""


class ERedesHTTPError(ERedesError):
    """Any other non-200 status."""

    def __init__(self, status: int) -> None:
        """Store the HTTP status."""
        super().__init__(f"E-REDES request failed with HTTP status {status}")
        self.status = status


class ERedesConnectionError(ERedesError):
    """Network failure or timeout talking to E-REDES."""


class ERedesUnexpectedResponseError(ERedesError):
    """The response body was not the ``edm/get`` envelope we understand."""


class ERedesUnsuccessfulResponseError(ERedesError):
    """A well-formed envelope carrying ``Body.Success: false``.

    Kept distinct from ``ERedesUnexpectedResponseError`` because the sync may
    tolerate it at the very start of the window (before the contract date).
    """
