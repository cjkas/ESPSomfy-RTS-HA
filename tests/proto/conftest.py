"""Fixtures for the protocol 2 tests: a fake device served on localhost.

proto.py imports nothing from Home Assistant, so these tests load it on its
own rather than through the integration's package.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from pathlib import Path
import sys

import aiohttp
from aiohttp import web
import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "custom_components" / "espsomfy_rts"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_device import HELLO, SYNC, FakeDevice  # noqa: E402
from proto import SYNC as SYNC_TYPE, Model, ProtoConnection, Schema  # noqa: E402


async def until(condition: Callable[[], object], timeout: float = 2) -> None:
    """Wait for condition() to hold, as the client takes frames in its own time."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def allow_localhost(request):
    """Let the tests reach the fake device under Home Assistant's harness.

    When it is installed, as for a run of all of tests/, its pytest-socket
    blocks sockets in every test.
    """
    if request.config.pluginmanager.has_plugin("socket"):
        request.getfixturevalue("socket_enabled")


@pytest.fixture
def recorded_schema() -> Schema:
    """The schema from the recorded sync."""
    schema = Schema()
    for f in SYNC:
        schema.add(f.get("d", {}))
    schema.hash = HELLO["d"]["sh"]
    return schema


@pytest.fixture
def model(recorded_schema) -> Model:
    """A model filled from the recorded sync."""
    m = Model(recorded_schema)
    for f in SYNC:
        device = Schema.without_rows(f.get("d", {}))
        if f["t"] == SYNC_TYPE and device:
            m.apply_sync(recorded_schema.expand(device))
    return m


@pytest.fixture
def device() -> FakeDevice:
    """The fake device; a test may change its settings before connecting."""
    return FakeDevice()


@pytest.fixture
async def host(device: FakeDevice):
    """The fake device's address, served for the test."""
    runner = web.AppRunner(device.app(), shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    yield f"127.0.0.1:{port}"
    await device.close_sockets()
    await runner.cleanup()


@pytest.fixture
async def session():
    """A client session for the test."""
    async with aiohttp.ClientSession() as s:
        yield s


@pytest.fixture
async def connect(session, host):
    """Opens connections to the fake device, and closes them after the test.

    connect(run=True, ...) also reads the connection's frames in the
    background, as the integration does once it is synced; timeout is
    connect()'s, and the other arguments go to ProtoConnection.
    """
    opened: list[ProtoConnection] = []
    running: list[asyncio.Task] = []

    async def open_connection(
        *, run: bool = False, timeout: float = 15, **kwargs
    ) -> ProtoConnection:
        conn = ProtoConnection(session, host, **kwargs)
        opened.append(conn)
        await conn.connect(timeout)
        if run:
            running.append(asyncio.create_task(conn.run()))
        return conn

    yield open_connection
    for conn in opened:
        await conn.close()
    for task in running:
        with contextlib.suppress(Exception):
            await task
