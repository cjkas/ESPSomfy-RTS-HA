"""Socket protocol 2 of the ESPSomfy RTS firmware.

The firmware's docs/PROTOCOL.md describes it: one websocket at ws://host/ws,
a hello, then a sync of the shade model in frames the client asks for one at a
time with next, then ops answered by their reply and events as things change.
Frames use short keys from a schema the device sends first.

Nothing here imports Home Assistant, so tests/proto runs it with plain pytest
against a fake device that replays what a real one sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
from typing import Any, NamedTuple
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

VERSION = 2
# The types a client reads before it has the schema, and next, which it sends
# during the sync.
HELLO = 1
REPLY = 2
NEXT = 3
SYNC = 4
SYNC_END = 5
# The keys of the schema's rows in a sync frame: types, fields, error codes
# and feeds.
ROWS = ("T", "F", "E", "Q")

# /loginContext's type.
SECURITY_NONE = 0
SECURITY_PIN = 1
SECURITY_PASSWORD = 2

# Ping interval; aiohttp drops the socket when the pong is half this late.
HEARTBEAT = 20


class ProtoError(Exception):
    """An op the device refused, or that never got its reply.

    code is an error name from the schema (badRequest, busy ...), or
    notConnected, closed or timeout from the client itself.
    """

    def __init__(
        self, code: str, desc: str | None = None, data: dict | None = None
    ) -> None:
        """Initialize the error."""
        super().__init__(desc or code)
        self.code = code
        self.desc = desc or code
        self.data = data or {}


class AuthRequired(Exception):
    """The device refused the socket: it is Secured and the token isn't good."""


class NoProtocol2(Exception):
    """The device has no protocol 2 socket: firmware from before it."""


class ConnectionClosed(Exception):
    """The socket closed before the sync was done."""


class LoginRefused(Exception):
    """/login refused the credentials."""

    def __init__(self, security_type: int) -> None:
        """Initialize with the device's security type."""
        super().__init__(f"login refused (security type {security_type})")
        self.security_type = security_type


class Schema:
    """The device's table of types, fields, error codes and feeds.

    It turns a frame's short keys into names and back, and knows what a field
    left out of a frame is: its default.
    """

    def __init__(self) -> None:
        """Initialize an empty schema."""
        self.hash: str | None = None
        self.types: dict[int, str] = {}
        self.type_numbers: dict[str, int] = {}
        # key -> (name, kind, default)
        self.fields: dict[str, tuple[str, str, Any]] = {}
        self.keys: dict[str, str] = {}
        self.errors: dict[int, str] = {}
        self.feed_numbers: dict[str, int] = {}

    def add(self, d: dict) -> None:
        """Take the schema's rows from a sync frame; they may span several."""
        for number, name in d.get("T", []):
            self.types[number] = name
            self.type_numbers[name] = number
        for key, name, kind, default in d.get("F", []):
            self.fields[key] = (name, kind, default)
            self.keys[name] = key
        for number, name in d.get("E", []):
            self.errors[number] = name
        for number, name in d.get("Q", []):
            self.feed_numbers[name] = number

    @property
    def empty(self) -> bool:
        """Whether no field has come yet."""
        return not self.fields

    def default(self, name: str) -> Any:
        """What a field left out of a frame is."""
        key = self.keys.get(name)
        if key is None:
            return None
        _, kind, default = self.fields[key]
        match kind:
            case "b":
                return default != 0
            case "s":
                return ""
            case "l":
                return []
            case "o":
                return {}
        return default

    def expand(self, value: Any) -> Any:
        """Short keys to names and 1/0 to booleans, all the way down.

        A field left out stays out; fill() puts the defaults in.
        """
        if isinstance(value, list):
            return [self.expand(v) for v in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for key, v in value.items():
            field = self.fields.get(key)
            if field is None:
                out[key] = self.expand(v)
                continue
            name, kind, _ = field
            if kind == "b" and isinstance(v, int):
                v = v != 0
            out[name] = self.expand(v)
        return out

    def pack(self, value: Any) -> Any:
        """Names to short keys and booleans to 1/0, for an op's payload.

        A field set to None is left out. A name the schema doesn't have is a
        KeyError: the device would refuse it anyway.
        """
        if isinstance(value, list):
            return [self.pack(v) for v in value]
        if isinstance(value, bool):
            return int(value)
        if not isinstance(value, dict):
            return value
        out = {}
        for name, v in value.items():
            if v is None:
                continue
            if name not in self.keys:
                raise KeyError(f"The device has no field {name}")
            out[self.keys[name]] = self.pack(v)
        return out

    def fill(self, obj: dict, names: tuple[str, ...]) -> dict:
        """Set each of names that obj lacks to its default; returns obj."""
        for name in names:
            if name not in obj:
                obj[name] = self.default(name)
        return obj


class _Item(NamedTuple):
    """A kind of item in the model: where it is kept, its id field and events."""

    name: str
    collection: str
    id_field: str
    state: str | None
    removed: str
    order: str


# The fields of each kind of item as the device writes them: what a frame
# that leaves one out means by it. The schema has each field's default, not
# which items have it. The firmware's data-src/js/model.js has the same lists.
_SHADE_STATE = (
    "shadeId", "position", "target", "direction", "myPos", "tiltPosition", "tiltTarget",
    "tiltDirection", "myTiltPos", "flags", "lastRollingCode",
)  # fmt: skip
FIELDS: dict[str, tuple[str, ...]] = {
    "header": (
        "maxRooms", "maxShades", "maxGroups", "maxGroupedShades", "maxLinkedRemotes",
        "startingAddress", "bitLength", "proto", "serverId", "hostname", "model",
        "chipModel", "fw", "connType",
    ),
    "room": ("roomId", "name", "sortOrder"),
    "shadeState": _SHADE_STATE,
    "shade": _SHADE_STATE + (
        "roomId", "name", "remoteAddress", "upTime", "downTime", "tiltTime", "stepSize",
        "paired", "tiltType", "shadeType", "bitLength", "proto", "flipCommands",
        "flipPosition", "inGroup", "sunSensor", "light", "simMy", "repeats", "sortOrder",
        "gpioUp", "gpioDown", "gpioMy", "gpioLLTrigger", "linkedRemotes",
    ),
    "groupState": ("groupId", "flags", "direction"),
    "group": (
        "groupId", "roomId", "name", "remoteAddress", "lastRollingCode", "bitLength",
        "proto", "sunSensor", "flipCommands", "flags", "repeats", "sortOrder", "linkedShades",
    ),
    "linkedRemote": ("remoteAddress", "lastRollingCode"),
    "memory": (
        "freeHeap", "maxHeap", "minHeap", "totalHeap", "uptime", "loopRate", "loopMaxUs",
    ),
    "wifi": ("ssid", "strength", "channel"),
    "ethernet": ("connected", "speed", "fullDuplex"),
}  # fmt: skip


class Model:
    """The shade model the sync sends and events keep up to date.

    Items are inserted or updated by id, so an event during the sync does no
    harm. It takes frames already expanded to names and fills in what a frame
    leaves out at its default.
    """

    ITEMS = (
        _Item("room", "rooms", "roomId", None, "roomRemoved", "roomOrder"),
        _Item("shade", "shades", "shadeId", "shadeState", "shadeRemoved", "shadeOrder"),
        _Item("group", "groups", "groupId", "groupState", "groupRemoved", "groupOrder"),
    )

    def __init__(self, schema: Schema) -> None:
        """Initialize an empty model."""
        self.schema = schema
        self.header: dict[str, Any] = {}
        self.rooms: dict[int, dict] = {}
        self.shades: dict[int, dict] = {}
        self.groups: dict[int, dict] = {}
        self.repeaters: list[int] = []
        self.status: dict[str, Any] = {}

    def apply_sync(self, d: dict) -> None:
        """Take the sections of a sync frame."""
        if "header" in d:
            self.header = self.schema.fill(dict(d["header"]), FIELDS["header"])
        for kind in self.ITEMS:
            for item in d.get(kind.collection, []):
                self._put(kind, item)
        if "repeaters" in d:
            self.repeaters = list(d["repeaters"])
        if "status" in d:
            self._put_status(d["status"], FIELDS["memory"])
            link = "ethernet" if self.header.get("connType") == "Ethernet" else "wifi"
            self._put_status(d["status"], FIELDS[link])

    def apply_event(self, name: str, d: dict) -> bool:
        """Take an event by its type's name.

        False for one that isn't about the model, or that changed nothing.
        """
        for kind in self.ITEMS:
            item_id = d.get(kind.id_field, 0)
            if name == kind.name:
                self._put(kind, d)
                return True
            if name == kind.state:
                return self._merge_state(kind, item_id, d)
            if name == kind.removed:
                return getattr(self, kind.collection).pop(item_id, None) is not None
            if name == kind.order:
                self._order(kind, d.get("ids", []))
                return True
        match name:
            case "repeaters":
                self.repeaters = list(d.get("repeaters", []))
            case "memory" | "wifi" | "ethernet":
                self._put_status(d, FIELDS[name])
            case _:
                return False
        return True

    def _put(self, kind: _Item, item: dict) -> None:
        """A whole item: what it had before is replaced."""
        whole = self.schema.fill(dict(item), FIELDS[kind.name])
        # A shade's linked remotes leave out their defaults too.
        if kind.name == "shade":
            whole["linkedRemotes"] = [
                self.schema.fill(dict(r), FIELDS["linkedRemote"])
                for r in whole["linkedRemotes"]
            ]
        getattr(self, kind.collection)[whole[kind.id_field]] = whole

    def _merge_state(self, kind: _Item, item_id: int, d: dict) -> bool:
        """Part of an item: only its state.

        A state of an item not synced yet waits for the item.
        """
        item = getattr(self, kind.collection).get(item_id)
        if item is None:
            return False
        item.update(self.schema.fill(dict(d), FIELDS[kind.state]))
        return True

    def _order(self, kind: _Item, ids: list[int]) -> None:
        items = getattr(self, kind.collection)
        for i, item_id in enumerate(ids):
            if item_id in items:
                items[item_id]["sortOrder"] = i

    def _put_status(self, d: dict, names: tuple[str, ...]) -> None:
        for name in names:
            self.status[name] = d[name] if name in d else self.schema.default(name)


class ProtoConnection:
    """One socket to the device.

    connect() opens it and returns once the sync is done, so the model is
    whole. run() then reads frames until the socket closes, applying events to
    the model and handing them to the listeners. It doesn't connect again: the
    caller does, with a new connection, passing the schema on so the device
    needn't send it again.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        *,
        token: str | None = None,
        schema: Schema | None = None,
        op_timeout: float = 10,
    ) -> None:
        """Initialize a connection that isn't open yet."""
        self.host = host
        self.token = token
        self.schema = schema or Schema()
        self.model = Model(self.schema)
        self.fw: str | None = None
        self.synced = False
        self._session = session
        self._op_timeout = op_timeout
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._hello = False
        self._hello_hash: str | None = None
        self._readable = False
        self._held: list[dict] = []
        self._listeners: list[Callable[[str, dict], None]] = []

    def url(self) -> str:
        """The socket's address, with the token and the schema hash it has."""
        params = {}
        if self.token:
            params["apikey"] = self.token
        if self.schema.hash:
            params["schema"] = self.schema.hash
        query = f"?{urlencode(params)}" if params else ""
        return f"ws://{self.host}/ws{query}"

    def add_listener(self, cb: Callable[[str, dict], None]) -> Callable[[], None]:
        """Call cb(name, d) for each device event, after the model has it.

        Returns a function that removes it.
        """
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    async def connect(self, timeout: float = 15) -> None:
        """Open the socket and take hello and the whole sync.

        Raises AuthRequired when the device wants a token, NoProtocol2 when it
        has no protocol 2 socket, ConnectionClosed when it closed the socket
        first (as it does with every slot taken), and aiohttp's or the OS's
        errors when it can't be reached.
        """
        try:
            async with asyncio.timeout(timeout):
                self._ws = await self._session.ws_connect(
                    self.url(), heartbeat=HEARTBEAT
                )
                while not self.synced:
                    if not await self._receive():
                        raise ConnectionClosed(
                            "the device closed the socket during the sync"
                        )
        except aiohttp.WSServerHandshakeError as err:
            if err.status == 401:
                raise AuthRequired from err
            raise NoProtocol2(f"/ws answered {err.status}") from err
        except BaseException:
            await self.close()
            raise

    async def run(self, receive_timeout: float = 60) -> None:
        """Read frames until the socket closes.

        The device sends its memory every 30 s, so a minute of silence means
        the link is dead even if TCP hasn't noticed: that raises TimeoutError.
        """
        try:
            while await self._receive(receive_timeout):
                pass
        finally:
            self._lost()

    async def close(self) -> None:
        """Close the socket; ops waiting for a reply fail."""
        self._lost()
        if self._ws is not None:
            await self._ws.close()

    async def op(self, name: str, **fields: Any) -> dict:
        """Send an op by its name with its fields by name.

        Returns the reply's fields; raises ProtoError when the device refuses
        it or no reply comes.
        """
        if not self.synced or self._ws is None or self._ws.closed:
            raise ProtoError("notConnected", "Not connected to the device")
        number = self.schema.type_numbers.get(name)
        if number is None:
            raise ProtoError("unknownOp", f"The device has no op {name}")
        try:
            d = self.schema.pack(fields)
        except KeyError as err:
            raise ProtoError("badRequest", str(err.args[0])) from None
        msg_id = self._next_id
        self._next_id += 1
        frame: dict[str, Any] = {"v": VERSION, "t": number, "id": msg_id}
        if d:
            frame["d"] = d
        reply = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = reply
        try:
            await self._ws.send_str(json.dumps(frame, separators=(",", ":")))
            async with asyncio.timeout(self._op_timeout):
                return await reply
        except TimeoutError:
            raise ProtoError("timeout", f"No reply to {name}") from None
        finally:
            self._pending.pop(msg_id, None)

    async def _receive(self, timeout: float | None = None) -> bool:
        """Take one message; False once the socket has closed."""
        msg = await self._ws.receive(timeout=timeout)
        if msg.type == aiohttp.WSMsgType.TEXT:
            await self._frame(msg.data)
            return True
        return msg.type not in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        )

    async def _frame(self, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            _LOGGER.debug("Bad frame %r", text)
            return
        if not isinstance(frame, dict):
            return
        if not self._hello:
            # The first frame from a protocol 2 device is always hello.
            if frame.get("v") != VERSION or frame.get("t") != HELLO:
                raise NoProtocol2(
                    f"the first frame wasn't a protocol 2 hello: {text[:80]}"
                )
        elif frame.get("v") != VERSION:
            _LOGGER.debug("Frame of protocol %s ignored", frame.get("v"))
            return
        d = frame.get("d") or {}
        # These have fixed numbers: they come before the schema.
        t = frame.get("t")
        if t == HELLO:
            self._take_hello(d)
        elif t == SYNC:
            await self._sync(d)
        elif t == SYNC_END:
            self._sync_end()
        elif not self._readable:
            # A frame the client can't read yet waits for the schema.
            self._held.append(frame)
        else:
            self._dispatch(frame)

    def _take_hello(self, d: dict) -> None:
        self._hello = True
        self.fw = d.get("fw")
        self._hello_hash = d.get("sh")
        # A schema kept for this hash needn't come again; any other is no
        # use, and the device sends the one it has.
        if self.schema.hash != self._hello_hash:
            self.schema = Schema()
            self.model = Model(self.schema)
        if self.schema.hash is not None:
            self._make_readable()

    async def _sync(self, d: dict) -> None:
        self.schema.add(d)
        device = {k: v for k, v in d.items() if k not in ROWS}
        # The schema's rows come first, so a frame with the device's sections
        # has the whole of it.
        if device:
            self._make_readable()
            self.model.apply_sync(self.schema.expand(device))
        await self._ws.send_str(
            json.dumps({"v": VERSION, "t": NEXT}, separators=(",", ":"))
        )

    def _sync_end(self) -> None:
        self._make_readable()
        # Only a whole schema is kept, so a sync cut short sends it again.
        self.schema.hash = self._hello_hash
        self.synced = True

    def _make_readable(self) -> None:
        """The schema is whole: frames that waited for it go, in order."""
        if self._readable:
            return
        self._readable = True
        held, self._held = self._held, []
        for frame in held:
            self._dispatch(frame)

    def _dispatch(self, frame: dict) -> None:
        d = self.schema.expand(frame.get("d") or {})
        if frame.get("t") == REPLY:
            self._reply(frame.get("id"), d)
            return
        name = self.schema.types.get(frame.get("t"))
        if name is None:
            return
        self.model.apply_event(name, d)
        for cb in list(self._listeners):
            cb(name, d)

    def _reply(self, msg_id: int | None, d: dict) -> None:
        err = None
        if not d.get("ok"):
            code = self.schema.errors.get(d.get("code"), "internal")
            err = ProtoError(code, d.get("desc"), d)
        reply = self._pending.get(msg_id)
        if reply is None or reply.done():
            if err is not None:
                _LOGGER.debug("The device refused a message: %s", err.desc)
            return
        if err is not None:
            reply.set_exception(err)
        else:
            reply.set_result(d)

    def _lost(self) -> None:
        """The socket has gone: nothing waiting gets its reply."""
        self.synced = False
        for reply in self._pending.values():
            if not reply.done():
                reply.set_exception(ProtoError("closed", "The connection closed"))
        self._pending.clear()


async def login_context(
    session: aiohttp.ClientSession, host: str, timeout: float = 10
) -> dict:
    """The device's security type and identity, which need no login."""
    async with asyncio.timeout(timeout):
        async with session.get(f"http://{host}/loginContext") as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


async def login(
    session: aiohttp.ClientSession,
    host: str,
    *,
    username: str = "",
    password: str = "",
    pin: str = "",
    timeout: float = 10,
) -> str:
    """A token from /login; LoginRefused when the credentials aren't right.

    Under None the device gives a token without checking anything.
    """
    body = {"username": username, "password": password, "pin": pin}
    async with asyncio.timeout(timeout):
        async with session.put(f"http://{host}/login", json=body) as resp:
            # The firmware answers a refusal with 401; before protocol 2 it
            # was 200 with success false.
            if resp.status == 401:
                data = await resp.json(content_type=None)
                raise LoginRefused(int(data.get("type", 0)))
            resp.raise_for_status()
            data = await resp.json(content_type=None)
    if not data.get("success"):
        raise LoginRefused(int(data.get("type", 0)))
    return data.get("apiKey", "")
