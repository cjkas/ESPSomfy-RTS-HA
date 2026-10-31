"""Config flow for ESPSomfy RTS."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import zeroconf
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PIN,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import aiohttp_client
from homeassistant.util.network import is_host_valid

from . import proto
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# What each security mode asks for. /loginContext and /login are on port 80
# in every firmware, before protocol 2 too.
CREDENTIALS = {
    proto.SECURITY_PIN: (CONF_PIN,),
    proto.SECURITY_PASSWORD: (CONF_USERNAME, CONF_PASSWORD),
}
NO_CREDENTIALS = {CONF_USERNAME: "", CONF_PASSWORD: "", CONF_PIN: ""}


class CannotConnect(Exception):
    """The device didn't answer, or not as ESPSomfy RTS does."""


async def _login_context(hass: HomeAssistant, host: str) -> dict[str, Any]:
    """The device's security mode and identity."""
    session = aiohttp_client.async_get_clientsession(hass)
    try:
        info = await proto.login_context(session, host)
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise CannotConnect from err
    if not isinstance(info, dict) or not info.get("serverId"):
        raise CannotConnect
    return info


async def _check_credentials(
    hass: HomeAssistant, host: str, mode: int, user_input: dict[str, Any]
) -> dict[str, str]:
    """The credentials for mode, once the device has taken them.

    Raises LoginRefused, LoginThrottled or CannotConnect.
    """
    creds = dict(NO_CREDENTIALS)
    for key in CREDENTIALS.get(mode, ()):
        creds[key] = user_input.get(key, "")
    session = aiohttp_client.async_get_clientsession(hass)
    try:
        await proto.login(session, host, **creds)
    except proto.LoginThrottled:
        raise
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise CannotConnect from err
    return creds


def _host_valid(host: str) -> bool:
    """A hostname or IP address, with a port when it isn't on 80."""
    if is_host_valid(host):
        return True
    name, _, port = host.rpartition(":")
    return port.isdigit() and int(port) < 65536 and is_host_valid(name)


def _title(device: dict[str, Any], host: str) -> str:
    """The device's hostname, which names its entry."""
    return device.get("hostname") or host


def _login_error(mode: int) -> str:
    return "invalid_pin" if mode == proto.SECURITY_PIN else "invalid_password"


def _credentials_schema(mode: int, data: Mapping[str, Any] | None = None) -> vol.Schema:
    data = data or {}
    return vol.Schema(
        {
            vol.Required(key, description={"suggested_value": data.get(key)}): str
            for key in CREDENTIALS[mode]
        }
    )


# The ConfigFlow is only accessed when first setting up the integration.  This simply
# collects the initial data from the user to determine whether the integration can
# be installed.
class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configuration flow for ESPSomfyController."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self.zero_conf: zeroconf.ZeroconfServiceInfo
        self.server_id = None
        self.host: str | None = None
        # /loginContext's answer.
        self.device: dict[str, Any] = {}
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the device's address."""
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if not _host_valid(host):
                errors[CONF_HOST] = "invalid_host"
            else:
                try:
                    await self._read_device(host)
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(
                        f"espsomfy_{self.device['serverId']}"
                    )
                    self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                    return await self.async_step_credentials()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        default=(user_input or {}).get(CONF_HOST, ""),
                    ): str
                }
            ),
            errors=errors,
        )

    async def async_step_zeroconf(
        self, discovery_info: zeroconf.ZeroconfServiceInfo
    ) -> FlowResult:
        """Handle zeroconf discovery."""
        self.zero_conf = discovery_info

        # Check if already configured
        self.server_id = discovery_info.properties.get("serverId", "")
        # This was part of PR#54 and was incorrect the server id is simply the chip id of the ESP32 but could
        # be replicated by other items.  If this server id is already configured then we do not want
        # to add the device again.  Since the unique id contains the server id this will always be the same
        # as identified by the chip id on the ESP32.
        # await self.async_set_unique_id(f"{self.server_id}")
        self.host = discovery_info.host
        await self.async_set_unique_id(f"espsomfy_{self.server_id}")

        self._abort_if_unique_id_configured(
            updates={CONF_HOST: discovery_info.host}, reload_on_update=True
        )
        self.context.update(
            {
                "title_placeholders": {
                    CONF_NAME: discovery_info.hostname,
                    CONF_HOST: discovery_info.host,
                    "model": discovery_info.properties.get("model", ""),
                    "configuration_url": f"http://{discovery_info.host}",
                },
            }
        )
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a flow initiated by zeroconf."""
        if user_input is not None:
            try:
                await self._read_device(self.zero_conf.host)
            except CannotConnect:
                return self.async_abort(reason="cannot_connect")
            return await self.async_step_credentials()

        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                CONF_NAME: self.zero_conf.hostname,
                "model": self.zero_conf.properties.get("model", ""),
                "server_id": self.server_id,
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        """The device refused the stored credentials."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        try:
            await self._read_device(entry_data[CONF_HOST])
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        return await self.async_step_credentials()

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for the PIN, or the username and password, when Secured."""
        mode = int(self.device.get("type", proto.SECURITY_NONE))
        if mode not in CREDENTIALS:
            return self._finish(dict(NO_CREDENTIALS))
        errors = {}
        placeholders = {CONF_NAME: _title(self.device, self.host)}
        if user_input is not None:
            try:
                creds = await _check_credentials(self.hass, self.host, mode, user_input)
            except proto.LoginRefused:
                errors["base"] = _login_error(mode)
            except proto.LoginThrottled as err:
                errors["base"] = "too_many_attempts"
                placeholders["retry_after"] = str(err.retry_after)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                return self._finish(creds)
        return self.async_show_form(
            step_id="credentials",
            data_schema=_credentials_schema(mode, user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _read_device(self, host: str) -> None:
        self.device = await _login_context(self.hass, host)
        self.host = host

    @callback
    def _finish(self, creds: dict[str, str]) -> FlowResult:
        if self._reauth_entry is not None:
            entry = self._reauth_entry
            return self.async_update_reload_and_abort(
                entry, data={**entry.data, **creds}
            )
        return self.async_create_entry(
            title=_title(self.device, self.host),
            data={CONF_HOST: self.host, **creds},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for espsomfy_rts."""
        return ESPSomfyOptionsFlowHandler(config_entry)


class ESPSomfyOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow for espsomfy_rts component."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the OptionsFlow."""
        self._config_entry = config_entry
        self._errors: dict[str, Any] = {}
        self._host = (
            config_entry.data.get(CONF_HOST, "")
            if config_entry and config_entry.data
            else ""
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure options for espsomfy_rts."""
        errors = {}
        placeholders = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if not _host_valid(host):
                errors[CONF_HOST] = "invalid_host"
            else:
                try:
                    device = await _login_context(self.hass, host)
                    mode = int(device.get("type", proto.SECURITY_NONE))
                    creds = await _check_credentials(self.hass, host, mode, user_input)
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except proto.LoginRefused:
                    errors["base"] = _login_error(mode)
                except proto.LoginThrottled as err:
                    errors["base"] = "too_many_attempts"
                    placeholders["retry_after"] = str(err.retry_after)
                else:
                    data = {CONF_HOST: host, **creds}
                    title = _title(device, host)
                    changed = data != dict(self._config_entry.data)
                    # Update config entry with data from user input
                    self.hass.config_entries.async_update_entry(
                        self._config_entry, title=title, data=data
                    )
                    # The socket is still on the old address and credentials.
                    # Here, not in an update listener: setup updates the title.
                    if changed:
                        self.hass.config_entries.async_schedule_reload(
                            self._config_entry.entry_id
                        )
                    return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="init",
            data_schema=_get_data_schema(user_input or self._config_entry.data),
            errors=errors,
            description_placeholders=placeholders,
        )


def _get_data_schema(data: Mapping[str, Any]) -> vol.Schema:
    """The host and every credential, with the values given before."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=data.get(CONF_HOST, "")): str,
            vol.Optional(
                CONF_USERNAME,
                description={"suggested_value": data.get(CONF_USERNAME)},
            ): str,
            vol.Optional(
                CONF_PASSWORD,
                description={"suggested_value": data.get(CONF_PASSWORD)},
            ): str,
            vol.Optional(
                CONF_PIN,
                description={"suggested_value": data.get(CONF_PIN)},
            ): str,
        }
    )
