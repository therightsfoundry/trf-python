"""Parity between the sync and async surfaces, the registry and the client constructors.

These tests make API evolution cheap and safe: a new filter, option or method
added to one side only fails here instead of drifting silently.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

import pytest

import rightsfoundry as rf
from rightsfoundry._config import DEFAULTS
from rightsfoundry._endpoints import ENDPOINTS, LIST_QUERY_PARAMS
from rightsfoundry.resources import (
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

PAIRS = [
    (TracksResource, AsyncTracksResource),
    (TaxonomiesResource, AsyncTaxonomiesResource),
    (CreditsResource, AsyncCreditsResource),
    (HealthResource, AsyncHealthResource),
    (SyncLinksResource, AsyncSyncLinksResource),
    (rf.Client, rf.AsyncClient),
]

#: Methods whose names differ by design between the two clients.
_ASYMMETRIC = {"aclose", "close"}


def _public_methods(cls: type[Any]) -> dict[str, Any]:
    return {
        name: member
        for name, member in inspect.getmembers(cls, predicate=inspect.isfunction)
        if not name.startswith("_") and name not in _ASYMMETRIC
    }


def _shape(fn: Any) -> list[tuple[str, Any, Any]]:
    sig = inspect.signature(fn)
    return [(p.name, p.kind, p.default) for p in sig.parameters.values()]


@pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS, ids=lambda c: c.__name__)
def test_sync_and_async_have_the_same_methods_and_signatures(sync_cls: type[Any], async_cls: type[Any]) -> None:
    sync_methods = _public_methods(sync_cls)
    async_methods = _public_methods(async_cls)
    assert set(sync_methods) == set(async_methods)
    for name, fn in sync_methods.items():
        assert _shape(fn) == _shape(async_methods[name]), f"{sync_cls.__name__}.{name}"
        returns_iterator = name == "iter" or name.endswith("_iter") or name == "with_options"
        if not returns_iterator and sync_cls is not rf.Client:
            assert inspect.iscoroutinefunction(async_methods[name]), f"{async_cls.__name__}.{name} must be async"


@pytest.mark.parametrize("method", ["list", "iter"])
def test_list_and_iter_filters_match_the_registry(method: str) -> None:
    params = inspect.signature(getattr(TracksResource, method)).parameters
    registry = {p.name for p in LIST_QUERY_PARAMS}
    filters = registry - {"page", "limit"}
    assert filters <= set(params), sorted(filters - set(params))
    extra = (
        set(params)
        - registry
        - {
            "self",
            "max_credits",
            "timeout",
            "max_retries",
            "idempotency_key",
            "send_max_credits_header",
            "options",
            "start_page",
            "max_pages",
            "max_items",
        }
    )
    assert extra == set(), sorted(extra)


def test_charged_methods_take_max_credits_and_every_method_takes_call_options() -> None:
    option_names = {"timeout", "max_retries", "send_max_credits_header"}
    for cls in (TracksResource, TaxonomiesResource, CreditsResource, HealthResource):
        for name, fn in _public_methods(cls).items():
            params = set(inspect.signature(fn).parameters)
            assert option_names <= params, f"{cls.__name__}.{name}"
            single_call = not (name == "iter" or name.endswith("_iter"))
            if single_call:
                assert {"options", "idempotency_key"} <= params, f"{cls.__name__}.{name}"
            if cls is TracksResource:
                assert "max_credits" in params, f"{cls.__name__}.{name}"


def test_call_options_fields_match_the_keywords() -> None:
    fields = set(inspect.signature(rf.CallOptions).parameters)
    assert fields == {"timeout", "max_credits", "max_retries", "idempotency_key", "send_max_credits_header"}
    request = set(inspect.signature(rf.Client.request).parameters)
    assert fields <= request and "options" in request


@pytest.mark.parametrize("cls", [rf.Client, rf.AsyncClient])
def test_constructor_defaults_match_the_config_defaults(cls: type[Any]) -> None:
    params = {name: p.default for name, p in inspect.signature(cls.__init__).parameters.items() if name != "self"}
    assert set(params) == set(DEFAULTS)
    for name, default in DEFAULTS.items():
        constructor = params[name]
        if isinstance(default, tuple) or isinstance(constructor, tuple):
            assert tuple(constructor) == tuple(default), name
        else:
            assert constructor == default or constructor is default, name


def test_every_registry_endpoint_is_wired_to_a_decoder_and_a_method() -> None:
    method_for: dict[str, Callable[..., Any]] = {
        "tracks.list": TracksResource.list,
        "tracks.get": TracksResource.get,
        "tracks.deep_search": TracksResource.deep_search,
        "tracks.search_similar": TracksResource.search_similar,
        "tracks.sync": TracksResource.sync,
        "taxonomies.list": TaxonomiesResource.list,
        "credits.retrieve": CreditsResource.retrieve,
        "health.check": HealthResource.check,
        "health.live": HealthResource.live,
        "health.ready": HealthResource.ready,
    }
    assert set(method_for) == set(ENDPOINTS)
    for name, spec in ENDPOINTS.items():
        assert spec.decoder is not None, name
        assert spec.response_model and hasattr(rf, spec.response_model), name
        assert spec.summary, name
        for field in spec.body:
            assert field.name in inspect.signature(method_for[name]).parameters, (name, field.name)
        if spec.operation.startswith("health."):
            assert spec.default_max_retries == 0


def test_build_rejects_misuse() -> None:
    with pytest.raises(rf.ValidationError, match="needs a track id"):
        ENDPOINTS["tracks.get"].build()
    with pytest.raises(rf.ValidationError, match="takes no track id"):
        ENDPOINTS["tracks.list"].build(track_id=1)
    with pytest.raises(rf.ValidationError, match="takes no request body"):
        ENDPOINTS["tracks.list"].build(body={"x": 1})
    with pytest.raises(rf.ValidationError, match="unknown body field"):
        ENDPOINTS["tracks.sync"].build(track_id=1, body={"surprise": True})
    with pytest.raises(rf.ValidationError, match="query is required"):
        ENDPOINTS["tracks.deep_search"].build(body={})
    with pytest.raises(rf.ValidationError, match="limit must be at most 100"):
        ENDPOINTS["tracks.deep_search"].build(body={"query": "x", "limit": 101})
    op = ENDPOINTS["tracks.search_similar"].build(body={"file_url": "http://cdn.example.com/a.wav", "page": 3})
    assert op.plain_http_file_url and op.cost_key == "file_search_next_page" and op.timeout is None
    assert op.max_credits is None and op.idempotency_key is None and op.operation == "tracks.search_similar"
