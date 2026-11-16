"""Per-call options shared by every typed method and ``client.request``."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from ._errors import ValidationError
from ._validation import validate_idempotency_key

__all__ = ["CallOptions", "resolve_options", "validate_timeout"]


def validate_timeout(value: Any) -> Optional[float]:
    """A per-call timeout in seconds, ``0 < t <= 600`` (``None`` = the client's)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= 600:
        raise ValidationError("timeout must be a number of seconds in (0, 600]")
    return float(value)


@dataclass(frozen=True)
class CallOptions:
    """Options for one call. Every field defaults to "use the client's setting".

    * ``timeout`` — read timeout for this call, in seconds (0 < t ≤ 600); it also
      caps the connect timeout.
    * ``max_credits`` — refuse to send if the call is projected to cost more
      (overrides ``max_credits_per_request``). Ignored by free endpoints.
    * ``max_retries`` — retry budget for this call (0–10), overriding the client's.
    * ``idempotency_key`` — sent as ``Idempotency-Key`` (1–255 printable ASCII
      characters without spaces). With a key set, ambiguous outcomes (408, 502,
      504, write/read failures) are retried even under ``refund_backed``, because
      a server that honours the header cannot charge the replay twice.
    * ``send_max_credits_header`` — when a cap applies, also send it to the API as
      ``X-TRF-Max-Credits: <int>`` so a server that supports it can enforce it too.

    Invalid values raise :class:`~rightsfoundry.ValidationError` at construction.
    """

    timeout: Optional[float] = None
    max_credits: Optional[int] = None
    max_retries: Optional[int] = None
    idempotency_key: Optional[str] = None
    send_max_credits_header: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "timeout", validate_timeout(self.timeout))
        credits = self.max_credits
        if credits is not None and (isinstance(credits, bool) or not isinstance(credits, int) or credits < 0):
            raise ValidationError("max_credits must be a non-negative integer")
        retries = self.max_retries
        if retries is not None and (
            isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10
        ):
            raise ValidationError("max_retries must be an integer from 0 to 10")
        validate_idempotency_key(self.idempotency_key)
        if not isinstance(self.send_max_credits_header, bool):
            raise ValidationError("send_max_credits_header must be a bool")


DEFAULT_OPTIONS = CallOptions()


def resolve_options(
    options: Optional[CallOptions] = None,
    *,
    timeout: Any = None,
    max_credits: Any = None,
    max_retries: Any = None,
    idempotency_key: Any = None,
    send_max_credits_header: Any = None,
) -> CallOptions:
    """Merge per-call keywords over an optional :class:`CallOptions` (keywords win; ``None`` = not given)."""
    if options is not None and not isinstance(options, CallOptions):
        raise ValidationError("options must be a rightsfoundry.CallOptions")
    base = options or DEFAULT_OPTIONS
    changes: dict[str, Any] = {
        name: value
        for name, value in (
            ("timeout", timeout),
            ("max_credits", max_credits),
            ("max_retries", max_retries),
            ("idempotency_key", idempotency_key),
            ("send_max_credits_header", send_max_credits_header),
        )
        if value is not None
    }
    return replace(base, **changes) if changes else base
