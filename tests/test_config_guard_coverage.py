"""Error branches of configuration, the credit guard and Retry-After parsing (H8 coverage gate)."""

from __future__ import annotations

import copy
import logging
import pickle
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import CANARY_KEY, MockTransport
from rightsfoundry._config import (
    NOT_GIVEN,
    NotGiven,
    ProxyConfig,
    _parse_proxy,
    build_config,
    normalize_host_pattern,
    safe_option_repr,
)
from rightsfoundry._guard import CreditGuard
from rightsfoundry._models import Credits
from rightsfoundry._retry import parse_retry_after


def cfg(**kw: Any) -> rf.ClientConfig:
    kw.setdefault("api_key", CANARY_KEY)
    return build_config(**kw)


def test_not_given_sentinel() -> None:
    assert NotGiven() is NOT_GIVEN and repr(NOT_GIVEN) == "NOT_GIVEN" and not NOT_GIVEN
    assert pickle.loads(pickle.dumps(NOT_GIVEN)) is NOT_GIVEN


def test_client_config_is_immutable_and_unpicklable() -> None:
    c = cfg()
    assert c.is_https
    with pytest.raises(AttributeError):
        c.max_retries = 5
    with pytest.raises(AttributeError):
        del c.max_retries
    for fn in (pickle.dumps, copy.copy):
        with pytest.raises(TypeError):
            fn(c)
    with pytest.raises(TypeError):
        c.__getstate__()
    with pytest.raises(TypeError):
        c.__reduce__()
    assert "c0ffee11" not in safe_option_repr(CANARY_KEY)


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"max_retries": 1.5}, "max_retries"),
        ({"allowed_hosts": [5]}, "allowed_hosts"),
        ({"allowed_hosts": ["bücher.example"]}, "ASCII"),
        ({"allowed_hosts": "example.com"}, "allowed_hosts"),
        ({"asset_hosts": "assets.example.com"}, "asset_hosts"),
        ({"asset_hosts": []}, "asset_hosts"),
        ({"base_url": 5}, "base_url"),
        ({"base_url": "https://:443"}, "base_url"),
        ({"base_url": "https://[::1"}, "base_url"),
        ({"base_url": "https://bücher.example", "allowed_hosts": ["xn--bcher-kva.example"]}, "base_url"),
        ({"base_url": "https://gw.example.com/a%2Fb", "allowed_hosts": ["gw.example.com"]}, "base_url"),
        ({"base_url": "https://1.2.3.999", "allowed_hosts": ["gw.example.com"]}, "base_url"),
        ({"custom_headers": ["X-A"]}, "custom_headers"),
        ({"custom_headers": {"X-A": 5}}, "custom_headers"),
        ({"custom_headers": {"X-A": "1", "x-a": "2"}}, "given twice"),
        ({"retryable_status_codes": "503"}, "retryable_status_codes"),
        ({"async_transport": object()}, "async_transport"),
        ({"ca_bundle": 5}, "ca_bundle"),
        ({"proxy_url": 5}, "proxy_url"),
        ({"proxy_url": "http://[::1"}, "proxy_url"),
        ({"proxy_url": "http://bücher.example:3128"}, "ASCII"),
    ],
)
def test_invalid_options_name_the_option(options: dict[str, Any], match: str) -> None:
    with pytest.raises(rf.ConfigurationError, match=match):
        cfg(**options)


def test_unauthenticated_with_a_key_and_explicit_none_key() -> None:
    with pytest.raises(rf.ConfigurationError):
        build_config(unauthenticated=True, api_key=CANARY_KEY)
    with pytest.raises(rf.ConfigurationError, match="api_key=None"):
        build_config(api_key=None)
    assert build_config(api_key=rf.Secret(CANARY_KEY)).api_key == rf.Secret(CANARY_KEY)


def test_proxy_parsing_details(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _parse_proxy(None, None) is None and _parse_proxy("", None) is None
    existing = ProxyConfig("p.example.com", 8080, None, ())
    assert _parse_proxy(existing, None) is existing
    bare = _parse_proxy("proxy.example.com:3128", "*")
    assert bare is not None and bare.scheme == "http" and not bare.applies_to("anything.example")
    with_no_proxy = _parse_proxy("http://proxy.example.com", ".internal,api.local")
    assert with_no_proxy is not None and with_no_proxy.port == 8080
    assert not with_no_proxy.applies_to("svc.internal") and with_no_proxy.applies_to("api.therightsfoundry.com")
    monkeypatch.setenv("NO_PROXY", "example.com")
    assert cfg(proxy_url="http://p.example.com").proxy.no_proxy == ("example.com",)  # type: ignore[union-attr]


def test_env_booleans_and_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_AUTO_CHECK_CREDITS", "0")
    assert cfg().auto_check_credits is False
    monkeypatch.setenv("TRF_AUTO_CHECK_CREDITS", "maybe")
    with pytest.raises(rf.ConfigurationError, match="TRF_AUTO_CHECK_CREDITS"):
        cfg()
    monkeypatch.delenv("TRF_AUTO_CHECK_CREDITS")
    monkeypatch.setenv("TRF_LOG", "loud")
    with pytest.raises(rf.ConfigurationError, match="TRF_LOG"):
        cfg()
    monkeypatch.setenv("TRF_LOG", "warn")
    cfg(logger=logging.getLogger("mine"))  # an explicit logger is left alone


def test_wildcard_host_matching_rules() -> None:
    assert normalize_host_pattern("allowed_hosts", "*.Example.COM.") == "*.example.com"
    with pytest.raises(rf.ConfigurationError, match="IP"):
        normalize_host_pattern("allowed_hosts", "127.0.0.1")


# --- the credit guard, unit level ---------------------------------------------------------------------------


def guard(**kw: Any) -> CreditGuard:
    options: dict[str, Any] = {
        "max_credits_per_request": None,
        "auto_check_credits": False,
        "credit_check_ttl_ms": 30_000,
        "rate_card_ttl_ms": 60_000,
        "guard_failure_mode": "closed",
        "min_bandwidth_bytes": None,
        "low_credit_warning_threshold": None,
        "on_low_credit": None,
        "logger": logging.getLogger("rightsfoundry"),
    }
    options.update(kw)
    return CreditGuard(**options)


def test_guard_free_calls_never_fetch_and_free_keys_cost_nothing() -> None:
    g = guard(max_credits_per_request=5)
    assert g.needs_fetch(charged=False, max_credits=5) is False
    assert g.project(None) == 0 and g.project("sync") is None


def test_guard_without_snapshot_in_open_mode_warns(caplog: pytest.LogCaptureFixture) -> None:
    g = guard(auto_check_credits=True, guard_failure_mode="open")
    g._rate_card = {"sync": 10}
    with caplog.at_level(logging.WARNING, logger="rightsfoundry"):
        assert g.enforce(operation="tracks.sync", cost_key="sync", max_credits=None) == 10
    assert any("no balance snapshot" in r.getMessage() for r in caplog.records)


def test_guard_ignores_malformed_spendable_header_and_awaitable_callbacks(caplog: pytest.LogCaptureFixture) -> None:
    async def callback(event: Any) -> None:
        return None

    g = guard(low_credit_warning_threshold=100, on_low_credit=callback)
    g.observe_headers({"x-trf-credits-spendable": "lots"})
    assert g._snapshot is None
    with caplog.at_level(logging.WARNING, logger="rightsfoundry"):
        g.observe_headers({"x-trf-credits-spendable": "5"})
    assert any("awaitable" in r.getMessage() for r in caplog.records)


def test_guard_low_credit_callback_errors_are_contained(caplog: pytest.LogCaptureFixture) -> None:
    def explode(event: Any) -> None:
        raise RuntimeError("callback bug")

    g = guard(low_credit_warning_threshold=100, on_low_credit=explode)
    with caplog.at_level(logging.WARNING, logger="rightsfoundry"):
        g.observe_insufficient(3)
    assert any("on_low_credit callback raised" in r.getMessage() for r in caplog.records)
    g.observe_insufficient(None)


def test_guard_bandwidth_floor() -> None:
    from helpers import credits_json

    g = guard(auto_check_credits=True, min_bandwidth_bytes=1_000_000)
    g.observe_credits(Credits.from_json(credits_json(1000)))
    with pytest.raises(rf.PreflightInsufficientCreditsError) as info:
        g.enforce(operation="tracks.sync", cost_key="sync", max_credits=None)
    assert info.value.resource == "bandwidth"


# --- Retry-After --------------------------------------------------------------------------------------------


def test_retry_after_dates() -> None:
    import datetime as dt

    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    assert parse_retry_after("Thu, 01 Jan 2026 00:00:10 GMT", now=now) == 10.0
    assert parse_retry_after("Thu, 01 Jan 2025 00:00:10 GMT", now=now) == 0.0
    assert parse_retry_after("Mon, 99 Jan 2026 00:00:00 GMT", now=now) is None
    assert parse_retry_after(None) is None and parse_retry_after("-1") is None and parse_retry_after("1.5") is None


def test_client_rejects_with_options_api_key_on_unauthenticated_clients() -> None:
    c = rf.Client.unauthenticated(transport=MockTransport())
    with pytest.raises(rf.ConfigurationError):
        c.with_options(api_key=CANARY_KEY)
    with pytest.raises(rf.ConfigurationError):
        rf.Client.unauthenticated(api_key=CANARY_KEY)
