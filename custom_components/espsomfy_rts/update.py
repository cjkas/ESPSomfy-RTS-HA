"""Support for ESPSomfy RTS updates."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any, cast

import aiohttp

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, EVT_CONNECTED, EVT_FWSTATUS, EVT_UPDPROGRESS
from .controller import ESPSomfyController
from .entity import ESPSomfyEntity

_LOGGER = logging.getLogger(__name__)

# The firmware no longer looks for releases itself (its ADR 0007): Home
# Assistant asks GitHub, which has the memory and TLS for it.
LATEST_RELEASE_URL = "https://api.github.com/repos/cjkas/ESPSomfy-RTS/releases/latest"
RELEASE_CHECK_INTERVAL = timedelta(hours=6)


async def fetch_latest_release(session: aiohttp.ClientSession) -> dict[str, str] | None:
    """The latest release's version, page and notes; None when GitHub can't say.

    GitHub's latest release is never a draft or a pre-release.
    """
    try:
        async with asyncio.timeout(30):
            async with session.get(
                LATEST_RELEASE_URL,
                headers={"Accept": "application/vnd.github+json"},
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "GitHub answered %s for the latest release", resp.status
                    )
                    return None
                data = await resp.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        _LOGGER.debug("No latest release from GitHub: %r", err)
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("tag_name")
    if not isinstance(version, str) or not version:
        return None
    url = data.get("html_url")
    notes = data.get("body")
    return {
        "version": version,
        "url": url
        if isinstance(url, str) and url
        else f"https://github.com/cjkas/ESPSomfy-RTS/releases/tag/{version}",
        "notes": notes if isinstance(notes, str) else "",
    }


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ESPSomfy RTS update based on a config entry."""
    controller: ESPSomfyController = hass.data[DOMAIN][config_entry.entry_id]
    data = controller.api.get_config()
    if "serverId" in data:
        async_add_entities([ESPSomfyRTSUpdateEntity(controller)])


class ESPSomfyRTSUpdateEntity(ESPSomfyEntity, UpdateEntity):
    """Defines an ESPSomfy RTS update entity."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_supported_features = (
        UpdateEntityFeature.SPECIFIC_VERSION
        | UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
    )
    _attr_title = "ESPSomfy RTS Firmware"

    def __init__(self, controller: ESPSomfyController) -> None:
        """Initialize the update entity."""
        self._controller = controller
        self._available = True
        self._attr_name = "Firmware Update"
        self._attr_unique_id = f"update_{controller.unique_id}"
        self._update_status = 0
        self._fw_progress = 100
        self._app_progress = 100
        self._total_progress = 100
        # Protocol 2's latest release, from GitHub; None until GitHub says.
        self._release: dict[str, str] | None = None
        if controller.api.protocol == 2:
            # The firmware is updated from its own page (the firmware's
            # ADR 0007): this tells of a newer release, without installing it.
            self._attr_supported_features = UpdateEntityFeature.RELEASE_NOTES
        elif controller.check_for_update:
            self._attr_supported_features = (
                UpdateEntityFeature.INSTALL
                | UpdateEntityFeature.SPECIFIC_VERSION
                | UpdateEntityFeature.PROGRESS
                | UpdateEntityFeature.BACKUP
            )

        super().__init__(controller=controller, data=None)

    async def async_added_to_hass(self) -> None:
        """Look for the latest release now and every RELEASE_CHECK_INTERVAL."""
        await super().async_added_to_hass()
        if self._controller.api.protocol != 2:
            return
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_check_release, RELEASE_CHECK_INTERVAL
            )
        )
        # In the background: setup doesn't wait on GitHub, and unload cancels it.
        self.platform.config_entry.async_create_background_task(
            self.hass, self._async_check_release(), "espsomfy_rts latest release"
        )

    async def _async_check_release(self, _now: datetime | None = None) -> None:
        """Take the latest release from GitHub; keep the last one when it can't say."""
        release = await fetch_latest_release(async_get_clientsession(self.hass))
        if release is not None and release != self._release:
            self._release = release
            self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if (
            self._controller.data["event"] == EVT_CONNECTED
            and "connected" in self._controller.data
        ):
            self._available = bool(self._controller.data["connected"])
            self.async_write_ha_state()
        elif self._controller.data["event"] == EVT_FWSTATUS:
            if (
                self._controller.check_for_update
                and self._controller.internet_available
            ):
                self._attr_supported_features = (
                    UpdateEntityFeature.INSTALL
                    | UpdateEntityFeature.SPECIFIC_VERSION
                    | UpdateEntityFeature.PROGRESS
                    | UpdateEntityFeature.BACKUP
                )
            else:
                self._attr_supported_features = (
                    UpdateEntityFeature.SPECIFIC_VERSION | UpdateEntityFeature.PROGRESS
                )
            self.async_write_ha_state()
        elif self.controller.data["event"] == EVT_UPDPROGRESS:
            d = self.controller.data
            if "part" in d:
                if int(d["part"]) == 0:
                    self._app_progress = 0
                    self._fw_progress = (int(d["loaded"]) / int(d["total"])) * 100
                elif int(d["part"]) == 100:
                    self._fw_progress = 100
                    self._app_progress = (int(d["loaded"]) / int(d["total"])) * 100
                self._total_progress = int((self._fw_progress + self._app_progress) / 2)
                self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Indicates whether the shade is available."""
        return self._available

    @property
    def can_install(self) -> bool:
        """Indicates whether the current version supports firmware installation."""

    @property
    def installed_version(self) -> str | None:
        """Version currently installed and in use."""
        if (version := self.coordinator.version) is None:
            return None
        return str(version)

    @property
    def latest_version(self) -> str | None:
        """Latest version available for install."""
        if self._controller.api.protocol == 2:
            return self._release["version"] if self._release else None
        if self.coordinator.check_for_update is False:
            return None
        if (latest := self.coordinator.latest_version) is None:
            return None
        return str(latest)

    @property
    def in_progress(self) -> bool | int | None:
        """Update installation progress."""
        if self._total_progress < 100:
            return self._total_progress
        return False

    @property
    def release_url(self) -> str | None:
        """URL to the full release notes of the latest version available."""
        if self._controller.api.protocol == 2:
            return self._release["url"] if self._release else None
        if (version := self.latest_version) is None:
            return None
        return f"https://github.com/cjkas/ESPSomfy-RTS/releases/tag/{version}"

    async def async_release_notes(self) -> str | None:
        """The latest release's notes, from GitHub."""
        return self._release["notes"] if self._release else None

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Install an update."""
        if backup:
            # Raises when there is no backup, so nothing is installed without one.
            await self._controller.create_backup()
        # We cast here, we know that the latest_version is supposed to be a string.
        version = cast(str, self.latest_version)
        if version is not None:
            await self.controller.update_firmware(version)
