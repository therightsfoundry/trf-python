"""The declarative endpoint registry — the single source of truth for the wire contract.

Every v1 operation the SDK wraps is described once, here: HTTP method, path
template, auth mode, retry class, how its price is chosen, which query
parameters and body fields it accepts, how its response is decoded and which
non-2xx statuses are answers rather than errors.

The registry is executable: :meth:`EndpointSpec.build` validates a call's
arguments against the spec and returns the :class:`OperationSpec` the pipeline
sends. Resources contain no per-endpoint wire logic of their own.
``scripts/check_openapi_sync.py`` and the test suite introspect :data:`ENDPOINTS`
to prove the SDK and ``openapi.yaml`` agree.

Adding an endpoint the API shipped
----------------------------------
1. Add an :class:`EndpointSpec` to ``_SPECS`` (query params / body fields, a
   decoder and a cost-key rule).
2. Add a model in ``_models.py`` — declare its fields; decoding is automatic.
3. Add a resource method pair (sync + async) that calls ``spec.build(...)``.
4. ``python scripts/check_openapi_sync.py`` + ``pytest`` (parity tests check the pair).
"""

from __future__ import annotations

import re
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional
from urllib.parse import quote

from ._errors import UnexpectedResponseError, ValidationError
from ._models import (
    Credits,
    HealthStatus,
    LivenessStatus,
    ReadinessStatus,
    SyncLink,
    Taxonomies,
    Track,
    TrackPage,
)
from ._options import DEFAULT_OPTIONS, CallOptions
from ._params import LIST_QUERY_PARAMS, BodyField, QueryKind, QueryParam
from ._validation import build_body, build_query, validate_track_id

__all__ = [
    "ENDPOINTS",
    "LIST_QUERY_PARAMS",
    "PUBLIC_COST_KEYS",
    "AuthMode",
    "BodyField",
    "EndpointSpec",
    "OperationSpec",
    "QueryKind",
    "QueryParam",
    "RetryClass",
    "match_endpoint",
]

AuthMode = Literal["bearer", "none"]
RetryClass = Literal["charged", "sync", "free"]
#: ``decoder(parsed_json, meta)``; ``meta`` has ``request_id``, ``retried``, ``http_status``, ``retry_after``.
Decoder = Callable[[Any, Mapping[str, Any]], Any]

#: Public cost keys published by ``GET /v1/credits`` (``costs``). Checked against
#: the OpenAPI document by ``scripts/check_openapi_sync.py``.
PUBLIC_COST_KEYS: tuple[str, ...] = (
    "tracks_list",
    "tracks_search_first_page",
    "tracks_search_next_page",
    "track_detail",
    "deep_search_first_page",
    "deep_search_next_page",
    "file_search_first_page",
    "file_search_next_page",
    "sync",
    "export_sync",
    "stream",
)


# --- cost-key rules ------------------------------------------------------------------


def _page_number(args: Mapping[str, Any]) -> int:
    try:
        return int(args.get("page") or 1)
    except (TypeError, ValueError):
        return 1


def _first_or_next(first: str, nxt: str) -> Callable[[Mapping[str, Any]], Optional[str]]:
    def resolve(args: Mapping[str, Any]) -> Optional[str]:
        return first if _page_number(args) <= 1 else nxt

    return resolve


def _list_cost(args: Mapping[str, Any]) -> Optional[str]:
    search = args.get("search")
    if not isinstance(search, str) or not search.strip():
        return "tracks_list"
    return _first_or_next("tracks_search_first_page", "tracks_search_next_page")(args)


def _sync_cost(args: Mapping[str, Any]) -> Optional[str]:
    return "export_sync" if args.get("external_export") is True else "sync"


def _const(key: Optional[str]) -> Callable[[Mapping[str, Any]], Optional[str]]:
    return lambda _args: key


# --- response decoders -----------------------------------------------------------------


def _data(payload: Any) -> Any:
    if not isinstance(payload, Mapping) or "data" not in payload:
        raise UnexpectedResponseError("unexpected response shape: missing 'data'")
    return payload["data"]


def _page_decoder(payload: Any, meta: Mapping[str, Any]) -> TrackPage:
    return TrackPage.from_json(payload, request_id=meta["request_id"], retried=meta["retried"])


def _track_decoder(payload: Any, meta: Mapping[str, Any]) -> Track:
    return Track.from_json(_data(payload))


def _sync_decoder(payload: Any, meta: Mapping[str, Any]) -> SyncLink:
    return SyncLink.from_json(_data(payload), request_id=meta["request_id"], retried=meta["retried"])


def _taxonomies_decoder(payload: Any, meta: Mapping[str, Any]) -> Taxonomies:
    return Taxonomies.from_json(_data(payload), request_id=meta["request_id"])


def _credits_decoder(payload: Any, meta: Mapping[str, Any]) -> Credits:
    return Credits.from_json(payload, request_id=meta["request_id"])


def _status_decoder(model: Any) -> Decoder:
    def decode(payload: Any, meta: Mapping[str, Any]) -> Any:
        return model.from_json(
            payload,
            http_status=meta["http_status"],
            retry_after=meta["retry_after"],
            request_id=meta["request_id"],
        )

    return decode


# --- body fields ---------------------------------------------------------------------

_PAGE = BodyField("page", "int", default=1, minimum=1, description="1-based page number")
_LIMIT = BodyField("limit", "int", default=20, minimum=1, maximum=100, description="results per page (1-100)")
_INSTRUMENTAL = BodyField("instrumental", "bool", default=False, description="instrumental tracks only")


# --- specs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointSpec:
    """Static, executable description of one API operation."""

    operation: str
    method: str
    path: str
    auth: AuthMode
    retry_class: RetryClass
    cost_key: Callable[[Mapping[str, Any]], Optional[str]] = field(compare=False)
    query_params: tuple[QueryParam, ...] = ()
    body: tuple[BodyField, ...] = ()
    #: Non-2xx statuses that are answers, not errors (health 503 = "degraded").
    allow_statuses: frozenset[int] = frozenset()
    response_model: str = ""
    decoder: Optional[Decoder] = field(default=None, compare=False, repr=False)
    #: Retry budget when the caller sets none (``None`` = the client's ``max_retries``).
    default_max_retries: Optional[int] = None
    #: False for operations ``openapi.yaml`` does not list yet.
    in_openapi: bool = True
    summary: str = ""

    @property
    def body_fields(self) -> tuple[str, ...]:
        """Names of the JSON body properties this operation sends."""
        return tuple(f.name for f in self.body)

    @property
    def path_regex(self) -> re.Pattern[str]:
        pattern = "[0-9]{1,15}".join(re.escape(part) for part in self.path.split("{id}"))
        return re.compile(f"^{pattern}\\Z")

    @property
    def charged(self) -> bool:
        return self.retry_class != "free"

    def build(
        self,
        *,
        track_id: Any = None,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        options: Optional[CallOptions] = None,
    ) -> OperationSpec:
        """Validate one call's arguments against this spec and return what the pipeline sends.

        ``query`` uses the Python keyword names of :attr:`query_params`; ``body``
        uses :attr:`body` field names. Nothing is sent: a bad argument raises
        :class:`~rightsfoundry.ValidationError` before any network I/O.
        """
        path = self.path
        if "{id}" in path:
            if track_id is None:
                raise ValidationError(f"{self.operation} needs a track id")
            path = path.replace("{id}", quote(validate_track_id(track_id), safe=""))
        elif track_id is not None:
            raise ValidationError(f"{self.operation} takes no track id")
        encoded = build_query(query or {}, self.query_params, self.operation)
        if self.body:
            json_body, plain_http = build_body(body or {}, self.body, self.operation)
        elif body:
            raise ValidationError(f"{self.operation} takes no request body")
        else:
            json_body, plain_http = None, False
        return OperationSpec(
            endpoint=self,
            path=path,
            query=encoded,
            json_body=json_body,
            cost_key=self.cost_key({**dict(encoded), **(json_body or {})}),
            options=options or DEFAULT_OPTIONS,
            decoder=self.decoder,
            plain_http_file_url=plain_http,
        )


@dataclass(frozen=True)
class OperationSpec:
    """One call, ready for the pipeline: resolved path, encoded query, typed body, options."""

    endpoint: EndpointSpec
    path: str
    query: tuple[tuple[str, str], ...] = ()
    json_body: Optional[Mapping[str, Any]] = None
    cost_key: Optional[str] = None
    options: CallOptions = DEFAULT_OPTIONS
    decoder: Optional[Decoder] = field(default=None, compare=False)
    #: For ``client.request`` on a path the registry does not know.
    operation_name: Optional[str] = None
    #: True when a ``file_url`` uses plain ``http://`` (the resource warns once).
    plain_http_file_url: bool = field(default=False, compare=False)

    @property
    def operation(self) -> str:
        return self.operation_name or self.endpoint.operation

    @property
    def max_credits(self) -> Optional[int]:
        return self.options.max_credits

    @property
    def timeout(self) -> Optional[float]:
        return self.options.timeout

    @property
    def idempotency_key(self) -> Optional[str]:
        return self.options.idempotency_key


_SPECS: tuple[EndpointSpec, ...] = (
    EndpointSpec(
        operation="tracks.list",
        method="GET",
        path="/v1/tracks",
        auth="bearer",
        retry_class="charged",
        cost_key=_list_cost,
        query_params=LIST_QUERY_PARAMS,
        response_model="TrackPage",
        decoder=_page_decoder,
        summary="Browse, filter or text-search the catalogue",
    ),
    EndpointSpec(
        operation="tracks.deep_search",
        method="POST",
        path="/v1/tracks/deep-search",
        auth="bearer",
        retry_class="charged",
        cost_key=_first_or_next("deep_search_first_page", "deep_search_next_page"),
        body=(
            BodyField("query", "text", required=True, max_length=500, description="the brief"),
            _PAGE,
            _LIMIT,
            _INSTRUMENTAL,
        ),
        response_model="TrackPage",
        decoder=_page_decoder,
        summary="Deep Search: natural-language brief",
    ),
    EndpointSpec(
        operation="tracks.search_similar",
        method="POST",
        path="/v1/tracks/search",
        auth="bearer",
        retry_class="charged",
        cost_key=_first_or_next("file_search_first_page", "file_search_next_page"),
        body=(
            BodyField("file_url", "file_url", required=True, description="public .mp3/.wav reference recording"),
            _PAGE,
            _LIMIT,
            _INSTRUMENTAL,
        ),
        response_model="TrackPage",
        decoder=_page_decoder,
        summary="Acoustic Similarity Search by reference recording",
    ),
    EndpointSpec(
        operation="tracks.sync",
        method="POST",
        path="/v1/tracks/{id}/sync",
        auth="bearer",
        retry_class="sync",
        cost_key=_sync_cost,
        body=(BodyField("external_export", "bool", default=False, description="Export Sync (offline / outside use)"),),
        response_model="SyncLink",
        decoder=_sync_decoder,
        summary="Mint a signed 1-hour link to the WAV master (sync licence)",
    ),
    EndpointSpec(
        operation="tracks.get",
        method="GET",
        path="/v1/tracks/{id}",
        auth="bearer",
        retry_class="charged",
        cost_key=_const("track_detail"),
        response_model="Track",
        decoder=_track_decoder,
        summary="One track with every alternate version",
    ),
    EndpointSpec(
        operation="taxonomies.list",
        method="GET",
        path="/v1/taxonomies",
        auth="bearer",
        retry_class="free",
        cost_key=_const(None),
        response_model="Taxonomies",
        decoder=_taxonomies_decoder,
        summary="Every facet value with track counts (free)",
    ),
    EndpointSpec(
        operation="credits.retrieve",
        method="GET",
        path="/v1/credits",
        auth="bearer",
        retry_class="free",
        cost_key=_const(None),
        response_model="Credits",
        decoder=_credits_decoder,
        summary="Balance, overdraft and your live rate card (free)",
    ),
    EndpointSpec(
        operation="health.check",
        method="GET",
        path="/v1/health",
        auth="none",
        retry_class="free",
        cost_key=_const(None),
        allow_statuses=frozenset({503}),
        response_model="HealthStatus",
        decoder=_status_decoder(HealthStatus),
        default_max_retries=0,
        in_openapi=False,
        summary="Health probe (200 healthy / 503 degraded)",
    ),
    EndpointSpec(
        operation="health.live",
        method="GET",
        path="/v1/health/live",
        auth="none",
        retry_class="free",
        cost_key=_const(None),
        allow_statuses=frozenset({503}),
        response_model="LivenessStatus",
        decoder=_status_decoder(LivenessStatus),
        default_max_retries=0,
        in_openapi=False,
        summary="Liveness probe (zero I/O)",
    ),
    EndpointSpec(
        operation="health.ready",
        method="GET",
        path="/v1/health/ready",
        auth="none",
        retry_class="free",
        cost_key=_const(None),
        allow_statuses=frozenset({503}),
        response_model="ReadinessStatus",
        decoder=_status_decoder(ReadinessStatus),
        default_max_retries=0,
        in_openapi=False,
        summary="Readiness probe (database + billing binding)",
    ),
)

#: Read-only registry: operation name → spec.
ENDPOINTS: Mapping[str, EndpointSpec] = types.MappingProxyType({spec.operation: spec for spec in _SPECS})


def match_endpoint(method: str, path: str) -> Optional[EndpointSpec]:
    """Find the registered endpoint for a concrete ``(method, path)`` (for ``client.request``)."""
    upper = method.upper()
    for spec in _SPECS:
        if spec.method == upper and spec.path_regex.fullmatch(path):
            return spec
    return None
