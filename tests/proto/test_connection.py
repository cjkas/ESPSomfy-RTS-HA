"""The connection against the fake device: the sync, ops, events, refusals."""

import asyncio

from fake_device import HELLO, TOKEN, frame
from proto import (
    SECURITY_PIN,
    AuthRequired,
    ConnectionClosed,
    LoginRefused,
    NoProtocol2,
    ProtoConnection,
    ProtoError,
    login,
    login_context,
)
import pytest


async def connected(session, host, **kwargs) -> ProtoConnection:
    conn = ProtoConnection(session, host, **kwargs)
    await conn.connect()
    return conn


async def test_connect_returns_with_the_model_synced(session, host):
    conn = await connected(session, host)
    assert conn.synced
    assert conn.fw == HELLO["d"]["fw"]
    assert len(conn.model.shades) == 10
    # The hash is kept only once the whole schema has come.
    assert conn.schema.hash == HELLO["d"]["sh"]
    await conn.close()


async def test_an_op_goes_packed_and_resolves_with_its_reply(session, host, device):
    conn = await connected(session, host)
    task = asyncio.create_task(conn.run())
    reply = await conn.op("shadeCommand", shadeId=1, command="Up", repeat=None)
    assert reply == {"ok": True}
    assert device.ops[-1] == ("shadeCommand", {"shadeId": 1, "command": "Up"})
    await conn.close()
    await task


async def test_a_refused_op_raises_with_the_error_name(session, host, device):
    device.replies["shadeCommand"] = {"code": 5, "desc": "The command queue is full"}
    conn = await connected(session, host)
    task = asyncio.create_task(conn.run())
    with pytest.raises(ProtoError) as err:
        await conn.op("shadeCommand", shadeId=1, command="Up")
    assert err.value.code == "busy"
    assert err.value.desc == "The command queue is full"
    await conn.close()
    await task


async def test_an_op_without_a_reply_times_out(session, host, device):
    device.replies["reboot"] = None
    conn = await connected(session, host, op_timeout=0.2)
    task = asyncio.create_task(conn.run())
    with pytest.raises(ProtoError) as err:
        await conn.op("reboot")
    assert err.value.code == "timeout"
    await conn.close()
    await task


async def test_an_op_the_device_does_not_have_is_not_sent(session, host, device):
    conn = await connected(session, host)
    with pytest.raises(ProtoError) as err:
        await conn.op("selfDestruct")
    assert err.value.code == "unknownOp"
    with pytest.raises(ProtoError) as err:
        await conn.op("shadeCommand", nonsense=1)
    assert err.value.code == "badRequest"
    assert device.ops == []
    await conn.close()


async def test_an_op_before_connect_is_not_connected(session, host):
    conn = ProtoConnection(session, host)
    with pytest.raises(ProtoError) as err:
        await conn.op("reboot")
    assert err.value.code == "notConnected"


async def test_events_change_the_model_then_reach_the_listeners(session, host, device):
    conn = await connected(session, host)
    seen = []
    conn.add_listener(
        lambda name, d: seen.append((name, d, conn.model.shades[1]["position"]))
    )
    task = asyncio.create_task(conn.run())
    await device.send("shadeState", shadeId=1, position=40, target=40)
    await device.send("memory", freeHeap=1234)
    await asyncio.sleep(0.1)
    assert seen[0] == ("shadeState", {"shadeId": 1, "position": 40, "target": 40}, 40)
    assert seen[1][0] == "memory"
    assert conn.model.status["freeHeap"] == 1234
    await conn.close()
    await task


async def test_an_event_before_the_schema_waits_for_it(session, host, device):
    device.early.append(frame("shadeState", {"shadeId": 1, "position": 55}))
    conn = ProtoConnection(session, host)
    seen = []
    conn.add_listener(lambda name, d: seen.append(name))
    await conn.connect()
    assert seen == ["shadeState"]
    # Applied ahead of the sync frame formatted after it, which holds 7.
    assert conn.model.shades[1]["position"] == 7
    await conn.close()


async def test_a_schema_kept_from_before_is_not_sent_again(session, host, device):
    first = await connected(session, host)
    await first.close()
    again = await connected(session, host, schema=first.schema)
    assert device.schema_params == [None, HELLO["d"]["sh"]]
    assert again.schema is first.schema
    assert len(again.model.shades) == 10
    await again.close()


async def test_a_schema_for_another_hash_is_dropped(session, host, recorded_schema):
    recorded_schema.hash = "0ld"
    recorded_schema.types.clear()
    conn = await connected(session, host, schema=recorded_schema)
    assert conn.schema is not recorded_schema
    assert conn.schema.type_numbers["reboot"] == 54
    assert conn.schema.hash == HELLO["d"]["sh"]
    await conn.close()


async def test_a_secured_device_wants_the_token(session, host, device):
    device.security = SECURITY_PIN
    with pytest.raises(AuthRequired):
        await connected(session, host)
    with pytest.raises(AuthRequired):
        await connected(session, host, token="stale")
    conn = await connected(session, host, token=TOKEN)
    assert conn.synced
    await conn.close()


async def test_firmware_without_the_socket_is_not_protocol_2(session, host, device):
    device.ws_status = 404
    with pytest.raises(NoProtocol2):
        await connected(session, host)


async def test_a_socket_closed_first_is_closed_not_old_firmware(session, host, device):
    # As the device does with every slot taken.
    async def close_at_once(ws, skip_schema):
        await ws.close()

    device._serve = close_at_once
    with pytest.raises(ConnectionClosed):
        await connected(session, host)


async def test_a_close_ends_run_and_fails_the_ops_waiting(session, host, device):
    device.replies["reboot"] = None
    conn = await connected(session, host)
    task = asyncio.create_task(conn.run())
    op = asyncio.create_task(conn.op("reboot"))
    await asyncio.sleep(0.1)
    await device.close_sockets()
    await asyncio.wait_for(task, 2)
    with pytest.raises(ProtoError) as err:
        await op
    assert err.value.code == "closed"
    assert not conn.synced


async def test_a_silent_link_times_out(session, host):
    conn = await connected(session, host)
    with pytest.raises(TimeoutError):
        await conn.run(receive_timeout=0.2)
    await conn.close()


async def test_login_context_says_how_the_device_is_secured(session, host, device):
    device.security = SECURITY_PIN
    context = await login_context(session, host)
    assert context["type"] == SECURITY_PIN
    assert context["serverId"] == "A1B2C3"


async def test_login_gives_the_token_or_refuses(session, host, device):
    assert await login(session, host) == TOKEN
    device.security = SECURITY_PIN
    device.pin = "1234"
    assert await login(session, host, pin="1234") == TOKEN
    with pytest.raises(LoginRefused) as err:
        await login(session, host, pin="0000")
    assert err.value.security_type == SECURITY_PIN


async def test_a_refusal_from_older_firmware_is_a_refusal_too(session, host, device):
    from aiohttp import web

    async def refuse(request):
        return web.json_response({"type": 2, "success": False})

    app = web.Application()
    app.router.add_put("/login", refuse)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        with pytest.raises(LoginRefused) as err:
            await login(session, f"127.0.0.1:{runner.addresses[0][1]}", password="x")
        assert err.value.security_type == 2
    finally:
        await runner.cleanup()
