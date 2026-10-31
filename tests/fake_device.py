"""A fake ESPSomfy RTS device: an aiohttp app that answers as the firmware does.

/loginContext and /login, /backup, and /ws replaying the hello and sync a
real device sent (tests/fixtures/proto2). Ops get ok unless a test sets
another reply. It packs and reads frames with its own table from the recorded
schema, never with the client's code under test.

With legacy set it is firmware before protocol 2 instead: /ws answers 404,
and /discovery, the command routes and an old socket at /legacy answer as the
HTTP API on port 8081 and the socket on 8080 did.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

FIXTURES = Path(__file__).parent / "fixtures" / "proto2"
HELLO = json.loads((FIXTURES / "hello.json").read_text())
SYNC = json.loads((FIXTURES / "sync.json").read_text())
SCHEMA = json.loads((FIXTURES / "schema.json").read_text())

TYPES = {name: int(number) for number, name in SCHEMA["types"].items()}
TYPE_NAMES = {number: name for name, number in TYPES.items()}
KEYS = {f["name"]: key for key, f in SCHEMA["fields"].items()}
NAMES = {key: f["name"] for key, f in SCHEMA["fields"].items()}
ROWS = ("T", "F", "E", "Q")

# The security modes, as /loginContext's type gives them.
MODE_NONE = 0
MODE_PIN = 1
MODE_PASSWORD = 2

TOKEN = "a-token"
# As in the recorded sync's header, which the device's other routes agree with.
SERVER_ID = "serverId 2"
HOSTNAME = "hostname 3"


def pack(value: Any) -> Any:
    """Names to short keys and booleans to 1/0, as the firmware writes them."""
    if isinstance(value, list):
        return [pack(v) for v in value]
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, dict):
        return {KEYS[name]: pack(v) for name, v in value.items()}
    return value


def unpack(value: Any) -> Any:
    """Short keys to names; values as sent."""
    if isinstance(value, list):
        return [unpack(v) for v in value]
    if isinstance(value, dict):
        return {NAMES.get(key, key): unpack(v) for key, v in value.items()}
    return value


def frame(type_name: str, d: dict | None = None, msg_id: int | None = None) -> str:
    """A frame from the device, packed."""
    out: dict[str, Any] = {"v": 2, "t": TYPES[type_name]}
    if msg_id is not None:
        out["id"] = msg_id
    if d:
        out["d"] = pack(d)
    return json.dumps(out)


class FakeDevice:
    """What the tests set and see of the device."""

    def __init__(self) -> None:
        """Initialize a device under None, as a real one is out of the box."""
        self.security_mode = MODE_NONE
        self.username = ""
        self.password = ""
        self.pin = ""
        # /loginContext's permissions: 1 is "Secure Config Only", where old
        # firmware takes commands without the token.
        self.permissions = 0
        # What /login gives and the socket and /backup take.
        self.token = TOKEN
        # How /login refuses: "401" as the firmware does now, "200" with
        # success false as it did before protocol 2, "401 text" with no JSON.
        self.login_refusal = "401"
        # Seconds the login throttle still refuses /login for, as 429.
        self.throttled_for: int | None = None
        # Old firmware has no /ws: set this to the status it answers instead.
        self.ws_status: int | None = None
        # The hello sent; None for a socket that opens and says nothing.
        self.hello: dict | None = HELLO
        # Close each socket as soon as it opens, as with every slot taken.
        self.close_at_once = False
        # Frames sent right after hello, before the sync.
        self.early: list[str] = []
        # Op name -> the reply's fields, or None for no reply at all.
        self.replies: dict[str, dict | None] = {}
        # Op name -> how many times it is refused busy without its id first,
        # as the device does while it puts another long message together.
        self.busy_without_id: dict[str, int] = {}
        # (op name, fields by name) as received.
        self.ops: list[tuple[str, dict]] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.schema_params: list[str | None] = []
        # The apikey header of each /backup.
        self.backups: list[str | None] = []
        # The status and body /backup answers with once authorized.
        self.backup_reply = (200, '{"version": 23, "shades": []}')
        self.legacy = False
        # (route, body) of each command to the HTTP API.
        self.puts: list[tuple[str, dict | None]] = []

    def app(self) -> web.Application:
        """The aiohttp app to serve."""
        app = web.Application()
        app.router.add_get("/loginContext", self._login_context)
        app.router.add_put("/login", self._login)
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/backup", self._backup)
        app.router.add_get("/discovery", self._discovery)
        app.router.add_put("/{route}", self._put)
        app.router.add_get("/legacy", self._legacy_ws)
        return app

    @asynccontextmanager
    async def serve(self) -> AsyncIterator[str]:
        """The app served on a free port of localhost, by its address."""
        runner = web.AppRunner(self.app(), shutdown_timeout=0.1)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        try:
            yield f"127.0.0.1:{runner.addresses[0][1]}"
        finally:
            await self.close_sockets()
            await runner.cleanup()

    async def send(self, type_name: str, **fields: Any) -> None:
        """Send an event to every client."""
        for ws in self.sockets:
            await ws.send_str(frame(type_name, fields))

    async def send_legacy(self, event: str, **fields: Any) -> None:
        """Send an event on the old socket, framed as it was."""
        for ws in self.sockets:
            await ws.send_str(f"42[{event},{json.dumps(fields)}]")

    async def close_sockets(self) -> None:
        """Close every client's socket, as a security save or reboot does."""
        for ws in list(self.sockets):
            await ws.close()

    async def _login_context(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "type": self.security_mode,
                "permissions": self.permissions,
                "serverId": SERVER_ID,
                "version": HELLO["d"]["fw"],
                "model": "ESPSomfyRTS",
                "hostname": HOSTNAME,
            }
        )

    async def _login(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.throttled_for is not None:
            secs = self.throttled_for
            return web.json_response(
                {
                    "type": self.security_mode,
                    "msg": f"Too many failed logins. Try again in {secs} s.",
                    "success": False,
                    "retryAfter": secs,
                },
                status=429,
                headers={"Retry-After": str(secs)},
            )
        ok = (
            self.security_mode == MODE_NONE
            or (self.security_mode == MODE_PIN and body.get("pin") == self.pin)
            or (
                self.security_mode == MODE_PASSWORD
                and body.get("username") == self.username
                and body.get("password") == self.password
            )
        )
        if ok:
            return web.json_response(
                {
                    "type": self.security_mode,
                    "apiKey": self.token,
                    "msg": "Success",
                    "success": True,
                }
            )
        if self.login_refusal == "401 text":
            return web.Response(status=401, text="Unauthorized")
        return web.json_response(
            {
                "type": self.security_mode,
                "msg": "Invalid credentials",
                "success": False,
            },
            status=401 if self.login_refusal == "401" else 200,
        )

    def _authorized(self, token: str | None) -> bool:
        return self.security_mode == MODE_NONE or token == self.token

    async def _backup(self, request: web.Request) -> web.Response:
        token = request.headers.get("apikey")
        self.backups.append(token)
        if not self._authorized(token):
            return web.Response(status=401, text="Unauthorized API Key")
        status, body = self.backup_reply
        return web.Response(status=status, text=body)

    async def _discovery(self, request: web.Request) -> web.Response:
        if not self.legacy:
            return web.Response(status=404)
        shades = [
            {
                "shadeId": 1,
                "name": "Living room",
                "shadeType": 0,
                "position": 30,
                "flipPosition": False,
                "tiltType": 0,
            }
        ]
        return web.json_response(
            {
                "serverId": SERVER_ID,
                "model": "ESPSomfyRTS",
                "hostname": HOSTNAME,
                "version": "v2.4.7",
                "connType": "Wifi",
                "rooms": [],
                "shades": shades,
                "groups": [],
            }
        )

    async def _put(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        self.puts.append((request.match_info["route"], body))
        return web.json_response({"ok": True})

    async def _legacy_ws(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        try:
            async for _ in ws:
                pass
        finally:
            self.sockets.remove(ws)
        return ws

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if self.legacy:
            return web.Response(status=404)
        if self.ws_status is not None:
            return web.Response(status=self.ws_status)
        if not self._authorized(request.query.get("apikey")):
            return web.Response(status=401)
        known = request.query.get("schema")
        self.schema_params.append(known)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.close_at_once:
            await ws.close()
            return ws
        self.sockets.append(ws)
        try:
            await self._serve(ws, skip_schema=known == HELLO["d"]["sh"])
        finally:
            self.sockets.remove(ws)
        return ws

    def _sync_frames(self, skip_schema: bool) -> list[str]:
        """The recorded sync, without the schema's rows for a client that has it."""
        frames = []
        for f in SYNC:
            if skip_schema and f["t"] == TYPES["sync"]:
                d = {k: v for k, v in f["d"].items() if k not in ROWS}
                if not d:
                    continue
                f = {**f, "d": d}
            frames.append(json.dumps(f))
        return frames

    async def _serve(self, ws: web.WebSocketResponse, skip_schema: bool) -> None:
        if self.hello is None:
            async for _ in ws:
                pass
            return
        await ws.send_str(json.dumps(self.hello))
        for text in self.early:
            await ws.send_str(text)
        sync = self._sync_frames(skip_schema)
        await ws.send_str(sync.pop(0))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            f = json.loads(msg.data)
            name = TYPE_NAMES.get(f["t"])
            if name == "next":
                if sync:
                    await ws.send_str(sync.pop(0))
                continue
            if self.busy_without_id.get(name, 0) > 0:
                self.busy_without_id[name] -= 1
                await ws.send_str(frame("reply", {"code": 5, "desc": "busy"}))
                continue
            self.ops.append((name, unpack(f.get("d", {}))))
            if "id" not in f:
                continue
            reply = self.replies.get(name, {"ok": True})
            if reply is not None:
                await ws.send_str(frame("reply", reply, f["id"]))
