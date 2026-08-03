"""Thin client for EDC portal's internal (undocumented) JSON API.

Authentication lives in `auth.py` - this module only knows how to call the data
endpoint with an already-valid access token and how to make sense of what comes
back.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timezone

import async_timeout

from .auth import EdcApiError, EdcAuthError
from .const import API_TIMEOUT, API_URL, EDC_CONTRACT_TYPE, PORTAL_ORIGIN, REDIRECT_URI

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "EdcApiError",
    "EdcAuthError",
    "async_fetch_overview",
    "ean_is_missing",
    "parse_days",
    "parse_intervals",
]


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
        # Not optional: without it the backend answers 403
        # SECURITY_OPERATION_NOT_ALLOWED even for a valid token, because it has
        # no idea which contract type the request is about.
        "Edc-Contract-Type": EDC_CONTRACT_TYPE,
        # The portal tags every call with a fresh correlation id; mirroring that
        # keeps our requests indistinguishable from its own and gives EDC
        # something to grep for if they ever need to trace one.
        "X-Correlation-ID": str(uuid.uuid4()),
        "Origin": PORTAL_ORIGIN,
        "Referer": REDIRECT_URI,
    }
    # The portal sends the actual current UTC instant with milliseconds, not
    # midnight. Matching it exactly removes one more way our request could look
    # different from the frontend's.
    now = datetime.now(timezone.utc)
    body = {
        "eans": [ean],
        "currentEnteredDateTime": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        "inputData": True,
        "outputData": True,
        "dateFrom": date_from.isoformat(),
        "dateTo": date_to.isoformat(),
        "fileName": "_",
    }
    try:
        async with async_timeout.timeout(API_TIMEOUT):
            async with session.post(API_URL, json=body, headers=headers) as resp:
                text = await resp.text()
                if resp.status in (401, 403):
                    raise EdcAuthError(
                        f"EDC API odmítlo token (HTTP {resp.status}): {text[:200]}"
                    )
                if resp.status != 200:
                    raise EdcApiError(f"EDC API chyba (HTTP {resp.status}): {text[:200]}")
                return json.loads(text)
    except (EdcAuthError, EdcApiError):
        raise
    except Exception as err:  # noqa: BLE001
        raise EdcApiError(f"EDC API nedostupné: {err}") from err


def _column_indexes(payload: dict, ean: str) -> tuple[int | None, int | None]:
    """Find which entries in each row's `values` belong to this EAN.

    `valueColumns` describes the layout. What the two directions actually mean
    was verified against the portal's own "Podíl spotřeby energie" figures for
    2026-07-31:

        IN  = 41.52 kWh  total metered delivery (export)
        OUT = 39.84 kWh  the portal's "Prodáno obchodníkovi"
        -> successfully shared = IN - OUT = 1.68 kWh, matching the portal's
           "Sdílená energie" and its 4.0 % share

    So OUT is *not* the shared volume - it is the part of the export that left
    the sharing scheme and was sold to the trader. Earlier versions treated OUT
    as "shared", which reported the two the wrong way round.

    Positions in `values` are not guaranteed, so they're always looked up by
    `dir` rather than assumed.
    """
    columns = payload.get("valueColumns", [])
    in_idx = next(
        (i for i, c in enumerate(columns) if c.get("dir") == "IN" and c.get("ean") == ean), None
    )
    out_idx = next(
        (i for i, c in enumerate(columns) if c.get("dir") == "OUT" and c.get("ean") == ean), None
    )
    return in_idx, out_idx


def _value_at(values: list, idx: int | None) -> float:
    if idx is None or idx >= len(values):
        return 0.0
    return values[idx].get("v") or 0.0


def _split(values: list, in_idx: int | None, out_idx: int | None) -> tuple[float, float]:
    """One interval's (total export, successfully shared) in kWh.

    Shared is a derived number, not something the API hands over: it's the
    export minus what was sold to the trader. Clamped at zero because rounding
    in the source data can otherwise produce a tiny negative share.
    """
    exported = _value_at(values, in_idx)
    sold_to_trader = _value_at(values, out_idx)
    return exported, max(0.0, exported - sold_to_trader)


def parse_intervals(payload: dict, ean: str, day: str) -> dict | None:
    """Pull the 15-minute curve for a single day out of the same payload.

    The API already returns this detail - `parse_days` just sums it away - so
    this costs no extra request. Returns None when the day isn't in the
    payload at all.

    The times come from the response rather than being computed as
    `index * 15 min`, because clock-change days have 92 or 100 intervals
    instead of 96 and generated times would drift by an hour after the switch.

    Shape is three parallel arrays instead of a list of objects to keep the
    entity attribute small (~2 kB instead of ~6 kB for 96 intervals).
    """
    in_idx, out_idx = _column_indexes(payload, ean)
    rows = [r for r in payload.get("content", []) if r.get("date") == day]
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("order") or 0, r.get("start") or ""))

    times: list[str] = []
    produced: list[float] = []
    shared: list[float] = []
    for row in rows:
        exported, was_shared = _split(row.get("values", []), in_idx, out_idx)
        times.append(row.get("start"))
        produced.append(round(exported, 3))
        shared.append(round(was_shared, 3))

    return {"datum": day, "casy": times, "vyroba": produced, "sdileno": shared}


def parse_days(payload: dict, ean: str) -> dict[str, dict[str, float]]:
    """Sum 15-min interval values into per-day totals.

    The shared volume is derived per interval and only then summed. Doing it
    the other way round (sum both columns, subtract at the end) would give the
    same number here, but per-interval keeps the daily totals consistent with
    `parse_intervals` even when a single interval needs clamping.

    Only days that are ACTUALLY present in the response are returned - if the
    portal hasn't processed/settled a day yet it simply won't appear in
    `content`, and we must not invent a zero entry for it (that would
    permanently hide the gap instead of retrying later).
    """
    in_idx, out_idx = _column_indexes(payload, ean)

    days: dict[str, dict[str, float]] = {}
    for row in payload.get("content", []):
        d = row.get("date")
        if not d:
            continue
        bucket = days.setdefault(d, {"measured": 0.0, "shared": 0.0})
        exported, was_shared = _split(row.get("values", []), in_idx, out_idx)
        bucket["measured"] += exported
        bucket["shared"] += was_shared

    for d in days:
        days[d]["measured"] = round(days[d]["measured"], 3)
        days[d]["shared"] = round(days[d]["shared"], 3)
    return days


def ean_is_missing(payload: dict, ean: str) -> bool:
    """True if the portal explicitly reports this EAN as unknown."""
    return ean in (payload.get("missingEans") or [])
