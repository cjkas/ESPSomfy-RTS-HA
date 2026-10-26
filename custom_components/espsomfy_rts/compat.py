"""Protocol 2's model and events in the shape the entities read.

The entities were written for the HTTP API's /discovery and the old socket's
events. Rather than change each of them, the protocol 2 transport hands them
the same dicts: the names protocol 2 changed are put back, and an event
carries the whole item from the model, so a field the frame left out at its
default is still there. Nothing here imports Home Assistant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .proto import Model

# The old socket's event names, which the entities filter on.
EVT_SHADESTATE = "shadeState"
EVT_SHADEREMOVED = "shadeRemoved"
EVT_SHADECOMMAND = "shadeCommand"
EVT_GROUPSTATE = "groupState"
EVT_MEMSTATUS = "memStatus"
EVT_WIFISTRENGTH = "wifiStrength"
EVT_ETHERNET = "ethernet"

_COMMANDED = ("shadeId", "remoteAddress", "command", "source", "sourceAddress")


def config(model: Model) -> dict[str, Any]:
    """What /discovery gave: the device, its shades and groups, and its status."""
    header = model.header
    out: dict[str, Any] = {
        "serverId": header["serverId"],
        "model": header["model"],
        "hostname": header["hostname"],
        "chipModel": header["chipModel"],
        "connType": header["connType"],
        "version": header["fw"],
        # Protocol 2 has no update check: the firmware is updated from its
        # own page (the firmware's ADR 0007).
        "checkForUpdate": False,
        "rooms": _ordered(model.rooms),
        "shades": _ordered(model.shades),
        "groups": [group(model, g) for g in _ordered(model.groups)],
        "memory": memory(model),
    }
    if header["connType"] == "Ethernet":
        out.update(ethernet(model))
    else:
        out["ssid"] = model.status["ssid"]
    return out


def group(model: Model, item: dict) -> dict[str, Any]:
    """A group with its linked shades as the shades, not their ids."""
    linked = []
    for shade_id in item["linkedShades"]:
        shade = model.shades.get(shade_id)
        if shade is not None:
            linked.append(
                {
                    "shadeId": shade_id,
                    "name": shade["name"],
                    "shadeType": shade["shadeType"],
                    "flipPosition": shade["flipPosition"],
                }
            )
    return {**item, "linkedShades": linked}


def memory(model: Model) -> dict[str, Any]:
    """The memory status under the old socket's names."""
    s = model.status
    return {
        "free": s["freeHeap"],
        "max": s["maxHeap"],
        "min": s["minHeap"],
        "total": s["totalHeap"],
        "uptime": s["uptime"],
        "loopRate": s["loopRate"],
        "loopMaxUs": s["loopMaxUs"],
    }


def ethernet(model: Model) -> dict[str, Any]:
    """The Ethernet status under the old socket's names."""
    s = model.status
    return {
        "connected": s["connected"],
        "speed": s["speed"],
        "fullduplex": s["fullDuplex"],
    }


def event(model: Model, name: str, d: dict) -> dict[str, Any] | None:
    """The old socket's event for a protocol 2 event the model has applied.

    None for an event the entities never had.
    """
    if name in ("shadeState", "shade"):
        shade = model.shades.get(d.get("shadeId"))
        if shade is None:
            return None
        return {**shade, "event": EVT_SHADESTATE}
    if name == "shadeRemoved":
        return {"event": EVT_SHADEREMOVED, "shadeId": d.get("shadeId")}
    if name in ("groupState", "group"):
        item = model.groups.get(d.get("groupId"))
        if item is None:
            return None
        return {**group(model, item), "event": EVT_GROUPSTATE}
    if name == "shadeCommanded":
        cmd = model.schema.fill(dict(d), _COMMANDED)
        return {
            "event": EVT_SHADECOMMAND,
            "shadeId": cmd["shadeId"],
            "remoteAddress": cmd["remoteAddress"],
            "cmd": cmd["command"],
            "source": cmd["source"],
            "sourceAddress": cmd["sourceAddress"],
        }
    if name == "memory":
        return {**memory(model), "event": EVT_MEMSTATUS}
    if name == "wifi":
        s = model.status
        return {
            "event": EVT_WIFISTRENGTH,
            "ssid": s["ssid"],
            "strength": s["strength"],
            "channel": s["channel"],
        }
    if name == "ethernet":
        return {**ethernet(model), "event": EVT_ETHERNET}
    return None


def state_events(model: Model) -> list[dict[str, Any]]:
    """An event for every shade, group and status, as after a sync.

    The entities catch up on what changed while the socket was down.
    """
    out = [event(model, "shadeState", {"shadeId": i}) for i in model.shades]
    out += [event(model, "groupState", {"groupId": i}) for i in model.groups]
    out.append(event(model, "memory", {}))
    link = "ethernet" if model.header.get("connType") == "Ethernet" else "wifi"
    out.append(event(model, link, {}))
    return out


def _ordered(items: dict[int, dict]) -> list[dict]:
    return sorted(items.values(), key=lambda item: item.get("sortOrder", 0))
