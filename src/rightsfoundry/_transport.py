"""Transports: the only code that opens sockets (SEC-12/15/16/19, ``docs/security.md``).

* :class:`HTTPTransport` — the default. Standard-library ``http.client`` with a
  bounded, lock-guarded, per-``(scheme, host, port)`` idle pool, a strict
  ``ssl.SSLContext`` (TLS 1.2+, ``CERT_REQUIRED``, hostname checking; immune to
  ``PYTHONHTTPSVERIFY=0`` and to monkey-patched default contexts), separate
  connect / write / per-chunk read timeouts, optional ``http://`` or ``https://``
  CONNECT proxying, and
  fork safety (pooled sockets are discarded in forked children).
* :class:`AsyncExecutorTransport` — wraps a sync transport in a bounded
  ``ThreadPoolExecutor`` so :class:`~rightsfoundry.AsyncClient` shares one
  audited HTTP parser and TLS stack. ``asyncio.CancelledError`` propagates; the
  in-flight thread finishes in the background and its response is discarded.

Transports never follow redirects, never decompress and never enforce size
limits — the pipeline does all of that, so injected transports get the same
protection.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import http.client
import io
import os
import select
import socket
import ssl
import threading
import time
import weakref
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, Union, runtime_checkable
from urllib.parse import urlsplit

from ._errors import (
    APIConnectionError,
    APITimeoutError,
    RightsFoundryError,
    TLSVerificationError,
    ValidationError,
)
from ._redact import _RedactingHeaders, redact_text, redact_url
from ._secret import _reveal

if TYPE_CHECKING:
    from ._config import ProxyConfig

__all__ = [
    "AsyncExecutorTransport",
    "AsyncRawResponse",
    "AsyncTransport",
    "HTTPTransport",
    "Headers",
    "PreparedRequest",
    "RawResponse",
    "Transport",
    "build_ssl_context",
]

CHUNK_SIZE = 64 * 1024
_IDLE_TTL_SECONDS = 25.0
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


class Headers:
    """A small case-insensitive, order-preserving, multi-value header collection."""

    __slots__ = ("_items",)

    def __init__(self, items: Union[Iterable[tuple[str, str]], dict[str, str], None] = None) -> None:
        if items is None:
            self._items: list[tuple[str, str]] = []
        elif isinstance(items, dict):
            self._items = [(str(k), str(v)) for k, v in items.items()]
        else:
            self._items = [(str(k), str(v)) for k, v in items]

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        lname = name.lower()
        for k, v in self._items:
            if k.lower() == lname:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        lname = name.lower()
        return [v for k, v in self._items if k.lower() == lname]

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def lower_dict(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in self._items:
            out.setdefault(k.lower(), v)
        return out

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(list(self._items))

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"Headers({len(self._items)} headers)"


class PreparedRequest:
    """A fully built request handed to a transport. Its ``repr`` is redacted.

    ``headers`` is a redacting container: iterate it to send, but ``repr`` shows
    ``Authorization: Bearer [REDACTED fp=…]``.
    """

    __slots__ = ("body", "headers", "method", "url")

    def __init__(self, method: str, url: str, headers: _RedactingHeaders, body: Optional[bytes] = None) -> None:
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body

    def __repr__(self) -> str:
        return f"PreparedRequest({self.method} {redact_url(self.url)}, headers={self.headers!r})"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("PreparedRequest cannot be pickled")


class RawResponse:
    """What a sync transport returns: status, headers and an iterator of body chunks.

    Build one in a custom transport with ``RawResponse(status, headers, body)``
    where ``body`` is ``bytes`` or an iterable of ``bytes`` chunks.
    """

    def __init__(
        self,
        status: int,
        headers: Union[Headers, Iterable[tuple[str, str]], dict[str, str]],
        body: Union[bytes, Iterable[bytes]] = b"",
        *,
        reason: str = "",
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        self.status = int(status)
        self.headers = headers if isinstance(headers, Headers) else Headers(headers)
        self.reason = reason
        self._body = body
        self._on_close = on_close
        self._closed = False

    def iter_bytes(self) -> Iterator[bytes]:
        body = self._body
        if isinstance(body, (bytes, bytearray)):
            for i in range(0, len(body), CHUNK_SIZE):
                yield bytes(body[i : i + CHUNK_SIZE])
            return
        for chunk in body:
            if chunk:
                yield bytes(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_body = getattr(self._body, "close", None)
        if callable(close_body):
            close_body()
        if self._on_close is not None:
            self._on_close()

    def __repr__(self) -> str:
        return f"RawResponse(status={self.status})"


class AsyncRawResponse:
    """What an async transport returns: status, headers and an async iterator of chunks."""

    def __init__(
        self,
        status: int,
        headers: Union[Headers, Iterable[tuple[str, str]], dict[str, str]],
        body: Union[bytes, AsyncIterable[bytes], Iterable[bytes]] = b"",
        *,
        reason: str = "",
        on_close: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.status = int(status)
        self.headers = headers if isinstance(headers, Headers) else Headers(headers)
        self.reason = reason
        self._body = body
        self._on_close = on_close
        self._closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        body = self._body
        if isinstance(body, (bytes, bytearray)):
            for i in range(0, len(body), CHUNK_SIZE):
                yield bytes(body[i : i + CHUNK_SIZE])
            return
        if isinstance(body, AsyncIterable):
            async for chunk in body:
                if chunk:
                    yield bytes(chunk)
            return
        for chunk in body:
            if chunk:
                yield bytes(chunk)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._on_close is not None:
            result = self._on_close()
            if asyncio.iscoroutine(result):
                await result

    def __repr__(self) -> str:
        return f"AsyncRawResponse(status={self.status})"


@runtime_checkable
class Transport(Protocol):
    """Sync transport interface. MUST NOT follow redirects."""

    def send(
        self,
        request: PreparedRequest,
        *,
        connect_timeout: float,
        read_timeout: float,
        write_timeout: float,
    ) -> RawResponse: ...


@runtime_checkable
class AsyncTransport(Protocol):
    """Async transport interface. MUST NOT follow redirects."""

    async def send(
        self,
        request: PreparedRequest,
        *,
        connect_timeout: float,
        read_timeout: float,
        write_timeout: float,
    ) -> AsyncRawResponse: ...


# --- TLS ------------------------------------------------------------------------------


def build_ssl_context(ca_bundle: Optional[str] = None) -> ssl.SSLContext:
    """SEC-15: TLS 1.2+, certificate and hostname verification, no bypass.

    Built explicitly (not via ``ssl._create_default_https_context``), so neither
    ``PYTHONHTTPSVERIFY=0`` nor a monkey-patched default context affects it.
    ``ca_bundle`` *replaces* the system trust store with the given PEM bundle (for
    a gateway behind a private CA); verification stays on.
    """
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_bundle)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    if ctx.verify_mode != ssl.CERT_REQUIRED or not ctx.check_hostname:  # pragma: no cover
        raise RuntimeError("rightsfoundry: TLS verification could not be enabled")
    if hasattr(ssl, "OP_NO_COMPRESSION"):
        ctx.options |= ssl.OP_NO_COMPRESSION
    return ctx


# --- fork safety -----------------------------------------------------------------------

_LIVE_TRANSPORTS: weakref.WeakSet[HTTPTransport] = weakref.WeakSet()


def _after_fork_in_child() -> None:
    for transport in list(_LIVE_TRANSPORTS):
        transport._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (socket.timeout, TimeoutError))


def _describe(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")


class _PooledConnection:
    __slots__ = ("conn", "generation", "idle_since", "key")

    def __init__(self, conn: http.client.HTTPConnection, key: tuple[str, str, int], generation: int) -> None:
        self.conn = conn
        self.key = key
        self.generation = generation
        self.idle_since = time.monotonic()


class HTTPTransport:
    """Default zero-dependency transport over ``http.client``."""

    def __init__(
        self,
        *,
        max_connections: int = 10,
        ca_bundle: Optional[str] = None,
        proxy: Optional[ProxyConfig] = None,
        allow_plain_http_loopback: bool = False,
    ) -> None:
        self.max_connections = max_connections
        self._ssl_context = build_ssl_context(ca_bundle)
        self._proxy = proxy
        self._allow_plain_http_loopback = allow_plain_http_loopback
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str, int], list[_PooledConnection]] = {}
        self._slots = threading.BoundedSemaphore(max_connections)
        self._generation = 0
        self._closed = False
        _LIVE_TRANSPORTS.add(self)

    @property
    def ssl_context(self) -> ssl.SSLContext:
        return self._ssl_context

    def idle_connection_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._idle.values())

    def _reset_after_fork(self) -> None:
        # The parent's locks may be held and its sockets are shared with the
        # parent; never touch either. Drop references and start a fresh pool.
        self._lock = threading.Lock()
        self._idle = {}
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._generation += 1

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pools, self._idle = self._idle, {}
        for entries in pools.values():
            for entry in entries:
                entry.conn.close()

    def __del__(self) -> None:
        # Close idle sockets of a transport that was never closed explicitly, so
        # integrators do not see ResourceWarnings from abandoned clients.
        try:
            pools = self._idle
            self._idle = {}
            for entries in pools.values():
                for entry in entries:
                    entry.conn.close()
        except Exception:  # noqa: BLE001, S110 - never raise from a finalizer
            pass

    # --- pool ------------------------------------------------------------------------

    @staticmethod
    def _dropped(conn: http.client.HTTPConnection) -> bool:
        sock = conn.sock
        if sock is None:
            return True
        try:
            readable, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            return True
        return bool(readable)

    def _checkout(self, key: tuple[str, str, int]) -> Optional[_PooledConnection]:
        now = time.monotonic()
        stale: list[_PooledConnection] = []
        found: Optional[_PooledConnection] = None
        with self._lock:
            entries = self._idle.get(key, [])
            while entries:
                entry = entries.pop()
                if (
                    entry.generation == self._generation
                    and now - entry.idle_since < _IDLE_TTL_SECONDS
                    and not self._dropped(entry.conn)
                ):
                    found = entry
                    break
                stale.append(entry)
        for entry in stale:
            entry.conn.close()
        return found

    def _checkin(self, entry: _PooledConnection) -> None:
        with self._lock:
            if self._closed or entry.generation != self._generation:
                keep = False
            else:
                entries = self._idle.setdefault(entry.key, [])
                keep = len(entries) < self.max_connections
                if keep:
                    entry.idle_since = time.monotonic()
                    entries.append(entry)
        if not keep:
            entry.conn.close()

    def _new_connection(self, scheme: str, host: str, port: int, connect_timeout: float) -> http.client.HTTPConnection:
        proxy = self._proxy
        use_proxy = proxy is not None and host not in _LOOPBACK and proxy.applies_to(host)
        if scheme == "https":
            if use_proxy and proxy is not None:
                tunnel_headers: dict[str, str] = {}
                if proxy.auth is not None:
                    tunnel_headers["Proxy-Authorization"] = "Basic " + _reveal(proxy.auth)
                if proxy.scheme == "https":
                    return _TLSProxyConnection(
                        proxy.host,
                        proxy.port,
                        tunnel_host=host,
                        tunnel_port=port,
                        tunnel_headers=tunnel_headers,
                        timeout=connect_timeout,
                        context=self._ssl_context,
                    )
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    proxy.host,
                    proxy.port,
                    timeout=connect_timeout,
                    context=self._ssl_context,
                )
                conn.set_tunnel(host, port, headers=tunnel_headers)
                return conn
            return http.client.HTTPSConnection(host, port, timeout=connect_timeout, context=self._ssl_context)
        if not (self._allow_plain_http_loopback and host in _LOOPBACK):
            raise APIConnectionError(
                "plain http is only allowed to a loopback host with allow_insecure_localhost=True",
                phase="connect",
                charge_state="not_charged",
            )
        return http.client.HTTPConnection(host, port, timeout=connect_timeout)

    # --- send ------------------------------------------------------------------------

    def send(
        self,
        request: PreparedRequest,
        *,
        connect_timeout: float,
        read_timeout: float,
        write_timeout: float,
    ) -> RawResponse:
        if self._closed:
            raise APIConnectionError("the client has been closed", phase="connect", charge_state="not_charged")
        parts = urlsplit(request.url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if scheme == "https" else 80)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        key = (scheme, host, port)

        if not self._slots.acquire(timeout=connect_timeout + read_timeout):
            raise APITimeoutError(
                "timed out waiting for a free connection in the pool (max_connections)",
                phase="connect",
                charge_state="not_charged",
            )
        slots = self._slots
        generation = self._generation
        # Every failure below is mapped to an SDK error and raised *outside* the
        # ``except`` block that caught it, so the raised error has no ``__context__``
        # chain into http.client frames (whose locals hold header values, SEC-05).
        failure: Optional[BaseException] = None
        resp: Optional[http.client.HTTPResponse] = None
        entry: Optional[_PooledConnection] = None
        try:
            entry = self._checkout(key)
            if entry is None:
                conn = self._new_connection(scheme, host, port, connect_timeout)
                entry = _PooledConnection(conn, key, generation)
                failure = _connect(conn, host, connect_timeout)
            if failure is None:
                failure = _write_request(entry.conn, request, target, write_timeout)
            if failure is None:
                resp, failure = _read_response(entry.conn, read_timeout)
        except BaseException:
            slots.release()
            raise
        if failure is not None or resp is None:
            slots.release()
            raise failure or APIConnectionError("no response", phase="read", charge_state="unknown")
        return _HTTPResponse(self, entry, resp, slots, read_timeout)


def _io_failure(
    exc: BaseException, *, phase: str, timeout_message: str, message: str, charge_state: str
) -> Optional[RightsFoundryError]:
    """Map a socket/http.client failure to an SDK error; ``None`` = not ours, re-raise it."""
    if _is_timeout(exc):
        return APITimeoutError(timeout_message, phase=phase, charge_state=charge_state)
    if isinstance(exc, (OSError, http.client.HTTPException, ValueError)):
        return APIConnectionError(f"{message}: {_describe(exc)}", phase=phase, charge_state=charge_state)
    return None


def _connect(conn: http.client.HTTPConnection, host: str, connect_timeout: float) -> Optional[RightsFoundryError]:
    failure: Optional[RightsFoundryError] = None
    try:
        conn.connect()
    except ssl.SSLCertVerificationError as exc:
        failure = TLSVerificationError(
            f"TLS certificate verification failed for {host}: {redact_text(str(exc.verify_message or exc))}",
            phase="connect",
        )
    except BaseException as exc:
        failure = _io_failure(
            exc,
            phase="connect",
            timeout_message=f"connect timed out after {connect_timeout:g}s to {host}",
            message=f"could not connect to {host}",
            charge_state="not_charged",
        )
        if failure is None:
            conn.close()
            raise
    if failure is not None:
        conn.close()
    return failure


def _write_request(
    conn: http.client.HTTPConnection, request: PreparedRequest, target: str, write_timeout: float
) -> Optional[RightsFoundryError]:
    """Write the request line, headers and body (H1: no header value outlives this frame).

    The header loop binds each ``(name, value)`` pair, the last of which is the
    ``Authorization`` value. Both names are cleared in ``finally`` so a frame-locals
    capture (Sentry and friends record every frame of a traceback) never sees the key.
    """
    failure: Optional[RightsFoundryError] = None
    name: Optional[str] = None
    value: Optional[str] = None
    sent = False
    sock = conn.sock
    if sock is not None:
        sock.settimeout(write_timeout)
    try:
        conn.putrequest(request.method, target, skip_accept_encoding=True)
        for name, value in request.headers:
            conn.putheader(name, value)
        name = value = None
        if request.body is not None:
            conn.putheader("Content-Length", str(len(request.body)))
        sent = True
        conn.endheaders(request.body)
    except BaseException as exc:
        name = value = None
        conn.close()
        if not sent and isinstance(exc, ValueError):
            # http.client refused a header before a byte left the process.
            return ValidationError(f"the request could not be encoded: {_describe(exc)}")
        failure = _io_failure(
            exc,
            phase="write",
            timeout_message=f"write timed out after {write_timeout:g}s",
            message="connection failed while sending the request",
            charge_state="unknown",
        )
        if failure is None:
            raise
    finally:
        name = value = None
    return failure


def _read_response(
    conn: http.client.HTTPConnection, read_timeout: float
) -> tuple[Optional[http.client.HTTPResponse], Optional[RightsFoundryError]]:
    if conn.sock is not None:
        conn.sock.settimeout(read_timeout)
    try:
        return conn.getresponse(), None
    except BaseException as exc:
        conn.close()
        failure = _io_failure(
            exc,
            phase="read",
            timeout_message=f"read timed out after {read_timeout:g}s waiting for the response",
            message="connection failed while reading the response",
            charge_state="unknown",
        )
        if failure is None:
            raise
    return None, failure


# --- https:// proxies: TLS to the proxy, CONNECT, then TLS to the origin inside it ------------

_MAX_CONNECT_RESPONSE = 16 * 1024


class _TLSInTLSSocket:
    """A TLS session carried inside another TLS socket (``ssl.SSLObject`` over memory BIOs).

    Implements what ``http.client`` uses: ``sendall``, ``makefile("rb")``,
    ``settimeout``, ``close`` and ``fileno`` (for the idle-pool liveness check).
    Certificate and hostname verification use the transport's strict context.
    """

    def __init__(self, outer: ssl.SSLSocket, context: ssl.SSLContext, server_hostname: str) -> None:
        self._outer = outer
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._tls = context.wrap_bio(self._incoming, self._outgoing, server_hostname=server_hostname)
        self._closed = False
        self._run(self._tls.do_handshake)

    def _flush(self) -> None:
        data = self._outgoing.read()
        if data:
            self._outer.sendall(data)

    def _run(self, op: Callable[[], Any]) -> Any:
        while True:
            try:
                result = op()
            except ssl.SSLWantReadError:
                self._flush()
                data = self._outer.recv(CHUNK_SIZE)
                if data:
                    self._incoming.write(data)
                else:
                    self._incoming.write_eof()
                continue
            self._flush()
            return result

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = int(self._run(functools.partial(self._tls.write, view)))
            view = view[written:]

    def recv_into(self, buffer: Any) -> int:
        try:
            return int(self._run(functools.partial(self._tls.read, len(buffer), buffer)))
        except ssl.SSLZeroReturnError:
            return 0

    def makefile(self, mode: str = "rb", *args: Any, **kwargs: Any) -> io.BufferedReader:
        return io.BufferedReader(_TLSInTLSReader(self), CHUNK_SIZE)

    def settimeout(self, value: Optional[float]) -> None:
        self._outer.settimeout(value)

    def fileno(self) -> int:
        return self._outer.fileno()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._outer.close()


class _TLSInTLSReader(io.RawIOBase):
    def __init__(self, sock: _TLSInTLSSocket) -> None:
        super().__init__()
        self._sock = sock

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        return self._sock.recv_into(buffer)


class _TLSProxyConnection(http.client.HTTPSConnection):
    """``https://`` proxy: verified TLS to the proxy, ``CONNECT``, verified TLS to the origin.

    ``Proxy-Authorization`` travels only inside the first TLS session and only on
    the ``CONNECT`` request; the origin never sees it.
    """

    def __init__(
        self,
        proxy_host: str,
        proxy_port: int,
        *,
        tunnel_host: str,
        tunnel_port: int,
        tunnel_headers: dict[str, str],
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(proxy_host, proxy_port, timeout=timeout, context=context)
        self._origin = (tunnel_host, tunnel_port)
        self._proxy_headers = tunnel_headers
        self._tls_context = context

    def connect(self) -> None:
        origin_host, origin_port = self._origin
        raw = socket.create_connection((self.host, self.port), self.timeout)
        try:
            outer = self._tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise
        try:
            authority = f"[{origin_host}]" if ":" in origin_host else origin_host
            lines = [f"CONNECT {authority}:{origin_port} HTTP/1.1", f"Host: {authority}:{origin_port}"]
            lines.extend(f"{k}: {v}" for k, v in self._proxy_headers.items())
            outer.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            del lines
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = outer.recv(4096)
                if not chunk:
                    raise http.client.RemoteDisconnected("proxy closed the connection during CONNECT")
                head += chunk
                if len(head) > _MAX_CONNECT_RESPONSE:
                    raise http.client.HTTPException("proxy CONNECT response is too large")
            status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = status_line.split(" ", 2)
            if len(parts) < 2 or not parts[1].isdigit() or not 200 <= int(parts[1]) < 300:
                raise OSError(f"Tunnel connection failed: {redact_text(status_line[:100])}")
            if head.split(b"\r\n\r\n", 1)[1]:
                raise http.client.HTTPException("proxy sent data before the tunnel was established")
            self.sock = _TLSInTLSSocket(outer, self._tls_context, origin_host)
        except BaseException:
            outer.close()
            raise


class _HTTPResponse(RawResponse):
    """A pooled response: the connection returns to the pool only after a full read."""

    def __init__(
        self,
        transport: HTTPTransport,
        entry: _PooledConnection,
        resp: http.client.HTTPResponse,
        slots: threading.BoundedSemaphore,
        read_timeout: float,
    ) -> None:
        super().__init__(resp.status, resp.getheaders(), b"", reason=resp.reason)
        self._transport = transport
        self._entry = entry
        self._resp = resp
        self._slots = slots
        self._read_timeout = read_timeout
        self._complete = False
        self._released = False

    def iter_bytes(self) -> Iterator[bytes]:
        resp = self._resp
        while True:
            chunk, failure = self._read_chunk(resp)
            if failure is not None:
                raise failure
            if not chunk:
                self._complete = True
                # Python 3.9's read1() does not mark a fully-read response closed, and
                # http.client then refuses the next request on this connection.
                resp.close()
                self._finish()
                return
            yield chunk

    def _read_chunk(self, resp: http.client.HTTPResponse) -> tuple[bytes, Optional[RightsFoundryError]]:
        try:
            chunk = resp.read1(CHUNK_SIZE)
            remaining = getattr(resp, "length", None)
            if not chunk and isinstance(remaining, int) and remaining > 0:
                # read1() returns b"" when the peer hangs up before Content-Length
                # bytes arrived; that is a failed read, not the end of the body.
                raise http.client.IncompleteRead(b"", remaining)
            return chunk, None
        except BaseException as exc:
            self._entry.conn.close()
            self._release()
            failure = _io_failure(
                exc,
                phase="read",
                timeout_message=f"read timed out after {self._read_timeout:g}s between body chunks",
                message="connection failed while reading the body",
                charge_state="unknown",
            )
            if failure is None:
                raise
        return b"", failure

    def _release(self) -> None:
        if not self._released:
            self._released = True
            self._slots.release()

    def _finish(self) -> None:
        if self._released:
            return
        if self._complete and not self._resp.will_close:
            self._transport._checkin(self._entry)
        else:
            self._entry.conn.close()
        self._release()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._complete:
            # Unread body (size limit, redirect, error): never reuse the socket.
            self._resp.close()
            self._entry.conn.close()
        self._finish()


# --- async ----------------------------------------------------------------------------


class _ExecutorAsyncResponse(AsyncRawResponse):
    def __init__(self, raw: RawResponse, executor: concurrent.futures.Executor) -> None:
        super().__init__(raw.status, raw.headers, b"", reason=raw.reason)
        self._raw = raw
        self._executor = executor
        self._pending: Optional[concurrent.futures.Future[bytes]] = None

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        iterator = self._raw.iter_bytes()
        while True:
            future = self._executor.submit(next, iterator, b"")
            self._pending = future
            chunk = await asyncio.wrap_future(future)
            self._pending = None
            if not chunk:
                return
            yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = self._pending
        if pending is not None and not pending.done():
            pending.add_done_callback(lambda _f: self._raw.close())
        else:
            self._raw.close()


class AsyncExecutorTransport:
    """Runs a sync :class:`Transport` in a bounded thread pool for ``AsyncClient``."""

    def __init__(self, transport: Transport, *, max_workers: int = 10) -> None:
        self._transport = transport
        self._max_workers = max_workers
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._lock = threading.Lock()

    @property
    def transport(self) -> Transport:
        return self._transport

    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self._max_workers, thread_name_prefix="rightsfoundry"
                )
            return self._executor

    async def send(
        self,
        request: PreparedRequest,
        *,
        connect_timeout: float,
        read_timeout: float,
        write_timeout: float,
    ) -> AsyncRawResponse:
        executor = self._get_executor()
        future = executor.submit(
            self._transport.send,
            request,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
        )
        try:
            raw = await asyncio.wrap_future(future)
        except asyncio.CancelledError:

            def _discard(f: concurrent.futures.Future[RawResponse]) -> None:
                if not f.cancelled() and f.exception() is None:
                    f.result().close()

            future.add_done_callback(_discard)
            raise
        return _ExecutorAsyncResponse(raw, executor)

    def run_sync(self, fn: Callable[..., Any], *args: Any) -> concurrent.futures.Future[Any]:
        return self._get_executor().submit(fn, *args)

    def close(self, *, close_transport: bool = True) -> None:
        """Shut the thread pool down and (by default) close the wrapped transport."""
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=False)
        if close_transport:
            close = getattr(self._transport, "close", None)
            if callable(close):
                close()
