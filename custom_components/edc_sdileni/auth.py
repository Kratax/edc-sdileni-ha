"""Authentication against the EDC portal's Keycloak SSO.

Why this is more than a single POST
-----------------------------------
The portal's frontend is a public OIDC client (`oidc-client-ts`) that logs in
with **authorization code + PKCE** against Keycloak. The obvious shortcut for a
headless integration is the Resource Owner Password Credentials grant
("password grant"), but in practice that path is fragile: it is a per-client
toggle in Keycloak, it silently bypasses parts of the realm's browser
authentication flow, and any per-user condition attached to that flow (a
required action, a second factor, a federated identity) makes it fail with a
useless `invalid_grant / Invalid user credentials`.

So this module logs in the same way a browser would:

1. `GET /protocol/openid-connect/auth` with a PKCE challenge -> Keycloak
   returns its login page containing a `kc-form-login` form.
2. POST the credentials to that form's action URL, carrying Keycloak's
   cookies. The realm may split this into two steps (username first, then
   password), so we follow up to `_MAX_FORM_STEPS` forms and fill whichever
   fields each one asks for.
3. Keycloak answers with a 302 to the redirect URI carrying `?code=...`. We do
   **not** follow it (nothing is listening there) - we just read the code.
4. Exchange the code for tokens at the token endpoint, passing the PKCE
   verifier.

The login asks for the `offline_access` scope. When the realm grants it, the
resulting refresh token is an *offline* token that outlives the SSO session, so
from that point on the integration only ever refreshes and never replays the
password. If the scope is refused we retry without it, and if the whole browser
flow is unavailable we fall back to the plain password grant.

Everything here uses a throwaway `aiohttp` session with its own cookie jar, so
Keycloak's session cookies never leak into Home Assistant's shared jar.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import async_timeout
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    AUTH_URL,
    CLIENT_ID,
    PORTAL_ORIGIN,
    LOGIN_TIMEOUT,
    REDIRECT_URI,
    SCOPE_BASIC,
    SCOPE_OFFLINE,
    STORAGE_VERSION,
    TOKEN_EXPIRY_LEEWAY,
    TOKEN_STORAGE_KEY_PREFIX,
    TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

# Keycloak realms can chain several forms (username -> password -> ...). Three
# is enough for every layout we care about and stops us looping forever if the
# realm keeps handing back a form we don't know how to fill.
_MAX_FORM_STEPS = 3

_FORM_RE = re.compile(
    r"<form\b[^>]*\bid=[\"']kc-form-login[\"'][^>]*>(.*?)</form>",
    re.IGNORECASE | re.DOTALL,
)
_ANY_FORM_RE = re.compile(
    r"<form\b([^>]*\baction=[\"'][^\"']*login-actions[^\"']*[\"'][^>]*)>(.*?)</form>",
    re.IGNORECASE | re.DOTALL,
)
_ACTION_RE = re.compile(r"\baction=[\"']([^\"']+)[\"']", re.IGNORECASE)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"\b(name|type|value)=[\"']([^\"']*)[\"']", re.IGNORECASE)
# Keycloak renders form/page errors into one of these containers.
_ERROR_RE = re.compile(
    r"<(?:span|div|p)\b[^>]*(?:id=[\"'](?:input-error[\w-]*|kc-error-message)[\"']"
    r"|class=[\"'][^\"']*(?:kc-feedback-text|alert-error|pf-m-error)[^\"']*[\"'])[^>]*>(.*?)"
    r"</(?:span|div|p)>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


class EdcAuthError(UpdateFailed):
    """EDC rejected the credentials (or the account can't log in this way).

    Retrying with the same credentials will not help, so callers should stop
    and ask the user for new ones instead of hammering the SSO.
    """


class EdcCredentialsRejected(EdcAuthError):
    """Keycloak looked at the submitted e-mail/password and said no.

    Distinct from EdcAuthError because it is *definitive*: there is no point
    trying the same credentials again with a different scope or a different
    grant type. Retrying only burns attempts against Keycloak's brute-force
    detection, which is how a working account ends up temporarily locked.
    """


class EdcApiError(UpdateFailed):
    """Anything else: network, timeout, 5xx, unexpected response shape."""


@dataclass
class EdcTokens:
    """Access token plus what we need to renew it without a password."""

    access_token: str
    expires_at: float
    refresh_token: str | None = None
    refresh_expires_at: float | None = None
    offline: bool = False

    @property
    def access_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - TOKEN_EXPIRY_LEEWAY

    @property
    def refresh_valid(self) -> bool:
        if not self.refresh_token:
            return False
        if self.refresh_expires_at is None:
            return True  # offline tokens report expires_in = 0, i.e. "never"
        return time.time() < self.refresh_expires_at - TOKEN_EXPIRY_LEEWAY

    def as_dict(self) -> dict:
        return {
            "refresh_token": self.refresh_token,
            "refresh_expires_at": self.refresh_expires_at,
            "offline": self.offline,
        }


def _tokens_from_payload(payload: dict, *, offline: bool) -> EdcTokens:
    now = time.time()
    access = payload.get("access_token")
    if not access:
        raise EdcApiError("Odpověď SSO neobsahuje access_token")
    refresh_expires_in = payload.get("refresh_expires_in")
    return EdcTokens(
        access_token=access,
        expires_at=now + float(payload.get("expires_in") or 300),
        refresh_token=payload.get("refresh_token"),
        # Keycloak reports refresh_expires_in = 0 for offline tokens, meaning
        # "does not expire". Anything else is a real deadline.
        refresh_expires_at=(
            None
            if not refresh_expires_in
            else now + float(refresh_expires_in)
        ),
        offline=offline,
    )


# --------------------------------------------------------------------- PKCE
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# ------------------------------------------------------------ HTML plumbing
def _extract_login_form(page: str) -> tuple[str, dict[str, str]] | None:
    """Return (action_url, {field_name: prefilled_value}) for Keycloak's form.

    Returns None when the page contains no login form at all - that's how we
    tell "credentials rejected / some other page" apart from "here's the next
    step of the login".
    """
    match = _FORM_RE.search(page)
    if match:
        # The id-based regex captures only the body, so pull the action out of
        # the full opening tag separately.
        open_tag = page[match.start() : match.start(1)]
        body = match.group(1)
    else:
        match = _ANY_FORM_RE.search(page)
        if not match:
            return None
        open_tag = match.group(1)
        body = match.group(2)

    action_match = _ACTION_RE.search(open_tag)
    if not action_match:
        return None
    action = html.unescape(action_match.group(1))

    fields: dict[str, str] = {}
    for raw_input in _INPUT_RE.findall(body):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(raw_input)}
        name = attrs.get("name")
        if not name or attrs.get("type", "").lower() == "submit":
            continue
        fields[name] = html.unescape(attrs.get("value", ""))
    return action, fields


def _extract_error(page: str) -> str | None:
    """Pull Keycloak's own error text out of a rendered page, if any."""
    for raw in _ERROR_RE.findall(page):
        text = html.unescape(_TAG_RE.sub("", raw)).strip()
        text = " ".join(text.split())
        if text:
            return text[:200]
    return None


def _code_from_location(location: str) -> str | None:
    query = parse_qs(urlparse(location).query)
    codes = query.get("code")
    return codes[0] if codes else None


def _describe_token_error(status: int, text: str) -> str:
    """Turn a Keycloak token-endpoint failure into something actionable."""
    error = description = ""
    try:
        payload = json.loads(text)
        error = str(payload.get("error") or "")
        description = str(payload.get("error_description") or "")
    except Exception:  # noqa: BLE001 - non-JSON body, use it verbatim
        description = text[:200]

    hints = {
        "invalid_grant": (
            "EDC odmítlo přihlašovací údaje. Zkontroluj e-mail a heslo — přihlašovací "
            "jméno je e-mail, kterým se hlásíš na portal.edc-cr.cz."
        ),
        "unauthorized_client": (
            "EDC pro tohoto klienta nepovoluje tento typ přihlášení."
        ),
        "unsupported_grant_type": (
            "EDC tento typ přihlášení nepodporuje."
        ),
        "invalid_scope": "EDC nepovolilo požadovaný rozsah oprávnění.",
    }
    hint = hints.get(error)
    parts = [p for p in (hint, description and f"({error or status}: {description})") if p]
    return " ".join(parts) or f"SSO chyba HTTP {status}"


# -------------------------------------------------------------- login flows
async def _async_token_request(session, data: dict, *, offline: bool) -> EdcTokens:
    async with async_timeout.timeout(LOGIN_TIMEOUT):
        async with session.post(
            TOKEN_URL,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                # The portal's SPA sends these; some backend security filters
                # reject requests that don't look like they came from it.
                "Origin": PORTAL_ORIGIN,
                "Referer": REDIRECT_URI,
            },
        ) as resp:
            text = await resp.text()
            if resp.status in (400, 401, 403):
                message = _describe_token_error(resp.status, text)
                if '"invalid_grant"' in text and "credential" in text.lower():
                    raise EdcCredentialsRejected(message)
                raise EdcAuthError(message)
            if resp.status != 200:
                raise EdcApiError(f"SSO chyba HTTP {resp.status}: {text[:200]}")
            return _tokens_from_payload(json.loads(text), offline=offline)


async def _async_browser_login(session, username: str, password: str, scope: str) -> EdcTokens:
    """Authorization code + PKCE, driving Keycloak's own login form."""
    verifier, challenge = _pkce_pair()
    state = _b64url(os.urandom(16))
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "nonce": _b64url(os.urandom(16)),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    async with async_timeout.timeout(LOGIN_TIMEOUT):
        async with session.get(f"{AUTH_URL}?{urlencode(params)}") as resp:
            if resp.status != 200:
                raise EdcApiError(
                    f"SSO nevrátilo přihlašovací stránku (HTTP {resp.status})"
                )
            page = await resp.text()

    code: str | None = None
    remaining = {"username": username, "password": password}

    for step in range(_MAX_FORM_STEPS):
        form = _extract_login_form(page)
        if form is None:
            error = _extract_error(page)
            raise EdcAuthError(
                f"EDC odmítlo přihlášení: {error}"
                if error
                else "EDC odmítlo přihlášení (SSO nevrátilo ani formulář, ani kód)."
            )

        action, fields = form
        # Keep Keycloak's hidden state (session_code, execution, credentialId,
        # ...) exactly as rendered and only fill in what it's asking us for.
        payload = dict(fields)
        asked_for = [name for name in ("username", "password") if name in fields]
        if not asked_for:
            # A form we don't understand: 2FA/OTP, terms, forced password
            # change... Say so plainly rather than POSTing blind.
            unknown = ", ".join(sorted(set(fields) - {"credentialId"})) or "?"
            raise EdcAuthError(
                "EDC vyžaduje další krok přihlášení, který integrace neumí "
                f"(formulář s polem: {unknown}). Přihlas se na portal.edc-cr.cz "
                "v prohlížeči a dokonči, co portál žádá."
            )
        for name in asked_for:
            payload[name] = remaining[name]

        async with async_timeout.timeout(LOGIN_TIMEOUT):
            async with session.post(action, data=payload, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    code = _code_from_location(location)
                    if code:
                        break
                    query = parse_qs(urlparse(location).query)
                    err = (query.get("error_description") or query.get("error") or [""])[0]
                    raise EdcAuthError(
                        f"EDC odmítlo přihlášení: {err}" if err
                        else "SSO přesměrovalo bez autorizačního kódu."
                    )
                if resp.status != 200:
                    raise EdcApiError(
                        f"SSO chyba při odesílání přihlášení (HTTP {resp.status})"
                    )
                page = await resp.text()

        # A 200 means Keycloak re-rendered something: either the next step of
        # the flow, or the same form with an error. An error on a form we just
        # filled completely is a rejection - don't retry it.
        error = _extract_error(page)
        if error and "password" in asked_for:
            raise EdcCredentialsRejected(f"EDC odmítlo přihlášení: {error}")
    else:
        raise EdcAuthError(
            "Přihlášení do EDC se nepodařilo dokončit — SSO stále vrací další "
            "formulář. Zkus se přihlásit na portal.edc-cr.cz v prohlížeči."
        )

    if not code:
        raise EdcAuthError("SSO nevrátilo autorizační kód.")

    return await _async_token_request(
        session,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
        offline="offline_access" in scope,
    )


async def _async_password_grant(session, username: str, password: str, scope: str) -> EdcTokens:
    """Fallback: Resource Owner Password Credentials grant."""
    return await _async_token_request(
        session,
        {
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "username": username,
            "password": password,
            "scope": scope,
        },
        offline="offline_access" in scope,
    )


async def async_login(hass: HomeAssistant, username: str, password: str) -> EdcTokens:
    """Log in, preferring the browser flow and an offline (long-lived) token.

    Order of attempts: browser flow with `offline_access`, browser flow without
    it, then the password grant with and without it. The first success wins;
    the last meaningful error is what the user gets told about.
    """
    # Own cookie jar: Keycloak's session cookies are ours alone and are thrown
    # away as soon as the login finishes.
    session = async_create_clientsession(hass, verify_ssl=True)
    attempts = (
        ("browser", SCOPE_OFFLINE),
        ("browser", SCOPE_BASIC),
        ("password", SCOPE_OFFLINE),
        ("password", SCOPE_BASIC),
    )
    last_auth_error: EdcAuthError | None = None
    last_api_error: EdcApiError | None = None

    try:
        for method, scope in attempts:
            try:
                if method == "browser":
                    tokens = await _async_browser_login(session, username, password, scope)
                else:
                    tokens = await _async_password_grant(session, username, password, scope)
            except EdcCredentialsRejected:
                # Wrong e-mail/password won't become right on the next attempt,
                # and hammering Keycloak gets the account locked. Stop here.
                raise
            except EdcAuthError as err:
                last_auth_error = err
                _LOGGER.debug(
                    "EDC sdílení: přihlášení (%s, scope=%s) odmítnuto: %s", method, scope, err
                )
                continue
            except EdcApiError as err:
                last_api_error = err
                _LOGGER.debug(
                    "EDC sdílení: přihlášení (%s, scope=%s) selhalo technicky: %s",
                    method, scope, err,
                )
                continue
            except Exception as err:  # noqa: BLE001 - network/timeout/parse
                last_api_error = EdcApiError(f"SSO nedostupné: {err}")
                _LOGGER.debug(
                    "EDC sdílení: přihlášení (%s, scope=%s) vyhodilo %s", method, scope, err
                )
                continue

            _LOGGER.debug(
                "EDC sdílení: přihlášení OK (%s, scope=%s, offline_token=%s)",
                method, scope, tokens.offline,
            )
            return tokens
    finally:
        await session.close()

    # Credentials being wrong is the far more actionable diagnosis, so it wins
    # over a transient technical failure when we have both.
    raise last_auth_error or last_api_error or EdcApiError("Přihlášení do EDC selhalo")


async def async_refresh(hass: HomeAssistant, tokens: EdcTokens) -> EdcTokens:
    """Renew an access token from a refresh token. No password involved."""
    if not tokens.refresh_token:
        raise EdcAuthError("Chybí refresh token")
    session = async_create_clientsession(hass, verify_ssl=True)
    try:
        renewed = await _async_token_request(
            session,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": tokens.refresh_token,
            },
            offline=tokens.offline,
        )
    finally:
        await session.close()
    # Keycloak may omit a new refresh token on rotation-disabled realms.
    if not renewed.refresh_token:
        renewed.refresh_token = tokens.refresh_token
        renewed.refresh_expires_at = tokens.refresh_expires_at
    return renewed


class EdcTokenManager:
    """One login per config entry, shared by every EAN's coordinator.

    Previously each coordinator logged in for every fetch, which meant a
    backfill across several EANs replayed the password dozens of times - slow,
    and a good way to get an account temporarily locked. This holds a single
    access token, renews it from the refresh token when it ages out, and only
    falls back to a full login when there is no usable refresh token left.

    The refresh token is cached in Home Assistant's storage, so a restart
    normally costs one refresh call instead of a fresh password login.
    """

    def __init__(self, hass: HomeAssistant, username: str, password: str, entry_id: str) -> None:
        self._hass = hass
        self._username = username
        self._password = password
        self._tokens: EdcTokens | None = None
        self._lock = asyncio.Lock()
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"{TOKEN_STORAGE_KEY_PREFIX}_{entry_id}"
        )

    async def async_load(self) -> None:
        """Pick up a refresh token cached by a previous Home Assistant run."""
        stored = await self._store.async_load()
        if not stored or not stored.get("refresh_token"):
            return
        self._tokens = EdcTokens(
            access_token="",
            expires_at=0.0,
            refresh_token=stored["refresh_token"],
            refresh_expires_at=stored.get("refresh_expires_at"),
            offline=bool(stored.get("offline")),
        )
        _LOGGER.debug("EDC sdílení: nalezen uložený refresh token, heslo nebude potřeba")

    async def _async_persist(self) -> None:
        if self._tokens is None:
            await self._store.async_remove()
            return
        await self._store.async_save(self._tokens.as_dict())

    async def async_get_access_token(self) -> str:
        """Return a currently valid access token, obtaining one if needed."""
        async with self._lock:
            if self._tokens and self._tokens.access_valid:
                return self._tokens.access_token

            if self._tokens and self._tokens.refresh_valid:
                try:
                    self._tokens = await async_refresh(self._hass, self._tokens)
                    await self._async_persist()
                    return self._tokens.access_token
                except EdcAuthError as err:
                    # An expired/revoked refresh token is not a bad password -
                    # drop it and log in properly below.
                    _LOGGER.debug(
                        "EDC sdílení: refresh tokenu neuspěl (%s), přihlásím se znovu", err
                    )
                    self._tokens = None

            self._tokens = await async_login(self._hass, self._username, self._password)
            await self._async_persist()
            return self._tokens.access_token

    async def async_invalidate(self) -> None:
        """Forget everything we know - used when the API rejects our token."""
        async with self._lock:
            self._tokens = None
            await self._async_persist()

    def update_credentials(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._tokens = None


async def async_clear_stored_tokens(hass: HomeAssistant, entry_id: str) -> None:
    """Drop a cached refresh token (e.g. after the user re-enters credentials)."""
    await Store(hass, STORAGE_VERSION, f"{TOKEN_STORAGE_KEY_PREFIX}_{entry_id}").async_remove()
