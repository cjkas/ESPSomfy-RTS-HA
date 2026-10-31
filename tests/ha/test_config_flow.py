"""The config flow: the address, then credentials only when Secured."""

from __future__ import annotations

from ipaddress import ip_address
import socket

from harness import UNIQUE_ID, until
from fake_device import HOSTNAME, MODE_PASSWORD, MODE_PIN, SERVER_ID
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import (
    SOURCE_USER,
    SOURCE_ZEROCONF,
    ConfigEntryState,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.espsomfy_rts.const import DOMAIN

NO_CREDENTIALS = {"username": "", "password": "", "pin": ""}


async def start(hass, host: str):
    """The user flow, past the address."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": host}
    )


async def test_an_open_device_needs_only_its_address(hass, host, device):
    """Under None there is nothing to ask."""
    result = await start(hass, host)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == HOSTNAME
    assert result["data"] == {"host": host, **NO_CREDENTIALS}
    assert result["result"].unique_id == UNIQUE_ID


async def test_a_password_is_asked_and_checked(hass, host, device):
    """A refused password is asked again; the right one is kept."""
    device.security_mode = MODE_PASSWORD
    device.username, device.password = "admin", "secret"
    result = await start(hass, host)
    assert result["step_id"] == "credentials"
    assert set(result["data_schema"].schema) == {"username", "password"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "admin", "password": "wrong"}
    )
    assert result["errors"] == {"base": "invalid_password"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"username": "admin", "password": "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "host": host,
        "username": "admin",
        "password": "secret",
        "pin": "",
    }


async def test_a_throttled_login_says_when_to_try_again(hass, host, device):
    """The device's login throttle is its own error, not cannot_connect."""
    device.security_mode = MODE_PIN
    device.pin = "1234"
    result = await start(hass, host)
    device.throttled_for = 30
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pin": "1234"}
    )
    assert result["errors"] == {"base": "too_many_attempts"}
    assert result["description_placeholders"]["retry_after"] == "30"

    device.throttled_for = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pin": "1234"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("refusal", ["401", "200"])
async def test_a_pin_is_asked_and_checked(hass, host, device, refusal):
    """Firmware before protocol 2 refused with 200 and success false."""
    device.security_mode = MODE_PIN
    device.pin = "1234"
    device.login_refusal = refusal
    result = await start(hass, host)
    assert set(result["data_schema"].schema) == {"pin"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pin": "0000"}
    )
    assert result["errors"] == {"base": "invalid_pin"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pin": "1234"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["pin"] == "1234"


async def test_an_address_nothing_answers(hass, socket_enabled):
    """A closed port is cannot_connect, and the address is asked again."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    result = await start(hass, f"127.0.0.1:{port}")
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_not_an_address(hass):
    """An address that can't be one is refused before anything is sent."""
    result = await start(hass, "not an address!")
    assert result["errors"] == {"host": "invalid_host"}


async def test_a_device_already_set_up(hass, host, device):
    """The same server id updates the entry's address and stops."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=UNIQUE_ID, data={"host": "10.0.0.9"}
    )
    entry.add_to_hass(hass)
    result = await start(hass, host)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == host


class _Discovered(ZeroconfServiceInfo):
    """A discovery whose host carries the fake device's port.

    A real device answers on port 80, which its address alone means.
    """

    def __init__(self, host: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._host = host

    @property
    def host(self) -> str:
        return self._host


async def test_zeroconf_asks_for_credentials_when_secured(hass, host, device):
    """A discovered Secured device gets the same credentials step."""
    device.security_mode = MODE_PIN
    device.pin = "1234"
    info = _Discovered(
        host,
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        port=8080,
        hostname="espsomfy.local.",
        type="_espsomfy_rts._tcp.local.",
        name="espsomfy._espsomfy_rts._tcp.local.",
        properties={"serverId": SERVER_ID, "model": "ESPSomfyRTS"},
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info
    )
    assert result["step_id"] == "zeroconf_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "credentials"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"pin": "1234"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "host": host,
        "username": "",
        "password": "",
        "pin": "1234",
    }


async def test_reauth_takes_new_credentials(hass, entry, device):
    """The credentials the device takes go in the entry, which is set up again."""
    device.security_mode = MODE_PASSWORD
    device.username, device.password = "admin", "secret"
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    [flow] = hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"username": "admin", "password": "wrong"}
    )
    assert result["errors"] == {"base": "invalid_password"}
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"username": "admin", "password": "secret"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "secret"
    assert entry.state.recoverable
    assert hass.data[DOMAIN][entry.entry_id].api.protocol == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_options_check_the_new_credentials(hass, entry, device):
    """Changing the credentials in the options checks them first."""
    device.security_mode = MODE_PIN
    device.pin = "1234"
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"host": entry.data["host"], "pin": "0000"}
    )
    assert result["errors"] == {"base": "invalid_pin"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"host": entry.data["host"], "pin": "1234"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data["pin"] == "1234"
    assert entry.title == HOSTNAME


@pytest.fixture
async def moved(device, socket_enabled):
    """The same device on a second port, as if it had moved; outlasts loaded."""
    async with device.serve() as address:
        yield address


async def test_options_with_a_new_address_connect_to_it(hass, moved, loaded, device):
    """The entry is set up again on the address the options give."""
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"host": moved}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.data[DOMAIN][loaded.entry_id].api.data["host"] == moved
    assert loaded.state is ConfigEntryState.LOADED
    assert len(device.schema_params) == 2
    # The socket to the old address closed.
    await until(lambda: len(device.sockets) == 1)


async def test_options_unchanged_keep_the_connection(hass, loaded, device):
    """Saving the options as they were sets nothing up again."""
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"host": loaded.data["host"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(device.schema_params) == 1
