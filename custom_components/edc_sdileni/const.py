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
SSO_REALM_URL = "https://sso.portal.edc-cr.cz/auth/realms/edc"
TOKEN_URL = f"{SSO_REALM_URL}/protocol/openid-connect/token"
AUTH_URL = f"{SSO_REALM_URL}/protocol/openid-connect/auth"
API_URL = "https://api.portal.edc-cr.cz/api/v0/profiles-data/standard/overview"
CLIENT_ID = "a63c22a3-6e1d-4eac-b383-d06373da046a"

# The portal's own SPA is a public OIDC client using authorization code + PKCE
# with this exact redirect URI. Keycloak validates redirect_uri against the
# client's registered list, so this value is not arbitrary.
REDIRECT_URI = "https://portal.edc-cr.cz/"
PORTAL_ORIGIN = "https://portal.edc-cr.cz"

# The portal's frontend sends this on every data call and the backend refuses
# the operation without it (403 SECURITY_OPERATION_NOT_ALLOWED) even when the
# access token is perfectly valid. It names the contract type whose data is
# being requested and matches the `/standard/` segment in API_URL.
EDC_CONTRACT_TYPE = "STANDARD"

# `offline_access` asks Keycloak for an offline refresh token, which does not
# die with the SSO session. That's what lets the integration keep working for
# months without ever replaying the password. If the realm refuses the scope
# we transparently fall back to a plain `openid` login.
SCOPE_OFFLINE = "openid offline_access"
SCOPE_BASIC = "openid"

# Refresh the access token this many seconds before it actually expires, so a
# request never fails just because the token died in flight.
TOKEN_EXPIRY_LEEWAY = 60

LOGIN_TIMEOUT = 30
API_TIMEOUT = 60

# Retry behaviour when the portal/SSO is unreachable or errors out.
RETRY_FIRST_DELAY = 300  # 5 minutes
RETRY_REPEAT_DELAY = 3600  # 1 hour thereafter, until it succeeds

# How far back to re-check for days the portal settled late. Days older than
# this are assumed final and aren't queried again.
RECENT_GAP_DAYS = 35

# Split large backfill ranges into chunks so one flaky request doesn't lose
# all progress, and so we don't ask the API for a huge date range at once.
CHUNK_DAYS = 60

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = f"{DOMAIN}_history"

# Semantics version of what's inside the history store, independent of the HA
# Store format version. Bumped when the meaning of stored numbers changes and
# already-saved data has to be recomputed.
#   1 -> `shared` wrongly held the volume sold to the trader (API's OUT column)
#   2 -> `shared` holds the actually shared volume (IN - OUT)
DATA_VERSION = 2
# Where the (long-lived) refresh token is cached so a Home Assistant restart
# doesn't need to log in again.
TOKEN_STORAGE_KEY_PREFIX = f"{DOMAIN}_tokens"
