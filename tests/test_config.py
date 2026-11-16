"""Configuration: precedence, validation, base_url (SEC-08), headers (SEC-14), immutability."""

from __future__ import annotations

import logging
import pickle
import sys
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import CANARY_KEY, OTHER_KEY, AsyncMockTransport, MockTransport, Reply, client, credits_json
from rightsfoundry._config import build_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "TRF_API_KEY",
        "TRF_ENVIRONMENT",
        "TRF_BASE_URL",
        "TRF_CA_BUNDLE",
        "TRF_CONNECT_TIMEOUT",
        "TRF_READ_TIMEOUT",
        "TRF_MAX_RETRIES",
        "TRF_MAX_CREDITS_PER_REQUEST",
        "TRF_LOW_CREDIT_WARNING_THRESHOLD",
        "TRF_AUTO_CHECK_CREDITS",
        "TRF_USER_AGENT_SUFFIX",
        "TRF_LOG",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def cfg(**kw: Any) -> rf.ClientConfig:
    kw.setdefault("api_key", CANARY_KEY)
    return build_config(**kw)


# --- api key -------------------------------------------------------------------------


def test_key_from_env_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_API_KEY", f"  {CANARY_KEY.upper()}\n")
    c = rf.Client(transport=MockTransport())
    monkeypatch.setenv("TRF_API_KEY", OTHER_KEY)
    assert c.config.api_key is not None
    assert c.config.api_key == rf.Secret(CANARY_KEY)  # trimmed + lowercased, read once
    assert c.with_options(max_retries=0).config.api_key == rf.Secret(CANARY_KEY)


def test_explicit_none_does_not_fall_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_API_KEY", CANARY_KEY)
    with pytest.raises(rf.ConfigurationError, match="does not fall back"):
        rf.Client(api_key=None)


def test_missing_key_fails_at_construction() -> None:
    with pytest.raises(rf.ConfigurationError, match="api_key is required"):
        rf.Client()


def test_explicit_key_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_API_KEY", OTHER_KEY)
    assert build_config(api_key=CANARY_KEY).api_key == rf.Secret(CANARY_KEY)


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ("AB12", "public code"),
        ("abc", "exactly 128"),
        (CANARY_KEY[:-1], "exactly 128"),
        (CANARY_KEY[:-1] + "g", "hexadecimal"),
        (CANARY_KEY[:64] + "\n" + CANARY_KEY[65:], "hexadecimal"),
        ("", "empty"),
        (123, "string"),
    ],
)
def test_key_validation_messages_never_echo_the_value(value: Any, fragment: str) -> None:
    with pytest.raises(rf.ConfigurationError) as info:
        build_config(api_key=value)
    assert fragment in str(info.value)
    if isinstance(value, str) and len(value) > 8:
        assert value.strip()[:20] not in str(info.value)


def test_secret_instance_is_accepted() -> None:
    assert build_config(api_key=rf.Secret(CANARY_KEY)).api_key == rf.Secret(CANARY_KEY)


# --- environment & base_url ------------------------------------------------------------


def test_defaults() -> None:
    c = cfg()
    assert c.environment == "production"
    assert c.base_url == "https://api.therightsfoundry.com"
    assert c.connect_timeout == 10.0 and c.read_timeout == 60.0 and c.write_timeout == 30.0
    assert c.max_retries == 2 and c.jitter == "full" and c.charged_retry_policy == "refund_backed"
    assert c.retryable_status_codes == frozenset({408, 429, 500, 502, 503, 504})
    assert c.max_response_bytes == 10 * 1024 * 1024
    assert c.max_download_bytes == 2 * 1024**3
    assert c.asset_hosts == ("assets.therightsfoundry.com",)
    assert c.guard_failure_mode == "closed"
    assert c.auto_check_credits is False
    assert c.taxonomies_cache_ttl_ms == 0


def test_staging_environment_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="rightsfoundry"):
        c = cfg(environment="staging")
    assert c.base_url == "https://api-staging.therightsfoundry.com"
    assert any("STAGING" in r.getMessage() for r in caplog.records)


def test_environment_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_ENVIRONMENT", "staging")
    assert cfg().base_url == "https://api-staging.therightsfoundry.com"


def test_base_url_staging_host_infers_environment() -> None:
    c = cfg(base_url="https://api-staging.therightsfoundry.com/")
    assert c.environment == "staging"
    assert c.base_url == "https://api-staging.therightsfoundry.com"


def test_environment_and_base_url_disagree() -> None:
    with pytest.raises(rf.ConfigurationError, match="belongs to environment"):
        cfg(environment="production", base_url="https://api-staging.therightsfoundry.com")


def test_env_base_url_cannot_point_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_BASE_URL", "https://evil.example.com")
    with pytest.raises(rf.ConfigurationError, match="allowed_hosts"):
        cfg()


def test_allowed_hosts_gateway_with_prefix() -> None:
    c = cfg(base_url="https://gw.example.com:8443/trf/", allowed_hosts=["gw.example.com"])
    assert c.base_url == "https://gw.example.com:8443/trf"


def test_allowed_hosts_wildcard_matches_one_label() -> None:
    assert cfg(base_url="https://a.example.com", allowed_hosts=["*.example.com"]).api_host == "a.example.com"
    with pytest.raises(rf.ConfigurationError):
        cfg(base_url="https://a.b.example.com", allowed_hosts=["*.example.com"])
    with pytest.raises(rf.ConfigurationError):
        cfg(base_url="https://example.com", allowed_hosts=["*.example.com"])


@pytest.mark.parametrize(
    "url",
    [
        "http://api.therightsfoundry.com",
        "https://user:pass@api.therightsfoundry.com",
        "https://api.therightsfoundry.com@evil.example.com",
        "https://api.therightsfoundry.com?x=1",
        "https://api.therightsfoundry.com#frag",
        "https://api.therightsfoundry.com:8443",
        "https://api.therightsfoundry.com/v1",
        "https://169.254.169.254",
        "https://10.0.0.5",
        "https://2130706433",
        "https://0x7f000001",
        "https://0177.0.0.1",
        "https://127.1",
        "https://[::1]",
        "https://[fd00::1]",
        "https://аpi.therightsfoundry.com",  # Cyrillic а (homograph)
        "https://api.therightsfoundry.com\\@evil.com",
        "ftp://api.therightsfoundry.com",
        "javascript:alert(1)",
        "//api.therightsfoundry.com",
        "https://api.therightsfoundry.com.evil.com",
        "https://evil.com/api.therightsfoundry.com",
        "https:// api.therightsfoundry.com",
        "",
    ],
)
def test_base_url_rejections(url: str) -> None:
    with pytest.raises(rf.ConfigurationError):
        cfg(base_url=url)


def test_base_url_normalisation() -> None:
    c = cfg(base_url="HTTPS://API.TheRightsFoundry.COM./")
    assert c.base_url == "https://api.therightsfoundry.com"


def test_ip_allowed_hosts_entries_rejected() -> None:
    with pytest.raises(rf.ConfigurationError, match="not IP"):
        cfg(allowed_hosts=["10.0.0.1"])


def test_loopback_requires_code_opt_in_and_non_production() -> None:
    with pytest.raises(rf.ConfigurationError, match="allow_insecure_localhost"):
        cfg(base_url="http://127.0.0.1:8080", environment="staging")
    with pytest.raises(rf.ConfigurationError, match="production"):
        cfg(base_url="http://127.0.0.1:8080", allow_insecure_localhost=True)
    c = cfg(base_url="http://127.0.0.1:8080", environment="staging", allow_insecure_localhost=True)
    assert c.base_url == "http://127.0.0.1:8080"
    c6 = cfg(base_url="http://[::1]:9000", environment="staging", allow_insecure_localhost=True)
    assert c6.base_url == "http://[::1]:9000"


def test_no_env_can_enable_insecure_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("TRF_ENVIRONMENT", "staging")
    with pytest.raises(rf.ConfigurationError):
        cfg()


# --- numeric options -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("connect_timeout", 0.05),
        ("connect_timeout", 121),
        ("connect_timeout", True),
        ("read_timeout", 0.5),
        ("read_timeout", 601),
        ("write_timeout", 0),
        ("total_timeout", 10.0),  # < read_timeout
        ("max_retries", -1),
        ("max_retries", 11),
        ("retry_initial_delay_ms", 10),
        ("retry_max_delay_ms", 100),  # < initial 500
        ("retry_max_delay_ms", 300_001),
        ("jitter", "random"),
        ("max_retry_after_ms", 600_001),
        ("retryable_status_codes", [400]),
        ("retryable_status_codes", [404]),
        ("retryable_status_codes", [428]),
        ("retryable_status_codes", [600]),
        ("charged_retry_policy", "sometimes"),
        ("max_credits_per_request", -1),
        ("credit_check_ttl_ms", 999),
        ("rate_card_ttl_ms", 3_600_001),
        ("guard_failure_mode", "ajar"),
        ("max_connections", 0),
        ("max_connections", 257),
        ("max_response_bytes", 1024),
        ("max_response_bytes", 101 * 1024 * 1024),
        ("max_download_bytes", 1024),
        ("taxonomies_cache_ttl_ms", -1),
        ("on_request", "not callable"),
        ("hook_include_bodies", "yes"),
        ("user_agent_suffix", "bad\nsuffix"),
        ("user_agent_suffix", "x" * 129),
        ("max_concurrency", 0),
        ("logger", "rightsfoundry"),
        ("transport", object()),
        ("ca_bundle", "/nonexistent/ca.pem"),
        ("asset_hosts", []),
        ("allowed_hosts", "api.example.com"),
        ("auto_check_credits", "true"),
    ],
)
def test_option_rejections(option: str, value: Any) -> None:
    with pytest.raises(rf.ConfigurationError, match=option.split("_")[0]):
        cfg(**{option: value})


def test_unknown_option_rejected() -> None:
    with pytest.raises(rf.ConfigurationError, match="unknown option"):
        cfg(retries=3)


def test_retryable_408_and_5xx_accepted() -> None:
    assert cfg(retryable_status_codes=[408, 429, 520]).retryable_status_codes == frozenset({408, 429, 520})


def test_env_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("TRF_READ_TIMEOUT", "30.5")
    monkeypatch.setenv("TRF_MAX_RETRIES", "4")
    monkeypatch.setenv("TRF_MAX_CREDITS_PER_REQUEST", "25")
    monkeypatch.setenv("TRF_LOW_CREDIT_WARNING_THRESHOLD", "-100")
    monkeypatch.setenv("TRF_AUTO_CHECK_CREDITS", "1")
    monkeypatch.setenv("TRF_USER_AGENT_SUFFIX", "acme-editor/4.2")
    c = cfg()
    assert c.connect_timeout == 5.0 and c.read_timeout == 30.5 and c.max_retries == 4
    assert c.max_credits_per_request == 25 and c.low_credit_warning_threshold == -100
    assert c.auto_check_credits is True and c.user_agent_suffix == "acme-editor/4.2"


def test_kwargs_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_MAX_RETRIES", "4")
    assert cfg(max_retries=0).max_retries == 0


@pytest.mark.parametrize(
    ("name", "value"), [("TRF_AUTO_CHECK_CREDITS", "yes"), ("TRF_MAX_RETRIES", "two"), ("TRF_LOG", "loud")]
)
def test_bad_env_values(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(rf.ConfigurationError):
        cfg()


def test_trf_log_debug_enables_library_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("rightsfoundry")
    before_level, before_handlers = logger.level, list(logger.handlers)
    try:
        monkeypatch.setenv("TRF_LOG", "debug")
        cfg()
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(before_level)
        logger.handlers[:] = before_handlers


# --- custom headers (SEC-14) ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "X-Admin-Token",
        "x-admin-token",
        "X-API-Key",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Real-IP",
        "X-Request-Id",
        "X-Original-URL",
        "X-Http-Method-Override",
        "X-Middleware-Subrequest",
        "Authorization",
        "Proxy-Authorization",
        "Cookie",
        "Host",
        "Content-Length",
        "Content-Type",
        "Transfer-Encoding",
        "Connection",
        "Upgrade",
        "TE",
        "Expect",
        "User-Agent",
        "Sec-Fetch-Site",
        "CF-Connecting-IP",
        "Accept",
        "X-" + "a" * 65,
        "X_Underscore",
        "X-Bad Name",
    ],
)
def test_custom_header_names_refused(name: str) -> None:
    with pytest.raises(rf.ConfigurationError, match="custom_headers"):
        cfg(custom_headers={name: "v"})


@pytest.mark.parametrize("value", ["a\r\nInjected: 1", "nul\x00", "é", "x" * 1025])
def test_custom_header_values_refused(value: str) -> None:
    with pytest.raises(rf.ConfigurationError, match="custom_headers"):
        cfg(custom_headers={"X-Trace": value})


def test_custom_headers_total_size_limit() -> None:
    headers = {f"X-H{i}": "v" * 1000 for i in range(9)}
    with pytest.raises(rf.ConfigurationError, match="8 KiB"):
        cfg(custom_headers=headers)


def test_custom_headers_sent_to_api_only() -> None:
    t = MockTransport([Reply(200, credits_json())])
    c = client(t, custom_headers={"X-Team": "video", "traceparent": "00-abc-def-01", "baggage": "k=v"})
    c.credits.retrieve()
    sent = t.calls[0].headers
    assert sent["x-team"] == "video" and sent["traceparent"] == "00-abc-def-01" and sent["baggage"] == "k=v"


# --- immutability, pickling, repr ------------------------------------------------------------


def test_config_is_immutable_and_unpicklable() -> None:
    c = cfg()
    with pytest.raises(AttributeError):
        c.max_retries = 5
    with pytest.raises(TypeError):
        pickle.dumps(c)
    assert CANARY_KEY not in repr(c)


def test_client_unpicklable_and_redacted() -> None:
    c = rf.Client(api_key=CANARY_KEY, transport=MockTransport())
    with pytest.raises(TypeError):
        pickle.dumps(c)
    text = repr(c) + str(c) + f"{c}"
    assert CANARY_KEY not in text
    assert f"[REDACTED fp={c.api_key_fingerprint}]" in text
    async_c = rf.AsyncClient(api_key=CANARY_KEY, async_transport=AsyncMockTransport())
    with pytest.raises(TypeError):
        pickle.dumps(async_c)


def test_with_options_revalidates_and_shares_pool() -> None:
    c = rf.Client(api_key=CANARY_KEY)
    try:
        d = c.with_options(max_retries=5)
        assert d.config.max_retries == 5 and d._transport is c._transport
        with pytest.raises(rf.ConfigurationError):
            c.with_options(max_retries=99)
        staging = c.with_options(environment="staging")
        assert staging.base_url == "https://api-staging.therightsfoundry.com"
        d.close()  # does not close the shared pool
        assert c._transport._closed is False
    finally:
        c.close()


def test_unauthenticated_client_exposes_only_health_and_downloads() -> None:
    c = rf.Client.unauthenticated(transport=MockTransport())
    assert c.config.api_key is None
    for attr in ("tracks", "credits", "taxonomies"):
        with pytest.raises(rf.ConfigurationError, match="unauthenticated"):
            getattr(c, attr)
    with pytest.raises(rf.ConfigurationError):
        c.request("GET", "/v1/credits")
    with pytest.raises(rf.ConfigurationError):
        rf.Client.unauthenticated(api_key=CANARY_KEY)
    assert c.health is not None and c.sync_links is not None


def test_browser_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "emscripten")
    with pytest.raises(rf.ConfigurationError, match="browser"):
        cfg()
    assert cfg(dangerously_allow_browser=True).dangerously_allow_browser is True


def test_custom_transport_in_production_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="rightsfoundry"):
        cfg(transport=MockTransport())
    assert any("custom transport" in r.getMessage() for r in caplog.records)


def test_proxy_from_env_and_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:s3cret@proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,.example.com")
    c = cfg()
    assert c.proxy is not None
    assert c.proxy.host == "proxy.internal" and c.proxy.port == 3128
    assert "s3cret" not in repr(c.proxy) and "s3cret" not in repr(c)
    assert c.proxy.applies_to("api.therightsfoundry.com")
    assert not c.proxy.applies_to("a.example.com")
    https_proxy = cfg(proxy_url="https://proxy.example.com").proxy
    assert https_proxy is not None and https_proxy.scheme == "https" and https_proxy.port == 443
    assert repr(https_proxy) == "ProxyConfig(https://proxy.example.com:443)"
    with pytest.raises(rf.ConfigurationError, match=r"proxy_url.*'socks5'"):
        cfg(proxy_url="socks5://proxy.example.com:1080")


def test_unsupported_env_proxy_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "socks5://proxy.internal:1080")
    with pytest.raises(rf.ConfigurationError, match="HTTPS_PROXY") as info:
        cfg()
    assert "proxy_url" not in str(info.value)
    monkeypatch.delenv("HTTPS_PROXY")
    monkeypatch.setenv("https_proxy", "socks5://proxy.internal:1080")
    with pytest.raises(rf.ConfigurationError, match="https_proxy"):
        cfg()


def test_ssl_monkeypatch_detection_logs_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import ssl

    monkeypatch.setattr(ssl, "_create_default_https_context", ssl._create_unverified_context)
    with caplog.at_level(logging.ERROR, logger="rightsfoundry"):
        cfg()
    assert any("unverified" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(sys.version_info >= (3, 10), reason="only Python 3.9 is end-of-life")
def test_eol_runtime_warning_once(monkeypatch: pytest.MonkeyPatch) -> None:  # pragma: no cover - 3.9 only
    import rightsfoundry._config as config_module

    monkeypatch.setattr(config_module, "_eol_warned", False)
    with pytest.warns(DeprecationWarning, match="end-of-life"):
        cfg()
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg()  # second construction stays silent


def test_with_options_pool_sharing_rules() -> None:
    c = rf.Client(api_key=CANARY_KEY)
    try:
        assert c.with_options(max_retries=1)._transport is c._transport
        assert c.with_options(max_connections=3)._transport is not c._transport
        staging = c.with_options(base_url="https://api-staging.therightsfoundry.com")
        assert staging.environment == "staging" and staging._transport is not c._transport
    finally:
        c.close()
