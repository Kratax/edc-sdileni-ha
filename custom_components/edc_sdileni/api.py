"""Thin client for EDC portal's internal (undocumented) JSON API."""
from __future__ import annotations

import json
import logging
from datetime import date

import async_timeout
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import API_URL, CLIENT_ID, TOKEN_URL

_LOGGER = logging.getLogger(__name__)


class EdcAuthError(UpdateFailed):
    """Username/password rejected by EDC SSO (wrong credentials, or the
    'password' grant type disabled for this client)."""


class EdcApiError(UpdateFailed):
    """Any other failure talking to EDC (network, timeout, 5xx, ...)."""


async def async_get_access_token(session, username: str, password: str) -> str:
    """Log in via Keycloak's Resource Owner Password Credentials grant."""
    data = {
        "grant_type": "password",
        "client_id": CLIENT_ID,
        "username": username,
        "password": password,
        "scope": "openid",
    }
    try:
        async with async_timeout.timeout(30):
            async with session.post(TOKEN_URL, data=data) as resp:
                text = await resp.text()
                if resp.status in (400, 401, 403):
                    _LOGGER.error(
                        "EDC sdílení: přihlášení odmítnuto (HTTP %s): %s", resp.status, text[:300]
                    )
                    raise EdcAuthError(
                        f"Přihlášení do EDC odmítnuto (HTTP {resp.status}): {text[:300]}"
                    )
                if resp.status != 200:
                    raise EdcApiError(f"EDC SSO chyba (HTTP {resp.status}): {text[:300]}")
                payload = json.loads(text)
                token = payload.get("access_token")
                if not token:
                    raise EdcApiError(f"EDC token response bez access_token: {text[:300]}")
                return token
    except (EdcAuthError, EdcApiError):
        raise
    except Exception as err:  # noqa: BLE001 - network/timeout/etc
        raise EdcApiError(f"EDC SSO nedostupné: {err}") from err


async def async_fetch_overview(
    session, token: str, ean: str, date_from: date, date_to: date
) -> dict:
    """Fetch the raw 15-min overview payload for [date_from, date_to] (inclusive).

    The response also contains `missingEans`: EANs the portal doesn't
    recognise at all for this account (typo, not registered, no longer
    shared, ...). Callers should check that list and stop retrying such
    EANs rather than hammering the API for something that will never exist.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    body = {
        "eans": [ean],
        "currentEnteredDateTime": f"{date.today().isoformat()}T00:00:00.000Z",
        "inputData": True,
        "outputData": True,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "fileName": "_",
    }
    try:
        async with async_timeout.timeout(60):
            async with session.post(API_URL, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status in (401, 403):
                    raise EdcAuthError(f"EDC API odmítlo token (HTTP {resp.status}): {text[:300]}")
                if resp.status != 200:
                    raise EdcApiError(f"EDC API chyba (HTTP {resp.status}): {text[:300]}")
                return json.loads(text)
    except (EdcAuthError, EdcApiError):
        raise
    except Exception as err:  # noqa: BLE001
        raise EdcApiError(f"EDC API nedostupné: {err}") from err


def parse_days(payload: dict, ean: str) -> dict[str, dict[str, float]]:
    """Sum 15-min interval values into per-day totals.

    Only days that are ACTUALLY present in the response are returned - if the
    portal hasn't processed/settled a day yet it simply won't appear in
    `content`, and we must not invent a zero entry for it (that would
    permanently hide the gap instead of retrying later).
    """
    columns = payload.get("valueColumns", [])
    in_idx = next(
        (i for i, c in enumerate(columns) if c.get("dir") == "IN" and c.get("ean") == ean), None
    )
    out_idx = next(
        (i for i, c in enumerate(columns) if c.get("dir") == "OUT" and c.get("ean") == ean), None
    )

    days: dict[str, dict[str, float]] = {}
    for row in payload.get("content", []):
        d = row.get("date")
        if not d:
            continue
        bucket = days.setdefault(d, {"measured": 0.0, "shared": 0.0})
        values = row.get("values", [])
        if in_idx is not None and in_idx < len(values):
            bucket["measured"] += values[in_idx].get("v") or 0.0
        if out_idx is not None and out_idx < len(values):
            bucket["shared"] += values[out_idx].get("v") or 0.0

    for d in days:
        days[d]["measured"] = round(days[d]["measured"], 3)
        days[d]["shared"] = round(days[d]["shared"], 3)
    return days


def ean_is_missing(payload: dict, ean: str) -> bool:
    """True if the portal explicitly reports this EAN as unknown."""
    return ean in (payload.get("missingEans") or [])
