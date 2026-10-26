"""The shade model from the recorded sync, and the events that change it."""

from __future__ import annotations


def test_the_sync_fills_in_what_it_leaves_out(model):
    """Items get every field, at its default where the device left it out."""
    assert len(model.shades) == 10
    assert len(model.groups) == 2
    assert len(model.rooms) == 2
    # Shade 2 was at 0, which the device leaves out.
    shade = model.shades[2]
    assert shade["position"] == 0
    assert shade["myPos"] == -1
    assert shade["tiltType"] == 0
    assert shade["paired"] is True
    assert shade["linkedRemotes"][1]["lastRollingCode"] == 0
    assert model.shades[1]["position"] == 7
    # A group's shades are ids.
    assert model.groups[2]["linkedShades"] == [7, 6, 5]
    assert model.groups[2]["sunSensor"] is False
    assert model.header["serverId"] == "serverId 2"
    assert model.header["connType"] == "Wifi"
    assert model.header["fw"] == "v3.0.14"


def test_the_sync_status_is_memory_and_the_link_in_use(model):
    """The status holds the memory and, on WiFi, the WiFi fields only."""
    assert model.status["freeHeap"] == 148760
    assert model.status["loopMaxUs"] == 106737
    assert model.status["strength"] == -52
    assert model.status["channel"] == 6
    assert "speed" not in model.status


def test_a_shade_state_is_merged_with_its_defaults(model):
    """A state left out of shadeState is at its default; the rest is kept."""
    assert model.apply_event("shadeState", {"shadeId": 1, "target": 50, "direction": 1})
    shade = model.shades[1]
    # Left out: back at 0.
    assert shade["position"] == 0
    assert shade["target"] == 50
    assert shade["direction"] == 1
    # Not part of the state: kept.
    assert shade["name"] == "name 7"


def test_a_state_of_a_shade_not_synced_yet_changes_nothing(model):
    """A state waits for its shade."""
    assert not model.apply_event("shadeState", {"shadeId": 99, "position": 5})
    assert 99 not in model.shades


def test_a_whole_shade_replaces_the_one_before(model):
    """The shade event carries the whole shade."""
    assert model.apply_event("shade", {"shadeId": 1, "name": "Office", "shadeType": 3})
    shade = model.shades[1]
    assert shade["name"] == "Office"
    assert shade["shadeType"] == 3
    assert shade["position"] == 0
    assert shade["linkedRemotes"] == []


def test_removed_items_go(model):
    """Removing an id the model doesn't have changes nothing."""
    assert model.apply_event("shadeRemoved", {"shadeId": 3})
    assert 3 not in model.shades
    assert not model.apply_event("shadeRemoved", {"shadeId": 3})
    assert model.apply_event("groupRemoved", {"groupId": 1})
    assert list(model.groups) == [2]


def test_an_order_sets_the_sort_order(model):
    """An order event numbers the items in its order."""
    assert model.apply_event("groupOrder", {"ids": [2, 1]})
    assert model.groups[2]["sortOrder"] == 0
    assert model.groups[1]["sortOrder"] == 1


def test_group_state_memory_and_wifi(model):
    """Group states merge; memory and WiFi go to the status."""
    assert model.apply_event("groupState", {"groupId": 2, "flags": 0x20})
    assert model.groups[2]["flags"] == 0x20
    assert model.groups[2]["name"] == "name 69"
    assert model.apply_event("memory", {"freeHeap": 1000, "uptime": 5})
    assert model.status["freeHeap"] == 1000
    assert model.status["loopRate"] == 0
    assert model.apply_event("wifi", {"ssid": "x", "strength": -60})
    assert model.status["channel"] == 0


def test_an_event_about_something_else_is_not_the_models(model):
    """A frame from the frames feed isn't about the model."""
    assert not model.apply_event("frame", {"remoteAddress": 1})
