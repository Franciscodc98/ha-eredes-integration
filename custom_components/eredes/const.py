"""Constants for the E-REDES integration."""

import re
from typing import Final

DOMAIN: Final = "eredes"

# Configuration keys. A config entry stores the CPE and nothing else.
CONF_CPE: Final = "cpe"

# Keys under which earlier versions stored the access token. Migration to
# version 3 deletes all of them (see async_migrate_entry).
LEGACY_TOKEN_KEYS: Final = ("access_token", "session_cookie", "aat_token")

# Action
SERVICE_SYNC: Final = "sync"
ATTR_TOKEN: Final = "token"
ATTR_CPE: Final = "cpe"
ATTR_DRY_RUN: Final = "dry_run"
ATTR_HISTORY_DAYS: Final = "history_days"

# API
BASE_URL: Final = "https://balcaodigital.e-redes.pt"

# PT followed by 18 alphanumerics (e.g. PT0002000012345678AB), after removing
# spaces and upper-casing.
CPE_PATTERN: Final = re.compile(r"^PT[0-9A-Z]{18}$")


def normalize_cpe(value: str) -> str:
    """Strip whitespace and upper-case a pasted CPE."""
    return "".join(value.split()).upper()
