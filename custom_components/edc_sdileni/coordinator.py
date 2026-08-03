"""Coordinator: persistent history store + daily fetch + backfill + retries."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import EdcAuthError, async_fetch_overview, ean_is_missing, parse_days
from .auth import EdcTokenManager
from .const import (
    CHUNK_DAYS,
    RETRY_FIRST_DELAY,
    RETRY_REPEAT_DELAY,
    STORAGE_KEY_PREFIX,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


def _daterange_chunks(date_from: date, date_to: date, chunk_days: int):
    cur = date_from
    while cur <= date_to:
        end = min(cur + timedelta(days=chunk_days - 1), date_to)
        yield cur, end
        cur = end + timedelta(days=1)


class EdcCoordinator(DataUpdateCoordinator):
    """Fetches EDC sharing data for ONE EAN on a schedule, with retries and backfill.

    History (per-day totals) is kept in an HA Store so it survives restarts.
    `self.data` always reflects the most recently *completed* day we have
    data for, plus the full known history for graphing.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        token_manager: EdcTokenManager,
        ean: str,
        history_start: date,
        on_ean_not_found: Callable[[str], Awaitable[None]] | None = None,
        on_auth_failure: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"EDC sdílení ({ean})", update_interval=None)
        # Shared with every other EAN under the same config entry: one login,
        # one access token, refreshed centrally.
        self._tokens = token_manager
        self._ean = ean
        self._history_start = history_start
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{ean}")
        self.history: dict[str, dict[str, float]] = {}
        self._retry_unsub = None
        self._consecutive_failures = 0
        self.ean_not_found = False
        self._on_ean_not_found = on_ean_not_found
        self._on_auth_failure = on_auth_failure

    # ---------------------------------------------------------------- store
    async def async_load_history(self) -> None:
        stored = await self._store.async_load()
        self.history = stored.get("days", {}) if stored else {}
        _LOGGER.debug(
            "EDC sdílení (%s): načteno %d dní z lokálního úložiště", self._ean, len(self.history)
        )

    async def _async_save_history(self) -> None:
        await self._store.async_save({"days": self.history})

    # ------------------------------------------------------------- fetching
    async def _async_fetch_range(self, date_from: date, date_to: date) -> dict[str, dict]:
        """Fetch+parse one date range, chunked, merging into self.history.

        Stops immediately (without raising) if the portal reports our EAN as
        unknown - that's a permanent condition, not something retries fix.
        """
        if self.ean_not_found:
            return {}

        session = async_get_clientsession(self.hass)
        token = await self._tokens.async_get_access_token()

        new_days: dict[str, dict] = {}
        for chunk_from, chunk_to in _daterange_chunks(date_from, date_to, CHUNK_DAYS):
            try:
                payload = await async_fetch_overview(
                    session, token, self._ean, chunk_from, chunk_to
                )
            except EdcAuthError:
                # The API refused our token mid-run. That usually means it just
                # expired (long backfill) or was revoked server-side, not that
                # the password is wrong - so throw the token away and retry the
                # same chunk once with a brand new one.
                _LOGGER.debug(
                    "EDC sdílení (%s): API odmítlo token, obnovuji a zkouším znovu", self._ean
                )
                await self._tokens.async_invalidate()
                token = await self._tokens.async_get_access_token()
                payload = await async_fetch_overview(
                    session, token, self._ean, chunk_from, chunk_to
                )

            if ean_is_missing(payload, self._ean):
                _LOGGER.error(
                    "EDC sdílení (%s): portál tento EAN nezná, dál ho nebudu zkoušet stahovat",
                    self._ean,
                )
                self.ean_not_found = True
                if self._on_ean_not_found:
                    await self._on_ean_not_found(self._ean)
                break

            days = parse_days(payload, self._ean)
            if days:
                self.history.update(days)
                new_days.update(days)
                await self._async_save_history()
        return new_days

    async def _async_update_data(self):
        """Regular daily poll: just look at the last few days (cheap)."""
        if self.ean_not_found:
            return self._build_result()
        today = date.today()
        date_from = today - timedelta(days=3)
        await self._async_fetch_range(date_from, today)
        return self._build_result()

    def _build_result(self) -> dict:
        today = date.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        if yesterday in self.history:
            latest_date = yesterday
        else:
            known = [d for d in self.history if d < today.isoformat()]
            latest_date = max(known) if known else None
        latest = self.history.get(latest_date, {"measured": 0.0, "shared": 0.0})
        return {
            "days": self.history,
            "latest_date": latest_date,
            "latest": latest,
            "ean_not_found": self.ean_not_found,
        }

    # -------------------------------------------------------------- backfill
    async def async_backfill_if_needed(self) -> None:
        """On startup: if the current month has gaps, fetch everything missing
        since history_start (bounded, configurable) through yesterday.
        """
        if self.ean_not_found:
            return

        today = date.today()
        yesterday = today - timedelta(days=1)
        month_start = today.replace(day=1)
        if yesterday < month_start:
            # Today is the 1st of the month -> nothing from "this month" has
            # fully elapsed yet. Check the previous month instead so we don't
            # spuriously skip the gap-check right after a month boundary.
            month_start = (month_start - timedelta(days=1)).replace(day=1)

        month_days = {
            (month_start + timedelta(days=i)).isoformat()
            for i in range((yesterday - month_start).days + 1)
        }

        missing_this_month = month_days - self.history.keys()
        if not missing_this_month:
            _LOGGER.debug(
                "EDC sdílení (%s): aktuální měsíc je kompletní, backfill přeskočen", self._ean
            )
            return

        full_range_days = {
            (self._history_start + timedelta(days=i)).isoformat()
            for i in range((yesterday - self._history_start).days + 1)
        }
        missing = sorted(full_range_days - self.history.keys())
        if not missing:
            return

        _LOGGER.info(
            "EDC sdílení (%s): chybí %d dní od %s do %s, doplňuji z portálu",
            self._ean,
            len(missing),
            missing[0],
            missing[-1],
        )
        await self._async_fetch_range(
            datetime.fromisoformat(missing[0]).date(),
            datetime.fromisoformat(missing[-1]).date(),
        )

    # ---------------------------------------------------------------- retry
    async def async_run_with_retry(self) -> None:
        """Run a refresh; on transient failure, schedule 5min-then-hourly
        retries. On auth failure, stop retrying (a stale password won't fix
        itself) and notify the integration so it can start a reauth flow. On
        "EAN not found", there's nothing to retry either.
        """
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None

        if self.ean_not_found:
            return

        await self.async_refresh()

        if self.last_update_success:
            if self._consecutive_failures:
                _LOGGER.info("EDC sdílení (%s): spojení obnoveno", self._ean)
            self._consecutive_failures = 0
            return

        if isinstance(self.last_exception, EdcAuthError):
            _LOGGER.error(
                "EDC sdílení (%s): neplatné přihlašovací údaje, přihlašování nebudu dál "
                "opakovat dokud je neopravíš (%s)",
                self._ean,
                self.last_exception,
            )
            if self._on_auth_failure:
                await self._on_auth_failure()
            return

        self._consecutive_failures += 1
        delay = RETRY_FIRST_DELAY if self._consecutive_failures == 1 else RETRY_REPEAT_DELAY
        _LOGGER.warning(
            "EDC sdílení (%s): aktualizace selhala (%s). Další pokus za %d s.",
            self._ean,
            self.last_exception,
            delay,
        )

        async def _retry(_now):
            await self.async_run_with_retry()

        self._retry_unsub = async_call_later(self.hass, delay, _retry)

    def cancel_retry(self) -> None:
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None
