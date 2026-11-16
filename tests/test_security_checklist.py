"""Release-blocking security checklist (see docs/security.md)."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import gzip
import io
import itertools
import json
import logging
import os
import pickle
import socket
import ssl
import subprocess
import sys
import threading
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import (
    CANARY_KEY,
    CANARY_SIG,
    SIGNED_MP3,
    SIGNED_WAV,
    MockTransport,
    Reply,
    client,
    credits_json,
    error_json,
    page_json,
    sync_json,
)
from rightsfoundry._config import build_config
from rightsfoundry._pipeline import parse_json
from rightsfoundry._redact import redact_headers, redact_text, redact_url, redact_value
from rightsfoundry._transport import HTTPTransport, build_ssl_context
from rightsfoundry._validation import validate_track_id
from servers import RequestHandler, make_self_signed, serve, tls_server_context

FORBIDDEN = (CANARY_KEY, CANARY_KEY.upper(), CANARY_SIG)


def assert_clean(text: str) -> None:
    for needle in FORBIDDEN:
        assert needle not in text, "secret leaked"


# --- 1 & 2: canary key and canary sig never leak ----------------------------------------------------------


def test_canary_key_and_sig_never_leak(caplog: pytest.LogCaptureFixture) -> None:
    events: list[Any] = []
    echo = f"server echoed Bearer {CANARY_KEY} and {SIGNED_WAV}"
    t = MockTransport(
        [
            Reply(200, credits_json()),
            Reply(200, page_json()),
            Reply(200, sync_json()),
            Reply(500, error_json(echo, message=echo)),
            Reply(500, error_json(echo)),
            Reply(500, error_json(echo)),
            Reply(302, b"", {"location": SIGNED_WAV}),
            Reply(raises=rf.APIConnectionError(f"boom {CANARY_KEY}", phase="connect")),
            Reply(raises=rf.APIConnectionError(f"boom {CANARY_KEY}", phase="connect")),
            Reply(raises=rf.APIConnectionError(f"boom {CANARY_KEY}", phase="connect")),
        ]
    )
    c = client(
        t,
        on_request=events.append,
        on_response=events.append,
        on_error=events.append,
        hook_include_bodies=True,
        low_credit_warning_threshold=10**9,
        on_low_credit=events.append,
    )
    errors: list[rf.RightsFoundryError] = []
    with caplog.at_level(logging.DEBUG, logger="rightsfoundry"):
        c.credits.retrieve()
        page = c.tracks.list(search="dark", key="A minor")
        link = c.tracks.sync(29446060)
        for _ in range(3):
            try:
                c.tracks.list()
            except rf.RightsFoundryError as exc:
                errors.append(exc)

    # the application still gets the real values
    assert link.download_url == SIGNED_WAV and page.data[0].asset_url == SIGNED_MP3

    blobs = [
        caplog.text,
        repr(events),
        str(events),
        repr(c),
        str(c),
        repr(c.config),
        repr(link),
        str(link),
        repr(page),
        repr(page.data[0]),
        repr(c.config.api_key),
        f"{c.config.api_key}",
        format(c.config.api_key, ">60"),
    ]
    for err in errors:
        blobs += [str(err), repr(err), repr(err.args), json.dumps(err.body, default=str)]
        restored = pickle.loads(pickle.dumps(err))
        assert type(restored) is type(err) and restored.status == err.status
        blobs += [str(restored), repr(restored.__dict__)]
        blobs.append(pickle.dumps(err).decode("latin-1"))
    for e in events:
        if isinstance(e, rf.ErrorEvent):
            blobs.append(str(e.error))
    for blob in blobs:
        assert_clean(blob)
    assert any("sig=[REDACTED]" in b for b in blobs)
    assert any("[REDACTED fp=" in b for b in blobs)
    assert len(errors) == 3


def test_secret_wrapper() -> None:
    s = rf.Secret(CANARY_KEY)
    assert_clean(repr(s) + str(s) + f"{s!s}{s!r}" + f"{s}")
    assert s == rf.Secret(CANARY_KEY) and s != rf.Secret("0" * 128) and s != CANARY_KEY
    with pytest.raises(TypeError):
        pickle.dumps(s)
    with pytest.raises(AttributeError):
        s._value = "x"
    assert copy.deepcopy(s) is s
    assert len(s.fingerprint) == 8 and s.fingerprint in repr(s)


def test_request_headers_container_redacted_in_tracebacks() -> None:
    captured: list[rf.PreparedRequest] = []

    class Capture(MockTransport):
        def send(self, request: rf.PreparedRequest, **kw: float) -> rf.RawResponse:
            captured.append(request)
            return super().send(request, **kw)

    c = client(Capture([Reply(200, page_json())]))
    c.tracks.list()
    assert_clean(repr(captured[0]) + repr(captured[0].headers) + str(captured[0]))
    with pytest.raises(TypeError):
        pickle.dumps(captured[0])


def test_musical_key_is_not_redacted() -> None:
    url = f"https://api.therightsfoundry.com/v1/tracks?key=A+minor&search=dark&sig={CANARY_SIG}"
    out = redact_url(url)
    assert "key=A+minor" in out and "sig=[REDACTED]" in out and CANARY_SIG not in out
    t = MockTransport([Reply(200, page_json())])
    c = client(t)
    page = c.tracks.list(key="A minor")
    assert ("key", "A minor") in t.calls[0].query and page.data[0].musical_key == "A minor"


def test_redactor_patterns() -> None:
    assert redact_text("Bearer abc.def-ghi") == "Bearer [REDACTED]"
    assert redact_text("x " + "F" * 130 + " y") == "x [REDACTED_TOKEN] y"
    assert redact_text("http://u:p@proxy:3128/") == "http://[REDACTED]@proxy:3128/"
    assert redact_headers({"Authorization": "Bearer x", "Proxy-Authorization": "Basic y", "Cookie": "c"}) == {
        "Authorization": "Bearer [REDACTED]",
        "Proxy-Authorization": "[REDACTED]",
    }
    body = redact_value({"data": {"download_url": SIGNED_WAV, "list": [SIGNED_MP3], "peak_data_url": None}})
    assert body["data"]["download_url"].endswith("sig=[REDACTED]") and body["data"]["peak_data_url"] is None


# --- 3: base-URL fuzzing --------------------------------------------------------------------------------------


def _fuzz_urls() -> list[str]:
    schemes = ["https://", "HTTPS://", "http://", "hTtPs://", "https:/", "https:\\\\", "//", ""]
    hosts = [
        "api.therightsfoundry.com",
        "API.THERIGHTSFOUNDRY.COM.",
        "api.therightsfoundry.com.evil.com",
        "evil.com#api.therightsfoundry.com",
        "api.therightsfoundry.com@evil.com",
        "evil.com@api.therightsfoundry.com",
        "evil.com\\@api.therightsfoundry.com",
        "api.therightsfoundry.com\\evil.com",
        "ápi.therightsfoundry.com",
        "xn--pi-dna.therightsfoundry.com",
        "169.254.169.254",
        "2852039166",
        "0xA9FEA9FE",
        "0251.0376.0251.0376",
        "169.254.43518",
        "[::ffff:169.254.169.254]",
        "[fe80::1]",
        "127.0.0.1",
        "localhost",
        "0",
        "0.0.0.0",
        "api.therightsfoundry.com:443",
        "api.therightsfoundry.com:80",
        "api.therightsfoundry.com:",
        "api-staging.therightsfoundry.com",
    ]
    suffixes = ["", "/", "/v1", "?a=b", "#x", "/..", "/%2e%2e"]
    return [s + h + x for s, h, x in itertools.product(schemes, hosts, suffixes)]


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_base_url_fuzz_only_allowlisted_hosts_survive(environment: str) -> None:
    accepted = 0
    for url in _fuzz_urls():
        try:
            c = build_config(api_key=CANARY_KEY, base_url=url, environment=environment, allow_insecure_localhost=True)
        except rf.ConfigurationError as exc:
            assert_clean(str(exc))
            continue
        accepted += 1
        if c.api_host in ("localhost", "127.0.0.1", "::1"):
            # loopback is only reachable with the code-only opt-in, outside production
            assert environment == "staging"
            continue
        assert c.api_scheme == "https"
        assert c.api_host in ("api.therightsfoundry.com", "api-staging.therightsfoundry.com")
        assert c.base_url == f"https://{c.api_host}"
    assert accepted > 0


# --- 4: track-id fuzzing ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        0,
        -1,
        10**15,
        "١٢٣",
        "１２３",
        "²",
        " 123",
        "123 ",
        "+123",
        "-123",
        "12.3",
        "1e3",
        "../1",
        "1/../2",
        "%2e%2e",
        "1%2f2",
        "1?x=1",
        "1#x",
        "0x1F",
        "",
        "1234567890123456",
        "0",
        "000",
        None,
        1.0,
        b"123",
        [1],
    ],
)
def test_track_id_fuzz(value: Any) -> None:
    with pytest.raises(rf.InvalidTrackIdError):
        validate_track_id(value)
    c = client(MockTransport())
    with pytest.raises(rf.ValidationError):
        c.tracks.get(value)
    with pytest.raises(rf.ValidationError):
        c.tracks.sync(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "1"), ("007", "7"), (999_999_999_999_999, "999999999999999"), ("123456789012345", "123456789012345")],
)
def test_track_id_accepted(value: Any, expected: str) -> None:
    assert validate_track_id(value) == expected


# --- 5: oversized bodies & gzip bombs ------------------------------------------------------------------------


def _local(server_port: int, **kw: Any) -> rf.Client:
    c = rf.Client(
        api_key=CANARY_KEY,
        environment="staging",
        base_url=f"http://127.0.0.1:{server_port}",
        allow_insecure_localhost=True,
        max_retries=0,
        **kw,
    )
    return c


def test_declared_oversized_body_aborts_before_reading() -> None:
    def handler(h: RequestHandler) -> None:
        h.reply(200, b"{}", length=50 * 1024 * 1024)
        h.close_connection = True

    with serve(handler) as server:
        c = _local(server.port)
        with pytest.raises(rf.ResponseTooLargeError) as info:
            c.taxonomies.list()
        assert info.value.observed_bytes == 50 * 1024 * 1024
        c.close()


def test_streamed_oversized_body_aborts() -> None:
    def handler(h: RequestHandler) -> None:
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        chunk = b" " * 65536
        try:
            for _ in range(64):
                h.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            h.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass

    with serve(handler) as server:
        c = _local(server.port, max_response_bytes=1024 * 1024)
        with pytest.raises(rf.ResponseTooLargeError):
            c.taxonomies.list()
        c.close()


def test_gzip_bomb_is_bounded_in_memory() -> None:
    bomb = gzip.compress(b"0" * (200 * 1024 * 1024), compresslevel=9)
    assert len(bomb) < 1024 * 1024

    def handler(h: RequestHandler) -> None:
        h.reply(200, bomb, {"Content-Encoding": "gzip", "Content-Type": "application/json"})

    with serve(handler) as server:
        c = _local(server.port, max_response_bytes=10 * 1024 * 1024)
        tracemalloc.start()
        try:
            with pytest.raises(rf.ResponseTooLargeError):
                c.taxonomies.list()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 40 * 1024 * 1024, f"peak {peak} bytes"
        c.close()


def test_invalid_gzip_is_unexpected_response() -> None:
    t = MockTransport([Reply(200, b"not gzip", {"content-encoding": "gzip"})])
    with pytest.raises(rf.UnexpectedResponseError):
        client(t).taxonomies.list()


# --- 6: redirects never followed ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_api_redirect_never_followed(status: int) -> None:
    def handler(h: RequestHandler) -> None:
        h.reply(status, b"", {"Location": "https://evil.example.com/v1/tracks"})

    with serve(handler) as server:
        c = _local(server.port)
        with pytest.raises(rf.UnexpectedRedirectError) as info:
            c.tracks.list()
        assert len(server.requests) == 1 and info.value.location == "https://evil.example.com/v1/tracks"
        c.close()


def test_health_never_sends_token_and_admin_header_never_sent() -> None:
    def handler(h: RequestHandler) -> None:
        h.reply(200, b'{"success":true,"status":"alive"}', {"Content-Type": "application/json"})

    with serve(handler) as server:
        c = _local(server.port)
        assert c.health.live().alive
        c.close()
        headers = server.requests[0]["headers"]
        assert isinstance(headers, dict)
        assert "authorization" not in headers and "x-admin-token" not in headers and "x-api-key" not in headers


# --- 7: TLS ----------------------------------------------------------------------------------------------------------


def test_tls_context_is_strict() -> None:
    ctx = build_ssl_context()
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
    transport = rf.Client(api_key=CANARY_KEY)._transport
    assert isinstance(transport, HTTPTransport)
    assert transport.ssl_context.verify_mode == ssl.CERT_REQUIRED
    transport.close()


def test_tls_immune_to_env_and_monkeypatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONHTTPSVERIFY", "0")
    monkeypatch.setattr(ssl, "_create_default_https_context", ssl._create_unverified_context)
    ctx = build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    made = make_self_signed(tmp_path_factory.mktemp("tls"))
    if made is None:
        pytest.skip("openssl CLI not available")
    return made


def _tls_client(port: int, **kw: Any) -> rf.Client:
    return rf.Client(
        api_key=CANARY_KEY,
        environment="staging",
        base_url=f"https://localhost:{port}",
        allow_insecure_localhost=True,
        **kw,
    )


def _ok(h: RequestHandler) -> None:
    h.reply(200, b'{"success":true,"status":"alive"}', {"Content-Type": "application/json"})


def test_self_signed_certificate_rejected_without_retry(certs: tuple[Path, Path]) -> None:
    cert, key = certs
    with serve(_ok, tls_context=tls_server_context(cert, key)) as server:
        c = _tls_client(server.port, max_retries=5)
        c._pipeline._sleep = lambda _s: pytest.fail("TLS failures must not be retried")
        with pytest.raises(rf.HealthCheckError):
            c.health.live()
        with pytest.raises(rf.TLSVerificationError) as info:
            c.credits.retrieve()
        assert info.value.attempts == 1 and info.value.charge_state == "not_charged"
        assert server.requests == []
        c.close()


def test_wrong_hostname_rejected(tmp_path: Path) -> None:
    made = make_self_signed(tmp_path, cn="other.example")
    if made is None:
        pytest.skip("openssl CLI not available")
    cert, key = made
    with serve(_ok, tls_context=tls_server_context(cert, key)) as server:
        c = _tls_client(server.port, ca_bundle=str(cert))
        with pytest.raises(rf.TLSVerificationError):
            c.credits.retrieve()
        c.close()


def test_trusted_ca_bundle_succeeds(certs: tuple[Path, Path]) -> None:
    cert, key = certs
    with serve(_ok, tls_context=tls_server_context(cert, key)) as server:
        c = _tls_client(server.port, ca_bundle=str(cert))
        assert c.health.live().alive
        c.close()


def test_legacy_tls_server_rejected(certs: tuple[Path, Path]) -> None:
    cert, key = certs
    try:
        ctx = tls_server_context(cert, key, max_version=ssl.TLSVersion.TLSv1_1)
    except (ssl.SSLError, ValueError):
        pytest.skip("this OpenSSL cannot even offer TLS 1.1")
    with serve(_ok, tls_context=ctx) as server:
        c = _tls_client(server.port, ca_bundle=str(cert), max_retries=0)
        with pytest.raises(rf.APIConnectionError):
            c.credits.retrieve()
        c.close()


# --- 8: fork safety ---------------------------------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_fork_discards_pooled_connections() -> None:
    def handler(h: RequestHandler) -> None:
        h.reply(200, b'{"success":true,"status":"alive"}', {"Content-Type": "application/json"})

    with serve(handler) as server:
        c = _local(server.port)
        c.health.live()
        transport = c._transport
        assert isinstance(transport, HTTPTransport)
        assert transport.idle_connection_count() == 1
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # fork() in a threaded process
            pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            os._exit(0 if transport.idle_connection_count() == 0 else 1)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert transport.idle_connection_count() == 1  # the parent keeps its pool
        c.close()


# --- 9: safe JSON --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload", [b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}', b"\xff\xfe", b"{", b"[1,]"]
)
def test_unsafe_json_rejected(payload: bytes) -> None:
    with pytest.raises(rf.UnexpectedResponseError):
        parse_json(payload)


def test_json_nesting_limit() -> None:
    assert parse_json(b"[" * 64 + b"]" * 64) is not None
    with pytest.raises(rf.UnexpectedResponseError, match="64"):
        parse_json(b"[" * 65 + b"]" * 65)
    with pytest.raises(rf.UnexpectedResponseError):
        parse_json(b"[" * 200_000 + b"]" * 200_000)


def test_deep_json_through_pipeline() -> None:
    t = MockTransport([Reply(200, b'{"success":true,"data":' + b"[" * 100 + b"]" * 100 + b"}")])
    with pytest.raises(rf.UnexpectedResponseError):
        client(t).taxonomies.list()


# --- SEC-01: zero runtime dependencies ------------------------------------------------------------


@pytest.mark.skipif(sys.version_info < (3, 10), reason="sys.stdlib_module_names needs 3.10")
def test_imports_only_stdlib() -> None:
    code = (
        "import sys, json; import rightsfoundry, rightsfoundry._cli, rightsfoundry._action;"
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    src = str(Path(rf.__file__).resolve().parents[1])
    out = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {src!r}); " + code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    modules = set(json.loads(out))
    if sys.version_info >= (3, 10):  # also lets mypy check this file on 3.9
        foreign = {
            m for m in modules if m not in sys.stdlib_module_names and not m.startswith("_") and m != "rightsfoundry"
        }
        assert foreign == set()


def test_async_canary(caplog: pytest.LogCaptureFixture) -> None:
    from helpers import AsyncMockTransport, async_client

    async def main() -> None:
        events: list[Any] = []
        t = AsyncMockTransport(
            [Reply(200, sync_json()), Reply(503, error_json(f"oops {CANARY_KEY}"))] + [Reply(503, error_json("x"))] * 2
        )
        c = async_client(t, on_response=events.append, hook_include_bodies=True)
        link = await c.tracks.sync(1)
        assert link.download_url == SIGNED_WAV
        with pytest.raises(rf.ServiceUnavailableError) as info:
            await c.tracks.sync(2)
        assert_clean(repr(events) + str(info.value) + repr(info.value.body) + caplog.text + repr(link))

    with caplog.at_level(logging.DEBUG, logger="rightsfoundry"):
        asyncio.run(main())


def test_cli_output_redacts(capsys: pytest.CaptureFixture[str]) -> None:
    buf = io.StringIO()
    buf.write(redact_text(SIGNED_WAV))
    assert_clean(buf.getvalue())


# --- H1: the key never sits in a traceback frame's locals -------------------------------------------------


def _frames_of(exc: BaseException) -> list[Any]:
    """Every frame reachable from ``exc``: its traceback and its cause/context chain."""
    frames: list[Any] = []
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        tb = current.__traceback__
        while tb is not None:
            frames.append(tb.tb_frame)
            tb = tb.tb_next
        for nxt in (current.__cause__, current.__context__):
            if nxt is not None:
                stack.append(nxt)
    return frames


def _safe_text(value: Any) -> str:
    try:
        return repr(value) + str(value)
    except Exception:  # a broken repr captures nothing either
        return ""


def assert_no_key_in_frame_locals(exc: BaseException) -> None:
    """What an error tracker that records frame locals (Sentry's default) would capture."""
    frames = _frames_of(exc)
    assert frames, "the error carries no traceback"
    for frame in frames:
        for name, value in frame.f_locals.items():
            text = _safe_text(value)
            for needle in FORBIDDEN:
                assert needle not in text, f"{frame.f_code.co_name}.{name} holds the key"
    assert_clean(repr(exc) + str(exc))


@contextlib.contextmanager
def raw_server(script: Any) -> Iterator[int]:
    """A one-connection-at-a-time TCP server driven by ``script(conn)``."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.1)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (OSError, socket.timeout):
                continue
            with conn:
                try:
                    script(conn)
                except OSError:
                    pass

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(5)
        listener.close()


def _read_request(conn: socket.socket) -> None:
    data = b""
    conn.settimeout(5)
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return
        data += chunk


def _local_client(port: int) -> rf.Client:
    return rf.Client(
        api_key=CANARY_KEY,
        environment="staging",
        base_url=f"http://127.0.0.1:{port}",
        allow_insecure_localhost=True,
        max_retries=0,
    )


def test_h1_connect_failure_frames_hold_no_key() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here now
    c = _local_client(port)
    with pytest.raises(rf.APIConnectionError) as info:
        c.credits.retrieve()
    assert info.value.phase == "connect"
    assert_no_key_in_frame_locals(info.value)
    c.close()


@pytest.mark.parametrize("failure", [BrokenPipeError(32, "Broken pipe"), socket.timeout("timed out")])
def test_h1_write_failure_frames_hold_no_key(monkeypatch: pytest.MonkeyPatch, failure: OSError) -> None:
    import http.client

    def boom(self: http.client.HTTPConnection, data: Any) -> None:
        raise failure

    with raw_server(_read_request) as port:
        c = _local_client(port)
        monkeypatch.setattr(http.client.HTTPConnection, "send", boom)
        with pytest.raises(rf.APIConnectionError) as info:
            c.credits.retrieve()
        assert info.value.phase == "write"
        assert info.value.__context__ is None and info.value.__cause__ is None
        assert_no_key_in_frame_locals(info.value)
        c.close()


def test_h1_status_read_failure_frames_hold_no_key() -> None:
    with raw_server(_read_request) as port:  # reads the request, then hangs up without a status line
        c = _local_client(port)
        with pytest.raises(rf.APIConnectionError) as info:
            c.credits.retrieve()
        assert info.value.phase == "read" and info.value.charge_state == "not_charged"  # free endpoint
        assert_no_key_in_frame_locals(info.value)
        c.close()


def test_h1_mid_body_failure_frames_hold_no_key() -> None:
    def truncated(conn: socket.socket) -> None:
        _read_request(conn)
        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{"succ')

    with raw_server(truncated) as port:
        c = _local_client(port)
        with pytest.raises(rf.APIConnectionError) as info:
            c.credits.retrieve()
        assert info.value.phase == "read"
        assert_no_key_in_frame_locals(info.value)
        c.close()


def test_h1_header_loop_locals_are_cleared_even_when_interrupted() -> None:
    """If something unmappable (KeyboardInterrupt) escapes mid-write, the helper frame is clean."""
    from rightsfoundry._redact import _RedactingHeaders
    from rightsfoundry._secret import Secret
    from rightsfoundry._transport import PreparedRequest, _write_request

    class Conn:
        sock = None

        def putrequest(self, *a: Any, **k: Any) -> None:
            pass

        def putheader(self, *a: Any) -> None:
            pass

        def endheaders(self, body: Any = None) -> None:
            raise KeyboardInterrupt

        def close(self) -> None:
            pass

    headers = _RedactingHeaders([("Authorization", f"Bearer {CANARY_KEY}")], Secret(CANARY_KEY))
    request = PreparedRequest("GET", "https://api.therightsfoundry.com/v1/credits", headers)
    with pytest.raises(KeyboardInterrupt) as info:
        _write_request(Conn(), request, "/v1/credits", 5.0)  # type: ignore[arg-type]
    helper = [f for f in _frames_of(info.value) if f.f_code.co_name == "_write_request"]
    assert helper and helper[0].f_locals["name"] is None and helper[0].f_locals["value"] is None
    assert_no_key_in_frame_locals(info.value)


def test_h1_invalid_header_is_a_validation_error_before_any_byte() -> None:
    from rightsfoundry._redact import _RedactingHeaders
    from rightsfoundry._transport import PreparedRequest, _write_request

    class Conn:
        sock = None
        sent = False

        def putrequest(self, *a: Any, **k: Any) -> None:
            pass

        def putheader(self, name: str, value: str) -> None:
            raise ValueError(f"Invalid header value {value!r}")

        def endheaders(self, body: Any = None) -> None:
            self.sent = True

        def close(self) -> None:
            pass

    request = PreparedRequest("GET", "https://x/v1/credits", _RedactingHeaders([("X-Bad", f"Bearer {CANARY_KEY}\n")]))
    failure = _write_request(Conn(), request, "/v1/credits", 5.0)  # type: ignore[arg-type]
    assert isinstance(failure, rf.ValidationError) and failure.charge_state == "not_charged"
    assert_clean(str(failure))
