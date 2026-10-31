"""Controller for all the devices."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

import aiohttp
from packaging.version import parse as version_parse

from homeassistant.components import persistent_notification
from homeassistant.components.cover import CoverDeviceClass, CoverEntityFeature
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PIN,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import (
    aiohttp_client,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import compat, proto
from .const import (
    API_DISCOVERY,
    API_GROUPCOMMAND,
    API_GROUPS,
    API_REBOOT,
    API_SETPOSITIONS,
    API_SETSENSOR,
    API_SHADECOMMAND,
    API_SHADES,
    API_TILTCOMMAND,
    DOMAIN,
    EVT_CONNECTED,
    EVT_ETHERNET,
    EVT_FWSTATUS,
    EVT_GROUPSTATE,
    EVT_MEMSTATUS,
    EVT_SHADEADDED,
    EVT_SHADECOMMAND,
    EVT_SHADEREMOVED,
    EVT_SHADESTATE,
    EVT_UPDPROGRESS,
    EVT_WIFISTRENGTH,
    PLATFORMS,
)
from .proto import ProtoConnection

_LOGGER = logging.getLogger(__name__)

# The op that does what each HTTP route did, with the same fields.
OPS = {
    API_SHADECOMMAND: "shadeCommand",
    API_GROUPCOMMAND: "groupCommand",
    API_TILTCOMMAND: "tiltCommand",
    API_SETPOSITIONS: "setPositions",
    API_SETSENSOR: "setSensor",
    API_REBOOT: "reboot",
}
# /loginContext's permissions bit of "Secure Config Only": old firmware then
# takes commands without the token.
PERMISSION_CONFIG_ONLY = 0x01

# Bounds the TCP connect plus the HTTP upgrade so a stalled attempt is retried.
CONNECT_TIMEOUT = 15
# Ping interval; aiohttp drops the socket when the pong is half this late.
HEARTBEAT = 20
# The device emits wifiStrength/memStatus every few seconds, so a minute of
# silence means the link is dead even if TCP has not noticed yet.
RECEIVE_TIMEOUT = 60
MAX_BACKOFF = 30
# A connection that stayed up this long resets the backoff.
STABLE_AFTER = 60
# The device puts the backup together before it answers.
BACKUP_TIMEOUT = 30


def get_device(
    hass: HomeAssistant, unique_id: str, config_entry_id: str
) -> dr.DeviceEntry | None:
    """Get the controller device owned by the config entry."""
    registry = dr.async_get(hass)
    # async_get_device is deprecated and stops working in HA 2027.8; older HA
    # versions lack the per-config-entry lookup that replaces it.
    if hasattr(registry, "async_get_device_by_identifier"):
        return registry.async_get_device_by_identifier(
            (DOMAIN, unique_id), config_entry_id
        )
    return registry.async_get_device(identifiers={(DOMAIN, unique_id)})


def _write_backup(path: str, body: bytes) -> None:
    """The backup as the device sent it, in its own file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)


class _StopListening(Exception):
    """The listener gives up: connecting again can't help."""


class Listener:
    """Keeps a socket to the device open, connecting again when it closes."""

    def __init__(
        self, hass: HomeAssistant, url: str, onpacket, onopen, onclose
    ) -> None:
        """Initialize a new listener."""
        self.hass = hass
        self.url = url
        self.onpacket = onpacket
        self.onopen = onopen
        self.onclose = onclose
        # None until the first attempt, so a device that is down at startup
        # still reports disconnected.
        self.connected: bool | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._opened_at: float | None = None

    def start(self) -> None:
        """Start the connect/reconnect loop."""
        self._stopping = False
        self._task = self.hass.async_create_background_task(
            self._run(), name=f"espsomfy_rts socket {self.url}"
        )

    async def stop(self) -> None:
        """Stop the loop and close the socket."""
        self._stopping = True
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _serve(self) -> Any:
        """Connect, then read until the socket closes; returns why it did.

        Calls _opened() once the socket is open.
        """
        raise NotImplementedError

    def _opened(self) -> None:
        self._opened_at = time.monotonic()
        _LOGGER.info("Connected to %s", self.url)
        self._set_connected(True)

    async def _run(self) -> None:
        """Keep the socket connected until stopped."""
        backoff = 1
        while True:
            self._opened_at = None
            try:
                reason = await self._serve()
            except TimeoutError:
                reason = "timed out" if self._opened_at else "connect timed out"
            except (aiohttp.ClientError, OSError) as err:
                reason = err
            except _StopListening:
                self._set_connected(False)
                return
            except Exception as err:
                # A bug shouldn't end the loop: the entities would show the
                # device connected while nothing reads the socket.
                _LOGGER.exception("Unexpected error on %s", self.url)
                reason = err
            finally:
                if self._stopping:
                    self.connected = False

            opened_at = self._opened_at
            if opened_at is not None:
                uptime = time.monotonic() - opened_at
                if uptime > STABLE_AFTER:
                    backoff = 1
                _LOGGER.info(
                    "Disconnected from %s after %ds: %s, retrying in %ds",
                    self.url,
                    uptime,
                    reason,
                    backoff,
                )
            else:
                _LOGGER.debug(
                    "Connecting to %s failed: %s, retrying in %ds",
                    self.url,
                    reason,
                    backoff,
                )
            self._set_connected(False)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _set_connected(self, state: bool) -> None:
        """Report a change of connection state."""
        if self.connected == state:
            return
        self.connected = state
        if state:
            self.onopen()
        else:
            self.onclose()


class SocketListener(Listener):
    """The old socket on port 8080, of firmware before protocol 2."""

    def __init__(
        self, hass: HomeAssistant, url: str, onpacket, onopen, onclose
    ) -> None:
        """Initialize a new socket listener."""
        super().__init__(hass, url, onpacket, onopen, onclose)
        self.filter = None

    def set_filter(self, arr: Any) -> None:
        """Filter for the events."""
        self.filter = arr.copy()

    async def _serve(self) -> Any:
        session = async_get_clientsession(self.hass)
        async with asyncio.timeout(CONNECT_TIMEOUT):
            ws = await session.ws_connect(self.url, heartbeat=HEARTBEAT, autoping=True)
        async with ws:
            self._opened()
            while True:
                msg = await ws.receive(timeout=RECEIVE_TIMEOUT)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    return ws.exception() or "socket error"
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    return "closed by device"

    def _handle_message(self, message: str) -> None:
        """Process the incoming message."""
        try:
            if message.startswith("42["):
                ndx = message.find(",")
                event = message[3:ndx]
                if not self.filter or event in self.filter:
                    payload = message[ndx + 1 : -1]
                    data = json.loads(payload)
                    data["event"] = event
                    self.onpacket(data)
        except ValueError:
            _LOGGER.debug("Bad socket payload %r", message, exc_info=True)


class ProtoListener(Listener):
    """Protocol 2's socket at ws://host/ws.

    Each connection ends with the socket: the next is a new one, which logs in
    again when the device refuses the token. The events reach onpacket in the
    old socket's shape (compat.py).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: ESPSomfyAPI,
        conn: ProtoConnection | None,
        onpacket,
        onopen,
        onclose,
        onauthfailed,
    ) -> None:
        """Initialize, with the connection setup opened when there is one."""
        super().__init__(hass, f"ws://{api.get_host()}/ws", onpacket, onopen, onclose)
        self.api = api
        self.onauthfailed = onauthfailed
        self._conn = conn

    async def stop(self) -> None:
        """Stop the loop and close the socket, the one setup opened too."""
        await super().stop()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _serve(self) -> Any:
        conn, self._conn = self._conn, None
        try:
            if conn is None:
                conn = await self.api.open_connection()
        except (proto.AuthRequired, proto.LoginRefused):
            _LOGGER.warning("%s refused the login", self.api.get_host())
            self.onauthfailed()
            raise _StopListening from None
        except (proto.NoProtocol2, proto.VersionMismatch) as err:
            # The firmware changed to one that doesn't speak protocol 2:
            # setting up again picks the HTTP API, or reports the mismatch.
            _LOGGER.warning("%s: %s", self.api.get_host(), err)
            self.api.reload()
            raise _StopListening from None
        except (proto.ConnectionClosed, ValueError) as err:
            # ValueError: /login answered something that isn't JSON.
            return err
        model = conn.model

        def on_event(name: str, d: dict) -> None:
            try:
                data = compat.event(model, name, d)
                if data is not None:
                    self.onpacket(data)
            except Exception:
                # One event an entity can't take shouldn't drop the socket.
                _LOGGER.exception("Error handling %s from %s", name, self.url)

        remove = conn.add_listener(on_event)
        try:
            self.api.attach(conn)
            self._opened()
            # Events missed while the socket was down are in the sync.
            for data in compat.state_events(model):
                self.onpacket(data)
            await conn.run(RECEIVE_TIMEOUT)
        finally:
            remove()
            self.api.detach(conn)
            await conn.close()
        return "closed by device"


class ESPSomfyController(DataUpdateCoordinator):
    """Data coordinator/controller for receiving from ESPSomfy_RTS."""

    def __init__(self, config_entry_id, hass: HomeAssistant, api: ESPSomfyAPI) -> None:
        """Initialize data coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name=DOMAIN,
            # The setting below is only for polling.
            # update_interval=timedelta(seconds=5),
        )
        self.config_entry_id = config_entry_id
        self.api = api
        self.ws_listener = None

    @property
    def device_name(self) -> str:
        """Get the device name from the host."""
        return self.api.deviceName

    @property
    def server_id(self) -> str:
        """Get the server id from the api."""
        return self.api.server_id

    @property
    def unique_id(self) -> str:
        """Get a unique id for the controller."""
        return f"espsomfy_{self.server_id}"

    @property
    def model(self) -> str:
        """Get the model for the controller."""
        return self.api.model

    @property
    def version(self) -> str:
        """Get the current version for the controller."""
        return self.api.version

    @property
    def latest_version(self) -> str:
        """Get the latest version for the controller."""
        return self.api.latest_version

    @property
    def check_for_update(self) -> bool:
        """Indicate whether the firmware should check for updates."""
        return self.api.check_for_update

    @property
    def internet_available(self) -> bool:
        """Indicates whether the ESPSomfy RTS hardware has internet access."""
        return self.api.internet_available

    @property
    def can_update(self) -> bool:
        """Get a flag that indicates whether the firmware can be updated."""
        return self.api.can_update

    async def ws_close(self) -> None:
        """Close the tasks and sockets."""
        if self.ws_listener is not None:
            await self.ws_listener.stop()
            self.ws_listener = None

    async def ws_connect(self, conn: ProtoConnection | None = None):
        """Connect to WebSocket.

        conn is protocol 2's socket when setup has just opened it.
        """
        await self.ws_close()
        if self.api.protocol == 2:
            self.ws_listener = ProtoListener(
                self.hass,
                self.api,
                conn,
                self.ws_onpacket,
                self.ws_onopen,
                self.ws_onclose,
                self.ws_onauthfailed,
            )
            self.ws_listener.start()
            return
        self.ws_listener = SocketListener(
            self.hass,
            self.api.get_sock_url(),
            self.ws_onpacket,
            self.ws_onopen,
            self.ws_onclose,
        )
        self.ws_listener.set_filter(
            [
                EVT_CONNECTED,
                EVT_SHADEADDED,
                EVT_SHADEREMOVED,
                EVT_SHADESTATE,
                EVT_SHADECOMMAND,
                EVT_GROUPSTATE,
                EVT_FWSTATUS,
                EVT_UPDPROGRESS,
                EVT_WIFISTRENGTH,
                EVT_ETHERNET,
                EVT_MEMSTATUS,
            ]
        )
        self.ws_listener.start()

    async def create_backup(self) -> str:
        """Save the device's backup in HA's config directory; returns its path."""
        return await self.api.create_backup()

    async def update_firmware(self, version) -> bool:
        """Start the firmware update process."""
        return await self.api.update_firmware(version)

    async def set_host(self, host) -> None:
        """Set a host name and reloads the sockets if the host has changed."""
        if self.api.get_host() != host:
            # Tear down the socket
            self.api.set_host(host)
            await self.ws_connect()

    def ensure_group_configured(self, data):
        """Ensure the group exists on Home Assistant."""
        uuid = f"{self.unique_id}_group{data['groupId']}"
        device = get_device(self.hass, self.unique_id, self.config_entry_id)
        entities = er.async_get(self.hass)
        for entity in er.async_entries_for_config_entry(entities, self.config_entry_id):
            if entity.unique_id == uuid:
                return
        dev_features = (
            CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
        )

        dev_class = CoverDeviceClass.SHADE
        # Reload all the shades
        # self.api.load_shades()
        # I have no idea whether this reloads the devices or not.
        entities.async_get_or_create(
            domain=DOMAIN,
            platform=Platform.COVER,
            original_device_class=dev_class,
            unique_id=uuid,
            device_id=device.id,
            original_name=data["name"],
            suggested_object_id=f"{str(data['name']).lower().replace(' ', '_')}",
            supported_features=dev_features,
        )

    def ensure_shade_configured(self, data):
        """Ensure the shade exists on Home Assistant."""
        uuid = f"{self.unique_id}_{data['shadeId']}"

        device = get_device(self.hass, self.unique_id, self.config_entry_id)

        entities = er.async_get(self.hass)

        for entity in er.async_entries_for_config_entry(entities, self.config_entry_id):
            if entity.unique_id == uuid:
                return
        dev_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.STOP
            | CoverEntityFeature.SET_POSITION
        )

        dev_class = CoverDeviceClass.SHADE
        if "shadeType" in data:
            match int(data["shadeType"]):
                case 1:
                    dev_class = CoverDeviceClass.BLIND
                    if "tiltType" in data:
                        match int(data["tiltType"]):
                            case 1 | 2:
                                dev_features |= (
                                    CoverEntityFeature.OPEN_TILT
                                    | CoverEntityFeature.CLOSE_TILT
                                    | CoverEntityFeature.SET_TILT_POSITION
                                )
                    elif "hasTilt" in data and data["hasTilt"] is True:
                        dev_features |= (
                            CoverEntityFeature.OPEN_TILT
                            | CoverEntityFeature.CLOSE_TILT
                            | CoverEntityFeature.SET_TILT_POSITION
                        )
                case 2 | 7 | 8:
                    dev_class = CoverDeviceClass.CURTAIN
                case 3:
                    dev_class = CoverDeviceClass.AWNING
                case _:
                    dev_class = CoverDeviceClass.SHADE

        # Reload all the shades
        # self.api.load_shades()
        # I have no idea whether this reloads the devices or not.
        entities.async_get_or_create(
            domain=DOMAIN,
            platform=Platform.COVER,
            original_device_class=dev_class,
            unique_id=uuid,
            device_id=device.id,
            original_name=data["name"],
            suggested_object_id=f"{str(data['name']).lower().replace(' ', '_')}",
            supported_features=dev_features,
        )

    def ws_onpacket(self, data):
        """Packet from the websocket."""
        # Below doesn't work.  Near as I can tell there is no
        # real way of adding an entity on the fly.  All this
        # does is add an entity that is not really attached.
        # if data["event"] == EVT_SHADEADDED:
        #    self.ensure_shade_configured(data)

        # Catch the fwStatus messages before they go anywhere
        # this will allow us to simply update the latest firmware
        if "event" in data and data["event"] == EVT_FWSTATUS:
            self.api.set_firmware(data)

        self.async_set_updated_data(data=data)

    def ws_onopen(self):
        """Websocket is opened."""
        _LOGGER.debug("ESPSomfy RTS Socket was opened")
        if self.api.is_configured:
            _LOGGER.debug("ESPSomfy RTS Already Configured")
            data = {"event": EVT_CONNECTED, "connected": True}
            self.async_set_updated_data(data=data)
        else:
            _LOGGER.debug("ESPSomfy RTS configuring entities")
            loop = asyncio.get_event_loop()
            coro = loop.create_task(self.api.get_initial())

            def handle_connected(_coro):
                data = {"event": EVT_CONNECTED, "connected": True}
                self.async_set_updated_data(data=data)

            coro.add_done_callback(handle_connected)

    def ws_onclose(self):
        """Socket closed."""
        data = {"event": EVT_CONNECTED, "connected": False}
        self.async_set_updated_data(data=data)

    def ws_onauthfailed(self):
        """The device refused the stored credentials: ask for new ones."""
        entry = self.hass.config_entries.async_get_entry(self.config_entry_id)
        if entry is not None:
            entry.async_start_reauth(self.hass)


class ESPSomfyAPI:
    """API for sending data to ESPSomfy RTS hardware device."""

    def __init__(self, hass: HomeAssistant, config_entry_id, data) -> None:
        """Initialize the API."""
        self.hass = hass
        self.data = data
        self.set_host(data[CONF_HOST])
        self._config: Any = {}
        self._session = async_get_clientsession(self.hass, verify_ssl=False)
        self._authType = 0
        self._needsKey = False
        self._headers = {"apikey": ""}
        self._canLogin = False
        self._deviceName = data[CONF_HOST]
        self._can_update = False
        self._config_entry_id = config_entry_id
        self._configured = False
        # 2 once the device answered on protocol 2's socket; 1 is the HTTP
        # API and the old socket of firmware before it.
        self.protocol = 1
        self._token: str | None = None
        # Kept across connections, so the device needn't send it each time.
        self._schema: proto.Schema | None = None
        self._conn: ProtoConnection | None = None

    @property
    def shades(self) -> Any:
        """Return the state shades."""
        if "shades" in self._config:
            return self._config["shades"]
        return []

    @property
    def groups(self) -> Any:
        """Return the state groups."""
        if "groups" in self._config:
            return self._config["groups"]
        return []

    @property
    def server_id(self) -> str | None:
        """Getter for the server id."""
        if "serverId" in self._config:
            return self._config["serverId"]

    @property
    def version(self) -> str:
        """Getter for the api version."""
        if "version" in self._config:
            return self._config["version"]
        if "fwVersion" in self._config:
            return self._config["fwVersion"]
        return "0.0.0"

    @property
    def latest_version(self) -> str | None:
        """Getter for the latest version."""
        if "latest" in self._config:
            if self._config["latest"] == "":
                return None
            return self._config["latest"]
        return None

    @property
    def model(self) -> str:
        """Getter for the model number."""
        return self._config["model"]

    @property
    def apiKey(self) -> str:
        """Getter for the api key."""
        return self._config["apiKey"]

    @property
    def deviceName(self) -> str:
        """Getter for the device name."""
        return self._deviceName

    @property
    def can_update(self) -> bool:
        """Getter for whether the firmware is updatable."""
        return self._can_update

    @property
    def backup_dir(self) -> str:
        """Gets the backup directory for the device."""
        return self.hass.config.path(f"ESPSomfyRTS_{self.server_id}")

    @property
    def check_for_update(self) -> bool:
        """Check for the current update."""
        if "checkForUpdate" in self._config:
            return self._config["checkForUpdate"]
        return self._can_update

    @property
    def internet_available(self) -> bool:
        """Check to see if the ESPSomfy RTS hardware has internet."""
        if "inetAvailable" in self._config:
            return self._config["inetAvailable"]
        return self._can_update

    @property
    def is_configured(self) -> bool:
        """Indicates whether the integration has been configured."""
        return self._configured

    def set_host(self, host) -> None:
        """Set the host for the integration."""
        self._host = host
        # The old socket and HTTP API have their own ports; a port given with
        # the address is port 80's.
        name, sep, port = host.rpartition(":")
        if not (sep and port.isdigit() and ":" not in name):
            name = host
        self._sock_url = f"ws://{name}:8080"
        self._api_url = f"http://{name}:8081"
        self._config_url = f"http://{self._host}"

    def get_host(self):
        """Get the current host."""
        return self._host

    def get_sock_url(self):
        """Get the socket interface url."""
        return self._sock_url

    def get_api_url(self):
        """Get that url used for api reference."""
        return self._api_url

    def get_config_url(self) -> str:
        """Get the configuration url."""
        return self._config_url

    def get_config(self):
        """Return the initial config."""
        return self._config

    def get_data(self):
        """Return the internal data."""
        return self.data

    def set_firmware(self, data) -> None:
        """Set the firmware data from the socket."""
        cver = "0.0.0"
        if "version" in self._config:
            cver = self._config["version"]
        new_ver = cver
        if "fwVersion" in data:
            new_ver = data["fwVersion"]
            if "name" in new_ver:
                new_ver = new_ver["name"]
        elif "version" in data:
            new_ver = data["version"]
        if "latest" in data:
            latest_ver = data["latest"]
            if "name" in latest_ver:
                latest_ver = latest_ver["name"]
            self._config["latest"] = latest_ver
        if "checkForUpdate" in data:
            self._config["checkForUpdate"] = data["checkForUpdate"]
        if "inetAvailable" in data:
            self._config["inetAvailable"] = data["inetAvailable"]
        if cver != new_ver:
            # print(f"Version: {cver} to {new_ver}")
            if dev := get_device(
                self.hass, f"espsomfy_{self.server_id}", self._config_entry_id
            ):
                dr.async_get(self.hass).async_update_device(
                    dev.id, sw_version=new_ver
                )
        self._config["version"] = new_ver
        v = version_parse(new_ver)
        if (
            (v.major > 2)
            or (v.major == 2 and v.minor > 2)
            or (v.major == 2 and v.minor == 2 and v.micro > 0)
        ):
            self._can_update = True
        else:
            self._can_update = False

    async def check_address(self, url) -> bool:
        """Send a head to a url to check if it exists."""
        try:
            async with self._session.get(url) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001, S110
            # We don't care to do anything other than catch expections here.  If anything goes wrong there is an issue
            # with the address and we should not use it.
            pass
        return False

    async def setup(self) -> ProtoConnection | None:
        """Find which protocol the device speaks, take its model and set up.

        Returns protocol 2's open socket, or None for firmware before it.
        Raises ConfigEntryAuthFailed when it refuses the stored credentials,
        ConfigEntryNotReady when it can't be reached.
        """
        try:
            conn = await self.open_connection()
        except proto.NoProtocol2:
            conn = None
        except (proto.AuthRequired, proto.LoginRefused) as err:
            raise ConfigEntryAuthFailed(
                f"{self._host} refused the login: {err}"
            ) from err
        except proto.VersionMismatch as err:
            raise ConfigEntryError(f"{self._host}: {err}") from err
        except (
            TimeoutError,
            aiohttp.ClientError,
            OSError,
            ValueError,
            proto.ConnectionClosed,
        ) as err:
            raise ConfigEntryNotReady(
                f"Could not connect to ESPSomfy RTS at {self._host}: {err}"
            ) from err
        if conn is None:
            await self._setup_legacy()
            return None
        self.protocol = 2
        try:
            self.attach(conn)
            await self._forward_setups()
        except BaseException:
            await conn.close()
            raise
        return conn

    async def _setup_legacy(self) -> None:
        """Firmware before protocol 2: the HTTP API on 8081 and the old socket.

        Its token goes in the apikey header. A device secured only for its
        configuration needs none for commands, so a refused login doesn't
        stop it.
        """
        try:
            await self._log_in()
        except proto.LoginRefused as err:
            if await self._token_needed():
                raise ConfigEntryAuthFailed(
                    f"{self._host} refused the login: {err}"
                ) from err
            _LOGGER.warning("%s: %s", self._host, err)
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            _LOGGER.debug("%s: no login: %s", self._host, err)
        await self.get_initial()
        if not self._configured:
            raise ConfigEntryNotReady(
                f"Could not find ESPSomfy RTS device with address {self._api_url}"
            )

    async def _token_needed(self) -> bool:
        """Whether old firmware needs the token for commands, not only config."""
        try:
            context = await proto.login_context(self._session, self._host)
        except (TimeoutError, aiohttp.ClientError, ValueError):
            return False
        return not int(context.get("permissions", 0)) & PERMISSION_CONFIG_ONLY

    async def _log_in(self) -> None:
        """A token from /login with the stored credentials."""
        self._token = await proto.login(
            self._session,
            self._host,
            username=self.data.get(CONF_USERNAME) or "",
            password=self.data.get(CONF_PASSWORD) or "",
            pin=self.data.get(CONF_PIN) or "",
        )
        self._headers["apikey"] = self._token

    async def open_connection(self) -> ProtoConnection:
        """Protocol 2's socket, once the sync is done.

        A token the device refuses gets one more login, as the device's
        credentials or address may have changed.
        """
        try:
            return await self._connect()
        except proto.AuthRequired:
            await self._log_in()
        return await self._connect()

    async def _connect(self) -> ProtoConnection:
        conn = ProtoConnection(
            self._session, self._host, token=self._token, schema=self._schema
        )
        await conn.connect()
        return conn

    def attach(self, conn: ProtoConnection) -> None:
        """Send ops on conn, and take the device and its shades from its sync."""
        self._conn = conn
        self._schema = conn.schema
        cfg = compat.config(conn.model)
        version = cfg.pop("version")
        self._config.update(cfg)
        self._deviceName = cfg["hostname"]
        self.set_firmware({"version": version})

    def detach(self, conn: ProtoConnection) -> None:
        """Conn has closed: no ops until the next."""
        if self._conn is conn:
            self._conn = None

    def reload(self) -> None:
        """Set the entry up again."""
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry_id)
        )

    async def _forward_setups(self) -> None:
        if self._configured:
            return
        _LOGGER.debug("ESPSomfy RTS Setting up entities")
        entry = self.hass.config_entries.async_get_entry(self._config_entry_id)
        await self.hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        self._configured = True

    async def update_firmware(self, version) -> bool:
        "Update to the latest firmware version."
        if self.protocol == 2:
            raise HomeAssistantError(
                "Update the firmware from the ESPSomfy RTS device's own page"
            )
        url = f"{self._api_url}/downloadFirmware?ver={version}"
        async with self._session.get(url, headers=self._headers) as resp:
            if resp.status == 200:
                return True
            _LOGGER.error(await resp.text())
        return False

    async def create_backup(self) -> str:
        """Save the device's backup in HA's config directory; returns its path.

        Raises HomeAssistantError when no backup was saved, and why: a backup
        is never lost quietly. The failure is a persistent notification too,
        for backups nobody watches, as an automation's; the next backup saved
        dismisses it.
        """
        notification_id = f"{DOMAIN}_backup_{self.server_id}"
        try:
            path = await self._save_backup()
        except HomeAssistantError as err:
            self._backup_failed(notification_id, err)
            raise
        except Exception as err:  # noqa: BLE001
            # Unexpected, but a backup still never fails quietly.
            failure = HomeAssistantError(f"No backup: {err!r}")
            self._backup_failed(notification_id, failure)
            raise failure from err
        persistent_notification.async_dismiss(self.hass, notification_id)
        return path

    def _backup_failed(self, notification_id: str, err: HomeAssistantError) -> None:
        persistent_notification.async_create(
            self.hass,
            f"{dt_util.now().strftime('%Y-%m-%d %H:%M')}: {err}",
            title=f"ESPSomfy RTS backup of {self._deviceName} failed",
            notification_id=notification_id,
        )

    async def _save_backup(self) -> str:
        # Protocol 2's firmware serves everything on port 80.
        base = self._config_url if self.protocol == 2 else self._api_url
        url = f"{base}/backup?attach=true"
        try:
            status, body = await self._get_backup(url)
            if status == 401:
                # The token changed since the login, as a security save does.
                await self._log_in()
                status, body = await self._get_backup(url)
        except proto.LoginRefused as err:
            raise HomeAssistantError(
                f"No backup: {self._host} refused the login: {err}"
            ) from err
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
            raise HomeAssistantError(
                f"No backup: getting it from {self._host} failed: {err!r}"
            ) from err
        if status != 200:
            reason = body.decode(errors="replace").strip()[:200]
            raise HomeAssistantError(
                f"No backup: {self._host} answered {status} {reason}"
            )
        if not body.strip():
            raise HomeAssistantError(f"No backup: {self._host} sent an empty one")
        path = os.path.join(
            self.backup_dir, f"{dt_util.now().strftime('%Y-%m-%dT%H_%M_%S')}.backup"
        )
        try:
            await self.hass.async_add_executor_job(_write_backup, path, body)
        except OSError as err:
            raise HomeAssistantError(f"No backup: saving {path} failed: {err}") from err
        _LOGGER.info("Saved the backup of %s to %s", self._host, path)
        return path

    async def _get_backup(self, url: str) -> tuple[int, bytes]:
        async with asyncio.timeout(BACKUP_TIMEOUT):
            async with self._session.get(url, headers=self._headers) as resp:
                return resp.status, await resp.read()

    def get_backups(self) -> list[str] | None:
        """Get a list of all the available backups."""
        f: list[str] = []
        if not os.path.exists(self.backup_dir):
            return None
        files = os.listdir(self.backup_dir)
        f = [
            file
            for file in files
            if os.path.isfile(os.path.join(self.backup_dir, file))
            and file.endswith(".backup")
            and file[:1].isdigit()
        ]
        f.sort(reverse=True)
        return f

    def apply_data(self, data) -> None:
        """Apply the returned data to the configuration."""
        self._config["serverId"] = data["serverId"]
        self._config["model"] = data["model"]
        if "chipModel" in data:
            self._config["chipModel"] = data["chipModel"]
        if "connType" in data:
            self._config["connType"] = data["connType"]
        if "checkForUpdate" in data:
            self._config["checkForUpdate"] = data["checkForUpdate"]
        if "rooms" in data:
            self._config["rooms"] = data["rooms"]
        elif "rooms" not in self._config:
            self._config["rooms"] = []
        if "shades" in data:
            self._config["shades"] = data["shades"]
        elif "shades" not in self._config:
            self._config["shades"] = []
        if "groups" in data:
            self._config["groups"] = data["groups"]
        elif "groups" not in self._config:
            self._config["groups"] = []
        if "hostname" in data:
            self._config["hostname"] = data["hostname"]
            self._deviceName = data["hostname"]
        if "authType" in data:
            self._config["authType"] = data["authType"]
            self._canLogin = True
        elif "authType" not in self._config:
            self._config["authType"] = 0
            self._canLogin = False
        if "permissions" in data:
            self._config["permissions"] = data["permissions"]
        elif "permissions" not in self._config:
            self._config["permissions"] = 1
        if "memory" in data:
            self._config["memory"] = data["memory"]
        self._needsKey = False
        if self._config["authType"] > 0:
            if self._config["permissions"] != 1:
                self._needsKey = True
        self.set_firmware(data)

    async def load_shades(self) -> Any | None:
        """Load all the shades from the controller."""
        async with self._session.get(
            f"{self._api_url}{API_SHADES}", headers=self._headers
        ) as resp:
            if resp.status == 200:
                self._config["shades"] = await resp.json()
                return self._config["shades"]
            _LOGGER.error(await resp.text())

    async def load_groups(self) -> Any | None:
        """Load all the groups from the controller."""
        async with self._session.get(
            f"{self._api_url}{API_GROUPS}", headers=self._headers
        ) as resp:
            if resp.status == 200:
                self._config["groups"] = await resp.json()
                return self._config["groups"]
            _LOGGER.error(await resp.text())

    async def tilt_open(self, shade_id: int):
        """Send the command to open the tilt."""
        await self.tilt_command({"shadeId": shade_id, "command": "up"})

    async def tilt_close(self, shade_id: int):
        """Send the command to close the tilt."""
        await self.tilt_command({"shadeId": shade_id, "command": "down"})

    async def position_tilt(self, shade_id: int, position: int):
        """Send the command to position the shade."""
        # print(f"Setting tilt position to {position}")
        await self.tilt_command({"shadeId": shade_id, "target": position})

    async def sun_flag_off(self, shade_id: int):
        """Send the command to turn off the sun flag."""
        await self.shade_command({"shadeId": shade_id, "command": "flag"})

    async def sun_flag_on(self, shade_id: int):
        """Send the command to turn off the sun flag."""
        await self.shade_command({"shadeId": shade_id, "command": "sunflag"})

    async def sun_flag_group_off(self, group_id: int):
        """Send the command to turn off the sun flag."""
        await self.group_command({"groupId": group_id, "command": "flag"})

    async def sun_flag_group_on(self, group_id: int):
        """Send the command to turn off the sun flag."""
        await self.group_command({"groupId": group_id, "command": "sunflag"})

    async def open_shade(self, shade_id: int):
        """Send the command to open the shade."""
        await self.shade_command({"shadeId": shade_id, "command": "up"})

    async def close_shade(self, shade_id: int):
        """Send the command to close the shade."""
        await self.shade_command({"shadeId": shade_id, "command": "down"})

    async def toggle_shade(self, shade_id: int):
        """Sent the command to toggle."""
        await self.shade_command({"shadeId": shade_id, "command": "toggle"})

    async def stop_shade(self, shade_id: int):
        """Send the command to stop the shade."""
        # print(f"STOP ShadeId:{shade_id}")
        await self.shade_command({"shadeId": shade_id, "command": "my"})

    async def open_group(self, group_id: int):
        """Send the command to open the group."""
        await self.group_command({"groupId": group_id, "command": "up"})

    async def close_group(self, group_id: int):
        """Send the command to close the group."""
        await self.group_command({"groupId": group_id, "command": "down"})

    async def stop_group(self, group_id: int):
        """Send the command to stop the group."""
        await self.group_command({"groupId": group_id, "command": "my"})

    async def position_shade(self, shade_id: int, position: int):
        """Send the command to position the shade."""
        await self.shade_command({"shadeId": shade_id, "target": position})

    async def raw_command(self, shade_id: int, command: str, repeat: int):
        """Send the command to the shade."""
        await self.shade_command(
            {"shadeId": shade_id, "command": command, "repeat": repeat}
        )

    async def shade_command(self, data):
        """Send commands to ESPSomfyRTS via PUT request."""
        await self.put_command(API_SHADECOMMAND, data)

    async def set_current_position(self, shade_id: int, position: int):
        """Set the current position without moving the motor."""
        await self.put_command(
            API_SETPOSITIONS, {"shadeId": shade_id, "position": position}
        )

    async def set_current_tilt_position(self, shade_id: int, tilt_position: int):
        """Set the current tilt position without moving the motor."""
        await self.put_command(
            API_SETPOSITIONS, {"shadeId": shade_id, "tiltPosition": tilt_position}
        )

    async def set_sunny(self, shade_id: int, sunny: bool):
        """Set the sunny condition for the motor."""
        await self.put_command(API_SETSENSOR, {"shadeId": shade_id, "sunny": sunny})

    async def set_windy(self, shade_id: int, windy: bool):
        """Set the windy condition for the motor."""
        await self.put_command(API_SETSENSOR, {"shadeId": shade_id, "windy": windy})

    async def put_command(self, command, data):
        """Send a put command to the device.

        On protocol 2 the route's op of the same name goes on the socket, and
        a refusal is raised.
        """
        if self.protocol == 2:
            op = OPS.get(command)
            if op is None:
                raise HomeAssistantError(f"Protocol 2 has no op for {command}")
            await self._op(op, data or {})
            return
        async with self._session.put(
            f"{self._api_url}{command}", json=data, headers=self._headers
        ) as resp:
            if resp.status == 200:
                pass
            else:
                _LOGGER.error(await resp.text())

    async def _op(self, name: str, fields: dict) -> None:
        if self._conn is None:
            raise HomeAssistantError(f"Not connected to ESPSomfy RTS at {self._host}")
        try:
            await self._conn.op(name, **fields)
        except proto.ProtoError as err:
            raise HomeAssistantError(
                f"ESPSomfy RTS refused {name}: {err.desc}"
            ) from err

    async def group_command(self, data):
        """Send commands to ESPSomfyRTS via PUT request."""
        await self.put_command(API_GROUPCOMMAND, data)

    async def tilt_command(self, data):
        """Send tilt commands to ESPSomfyRTS via PUT request."""
        await self.put_command(API_TILTCOMMAND, data)

    async def get_initial(self):
        """Get the initial config from ESPSomfy RTS."""
        try:
            self._session = aiohttp_client.async_get_clientsession(self.hass)
            async with self._session.get(
                f"{self._api_url}{API_DISCOVERY}", headers=self._headers
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.apply_data(data)
                    await self._forward_setups()
                else:
                    _LOGGER.error(await resp.text())
        except aiohttp.ClientError:
            pass
