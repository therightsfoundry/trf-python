"""SEC-06: the one redactor every diagnostic string passes through.

Applied to exception messages, log lines, hook events and error ``body``
attributes. What it removes:

* ``Authorization`` / ``Proxy-Authorization`` header values,
* the configured API key's exact value (replaced by its fingerprint),
* any run of 128+ hex characters (any Rights Foundry token, including someone else's),
* ``sig=<value>`` in any URL (signed asset URLs are bearer capabilities),
* userinfo in any URL (``https://user:pass@host``),
* ``Authorization: …`` values in free text, and ``Bearer <token>`` fragments
  whose token is credential-shaped (``bearer capability`` is prose and stays).

The musical ``key`` query parameter (``key=A+minor``) is catalogue data and is
deliberately left alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Optional

from ._secret import Secret, _reveal

__all__ = [
    "RESPONSE_HEADER_ALLOWLIST",
    "_RedactingHeaders",
    "redact_headers",
    "redact_response_headers",
    "redact_text",
    "redact_url",
    "redact_value",
]

_HEX_RUN = re.compile(r"[0-9A-Fa-f]{128,}")
_SIG_PARAM = re.compile(r"(?<![A-Za-z0-9_])(sig=)[^&#\s\"'<>\\]*", re.IGNORECASE)
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/?#\s@\"'<>]*@")
_BEARER = re.compile(r"(?i)\b(bearer)\s+(?!\[REDACTED)([A-Za-z0-9._~+/=\-]+)")
#: ``Authorization: <anything>`` / ``Proxy-Authorization=<anything>`` in free text: the
#: header context alone is enough to redact the value, however short it is.
_AUTH_CONTEXT = re.compile(
    r"(?i)\b((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?)(?:(bearer|basic)\s+)?(?!\[REDACTED)([^\s\"',;]+)"
)
_TOKEN_PUNCT = frozenset("._~+/=-0123456789")


def _token_like(word: str) -> bool:
    """Whether the word after ``bearer`` looks like a credential rather than prose.

    A credential has a digit or token punctuation (``.``, ``-``, ``_``, ``~``,
    ``+``, ``/``, ``=``), mixed case after its first letter, or is at least 24
    characters long. ``"bearer capability"`` stays readable; ``"Bearer c0ffee…"``
    and ``"Bearer abc.def-ghi"`` do not.
    """
    if any(c in _TOKEN_PUNCT for c in word) or len(word) >= 24:
        return True
    tail = word[1:]
    return any(c.isupper() for c in tail) and any(c.islower() for c in tail)


def _auth_context_sub(match: re.Match[str]) -> str:
    scheme = match.group(2)
    return f"{match.group(1)}{scheme + ' ' if scheme else ''}[REDACTED]"


def _bearer_sub(match: re.Match[str]) -> str:
    return f"{match.group(1)} [REDACTED]" if _token_like(match.group(2)) else match.group(0)


#: Response headers that hooks and log lines may see. ``set-cookie`` never is.
RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "x-request-id",
        "retry-after",
        "cache-control",
        "cf-ray",
        "date",
        "x-trf-credits-charged",
        "x-trf-credits-spendable",
    }
)

_MAX_HOOK_HEADER = 8 * 1024
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization"})
_DROP_HEADERS = frozenset({"cookie", "set-cookie"})


def redact_text(text: str, secret: Optional[Secret] = None) -> str:
    """Return ``text`` with every secret pattern replaced."""
    if not text:
        return text
    out = text
    if secret is not None:
        raw = _reveal(secret)
        if raw:
            out = re.sub(re.escape(raw), secret.redacted(), out, flags=re.IGNORECASE)
    out = _HEX_RUN.sub("[REDACTED_TOKEN]", out)
    out = _SIG_PARAM.sub(r"\1[REDACTED]", out)
    out = _USERINFO.sub(r"\1[REDACTED]@", out)
    out = _AUTH_CONTEXT.sub(_auth_context_sub, out)
    out = _BEARER.sub(_bearer_sub, out)
    return out


def redact_url(url: str, secret: Optional[Secret] = None) -> str:
    """Redact ``sig`` and userinfo in a URL; every other query parameter is kept."""
    return redact_text(url, secret)


def redact_value(value: Any, secret: Optional[Secret] = None, _depth: int = 0) -> Any:
    """Deep-copy a JSON-like value with every string redacted.

    Covers ``download_url`` / ``asset_url`` / ``peak_data_url`` because their
    ``sig`` parameter is caught wherever it appears.
    """
    if _depth > 64:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_text(value, secret)
    if isinstance(value, Mapping):
        return {redact_text(str(k), secret): redact_value(v, secret, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, secret, _depth + 1) for v in value]
    return value


def redact_headers(
    headers: Iterable[tuple[str, str]] | Mapping[str, str],
    secret: Optional[Secret] = None,
) -> dict[str, str]:
    """Redact a request header collection for hooks and logs."""
    items: Iterable[tuple[str, str]] = headers.items() if isinstance(headers, Mapping) else headers
    out: dict[str, str] = {}
    for name, value in items:
        lname = name.lower()
        if lname in _DROP_HEADERS:
            continue
        if lname == "authorization":
            out[name] = f"Bearer {secret.redacted()}" if secret is not None else "Bearer [REDACTED]"
        elif lname == "proxy-authorization":
            out[name] = "[REDACTED]"
        else:
            out[name] = redact_text(value, secret)
    return out


def redact_response_headers(headers: Iterable[tuple[str, str]], secret: Optional[Secret] = None) -> dict[str, str]:
    """Allowlisted, size-capped, redacted response headers for hooks and logs."""
    out: dict[str, str] = {}
    for name, value in headers:
        lname = name.lower()
        if lname not in RESPONSE_HEADER_ALLOWLIST or len(value) > _MAX_HOOK_HEADER:
            continue
        out[lname] = redact_text(value, secret)
    return out


class _RedactingHeaders:
    """An ordered header list whose ``repr`` never shows a credential.

    Used for the outgoing header container so that a frame-locals capture by an
    error tracker shows ``[REDACTED]`` instead of the bearer token.
    """

    __slots__ = ("_items", "_secret")

    def __init__(self, items: Iterable[tuple[str, str]] = (), secret: Optional[Secret] = None) -> None:
        self._items: list[tuple[str, str]] = list(items)
        self._secret = secret

    def add(self, name: str, value: str) -> None:
        self._items.append((name, value))

    def get(self, name: str) -> Optional[str]:
        lname = name.lower()
        for k, v in self._items:
            if k.lower() == lname:
                return v
        return None

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None

    def items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def names(self) -> list[str]:
        return [k for k, _ in self._items]

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(list(self._items))

    def __len__(self) -> int:
        return len(self._items)

    def redacted(self) -> dict[str, str]:
        return redact_headers(self._items, self._secret)

    def __repr__(self) -> str:
        return f"_RedactingHeaders({self.redacted()!r})"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("request headers cannot be pickled")
