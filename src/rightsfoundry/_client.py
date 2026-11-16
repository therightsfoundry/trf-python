"""``Client`` (sync, thread-safe) and ``AsyncClient`` (asyncio, task-safe)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Callable, NoReturn, Optional, TypeVar, Union

from ._config import NOT_GIVEN, ClientConfig, NotGiven, build_config
from ._endpoints import EndpointSpec, OperationSpec, match_endpoint
from ._errors import ConfigurationError, ValidationError
from ._guard import CreditGuard
from ._models import LowCreditEvent
from ._options import CallOptions, resolve_options
from ._pipeline import AsyncPipeline, SyncPipeline
from ._secret import Secret
from ._transport import AsyncExecutorTransport, HTTPTransport
from ._validation import validate_request_path, validate_request_query
from .resources import (
    AsyncCreditsResource,
    AsyncHealthResource,
    AsyncSyncLinksResource,
    AsyncTaxonomiesResource,
    AsyncTracksResource,
    CreditsResource,
    HealthResource,
    SyncLinksResource,
    TaxonomiesResource,
    TracksResource,
)

if TYPE_CHECKING:
    from ._transport import AsyncTransport, Transport

__all__ = ["AsyncClient", "Client"]

_Self = TypeVar("_Self", bound="_BaseClient")
_OptStr = Union[str, None, NotGiven]
_OptInt = Union[int, None, NotGiven]
_OptFloat = Union[float, None, NotGiven]
_Hook = Optional[Callable[[Any], Any]]

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})


def _request_op(
    method: str,
    path: str,
    *,
    query: Optional[Mapping[str, Any]],
    json_body: Any,
    options: CallOptions,
) -> OperationSpec:
    if not isinstance(method, str) or method.upper() not in _METHODS:
        raise ValidationError(f"method must be one of {', '.join(sorted(_METHODS))}")
    upper = method.upper()
    path = validate_request_path(path)
    encoded_query = validate_request_query(query)
    body: Optional[Mapping[str, Any]] = None
    if json_body is not None:
        if upper in ("GET", "HEAD"):
            raise ValidationError(f"{upper} requests take no json body")
        if not isinstance(json_body, Mapping):
            raise ValidationError("json must be a mapping (JSON object)")
        try:
            json.dumps(json_body, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"json is not serialisable as strict JSON: {exc}") from None
        body = dict(json_body)
    known = match_endpoint(upper, path)
    if known is not None:
        endpoint = known
        args: dict[str, Any] = dict(encoded_query)
        if body is not None:
            args.update(body)
        cost_key = endpoint.cost_key(args)
        name = f"request:{endpoint.operation}"
    else:
        endpoint = EndpointSpec(
            operation=f"request {upper} {path}",
            method=upper,
            path=path,
            auth="bearer",
            retry_class="charged",
            cost_key=lambda _a: "unknown",
        )
        cost_key = "unknown"
        name = endpoint.operation
    endpoint_for_call = EndpointSpec(
        operation=name,
        method=upper,
        path=endpoint.path,
        auth=endpoint.auth,
        retry_class=endpoint.retry_class,
        cost_key=endpoint.cost_key,
        allow_statuses=endpoint.allow_statuses,
        default_max_retries=endpoint.default_max_retries,
    )
    return OperationSpec(
        endpoint=endpoint_for_call,
        path=path,
        query=encoded_query,
        json_body=body,
        cost_key=cost_key,
        options=options,
        decoder=None,
    )


def _can_share(old: ClientConfig, new: ClientConfig, overrides: Mapping[str, Any]) -> bool:
    """A derived client may reuse the connection pool only if nothing the pool depends on changed."""
    return (
        "transport" not in overrides
        and "async_transport" not in overrides
        and new.ca_bundle == old.ca_bundle
        and new.proxy is old.proxy
        and new.max_connections == old.max_connections
        and new.allow_insecure_localhost == old.allow_insecure_localhost
        and new.environment == old.environment
    )


class _BaseClient:
    """Shared construction, validation and representation."""

    _config: ClientConfig
    _guard: CreditGuard
    _owns_transport: bool

    def __init__(
        self,
        *,
        api_key: Union[str, Secret, NotGiven, None] = NOT_GIVEN,
        environment: _OptStr = NOT_GIVEN,
        base_url: _OptStr = NOT_GIVEN,
        allowed_hosts: Iterable[str] = (),
        asset_hosts: Iterable[str] = ("assets.therightsfoundry.com",),
        ca_bundle: _OptStr = NOT_GIVEN,
        proxy_url: _OptStr = NOT_GIVEN,
        connect_timeout: _OptFloat = NOT_GIVEN,
        read_timeout: _OptFloat = NOT_GIVEN,
        write_timeout: float = 30.0,
        total_timeout: Optional[float] = None,
        max_retries: _OptInt = NOT_GIVEN,
        retry_initial_delay_ms: int = 500,
        retry_max_delay_ms: int = 8_000,
        jitter: str = "full",
        max_retry_after_ms: int = 30_000,
        retryable_status_codes: Iterable[int] = (408, 429, 500, 502, 503, 504),
        charged_retry_policy: str = "refund_backed",
        max_credits_per_request: _OptInt = NOT_GIVEN,
        low_credit_warning_threshold: _OptInt = NOT_GIVEN,
        on_low_credit: Optional[Callable[[LowCreditEvent], Any]] = None,
        auto_check_credits: Union[bool, NotGiven] = NOT_GIVEN,
        credit_check_ttl_ms: int = 30_000,
        rate_card_ttl_ms: int = 60_000,
        guard_failure_mode: str = "closed",
        min_bandwidth_bytes: Optional[int] = None,
        on_request: _Hook = None,
        on_response: _Hook = None,
        on_error: _Hook = None,
        hook_include_bodies: bool = False,
        custom_headers: Optional[Mapping[str, str]] = None,
        user_agent_suffix: _OptStr = NOT_GIVEN,
        transport: Optional[Transport] = None,
        async_transport: Optional[AsyncTransport] = None,
        logger: Optional[logging.Logger] = None,
        max_connections: int = 10,
        max_response_bytes: int = 10 * 1024 * 1024,
        max_download_bytes: int = 2 * 1024 * 1024 * 1024,
        dangerously_allow_browser: bool = False,
        allow_insecure_localhost: bool = False,
        max_concurrency: Optional[int] = None,
        taxonomies_cache_ttl_ms: int = 0,
    ) -> None:
        options = dict(locals())
        options.pop("self")
        options.pop("__class__", None)
        self._setup(build_config(**options), shared=None)

    # construction helpers ---------------------------------------------------------------

    def _setup(self, config: ClientConfig, shared: Optional[_BaseClient]) -> None:
        raise NotImplementedError

    def _make_guard(self, config: ClientConfig) -> CreditGuard:
        return CreditGuard(
            max_credits_per_request=config.max_credits_per_request,
            auto_check_credits=config.auto_check_credits,
            credit_check_ttl_ms=config.credit_check_ttl_ms,
            rate_card_ttl_ms=config.rate_card_ttl_ms,
            guard_failure_mode=config.guard_failure_mode,
            min_bandwidth_bytes=config.min_bandwidth_bytes,
            low_credit_warning_threshold=config.low_credit_warning_threshold,
            on_low_credit=config.on_low_credit,
            logger=config.logger,
        )

    @staticmethod
    def _default_transport(config: ClientConfig) -> HTTPTransport:
        return HTTPTransport(
            max_connections=config.max_connections,
            ca_bundle=config.ca_bundle,
            proxy=config.proxy,
            allow_plain_http_loopback=config.allow_insecure_localhost and config.environment != "production",
        )

    @classmethod
    def _from_config(cls: type[_Self], config: ClientConfig, shared: Optional[_BaseClient]) -> _Self:
        obj = cls.__new__(cls)
        obj._setup(config, shared)
        return obj

    @classmethod
    def unauthenticated(cls: type[_Self], **options: Any) -> _Self:
        """A client without an API key: only ``health`` and ``sync_links.download`` work."""
        if "api_key" in options and options["api_key"] is not None:
            raise ConfigurationError("unauthenticated() does not take an api_key")
        options.pop("api_key", None)
        return cls._from_config(build_config(unauthenticated=True, **options), None)

    def _merged_options(self, overrides: Mapping[str, Any]) -> ClientConfig:
        options = self._config.to_options()
        if self._config.unauthenticated and "api_key" in overrides:
            raise ConfigurationError("with_options cannot add an api_key to an unauthenticated client")
        if "environment" in overrides and "base_url" not in overrides:
            options.pop("base_url", None)
        if "base_url" in overrides and "environment" not in overrides:
            options.pop("environment", None)
        options.update(overrides)
        return build_config(unauthenticated=self._config.unauthenticated, **options)

    # public properties -----------------------------------------------------------------

    @property
    def config(self) -> ClientConfig:
        return self._config

    @property
    def environment(self) -> str:
        return self._config.environment

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def api_key_fingerprint(self) -> Optional[str]:
        """The key fingerprint shown in logs as ``[REDACTED fp=…]`` and on /dashboard/token."""
        return self._config.api_key.fingerprint if self._config.api_key is not None else None

    def _require_key(self, what: str) -> None:
        if self._config.api_key is None:
            raise ConfigurationError(
                f"{what} needs an API key; this client was created with unauthenticated() and "
                "exposes only health and sync_links"
            )

    def __repr__(self) -> str:
        key = self._config.api_key.redacted() if self._config.api_key is not None else "None"
        return (
            f"{type(self).__name__}(environment={self._config.environment!r}, "
            f"base_url={self._config.base_url!r}, api_key={key}, "
            f"max_retries={self._config.max_retries}, "
            f"charged_retry_policy={self._config.charged_retry_policy!r})"
        )

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError(f"{type(self).__name__} cannot be pickled (it holds the API key)")

    def __reduce_ex__(self, protocol: Any) -> NoReturn:
        raise TypeError(f"{type(self).__name__} cannot be pickled (it holds the API key)")

    def __getstate__(self) -> NoReturn:
        raise TypeError(f"{type(self).__name__} cannot be pickled (it holds the API key)")

    def __copy__(self) -> NoReturn:
        raise TypeError(f"{type(self).__name__} cannot be copied; use with_options()")

    def __deepcopy__(self, memo: Any) -> NoReturn:
        raise TypeError(f"{type(self).__name__} cannot be copied; use with_options()")


class Client(_BaseClient):
    """Synchronous, thread-safe client for The Rights Foundry API.

    >>> import rightsfoundry
    >>> client = rightsfoundry.Client()          # reads TRF_API_KEY once
    >>> page = client.tracks.list(mood="uplifting", limit=10)

    Use as a context manager or call :meth:`close` to release pooled sockets.
    """

    _pipeline: SyncPipeline
    _transport: Any

    def _setup(self, config: ClientConfig, shared: Optional[_BaseClient]) -> None:
        self._config = config
        if isinstance(shared, Client):
            transport = shared._transport
            self._owns_transport = False
        else:
            transport = config.transport or self._default_transport(config)
            self._owns_transport = config.transport is None
        self._transport = transport
        self._guard = self._make_guard(config)
        self._pipeline = SyncPipeline(config, transport, self._guard)
        self._tracks = TracksResource(self._pipeline)
        self._taxonomies = TaxonomiesResource(self._pipeline, config.taxonomies_cache_ttl_ms)
        self._credits = CreditsResource(self._pipeline)
        self.health = HealthResource(self._pipeline)
        self.sync_links = SyncLinksResource(config, transport)

    @property
    def tracks(self) -> TracksResource:
        """Catalogue search, Deep Search, Acoustic Similarity Search and sync licensing."""
        self._require_key("tracks")
        return self._tracks

    @property
    def taxonomies(self) -> TaxonomiesResource:
        self._require_key("taxonomies")
        return self._taxonomies

    @property
    def credits(self) -> CreditsResource:
        self._require_key("credits")
        return self._credits

    def with_options(self, **overrides: Any) -> Client:
        """A new client with some options changed, sharing this client's connection pool.

        Full validation re-runs on the merged configuration.
        """
        config = self._merged_options(overrides)
        return Client._from_config(config, self if _can_share(self._config, config, overrides) else None)

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        json: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        max_credits: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        max_retries: Optional[int] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> Any:
        """Low-level escape hatch for endpoints added before an SDK release.

        Goes through the full pipeline (auth, guards, redaction, bounded reads).
        ``path`` must start with ``/v1/`` and use only ``[A-Za-z0-9/_-]``. Unknown
        endpoints are retried as ``charged`` (the conservative class). Returns the
        parsed JSON body — an object, array or scalar — or ``None`` for an empty
        2xx answer (``204 No Content``, ``HEAD``).
        """
        op = _request_op(
            method,
            path,
            query=query,
            json_body=json,
            options=resolve_options(
                options,
                timeout=timeout,
                max_credits=max_credits,
                max_retries=max_retries,
                idempotency_key=idempotency_key,
                send_max_credits_header=send_max_credits_header,
            ),
        )
        if op.endpoint.auth == "bearer":
            self._require_key("request")
        return self._pipeline.execute(op)

    def close(self) -> None:
        """Close pooled connections (only if this client created the transport)."""
        if self._owns_transport:
            close = getattr(self._transport, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncClient(_BaseClient):
    """asyncio client. One per event loop; task-safe.

    Runs the same hardened ``http.client`` transport in a bounded thread pool
    (``max_connections`` workers), or a native ``async_transport`` you inject.

    >>> async with rightsfoundry.AsyncClient() as client:
    ...     credits = await client.credits.retrieve()
    """

    _pipeline: AsyncPipeline
    _async_transport: Any
    _sync_transport: Any
    _owns_sync_transport: bool

    def _setup(self, config: ClientConfig, shared: Optional[_BaseClient]) -> None:
        self._config = config
        self._owns_sync_transport = False
        if isinstance(shared, AsyncClient):
            async_transport = shared._async_transport
            self._sync_transport = shared._sync_transport
            self._owns_transport = False
        elif config.async_transport is not None:
            async_transport = config.async_transport
            self._sync_transport = None
            self._owns_transport = False
        else:
            sync_transport = config.transport or self._default_transport(config)
            self._sync_transport = sync_transport
            async_transport = AsyncExecutorTransport(sync_transport, max_workers=config.max_connections)
            # The executor wrapper is always ours; the sync transport only when we built it.
            self._owns_transport = True
            self._owns_sync_transport = config.transport is None
        self._async_transport = async_transport
        self._guard = self._make_guard(config)
        self._pipeline = AsyncPipeline(config, async_transport, self._guard)
        self._tracks = AsyncTracksResource(self._pipeline)
        self._taxonomies = AsyncTaxonomiesResource(self._pipeline, config.taxonomies_cache_ttl_ms)
        self._credits = AsyncCreditsResource(self._pipeline)
        self.health = AsyncHealthResource(self._pipeline)
        self.sync_links = AsyncSyncLinksResource(config, async_transport)

    @property
    def tracks(self) -> AsyncTracksResource:
        self._require_key("tracks")
        return self._tracks

    @property
    def taxonomies(self) -> AsyncTaxonomiesResource:
        self._require_key("taxonomies")
        return self._taxonomies

    @property
    def credits(self) -> AsyncCreditsResource:
        self._require_key("credits")
        return self._credits

    def with_options(self, **overrides: Any) -> AsyncClient:
        config = self._merged_options(overrides)
        return AsyncClient._from_config(config, self if _can_share(self._config, config, overrides) else None)

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        json: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        max_credits: Optional[int] = None,
        idempotency_key: Optional[str] = None,
        max_retries: Optional[int] = None,
        send_max_credits_header: Optional[bool] = None,
        options: Optional[CallOptions] = None,
    ) -> Any:
        """Async low-level escape hatch. See :meth:`Client.request`."""
        op = _request_op(
            method,
            path,
            query=query,
            json_body=json,
            options=resolve_options(
                options,
                timeout=timeout,
                max_credits=max_credits,
                max_retries=max_retries,
                idempotency_key=idempotency_key,
                send_max_credits_header=send_max_credits_header,
            ),
        )
        if op.endpoint.auth == "bearer":
            self._require_key("request")
        return await self._pipeline.execute(op)

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        """Release this client's thread pool and, if it created it, the connection pool.

        A ``transport=`` you injected is never closed: you own its lifecycle.
        """
        if not self._owns_transport:
            return
        transport = self._async_transport
        if isinstance(transport, AsyncExecutorTransport):
            transport.close(close_transport=self._owns_sync_transport)
        else:
            close = getattr(transport, "close", None)
            if callable(close):
                close()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
