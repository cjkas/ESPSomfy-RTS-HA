"""The update entity: protocol 2's latest release, from GitHub."""

from __future__ import annotations

import aiohttp
from harness import UNIQUE_ID, until
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.components.update import UpdateEntityFeature
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.espsomfy_rts import update
from custom_components.espsomfy_rts.const import DOMAIN


def update_state(hass):
    """The update entity's state."""
    found = er.async_get(hass).async_get_entity_id(
        "update", DOMAIN, f"update_{UNIQUE_ID}"
    )
    assert found is not None
    return hass.states.get(found)


def check_again(hass) -> None:
    """Let RELEASE_CHECK_INTERVAL pass.

    The device's socket times out over that time too: the entity is
    unavailable until it is back.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + update.RELEASE_CHECK_INTERVAL)


def latest(hass) -> str | None:
    """The latest version, None while the entity is unavailable."""
    return update_state(hass).attributes.get("latest_version")


async def test_a_newer_release_is_an_update(hass, loaded, device, hass_ws_client):
    """GitHub's latest release is the latest version, with its page and notes."""
    await until(lambda: update_state(hass).state == STATE_ON)
    state = update_state(hass)
    assert state.attributes["installed_version"] == "v3.0.14"
    assert state.attributes["latest_version"] == "v4.0.1"
    assert state.attributes["release_url"] == (
        "https://github.com/cjkas/ESPSomfy-RTS/releases/tag/v4.0.1"
    )
    # Installed from the device's own page, never from here.
    assert state.attributes["supported_features"] == UpdateEntityFeature.RELEASE_NOTES
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "update/release_notes", "entity_id": state.entity_id}
    )
    reply = await client.receive_json()
    assert reply["success"], reply
    assert reply["result"] == "What's new in v4.0.1"


async def test_the_latest_release_installed_is_up_to_date(hass, entry, device):
    """The same version installed as released: no update."""
    device.release[1]["tag_name"] = "v3.0.14"
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: update_state(hass).state == STATE_OFF)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_latest_is_unknown_until_github_answers(hass, entry, device):
    """A check that fails leaves it unknown; the next one tells."""
    device.release = (503, {"message": "busy"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await until(lambda: device.release_checks == 1)
    await hass.async_block_till_done()
    assert update_state(hass).state == STATE_UNKNOWN
    device.release = (200, {"tag_name": "v4.0.1"})
    check_again(hass)
    await until(lambda: update_state(hass).state == STATE_ON)
    assert latest(hass) == "v4.0.1"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_failed_check_keeps_the_last_release(hass, loaded, device):
    """GitHub not answering doesn't forget the release it told of."""
    await until(lambda: update_state(hass).state == STATE_ON)
    device.release = (500, {})
    check_again(hass)
    await until(
        lambda: device.release_checks == 2
        and update_state(hass).state != STATE_UNAVAILABLE
    )
    await hass.async_block_till_done()
    assert update_state(hass).state == STATE_ON
    assert latest(hass) == "v4.0.1"


async def test_a_later_release_replaces_the_last(hass, loaded, device):
    """Each check takes the release GitHub has now."""
    await until(lambda: update_state(hass).state == STATE_ON)
    device.release = (200, {"tag_name": "v4.1.0"})
    check_again(hass)
    await until(lambda: latest(hass) == "v4.1.0")


async def test_a_release_without_its_page_or_notes(device, host):
    """The page is the tag's when GitHub leaves it out; the notes empty."""
    device.release = (200, {"tag_name": "v4.0.1", "html_url": None, "body": None})
    async with aiohttp.ClientSession() as session:
        release = await update.fetch_latest_release(session)
    assert release == {
        "version": "v4.0.1",
        "url": "https://github.com/cjkas/ESPSomfy-RTS/releases/tag/v4.0.1",
        "notes": "",
    }


@pytest.mark.parametrize(
    "body",
    [[], "v4.0.1", {}, {"tag_name": ""}, {"tag_name": 401}],
    ids=["list", "string", "no tag", "empty tag", "number tag"],
)
async def test_an_answer_without_a_version_is_no_release(device, host, body):
    """Whatever GitHub answers without a tag is no release, not an error."""
    device.release = (200, body)
    async with aiohttp.ClientSession() as session:
        assert await update.fetch_latest_release(session) is None


async def test_github_out_of_reach_is_no_release(monkeypatch, socket_enabled):
    """No connection is no release, not an error."""
    monkeypatch.setattr(update, "LATEST_RELEASE_URL", "http://127.0.0.1:9/")
    async with aiohttp.ClientSession() as session:
        assert await update.fetch_latest_release(session) is None
