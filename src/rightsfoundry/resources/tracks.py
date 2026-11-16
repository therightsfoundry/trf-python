"""``client.tracks`` — catalogue listing, Deep Search, Acoustic Similarity Search and sync licensing.

Every method builds its request from the endpoint registry (``ENDPOINTS[...].build``);
nothing here knows paths, body shapes or price keys. The sync and async classes
expose identical signatures (``tests/test_architecture_parity.py`` enforces it).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Optional, Union, cast

from .._endpoints import ENDPOINTS, LIST_QUERY_PARAMS, OperationSpec
from .._errors import APIConnectionError, APITimeoutError, RightsFoundryError
from .._models import SyncLink, Track, TrackPage
from .._options import CallOptions, resolve_options
from .._pagination import AsyncPageIterator, SyncPageIterator

if TYPE_CHECKING:
    from .._pipeline import AsyncPipeline, SyncPipeline

__all__ = ["AsyncTracksResource", "TracksResource"]

TrackId = Union[int, str]

_LIST = ENDPOINTS["tracks.list"]
_GET = ENDPOINTS["tracks.get"]
_DEEP = ENDPOINTS["tracks.deep_search"]
_SIMILAR = ENDPOINTS["tracks.search_similar"]
_SYNC = ENDPOINTS["tracks.sync"]

#: ``tracks.list`` / ``tracks.iter`` filter keywords, straight from the registry.
LIST_FILTERS: tuple[str, ...] = tuple(p.name for p in LIST_QUERY_PARAMS if p.name not in ("page", "limit"))


def _filters(local: Mapping[str, Any]) -> dict[str, Any]:
    return {name: local[name] for name in LIST_FILTERS if local.get(name) is not None}


def _cost_args(op: OperationSpec) -> dict[str, Any]:
    return {**dict(op.query), **(op.json_body or {})}


def _page_cost_key(op: OperationSpec) -> Any:
    args = _cost_args(op)
    return lambda page: op.endpoint.cost_key({**args, "page": page})


def _follower_timeout(op: OperationSpec, timeout: Optional[float]) -> APITimeoutError:
    return APITimeoutError(
        f"{op.operation}: an identical sync call on this client is still in flight and did not "
        f"finish within this call's timeout of {timeout:g}s; it may still mint the link",
        operation=op.operation,
        phase="total",
        charge_state="unknown",
    )


def _leader_interrupted(op: OperationSpec, exc: BaseException) -> APIConnectionError:
    return APIConnectionError(
        f"{op.operation}: the identical in-flight sync call this request joined was interrupted "
        f"({type(exc).__name__}); retry the call",
        operation=op.operation,
        phase="read",
        charge_state="unknown",
    )


def _for_follower(exc: BaseException) -> BaseException:
    """A follower gets its own copy of the leader's SDK error (tracebacks are per raise)."""
    if isinstance(exc, RightsFoundryError):
        return copy.copy(exc)
    return exc


class TracksResource:
    """Synchronous ``client.tracks``."""

    def __init__(self, pipeline: SyncPipeline) -> None:
        self._pipeline = pipeline
        self._inflight: dict[tuple[str, bool], concurrent.futures.Future[SyncLink]] = {}
        self._lock = threading.Lock()

    def list(
        self,
        *,
        page: Optional[int] = None,
        limit: Optional[int] = None,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        mood: Optional[str] = None,
        instrument: Optional[str] = None,
        theme: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        vocals: Optional[str] = None,
        key: Optional[str] = None,
        bpm_min: Optional[float] = None,
        bpm_max: Optional[float] = None,
        duration_min: Optional[float] = None,
        duration_max: Optional[float] = None,
        featured: Optional[bool] = None,
        explicit: Optional[bool] = None,
        decade: Optional[str] = None,
        id: Optional[int] = None,
        include_children: Optional[bool] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> TrackPage:
        """``GET /v1/tracks`` — browse, filter or text-search the catalogue. **Charged.**

        Priced at ``tracks_list``; with a non-empty ``search``, page 1 at
        ``tracks_search_first_page`` and later pages at ``tracks_search_next_page``.
        ``limit`` is 1–100 (values above 100 are rejected client-side because the
        server silently caps them). ``bpm_min``/``bpm_max`` and
        ``duration_min``/``duration_max`` are sent as ``bpmMin`` … ``durationMax``.
        Per-call options (``timeout``, ``max_retries``, …) are described on
        :class:`~rightsfoundry.CallOptions`.
        """
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        query = {**_filters(locals()), "page": page, "limit": limit}
        return cast(TrackPage, self._pipeline.execute(_LIST.build(query=query, options=opts)))

    def iter(
        self,
        *,
        limit: int = 100,
        start_page: int = 1,
        max_pages: int = 50,
        max_items: Optional[int] = None,
        max_credits: Optional[int] = None,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        mood: Optional[str] = None,
        instrument: Optional[str] = None,
        theme: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        vocals: Optional[str] = None,
        key: Optional[str] = None,
        bpm_min: Optional[float] = None,
        bpm_max: Optional[float] = None,
        duration_min: Optional[float] = None,
        duration_max: Optional[float] = None,
        featured: Optional[bool] = None,
        explicit: Optional[bool] = None,
        decade: Optional[str] = None,
        id: Optional[int] = None,
        include_children: Optional[bool] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        send_max_credits_header: Optional[bool] = None,
    ) -> SyncPageIterator:
        """Lazily iterate ``tracks.list`` pages. **Every page is charged.**

        ``max_credits`` is a budget for the whole walk: the next page's cost is
        projected from your rate card and :class:`CreditBudgetExceededError` is
        raised before it would be exceeded. ``max_pages`` defaults to 50. The
        iterator is single-use (:class:`IteratorConsumedError` on a second pass).
        """
        opts = resolve_options(
            timeout=timeout, max_retries=max_retries, send_max_credits_header=send_max_credits_header
        )
        filters = {**_filters(locals()), "limit": limit}
        first = _LIST.build(query={**filters, "page": start_page}, options=opts)  # validate eagerly

        def fetch(page: int) -> TrackPage:
            op = _LIST.build(query={**filters, "page": page}, options=opts)
            return cast(TrackPage, self._pipeline.execute(op))

        return SyncPageIterator(
            operation=_LIST.operation,
            fetch=fetch,
            cost_key_for=_page_cost_key(first),
            project=lambda key: self._pipeline.ensure_rate_card(_LIST.operation, key),
            start_page=start_page,
            max_pages=max_pages,
            max_items=max_items,
            max_credits=max_credits,
        )

    def get(
        self,
        track_id: TrackId,
        *,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> Track:
        """``GET /v1/tracks/{id}`` — one track with its alternate versions (``child_tracks``).

        **Charged** at ``track_detail`` only when the track exists; a ``404`` is free.
        """
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        return cast(Track, self._pipeline.execute(_GET.build(track_id=track_id, options=opts)))

    def deep_search(
        self,
        query: str,
        *,
        page: int = 1,
        limit: int = 20,
        instrumental: bool = False,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> TrackPage:
        """``POST /v1/tracks/deep-search`` — natural-language brief, relevance ordered.

        **Charged:** page 1 at ``deep_search_first_page`` *every time it is sent*,
        later pages at ``deep_search_next_page``. Ask for a large page once.
        """
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        body = {"query": query, "page": page, "limit": limit, "instrumental": instrumental}
        return cast(TrackPage, self._pipeline.execute(_DEEP.build(body=body, options=opts)))

    def deep_search_iter(
        self,
        query: str,
        *,
        limit: int = 100,
        instrumental: bool = False,
        max_pages: int = 50,
        max_items: Optional[int] = None,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        send_max_credits_header: Optional[bool] = None,
    ) -> SyncPageIterator:
        """Iterate Deep Search pages (candidate set ≤ 1,000 → at most 10 pages at limit 100)."""
        opts = resolve_options(
            timeout=timeout, max_retries=max_retries, send_max_credits_header=send_max_credits_header
        )
        first = _DEEP.build(body={"query": query, "limit": limit, "instrumental": instrumental}, options=opts)
        base = dict(first.json_body or {})

        def fetch(page: int) -> TrackPage:
            op = _DEEP.build(body={**base, "page": page}, options=opts)
            return cast(TrackPage, self._pipeline.execute(op))

        return SyncPageIterator(
            operation=_DEEP.operation,
            fetch=fetch,
            cost_key_for=_page_cost_key(first),
            project=lambda key: self._pipeline.ensure_rate_card(_DEEP.operation, key),
            max_pages=max_pages,
            max_items=max_items,
            max_credits=max_credits,
        )

    def search_similar(
        self,
        file_url: str,
        *,
        page: int = 1,
        limit: int = 20,
        instrumental: bool = False,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> TrackPage:
        """``POST /v1/tracks/search`` — Acoustic Similarity Search by a reference recording.

        ``file_url`` must be a publicly reachable ``.mp3``/``.wav`` (≤ 120 MB). It is
        validated strictly before sending because **an unreachable file is charged
        and not refunded** (:class:`UnreachableFileError`).
        """
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        body = {"file_url": file_url, "page": page, "limit": limit, "instrumental": instrumental}
        op = _SIMILAR.build(body=body, options=opts)
        if op.plain_http_file_url:
            self._pipeline.core.warn_plain_http_file_url()
        return cast(TrackPage, self._pipeline.execute(op))

    def search_similar_iter(
        self,
        file_url: str,
        *,
        limit: int = 100,
        instrumental: bool = False,
        max_pages: int = 50,
        max_items: Optional[int] = None,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        send_max_credits_header: Optional[bool] = None,
    ) -> SyncPageIterator:
        """Iterate Acoustic Similarity Search pages. **Every page is charged.**"""
        opts = resolve_options(
            timeout=timeout, max_retries=max_retries, send_max_credits_header=send_max_credits_header
        )
        first = _SIMILAR.build(body={"file_url": file_url, "limit": limit, "instrumental": instrumental}, options=opts)
        if first.plain_http_file_url:
            self._pipeline.core.warn_plain_http_file_url()
        base = dict(first.json_body or {})

        def fetch(page: int) -> TrackPage:
            op = _SIMILAR.build(body={**base, "page": page}, options=opts)
            return cast(TrackPage, self._pipeline.execute(op))

        return SyncPageIterator(
            operation=_SIMILAR.operation,
            fetch=fetch,
            cost_key_for=_page_cost_key(first),
            project=lambda key: self._pipeline.ensure_rate_card(_SIMILAR.operation, key),
            max_pages=max_pages,
            max_items=max_items,
            max_credits=max_credits,
        )

    def sync(
        self,
        track_id: TrackId,
        *,
        external_export: bool = False,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> SyncLink:
        """``POST /v1/tracks/{id}/sync`` — mint a signed 1-hour link to the WAV master.

        **Charged** at ``sync`` (or ``export_sync`` with ``external_export=True``)
        when a new window opens; a repeat inside the live window is free
        (``credits_charged == 0``, ``reissued is True``) and keeps the original
        expiry. Concurrent calls for the same ``(track_id, external_export)`` on
        this client share one HTTP request; each caller's own ``max_credits`` and
        ``timeout`` still apply to it.
        """
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        op = _SYNC.build(track_id=track_id, body={"external_export": external_export}, options=opts)
        key = (op.path, bool((op.json_body or {}).get("external_export")))
        with self._lock:
            future = self._inflight.get(key)
            leader = future is None
            if future is None:
                future = concurrent.futures.Future()
                self._inflight[key] = future
        if not leader:
            # A follower is still bound by its own cap and deadline (H6).
            self._pipeline.preflight(op)
            try:
                return future.result(timeout=opts.timeout)
            except concurrent.futures.TimeoutError:
                raise _follower_timeout(op, opts.timeout) from None
            except Exception as exc:  # noqa: BLE001 - re-raised as the follower's own copy
                raise _for_follower(exc) from None
        try:
            link = cast(SyncLink, self._pipeline.execute(op))
        except Exception as exc:
            future.set_exception(exc)
            raise
        except BaseException as exc:
            # KeyboardInterrupt / SystemExit belong to the leader's thread only.
            future.set_exception(_leader_interrupted(op, exc))
            raise
        else:
            future.set_result(link)
            return link
        finally:
            with self._lock:
                if self._inflight.get(key) is future:
                    del self._inflight[key]


class AsyncTracksResource:
    """Asynchronous ``client.tracks`` (same semantics and signatures as :class:`TracksResource`)."""

    def __init__(self, pipeline: AsyncPipeline) -> None:
        self._pipeline = pipeline
        self._inflight: dict[tuple[str, bool], asyncio.Future[SyncLink]] = {}

    async def list(
        self,
        *,
        page: Optional[int] = None,
        limit: Optional[int] = None,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        mood: Optional[str] = None,
        instrument: Optional[str] = None,
        theme: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        vocals: Optional[str] = None,
        key: Optional[str] = None,
        bpm_min: Optional[float] = None,
        bpm_max: Optional[float] = None,
        duration_min: Optional[float] = None,
        duration_max: Optional[float] = None,
        featured: Optional[bool] = None,
        explicit: Optional[bool] = None,
        decade: Optional[str] = None,
        id: Optional[int] = None,
        include_children: Optional[bool] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> TrackPage:
        """``GET /v1/tracks`` (async). See :meth:`TracksResource.list`."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        query = {**_filters(locals()), "page": page, "limit": limit}
        return cast(TrackPage, await self._pipeline.execute(_LIST.build(query=query, options=opts)))

    def iter(
        self,
        *,
        limit: int = 100,
        start_page: int = 1,
        max_pages: int = 50,
        max_items: Optional[int] = None,
        max_credits: Optional[int] = None,
        search: Optional[str] = None,
        genre: Optional[str] = None,
        mood: Optional[str] = None,
        instrument: Optional[str] = None,
        theme: Optional[str] = None,
        artist: Optional[str] = None,
        album: Optional[str] = None,
        vocals: Optional[str] = None,
        key: Optional[str] = None,
        bpm_min: Optional[float] = None,
        bpm_max: Optional[float] = None,
        duration_min: Optional[float] = None,
        duration_max: Optional[float] = None,
        featured: Optional[bool] = None,
        explicit: Optional[bool] = None,
        decade: Optional[str] = None,
        id: Optional[int] = None,
        include_children: Optional[bool] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        send_max_credits_header: Optional[bool] = None,
    ) -> AsyncPageIterator:
        """``async for track in client.tracks.iter(...)``. **Every page is charged.**"""
        opts = resolve_options(
            timeout=timeout, max_retries=max_retries, send_max_credits_header=send_max_credits_header
        )
        filters = {**_filters(locals()), "limit": limit}
        first = _LIST.build(query={**filters, "page": start_page}, options=opts)

        async def fetch(page: int) -> TrackPage:
            op = _LIST.build(query={**filters, "page": page}, options=opts)
            return cast(TrackPage, await self._pipeline.execute(op))

        async def project(key: Optional[str]) -> Optional[int]:
            return await self._pipeline.ensure_rate_card(_LIST.operation, key)

        return AsyncPageIterator(
            operation=_LIST.operation,
            fetch=fetch,
            cost_key_for=_page_cost_key(first),
            project=project,
            start_page=start_page,
            max_pages=max_pages,
            max_items=max_items,
            max_credits=max_credits,
        )

    async def get(
        self,
        track_id: TrackId,
        *,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> Track:
        """``GET /v1/tracks/{id}`` (async). See :meth:`TracksResource.get`."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        return cast(Track, await self._pipeline.execute(_GET.build(track_id=track_id, options=opts)))

    async def deep_search(
        self,
        query: str,
        *,
        page: int = 1,
        limit: int = 20,
        instrumental: bool = False,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> TrackPage:
        """``POST /v1/tracks/deep-search`` (async). See :meth:`TracksResource.deep_search`."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        body = {"query": query, "page": page, "limit": limit, "instrumental": instrumental}
        return cast(TrackPage, await self._pipeline.execute(_DEEP.build(body=body, options=opts)))

    def deep_search_iter(
        self,
        query: str,
        *,
        limit: int = 100,
        instrumental: bool = False,
        max_pages: int = 50,
        max_items: Optional[int] = None,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        send_max_credits_header: Optional[bool] = None,
    ) -> AsyncPageIterator:
        """Iterate Deep Search pages (async). See :meth:`TracksResource.deep_search_iter`."""
        opts = resolve_options(
            timeout=timeout, max_retries=max_retries, send_max_credits_header=send_max_credits_header
        )
        first = _DEEP.build(body={"query": query, "limit": limit, "instrumental": instrumental}, options=opts)
        base = dict(first.json_body or {})

        async def fetch(page: int) -> TrackPage:
            op = _DEEP.build(body={**base, "page": page}, options=opts)
            return cast(TrackPage, await self._pipeline.execute(op))

        async def project(key: Optional[str]) -> Optional[int]:
            return await self._pipeline.ensure_rate_card(_DEEP.operation, key)

        return AsyncPageIterator(
            operation=_DEEP.operation,
            fetch=fetch,
            cost_key_for=_page_cost_key(first),
            project=project,
            max_pages=max_pages,
            max_items=max_items,
            max_credits=max_credits,
        )

    async def search_similar(
        self,
        file_url: str,
        *,
        page: int = 1,
        limit: int = 20,
        instrumental: bool = False,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> TrackPage:
        """``POST /v1/tracks/search`` (async). See :meth:`TracksResource.search_similar`."""
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        body = {"file_url": file_url, "page": page, "limit": limit, "instrumental": instrumental}
        op = _SIMILAR.build(body=body, options=opts)
        if op.plain_http_file_url:
            self._pipeline.core.warn_plain_http_file_url()
        return cast(TrackPage, await self._pipeline.execute(op))

    def search_similar_iter(
        self,
        file_url: str,
        *,
        limit: int = 100,
        instrumental: bool = False,
        max_pages: int = 50,
        max_items: Optional[int] = None,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        send_max_credits_header: Optional[bool] = None,
    ) -> AsyncPageIterator:
        """Iterate Acoustic Similarity Search pages (async). **Every page is charged.**"""
        opts = resolve_options(
            timeout=timeout, max_retries=max_retries, send_max_credits_header=send_max_credits_header
        )
        first = _SIMILAR.build(body={"file_url": file_url, "limit": limit, "instrumental": instrumental}, options=opts)
        if first.plain_http_file_url:
            self._pipeline.core.warn_plain_http_file_url()
        base = dict(first.json_body or {})

        async def fetch(page: int) -> TrackPage:
            op = _SIMILAR.build(body={**base, "page": page}, options=opts)
            return cast(TrackPage, await self._pipeline.execute(op))

        async def project(key: Optional[str]) -> Optional[int]:
            return await self._pipeline.ensure_rate_card(_SIMILAR.operation, key)

        return AsyncPageIterator(
            operation=_SIMILAR.operation,
            fetch=fetch,
            cost_key_for=_page_cost_key(first),
            project=project,
            max_pages=max_pages,
            max_items=max_items,
            max_credits=max_credits,
        )

    async def sync(
        self,
        track_id: TrackId,
        *,
        external_export: bool = False,
        max_credits: Optional[int] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> SyncLink:
        """Async ``POST /v1/tracks/{id}/sync`` with single-flight coalescing per ``(id, mode)``.

        See :meth:`TracksResource.sync`. A follower runs its own credit guard
        before joining, waits at most its own ``timeout``, and never receives the
        leader's cancellation.
        """
        opts = resolve_options(
            options,
            timeout=timeout,
            max_credits=max_credits,
            max_retries=max_retries,
            idempotency_key=idempotency_key,
            send_max_credits_header=send_max_credits_header,
        )
        op = _SYNC.build(track_id=track_id, body={"external_export": external_export}, options=opts)
        key = (op.path, bool((op.json_body or {}).get("external_export")))
        loop = asyncio.get_running_loop()
        task = self._inflight.get(key)
        if task is not None and (task.done() or task.get_loop() is not loop):
            # A finished task, or one stranded on a closed / different event loop.
            if self._inflight.get(key) is task:
                del self._inflight[key]
            task = None
        if task is None:
            task = asyncio.ensure_future(self._pipeline.execute(op))
            self._inflight[key] = task

            def _done(t: asyncio.Future[SyncLink], k: tuple[str, bool] = key) -> None:
                if self._inflight.get(k) is t:
                    del self._inflight[k]
                if not t.cancelled():
                    t.exception()  # mark retrieved; callers re-raise it

            task.add_done_callback(_done)
            return await asyncio.shield(task)
        await self._pipeline.preflight(op)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=opts.timeout)
        except asyncio.TimeoutError:
            raise _follower_timeout(op, opts.timeout) from None
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise _leader_interrupted(op, exc) from None
            raise  # this follower itself was cancelled
        except Exception as exc:  # noqa: BLE001 - re-raised as the follower's own copy
            raise _for_follower(exc) from None
        except BaseException as exc:  # noqa: BLE001 - KeyboardInterrupt/SystemExit belong to the leader
            raise _leader_interrupted(op, exc) from None
