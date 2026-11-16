"""``sync_links.download`` — streaming signed WAV masters / MP3 previews to disk.

This is the only code path that fetches a non-API URL, and it is deliberately
narrow (SEC-13, SEC-18):

* the URL must be ``https://<asset host>/stream/…?expires=…&sig=…`` on port 443
  with exactly those two query keys, and must not expire within 5 seconds;
* **no** ``Authorization`` header, custom headers or cookies are sent — the
  signature is the authorisation and the asset host is a different origin;
* redirects are never followed;
* the body streams in 64 KiB chunks to an exclusively-created ``0o600`` temp
  file in the destination directory, bounded by ``max_download_bytes``,
  ``fsync``-ed and atomically renamed; symlinks are refused and existing files
  are kept unless ``overwrite=True``;
* ``resume=True`` keeps a hidden partial file keyed by the destination *and* a
  hash of the asset path, so resuming into the same file name for a different
  track starts fresh instead of appending one master onto another. If a ranged
  resume is refused (416, 5xx, …), the partial is truncated and the download
  retried once from byte 0.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any, Literal, Optional, Union
from urllib.parse import parse_qsl, urlsplit

from ._config import LOOPBACK_LITERALS, ClientConfig, host_matches
from ._errors import (
    APIConnectionError,
    ResponseTooLargeError,
    RightsFoundryError,
    SyncLinkError,
    SyncLinkExpiredError,
    SyncLinkInvalidError,
    SyncMasterUnavailableError,
    TLSVerificationError,
    UnexpectedRedirectError,
    ValidationError,
)
from ._models import SyncLink, Track
from ._redact import _RedactingHeaders, redact_text
from ._retry import backoff_ms, parse_retry_after
from ._transport import Headers, PreparedRequest

__all__ = [
    "DownloadKind",
    "download_async",
    "download_sync",
    "sanitize_file_name",
    "validate_asset_url",
]

DownloadKind = Literal["sync", "preview"]
Source = Union[SyncLink, Track, str]
PathArg = Union[str, "os.PathLike[str]"]

_EXPIRY_MARGIN_SECONDS = 5
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}", re.ASCII)
_SIG = re.compile(r"[0-9a-fA-F]{16,512}", re.ASCII)
_EXPIRES = re.compile(r"[0-9]{1,12}", re.ASCII)
_RETRYABLE_ASSET_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_ERROR_BODY_LIMIT = 4096

_EXPIRED_HINT = (
    "Request a new link with tracks.sync(); a reissue inside the original window is free, "
    "but minting after expiry is charged again."
)


def _now() -> float:
    return time.time()


def validate_asset_url(url: Any, config: ClientConfig, *, now: Optional[float] = None) -> str:
    """SEC-13: refuse anything that is not a live, allowlisted signed asset URL."""
    if not isinstance(url, str) or not url:
        raise SyncLinkInvalidError("signed URL must be a non-empty string", charge_state="not_charged")
    if len(url) > 8192 or any(ord(c) < 0x21 or ord(c) > 0x7E for c in url) or "\\" in url:
        raise SyncLinkInvalidError("signed URL contains forbidden characters", charge_state="not_charged")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise SyncLinkInvalidError("signed URL is not a valid URL", charge_state="not_charged") from None
    host = (parts.hostname or "").lower().rstrip(".")
    loopback_ok = config.allow_insecure_localhost and config.environment != "production" and host in LOOPBACK_LITERALS
    if loopback_ok:
        if parts.scheme not in ("http", "https"):
            raise SyncLinkInvalidError("signed URL must use https", charge_state="not_charged")
    else:
        if parts.scheme != "https":
            raise SyncLinkInvalidError("signed URL must use https", charge_state="not_charged")
        if not host_matches(host, config.asset_hosts):
            raise SyncLinkInvalidError(
                "signed URL host is not an allowed asset host (asset_hosts)",
                charge_state="not_charged",
            )
        if port not in (None, 443):
            raise SyncLinkInvalidError("signed URL must use port 443", charge_state="not_charged")
    if "@" in parts.netloc:
        raise SyncLinkInvalidError("signed URL must not contain credentials", charge_state="not_charged")
    if parts.fragment:
        raise SyncLinkInvalidError("signed URL must not contain a fragment", charge_state="not_charged")
    path = parts.path
    if not path.startswith("/stream/") or "//" in path or any(seg in (".", "..") for seg in path.split("/")):
        raise SyncLinkInvalidError("signed URL path must start with /stream/", charge_state="not_charged")
    if "%2e" in path.lower() or "%2f" in path.lower():
        raise SyncLinkInvalidError(
            "signed URL path must not contain encoded dots or slashes", charge_state="not_charged"
        )
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise SyncLinkInvalidError("signed URL query is malformed", charge_state="not_charged") from None
    keys = [k for k, _ in pairs]
    if sorted(keys) != ["expires", "sig"]:
        raise SyncLinkInvalidError(
            "signed URL query must contain exactly 'expires' and 'sig'", charge_state="not_charged"
        )
    values = dict(pairs)
    if not _EXPIRES.fullmatch(values["expires"]) or not _SIG.fullmatch(values["sig"]):
        raise SyncLinkInvalidError("signed URL has a malformed expires or sig", charge_state="not_charged")
    current = _now() if now is None else now
    if int(values["expires"]) <= current + _EXPIRY_MARGIN_SECONDS:
        raise SyncLinkExpiredError(f"signed URL has expired. {_EXPIRED_HINT}", charge_state="not_charged")
    return url


def sanitize_file_name(name: Optional[str], fallback: str) -> str:
    """Basename only, ``[A-Za-z0-9._-]``, no leading dot, ≤ 255 bytes; else ``fallback``."""
    if not name:
        return fallback
    base = re.split(r"[\\/]", name)[-1]
    if base in (".", "..") or not _SAFE_NAME.fullmatch(base) or len(base.encode()) > 255:
        return fallback
    return base


def _resolve_source(source: Source, kind: DownloadKind) -> tuple[str, str]:
    """Return ``(url, suggested_file_name)``."""
    if kind not in ("sync", "preview"):
        raise ValidationError("kind must be 'sync' or 'preview'")
    if isinstance(source, SyncLink):
        if kind != "sync":
            raise ValidationError("a SyncLink is a WAV master; use kind='sync'")
        return source.download_url, sanitize_file_name(source.file_name, f"{source.id}.wav")
    if isinstance(source, Track):
        if kind != "preview":
            raise ValidationError(
                "a Track carries only the MP3 preview; call tracks.sync() for the WAV master, or pass kind='preview'"
            )
        if not source.asset_url:
            raise ValidationError(f"track {source.id} has no preview asset_url")
        name = urlsplit(source.asset_url).path.rsplit("/", 1)[-1]
        return source.asset_url, sanitize_file_name(name, f"{source.id}.mp3")
    if isinstance(source, str):
        try:
            name = urlsplit(source).path.rsplit("/", 1)[-1]
        except ValueError:
            name = ""
        fallback = "sync-master.wav" if kind == "sync" else "preview.mp3"
        return source, sanitize_file_name(name, fallback)
    raise ValidationError("link must be a SyncLink, a Track (kind='preview') or a signed URL string")


class _Target:
    """Resolved destination + temp file lifecycle (SEC-18)."""

    def __init__(self, dest: PathArg, suggested: str, *, overwrite: bool, resume: bool, source_key: str = "") -> None:
        if not isinstance(dest, (str, os.PathLike)):
            raise ValidationError("dest must be a path")
        raw = os.fspath(dest)
        if not raw:
            raise ValidationError("dest must not be empty")
        path = Path(raw)
        if raw.endswith((os.sep, "/")) and not path.exists():
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_dir() and not path.is_symlink():
            directory, name = path, suggested
        elif path.is_symlink():
            raise ValidationError("dest is a symlink; refusing to follow it")
        else:
            directory, name = path.parent if str(path.parent) else Path("."), path.name
            if not directory.is_dir():
                raise ValidationError("the destination directory does not exist")
        self.directory = directory
        self.final = directory / name
        self.overwrite = overwrite
        self.resume = resume
        if self.final.is_symlink():
            raise ValidationError("destination is a symlink; refusing to follow it")
        if self.final.exists():
            if self.final.is_dir():
                raise ValidationError("destination exists and is a directory")
            if not overwrite:
                raise ValidationError("destination file exists; pass overwrite=True to replace it")
        # Keyed by the asset path (not the signature, which changes on every reissue),
        # so a partial of one track is never resumed into another track's download.
        tag = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:12]
        self.partial = directory / f".{name}.{tag}.rightsfoundry-partial"
        self.temp: Optional[Path] = None
        self.fd: Optional[int] = None
        self.offset = 0

    def open(self) -> int:
        """Open the temp file; returns the byte offset to resume from (0 = fresh)."""
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        binary = getattr(os, "O_BINARY", 0)
        if self.resume:
            self.temp = self.partial
            try:
                fd = os.open(self.temp, os.O_WRONLY | os.O_APPEND | nofollow | binary)
            except FileNotFoundError:
                fd = os.open(self.temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | binary, 0o600)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValidationError("partial download file is a symlink; refusing") from None
                raise
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or (hasattr(os, "geteuid") and st.st_uid != os.geteuid()):
                os.close(fd)
                raise ValidationError("partial download file is not a regular file owned by this user")
            self.fd = fd
            self.offset = st.st_size
            return self.offset
        self.temp = self.directory / f".{self.final.name}.{secrets.token_hex(8)}.part"
        self.fd = os.open(self.temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | binary, 0o600)
        self.offset = 0
        return 0

    def _fd(self) -> int:
        if self.fd is None:
            raise ValidationError("download file is not open")
        return self.fd

    def truncate(self) -> None:
        fd = self._fd()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        self.offset = 0

    def write(self, chunk: bytes) -> None:
        fd = self._fd()
        view = memoryview(chunk)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        self.offset += len(chunk)

    def finalize(self) -> Path:
        fd = self._fd()
        if self.temp is None:
            raise ValidationError("download temp file is missing")
        os.fsync(fd)
        os.close(fd)
        self.fd = None
        if self.final.is_symlink():
            self._remove_temp()
            raise ValidationError("destination became a symlink; refusing")
        if self.overwrite:
            os.replace(self.temp, self.final)
        else:
            try:
                os.link(self.temp, self.final)
            except FileExistsError:
                self._remove_temp()
                raise ValidationError("destination file appeared during the download; not overwriting") from None
            except OSError:
                if self.final.exists():
                    self._remove_temp()
                    raise ValidationError("destination file exists; pass overwrite=True") from None
                os.replace(self.temp, self.final)
            else:
                os.unlink(self.temp)
        self.temp = None
        if hasattr(os, "O_DIRECTORY"):
            try:
                dfd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        return self.final

    def _remove_temp(self) -> None:
        if self.temp is not None:
            try:
                os.unlink(self.temp)
            except FileNotFoundError:
                pass
            self.temp = None

    def abort(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        if not self.resume:
            self._remove_temp()


def _headers(config: ClientConfig, offset: int) -> _RedactingHeaders:
    from ._pipeline import user_agent

    headers = _RedactingHeaders()
    headers.add("User-Agent", user_agent(config.user_agent_suffix))
    headers.add("Accept", "*/*")
    headers.add("Accept-Encoding", "identity")
    if offset > 0:
        headers.add("Range", f"bytes={offset}-")
    return headers


def _asset_error(status: int, headers: Headers, text: str, kind: DownloadKind) -> RightsFoundryError:
    message = redact_text(text.strip()[:200])
    if 300 <= status < 400:
        location = headers.get("location")
        return UnexpectedRedirectError(
            f"asset host answered HTTP {status}; redirects are never followed"
            + (f" (Location: {redact_text(location)})" if location else ""),
            status=status,
            location=redact_text(location) if location else None,
            charge_state="not_charged",
        )
    if status == 403 and message == "URL has expired":
        return SyncLinkExpiredError(
            f"signed URL has expired. {_EXPIRED_HINT}", status=status, charge_state="not_charged"
        )
    if status in (400, 403):
        return SyncLinkInvalidError(
            f"asset host refused the signed URL (HTTP {status}: {message or 'no detail'})",
            status=status,
            server_error=message or None,
            charge_state="not_charged",
        )
    if status == 404:
        what = "sync master" if kind == "sync" else "preview"
        return SyncMasterUnavailableError(
            f"the {what} file was not found on the asset host (HTTP 404)",
            status=status,
            server_error=message or None,
            charge_state="not_charged",
        )
    return SyncLinkError(
        f"asset host answered HTTP {status}: {message or 'no detail'}",
        status=status,
        server_error=message or None,
        charge_state="not_charged",
    )


def _parse_content_range(value: Optional[str]) -> Optional[tuple[int, int, Optional[int]]]:
    if not value:
        return None
    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)", value.strip())
    if not match:
        return None
    total = None if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), int(match.group(2)), total


class _Session:
    """State machine shared by the sync and async drivers."""

    def __init__(self, config: ClientConfig, url: str, target: _Target, kind: DownloadKind) -> None:
        self.config = config
        self.url = url
        self.target = target
        self.kind = kind
        self.attempt = 0
        self.expected_total: Optional[int] = None
        self.range_requested = False
        self.range_fallback_used = False

    def request(self) -> PreparedRequest:
        validate_asset_url(self.url, self.config)
        offset = self.target.offset
        self.range_requested = offset > 0
        return PreparedRequest("GET", self.url, _headers(self.config, offset), None)

    def timeouts(self, timeout: Optional[float]) -> dict[str, float]:
        cfg = self.config
        read = timeout if timeout is not None else cfg.read_timeout
        return {
            "connect_timeout": cfg.connect_timeout,
            "read_timeout": read,
            "write_timeout": cfg.write_timeout,
        }

    def start_body(self, status: int, headers: Headers) -> None:
        """Validate a 200/206 and prepare the file for the body."""
        cfg = self.config
        if status == 206:
            parsed = _parse_content_range(headers.get("content-range"))
            if not self.range_requested or parsed is None or parsed[0] != self.target.offset:
                raise SyncLinkError(
                    "asset host returned an unexpected partial response (Content-Range mismatch)",
                    status=status,
                    charge_state="not_charged",
                )
            self.expected_total = parsed[2]
        else:
            if self.range_requested:
                self.target.truncate()  # the server ignored Range: start over
            length = headers.get("content-length")
            self.expected_total = int(length) if length and length.strip().isdigit() else None
        if self.expected_total is not None and self.expected_total > cfg.max_download_bytes:
            raise ResponseTooLargeError(
                f"file is {self.expected_total} bytes, above max_download_bytes={cfg.max_download_bytes}",
                max_bytes=cfg.max_download_bytes,
                observed_bytes=self.expected_total,
                charge_state="not_charged",
            )

    def write(self, chunk: bytes) -> None:
        limit = self.config.max_download_bytes
        if self.target.offset + len(chunk) > limit:
            raise ResponseTooLargeError(
                f"download exceeded max_download_bytes={limit}",
                max_bytes=limit,
                observed_bytes=self.target.offset + len(chunk),
                charge_state="not_charged",
            )
        self.target.write(chunk)

    def finish(self) -> Path:
        if self.expected_total is not None and self.target.offset != self.expected_total:
            raise APIConnectionError(
                f"download ended early ({self.target.offset} of {self.expected_total} bytes)",
                phase="read",
                charge_state="not_charged",
            )
        return self.target.finalize()

    def retry_delay(self, status: Optional[int], retry_after: Optional[float]) -> Optional[float]:
        cfg = self.config
        if self.attempt > cfg.max_retries:
            return None
        if status is not None and status not in _RETRYABLE_ASSET_STATUSES:
            return None
        delay = backoff_ms(
            self.attempt,
            initial_ms=cfg.retry_initial_delay_ms,
            max_ms=cfg.retry_max_delay_ms,
            jitter=cfg.jitter,
        )
        if retry_after is not None:
            if retry_after * 1000 > cfg.max_retry_after_ms:
                return None
            delay = max(delay, retry_after * 1000)
        return delay

    def reset_for_retry(self) -> None:
        if not self.target.resume:
            self.target.truncate()

    def fallback_from_range(self, error: Optional[RightsFoundryError]) -> bool:
        """After a refused ranged resume, start over once with a plain ``GET``.

        The asset host may answer an out-of-range or stale ``Range`` with a 416,
        a 5xx or a mismatched ``206`` rather than a clean error; without this the
        same failing request would be retried until the partial was deleted by
        hand. Errors that a fresh request cannot fix (expired / invalid signature,
        redirect, missing file) are not retried this way.
        """
        if not self.range_requested or self.range_fallback_used:
            return False
        if isinstance(error, (UnexpectedRedirectError, SyncLinkInvalidError, SyncLinkExpiredError)):
            return False
        if isinstance(error, SyncMasterUnavailableError):
            return False
        self.range_fallback_used = True
        self.target.truncate()
        return True


def _prepare(
    config: ClientConfig,
    source: Source,
    dest: PathArg,
    *,
    overwrite: bool,
    resume: bool,
    kind: DownloadKind,
) -> _Session:
    if not isinstance(overwrite, bool) or not isinstance(resume, bool):
        raise ValidationError("overwrite and resume must be bools")
    url, suggested = _resolve_source(source, kind)
    validate_asset_url(url, config)
    target = _Target(dest, suggested, overwrite=overwrite, resume=resume, source_key=urlsplit(url).path)
    return _Session(config, url, target, kind)


def download_sync(
    config: ClientConfig,
    transport: Any,
    source: Source,
    dest: PathArg,
    *,
    overwrite: bool = False,
    resume: bool = False,
    kind: DownloadKind = "sync",
    timeout: Optional[float] = None,
) -> Path:
    session = _prepare(config, source, dest, overwrite=overwrite, resume=resume, kind=kind)
    target = session.target
    target.open()
    try:
        while True:
            session.attempt += 1
            raw = None
            try:
                raw = transport.send(session.request(), **session.timeouts(timeout))
                status = raw.status
                if status not in (200, 206):
                    text = b"".join(_bounded(raw.iter_bytes(), _ERROR_BODY_LIMIT)).decode("utf-8", "replace")
                    error = _asset_error(status, raw.headers, text, kind)
                    if session.fallback_from_range(error):
                        continue
                    delay = None
                    if not isinstance(error, (UnexpectedRedirectError, SyncLinkInvalidError, SyncLinkExpiredError)):
                        delay = session.retry_delay(status, parse_retry_after(raw.headers.get("retry-after")))
                    if delay is None:
                        raise error
                    raw.close()
                    time.sleep(delay / 1000)
                    session.reset_for_retry()
                    continue
                try:
                    session.start_body(status, raw.headers)
                except SyncLinkError as exc:
                    if session.fallback_from_range(exc):
                        continue
                    raise
                for chunk in raw.iter_bytes():
                    session.write(chunk)
                raw.close()
                raw = None
                return session.finish()
            except APIConnectionError as exc:
                if isinstance(exc, TLSVerificationError):
                    raise
                delay = session.retry_delay(None, None)
                if delay is None:
                    raise
                time.sleep(delay / 1000)
                session.reset_for_retry()
            finally:
                if raw is not None:
                    raw.close()
    except BaseException:
        target.abort()
        raise


def _bounded(chunks: Any, limit: int) -> list[bytes]:
    out: list[bytes] = []
    size = 0
    for chunk in chunks:
        out.append(chunk[: max(0, limit - size)])
        size += len(chunk)
        if size >= limit:
            break
    return out


async def download_async(
    config: ClientConfig,
    transport: Any,
    source: Source,
    dest: PathArg,
    *,
    overwrite: bool = False,
    resume: bool = False,
    kind: DownloadKind = "sync",
    timeout: Optional[float] = None,
) -> Path:
    loop = asyncio.get_running_loop()
    session = _prepare(config, source, dest, overwrite=overwrite, resume=resume, kind=kind)
    target = session.target
    target.open()
    try:
        while True:
            session.attempt += 1
            raw = None
            try:
                raw = await transport.send(session.request(), **session.timeouts(timeout))
                status = raw.status
                if status not in (200, 206):
                    parts: list[bytes] = []
                    size = 0
                    async for chunk in raw.aiter_bytes():
                        parts.append(chunk)
                        size += len(chunk)
                        if size >= _ERROR_BODY_LIMIT:
                            break
                    text = b"".join(parts)[:_ERROR_BODY_LIMIT].decode("utf-8", "replace")
                    error = _asset_error(status, raw.headers, text, kind)
                    if session.fallback_from_range(error):
                        continue
                    delay = None
                    if not isinstance(error, (UnexpectedRedirectError, SyncLinkInvalidError, SyncLinkExpiredError)):
                        delay = session.retry_delay(status, parse_retry_after(raw.headers.get("retry-after")))
                    if delay is None:
                        raise error
                    await raw.aclose()
                    await asyncio.sleep(delay / 1000)
                    session.reset_for_retry()
                    continue
                try:
                    session.start_body(status, raw.headers)
                except SyncLinkError as exc:
                    if session.fallback_from_range(exc):
                        continue
                    raise
                async for chunk in raw.aiter_bytes():
                    await loop.run_in_executor(None, session.write, chunk)
                await raw.aclose()
                raw = None
                return await loop.run_in_executor(None, session.finish)
            except APIConnectionError as exc:
                if isinstance(exc, TLSVerificationError):
                    raise
                delay = session.retry_delay(None, None)
                if delay is None:
                    raise
                await asyncio.sleep(delay / 1000)
                session.reset_for_retry()
            finally:
                if raw is not None:
                    await raw.aclose()
    except BaseException:
        target.abort()
        raise
