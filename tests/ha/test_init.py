"""The integration on protocol 2, and on firmware before it."""

from __future__ import annotations

from unittest.mock import patch

from conftest import UNIQUE_ID, until
from fake_device import MODE_PASSWORD
import pytest

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.espsomfy_rts.const import DOMAIN
from custom_components.espsomfy_rts.controller import ESPSomfyAPI


def entity_id(hass, platform: str, unique_id: str) -> str:
    """An entity's id from its unique id."""
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert found is not None, unique_id
    return found


def cover(hass, shade_id: int):
    """A shade's state."""
    return hass.states.get(entity_id(hass, "cover", f"{UNIQUE_ID}_{shade_id}"))


async def test_setup_takes_the_model_from_the_sync(hass, loaded, device):
    """Every shade and group is an entity, at the state the sync gave."""
    assert loaded.state is ConfigEntryState.LOADED
    assert loaded.title == "hostname 3"
    controller = hass.data[DOMAIN][loaded.entry_id]
    assert controller.api.protocol == 2
    assert len(hass.states.async_all("cover")) == 12
    # The device's position is how far closed; the cover's how far open.
    assert cover(hass, 1).attributes["current_position"] == 93
    assert cover(hass, 2).attributes["current_position"] == 100
    memory = hass.states.get(entity_id(hass, "sensor", f"free_memory_{UNIQUE_ID}"))
    assert memory.state == "148760"
    update = hass.states.get(entity_id(hass, "update", f"update_{UNIQUE_ID}"))
    assert update.attributes["installed_version"] == "v3.0.14"
    assert update.attributes["supported_features"] == 0


async def test_events_reach_the_entities(hass, loaded, device):
    """A shadeState with fields at their defaults left out moves the cover."""
    await device.send("shadeState", shadeId=1, target=50, direction=1)
    await until(lambda: cover(hass, 1).attributes["current_position"] == 100)
    assert cover(hass, 1).state == "closing"


async def test_a_shade_commanded_fires_the_bus_event(hass, loaded, device):
    """A remote's command reaches automations as before."""
    fired = []
    hass.bus.async_listen("espsomfy-rts_event", fired.append)
    await device.send("shadeCommanded", shadeId=2, command="Up", source="remote")
    await until(lambda: fired)
    assert fired[0].data["command"] == "Up"
    assert fired[0].data["source"] == "remote"
    assert fired[0].data["entity_id"] == cover(hass, 2).entity_id


async def test_services_send_ops(hass, loaded, device):
    """A cover's service is the op of the route it used."""
    shade = cover(hass, 1).entity_id
    await hass.services.async_call(
        "cover", "open_cover", {"entity_id": shade}, blocking=True
    )
    await hass.services.async_call(
        "cover",
        "set_cover_position",
        {"entity_id": shade, "position": 40},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "set_sunny", {"entity_id": shade, "sunny": True}, blocking=True
    )
    group = hass.states.get(entity_id(hass, "cover", f"{UNIQUE_ID}_group2"))
    await hass.services.async_call(
        "cover", "stop_cover", {"entity_id": group.entity_id}, blocking=True
    )
    assert device.ops == [
        ("shadeCommand", {"shadeId": 1, "command": "up"}),
        ("shadeCommand", {"shadeId": 1, "target": 60}),
        ("setSensor", {"shadeId": 1, "sunny": 1}),
        ("groupCommand", {"groupId": 2, "command": "my"}),
    ]


async def test_a_refused_op_is_an_error(hass, loaded, device):
    """The device's refusal reaches whoever called the service."""
    device.replies["shadeCommand"] = {"code": 6, "desc": "Radio TX unavailable"}
    with pytest.raises(HomeAssistantError, match="Radio TX unavailable"):
        await hass.services.async_call(
            "cover",
            "open_cover",
            {"entity_id": cover(hass, 1).entity_id},
            blocking=True,
        )


async def test_reboot_is_an_op(hass, loaded, device):
    """The reboot button sends reboot on the socket."""
    button = entity_id(hass, "button", f"reboot_{UNIQUE_ID}")
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    assert device.ops == [("reboot", {})]


async def test_a_lost_socket_comes_back(hass, loaded, device):
    """Entities are unavailable while the socket is down, then catch up."""
    await device.close_sockets()
    await until(lambda: cover(hass, 1).state == STATE_UNAVAILABLE)
    await until(lambda: cover(hass, 1).state != STATE_UNAVAILABLE)
    assert len(device.sockets) == 1
    # The schema wasn't sent again.
    assert device.schema_params[-1] is not None


async def test_secured_setup_logs_in(hass, entry, device):
    """The stored credentials get a token, which the socket and backup take."""
    device.security_mode = MODE_PASSWORD
    device.username, device.password = "admin", "secret"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "username": "admin", "password": "secret"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    with patch.object(hass.config, "config_dir", hass.config.config_dir):
        button = entity_id(hass, "button", f"backup_{UNIQUE_ID}")
        await hass.services.async_call(
            "button", "press", {"entity_id": button}, blocking=True
        )
    assert device.backups == [device.token]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_refused_credentials_ask_for_new_ones(hass, entry, device):
    """Setup with credentials the device refuses starts reauth."""
    device.security_mode = MODE_PASSWORD
    device.username, device.password = "admin", "secret"
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert flows[0]["step_id"] == "credentials"


async def test_a_changed_password_asks_for_new_credentials(hass, entry, device):
    """Refused again on reconnecting, the integration logs in once, then reauths."""
    device.security_mode = MODE_PASSWORD
    device.username, device.password = "admin", "secret"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "username": "admin", "password": "secret"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device.password = "changed"
    device.token = "another-token"
    await device.close_sockets()
    await until(lambda: hass.config_entries.flow.async_progress_by_handler(DOMAIN))
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert cover(hass, 1).state == STATE_UNAVAILABLE
    await hass.config_entries.async_unload(entry.entry_id)


async def test_old_firmware_keeps_the_http_api(hass, entry, device, host):
    """/ws answering 404 means the HTTP API and the old socket, as before."""
    device.legacy = True

    def set_host(api: ESPSomfyAPI, new_host: str) -> None:
        # The fake device serves the API and the old socket on its one port.
        api._host = new_host
        api._api_url = api._config_url = f"http://{new_host}"
        api._sock_url = f"ws://{new_host}/legacy"

    with patch.object(ESPSomfyAPI, "set_host", set_host):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        controller = hass.data[DOMAIN][entry.entry_id]
        assert controller.api.protocol == 1
        assert cover(hass, 1).attributes["current_position"] == 70
        await until(lambda: device.sockets)
        await device.send_legacy("shadeState", shadeId=1, position=100, direction=0)
        await until(lambda: cover(hass, 1).attributes["current_position"] == 0)
        await hass.services.async_call(
            "cover",
            "open_cover",
            {"entity_id": cover(hass, 1).entity_id},
            blocking=True,
        )
        assert device.puts == [("shadeCommand", {"shadeId": 1, "command": "up"})]
        assert device.ops == []
        await hass.config_entries.async_unload(entry.entry_id)
