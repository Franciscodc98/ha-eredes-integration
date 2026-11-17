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


# Tariff plans (config subentries)
SUBENTRY_PLAN: Final = "plan"
PLAN_OPTION: Final = "option"
PLAN_CYCLE: Final = "cycle"
PLAN_POWER_PER_DAY: Final = "power_per_day"
PLAN_DISCOUNT: Final = "energy_discount_percent"
# Energy price key used by tariffs.Plan -> subentry data field.
PLAN_PRICE_KEYS: Final = {
    "simple": "price_simple",
    "vazio": "price_vazio",
    "fora_vazio": "price_fora_vazio",
    "cheias": "price_cheias",
    "ponta": "price_ponta",
}

# Taxes (entry options). Rates are percentages in the UI.
OPT_VAT_REDUCED: Final = "vat_reduced_percent"
OPT_VAT_NORMAL: Final = "vat_normal_percent"
OPT_REDUCED_KWH: Final = "vat_reduced_kwh_per_30_days"
OPT_VAT_POWER: Final = "vat_power_percent"
OPT_IEC_PER_KWH: Final = "iec_per_kwh"
OPT_DGEG_PER_MONTH: Final = "dgeg_per_month"
OPT_CAV_PER_MONTH: Final = "cav_per_month"
OPT_VAT_CAV: Final = "vat_cav_percent"
