"""sync_links.download (SEC-13, SEC-18) against a real local HTTP server."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import rightsfoundry as rf
from helpers import CANARY_KEY, CANARY_SIG, FUTURE_EXPIRES, SIGNED_WAV, MockTransport, Reply, track_json
from rightsfoundry._download import sanitize_file_name, validate_asset_url
from servers import RequestHandler, ScriptedServer, serve

PAYLOAD = bytes(range(256)) * 1024  # 256 KiB


def partial_name(name: str = "29446060_track.wav", asset_path: str = "/stream/AB12/29446060/29446060_track.wav") -> str:
    """The hidden resume file: destination name + a hash of the asset path (H7)."""
    import hashlib

    return f".{name}.{hashlib.sha256(asset_path.encode()).hexdigest()[:12]}.rightsfoundry-partial"


PARTIAL = partial_name()


def local_client(server: ScriptedServer, **kw: object) -> rf.Client:
    c = rf.Client(
        api_key=CANARY_KEY,
        environment="staging",
        base_url=f"http://127.0.0.1:{server.port}",
        allow_insecure_localhost=True,
        custom_headers={"X-Team": "video"},
        **kw,  # type: ignore[arg-type]
    )
    c._pipeline._sleep = lambda _s: None
    return c


def link_for(server: ScriptedServer, *, expires: int = FUTURE_EXPIRES, name: str = "29446060_track.wav") -> rf.SyncLink:
    url = f"http://127.0.0.1:{server.port}/stream/AB12/29446060/{name}?expires={expires}&sig={CANARY_SIG}"
    return rf.SyncLink(
        id=29446060,
        title="Golden Hour",
        artist="Ada Lane",
        format="wav",
        file_name=name,
        download_url=url,
        expires_at="2100-01-01T00:00:00.000Z",
        expires_in=3600,
        credits_charged=10,
        reissued=False,
        external_export=False,
    )


def serve_payload(payload: bytes = PAYLOAD) -> object:
    def handler(h: RequestHandler) -> None:
        rng = h.headers.get("Range")
        if rng:
            match = re.fullmatch(r"bytes=(\d+)-", rng)
            assert match is not None
            start = int(match.group(1))
            body = payload[start:]
            h.reply(206, body, {"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"})
            return
        h.reply(200, payload, {"Content-Type": "audio/wav"})

    return handler


@pytest.fixture
def tmp(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path


def leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("."))


def test_download_to_directory_atomic_private_no_auth(tmp: Path) -> None:
    with serve(serve_payload()) as server:  # type: ignore[arg-type]
        c = local_client(server)
        path = c.sync_links.download(link_for(server), tmp)
        assert path == tmp / "29446060_track.wav"
        assert path.read_bytes() == PAYLOAD
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert leftovers(tmp) == []
        headers = server.requests[0]["headers"]
        assert isinstance(headers, dict)
        assert "authorization" not in headers and "x-team" not in headers and "cookie" not in headers
        assert headers["accept-encoding"] == "identity"
        c.close()


def test_download_to_explicit_file_and_overwrite(tmp: Path) -> None:
    target = tmp / "master.wav"
    target.write_bytes(b"old")
    with serve(serve_payload()) as server:  # type: ignore[arg-type]
        c = local_client(server)
        with pytest.raises(rf.ValidationError, match="exists"):
            c.sync_links.download(link_for(server), target)
        assert target.read_bytes() == b"old" and server.requests == []
        c.sync_links.download(link_for(server), target, overwrite=True)
        assert target.read_bytes() == PAYLOAD
        c.close()


def test_trailing_separator_creates_directory(tmp: Path) -> None:
    with serve(serve_payload()) as server:  # type: ignore[arg-type]
        c = local_client(server)
        path = c.sync_links.download(link_for(server), str(tmp / "masters") + os.sep)
        assert path.parent == tmp / "masters" and path.exists()
        c.close()


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="needs symlinks")
def test_symlink_destination_refused(tmp: Path) -> None:
    victim = tmp / "victim.txt"
    victim.write_text("keep")
    link = tmp / "29446060_track.wav"
    link.symlink_to(victim)
    with serve(serve_payload()) as server:  # type: ignore[arg-type]
        c = local_client(server)
        with pytest.raises(rf.ValidationError, match="symlink"):
            c.sync_links.download(link_for(server), tmp, overwrite=True)
        with pytest.raises(rf.ValidationError, match="symlink"):
            c.sync_links.download(link_for(server), link, overwrite=True)
        assert victim.read_text() == "keep"
        c.close()


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32", reason="needs symlinks")
def test_symlinked_partial_file_refused(tmp: Path) -> None:
    victim = tmp / "victim.txt"
    victim.write_text("keep")
    (tmp / PARTIAL).symlink_to(victim)
    with serve(serve_payload()) as server:  # type: ignore[arg-type]
        c = local_client(server)
        with pytest.raises((rf.ValidationError, OSError)):
            c.sync_links.download(link_for(server), tmp, resume=True)
        assert victim.read_text() == "keep"
        c.close()


def test_range_resume_206(tmp: Path) -> None:
    partial = tmp / PARTIAL
    partial.write_bytes(PAYLOAD[:100_000])
    with serve(serve_payload()) as server:  # type: ignore[arg-type]
        c = local_client(server)
        path = c.sync_links.download(link_for(server), tmp, resume=True)
        assert path.read_bytes() == PAYLOAD
        headers = server.requests[0]["headers"]
        assert isinstance(headers, dict) and headers["range"] == "bytes=100000-"
        assert not partial.exists()
        c.close()


def test_resume_when_server_ignores_range(tmp: Path) -> None:
    partial = tmp / PARTIAL
    partial.write_bytes(b"garbage" * 10)

    def handler(h: RequestHandler) -> None:
        h.reply(200, PAYLOAD)

    with serve(handler) as server:
        c = local_client(server)
        assert c.sync_links.download(link_for(server), tmp, resume=True).read_bytes() == PAYLOAD
        c.close()


def test_bad_content_range_rejected(tmp: Path) -> None:
    (tmp / PARTIAL).write_bytes(b"x" * 10)

    def handler(h: RequestHandler) -> None:
        h.reply(206, b"abc", {"Content-Range": "bytes 0-2/3"})  # even after the plain-GET fallback

    with serve(handler) as server:
        c = local_client(server)
        with pytest.raises(rf.SyncLinkError, match="Content-Range"):
            c.sync_links.download(link_for(server), tmp, resume=True)
        c.close()


def test_max_download_bytes_declared_length(tmp: Path) -> None:
    big = b"\0" * (1024 * 1024 + 10)
    with serve(serve_payload(big)) as server:  # type: ignore[arg-type]
        c = local_client(server, max_download_bytes=1024 * 1024)
        with pytest.raises(rf.ResponseTooLargeError):
            c.sync_links.download(link_for(server), tmp)
        assert list(tmp.iterdir()) == []
        c.close()


def test_max_download_bytes_streaming_without_length(tmp: Path) -> None:
    def handler(h: RequestHandler) -> None:
        h.send_response(200)
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        chunk = b"\0" * 65536
        for _ in range(20):
            h.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        h.wfile.write(b"0\r\n\r\n")

    with serve(handler) as server:
        c = local_client(server, max_download_bytes=1024 * 1024)
        with pytest.raises(rf.ResponseTooLargeError):
            c.sync_links.download(link_for(server), tmp)
        assert list(tmp.iterdir()) == []
        c.close()


@pytest.mark.parametrize(
    ("status", "text", "cls"),
    [
        (403, "URL has expired", rf.SyncLinkExpiredError),
        (403, "Invalid signature", rf.SyncLinkInvalidError),
        (400, "Missing signature parameters", rf.SyncLinkInvalidError),
        (404, "File not found", rf.SyncMasterUnavailableError),
    ],
)
def test_asset_error_mapping_not_retried(tmp: Path, status: int, text: str, cls: type[Exception]) -> None:
    def handler(h: RequestHandler) -> None:
        h.reply(status, text.encode(), {"Content-Type": "text/plain"})

    with serve(handler) as server:
        c = local_client(server)
        with pytest.raises(cls):
            c.sync_links.download(link_for(server), tmp)
        assert len(server.requests) == 1 and list(tmp.iterdir()) == []
        c.close()


def test_redirect_never_followed(tmp: Path) -> None:
    def handler(h: RequestHandler) -> None:
        h.reply(302, b"", {"Location": f"https://evil.example.com/steal?sig={CANARY_SIG}"})

    with serve(handler) as server:
        c = local_client(server)
        with pytest.raises(rf.UnexpectedRedirectError) as info:
            c.sync_links.download(link_for(server), tmp)
        assert len(server.requests) == 1
        assert CANARY_SIG not in str(info.value) and "sig=[REDACTED]" in str(info.value)
        c.close()


def test_transient_503_is_retried(tmp: Path) -> None:
    calls = {"n": 0}

    def handler(h: RequestHandler) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            h.reply(503, b"busy")
        else:
            h.reply(200, PAYLOAD)

    with serve(handler) as server:
        c = local_client(server, retry_initial_delay_ms=50, retry_max_delay_ms=50, jitter="none")
        assert c.sync_links.download(link_for(server), tmp).read_bytes() == PAYLOAD
        assert len(server.requests) == 2
        c.close()


def test_truncated_body_detected(tmp: Path) -> None:
    def handler(h: RequestHandler) -> None:
        h.reply(200, b"short", length=50)
        h.close_connection = True

    with serve(handler) as server:
        c = local_client(server, max_retries=0)
        with pytest.raises(rf.APIConnectionError):
            c.sync_links.download(link_for(server), tmp)
        assert list(tmp.iterdir()) == []
        c.close()


def test_preview_download_from_track(tmp: Path) -> None:
    with serve(serve_payload(b"ID3mp3")) as server:  # type: ignore[arg-type]
        c = local_client(server)
        url = f"http://127.0.0.1:{server.port}/stream/AB12/1/1_track.mp3?expires={FUTURE_EXPIRES}&sig={CANARY_SIG}"
        track = rf.Track.from_json(track_json(1, asset_url=url))
        path = c.sync_links.download(track, tmp, kind="preview")
        assert path.name == "1_track.mp3" and path.read_bytes() == b"ID3mp3"
        with pytest.raises(rf.ValidationError):
            c.sync_links.download(track, tmp)  # a Track is not a WAV master
        c.close()


def test_async_download(tmp: Path) -> None:
    with serve(serve_payload()) as server:  # type: ignore[arg-type]

        async def main() -> Path:
            c = rf.AsyncClient(
                api_key=CANARY_KEY,
                environment="staging",
                base_url=f"http://127.0.0.1:{server.port}",
                allow_insecure_localhost=True,
            )
            try:
                return await c.sync_links.download(link_for(server), tmp)
            finally:
                await c.aclose()

        path = asyncio.run(main())
        assert path.read_bytes() == PAYLOAD and leftovers(tmp) == []


def test_unauthenticated_client_can_download(tmp: Path) -> None:
    with serve(serve_payload()) as server:  # type: ignore[arg-type]
        c = rf.Client.unauthenticated(
            environment="staging", base_url=f"http://127.0.0.1:{server.port}", allow_insecure_localhost=True
        )
        assert c.sync_links.download(link_for(server), tmp).exists()
        c.close()


# --- pure URL validation -----------------------------------------------------------------------------


def prod_config() -> rf.ClientConfig:
    return rf.Client(api_key=CANARY_KEY, transport=MockTransport()).config


@pytest.mark.parametrize(
    "url",
    [
        SIGNED_WAV.replace("https://", "http://"),
        SIGNED_WAV.replace("assets.therightsfoundry.com", "assets.therightsfoundry.com.evil.com"),
        SIGNED_WAV.replace("assets.therightsfoundry.com", "api.therightsfoundry.com"),
        SIGNED_WAV.replace("assets.therightsfoundry.com", "assets.therightsfoundry.com:8443"),
        SIGNED_WAV.replace("https://", "https://user:pw@"),
        SIGNED_WAV.replace("/stream/", "/other/"),
        SIGNED_WAV.replace("/stream/AB12/", "/stream/AB12/../../"),
        SIGNED_WAV.replace("/stream/AB12/", "/stream/AB12/%2e%2e/"),
        SIGNED_WAV + "&extra=1",
        SIGNED_WAV + "#frag",
        SIGNED_WAV.replace(f"&sig={CANARY_SIG}", ""),
        SIGNED_WAV.replace(f"sig={CANARY_SIG}", "sig=not-hex"),
        SIGNED_WAV.replace(f"expires={FUTURE_EXPIRES}", "expires=soon"),
        SIGNED_WAV + f"&sig={CANARY_SIG}",
        "https://127.0.0.1/stream/AB12/x.wav?expires=4102444800&sig=" + CANARY_SIG,
        "",
        "not a url",
    ],
)
def test_asset_url_rejections(url: str) -> None:
    with pytest.raises(rf.SyncLinkInvalidError):
        validate_asset_url(url, prod_config())


def test_asset_url_expiry_margin() -> None:
    url = SIGNED_WAV.replace(f"expires={FUTURE_EXPIRES}", "expires=1000")
    with pytest.raises(rf.SyncLinkExpiredError, match="charged again"):
        validate_asset_url(url, prod_config())
    soon = SIGNED_WAV.replace(f"expires={FUTURE_EXPIRES}", "expires=1000")
    with pytest.raises(rf.SyncLinkExpiredError):
        validate_asset_url(soon, prod_config(), now=997.0)
    assert validate_asset_url(SIGNED_WAV, prod_config()) == SIGNED_WAV


def test_validation_happens_before_any_request(tmp: Path) -> None:
    t = MockTransport([Reply(200, b"x")])
    c = rf.Client(api_key=CANARY_KEY, transport=t)
    with pytest.raises(rf.SyncLinkInvalidError):
        c.sync_links.download("https://evil.example.com/stream/a.wav?expires=4102444800&sig=" + CANARY_SIG, tmp)
    assert t.calls == []


def test_mock_transport_download_sends_no_credentials(tmp: Path) -> None:
    t = MockTransport([Reply(200, PAYLOAD)])
    c = rf.Client(api_key=CANARY_KEY, transport=t, custom_headers={"X-Team": "a"})
    c.sync_links.download(SIGNED_WAV, tmp)
    assert set(t.calls[0].headers) == {"user-agent", "accept", "accept-encoding"}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("29446060_track.wav", "29446060_track.wav"),
        ("../../etc/passwd", "passwd"),
        ("dir\\evil.wav", "evil.wav"),
        (".hidden.wav", "fallback.wav"),
        ("bad name.wav", "fallback.wav"),
        ("trés.wav", "fallback.wav"),
        ("..", "fallback.wav"),
        ("", "fallback.wav"),
        (None, "fallback.wav"),
        ("a" * 300, "fallback.wav"),
    ],
)
def test_sanitize_file_name(name: object, expected: str) -> None:
    assert sanitize_file_name(name, "fallback.wav") == expected  # type: ignore[arg-type]


# --- H7: resume is keyed by track and survives a refused Range -------------------------------------------------


def test_h7_resume_into_same_name_for_another_track_starts_fresh(tmp: Path) -> None:
    other_track = partial_name(asset_path="/stream/AB12/11111111/29446060_track.wav")
    (tmp / other_track).write_bytes(b"TRACK-A" * 1000)  # a partial of a *different* master
    with serve(serve_payload()) as server:  # type: ignore[arg-type]
        c = local_client(server)
        path = c.sync_links.download(link_for(server), tmp, resume=True)
        assert path.read_bytes() == PAYLOAD  # not TRACK-A bytes + PAYLOAD
        headers = server.requests[0]["headers"]
        assert isinstance(headers, dict) and "range" not in headers
        assert (tmp / other_track).exists()  # left for its own track
        c.close()


@pytest.mark.parametrize("refusal", [416, 500, 503])
def test_h7_refused_range_falls_back_to_one_full_get(tmp: Path, refusal: int) -> None:
    (tmp / PARTIAL).write_bytes(PAYLOAD)  # already complete: a ranged request is out of range

    def handler(h: RequestHandler) -> None:
        if h.headers.get("Range"):
            h.reply(refusal, b"Range Not Satisfiable" if refusal == 416 else b"upstream error")
        else:
            h.reply(200, PAYLOAD, {"Content-Type": "audio/wav"})

    with serve(handler) as server:
        c = local_client(server, max_retries=0)
        path = c.sync_links.download(link_for(server), tmp, resume=True)
        assert path.read_bytes() == PAYLOAD
        assert len(server.requests) == 2
        first, second = (r["headers"] for r in server.requests)
        assert isinstance(first, dict) and isinstance(second, dict)
        assert first["range"] == f"bytes={len(PAYLOAD)}-" and "range" not in second
        assert not (tmp / PARTIAL).exists()
        c.close()


def test_h7_fallback_happens_once_then_the_error_surfaces(tmp: Path) -> None:
    (tmp / PARTIAL).write_bytes(b"x" * 10)

    def handler(h: RequestHandler) -> None:
        h.reply(500, b"always broken")

    with serve(handler) as server:
        c = local_client(server, max_retries=0)
        with pytest.raises(rf.SyncLinkError, match="500"):
            c.sync_links.download(link_for(server), tmp, resume=True)
        assert len(server.requests) == 2  # ranged, then one plain GET
        c.close()


def test_h7_expired_signature_is_not_retried_without_range(tmp: Path) -> None:
    (tmp / PARTIAL).write_bytes(b"x" * 10)

    def handler(h: RequestHandler) -> None:
        h.reply(403, b"URL has expired")

    with serve(handler) as server:
        c = local_client(server)
        with pytest.raises(rf.SyncLinkExpiredError):
            c.sync_links.download(link_for(server), tmp, resume=True)
        assert len(server.requests) == 1
        c.close()


def test_h7_async_resume_and_fallback(tmp: Path) -> None:
    (tmp / PARTIAL).write_bytes(PAYLOAD[:1000])
    calls = {"n": 0}

    def handler(h: RequestHandler) -> None:
        calls["n"] += 1
        if h.headers.get("Range") and calls["n"] == 1:
            h.reply(502, b"bad gateway")
            return
        serve_payload()(h)  # type: ignore[operator]

    with serve(handler) as server:
        c = rf.AsyncClient(
            api_key=CANARY_KEY,
            environment="staging",
            base_url=f"http://127.0.0.1:{server.port}",
            allow_insecure_localhost=True,
            max_retries=0,
        )
        path = asyncio.run(c.sync_links.download(link_for(server), tmp, resume=True))
        assert path.read_bytes() == PAYLOAD and calls["n"] == 2
        asyncio.run(c.aclose())


def test_h7_mismatched_206_falls_back_to_a_full_get(tmp: Path) -> None:
    (tmp / PARTIAL).write_bytes(b"x" * 10)

    def handler(h: RequestHandler) -> None:
        if h.headers.get("Range"):
            h.reply(206, b"abc", {"Content-Range": "bytes 0-2/3"})  # ignores our offset
        else:
            h.reply(200, PAYLOAD)

    with serve(handler) as server:
        c = local_client(server, max_retries=0)
        assert c.sync_links.download(link_for(server), tmp, resume=True).read_bytes() == PAYLOAD
        c.close()
