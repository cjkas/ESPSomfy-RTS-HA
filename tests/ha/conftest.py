"""Fixtures for the integration's tests in Home Assistant's test harness.

pytest-homeassistant-custom-component gives the hass fixture; the device is
the fake one the protocol 2 tests use, served on localhost.
"""

from __future__ import annotations

from pathlib import Path
import sys

from aiohttp import web
import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "ha"))

from fake_device import FakeDevice  # noqa: E402
from harness import UNIQUE_ID  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
)

from custom_components.espsomfy_rts.const import DOMAIN  # noqa: E402


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load the integration from custom_components."""
    return


@pytest.fixture
def device() -> FakeDevice:
    """The fake device; a test may change its settings before setup."""
    return FakeDevice()


@pytest.fixture
async def host(device: FakeDevice, socket_enabled):
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
def entry(hass, host) -> MockConfigEntry:
    """A config entry for the fake device, under None."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UNIQUE_ID,
        title="espsomfy",
        data={"host": host, "username": "", "password": "", "pin": ""},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def loaded(hass, entry):
    """The entry set up, and unloaded after the test."""
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
