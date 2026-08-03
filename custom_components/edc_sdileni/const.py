"""Constants for the EDC sdílení elektřiny integration."""

DOMAIN = "edc_sdileni"

CONF_EAN = "ean"
CONF_EANS = "eans"
CONF_UPDATE_HOUR = "update_hour"
CONF_UPDATE_MINUTE = "update_minute"
CONF_HISTORY_START = "history_start"
CONF_BACKFILL_DAYS = "backfill_days"

DEFAULT_UPDATE_HOUR = 10
DEFAULT_UPDATE_MINUTE = 30
# How far back to backfill if we discover the current month has gaps and no
# explicit history_start was configured. ~6 months.
DEFAULT_BACKFILL_DAYS = 180

# Reverse-engineered from the portal's own frontend network traffic:
# Sprava dat -> Zobrazeni a export dat sdileni elektriny -> "Zobrazit".
# This is NOT an official/public API - see README for caveats.
TOKEN_URL = "https://sso.portal.edc-cr.cz/auth/realms/edc/protocol/openid-connect/token"
API_URL = "https://api.portal.edc-cr.cz/api/v0/profiles-data/standard/overview"
CLIENT_ID = "a63c22a3-6e1d-4eac-b383-d06373da046a"

# Retry behaviour when the portal/SSO is unreachable or errors out.
RETRY_FIRST_DELAY = 300  # 5 minutes
RETRY_REPEAT_DELAY = 3600  # 1 hour thereafter, until it succeeds

# Split large backfill ranges into chunks so one flaky request doesn't lose
# all progress, and so we don't ask the API for a huge date range at once.
CHUNK_DAYS = 60

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}_history"
