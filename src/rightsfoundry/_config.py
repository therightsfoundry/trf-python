"""Immutable, validated client configuration.

Resolution order: explicit keyword → environment variable (read once, here) →
default. Every option is validated at construction; an invalid value raises
:class:`ConfigurationError` that names the option and the rule and never echoes
a secret. No environment variable can enable ``http://``, add an allowed host,
relax a guard's fail-closed mode or weaken TLS — those are code-only decisions.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import ssl
import sys
import warnings
from collections.abc import Iterable, Mapping
from typing import Any, Callable, Final, Literal, Optional, Union
from urllib.parse import unquote, urlsplit

from ._errors import ConfigurationError
from ._redact import redact_text
from ._secret import Secret
from ._validation import parse_legacy_ipv4

__all__ = [
    "API_HOSTS",
    "DEFAULT_ASSET_HOSTS",
    "NOT_GIVEN",
    "ClientConfig",
    "NotGiven",
    "ProxyConfig",
    "build_config",
]

Environment = Literal["production", "staging"]
Jitter = Literal["full", "equal", "none"]
ChargedRetryPolicy = Literal["never", "refund_backed", "always"]
GuardFailureMode = Literal["closed", "open"]


class NotGiven:
    """Sentinel for "argument omitted" (distinct from an explicit ``None``)."""

    _instance: Optional[NotGiven] = None

    def __new__(cls) -> NotGiven:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NOT_GIVEN"

    def __bool__(self) -> bool:
        return False

    def __reduce__(self) -> str:
        return "NOT_GIVEN"


NOT_GIVEN: Final = NotGiven()

API_HOSTS: Mapping[Environment, str] = {
    "production": "api.therightsfoundry.com",
    "staging": "api-staging.therightsfoundry.com",
}
DEFAULT_ASSET_HOSTS: tuple[str, ...] = ("assets.therightsfoundry.com",)
LOOPBACK_LITERALS = frozenset({"localhost", "127.0.0.1", "::1"})

_KEY_RE = re.compile(r"^[0-9a-f]{128}\Z")
_PUBLIC_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}\Z")
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)\Z", re.ASCII)
_GATEWAY_PATH = re.compile(r"^(/[A-Za-z0-9._~-]+)+/?\Z")
_UA_SUFFIX = re.compile(r"^[\x21-\x7e]+(?: [\x21-\x7e]+)*\Z")
_HEADER_NAME_X = re.compile(r"^X-[A-Za-z0-9-]{1,64}\Z", re.IGNORECASE)
_HEADER_VALUE = re.compile(r"^[\x20-\x7e]*\Z")
_TRACE_HEADERS = frozenset({"traceparent", "tracestate", "baggage"})
_RESERVED_X = frozenset({"x-admin-token", "x-api-key", "x-real-ip", "x-request-id", "x-http-method-override"})
_RESERVED_X_PREFIX = ("x-forwarded-", "x-original-", "x-middleware-")
_ALWAYS_REFUSED = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "upgrade",
        "te",
        "expect",
        "user-agent",
    }
)
_LOG_LEVELS: Mapping[str, int] = {
    "off": logging.CRITICAL + 10,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

_eol_warned = False
_log = logging.getLogger("rightsfoundry")
if not any(isinstance(h, logging.NullHandler) for h in _log.handlers):
    _log.addHandler(logging.NullHandler())


def _warn_eol_runtime() -> None:
    global _eol_warned
    if _eol_warned:
        return
    _eol_warned = True
    if sys.version_info < (3, 10):  # pragma: no cover - only on 3.9
        warnings.warn(
            "rightsfoundry: Python 3.9 reached end-of-life in October 2025 and no longer "
            "receives security fixes. Support will be dropped in a future minor release.",
            DeprecationWarning,
            stacklevel=4,
        )


class ProxyConfig:
    """An ``http://`` or ``https://`` CONNECT proxy. Credentials are held as a :class:`Secret`.

    With ``scheme="https"`` the connection to the proxy itself is TLS-verified with
    the client's trust store before ``CONNECT`` is sent; traffic to the API is TLS
    end to end inside the tunnel either way.
    """

    __slots__ = ("auth", "host", "no_proxy", "port", "scheme")

    def __init__(
        self,
        host: str,
        port: int,
        auth: Optional[Secret],
        no_proxy: tuple[str, ...],
        scheme: str = "http",
    ) -> None:
        self.host = host
        self.port = port
        self.auth = auth  # base64("user:pass") for Proxy-Authorization
        self.no_proxy = no_proxy
        self.scheme = scheme

    def applies_to(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        for entry in self.no_proxy:
            if entry == "*":
                return False
            entry = entry.lstrip(".")
            if host == entry or host.endswith("." + entry):
                return False
        return True

    def __repr__(self) -> str:
        auth = "[REDACTED]@" if self.auth is not None else ""
        return f"ProxyConfig({self.scheme}://{auth}{self.host}:{self.port})"


class ClientConfig:
    """Validated, immutable configuration. Build it with :func:`build_config`."""

    api_key: Optional[Secret]
    environment: Environment
    base_url: str
    api_host: str
    api_port: int
    api_scheme: str
    api_path_prefix: str
    environment_explicit: bool
    allowed_hosts: tuple[str, ...]
    asset_hosts: tuple[str, ...]
    ca_bundle: Optional[str]
    proxy: Optional[ProxyConfig]
    connect_timeout: float
    read_timeout: float
    write_timeout: float
    total_timeout: Optional[float]
    max_retries: int
    retry_initial_delay_ms: int
    retry_max_delay_ms: int
    jitter: Jitter
    max_retry_after_ms: int
    retryable_status_codes: frozenset[int]
    charged_retry_policy: ChargedRetryPolicy
    max_credits_per_request: Optional[int]
    low_credit_warning_threshold: Optional[int]
    on_low_credit: Optional[Callable[[Any], Any]]
    auto_check_credits: bool
    credit_check_ttl_ms: int
    rate_card_ttl_ms: int
    guard_failure_mode: GuardFailureMode
    min_bandwidth_bytes: Optional[int]
    on_request: Optional[Callable[[Any], Any]]
    on_response: Optional[Callable[[Any], Any]]
    on_error: Optional[Callable[[Any], Any]]
    hook_include_bodies: bool
    custom_headers: tuple[tuple[str, str], ...]
    user_agent_suffix: Optional[str]
    transport: Any
    async_transport: Any
    logger: logging.Logger
    max_connections: int
    max_response_bytes: int
    max_download_bytes: int
    dangerously_allow_browser: bool
    allow_insecure_localhost: bool
    max_concurrency: Optional[int]
    taxonomies_cache_ttl_ms: int
    unauthenticated: bool

    _frozen = False

    def __init__(self, values: Mapping[str, Any]) -> None:
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("ClientConfig is immutable; use client.with_options(...)")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ClientConfig is immutable")

    @property
    def is_https(self) -> bool:
        return self.api_scheme == "https"

    def to_options(self) -> dict[str, Any]:
        """The resolved options, suitable for ``build_config(**...)`` (``with_options``).

        Uses resolved values (never re-reads the environment) and carries the key as
        a :class:`Secret`, so no plaintext copy is created.
        """
        opts: dict[str, Any] = {
            "environment": self.environment,
            "base_url": self.base_url,
            "allowed_hosts": self.allowed_hosts,
            "asset_hosts": self.asset_hosts,
            "ca_bundle": self.ca_bundle,
            "proxy_url": self.proxy,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "write_timeout": self.write_timeout,
            "total_timeout": self.total_timeout,
            "max_retries": self.max_retries,
            "retry_initial_delay_ms": self.retry_initial_delay_ms,
            "retry_max_delay_ms": self.retry_max_delay_ms,
            "jitter": self.jitter,
            "max_retry_after_ms": self.max_retry_after_ms,
            "retryable_status_codes": tuple(sorted(self.retryable_status_codes)),
            "charged_retry_policy": self.charged_retry_policy,
            "max_credits_per_request": self.max_credits_per_request,
            "low_credit_warning_threshold": self.low_credit_warning_threshold,
            "on_low_credit": self.on_low_credit,
            "auto_check_credits": self.auto_check_credits,
            "credit_check_ttl_ms": self.credit_check_ttl_ms,
            "rate_card_ttl_ms": self.rate_card_ttl_ms,
            "guard_failure_mode": self.guard_failure_mode,
            "min_bandwidth_bytes": self.min_bandwidth_bytes,
            "on_request": self.on_request,
            "on_response": self.on_response,
            "on_error": self.on_error,
            "hook_include_bodies": self.hook_include_bodies,
            "custom_headers": dict(self.custom_headers),
            "user_agent_suffix": self.user_agent_suffix,
            "transport": self.transport,
            "async_transport": self.async_transport,
            "logger": None if self.logger is _log else self.logger,
            "max_connections": self.max_connections,
            "max_response_bytes": self.max_response_bytes,
            "max_download_bytes": self.max_download_bytes,
            "dangerously_allow_browser": self.dangerously_allow_browser,
            "allow_insecure_localhost": self.allow_insecure_localhost,
            "max_concurrency": self.max_concurrency,
            "taxonomies_cache_ttl_ms": self.taxonomies_cache_ttl_ms,
        }
        if not self.unauthenticated:
            opts["api_key"] = self.api_key
        return opts

    def __repr__(self) -> str:
        key = self.api_key.redacted() if self.api_key is not None else "None"
        return (
            f"ClientConfig(environment={self.environment!r}, base_url={self.base_url!r}, "
            f"api_key={key}, max_retries={self.max_retries}, "
            f"charged_retry_policy={self.charged_retry_policy!r}, "
            f"max_credits_per_request={self.max_credits_per_request!r}, "
            f"auto_check_credits={self.auto_check_credits!r}, proxy={self.proxy!r})"
        )

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("ClientConfig cannot be pickled (it holds the API key)")

    def __reduce_ex__(self, protocol: Any) -> Any:
        raise TypeError("ClientConfig cannot be pickled (it holds the API key)")

    def __getstate__(self) -> Any:
        raise TypeError("ClientConfig cannot be pickled (it holds the API key)")


# --- individual validators ---------------------------------------------------------


def _err(option: str, rule: str) -> ConfigurationError:
    return ConfigurationError(f"invalid {option}: {rule}")


def validate_api_key(value: Any, *, source: str = "api_key") -> Secret:
    if not isinstance(value, str):
        raise _err(source, "must be a string")
    key = value.strip()
    if not key:
        raise _err(source, "is empty")
    if _PUBLIC_CODE_RE.fullmatch(key):
        raise _err(
            source,
            "looks like the 4-character public code shown on /dashboard/token, not the "
            "secret key; reveal and copy the 128-character key instead",
        )
    if len(key) != 128:
        raise _err(source, f"must be exactly 128 hexadecimal characters (got {len(key)})")
    lowered = key.lower()
    if not _KEY_RE.fullmatch(lowered):
        raise _err(source, "must contain only hexadecimal characters 0-9 and a-f")
    return Secret(lowered)


def _float_range(option: str, value: Any, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _err(option, "must be a number of seconds")
    if not lo <= float(value) <= hi:
        raise _err(option, f"must be between {lo:g} and {hi:g} seconds")
    return float(value)


def _int_range(option: str, value: Any, lo: Optional[int], hi: Optional[int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(option, "must be an integer")
    if lo is not None and value < lo:
        raise _err(option, f"must be at least {lo}")
    if hi is not None and value > hi:
        raise _err(option, f"must be at most {hi}")
    checked: int = value
    return checked


def _enum(option: str, value: Any, choices: tuple[str, ...]) -> Any:
    if not isinstance(value, str) or value not in choices:
        raise _err(option, f"must be one of {', '.join(choices)}")
    return value


def _callable(option: str, value: Any) -> Optional[Callable[[Any], Any]]:
    if value is None:
        return None
    if not callable(value):
        raise _err(option, "must be callable")
    return value  # type: ignore[no-any-return]


def _bool(option: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _err(option, "must be a bool")
    return value


def normalize_host_pattern(option: str, value: Any) -> str:
    if not isinstance(value, str):
        raise _err(option, "entries must be strings")
    host = value.strip().lower().rstrip(".")
    if not host.isascii():
        raise _err(option, "entries must be ASCII hostnames (use punycode)")
    wildcard = host.startswith("*.")
    body = host[2:] if wildcard else host
    if not body or not all(_HOST_LABEL.fullmatch(label) for label in body.split(".")):
        raise _err(option, "entries must be hostnames, optionally with one leading '*.' label")
    if wildcard and body.count(".") < 1:
        raise _err(option, f"wildcard {host!r} is too broad; name at least two labels after '*.' (*.example.com)")
    if _ip_literal(body) is not None:
        raise _err(option, "entries must be hostnames, not IP addresses")
    return host


def host_matches(host: str, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if host.endswith(suffix) and "." not in host[: -len(suffix)] and host != suffix[1:]:
                return True
        elif host == pattern:
            return True
    return False


def _ip_literal(host: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    candidate = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    last = candidate.rsplit(".", 1)[-1]
    if re.fullmatch(r"(0x[0-9a-f]*|[0-9]+)", last, re.ASCII | re.IGNORECASE):
        legacy = parse_legacy_ipv4(candidate)
        if legacy is not None:
            return legacy
        # numeric last label but not a valid address: still not a hostname
        return ipaddress.IPv4Address(0)
    return None


def _validate_base_url(
    raw: Any,
    *,
    environment: Environment,
    environment_explicit: bool,
    allowed_hosts: tuple[str, ...],
    allow_insecure_localhost: bool,
) -> tuple[str, str, str, int, str, Environment]:
    """SEC-08. Returns ``(base_url, scheme, host, port, path_prefix, environment)``."""
    option = "base_url"
    if not isinstance(raw, str):
        raise _err(option, "must be a string")
    url = raw.strip()
    if not url:
        raise _err(option, "is empty")
    if any(ord(c) < 0x21 or ord(c) > 0x7E for c in url) or "\\" in url:
        raise _err(option, "must be printable ASCII without spaces or backslashes")
    if "?" in url or "#" in url:
        raise _err(option, "must not contain a query or fragment")
    try:
        parts = urlsplit(url)
        port_value = parts.port
    except ValueError as exc:
        raise _err(option, "is not a valid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise _err(option, "scheme must be https")
    if not parts.netloc or "@" in parts.netloc:
        raise _err(option, "must have a host and no user:password@ credentials")
    host_raw = parts.hostname or ""
    host = host_raw.lower().rstrip(".")
    if not host:
        raise _err(option, "must have a host")
    is_loopback_literal = host in LOOPBACK_LITERALS
    if is_loopback_literal:
        if not allow_insecure_localhost:
            raise _err(
                option,
                "loopback hosts require allow_insecure_localhost=True (code only) and a non-production environment",
            )
        if environment == "production":
            raise _err(option, "loopback hosts are refused when environment='production'")
    else:
        if scheme != "https":
            raise _err(option, "scheme must be https")
        if _ip_literal(host) is not None:
            raise _err(option, "host must be a hostname, not an IP address (in any encoding)")
        if not all(_HOST_LABEL.fullmatch(label) for label in host.split(".")):
            raise _err(option, "host must be an ASCII hostname (use punycode for IDNs)")
    first_party_env: Optional[Environment] = None
    for env_name, api_host in API_HOSTS.items():
        if host == api_host:
            first_party_env = env_name
    if first_party_env is None and not is_loopback_literal and not host_matches(host, allowed_hosts):
        raise _err(
            option,
            "host is not api.therightsfoundry.com or api-staging.therightsfoundry.com and is "
            "not in allowed_hosts (allowed_hosts can only be set in code)",
        )
    port = port_value if port_value is not None else (443 if scheme == "https" else 80)
    path = parts.path
    if first_party_env is not None:
        if port != 443:
            raise _err(option, "first-party API hosts are served on port 443 only")
        if path not in ("", "/"):
            raise _err(option, "first-party API hosts take no path prefix")
        if environment_explicit and environment != first_party_env:
            raise _err(
                option,
                f"host belongs to environment {first_party_env!r} but environment={environment!r} was given",
            )
        environment = first_party_env
        path = ""
    else:
        if path not in ("", "/"):
            if "%" in path or not _GATEWAY_PATH.fullmatch(path) or any(seg in (".", "..") for seg in path.split("/")):
                raise _err(option, "path prefix must match ^(/[A-Za-z0-9._~-]+)+/?$ with no '..'")
        path = path.rstrip("/")
        if unquote(path) != path:
            raise _err(option, "path prefix must not be percent-encoded")
    netloc = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    if port != default_port:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}{path}", scheme, host, port, path, environment


def _validate_custom_headers(value: Any) -> tuple[tuple[str, str], ...]:
    option = "custom_headers"
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise _err(option, "must be a mapping of header name to value")
    out: list[tuple[str, str]] = []
    total = 0
    seen: set[str] = set()
    for name, header_value in value.items():
        if not isinstance(name, str) or not isinstance(header_value, str):
            raise _err(option, "names and values must be strings")
        lname = name.lower()
        if lname in _ALWAYS_REFUSED or lname.startswith(("sec-", "cf-")):
            raise _err(option, f"header {name!r} cannot be set")
        if lname in _TRACE_HEADERS:
            pass
        elif _HEADER_NAME_X.fullmatch(name):
            if lname in _RESERVED_X or lname.startswith(_RESERVED_X_PREFIX):
                raise _err(option, f"header {name!r} is reserved and cannot be sent by the SDK")
        else:
            raise _err(
                option,
                f"header {name!r} is not allowed; use X-* names (not reserved ones) or "
                "traceparent / tracestate / baggage",
            )
        if lname in seen:
            raise _err(option, f"header {name!r} is given twice")
        seen.add(lname)
        if not _HEADER_VALUE.fullmatch(header_value):
            raise _err(option, f"value of {name!r} must be printable ASCII (no CR, LF or NUL)")
        size = len(header_value.encode("ascii"))
        if size > 1024:
            raise _err(option, f"value of {name!r} exceeds 1024 bytes")
        total += size + len(name)
        out.append((name, header_value))
    if total > 8 * 1024:
        raise _err(option, "total size exceeds 8 KiB")
    return tuple(out)


def _parse_proxy(value: Any, no_proxy_raw: Optional[str], *, source: str = "proxy_url") -> Optional[ProxyConfig]:
    import base64

    if value is None or value == "":
        return None
    if isinstance(value, ProxyConfig):
        return value
    if not isinstance(value, str):
        raise _err(source, "must be a string")
    text = value.strip()
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise _err(source, "is not a valid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise _err(
            source,
            f"proxy scheme {scheme!r} is not supported; use an http:// or https:// CONNECT proxy "
            "(traffic inside the tunnel is still TLS end to end)",
        )
    host = (parts.hostname or "").lower()
    if not host or not host.isascii():
        raise _err(source, "must have an ASCII host")
    auth: Optional[Secret] = None
    if parts.username is not None:
        creds = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        auth = Secret(base64.b64encode(creds.encode("utf-8")).decode("ascii"))
    no_proxy = tuple(entry.strip().lower() for entry in (no_proxy_raw or "").split(",") if entry.strip())
    default_port = 443 if scheme == "https" else 8080
    return ProxyConfig(host, port or default_port, auth, no_proxy, scheme)


def _env(name: str) -> Optional[str]:
    """An environment value with surrounding whitespace removed (``None`` when unset or blank).

    Values mounted from secret files often end in a newline; stripping here means
    every validator sees the value a person meant to set.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_or(name: str, cast: Callable[[str], Any], default: Any) -> Any:
    value = _env_number(name, cast)
    return default if value is None else value


def _env_number(name: str, cast: Callable[[str], Any]) -> Any:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return cast(raw.strip())
    except ValueError:
        raise _err(name, "is not a valid number") from None


def _env_bool(name: str) -> Optional[bool]:
    raw = _env(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in ("true", "1"):
        return True
    if normalized in ("false", "0"):
        return False
    raise _err(name, "must be true, false, 1 or 0")


#: Every keyword :func:`build_config` accepts, with its default.
DEFAULTS: Mapping[str, Any] = {
    "api_key": NOT_GIVEN,
    "environment": NOT_GIVEN,
    "base_url": NOT_GIVEN,
    "allowed_hosts": (),
    "asset_hosts": DEFAULT_ASSET_HOSTS,
    "ca_bundle": NOT_GIVEN,
    "proxy_url": NOT_GIVEN,
    "connect_timeout": NOT_GIVEN,
    "read_timeout": NOT_GIVEN,
    "write_timeout": 30.0,
    "total_timeout": None,
    "max_retries": NOT_GIVEN,
    "retry_initial_delay_ms": 500,
    "retry_max_delay_ms": 8_000,
    "jitter": "full",
    "max_retry_after_ms": 30_000,
    "retryable_status_codes": (408, 429, 500, 502, 503, 504),
    "charged_retry_policy": "refund_backed",
    "max_credits_per_request": NOT_GIVEN,
    "low_credit_warning_threshold": NOT_GIVEN,
    "on_low_credit": None,
    "auto_check_credits": NOT_GIVEN,
    "credit_check_ttl_ms": 30_000,
    "rate_card_ttl_ms": 60_000,
    "guard_failure_mode": "closed",
    "min_bandwidth_bytes": None,
    "on_request": None,
    "on_response": None,
    "on_error": None,
    "hook_include_bodies": False,
    "custom_headers": None,
    "user_agent_suffix": NOT_GIVEN,
    "transport": None,
    "async_transport": None,
    "logger": None,
    "max_connections": 10,
    "max_response_bytes": 10 * 1024 * 1024,
    "max_download_bytes": 2 * 1024 * 1024 * 1024,
    "dangerously_allow_browser": False,
    "allow_insecure_localhost": False,
    "max_concurrency": None,
    "taxonomies_cache_ttl_ms": 0,
}


def _given(value: Any) -> bool:
    return not isinstance(value, NotGiven)


def build_config(*, unauthenticated: bool = False, **options: Any) -> ClientConfig:
    """Validate every option and return an immutable :class:`ClientConfig`."""
    unknown = sorted(set(options) - set(DEFAULTS))
    if unknown:
        raise ConfigurationError(f"unknown option(s): {', '.join(unknown)}")
    _warn_eol_runtime()
    opts = {**DEFAULTS, **options}

    # SEC-02: no browser / WASM contexts unless explicitly, dangerously, allowed.
    allow_browser = _bool("dangerously_allow_browser", opts["dangerously_allow_browser"])
    if sys.platform == "emscripten" and not allow_browser:
        raise ConfigurationError(
            "The Rights Foundry API key is an account secret and must not run in a browser "
            "(Pyodide/WASM detected). Call the API from your server and send the browser only "
            "signed asset URLs, or pass dangerously_allow_browser=True."
        )

    # --- api key: explicit (incl. None) wins; only an omitted argument reads the env.
    api_key: Optional[Secret] = None
    if unauthenticated:
        if _given(opts["api_key"]) and opts["api_key"] is not None:
            raise ConfigurationError("unauthenticated clients do not take an api_key")
    elif _given(opts["api_key"]):
        if opts["api_key"] is None:
            raise ConfigurationError(
                "api_key is required; api_key=None does not fall back to TRF_API_KEY. Use "
                "Client.unauthenticated() for health checks and signed-link downloads."
            )
        if isinstance(opts["api_key"], Secret):
            from ._secret import _reveal

            api_key = validate_api_key(_reveal(opts["api_key"]))
        else:
            api_key = validate_api_key(opts["api_key"])
    else:
        env_key = os.environ.get("TRF_API_KEY")
        if env_key is None or not env_key.strip():
            raise ConfigurationError(
                "api_key is required: pass api_key=... or set TRF_API_KEY. Create a key at "
                "https://therightsfoundry.com/dashboard/token"
            )
        api_key = validate_api_key(env_key, source="TRF_API_KEY")

    environment_explicit = _given(opts["environment"]) or _env("TRF_ENVIRONMENT") is not None
    env_value = (
        opts["environment"]
        if _given(opts["environment"])
        else ((_env("TRF_ENVIRONMENT") or "production").strip().lower())
    )
    environment: Environment = _enum("environment", env_value, ("production", "staging"))

    allowed_hosts_in = opts["allowed_hosts"]
    if isinstance(allowed_hosts_in, str) or not isinstance(allowed_hosts_in, Iterable):
        raise _err("allowed_hosts", "must be a list of hostnames")
    allowed_hosts = tuple(normalize_host_pattern("allowed_hosts", h) for h in allowed_hosts_in)
    asset_hosts_in = opts["asset_hosts"]
    if isinstance(asset_hosts_in, str) or not isinstance(asset_hosts_in, Iterable):
        raise _err("asset_hosts", "must be a list of hostnames")
    asset_hosts = tuple(normalize_host_pattern("asset_hosts", h) for h in asset_hosts_in)
    if not asset_hosts:
        raise _err("asset_hosts", "must not be empty")

    allow_insecure_localhost = _bool("allow_insecure_localhost", opts["allow_insecure_localhost"])
    base_raw = opts["base_url"] if _given(opts["base_url"]) else _env("TRF_BASE_URL")
    if base_raw is None:
        base_raw = f"https://{API_HOSTS[environment]}"
    base_url, scheme, host, port, prefix, environment = _validate_base_url(
        base_raw,
        environment=environment,
        environment_explicit=environment_explicit,
        allowed_hosts=allowed_hosts,
        allow_insecure_localhost=allow_insecure_localhost,
    )

    ca_bundle = opts["ca_bundle"] if _given(opts["ca_bundle"]) else _env("TRF_CA_BUNDLE")
    if ca_bundle is not None:
        if not isinstance(ca_bundle, (str, os.PathLike)):
            raise _err("ca_bundle", "must be a file path")
        ca_bundle = os.fspath(ca_bundle)
        if not os.path.isfile(ca_bundle) or not os.access(ca_bundle, os.R_OK):
            raise _err("ca_bundle", "must be a readable file")

    no_proxy_env = _env("NO_PROXY") or _env("no_proxy")
    if _given(opts["proxy_url"]):
        proxy = _parse_proxy(opts["proxy_url"], no_proxy_env)
    elif _env("HTTPS_PROXY") is not None:
        proxy = _parse_proxy(_env("HTTPS_PROXY"), no_proxy_env, source="HTTPS_PROXY (environment)")
    else:
        proxy = _parse_proxy(_env("https_proxy"), no_proxy_env, source="https_proxy (environment)")

    connect_timeout = _float_range(
        "connect_timeout",
        opts["connect_timeout"] if _given(opts["connect_timeout"]) else _env_or("TRF_CONNECT_TIMEOUT", float, 10.0),
        0.1,
        120,
    )
    read_timeout = _float_range(
        "read_timeout",
        opts["read_timeout"] if _given(opts["read_timeout"]) else _env_or("TRF_READ_TIMEOUT", float, 60.0),
        1,
        600,
    )
    write_timeout = _float_range("write_timeout", opts["write_timeout"], 1, 600)
    total_timeout = opts["total_timeout"]
    if total_timeout is not None:
        total_timeout = _float_range("total_timeout", total_timeout, read_timeout, 86_400)

    max_retries_raw = opts["max_retries"] if _given(opts["max_retries"]) else _env_number("TRF_MAX_RETRIES", int)
    max_retries = _int_range("max_retries", 2 if max_retries_raw is None else max_retries_raw, 0, 10)
    retry_initial = _int_range("retry_initial_delay_ms", opts["retry_initial_delay_ms"], 50, 60_000)
    retry_max = _int_range("retry_max_delay_ms", opts["retry_max_delay_ms"], retry_initial, 300_000)
    jitter = _enum("jitter", opts["jitter"], ("full", "equal", "none"))
    max_retry_after = _int_range("max_retry_after_ms", opts["max_retry_after_ms"], 0, 600_000)

    codes_in = opts["retryable_status_codes"]
    if isinstance(codes_in, (str, bytes)) or not isinstance(codes_in, Iterable):
        raise _err("retryable_status_codes", "must be a set of integers")
    codes: set[int] = set()
    for code in codes_in:
        if isinstance(code, bool) or not isinstance(code, int) or not 400 <= code <= 599:
            raise _err("retryable_status_codes", "entries must be integers in 400-599")
        if 400 <= code <= 407 or 409 <= code <= 428:
            raise _err("retryable_status_codes", f"{code} is not a transient status and cannot be retried")
        codes.add(code)

    policy = _enum("charged_retry_policy", opts["charged_retry_policy"], ("never", "refund_backed", "always"))

    cap_raw = (
        opts["max_credits_per_request"]
        if _given(opts["max_credits_per_request"])
        else (_env_number("TRF_MAX_CREDITS_PER_REQUEST", int))
    )
    cap = None if cap_raw is None else _int_range("max_credits_per_request", cap_raw, 0, None)
    thr_raw = (
        opts["low_credit_warning_threshold"]
        if _given(opts["low_credit_warning_threshold"])
        else _env_number("TRF_LOW_CREDIT_WARNING_THRESHOLD", int)
    )
    threshold = None if thr_raw is None else _int_range("low_credit_warning_threshold", thr_raw, None, None)
    auto_check_raw = (
        opts["auto_check_credits"] if _given(opts["auto_check_credits"]) else _env_bool("TRF_AUTO_CHECK_CREDITS")
    )
    auto_check = _bool("auto_check_credits", False if auto_check_raw is None else auto_check_raw)
    min_bw = opts["min_bandwidth_bytes"]
    if min_bw is not None:
        min_bw = _int_range("min_bandwidth_bytes", min_bw, None, None)

    ua_raw = opts["user_agent_suffix"] if _given(opts["user_agent_suffix"]) else _env("TRF_USER_AGENT_SUFFIX")
    ua_suffix: Optional[str] = None
    if ua_raw is not None:
        if not isinstance(ua_raw, str) or len(ua_raw) > 128 or not _UA_SUFFIX.fullmatch(ua_raw):
            raise _err(
                "user_agent_suffix",
                "must be printable ASCII product tokens, at most 128 characters",
            )
        ua_suffix = ua_raw

    logger_in = opts["logger"]
    if logger_in is not None and not isinstance(logger_in, logging.Logger):
        raise _err("logger", "must be a logging.Logger")
    logger = logger_in or _log
    log_env = _env("TRF_LOG")
    if log_env is not None:
        level = _LOG_LEVELS.get(log_env.strip().lower())
        if level is None:
            raise _err("TRF_LOG", "must be off, error, warn, info or debug")
        if logger_in is None:
            _log.setLevel(level)
            if level <= logging.CRITICAL and not any(getattr(h, "_rightsfoundry_env", False) for h in _log.handlers):
                handler = logging.StreamHandler()
                handler._rightsfoundry_env = True  # type: ignore[attr-defined]
                handler.setFormatter(logging.Formatter("%(asctime)s rightsfoundry %(levelname)s %(message)s"))
                _log.addHandler(handler)

    transport = opts["transport"]
    if transport is not None and not callable(getattr(transport, "send", None)):
        raise _err(
            "transport",
            "must have a send(request, *, connect_timeout, read_timeout, write_timeout) method",
        )
    async_transport = opts["async_transport"]
    if async_transport is not None and not callable(getattr(async_transport, "send", None)):
        raise _err("async_transport", "must have an async send(...) method")
    if (transport is not None or async_transport is not None) and environment == "production":
        logger.warning(
            "rightsfoundry: a custom transport is in use with environment='production'; TLS "
            "verification inside it is the integrator's responsibility"
        )

    max_concurrency = opts["max_concurrency"]
    if max_concurrency is not None:
        max_concurrency = _int_range("max_concurrency", max_concurrency, 1, 10_000)

    if environment == "staging":
        logger.info("rightsfoundry: using the STAGING environment (%s)", base_url)
    if environment == "production" and ssl._create_default_https_context is getattr(
        ssl, "_create_unverified_context", None
    ):
        logger.error(
            "rightsfoundry: ssl._create_default_https_context has been replaced with the "
            "unverified context; the SDK uses its own verified SSLContext and is unaffected"
        )

    values: dict[str, Any] = {
        "api_key": api_key,
        "environment": environment,
        "environment_explicit": environment_explicit,
        "base_url": base_url,
        "api_scheme": scheme,
        "api_host": host,
        "api_port": port,
        "api_path_prefix": prefix,
        "allowed_hosts": allowed_hosts,
        "asset_hosts": asset_hosts,
        "ca_bundle": ca_bundle,
        "proxy": proxy,
        "connect_timeout": connect_timeout,
        "read_timeout": read_timeout,
        "write_timeout": write_timeout,
        "total_timeout": total_timeout,
        "max_retries": max_retries,
        "retry_initial_delay_ms": retry_initial,
        "retry_max_delay_ms": retry_max,
        "jitter": jitter,
        "max_retry_after_ms": max_retry_after,
        "retryable_status_codes": frozenset(codes),
        "charged_retry_policy": policy,
        "max_credits_per_request": cap,
        "low_credit_warning_threshold": threshold,
        "on_low_credit": _callable("on_low_credit", opts["on_low_credit"]),
        "auto_check_credits": auto_check,
        "credit_check_ttl_ms": _int_range("credit_check_ttl_ms", opts["credit_check_ttl_ms"], 1_000, 3_600_000),
        "rate_card_ttl_ms": _int_range("rate_card_ttl_ms", opts["rate_card_ttl_ms"], 1_000, 3_600_000),
        "guard_failure_mode": _enum("guard_failure_mode", opts["guard_failure_mode"], ("closed", "open")),
        "min_bandwidth_bytes": min_bw,
        "on_request": _callable("on_request", opts["on_request"]),
        "on_response": _callable("on_response", opts["on_response"]),
        "on_error": _callable("on_error", opts["on_error"]),
        "hook_include_bodies": _bool("hook_include_bodies", opts["hook_include_bodies"]),
        "custom_headers": _validate_custom_headers(opts["custom_headers"]),
        "user_agent_suffix": ua_suffix,
        "transport": transport,
        "async_transport": async_transport,
        "logger": logger,
        "max_connections": _int_range("max_connections", opts["max_connections"], 1, 256),
        "max_response_bytes": _int_range(
            "max_response_bytes", opts["max_response_bytes"], 64 * 1024, 100 * 1024 * 1024
        ),
        "max_download_bytes": _int_range("max_download_bytes", opts["max_download_bytes"], 1024 * 1024, None),
        "dangerously_allow_browser": allow_browser,
        "allow_insecure_localhost": allow_insecure_localhost,
        "max_concurrency": max_concurrency,
        "taxonomies_cache_ttl_ms": _int_range(
            "taxonomies_cache_ttl_ms", opts["taxonomies_cache_ttl_ms"], 0, 86_400_000
        ),
        "unauthenticated": unauthenticated,
    }
    return ClientConfig(values)


def safe_option_repr(value: Any) -> str:
    """Redacted ``repr`` for error messages that mention a configured value."""
    return redact_text(repr(value))
