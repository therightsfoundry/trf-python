"""Typed errors and the exact response classifier.

Every exception holds only redacted data: it is built from redacted strings and a
redacted copy of the response body, never from the prepared request or the
header map. Exceptions pickle (for ``multiprocessing``) and the pickle carries
only that redacted state.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from typing import Any, Literal, Optional

__all__ = [
    "APIConnectionError",
    "APIStatusError",
    "APITimeoutError",
    "AuthenticationError",
    "BadRequestError",
    "BillingUnavailableError",
    "ChargeState",
    "ConfigurationError",
    "CreditBudgetExceededError",
    "CreditCapExceededError",
    "CreditGuardError",
    "CreditGuardUnavailableError",
    "DailyCallLimitError",
    "ExpiredTokenError",
    "FeatureNotEnabledError",
    "GoneError",
    "HealthCheckError",
    "InactiveTokenError",
    "InsufficientCreditsError",
    "InternalServerError",
    "InvalidTokenError",
    "InvalidTrackIdError",
    "MethodNotAllowedError",
    "NotFoundError",
    "PermissionDeniedError",
    "PreflightInsufficientCreditsError",
    "RateLimitError",
    "ResponseTooLargeError",
    "RightsFoundryError",
    "SearchUnavailableError",
    "ServiceUnavailableError",
    "SyncLinkError",
    "SyncLinkExpiredError",
    "SyncLinkInvalidError",
    "SyncMasterUnavailableError",
    "TLSVerificationError",
    "TrackNotFoundError",
    "UnexpectedRedirectError",
    "UnexpectedResponseError",
    "UnreachableFileError",
    "ValidationError",
    "IteratorConsumedError",
    "classify_api_error",
]

ChargeState = Literal["not_charged", "refund_pending", "charged", "unknown"]
RetryClass = Literal["charged", "sync", "free"]

#: Attributes every error carries plus the specialised ones. All
#: default to ``None`` so ``except RightsFoundryError as e: e.status`` is always safe.
_ATTRS: tuple[str, ...] = (
    "status",
    "request_id",
    "operation",
    "server_error",
    "server_message",
    "body",
    "charge_state",
    "retry_after",
    "attempts",
    # specialised
    "phase",
    "api_credits",
    "bandwidth_bytes",
    "feature",
    "limit",
    "reset_at",
    "projected",
    "cap",
    "cost_key",
    "spent_projected",
    "next_cost",
    "resource",
    "refund_pending",
    "location",
    "max_bytes",
    "observed_bytes",
    "code",
)


def _restore(cls: type[RightsFoundryError], state: dict[str, Any]) -> RightsFoundryError:
    obj = cls.__new__(cls)
    Exception.__init__(obj, state.get("message", ""))
    obj.__dict__.update(state)
    return obj


class RightsFoundryError(Exception):
    """Base class of every SDK error. Never raised directly."""

    message: str
    status: Optional[int]
    request_id: Optional[str]
    operation: Optional[str]
    server_error: Optional[str]
    server_message: Optional[str]
    body: Optional[Mapping[str, Any]]
    charge_state: Optional[ChargeState]
    retry_after: Optional[float]
    attempts: int
    phase: Optional[str]
    api_credits: Optional[int]
    bandwidth_bytes: Optional[int]
    feature: Optional[str]
    limit: Optional[int]
    reset_at: Optional[_dt.datetime]
    projected: Optional[int]
    cap: Optional[int]
    cost_key: Optional[str]
    spent_projected: Optional[int]
    next_cost: Optional[int]
    resource: Optional[str]
    refund_pending: Optional[bool]
    location: Optional[str]
    max_bytes: Optional[int]
    observed_bytes: Optional[int]
    code: Optional[str]

    #: charge_state used when the raiser does not say otherwise
    _default_charge_state: Optional[ChargeState] = None

    def __init__(self, message: str, **attrs: Any) -> None:
        super().__init__(message)
        self.message = message
        for name in _ATTRS:
            setattr(self, name, None)
        self.attempts = 0
        self.charge_state = self._default_charge_state
        for name, value in attrs.items():
            if name not in _ATTRS:
                raise TypeError(f"unknown error attribute {name!r}")
            if value is not None:
                setattr(self, name, value)

    def __str__(self) -> str:
        if self.request_id:
            return f"{self.message} (request_id={self.request_id})"
        return self.message

    def __repr__(self) -> str:
        parts = [repr(self.message)]
        for name in ("status", "request_id", "operation", "charge_state"):
            value = getattr(self, name, None)
            if value is not None:
                parts.append(f"{name}={value!r}")
        return f"{type(self).__name__}({', '.join(parts)})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (_restore, (type(self), dict(self.__dict__)))


# --- client-side ---------------------------------------------------------------


class ConfigurationError(RightsFoundryError):
    """An option was invalid at construction. Never echoes secret values."""

    _default_charge_state = "not_charged"


class ValidationError(RightsFoundryError):
    """A client-side input check failed; nothing was sent."""

    _default_charge_state = "not_charged"


class InvalidTrackIdError(ValidationError):
    """A track id was not a positive integer of at most 15 ASCII digits."""


class CreditGuardError(RightsFoundryError):
    """A client-side credit guard tripped; nothing was sent."""

    _default_charge_state = "not_charged"


class CreditCapExceededError(CreditGuardError):
    """The projected cost exceeds ``max_credits_per_request`` / ``max_credits``."""


class CreditBudgetExceededError(CreditGuardError):
    """An iterator's ``max_credits`` budget would be exceeded by the next page."""


class PreflightInsufficientCreditsError(CreditGuardError):
    """``auto_check_credits`` estimated the balance cannot cover this call."""


class CreditGuardUnavailableError(CreditGuardError):
    """The rate card was unreadable (or the price unknown) and ``guard_failure_mode='closed'``."""


class IteratorConsumedError(RightsFoundryError, RuntimeError):
    """A page iterator was iterated a second time.

    Iterators are single-use because every page is charged: re-iterating would
    silently re-send (and re-pay for) every page. Call ``tracks.iter(...)`` again
    for a fresh walk.
    """

    _default_charge_state = "not_charged"


# --- transport -------------------------------------------------------------------


class APIConnectionError(RightsFoundryError):
    """DNS, connection refused/reset, TLS handshake or a read failure.

    ``phase`` is ``"connect"`` (request never written, nothing charged) or
    ``"write"`` / ``"read"`` (the API may have received it).
    """


class APITimeoutError(APIConnectionError):
    """A deadline elapsed; ``phase`` is ``connect`` | ``read`` | ``write`` | ``total``."""


class TLSVerificationError(APIConnectionError):
    """Certificate or hostname verification failed. Never retried."""

    _default_charge_state = "not_charged"


class ResponseTooLargeError(RightsFoundryError):
    """A body exceeded ``max_response_bytes`` / ``max_download_bytes``."""


class UnexpectedRedirectError(RightsFoundryError):
    """The API or asset host answered 3xx. Redirects are never followed."""


class UnexpectedResponseError(RightsFoundryError):
    """A body was not the JSON shape expected (for example an edge HTML 502)."""


# --- API status ------------------------------------------------------------------


class APIStatusError(RightsFoundryError):
    """Any non-2xx JSON answer from the API."""


class BadRequestError(APIStatusError):
    """400 — rejected before any work; nothing charged."""

    _default_charge_state: Optional[ChargeState] = "not_charged"


class UnreachableFileError(BadRequestError):
    """400 from Acoustic Similarity Search: the provider could not fetch/decode ``file_url``.

    **This call was charged and is not refunded.** Fix the URL before retrying.
    """

    _default_charge_state: Optional[ChargeState] = "charged"


class AuthenticationError(APIStatusError):
    """401 — the Authorization header was missing or malformed."""

    _default_charge_state = "not_charged"


class InsufficientCreditsError(APIStatusError):
    """402 — the account cannot cover this call. ``api_credits`` / ``bandwidth_bytes`` hold the balance."""

    _default_charge_state = "not_charged"


class PermissionDeniedError(APIStatusError):
    """403."""

    _default_charge_state = "not_charged"


class InvalidTokenError(PermissionDeniedError):
    """403 Invalid API token (wrong key, or a key from the other environment)."""


class InactiveTokenError(PermissionDeniedError):
    """403 API token is inactive."""


class ExpiredTokenError(PermissionDeniedError):
    """403 API token has expired (MCP connections only)."""


class FeatureNotEnabledError(PermissionDeniedError):
    """403 — an ability is switched off for this token. ``feature`` is ``deep_search`` or ``sync``."""


class NotFoundError(APIStatusError):
    """404."""

    _default_charge_state = "not_charged"


class TrackNotFoundError(NotFoundError):
    """404 Track not found (free)."""


class SyncMasterUnavailableError(NotFoundError):
    """404 No sync master is available for this track (free), or the asset host's 404."""


class MethodNotAllowedError(APIStatusError):
    """405 — an SDK bug if ever seen from a typed method."""

    _default_charge_state = "not_charged"


class GoneError(APIStatusError):
    """410 — a retired endpoint (only reachable through ``client.request``)."""

    _default_charge_state = "not_charged"


class RateLimitError(APIStatusError):
    """429. ``retry_after`` holds the server's ``Retry-After`` in seconds."""

    _default_charge_state = "not_charged"


class DailyCallLimitError(RateLimitError):
    """429 daily call cap (MCP connections). ``limit`` and ``reset_at`` are set."""


class InternalServerError(APIStatusError):
    """500. On charged operations a best-effort refund is queued (``refund_pending``)."""


class ServiceUnavailableError(APIStatusError):
    """503. ``retry_after`` holds the server's ``Retry-After``."""


class BillingUnavailableError(ServiceUnavailableError):
    """503 Billing is temporarily unavailable — the request was not served.

    ``openapi.yaml`` calls the money outcome "deduction unconfirmed": a pending
    deduction is cancelled when still pending. Check ``credits.retrieve()`` if
    the ledger matters.
    """

    _default_charge_state = "not_charged"


class SearchUnavailableError(ServiceUnavailableError):
    """503 Search is temporarily unavailable — the charge is refunded best-effort."""

    _default_charge_state = "refund_pending"


# --- downloads / health ------------------------------------------------------------


class SyncLinkError(RightsFoundryError):
    """A signed asset URL failed validation or the asset host refused it."""


class SyncLinkExpiredError(SyncLinkError):
    """The signed link expired. Minting a new link after expiry is charged again."""


class SyncLinkInvalidError(SyncLinkError):
    """The signed link is malformed, not an allowlisted asset URL, or its signature was refused."""


class HealthCheckError(RightsFoundryError):
    """A health probe could not reach the API at all. A 503 answer is a value, not this."""


# --- classifier ------------------------------------------------------------------

_FEATURE_DEEP = "Deep Search is not enabled for this API token. Contact support to enable it."
_FEATURE_SYNC = "Sync licensing is not enabled for this API token. Contact support to enable it."

#: R-1 forward compatibility: stable machine codes, if the API adds them.
CODE_TABLE: Mapping[str, type[APIStatusError]] = {
    "token_invalid": InvalidTokenError,
    "token_inactive": InactiveTokenError,
    "token_expired": ExpiredTokenError,
    "feature_disabled": FeatureNotEnabledError,
    "insufficient_credits": InsufficientCreditsError,
    "track_not_found": TrackNotFoundError,
    "sync_master_unavailable": SyncMasterUnavailableError,
    "daily_limit": DailyCallLimitError,
    "billing_unavailable": BillingUnavailableError,
    "search_unavailable": SearchUnavailableError,
    "file_unreachable": UnreachableFileError,
    "validation_failed": BadRequestError,
}

#: The HTTP statuses each ``code`` may arrive with. A code on any other status is
#: ignored (classification falls back to the message and the status), so a
#: ``{"code": "validation_failed"}`` on a ``500`` can never be reported as a free
#: ``BadRequestError``.
CODE_STATUSES: Mapping[str, frozenset[int]] = {
    "token_invalid": frozenset({401, 403}),
    "token_inactive": frozenset({401, 403}),
    "token_expired": frozenset({401, 403}),
    "feature_disabled": frozenset({403}),
    "insufficient_credits": frozenset({402}),
    "track_not_found": frozenset({404}),
    "sync_master_unavailable": frozenset({404}),
    "daily_limit": frozenset({429}),
    "billing_unavailable": frozenset({503}),
    "search_unavailable": frozenset({503}),
    "file_unreachable": frozenset({400, 422}),
    "validation_failed": frozenset({400, 422}),
}

#: Exact ``(status, error)`` → class. Exact comparison only.
MESSAGE_TABLE: Mapping[tuple[int, str], type[APIStatusError]] = {
    (
        400,
        "Could not download or decode the audio at file_url. It must be a publicly reachable .mp3 or .wav under 120MB.",
    ): UnreachableFileError,
    (401, "Missing authorization header"): AuthenticationError,
    (402, "Insufficient credits. Please top up to continue."): InsufficientCreditsError,
    (403, "Invalid API token"): InvalidTokenError,
    (403, "API token is inactive"): InactiveTokenError,
    (403, "API token has expired"): ExpiredTokenError,
    (403, _FEATURE_DEEP): FeatureNotEnabledError,
    (403, _FEATURE_SYNC): FeatureNotEnabledError,
    (404, "Track not found"): TrackNotFoundError,
    (404, "No sync master is available for this track"): SyncMasterUnavailableError,
    (
        429,
        "Daily call limit reached for this connection. It resets at 00:00 UTC.",
    ): DailyCallLimitError,
    (503, "Billing is temporarily unavailable. Please retry shortly."): BillingUnavailableError,
    (503, "Search is temporarily unavailable, please retry."): SearchUnavailableError,
}

#: Known exact 400 messages (all ``BadRequestError``); kept for the conformance suite.
KNOWN_BAD_REQUEST_MESSAGES: tuple[str, ...] = (
    "limit must be an integer",
    "limit must be at least 1",
    "page must be an integer",
    "page must be at least 1",
    "Invalid track ID",
    "Request body must be valid JSON",
    "query is required",
    "query is no longer supported on this endpoint; use POST /v1/tracks/deep-search",
    "file_url is required",
    "file_url must be a valid URL",
    "file_url must be an http(s) URL",
    "file_url must point to an .mp3 or .wav file",
    "Bad Request",
)

#: Prefixes of the API's client-input validation messages. On the operations
#: where a 400 can be charged, a 400 whose text is neither a known message nor a
#: validation message is reported as ``charge_state="unknown"`` rather than
#: ``"not_charged"``: a server copy edit to the charged "could not download"
#: message must never be mislabelled as free.
VALIDATION_MESSAGE_PREFIXES: tuple[str, ...] = (
    "limit must",
    "page must",
    "query is",
    "query must",
    "file_url is",
    "file_url must",
    "instrumental must",
    "external_export must",
    "Request body",
    "Invalid track ID",
    "Unrecognized key",
    "Unknown field",
)

#: Operations on which an unrecognised 400 might be the charged unreachable-file answer.
CHARGED_400_OPERATIONS: tuple[str, ...] = ("tracks.search_similar", "tracks.deep_search")


def _is_known_validation_message(text: Optional[str]) -> bool:
    if text is None:
        return False
    return text in KNOWN_BAD_REQUEST_MESSAGES or text.startswith(VALIDATION_MESSAGE_PREFIXES)


def _status_fallback(status: int) -> type[APIStatusError]:
    if status == 400:
        return BadRequestError
    if status == 401:
        return AuthenticationError
    if status == 402:
        return InsufficientCreditsError
    if status == 403:
        return PermissionDeniedError
    if status == 404:
        return NotFoundError
    if status == 405:
        return MethodNotAllowedError
    if status == 410:
        return GoneError
    if status == 429:
        return RateLimitError
    if status == 500:
        return InternalServerError
    if status == 503:
        return ServiceUnavailableError
    return APIStatusError


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _charge_state_for(cls: type[APIStatusError], status: int, retry_class: RetryClass) -> ChargeState:
    explicit = cls._default_charge_state
    if explicit is not None:
        return explicit
    if retry_class == "free":
        return "not_charged"
    if 400 <= status < 500 and status not in (408,):
        return "not_charged"
    if status == 500:
        return "refund_pending"
    return "unknown"


def classify_api_error(
    status: int,
    body: Optional[Mapping[str, Any]],
    *,
    operation: Optional[str],
    request_id: Optional[str],
    retry_after: Optional[float],
    attempts: int,
    retry_class: RetryClass,
    redacted_message: Optional[str] = None,
    redacted_detail: Optional[str] = None,
    redacted_body: Optional[Mapping[str, Any]] = None,
) -> APIStatusError:
    """Map a non-2xx JSON answer to its typed error.

    ``body`` is the parsed (unredacted) body and is used only for matching;
    the error stores ``redacted_body`` and the redacted message strings.
    """
    body = body or {}
    raw_error = body.get("error") if isinstance(body.get("error"), str) else None
    raw_detail = body.get("message") if isinstance(body.get("message"), str) else None
    code = body.get("code") if isinstance(body.get("code"), str) else None

    cls: Optional[type[APIStatusError]] = None
    if code is not None and status in CODE_STATUSES.get(code, frozenset()):
        # A code is honoured only on the statuses it can legitimately arrive with,
        # so a class never crosses into an unrelated status family.
        cls = CODE_TABLE.get(code)
    if cls is None and raw_error is not None:
        cls = MESSAGE_TABLE.get((status, raw_error))
    if cls is None:
        cls = _status_fallback(status)

    err_text = redacted_message if redacted_message is not None else (raw_error or "")
    detail_text = redacted_detail if redacted_detail is not None else raw_detail
    message = f"HTTP {status}"
    if err_text:
        message = f"{message}: {err_text}"
    if detail_text and detail_text != err_text:
        message = f"{message} — {detail_text}"
    if operation:
        message = f"{operation} failed with {message}"

    attrs: dict[str, Any] = {
        "status": status,
        "request_id": request_id,
        "operation": operation,
        "server_error": err_text or None,
        "server_message": detail_text,
        "body": redacted_body,
        "retry_after": retry_after,
        "attempts": attempts,
        "code": code,
    }

    if issubclass(cls, InsufficientCreditsError):
        attrs["api_credits"] = _int_or_none(body.get("apiCredits", body.get("api_credits")))
        attrs["bandwidth_bytes"] = _int_or_none(body.get("bandwidthBytes", body.get("bandwidth_bytes")))
    if issubclass(cls, FeatureNotEnabledError):
        feature = body.get("feature")
        if isinstance(feature, str):
            attrs["feature"] = feature
        elif raw_error == _FEATURE_SYNC or (operation or "").endswith(".sync"):
            attrs["feature"] = "sync"
        else:
            attrs["feature"] = "deep_search"
    if issubclass(cls, DailyCallLimitError):
        attrs["limit"] = _int_or_none(body.get("limit"))
        now = _dt.datetime.now(_dt.timezone.utc)
        if retry_after is not None:
            attrs["reset_at"] = now + _dt.timedelta(seconds=retry_after)
        else:
            tomorrow = (now + _dt.timedelta(days=1)).date()
            attrs["reset_at"] = _dt.datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=_dt.timezone.utc)
    charge_state = _charge_state_for(cls, status, retry_class)
    if (
        cls is BadRequestError
        and status == 400
        and (operation or "").endswith(CHARGED_400_OPERATIONS)
        and not _is_known_validation_message(raw_error)
    ):
        charge_state = "unknown"
    attrs["charge_state"] = charge_state
    if charge_state == "refund_pending":
        attrs["refund_pending"] = True
    return cls(message, **attrs)
