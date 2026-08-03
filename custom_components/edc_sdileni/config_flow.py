"""Config flow for EDC sdílení elektřiny.

Lets the user enter (and later edit) username/password and add/remove EANs
through the Home Assistant UI, with validation against the portal so typos
in the EAN or wrong credentials are caught immediately with a clear error
instead of silently failing later.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EdcAuthError, async_fetch_overview, async_get_access_token, ean_is_missing
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_EAN,
    CONF_EANS,
    CONF_HISTORY_START,
    CONF_UPDATE_HOUR,
    CONF_UPDATE_MINUTE,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_UPDATE_HOUR,
    DEFAULT_UPDATE_MINUTE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def _validate_login(hass: HomeAssistant, username: str, password: str) -> str | None:
    """Return None on success, else an error code for the form."""
    session = async_get_clientsession(hass)
    try:
        await async_get_access_token(session, username, password)
    except EdcAuthError as err:
        _LOGGER.error("EDC sdílení: přihlášení odmítnuto při konfiguraci: %s", err)
        return "invalid_auth"
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("EDC sdílení: portál nedostupný při konfiguraci: %s", err)
        return "cannot_connect"
    return None


async def _validate_ean(hass: HomeAssistant, username: str, password: str, ean: str) -> str | None:
    """Return None if the EAN is known to the portal, else an error code."""
    session = async_get_clientsession(hass)
    try:
        token = await async_get_access_token(session, username, password)
        today = date.today()
        payload = await async_fetch_overview(
            session, token, ean, today - timedelta(days=7), today
        )
    except EdcAuthError as err:
        _LOGGER.error("EDC sdílení: přihlášení odmítnuto při ověřování EAN %s: %s", ean, err)
        return "invalid_auth"
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("EDC sdílení: nešlo ověřit EAN %s (%s)", ean, err)
        return "cannot_connect"

    if ean_is_missing(payload, ean):
        _LOGGER.error("EDC sdílení: EAN %s nebyl na portálu nalezen", ean)
        return "ean_not_found"
    return None


class EdcConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            ean = user_input[CONF_EAN].strip()

            if not username:
                errors[CONF_USERNAME] = "required"
            if not password:
                errors[CONF_PASSWORD] = "required"
            if not ean:
                errors[CONF_EAN] = "required"

            if not errors:
                error = await _validate_login(self.hass, username, password)
                if error:
                    errors["base"] = error
                else:
                    error = await _validate_ean(self.hass, username, password, ean)
                    if error:
                        errors[CONF_EAN] = error

            if not errors:
                await self.async_set_unique_id(f"{DOMAIN}_{username}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"EDC ({username})",
                    data={CONF_USERNAME: username, CONF_PASSWORD: password},
                    options={
                        CONF_EANS: [ean],
                        CONF_UPDATE_HOUR: DEFAULT_UPDATE_HOUR,
                        CONF_UPDATE_MINUTE: DEFAULT_UPDATE_MINUTE,
                        CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_EAN): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]
            error = await _validate_login(self.hass, username, password)
            if error:
                errors["base"] = error
            else:
                assert self._reauth_entry is not None
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        schema = vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str})
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "EdcOptionsFlow":
        return EdcOptionsFlow(config_entry)


class EdcOptionsFlow(config_entries.OptionsFlow):
    """Add/remove EANs and tweak schedule/backfill settings after setup."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_ean", "remove_ean", "settings"],
        )

    async def async_step_add_ean(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            ean = user_input[CONF_EAN].strip()
            username = self.config_entry.data[CONF_USERNAME]
            password = self.config_entry.data[CONF_PASSWORD]
            existing = list(self.config_entry.options.get(CONF_EANS, []))

            if not ean:
                errors[CONF_EAN] = "required"
            elif ean in existing:
                errors[CONF_EAN] = "ean_already_added"
            else:
                error = await _validate_ean(self.hass, username, password, ean)
                if error:
                    errors[CONF_EAN] = error

            if not errors:
                new_options = dict(self.config_entry.options)
                new_options[CONF_EANS] = existing + [ean]
                return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema({vol.Required(CONF_EAN): str})
        return self.async_show_form(step_id="add_ean", data_schema=schema, errors=errors)

    async def async_step_remove_ean(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        existing = list(self.config_entry.options.get(CONF_EANS, []))
        if not existing:
            return self.async_abort(reason="no_eans")

        if user_input is not None:
            to_remove = user_input[CONF_EAN]
            new_options = dict(self.config_entry.options)
            new_options[CONF_EANS] = [e for e in existing if e != to_remove]
            return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema({vol.Required(CONF_EAN): vol.In(existing)})
        return self.async_show_form(step_id="remove_ean", data_schema=schema)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        current = self.config_entry.options
        if user_input is not None:
            new_options = dict(current)
            new_options[CONF_UPDATE_HOUR] = user_input[CONF_UPDATE_HOUR]
            new_options[CONF_UPDATE_MINUTE] = user_input[CONF_UPDATE_MINUTE]
            new_options[CONF_BACKFILL_DAYS] = user_input[CONF_BACKFILL_DAYS]
            history_start = user_input.get(CONF_HISTORY_START)
            if history_start:
                try:
                    date.fromisoformat(history_start)
                except ValueError:
                    errors[CONF_HISTORY_START] = "invalid_date"
                else:
                    new_options[CONF_HISTORY_START] = history_start
            if not errors:
                return self.async_create_entry(title="", data=new_options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_HOUR, default=current.get(CONF_UPDATE_HOUR, DEFAULT_UPDATE_HOUR)
                ): int,
                vol.Required(
                    CONF_UPDATE_MINUTE,
                    default=current.get(CONF_UPDATE_MINUTE, DEFAULT_UPDATE_MINUTE),
                ): int,
                vol.Required(
                    CONF_BACKFILL_DAYS,
                    default=current.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS),
                ): int,
                vol.Optional(
                    CONF_HISTORY_START, default=current.get(CONF_HISTORY_START, "")
                ): str,
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema, errors=errors)
