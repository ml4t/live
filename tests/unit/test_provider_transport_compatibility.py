from __future__ import annotations

import asyncio
import gc
import json
import socket
import ssl
import struct
import sys
import tracemalloc
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address

import httpx
import msgpack
import pytest
from alpaca.data.live import StockDataStream
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
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
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Loopback test CA")])
    authority = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, None, None),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(minutes=5))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, None, None),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "loopback-cert.pem", tmp_path / "loopback-key.pem"
    ca_file = tmp_path / "loopback-ca.pem"
    ca_file.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # pip-system-certs injects a client verifier that expects peer certificates on Windows.
    # Only the loopback server needs the underlying native class; clients retain that verifier.
    wrapped = getattr(server, "_ctx", None)
    if wrapped is not None:
        server = type(wrapped)(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_file, key_file)
    client = ssl.create_default_context(cafile=str(ca_file))
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
async def test_alpaca_msgpack_rejects_invalid_map_keys_without_retaining_decoded_values(
    secure, tls_contexts
):
    malformed = msgpack.packb([{42: b"A" * (512 * 1024)}], use_bin_type=True)
    healthy = False
    authentications = []

    async def peer(socket):
        if not healthy:
            await socket.send(malformed)
        else:
            await socket.send(msgpack.packb([{"T": "success", "msg": "connected"}]))
            authentications.append(msgpack.unpackb(await socket.recv())["action"])
            await socket.send(msgpack.packb([{"T": "success", "msg": "authenticated"}]))
        await socket.wait_closed()

    async with serve(peer, "127.0.0.1", 0, ssl=tls_contexts[0] if secure else None) as server:
        port = server.sockets[0].getsockname()[1]
        stream = StockDataStream(
            "fixture-key",
            "fixture-secret",
            url_override=f"{'wss' if secure else 'ws'}://127.0.0.1:{port}",
            websocket_params={"ssl": tls_contexts[1]} if secure else None,
        )

        async def reject_frame():
            try:
                with pytest.raises(ValueError, match="map key.*strict_map_key"):
                    await asyncio.wait_for(stream._connect(), 2)
            finally:
                await asyncio.wait_for(stream.close(), 2)

        await reject_frame()
        already_tracing = tracemalloc.is_tracing()
        if not already_tracing:
            tracemalloc.start()
        try:
            gc.collect()
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(16):
                await reject_frame()
            await asyncio.sleep(0)
            gc.collect()
            retained = tracemalloc.get_traced_memory()[0] - before
            assert retained < 4 * 1024 * 1024, f"malformed frames retained {retained} bytes"
        finally:
            if not already_tracing:
                tracemalloc.stop()

        healthy = True
        try:
            await asyncio.wait_for(stream._connect(), 2)
            await asyncio.wait_for(stream._auth(), 2)
            assert authentications == ["auth"]
        finally:
            await asyncio.wait_for(stream.close(), 2)
        assert stream._ws is None


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
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()
            try:
                with suppress(ConnectionResetError, BrokenPipeError):
                    await writer.wait_closed()
            finally:
                handlers.remove(task)

    server = await asyncio.start_server(
        peer, "127.0.0.1", 0, ssl=tls_contexts[0] if secure else None
    )
    port = server.sockets[0].getsockname()[1]
    try:
        if secure:
            async with httpx.AsyncClient(trust_env=False) as untrusted:
                with pytest.raises(httpx.ConnectError, match="(?i)certificate"):
                    await untrusted.get(f"https://127.0.0.1:{port}")
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
            stalled.clear()
            _, reset_peer = await asyncio.open_connection(
                "127.0.0.1", port, ssl=tls_contexts[1] if secure else None
            )
            reset_peer.write(b"GET /stall HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await reset_peer.drain()
            await asyncio.wait_for(stalled.wait(), 1)
            reset_peer.get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0),
            )
            reset_peer.transport.abort()
            with suppress(ConnectionResetError, BrokenPipeError):
                await reset_peer.wait_closed()
            assert (await client.get("/ok")).json()["code"] == "0"
        if handlers:
            await asyncio.wait_for(asyncio.gather(*handlers), 1)
        assert not handlers
    finally:
        server.close()
        await server.wait_closed()
