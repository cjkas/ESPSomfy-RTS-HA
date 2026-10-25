"""The schema against the one a device sent (tests/fixtures/proto2)."""

from fake_device import SCHEMA
import pytest


def test_the_schema_is_collected_from_every_sync_frame_it_spans(recorded_schema):
    s = recorded_schema
    assert len(s.types) == len(SCHEMA["types"])
    assert len(s.fields) == len(SCHEMA["fields"])
    assert s.type_numbers["shadeCommand"] == 16
    assert s.type_numbers["reboot"] == 54
    assert s.keys["shadeId"] == "s"
    assert s.errors[5] == "busy"
    assert s.feed_numbers["frames"] == 1


def test_short_keys_become_names_and_1_0_booleans_all_the_way_down(recorded_schema):
    s = recorded_schema
    shade = s.expand({"s": 3, "n": "Office", "i": 1, "fp": 0, "lk": [{"a": 5, "c": 9}]})
    assert shade == {
        "shadeId": 3,
        "name": "Office",
        "paired": True,
        "flipPosition": False,
        "linkedRemotes": [{"remoteAddress": 5, "lastRollingCode": 9}],
    }
    # A whole number that happens to be 1 stays a number.
    assert s.expand({"p": 1}) == {"position": 1}


def test_names_become_short_keys_for_an_op(recorded_schema):
    s = recorded_schema
    assert s.pack({"shadeId": 3, "command": "Up", "paired": True, "sunny": False}) == {
        "s": 3,
        "cm": "Up",
        "i": 1,
        "sn": 0,
    }
    assert s.pack({"ids": [3, 1, 2]}) == {"is": [3, 1, 2]}
    # Left out, not sent as null.
    assert s.pack({"shadeId": 3, "target": None}) == {"s": 3}
    with pytest.raises(KeyError, match="no field nonsense"):
        s.pack({"nonsense": 1})


def test_a_field_left_out_is_its_default(recorded_schema):
    s = recorded_schema
    assert s.default("myPos") == -1
    assert s.default("stepSize") == 100
    assert s.default("bitLength") == 56
    assert s.default("paired") is False
    assert s.default("name") == ""
    assert s.default("linkedShades") == []
    assert s.default("status") == {}
    assert s.fill({"shadeId": 2}, ("shadeId", "position", "myPos")) == {
        "shadeId": 2,
        "position": 0,
        "myPos": -1,
    }
