"""EDC sdílení elektřiny — config-entry based integration.

One config entry = one set of EDC credentials. Each EAN configured under
that entry gets its own EdcCoordinator and its own Home Assistant device
(with 3 sensors: výroba, sdíleno, podíl).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change

from .api import EdcAuthError, async_fetch_overview, ean_has_no_data
from .auth import EdcTokenManager
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_EANS,
    CONF_HISTORY_START,
    CONF_UPDATE_HOUR,
    CONF_UPDATE_MINUTE,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_UPDATE_HOUR,
    DEFAULT_UPDATE_MINUTE,
    DOMAIN,
)
from .coordinator import EdcCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    eans = list(entry.options.get(CONF_EANS, []))

    if not username or not password:
        _LOGGER.error("EDC sdílení: chybí přihlašovací jméno nebo heslo v konfiguraci")
        raise ConfigEntryAuthFailed("Chybí přihlašovací jméno nebo heslo")

    if not eans:
        _LOGGER.error("EDC sdílení: není nastaven žádný EAN")
        raise ConfigEntryError(
            "Není nastaven žádný EAN — přidej ho v Nastavení -> Zařízení a služby -> "
            "EDC sdílení elektřiny -> Možnosti."
        )

    session = async_get_clientsession(hass)

    # 1) One token manager per config entry, shared by all EANs. It reuses a
    #    refresh token cached from a previous run when there is one, so a
    #    restart normally doesn't replay the password at all.
    token_manager = EdcTokenManager(hass, username, password, entry.entry_id)
    await token_manager.async_load()

    # Get a token up front so bad credentials fail fast with a clear reauth
    # prompt instead of silently retrying forever.
    try:
        token = await token_manager.async_get_access_token()
    except EdcAuthError as err:
        _LOGGER.error("EDC sdílení: přihlášení odmítnuto (%s)", err)
        raise ConfigEntryAuthFailed(str(err)) from err
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("EDC sdílení: portál nedostupný při startu (%s)", err)
        raise ConfigEntryNotReady(f"EDC portál nedostupný: {err}") from err

    # 2) Validate each configured EAN. EANs the portal doesn't recognise are
    #    flagged with a Repair issue - but still kept and retried on schedule.
    #    The portal reports "no data" for a valid EAN whenever the period has
    #    nothing settled yet, so this signal is a hint, never a verdict.
    today = date.today()
    probe_from = today - timedelta(days=7)
    valid_eans: list[str] = []

    for ean in eans:
        issue_id = f"ean_no_data_{entry.entry_id}_{ean}"
        # Clear the issue id used up to 1.2.3, otherwise a stale "EAN not found"
        # warning from the reload-loop version would stay in Repairs forever.
        ir.async_delete_issue(hass, DOMAIN, f"ean_not_found_{entry.entry_id}_{ean}")
        try:
            payload = await async_fetch_overview(session, token, ean, probe_from, today)
        except EdcAuthError:
            raise  # credentials died mid-loop; let the outer handling above apply
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "EDC sdílení (%s): nešlo ověřit EAN při startu (%s), zkusím to znovu později",
                ean, err,
            )
            valid_eans.append(ean)  # transient - not a hard "not found"
            continue

        # The EAN is kept either way. A wrong EAN and a valid one whose data
        # simply isn't published yet look identical here, and refusing to set
        # up would break a legitimately fresh EAN. The Repair issue tells the
        # user; the schedule keeps trying and clears it once data appears.
        if ean_has_no_data(payload, ean):
            _LOGGER.warning(
                "EDC sdílení: portál nemá pro EAN %s žádná data za posledních 7 dní. "
                "Pokud jsi ho právě zaregistroval ke sdílení, je to v pořádku a data "
                "se objeví sama. Jinak si ho zkontroluj v Nastavení -> Zařízení "
                "a služby -> EDC sdílení elektřiny -> Možnosti.",
                ean,
            )
            ir.async_create_issue(
                hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="ean_no_data",
                translation_placeholders={"ean": ean},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        valid_eans.append(ean)

    hour = entry.options.get(CONF_UPDATE_HOUR, DEFAULT_UPDATE_HOUR)
    minute = entry.options.get(CONF_UPDATE_MINUTE, DEFAULT_UPDATE_MINUTE)
    backfill_days = entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
    history_start_str = entry.options.get(CONF_HISTORY_START)
    history_start = (
        date.fromisoformat(history_start_str)
        if history_start_str
        else today - timedelta(days=backfill_days)
    )

    async def _on_auth_failure() -> None:
        _LOGGER.error("EDC sdílení: spouštím reauth flow kvůli neplatným přihlašovacím údajům")
        entry.async_start_reauth(hass)

    coordinators: dict[str, EdcCoordinator] = {}
    unsub_timers = []

    for ean in valid_eans:
        coordinator = EdcCoordinator(
            hass,
            token_manager,
            ean,
            history_start,
            on_auth_failure=_on_auth_failure,
        )
        await coordinator.async_load_history()

        try:
            await coordinator.async_backfill_if_needed()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("EDC sdílení (%s): backfill při startu selhal: %s", ean, err)
        await coordinator.async_run_with_retry()

        def _make_callback(c: EdcCoordinator):
            async def _refresh(_now):
                await c.async_run_with_retry()

            return _refresh

        unsub = async_track_time_change(hass, _make_callback(coordinator), hour=hour, minute=minute, second=0)
        unsub_timers.append(unsub)
        coordinators[ean] = coordinator

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "token_manager": token_manager,
        "coordinators": coordinators,
        "unsub_timers": unsub_timers,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options changed (EAN added/removed, schedule changed) -> reload."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if data:
            for coordinator in data.get("coordinators", {}).values():
                coordinator.cancel_retry()
            for unsub in data.get("unsub_timers", []):
                unsub()
    return unloaded
