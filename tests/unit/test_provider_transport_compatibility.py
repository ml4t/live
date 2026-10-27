from __future__ import annotations

import asyncio
import json
import ssl
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from websockets.asyncio.server import serve

from ml4t.live.brokers.alpaca import TradingStream


@pytest.mark.parametrize("returns_coroutine", [False, True])
def test_trading_subscription_rejects_synchronous_handlers_before_transport(returns_coroutine):
    async def receive(update):
        return update

    def synchronous(update):
        return receive(update) if returns_coroutine else update

    stream = TradingStream("fixture-key", "fixture-secret")
    with pytest.raises(ValueError, match="handler must be a coroutine function"):
        stream.subscribe_trade_updates(synchronous)


@pytest.fixture
def tls_contexts(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "loopback-cert.pem", tmp_path / "loopback-key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_file, key_file)
    client = ssl.create_default_context(cafile=str(cert_file))
    return server, client


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
@pytest.mark.parametrize("secure", [False, True])
async def test_actual_legacy_trading_stream_reauthenticates_parses_and_closes_after_reconnect(
    secure, tls_contexts
):
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

    async with serve(peer, "127.0.0.1", 0, ssl=tls_contexts[0] if secure else None) as server:
        port = server.sockets[0].getsockname()[1]
        stream = TradingStream(
            "fixture-key",
            "fixture-secret",
            url_override=f"{'wss' if secure else 'ws'}://127.0.0.1:{port}",
            websocket_params={"ssl": tls_contexts[1]} if secure else None,
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
@pytest.mark.parametrize("secure", [False, True])
async def test_actual_httpx_anyio_pool_recovers_after_bad_wire_timeout_and_cancellation(
    secure, tls_contexts
):
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

    server = await asyncio.start_server(
        peer, "127.0.0.1", 0, ssl=tls_contexts[0] if secure else None
    )
    port = server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            base_url=f"{'https' if secure else 'http'}://127.0.0.1:{port}",
            trust_env=False,
            verify=tls_contexts[1] if secure else True,
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
