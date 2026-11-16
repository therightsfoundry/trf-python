"""Backoff, ``Retry-After`` parsing and the charge-safe retry matrix.

The API charges catalogue/AI-search calls *before* doing the work, so a blind
retry of a call that succeeded server-side is a second charge. Every outcome is
therefore classified, and retries are gated on what the server guarantees about
money for that outcome (see ``docs/billing-and-retries.md``).

When the caller supplies an ``idempotency_key`` the request is sent with an
``Idempotency-Key`` header; once the API deduplicates on it, a retried
ambiguous outcome cannot charge twice, so ambiguous outcomes become retryable
under the default ``refund_backed`` policy too.
"""

from __future__ import annotations

import datetime as _dt
import re
import secrets
from email.utils import parsedate_to_datetime
from typing import Literal, Optional

__all__ = [
    "Outcome",
    "backoff_ms",
    "classify_status_outcome",
    "parse_retry_after",
    "should_retry",
]

Outcome = Literal[
    "connect",  # request never written
    "rate_limited",  # 429
    "billing_unavailable",  # 503 billing — not served, pending deduction cancelled
    "search_unavailable",  # 503 search — refunded best-effort
    "server_error",  # 500 — refund queued best-effort
    "ambiguous",  # 408/502/504/other 503/other retryable — the API server may have run
    "read",  # read timeout / reset after send
    "write",  # failure while sending — treated as ambiguous
]

_RNG = secrets.SystemRandom()
_DELTA = re.compile(r"^[0-9]{1,9}\Z")
_IMF_FIXDATE = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), [0-9]{2} "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [0-9]{4} "
    r"[0-9]{2}:[0-9]{2}:[0-9]{2} GMT\Z"
)


def backoff_ms(
    attempt: int,
    *,
    initial_ms: int,
    max_ms: int,
    jitter: str,
    rng: Optional[secrets.SystemRandom] = None,
) -> float:
    """Delay before retry ``attempt`` (1 = first retry), CSPRNG-jittered."""
    r = rng or _RNG
    base = float(min(max_ms, initial_ms * (2 ** max(0, attempt - 1))))
    if jitter == "full":
        return r.uniform(0, base)
    if jitter == "equal":
        return base / 2 + r.uniform(0, base / 2)
    return base


def parse_retry_after(value: Optional[str], *, now: Optional[_dt.datetime] = None) -> Optional[float]:
    """RFC 9110 ``Retry-After`` → seconds; ``None`` when absent or invalid.

    Accepts ``delta-seconds`` (1–9 ASCII digits) and IMF-fixdate HTTP-dates. A date
    in the past is ``0``. Negative, fractional or garbage values are ignored.
    """
    if value is None:
        return None
    text = value.strip()
    if _DELTA.fullmatch(text):
        return float(int(text))
    if _IMF_FIXDATE.fullmatch(text):
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        # The pattern requires the literal "GMT" zone, so ``when`` is always aware.
        current = now or _dt.datetime.now(_dt.timezone.utc)
        return max(0.0, (when - current).total_seconds())
    return None


_BILLING = "Billing is temporarily unavailable. Please retry shortly."
_SEARCH = "Search is temporarily unavailable, please retry."


def classify_status_outcome(status: int, error_text: Optional[str], code: Optional[str]) -> Outcome:
    if status == 429:
        return "rate_limited"
    if status == 500:
        return "server_error"
    if status == 503:
        if code == "billing_unavailable" or error_text == _BILLING:
            return "billing_unavailable"
        if code == "search_unavailable" or error_text == _SEARCH:
            return "search_unavailable"
    return "ambiguous"


_AMBIGUOUS: frozenset[str] = frozenset({"ambiguous", "read", "write"})


def should_retry(outcome: Outcome, retry_class: str, policy: str, *, idempotent: bool = False) -> bool:
    """The charge-safe retry matrix (``docs/billing-and-retries.md``).

    ``idempotent`` is true when the request carries an ``Idempotency-Key``:
    ambiguous outcomes (408, 502, 504, write/read-phase failures) are then safe
    to retry under ``refund_backed`` as well.
    """
    if outcome in ("connect", "rate_limited"):
        return True
    if retry_class in ("free", "sync"):
        return True
    # charged operations
    if policy == "always":
        return True
    if policy == "never":
        return False
    # refund_backed: only outcomes that carry an explicit refund / cancellation,
    # plus ambiguous ones when an idempotency key makes a replay harmless.
    if idempotent and outcome in _AMBIGUOUS:
        return True
    return outcome in ("billing_unavailable", "search_unavailable", "server_error")
