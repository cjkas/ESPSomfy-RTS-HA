"""Protocol 2's model and events, in the shape the entities read."""

from __future__ import annotations

import compat


def test_config_is_what_discovery_gave(model):
    """The device, its shades and groups, and its status under the old names."""
    cfg = compat.config(model)
    assert cfg["serverId"] == "serverId 2"
    assert cfg["hostname"] == "hostname 3"
    assert cfg["version"] == "v3.0.14"
    assert cfg["connType"] == "Wifi"
    assert cfg["chipModel"] == "s3"
    assert cfg["checkForUpdate"] is False
    assert [s["shadeId"] for s in cfg["shades"]] == list(range(1, 11))
    # A shade at a default the sync left out has it.
    assert cfg["shades"][1]["position"] == 0
    assert cfg["shades"][0]["tiltType"] == 0
    assert cfg["memory"]["free"] == 148760
    assert cfg["memory"]["loopMaxUs"] == 106737
    assert cfg["ssid"] == "ssid 72"
    assert "speed" not in cfg


def test_a_group_lists_its_shades_not_their_ids(model):
    """The group entity reads each linked shade's id, type and flip."""
    group = compat.config(model)["groups"][1]
    assert group["groupId"] == 2
    assert [s["shadeId"] for s in group["linkedShades"]] == [7, 6, 5]
    assert group["linkedShades"][0] == {
        "shadeId": 7,
        "name": "name 43",
        "shadeType": 0,
        "flipPosition": False,
    }
    # The model keeps the ids.
    assert model.groups[2]["linkedShades"] == [7, 6, 5]


def test_a_shade_state_carries_the_whole_shade(model):
    """A position at its default is still there, which the event left out."""
    assert model.shades[1]["position"] == 7
    d = {"shadeId": 1, "target": 50, "direction": 1}
    model.apply_event("shadeState", d)
    data = compat.event(model, "shadeState", d)
    assert data["event"] == "shadeState"
    assert data["position"] == 0
    assert data["target"] == 50
    assert data["direction"] == 1
    assert data["name"] == "name 7"
    assert data["tiltType"] == 0


def test_a_saved_shade_is_a_shade_state(model):
    """The shade event, after an edit, reaches the shade's entity."""
    d = {"shadeId": 3, "name": "Kitchen", "flipPosition": True}
    model.apply_event("shade", d)
    data = compat.event(model, "shade", d)
    assert data["event"] == "shadeState"
    assert data["flipPosition"] is True
    assert data["name"] == "Kitchen"


def test_a_shade_commanded_is_the_old_shade_command(model):
    """command is cmd, and the fields left out are at their defaults."""
    d = {"shadeId": 2, "command": "Up", "source": "remote"}
    data = compat.event(model, "shadeCommanded", d)
    assert data == {
        "event": "shadeCommand",
        "shadeId": 2,
        "remoteAddress": 0,
        "cmd": "Up",
        "source": "remote",
        "sourceAddress": 0,
    }


def test_status_events_have_the_old_names(model):
    """memory is memStatus, wifi is wifiStrength, fullDuplex is fullduplex."""
    model.apply_event("memory", {"freeHeap": 1000, "maxHeap": 900})
    data = compat.event(model, "memory", {})
    assert data["event"] == "memStatus"
    assert (data["free"], data["max"], data["min"]) == (1000, 900, 0)

    model.apply_event("wifi", {"ssid": "home", "strength": -60, "channel": 11})
    assert compat.event(model, "wifi", {}) == {
        "event": "wifiStrength",
        "ssid": "home",
        "strength": -60,
        "channel": 11,
    }

    model.apply_event("ethernet", {"connected": True, "speed": 100})
    assert compat.event(model, "ethernet", {}) == {
        "event": "ethernet",
        "connected": True,
        "speed": 100,
        "fullduplex": False,
    }


def test_a_group_state_lists_its_shades(model):
    """groupState carries the group as the entities had it."""
    model.apply_event("groupState", {"groupId": 1, "flags": 1})
    data = compat.event(model, "groupState", {"groupId": 1, "flags": 1})
    assert data["event"] == "groupState"
    assert data["flags"] == 1
    assert data["linkedShades"][0]["shadeId"] == 5


def test_removed_and_unknown(model):
    """A removed shade keeps its id; the entities never had the rest."""
    model.apply_event("shadeRemoved", {"shadeId": 4})
    assert compat.event(model, "shadeRemoved", {"shadeId": 4}) == {
        "event": "shadeRemoved",
        "shadeId": 4,
    }
    # No entity reads these.
    assert compat.event(model, "room", {"roomId": 1}) is None
    assert compat.event(model, "groupRemoved", {"groupId": 1}) is None
    # A state for a shade the model hasn't got.
    assert compat.event(model, "shadeState", {"shadeId": 30}) is None


def test_state_events_cover_every_item_and_the_status(model):
    """After a sync the entities get every shade, group and the status."""
    events = compat.state_events(model)
    assert [e["event"] for e in events] == (
        ["shadeState"] * 10 + ["groupState"] * 2 + ["memStatus", "wifiStrength"]
    )
    assert events[0]["position"] == 7
