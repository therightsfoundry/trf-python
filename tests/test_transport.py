"""The default http.client transport: pooling, bounds, proxy tunnelling, timeouts."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import CANARY_KEY
from rightsfoundry._transport import HTTPTransport
from servers import RequestHandler, make_self_signed, serve, tls_server_context

ALIVE = b'{"success":true,"status":"alive"}'


def _alive(h: RequestHandler) -> None:
    h.reply(200, ALIVE, {"Content-Type": "application/json"})


def local(port: int, **kw: Any) -> rf.Client:
    return rf.Client(
        api_key=CANARY_KEY,
        environment="staging",
        base_url=f"http://127.0.0.1:{port}",
        allow_insecure_localhost=True,
        **kw,
    )


def test_keep_alive_connection_is_reused() -> None:
    with serve(_alive) as server:
        c = local(server.port)
        for _ in range(5):
            assert c.health.live().alive
        ports = {r["client_port"] for r in server.requests}
        assert len(server.requests) == 5 and len(ports) == 1
        transport = c._transport
        assert isinstance(transport, HTTPTransport) and transport.idle_connection_count() == 1
        c.close()
        assert transport.idle_connection_count() == 0


def test_error_bodies_are_drained_and_connection_reused() -> None:
    calls = {"n": 0}

    def handler(h: RequestHandler) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            h.reply(404, b'{"success":false,"error":"Track not found"}', {"Content-Type": "application/json"})
        else:
            h.reply(200, b'{"success":true,"data":{}}', {"Content-Type": "application/json"})

    with serve(handler) as server:
        c = local(server.port)
        with pytest.raises(rf.TrackNotFoundError):
            c.tracks.get(1)
        c.taxonomies.list()
        assert len({r["client_port"] for r in server.requests}) == 1
        c.close()


def test_server_closed_idle_connection_is_detected() -> None:
    def handler(h: RequestHandler) -> None:
        h.reply(200, ALIVE, {"Content-Type": "application/json", "Connection": "close"})
        h.close_connection = True

    with serve(handler) as server:
        c = local(server.port)
        assert c.health.live().alive
        time.sleep(0.05)
        assert c.health.live().alive
        assert len(server.requests) == 2
        c.close()


def test_closed_transport_refuses_requests() -> None:
    with serve(_alive) as server:
        c = local(server.port, max_retries=0)
        c.close()
        with pytest.raises(rf.HealthCheckError):
            c.health.live()


def test_pool_bounds_concurrency() -> None:
    active = 0
    peak = 0
    lock = threading.Lock()

    def handler(h: RequestHandler) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        _alive(h)

    with serve(handler) as server:
        c = local(server.port, max_connections=2)
        threads = [threading.Thread(target=c.health.live) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert peak <= 2 and len(server.requests) == 6
        c.close()


def test_connect_refused_is_connect_phase_and_retried() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    c = local(port, max_retries=2)
    delays: list[float] = []
    c._pipeline._sleep = delays.append
    with pytest.raises(rf.APIConnectionError) as info:
        c.credits.retrieve()
    assert info.value.phase == "connect" and info.value.charge_state == "not_charged"
    assert info.value.attempts == 3 and len(delays) == 2


def test_read_timeout_is_read_phase() -> None:
    def handler(h: RequestHandler) -> None:
        time.sleep(1.5)
        _alive(h)

    with serve(handler) as server:
        c = local(server.port, read_timeout=1.0, max_retries=0)
        with pytest.raises(rf.APITimeoutError) as info:
            c.tracks.list()
        assert info.value.phase == "read" and info.value.charge_state == "unknown"
        c.close()


# --- HTTP CONNECT proxy ----------------------------------------------------------------------------------


@contextmanager
def connect_proxy(target_port: int, *, require_auth: str | None = None) -> Iterator[dict[str, Any]]:
    """A minimal CONNECT proxy that tunnels every request to 127.0.0.1:target_port."""
    state: dict[str, Any] = {"connects": [], "port": 0}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    state["port"] = listener.getsockname()[1]
    stop = threading.Event()

    def pipe(a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def handle(client: socket.socket) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            head += chunk
        text = head.decode("latin-1")
        state["connects"].append(text)
        if require_auth is not None and f"Proxy-Authorization: Basic {require_auth}" not in text:
            client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            client.close()
            return
        upstream = socket.create_connection(("127.0.0.1", target_port))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        t1 = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
        t2 = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
        t1.start()
        t2.start()
        t1.join(10)
        t2.join(10)
        client.close()
        upstream.close()

    def accept_loop() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        stop.set()
        thread.join(5)
        listener.close()


@pytest.fixture(scope="module")
def gateway_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    made = make_self_signed(tmp_path_factory.mktemp("gw"), cn="gw.example.test")
    if made is None:
        pytest.skip("openssl CLI not available")
    return made


def test_https_through_connect_proxy_with_credentials(gateway_cert: tuple[Path, Path]) -> None:
    cert, key = gateway_cert
    import base64

    creds = base64.b64encode(b"user:s3cret-pw").decode()
    with (
        serve(_alive, tls_context=tls_server_context(cert, key)) as server,
        connect_proxy(server.port, require_auth=creds) as proxy,
    ):
        c = rf.Client(
            api_key=CANARY_KEY,
            base_url="https://gw.example.test:8443",
            allowed_hosts=["gw.example.test"],
            ca_bundle=str(cert),
            proxy_url=f"http://user:s3cret-pw@127.0.0.1:{proxy['port']}",
        )
        assert c.health.live().alive
        connect = proxy["connects"][0]
        assert connect.startswith("CONNECT gw.example.test:8443 HTTP/1.")  # 3.9 sends HTTP/1.0
        headers = server.requests[0]["headers"]
        assert isinstance(headers, dict)
        assert "proxy-authorization" not in headers and headers["host"] == "gw.example.test:8443"
        assert "s3cret" not in repr(c) and "s3cret" not in repr(c.config)
        c.close()


def test_proxy_auth_failure_is_redacted_connect_error(gateway_cert: tuple[Path, Path]) -> None:
    cert, key = gateway_cert
    with (
        serve(_alive, tls_context=tls_server_context(cert, key)) as server,
        connect_proxy(server.port, require_auth="never-matches") as proxy,
    ):
        c = rf.Client(
            api_key=CANARY_KEY,
            base_url="https://gw.example.test",
            allowed_hosts=["gw.example.test"],
            ca_bundle=str(cert),
            proxy_url=f"http://user:s3cret-pw@127.0.0.1:{proxy['port']}",
            max_retries=0,
        )
        with pytest.raises(rf.APIConnectionError) as info:
            c.credits.retrieve()
        assert info.value.phase == "connect" and "s3cret" not in str(info.value)
        assert server.requests == []
        c.close()
        del info
        # CPython 3.9's http.client._tunnel() leaves its status-line reader open after a
        # failed CONNECT (the SDK has already closed the connection). Collect it here.
        import gc
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            gc.collect()


# --- https:// proxies: TLS to the proxy, CONNECT, TLS to the origin inside it ------------------------------


@contextmanager
def tls_connect_proxy(
    target_port: int, context: Any, *, require_auth: str | None = None, reply: bytes | None = None
) -> Iterator[dict[str, Any]]:
    """A CONNECT proxy that itself speaks TLS (an ``https://`` proxy)."""
    import select
    import ssl

    state: dict[str, Any] = {"connects": [], "port": 0}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    state["port"] = listener.getsockname()[1]
    stop = threading.Event()

    def relay(a: Any, b: socket.socket) -> None:
        socks = [a, b]
        while True:
            readable, _, _ = select.select(socks, [], [], 5)
            if not readable:
                return
            for s in readable:
                try:
                    data = s.recv(65536)
                except ssl.SSLWantReadError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                (b if s is a else a).sendall(data)
            while a.pending():
                b.sendall(a.recv(a.pending()))

    def handle(raw: socket.socket) -> None:
        try:
            client = context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            head += chunk
        text = head.decode("latin-1")
        state["connects"].append(text)
        if reply is not None:
            client.sendall(reply)
            client.close()
            return
        if require_auth is not None and f"Proxy-Authorization: Basic {require_auth}" not in text:
            client.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            client.close()
            return
        upstream = socket.create_connection(("127.0.0.1", target_port))
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            relay(client, upstream)
        finally:
            client.close()
            upstream.close()

    def accept_loop() -> None:
        listener.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        stop.set()
        thread.join(5)
        listener.close()


def test_https_proxy_tunnels_tls_inside_tls(gateway_cert: tuple[Path, Path]) -> None:
    import base64

    cert, key = gateway_cert
    creds = base64.b64encode(b"user:s3cret-pw").decode()
    ctx = tls_server_context(cert, key)
    with (
        serve(_alive, tls_context=tls_server_context(cert, key)) as server,
        tls_connect_proxy(server.port, ctx, require_auth=creds) as proxy,
    ):
        c = rf.Client(
            api_key=CANARY_KEY,
            base_url="https://gw.example.test:8443",
            allowed_hosts=["gw.example.test"],
            ca_bundle=str(cert),
            proxy_url=f"https://user:s3cret-pw@127.0.0.1:{proxy['port']}",
        )
        assert c.config.proxy is not None and c.config.proxy.scheme == "https"
        for _ in range(3):  # pooled TLS-in-TLS connection is reused
            assert c.health.live().alive
        assert len(proxy["connects"]) == 1
        assert proxy["connects"][0].startswith("CONNECT gw.example.test:8443 HTTP/1.1")
        headers = server.requests[0]["headers"]
        assert isinstance(headers, dict) and "proxy-authorization" not in headers
        assert "s3cret" not in repr(c.config.proxy)
        c.close()


def test_https_proxy_refusal_and_bad_replies_are_connect_errors(gateway_cert: tuple[Path, Path]) -> None:
    cert, key = gateway_cert
    ctx = tls_server_context(cert, key)
    replies = [
        None,  # 407 from require_auth
        b"HTTP/1.1 200 OK\r\n\r\nsurprise",  # data before the tunnel exists
        b"garbage\r\n\r\n",
    ]
    for reply in replies:
        with (
            serve(_alive, tls_context=tls_server_context(cert, key)) as server,
            tls_connect_proxy(server.port, ctx, require_auth="never", reply=reply) as proxy,
        ):
            c = rf.Client(
                api_key=CANARY_KEY,
                base_url="https://gw.example.test",
                allowed_hosts=["gw.example.test"],
                ca_bundle=str(cert),
                proxy_url=f"https://user:s3cret-pw@127.0.0.1:{proxy['port']}",
                max_retries=0,
            )
            with pytest.raises(rf.APIConnectionError) as info:
                c.credits.retrieve()
            assert info.value.phase == "connect" and "s3cret" not in str(info.value)
            assert server.requests == []
            c.close()


def test_https_proxy_with_an_untrusted_certificate_is_refused(
    gateway_cert: tuple[Path, Path], tmp_path_factory: pytest.TempPathFactory
) -> None:
    other = make_self_signed(tmp_path_factory.mktemp("other"), cn="other.example.test")
    if other is None:
        pytest.skip("openssl CLI not available")
    cert, key = gateway_cert
    with (
        serve(_alive, tls_context=tls_server_context(cert, key)) as server,
        tls_connect_proxy(server.port, tls_server_context(*other)) as proxy,
    ):
        c = rf.Client(
            api_key=CANARY_KEY,
            base_url="https://gw.example.test",
            allowed_hosts=["gw.example.test"],
            ca_bundle=str(cert),
            proxy_url=f"https://127.0.0.1:{proxy['port']}",
            max_retries=0,
        )
        with pytest.raises(rf.TLSVerificationError):
            c.credits.retrieve()
        c.close()


def test_https_proxy_closing_during_connect(gateway_cert: tuple[Path, Path]) -> None:
    cert, key = gateway_cert
    with tls_connect_proxy(1, tls_server_context(cert, key), reply=b"") as proxy:
        c = rf.Client(
            api_key=CANARY_KEY,
            base_url="https://gw.example.test",
            allowed_hosts=["gw.example.test"],
            ca_bundle=str(cert),
            proxy_url=f"https://127.0.0.1:{proxy['port']}",
            max_retries=0,
        )
        with pytest.raises(rf.APIConnectionError):
            c.credits.retrieve()
        c.close()


# --- failure paths of the default transport ---------------------------------------------------------------


def _request(port: int, path: str = "/v1/health/live") -> Any:
    from rightsfoundry._redact import _RedactingHeaders
    from rightsfoundry._transport import PreparedRequest

    return PreparedRequest("GET", f"http://127.0.0.1:{port}{path}", _RedactingHeaders([("Accept", "*/*")]))


def test_pool_exhaustion_times_out_without_sending() -> None:
    with serve(_alive) as server:
        t = HTTPTransport(max_connections=1, allow_plain_http_loopback=True)
        held = t.send(_request(server.port), connect_timeout=1, read_timeout=1, write_timeout=1)
        with pytest.raises(rf.APITimeoutError) as info:
            t.send(_request(server.port), connect_timeout=0.05, read_timeout=0.05, write_timeout=0.05)
        assert info.value.phase == "connect" and info.value.charge_state == "not_charged"
        held.close()  # unread body: the socket is discarded, the slot released
        assert t.idle_connection_count() == 0
        again = t.send(_request(server.port), connect_timeout=1, read_timeout=1, write_timeout=1)
        assert b"".join(again.iter_bytes()) == ALIVE
        t.close()


def test_closed_transport_and_plain_http_rules() -> None:
    t = HTTPTransport(allow_plain_http_loopback=False)
    with pytest.raises(rf.APIConnectionError, match="loopback"):
        t.send(_request(1), connect_timeout=1, read_timeout=1, write_timeout=1)
    t.close()
    with pytest.raises(rf.APIConnectionError, match="closed"):
        t.send(_request(1), connect_timeout=1, read_timeout=1, write_timeout=1)


def test_status_and_body_read_timeouts() -> None:
    def slow_status(h: RequestHandler) -> None:
        time.sleep(0.6)
        try:
            _alive(h)
        except OSError:  # the client gave up first, as intended
            pass

    def slow_body(h: RequestHandler) -> None:
        h.send_response(200)
        h.send_header("Content-Length", "100")
        h.end_headers()
        h.wfile.write(b"{")
        h.wfile.flush()
        time.sleep(0.6)
        h.close_connection = True

    t = HTTPTransport(allow_plain_http_loopback=True)
    with serve(slow_status) as server:
        with pytest.raises(rf.APITimeoutError) as info:
            t.send(_request(server.port), connect_timeout=1, read_timeout=0.2, write_timeout=1)
        assert info.value.phase == "read" and info.value.charge_state == "unknown"
    with serve(slow_body) as server:
        raw = t.send(_request(server.port), connect_timeout=1, read_timeout=0.2, write_timeout=1)
        with pytest.raises(rf.APITimeoutError, match="between body chunks"):
            b"".join(raw.iter_bytes())
        raw.close()
    t.close()


def test_connect_timeout_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    def hang(self: http.client.HTTPConnection) -> None:
        raise socket.timeout("timed out")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", hang)
    t = HTTPTransport(allow_plain_http_loopback=True)
    with pytest.raises(rf.APITimeoutError) as info:
        t.send(_request(1), connect_timeout=0.1, read_timeout=1, write_timeout=1)
    assert info.value.phase == "connect" and info.value.charge_state == "not_charged"


def test_unmappable_errors_propagate_and_release_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    import http.client

    class Boom(Exception):
        pass

    def fail(self: http.client.HTTPConnection) -> None:
        raise Boom

    monkeypatch.setattr(http.client.HTTPConnection, "connect", fail)
    t = HTTPTransport(max_connections=1, allow_plain_http_loopback=True)
    for _ in range(2):  # a leaked slot would make the second call time out instead
        with pytest.raises(Boom):
            t.send(_request(1), connect_timeout=0.1, read_timeout=0.1, write_timeout=0.1)


def test_fork_reset_discards_pooled_sockets() -> None:
    from rightsfoundry._transport import _after_fork_in_child

    with serve(_alive) as server:
        t = HTTPTransport(allow_plain_http_loopback=True)
        b"".join(t.send(_request(server.port), connect_timeout=1, read_timeout=1, write_timeout=1).iter_bytes())
        assert t.idle_connection_count() == 1
        held = t.send(_request(server.port), connect_timeout=1, read_timeout=1, write_timeout=1)
        _after_fork_in_child()  # what os.register_at_fork runs in a child
        assert t.idle_connection_count() == 0
        b"".join(held.iter_bytes())  # an old-generation connection is closed, not pooled
        assert t.idle_connection_count() == 0
        b"".join(t.send(_request(server.port), connect_timeout=1, read_timeout=1, write_timeout=1).iter_bytes())
        assert t.idle_connection_count() == 1
        t.__del__()
        assert t.idle_connection_count() == 0


def test_stale_and_dropped_idle_connections_are_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    from rightsfoundry import _transport

    with serve(_alive) as server:
        t = HTTPTransport(allow_plain_http_loopback=True)
        b"".join(t.send(_request(server.port), connect_timeout=1, read_timeout=1, write_timeout=1).iter_bytes())
        monkeypatch.setattr(_transport, "_IDLE_TTL_SECONDS", 0.0)
        b"".join(t.send(_request(server.port), connect_timeout=1, read_timeout=1, write_timeout=1).iter_bytes())
        ports = {r["client_port"] for r in server.requests}
        assert len(ports) == 2  # the expired idle socket was replaced
        t.close()
    assert HTTPTransport._dropped(type("C", (), {"sock": None})()) is True


def test_async_executor_discards_a_response_after_cancellation() -> None:
    import asyncio

    from rightsfoundry._transport import AsyncExecutorTransport, RawResponse

    closed: list[bool] = []
    started = threading.Event()
    release = threading.Event()

    class Slow:
        def send(self, request: Any, **_: float) -> RawResponse:
            started.set()
            release.wait(5)
            return RawResponse(200, {}, b"x", on_close=lambda: closed.append(True))

    wrapper = AsyncExecutorTransport(Slow())

    async def main() -> None:
        task = asyncio.ensure_future(wrapper.send(_request(1), connect_timeout=1, read_timeout=1, write_timeout=1))
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(main())
    for _ in range(50):
        if closed:
            break
        time.sleep(0.02)
    assert closed == [True]
    wrapper.close()
