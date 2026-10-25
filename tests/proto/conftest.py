"""Fixtures for the protocol 2 tests: a fake device served on localhost.

proto.py imports nothing from Home Assistant, so these tests load it on its
own rather than through the integration's package.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import aiohttp
from aiohttp import web
import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "custom_components" / "espsomfy_rts"))
sys.path.insert(0, str(ROOT / "tests"))

from fake_device import FIXTURES, SYNC, FakeDevice  # noqa: E402
from proto import Schema  # noqa: E402


@pytest.fixture
def recorded_schema() -> Schema:
    """The schema from the recorded sync."""
    schema = Schema()
    for f in SYNC:
        schema.add(f.get("d", {}))
    schema.hash = json.loads((FIXTURES / "hello.json").read_text())["d"]["sh"]
    return schema


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
