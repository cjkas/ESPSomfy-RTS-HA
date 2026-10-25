"""A fake ESPSomfy RTS device: an aiohttp app that answers as the firmware does.

/loginContext and /login, and /ws replaying the hello and sync a real device
sent (tests/fixtures/proto2). Ops get ok unless a test sets another reply.
It packs and reads frames with its own table from the recorded schema, not
with the client's code under test.
"""

from __future__ import annotations

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
ERRORS = {name: int(number) for number, name in SCHEMA["errors"].items()}
ROWS = ("T", "F", "E", "Q")

TOKEN = "a-token"


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

    def __init__(
        self,
        *,
        security: int = 0,
        username: str = "",
        password: str = "",
        pin: str = "",
    ) -> None:
        """Initialize a device in a security mode."""
        self.security = security
        self.username = username
        self.password = password
        self.pin = pin
        # Old firmware has no /ws: set this to the status it answers instead.
        self.ws_status: int | None = None
        # Frames sent right after hello, before the sync.
        self.early: list[str] = []
        # Op name -> the reply's fields, or None for no reply at all.
        self.replies: dict[str, dict | None] = {}
        # (op name, fields by name) as received.
        self.ops: list[tuple[str, dict]] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.connects = 0
        self.schema_params: list[str | None] = []

    def app(self) -> web.Application:
        """The aiohttp app to serve."""
        app = web.Application()
        app.router.add_get("/loginContext", self._login_context)
        app.router.add_put("/login", self._login)
        app.router.add_get("/ws", self._ws)
        return app

    async def send(self, type_name: str, **fields: Any) -> None:
        """Send an event to every client."""
        for ws in self.sockets:
            await ws.send_str(frame(type_name, fields))

    async def close_sockets(self) -> None:
        """Close every client's socket, as a security save or reboot does."""
        for ws in list(self.sockets):
            await ws.close()

    async def _login_context(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "type": self.security,
                "permissions": 0,
                "serverId": "A1B2C3",
                "version": HELLO["d"]["fw"],
                "model": "ESPSomfyRTS",
                "hostname": "espsomfy",
            }
        )

    async def _login(self, request: web.Request) -> web.Response:
        body = await request.json()
        ok = (
            self.security == 0
            or (self.security == 1 and body.get("pin") == self.pin)
            or (
                self.security == 2
                and body.get("username") == self.username
                and body.get("password") == self.password
            )
        )
        if not ok:
            return web.json_response(
                {"type": self.security, "msg": "Invalid credentials", "success": False},
                status=401,
            )
        return web.json_response(
            {"type": self.security, "apiKey": TOKEN, "msg": "Success", "success": True}
        )

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if self.ws_status is not None:
            return web.Response(status=self.ws_status)
        if self.security != 0 and request.query.get("apikey") != TOKEN:
            return web.Response(status=401)
        self.connects += 1
        known = request.query.get("schema")
        self.schema_params.append(known)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        try:
            await self._serve(ws, skip_schema=known == HELLO["d"]["sh"])
        finally:
            self.sockets.remove(ws)
        return ws

    async def _serve(self, ws: web.WebSocketResponse, skip_schema: bool) -> None:
        await ws.send_str(json.dumps(HELLO))
        for text in self.early:
            await ws.send_str(text)
        sync = []
        for f in SYNC:
            if skip_schema and f["t"] == TYPES["sync"]:
                d = {k: v for k, v in f["d"].items() if k not in ROWS}
                if not d:
                    continue
                f = {**f, "d": d}
            sync.append(json.dumps(f))
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
            self.ops.append((name, unpack(f.get("d", {}))))
            if "id" not in f:
                continue
            reply = self.replies.get(name, {"ok": True})
            if reply is not None:
                await ws.send_str(frame("reply", reply, f["id"]))
