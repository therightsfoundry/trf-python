"""``client.taxonomies``, ``client.credits``, ``client.health`` and ``client.sync_links``."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union, cast

from .._download import DownloadKind, PathArg, download_async, download_sync
from .._endpoints import ENDPOINTS
from .._errors import (
    APIConnectionError,
    HealthCheckError,
    UnexpectedRedirectError,
    UnexpectedResponseError,
)
from .._models import (
    Credits,
    HealthStatus,
    LivenessStatus,
    ReadinessStatus,
    SyncLink,
    Taxonomies,
    Track,
)
from .._options import CallOptions, resolve_options, validate_timeout

if TYPE_CHECKING:
    from .._config import ClientConfig
    from .._pipeline import AsyncPipeline, SyncPipeline

__all__ = [
    "AsyncCreditsResource",
    "AsyncHealthResource",
    "AsyncSyncLinksResource",
    "AsyncTaxonomiesResource",
    "CreditsResource",
    "HealthResource",
    "SyncLinksResource",
    "TaxonomiesResource",
]

Link = Union[SyncLink, Track, str]


class _TaxonomyCache:
    def __init__(self, ttl_ms: int) -> None:
        self.ttl = ttl_ms / 1000.0
        self._value: Optional[Taxonomies] = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> Optional[Taxonomies]:
        if self.ttl <= 0:
            return None
        with self._lock:
            if self._value is not None and time.monotonic() - self._at < self.ttl:
                return self._value
        return None

    def put(self, value: Taxonomies) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._value = value
            self._at = time.monotonic()


_TAXONOMIES = ENDPOINTS["taxonomies.list"]


class TaxonomiesResource:
    """``GET /v1/taxonomies`` — free. Cached in-process when ``taxonomies_cache_ttl_ms`` > 0."""

    def __init__(self, pipeline: SyncPipeline, cache_ttl_ms: int) -> None:
        self._pipeline = pipeline
        self._cache = _TaxonomyCache(cache_ttl_ms)

    def list(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> Taxonomies:
        opts = resolve_options(
            options,
            timeout=timeout,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        cached = self._cache.get()
        if cached is not None:
            return cached
        value = cast(Taxonomies, self._pipeline.execute(_TAXONOMIES.build(options=opts)))
        self._cache.put(value)
        return value


class AsyncTaxonomiesResource:
    def __init__(self, pipeline: AsyncPipeline, cache_ttl_ms: int) -> None:
        self._pipeline = pipeline
        self._cache = _TaxonomyCache(cache_ttl_ms)

    async def list(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> Taxonomies:
        opts = resolve_options(
            options,
            timeout=timeout,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        cached = self._cache.get()
        if cached is not None:
            return cached
        value = cast(Taxonomies, await self._pipeline.execute(_TAXONOMIES.build(options=opts)))
        self._cache.put(value)
        return value


class CreditsResource:
    """``GET /v1/credits`` — free and unmetered. Also refreshes the client's credit guard."""

    def __init__(self, pipeline: SyncPipeline) -> None:
        self._pipeline = pipeline

    def retrieve(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> Credits:
        """``GET /v1/credits`` — free. Also refreshes the client's credit guard."""
        return self._pipeline.refresh_credits(
            resolve_options(
                options,
                timeout=timeout,
                max_retries=max_retries,
                idempotency_key=idempotency_key,
                send_max_credits_header=send_max_credits_header,
            )
        )


class AsyncCreditsResource:
    def __init__(self, pipeline: AsyncPipeline) -> None:
        self._pipeline = pipeline

    async def retrieve(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> Credits:
        """``GET /v1/credits`` (async) — free. Also refreshes the client's credit guard."""
        return await self._pipeline.refresh_credits(
            resolve_options(
                options,
                timeout=timeout,
                max_retries=max_retries,
                idempotency_key=idempotency_key,
                send_max_credits_header=send_max_credits_header,
            )
        )


def _health_error(exc: Exception, operation: str) -> HealthCheckError:
    return HealthCheckError(
        f"{operation}: the API could not be reached ({getattr(exc, 'message', str(exc))})",
        operation=operation,
        phase=getattr(exc, "phase", None),
        status=getattr(exc, "status", None),
        request_id=getattr(exc, "request_id", None),
        charge_state="not_charged",
    )


_HEALTH_FAILURES = (APIConnectionError, UnexpectedResponseError, UnexpectedRedirectError)


_HEALTH_DOC = """Unauthenticated probes. The API key is **never** sent. ``503`` answers are returned as values.

    Probes default to ``max_retries=0``: a degraded ``503`` is returned at once
    instead of after ``Retry-After`` sleeps. Pass ``max_retries=2`` to retry.
    """


class HealthResource:
    __doc__ = _HEALTH_DOC

    def __init__(self, pipeline: SyncPipeline) -> None:
        self._pipeline = pipeline

    def _run(self, operation: str, options: CallOptions) -> Any:
        try:
            return self._pipeline.execute(ENDPOINTS[operation].build(options=options))
        except _HEALTH_FAILURES as exc:
            raise _health_error(exc, operation) from None

    def check(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> HealthStatus:
        """``GET /v1/health`` → ``healthy`` (200) or ``degraded`` (503)."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        return cast(HealthStatus, self._run("health.check", opts))

    def live(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> LivenessStatus:
        """``GET /v1/health/live`` — zero I/O on the server."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        return cast(LivenessStatus, self._run("health.live", opts))

    def ready(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> ReadinessStatus:
        """``GET /v1/health/ready`` → ``ready`` (200) or ``not ready`` (503)."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        return cast(ReadinessStatus, self._run("health.ready", opts))


class AsyncHealthResource:
    __doc__ = _HEALTH_DOC

    def __init__(self, pipeline: AsyncPipeline) -> None:
        self._pipeline = pipeline

    async def _run(self, operation: str, options: CallOptions) -> Any:
        try:
            return await self._pipeline.execute(ENDPOINTS[operation].build(options=options))
        except _HEALTH_FAILURES as exc:
            raise _health_error(exc, operation) from None

    async def check(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> HealthStatus:
        """``GET /v1/health`` (async)."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        return cast(HealthStatus, await self._run("health.check", opts))

    async def live(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> LivenessStatus:
        """``GET /v1/health/live`` (async)."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        return cast(LivenessStatus, await self._run("health.live", opts))

    async def ready(
        self,
        *,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> ReadinessStatus:
        """``GET /v1/health/ready`` (async)."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        return cast(ReadinessStatus, await self._run("health.ready", opts))


class SyncLinksResource:
    """Downloads signed WAV masters (``kind="sync"``) or MP3 previews (``kind="preview"``).

    Free: the sync charge was taken when the link was minted. The asset host gets
    **no** ``Authorization`` header, custom headers or cookies. Preview streams
    are metered today and are intended to be billed from API credits
    (``costs.stream``) in a future update.
    """

    def __init__(self, config: ClientConfig, transport: Any) -> None:
        self._config = config
        self._transport = transport

    def download(
        self,
        link: Link,
        dest: PathArg,
        *,
        overwrite: bool = False,
        resume: bool = False,
        kind: DownloadKind = "sync",
        timeout: Optional[float] = None,
    ) -> Path:
        """Stream ``link`` to ``dest`` (a file path or an existing directory) atomically."""
        return download_sync(
            self._config,
            self._transport,
            link,
            dest,
            overwrite=overwrite,
            resume=resume,
            kind=kind,
            timeout=validate_timeout(timeout),
        )


class AsyncSyncLinksResource:
    def __init__(self, config: ClientConfig, transport: Any) -> None:
        self._config = config
        self._transport = transport

    async def download(
        self,
        link: Link,
        dest: PathArg,
        *,
        overwrite: bool = False,
        resume: bool = False,
        kind: DownloadKind = "sync",
        timeout: Optional[float] = None,
    ) -> Path:
        return await download_async(
            self._config,
            self._transport,
            link,
            dest,
            overwrite=overwrite,
            resume=resume,
            kind=kind,
            timeout=validate_timeout(timeout),
        )
