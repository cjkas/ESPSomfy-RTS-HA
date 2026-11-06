"""Position commands from HomeKit and Home Assistant's cover slider."""

import pytest

from homeassistant.helpers import entity_registry as er
from homeassistant.exceptions import HomeAssistantError

from custom_components.espsomfy_rts.const import DOMAIN
from harness import UNIQUE_ID


def group_entity(hass):
    """The native group from the device's sync."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "cover", DOMAIN, f"{UNIQUE_ID}_group1"
    )
    assert entity_id is not None
    return entity_id


@pytest.mark.parametrize("position,command", [(0, "down"), (100, "up")])
async def test_group_endpoint_uses_one_native_command(
    hass, loaded, device, position, command
):
    """HomeKit endpoints use the group remote, including repeated requests."""
    entity_id = group_entity(hass)
    for _ in range(2):
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": entity_id, "position": position}, blocking=True,
        )
    assert device.ops == [
        ("groupCommand", {"groupId": 1, "command": command}),
        ("groupCommand", {"groupId": 1, "command": command}),
    ]


async def test_group_intermediate_position_keeps_individual_targets(hass, loaded, device):
    """A timed intermediate position still targets each member separately."""
    await hass.services.async_call(
        "cover", "set_cover_position",
        {"entity_id": group_entity(hass), "position": 35}, blocking=True,
    )
    assert len(device.ops) == 10
    assert {fields["shadeId"] for _, fields in device.ops} == set(range(1, 11))
    assert all(name == "shadeCommand" and fields["target"] == 65
               for name, fields in device.ops)


async def test_group_endpoint_reports_a_refused_transmission(hass, loaded, device):
    """A refused native command is surfaced rather than silently fanned out."""
    device.replies["groupCommand"] = {"code": 6, "desc": "Radio TX unavailable"}
    with pytest.raises(HomeAssistantError, match="Radio TX unavailable"):
        await hass.services.async_call(
            "cover", "set_cover_position",
            {"entity_id": group_entity(hass), "position": 100}, blocking=True,
        )
    assert device.ops == [("groupCommand", {"groupId": 1, "command": "up"})]
