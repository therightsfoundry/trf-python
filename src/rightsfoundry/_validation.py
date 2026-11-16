"""Client-side input validation (SEC-09, SEC-10, SEC-11).

Everything here runs before a byte is sent. A failure raises
:class:`~rightsfoundry.ValidationError` (``charge_state = "not_charged"``).
"""

from __future__ import annotations

import ipaddress
import logging
import math
import re
from collections.abc import Mapping
from typing import Any, Optional, Union
from urllib.parse import urlsplit

from ._errors import InvalidTrackIdError, ValidationError
from ._params import LIST_QUERY_PARAMS, BodyField, QueryParam

__all__ = [
    "MAX_TRACK_ID",
    "build_body",
    "build_list_query",
    "build_query",
    "encode_query_value",
    "has_control_chars",
    "strip_text",
    "utf16_length",
    "parse_legacy_ipv4",
    "validate_bool",
    "validate_file_url",
    "validate_idempotency_key",
    "validate_page_limit",
    "validate_query_text",
    "validate_request_path",
    "validate_request_query",
    "validate_track_id",
]

MAX_TRACK_ID = 999_999_999_999_999
_TRACK_ID_RE = re.compile(r"[0-9]{1,15}", re.ASCII)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_REQUEST_PATH_RE = re.compile(r"/v1/[A-Za-z0-9/_-]+", re.ASCII)
_QUERY_KEY_RE = re.compile(r"[A-Za-z0-9_]{1,64}", re.ASCII)
_PRINTABLE_ASCII = re.compile(r"[\x21-\x7e]{1,255}")
_ALLOWED_FILE_PORTS = frozenset({80, 443, 8080, 8443})
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_HOST_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)", re.ASCII)

TrackId = Union[int, str]

_log = logging.getLogger("rightsfoundry")


def has_control_chars(value: str) -> bool:
    return _CONTROL_RE.search(value) is not None


def validate_track_id(value: Any, *, name: str = "track_id") -> str:
    """Canonical decimal string for a track id (SEC-09).

    Accepts an ``int`` in ``1..999_999_999_999_999`` or a string of 1–15 ASCII
    digits. Booleans, Unicode digits, signs, whitespace and anything path-like
    are rejected, which mirrors the server's ``^\\d{1,15}$``.
    """
    if isinstance(value, bool):
        raise InvalidTrackIdError(f"{name} must be an integer, not a bool")
    if isinstance(value, int):
        if not 1 <= value <= MAX_TRACK_ID:
            raise InvalidTrackIdError(f"{name} must be between 1 and {MAX_TRACK_ID}")
        return str(value)
    if isinstance(value, str):
        if not _TRACK_ID_RE.fullmatch(value):
            raise InvalidTrackIdError(f"{name} must be 1 to 15 ASCII digits (got an invalid string)")
        number = int(value)
        if number < 1:
            raise InvalidTrackIdError(f"{name} must be at least 1")
        return str(number)
    raise InvalidTrackIdError(f"{name} must be an int or a digit string, not {type(value).__name__}")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(f"{name} must be finite")
    number: float = value
    return number


def encode_number(value: float) -> str:
    """Finite decimal without exponent: ``90`` → ``"90"``, ``90.5`` → ``"90.5"``."""
    if isinstance(value, int):
        return str(value)
    if value.is_integer():
        return str(int(value))
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def validate_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a bool")
    return value


def utf16_length(text: str) -> int:
    """Length in UTF-16 code units — what the API's JavaScript validators count.

    A supplementary-plane character (most emoji) is one Python code point but two
    UTF-16 units, so ``len()`` alone would let an over-long value pass here and
    fail with a ``400`` on the server.
    """
    return len(text) + sum(1 for c in text if ord(c) > 0xFFFF)


def strip_text(value: str) -> str:
    """Trim whitespace and byte-order marks (U+FEFF) the way JavaScript's ``trim()`` does."""
    text = value.strip()
    while text[:1] == "\ufeff" or text[-1:] == "\ufeff":
        text = text.strip("\ufeff").strip()
    return text


def _text(value: Any, name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    text = strip_text(value)
    if has_control_chars(text):
        raise ValidationError(f"{name} must not contain control characters")
    if utf16_length(text) > max_length:
        raise ValidationError(f"{name} must be at most {max_length} characters (UTF-16 code units)")
    return text


def _param(p: QueryParam, value: Any) -> Optional[str]:
    """Validate and encode one value; ``None`` means "omit"."""
    if p.kind == "bool":
        return "true" if validate_bool(value, p.name) else "false"
    if p.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{p.name} must be an integer")
        if p.name == "id":
            return validate_track_id(value, name="id")
        if p.minimum is not None and value < p.minimum:
            raise ValidationError(f"{p.name} must be at least {int(p.minimum)}")
        if p.maximum is not None and value > p.maximum:
            raise ValidationError(f"{p.name} must be at most {int(p.maximum)}")
        return str(value)
    if p.kind == "number":
        num = _number(value, p.name)
        if p.minimum is not None and num < p.minimum:
            raise ValidationError(f"{p.name} must be at least {encode_number(p.minimum)}")
        if p.maximum is not None and num > p.maximum:
            raise ValidationError(f"{p.name} must be at most {encode_number(p.maximum)}")
        return encode_number(num)
    if p.kind == "enum":
        if not isinstance(value, str) or value not in p.choices:
            raise ValidationError(f"{p.name} must be one of: {', '.join(p.choices)}")
        return value
    text = _text(value, p.name, p.max_length or 200)
    return text or None


def build_query(args: Mapping[str, Any], params: tuple[QueryParam, ...], operation: str) -> tuple[tuple[str, str], ...]:
    """Validate keyword arguments against ``params`` and encode them in a fixed (sorted) order."""
    known = {p.name: p for p in params}
    unknown = sorted(set(args) - set(known))
    if unknown:
        raise ValidationError(f"unknown parameter(s) for {operation}: {', '.join(unknown)}")
    out: dict[str, str] = {}
    values: dict[str, Any] = {}
    for name, value in args.items():
        if value is None:
            continue
        p = known[name]
        encoded = _param(p, value)
        if encoded is not None:
            out[p.wire] = encoded
            values[name] = value
    for lo, hi in (("bpm_min", "bpm_max"), ("duration_min", "duration_max")):
        if lo in values and hi in values and values[lo] > values[hi]:
            raise ValidationError(f"{lo} must be less than or equal to {hi}")
    return tuple(sorted(out.items()))


def build_list_query(args: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """Validate ``tracks.list`` keywords and encode them in a fixed (sorted) order."""
    return build_query(args, LIST_QUERY_PARAMS, "tracks.list")


def build_body(args: Mapping[str, Any], body: tuple[BodyField, ...], operation: str) -> tuple[dict[str, Any], bool]:
    """Validate a JSON body against its :class:`BodyField` specs.

    Missing optional fields take their declared default. Returns ``(body,
    plain_http_file_url)``; the flag is true when a ``file_url`` field uses
    ``http://`` (the caller warns once).
    """
    known = {f.name: f for f in body}
    unknown = sorted(set(args) - set(known))
    if unknown:
        raise ValidationError(f"unknown body field(s) for {operation}: {', '.join(unknown)}")
    out: dict[str, Any] = {}
    plain_http = False
    for f in body:
        value = args.get(f.name)
        if value is None:
            if f.required:
                raise ValidationError(f"{f.name} is required")
            value = f.default
        if f.kind == "text":
            out[f.name] = validate_query_text(value, name=f.name, max_length=f.max_length or 500)
        elif f.kind == "file_url":
            out[f.name], plain_http = validate_file_url(value)
        elif f.kind == "bool":
            out[f.name] = validate_bool(value, f.name)
        else:
            out[f.name] = _bounded_int(value, f.name, f.minimum, f.maximum)
    return out, plain_http


def _bounded_int(value: Any, name: str, minimum: Optional[int], maximum: Optional[int]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{name} must be at most {maximum}")
    checked: int = value
    return checked


def validate_page_limit(page: Any, limit: Any) -> tuple[int, int]:
    return _bounded_int(page, "page", 1, None), _bounded_int(limit, "limit", 1, 100)


def validate_query_text(query: Any, *, name: str = "query", max_length: int = 500) -> str:
    text = _text(query, name, max_length)
    if not text:
        raise ValidationError(f"{name} is required")
    return text


def parse_legacy_ipv4(host: str) -> Optional[ipaddress.IPv4Address]:
    """Parse WHATWG/inet_aton IPv4 forms: ``2130706433``, ``0x7f.1``, ``0177.0.0.1``, ``127.1``."""
    parts = host.split(".")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not 1 <= len(parts) <= 4:
        return None
    nums: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.lower().startswith("0x"):
                nums.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part.startswith("0"):
                nums.append(int(part, 8))
            else:
                if not part.isascii() or not part.isdigit():
                    return None
                nums.append(int(part, 10))
        except ValueError:
            return None
    *head, last = nums
    if any(n > 255 for n in head) or last >= 256 ** (5 - len(nums)):
        return None
    value = 0
    for n in head:
        value = value * 256 + n
    value = value * 256 ** (5 - len(nums)) + last
    if value > 0xFFFFFFFF:
        return None
    return ipaddress.IPv4Address(value)


def _looks_numeric_host(host: str) -> bool:
    last = host.rstrip(".").rsplit(".", 1)[-1]
    return bool(re.fullmatch(r"(0x[0-9a-f]*|[0-9]+)", last, re.ASCII | re.IGNORECASE))


def _is_internal_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def validate_file_url(value: Any) -> tuple[str, bool]:
    """SEC-11: validate an Acoustic Similarity Search ``file_url`` before a charged call.

    An unreachable file is **charged and not refunded**, and the URL is sent to
    a third party, so the SDK refuses anything the provider could not fetch.
    Returns ``(url, is_plain_http)``.
    """
    if not isinstance(value, str):
        raise ValidationError("file_url must be a string")
    url = strip_text(value)
    if not url:
        raise ValidationError("file_url is required")
    if utf16_length(url) > 2000:
        raise ValidationError("file_url must be at most 2000 characters")
    if has_control_chars(url) or re.search(r"\s", url) or "\\" in url:
        raise ValidationError("file_url must not contain whitespace, backslashes or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValidationError("file_url must be a valid URL") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValidationError("file_url must be an http(s) URL")
    if not parts.netloc:
        raise ValidationError("file_url must be a valid URL")
    if "@" in parts.netloc:
        raise ValidationError("file_url must not contain credentials (they would be forwarded to the search provider)")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise ValidationError("file_url must be a valid URL")
    if not host.isascii():
        raise ValidationError("file_url host must be ASCII (use punycode for internationalised names)")
    if not re.search(r"\.(mp3|wav)\Z", parts.path, re.IGNORECASE):
        raise ValidationError("file_url must point to an .mp3 or .wav file")
    if port is not None and port not in _ALLOWED_FILE_PORTS:
        raise ValidationError("file_url port must be one of 80, 443, 8080, 8443")
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if _looks_numeric_host(host):
            legacy = parse_legacy_ipv4(host)
            if legacy is None:
                raise ValidationError("file_url host is not a valid hostname") from None
            raise ValidationError("file_url must not use a numeric IP encoding; use a public hostname") from None
    if ip is not None:
        if _is_internal_ip(ip):
            raise ValidationError(
                "file_url must not point at a private, loopback, link-local, CGNAT or "
                "unique-local address (the search provider cannot reach it)"
            )
    else:
        lower = host.lower()
        if lower == "localhost" or lower.endswith(".localhost"):
            raise ValidationError("file_url must not point at localhost")
        if "." not in lower:
            raise ValidationError("file_url host must be a fully-qualified public hostname")
        if not all(_HOST_LABEL.fullmatch(label) for label in lower.split(".")):
            raise ValidationError("file_url host is not a valid hostname")
    return url, scheme == "http"


def validate_request_path(path: Any) -> str:
    """``client.request`` path rules: ``/v1/`` + ``[A-Za-z0-9/_-]``, no traversal or encoding."""
    if not isinstance(path, str):
        raise ValidationError("path must be a string")
    lowered = path.lower()
    if (
        ".." in path
        or "//" in path
        or "%2e" in lowered
        or "%2f" in lowered
        or "\\" in path
        or not _REQUEST_PATH_RE.fullmatch(path)
    ):
        raise ValidationError(
            "path must start with /v1/ and contain only A-Z a-z 0-9 / _ - (no '..', '//' or encoding)"
        )
    if lowered.startswith(("/v1/admin", "/v1/health/admin")):
        raise ValidationError("admin endpoints are not available through the public SDK")
    return path


def encode_query_value(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return encode_number(_number(value, name))
    if isinstance(value, str):
        if has_control_chars(value):
            raise ValidationError(f"query parameter {name} must not contain control characters")
        if utf16_length(value) > 2000:
            raise ValidationError(f"query parameter {name} is too long")
        return value
    raise ValidationError(f"query parameter {name} must be a str, int, float or bool")


def validate_request_query(query: Optional[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    if query is None:
        return ()
    if not isinstance(query, Mapping):
        raise ValidationError("query must be a mapping")
    out: list[tuple[str, str]] = []
    for name, value in query.items():
        if not isinstance(name, str) or not _QUERY_KEY_RE.fullmatch(name):
            raise ValidationError("query parameter names must match [A-Za-z0-9_]{1,64}")
        if value is None:
            continue
        out.append((name, encode_query_value(name, value)))
    return tuple(sorted(out))


def validate_idempotency_key(key: Any) -> Optional[str]:
    """Optional ``Idempotency-Key`` (API change request R-2): 1–255 printable ASCII."""
    if key is None:
        return None
    if not isinstance(key, str) or not _PRINTABLE_ASCII.fullmatch(key):
        raise ValidationError("idempotency_key must be 1-255 printable ASCII characters without spaces")
    return key
