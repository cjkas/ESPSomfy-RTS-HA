"""The connection against the fake device: the sync, ops, events, refusals."""

from __future__ import annotations

import asyncio

import aiohttp
from conftest import until
from fake_device import HELLO, MODE_PASSWORD, MODE_PIN, TOKEN, frame
from proto import (
    AuthRequired,
    ConnectionClosed,
    LoginRefused,
    LoginThrottled,
    NoProtocol2,
    ProtoConnection,
    ProtoError,
    VersionMismatch,
    login,
    login_context,
)
import pytest


async def test_connect_returns_with_the_model_synced(connect):
    """connect() returns once the whole sync has come."""
    conn = await connect()
    assert conn.synced
    assert conn.fw == HELLO["d"]["fw"]
    assert len(conn.model.shades) == 10
    # The hash is kept only once the whole schema has come.
    assert conn.schema.hash == HELLO["d"]["sh"]


async def test_an_op_goes_packed_and_resolves_with_its_reply(connect, device):
    """An op is sent with short keys and returns its reply's fields."""
    conn = await connect(run=True)
    reply = await conn.op("shadeCommand", shadeId=1, command="Up", repeat=None)
    assert reply == {"ok": True}
    assert device.ops[-1] == ("shadeCommand", {"shadeId": 1, "command": "Up"})


async def test_a_refused_op_raises_with_the_error_name(connect, device):
    """A refusal raises ProtoError with the schema's name for its code."""
    device.replies["shadeCommand"] = {"code": 5, "desc": "The command queue is full"}
    conn = await connect(run=True)
    with pytest.raises(ProtoError) as err:
        await conn.op("shadeCommand", shadeId=1, command="Up")
    assert err.value.code == "busy"
    assert err.value.desc == "The command queue is full"


async def test_an_op_without_a_reply_times_out(connect, device):
    """An op the device never answers fails as timeout."""
    device.replies["reboot"] = None
    conn = await connect(run=True, op_timeout=0.2)
    with pytest.raises(ProtoError) as err:
        await conn.op("reboot")
    assert err.value.code == "timeout"


async def test_a_long_op_refused_busy_without_its_id_goes_again(connect, device):
    """A long message refused while the device puts another together is resent."""
    device.busy_without_id["saveShade"] = 2
    conn = await connect(run=True)
    reply = await conn.op("saveShade", shadeId=1, name="x" * 1500)
    assert reply == {"ok": True}
    assert device.ops == [("saveShade", {"shadeId": 1, "name": "x" * 1500})]


async def test_a_short_op_is_not_resent_on_a_busy_without_id(connect, device):
    """Only a long message can be the one refused without its id."""
    device.busy_without_id["shadeCommand"] = 1
    conn = await connect(run=True, op_timeout=0.3)
    with pytest.raises(ProtoError) as err:
        await conn.op("shadeCommand", shadeId=1, command="Up")
    assert err.value.code == "timeout"


async def test_an_op_the_device_does_not_have_is_not_sent(connect, device):
    """Unknown ops and fields are refused before anything goes."""
    conn = await connect()
    with pytest.raises(ProtoError) as err:
        await conn.op("selfDestruct")
    assert err.value.code == "unknownOp"
    with pytest.raises(ProtoError) as err:
        await conn.op("shadeCommand", nonsense=1)
    assert err.value.code == "badRequest"
    assert device.ops == []


async def test_an_op_or_run_before_connect_is_not_connected(session, host):
    """Nothing can go before the socket is open and synced."""
    conn = ProtoConnection(session, host)
    with pytest.raises(ProtoError) as err:
        await conn.op("reboot")
    assert err.value.code == "notConnected"
    with pytest.raises(ProtoError) as err:
        await conn.run()
    assert err.value.code == "notConnected"


async def test_events_change_the_model_then_reach_the_listeners(connect, device):
    """A listener sees an event with the model already changed."""
    conn = await connect(run=True)
    seen = []
    conn.add_listener(
        lambda name, d: seen.append((name, d, conn.model.shades[1]["position"]))
    )
    await device.send("shadeState", shadeId=1, position=40, target=40)
    await device.send("memory", freeHeap=1234)
    await until(lambda: len(seen) == 2)
    assert seen[0] == ("shadeState", {"shadeId": 1, "position": 40, "target": 40}, 40)
    assert seen[1][0] == "memory"
    assert conn.model.status["freeHeap"] == 1234


async def test_a_removed_listener_hears_no_more(connect, device):
    """The remover stops the listener, and calling it again does nothing."""
    conn = await connect(run=True)
    seen = []
    remove = conn.add_listener(lambda name, d: seen.append(name))
    remove()
    remove()
    await device.send("memory", freeHeap=1)
    await until(lambda: conn.model.status["freeHeap"] == 1)
    assert seen == []


async def test_an_event_before_the_schema_waits_for_it(connect, device, session, host):
    """A frame that comes before the schema is dispatched once it is readable."""
    device.early.append(frame("shadeState", {"shadeId": 1, "position": 55}))
    conn = ProtoConnection(session, host)
    seen = []
    conn.add_listener(lambda name, d: seen.append(name))
    try:
        await conn.connect()
        assert seen == ["shadeState"]
        # Applied ahead of the sync frame formatted after it, which holds 7.
        assert conn.model.shades[1]["position"] == 7
    finally:
        await conn.close()


async def test_a_schema_kept_from_before_is_not_sent_again(connect, device):
    """The next connection passes the kept schema's hash and gets no schema."""
    first = await connect()
    await first.close()
    again = await connect(schema=first.schema)
    assert device.schema_params == [None, HELLO["d"]["sh"]]
    assert again.schema is first.schema
    assert len(again.model.shades) == 10


async def test_a_schema_for_another_hash_is_dropped(connect, recorded_schema):
    """A kept schema the device no longer has is replaced by the one it sends."""
    recorded_schema.hash = "0ld"
    recorded_schema.types.clear()
    conn = await connect(schema=recorded_schema)
    assert conn.schema is not recorded_schema
    assert conn.schema.type_numbers["reboot"] == 54
    assert conn.schema.hash == HELLO["d"]["sh"]


async def test_a_secured_device_wants_the_token(connect, device):
    """Under a security mode the socket needs a good token."""
    device.security_mode = MODE_PIN
    with pytest.raises(AuthRequired):
        await connect()
    with pytest.raises(AuthRequired):
        await connect(token="stale")
    conn = await connect(token=TOKEN)
    assert conn.synced


@pytest.mark.parametrize("status", [404, 200])
async def test_firmware_without_the_socket_is_not_protocol_2(connect, device, status):
    """Old firmware answers the upgrade with 404, or with a page."""
    device.ws_status = status
    with pytest.raises(NoProtocol2):
        await connect()


async def test_another_refused_upgrade_is_worth_trying_again(connect, device):
    """A device error is not old firmware: the caller tries again."""
    device.ws_status = 500
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await connect()
    assert err.value.status == 500


async def test_a_hello_of_another_version_is_a_mismatch(connect, device):
    """A device on another protocol version is not sent to the old HTTP API."""
    device.hello = {"v": 3, "t": 1, "d": {"fw": "v9.0.0", "sh": "x"}}
    with pytest.raises(VersionMismatch) as err:
        await connect()
    assert err.value.version == 3


async def test_a_socket_closed_first_is_closed_not_old_firmware(connect, device):
    """The device closes a socket at once with every slot taken."""
    device.close_at_once = True
    with pytest.raises(ConnectionClosed):
        await connect()


async def test_a_silent_socket_times_out(connect, device):
    """A socket that opens but never says hello is worth trying again."""
    device.hello = None
    with pytest.raises(TimeoutError):
        await connect(timeout=0.3)


async def test_a_close_ends_run_and_fails_the_ops_waiting(connect, device):
    """When the device closes the socket, run() returns and ops fail as closed."""
    device.replies["reboot"] = None
    conn = await connect()
    task = asyncio.create_task(conn.run())
    op = asyncio.create_task(conn.op("reboot"))
    await until(lambda: device.ops)
    await device.close_sockets()
    await asyncio.wait_for(task, 2)
    with pytest.raises(ProtoError) as err:
        await op
    assert err.value.code == "closed"
    assert not conn.synced


async def test_a_silent_link_times_out_and_closes(connect):
    """A minute of silence (here 0.2 s) ends run() with TimeoutError."""
    conn = await connect()
    with pytest.raises(TimeoutError):
        await conn.run(receive_timeout=0.2)
    assert conn._ws.closed


async def test_login_context_gives_the_security_mode(session, host, device):
    """/loginContext needs no login."""
    device.security_mode = MODE_PIN
    context = await login_context(session, host)
    assert context["type"] == MODE_PIN
    assert context["serverId"] == "serverId 2"


async def test_login_gives_the_token_or_refuses(session, host, device):
    """Under None any login works; under a PIN only the right one."""
    assert await login(session, host) == TOKEN
    device.security_mode = MODE_PIN
    device.pin = "1234"
    assert await login(session, host, pin="1234") == TOKEN
    with pytest.raises(LoginRefused) as err:
        await login(session, host, pin="0000")
    assert err.value.security_mode == MODE_PIN


async def test_a_throttled_login_says_how_long_to_wait(session, host, device):
    """429 isn't refused credentials: it is retried as a device error is."""
    device.security_mode = MODE_PIN
    device.pin = "1234"
    device.throttled_for = 30
    with pytest.raises(LoginThrottled) as err:
        await login(session, host, pin="1234")
    assert err.value.retry_after == 30
    assert isinstance(err.value, aiohttp.ClientError)
    assert not isinstance(err.value, LoginRefused)


@pytest.mark.parametrize(("refusal", "mode"), [("200", MODE_PASSWORD), ("401 text", 0)])
async def test_other_refusals_are_refusals_too(session, host, device, refusal, mode):
    """Firmware before protocol 2 refused with 200; a body may not be JSON."""
    device.security_mode = MODE_PASSWORD
    device.login_refusal = refusal
    with pytest.raises(LoginRefused) as err:
        await login(session, host, username="u", password="wrong")
    assert err.value.security_mode == mode
