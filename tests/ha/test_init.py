"""The integration on protocol 2, and on firmware before it."""

from __future__ import annotations

from unittest.mock import patch

from harness import UNIQUE_ID, until
from fake_device import MODE_PASSWORD, SERVER_ID
import pytest

from homeassistant.components import persistent_notification
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.espsomfy_rts import compat, controller as controller_module
from custom_components.espsomfy_rts.const import DOMAIN
from custom_components.espsomfy_rts.controller import ESPSomfyAPI
from custom_components.espsomfy_rts.update import ESPSomfyRTSUpdateEntity


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


async def test_an_event_that_fails_keeps_the_socket(hass, loaded, device, caplog):
    """An error handling one event is logged; the next event still arrives."""
    real = compat.event
    calls = []

    def event(model, name, d):
        calls.append(name)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real(model, name, d)

    with patch.object(compat, "event", event):
        await device.send("shadeState", shadeId=1, target=50, direction=1)
        await until(lambda: calls)
        await device.send("shadeState", shadeId=1, target=50, direction=1)
        await until(lambda: cover(hass, 1).attributes["current_position"] == 100)
    assert "boom" in caplog.text
    # Never connected again.
    assert len(device.schema_params) == 1


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


@pytest.mark.parametrize("tilt_type", [1, 2, 3, 4])
async def test_every_tilt_type_has_a_tilt_position(hass, loaded, device, tilt_type):
    """A shade of any tilt type but none keeps its tilt; 4 is euromode."""
    await device.send("shadeState", shadeId=1, tiltType=tilt_type, tiltPosition=30)
    await until(lambda: cover(hass, 1).attributes.get("current_tilt_position") == 70)


async def test_the_current_tilt_turns_as_the_rest(hass, loaded, device):
    """The device's tilt is how far closed, as its position is."""
    shade = cover(hass, 1).entity_id
    for service, field in (
        ("set_current_position", "position"),
        ("set_tilt_position", "tilt_position"),
        ("set_current_tilt_position", "tilt_position"),
    ):
        await hass.services.async_call(
            DOMAIN, service, {"entity_id": shade, field: 30}, blocking=True
        )
    assert device.ops == [
        ("setPositions", {"shadeId": 1, "position": 70}),
        ("tiltCommand", {"shadeId": 1, "target": 70}),
        ("setPositions", {"shadeId": 1, "tiltPosition": 70}),
    ]


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


async def test_reboot_follows_the_socket_and_backup_stays(hass, loaded, device):
    """Reboot is an op on the socket; Backup is HTTP, so a press always tries.

    HA skips an unavailable button without a word.
    """
    reboot = entity_id(hass, "button", f"reboot_{UNIQUE_ID}")
    backup = entity_id(hass, "button", f"backup_{UNIQUE_ID}")
    await device.close_sockets()
    await until(lambda: cover(hass, 1).state == STATE_UNAVAILABLE)
    assert hass.states.get(reboot).state == STATE_UNAVAILABLE
    assert hass.states.get(backup).state != STATE_UNAVAILABLE
    await until(lambda: hass.states.get(reboot).state != STATE_UNAVAILABLE)


async def press_backup(hass) -> None:
    """Press the Backup button."""
    button = entity_id(hass, "button", f"backup_{UNIQUE_ID}")
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )


def saved_backups(config_dir) -> list:
    """The backups saved under config_dir."""
    return list((config_dir / f"ESPSomfyRTS_{SERVER_ID}").glob("*.backup"))


def backup_notifications(hass) -> list[dict]:
    """The persistent notifications about the device's backup."""
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    return [n for key, n in notifications.items() if key.endswith(SERVER_ID)]


async def backup_fails(hass, error: str) -> None:
    """Pressing Backup raises error, which the notification gives too."""
    with pytest.raises(HomeAssistantError, match=error):
        await press_backup(hass)
    [notification] = backup_notifications(hass)
    assert error in notification["message"]


async def set_up_secured(hass, entry, device) -> None:
    """The entry set up on a device Secured with a password."""
    device.security_mode = MODE_PASSWORD
    device.username, device.password = "admin", "secret"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "username": "admin", "password": "secret"}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ((500, "backup failed"), "answered 500 backup failed"),
        ((200, ""), "sent an empty one"),
    ],
)
async def test_a_backup_the_device_does_not_give_is_an_error(
    hass, loaded, device, tmp_path, reply, error
):
    """Backup says why nothing was saved, rather than nothing."""
    hass.config.config_dir = str(tmp_path)
    device.backup_reply = reply
    with pytest.raises(HomeAssistantError, match=error):
        await press_backup(hass)
    assert saved_backups(tmp_path) == []
    [notification] = backup_notifications(hass)
    assert notification["title"] == "ESPSomfy RTS backup of hostname 3 failed"
    assert error in notification["message"]


async def test_a_failed_backup_stays_noticed_until_one_is_saved(
    hass, loaded, device, tmp_path
):
    """Failures share one notification; the next backup saved dismisses it."""
    hass.config.config_dir = str(tmp_path)
    await press_backup(hass)
    assert backup_notifications(hass) == []
    for status in (500, 503):
        device.backup_reply = (status, "backup failed")
        with pytest.raises(HomeAssistantError):
            await press_backup(hass)
    [notification] = backup_notifications(hass)
    assert "answered 503" in notification["message"]
    device.backup_reply = (200, '{"version": 23, "shades": []}')
    await press_backup(hass)
    assert backup_notifications(hass) == []


async def test_a_backup_asked_again_after_a_login_is_not_asked_a_third_time(
    hass, loaded, device, tmp_path
):
    """A 401 gets one more login, and a second 401 is the error."""
    hass.config.config_dir = str(tmp_path)
    device.backup_reply = (401, "Unauthorized API Key")
    await backup_fails(hass, "answered 401 Unauthorized API Key")
    assert len(device.backups) == 2


async def test_a_backup_whose_login_is_refused_is_an_error(
    hass, entry, device, tmp_path
):
    """Credentials the device no longer takes are the backup's error."""
    await set_up_secured(hass, entry, device)
    hass.config.config_dir = str(tmp_path)
    device.token, device.password = "another-token", "changed"
    await backup_fails(hass, "refused the login")
    assert saved_backups(tmp_path) == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_backup_that_takes_too_long_is_an_error(
    hass, loaded, device, tmp_path
):
    """A device that doesn't answer in time is the backup's error."""
    hass.config.config_dir = str(tmp_path)
    device.backup_delay = 1
    with patch.object(controller_module, "BACKUP_TIMEOUT", 0.05):
        await backup_fails(hass, "TimeoutError")


async def test_an_unexpected_backup_error_is_noticed_too(hass, loaded, tmp_path):
    """An error nobody foresaw still raises HomeAssistantError and notifies."""
    hass.config.config_dir = str(tmp_path)
    with patch.object(
        controller_module, "_write_backup", side_effect=RuntimeError("boom")
    ):
        await backup_fails(hass, "boom")


async def test_a_backup_that_cannot_be_saved_is_an_error(hass, loaded, tmp_path):
    """A backup HA can't write is an error too."""
    taken = tmp_path / "taken"
    taken.write_text("a file, not a directory")
    hass.config.config_dir = str(taken)
    with pytest.raises(HomeAssistantError, match="saving .* failed"):
        await press_backup(hass)


async def test_an_update_asking_for_a_backup_stops_without_one(hass, loaded):
    """Nothing is installed when the backup asked for wasn't saved."""
    controller = hass.data[DOMAIN][loaded.entry_id]
    entity = ESPSomfyRTSUpdateEntity(controller)
    with (
        patch.object(
            controller, "create_backup", side_effect=HomeAssistantError("No backup")
        ),
        patch.object(controller, "update_firmware") as update_firmware,
        pytest.raises(HomeAssistantError, match="No backup"),
    ):
        await entity.async_install(None, True)
    update_firmware.assert_not_called()


async def test_secured_setup_logs_in(hass, entry, device, tmp_path):
    """The stored credentials get a token, which the socket and backup take."""
    await set_up_secured(hass, entry, device)
    hass.config.config_dir = str(tmp_path)
    button = entity_id(hass, "button", f"backup_{UNIQUE_ID}")
    await hass.services.async_call(
        "button", "press", {"entity_id": button}, blocking=True
    )
    assert device.backups == [device.token]
    [backup] = saved_backups(tmp_path)
    assert backup.read_text() == '{"version": 23, "shades": []}'
    # A security save gives the device another token: one more login.
    old, device.token = device.token, "another-token"
    backup.unlink()
    await press_backup(hass)
    assert device.backups[1:] == [old, "another-token"]
    assert len(saved_backups(tmp_path)) == 1
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


async def test_a_throttled_login_is_retried_not_reauthed(hass, entry, device):
    """Setup while the device's login throttle refuses HA waits and tries again."""
    device.security_mode = MODE_PASSWORD
    device.username, device.password = "admin", "secret"
    device.throttled_for = 30
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "username": "admin", "password": "secret"}
    )
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


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


def serve_legacy_on_one_port(api: ESPSomfyAPI, new_host: str) -> None:
    """Point the HTTP API and the old socket at the fake device's one port."""
    api._host = new_host
    api._api_url = api._config_url = f"http://{new_host}"
    api._sock_url = f"ws://{new_host}/legacy"


async def test_the_old_ports_go_on_the_address_without_its_port(hass):
    """A port given with the address is port 80's, not the HTTP API's."""
    api = ESPSomfyAPI(hass, "entry", {"host": "10.0.0.5:8000"})
    assert api.get_api_url() == "http://10.0.0.5:8081"
    assert api.get_sock_url() == "ws://10.0.0.5:8080"
    assert api.get_config_url() == "http://10.0.0.5:8000"
    api = ESPSomfyAPI(hass, "entry", {"host": "espsomfy.local"})
    assert api.get_api_url() == "http://espsomfy.local:8081"


@pytest.mark.parametrize(("permissions", "loads"), [(0, False), (1, True)])
async def test_old_firmware_refusing_the_login(hass, entry, device, permissions, loads):
    """Reauth when old firmware needs the token for commands, not only config."""
    device.legacy = True
    device.security_mode = MODE_PASSWORD
    device.username, device.password = "admin", "secret"
    device.permissions = permissions
    with patch.object(ESPSomfyAPI, "set_host", serve_legacy_on_one_port):
        assert await hass.config_entries.async_setup(entry.entry_id) is loads
        await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ([] if loads else [SOURCE_REAUTH])
    await hass.config_entries.async_unload(entry.entry_id)


async def test_old_firmware_keeps_the_http_api(hass, entry, device, host):
    """/ws answering 404 means the HTTP API and the old socket, as before."""
    device.legacy = True
    with patch.object(ESPSomfyAPI, "set_host", serve_legacy_on_one_port):
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
