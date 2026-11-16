"""A tiny scripted local HTTP(S) server for real-socket tests."""

from __future__ import annotations

import shutil
import ssl
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

Handler = Callable[["RequestHandler"], None]


class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ScriptedServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _dispatch(self) -> None:
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "client_port": self.client_address[1],
            }
        )
        handler = self.server.handler
        if handler is None:
            self.send_error(500)
            return
        handler(self)

    do_GET = _dispatch
    do_POST = _dispatch
    do_HEAD = _dispatch

    def reply(
        self, status: int, body: bytes = b"", headers: Optional[dict[str, str]] = None, *, length: Optional[int] = None
    ) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class ScriptedServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), RequestHandler)
        self.handler: Optional[Handler] = None
        self.requests: list[dict[str, object]] = []

    @property
    def port(self) -> int:
        return int(self.server_address[1])


@contextmanager
def serve(
    handler: Optional[Handler] = None, *, tls_context: Optional[ssl.SSLContext] = None
) -> Iterator[ScriptedServer]:
    server = ScriptedServer()
    server.handler = handler
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def make_self_signed(tmp: Path, *, cn: str = "localhost") -> Optional[tuple[Path, Path]]:
    """Generate a throwaway self-signed cert with the openssl CLI (None if unavailable)."""
    openssl = shutil.which("openssl")
    if openssl is None:
        return None
    key = tmp / "key.pem"
    cert = tmp / "cert.pem"
    result = subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            f"/CN={cn}",
            "-addext",
            f"subjectAltName=DNS:{cn},IP:127.0.0.1",
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    return cert, key


def tls_server_context(cert: Path, key: Path, *, max_version: Optional[ssl.TLSVersion] = None) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    if max_version is not None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
            ctx.maximum_version = max_version
    return ctx


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="trf-test-"))
