from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import httpx
import pytest
from alpaca.trading.stream import TradingStream
from websockets.asyncio.server import serve


def trade_update() -> dict:
    return {
        "stream": "trade_updates",
        "data": {
            "event": "new",
            "timestamp": "2026-10-22T16:45:00Z",
            "order": {
                "id": "00000000-0000-0000-0000-000000000001",
                "client_order_id": "bounded-transport-control",
                "created_at": "2026-10-22T16:45:00Z",
                "updated_at": "2026-10-22T16:45:00Z",
                "submitted_at": "2026-10-22T16:45:00Z",
                "order_class": "simple",
                "time_in_force": "day",
                "status": "new",
                "extended_hours": False,
                "symbol": "AAPL",
                "qty": "1",
                "filled_qty": "0",
                "side": "buy",
                "type": "limit",
                "limit_price": "1.01",
            },
        },
    }


@pytest.mark.asyncio
async def test_actual_legacy_trading_stream_reauthenticates_parses_and_closes_after_reconnect():
    connections = 0
    received = []
    finished = asyncio.Event()

    async def peer(socket):
        nonlocal connections
        connections += 1
        assert json.loads(await socket.recv())["action"] == "authenticate"
        await socket.send(json.dumps({"data": {"status": "authorized"}}))
        assert json.loads(await socket.recv())["data"]["streams"] == ["trade_updates"]
        await socket.send(json.dumps(trade_update()))
        if connections == 1:
            await socket.close(code=1012, reason="bounded reconnect control")
        else:
            await socket.wait_closed()

    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = TradingStream(
            "fixture-key", "fixture-secret", url_override=f"ws://127.0.0.1:{port}"
        )

        async def receive(update):
            received.append(
                (
                    str(update.event),
                    update.order.symbol,
                    update.order.qty,
                    update.order.limit_price,
                )
            )
            if len(received) == 2:
                await stream.stop_ws()
                finished.set()

        stream.subscribe_trade_updates(receive)
        task = asyncio.create_task(stream._run_forever())
        try:
            await asyncio.wait_for(finished.wait(), 5)
            await asyncio.wait_for(task, 2)
            assert connections == 2
            assert received == [("new", "AAPL", "1", "1.01")] * 2
            assert stream._ws is None
        finally:
            await stream.stop_ws()
            await stream.close()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_actual_legacy_trading_stream_closes_rejected_auth_before_retry():
    closed = asyncio.Event()
    callbacks = []

    async def peer(socket):
        await socket.recv()
        await socket.send(json.dumps({"data": {"status": "unauthorized"}}))
        await socket.wait_closed()
        closed.set()

    async with serve(peer, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stream = TradingStream(
            "fixture-key", "fixture-secret", url_override=f"ws://127.0.0.1:{port}"
        )

        async def receive(update):
            callbacks.append(update)

        stream.subscribe_trade_updates(receive)
        task = asyncio.create_task(stream._run_forever())
        try:
            await asyncio.wait_for(closed.wait(), 2)
            await stream.stop_ws()
            await asyncio.wait_for(task, 2)
            assert not callbacks
            assert stream._ws is None
        finally:
            await stream.stop_ws()
            await stream.close()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_actual_httpx_anyio_pool_recovers_after_bad_wire_timeout_and_cancellation():
    stalled = asyncio.Event()
    handlers = set()

    async def peer(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            path = request.split(b" ")[1]
            if path == b"/stall":
                stalled.set()
                await reader.read()
            elif path == b"/malformed":
                writer.write(b"INVALID STATUS\r\n\r\n")
                await writer.drain()
            else:
                body = b'{"code":"0","data":[]}'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.remove(task)

    server = await asyncio.start_server(peer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            limits=httpx.Limits(max_connections=1),
            timeout=1,
        ) as client:
            assert (await client.get("/ok")).json() == {"code": "0", "data": []}
            with pytest.raises(httpx.RemoteProtocolError):
                await client.get("/malformed")
            with pytest.raises(httpx.ReadTimeout):
                await client.get("/stall", timeout=0.05)
            stalled.clear()
            request = asyncio.create_task(client.get("/stall"))
            await asyncio.wait_for(stalled.wait(), 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert (await client.get("/ok")).json()["code"] == "0"
        if handlers:
            await asyncio.wait_for(asyncio.gather(*handlers), 1)
        assert not handlers
    finally:
        server.close()
        await server.wait_closed()
