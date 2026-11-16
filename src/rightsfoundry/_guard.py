"""Client-side credit & bandwidth guardrails (``docs/billing-and-retries.md``).

All guards are advisory conveniences that stop a bug in *your* loop from
spending an account's balance. The API's billing service and its ``402`` remain the only
authority on what an account can spend.

The guard holds pure, lock-protected state. It never performs I/O itself: the
sync and async pipelines fetch ``GET /v1/credits`` when :meth:`needs_fetch`
says so and feed the result back through :meth:`observe_credits`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable, Optional

from ._errors import (
    CreditCapExceededError,
    CreditGuardUnavailableError,
    PreflightInsufficientCreditsError,
)
from ._models import Credits, LowCreditEvent

__all__ = ["CreditGuard"]


class _Snapshot:
    __slots__ = (
        "as_of",
        "balance",
        "bandwidth_spendable",
        "overdraft_limit",
        "public_code",
        "spendable",
        "taken_at",
    )

    def __init__(
        self,
        *,
        spendable: int,
        balance: Optional[int],
        overdraft_limit: Optional[int],
        bandwidth_spendable: Optional[int],
        as_of: Optional[str],
        public_code: Optional[str],
        taken_at: float,
    ) -> None:
        self.spendable = spendable
        self.balance = balance
        self.overdraft_limit = overdraft_limit
        self.bandwidth_spendable = bandwidth_spendable
        self.as_of = as_of
        self.public_code = public_code
        self.taken_at = taken_at


class CreditGuard:
    """Rate card cache, balance snapshot and low-credit edge trigger for one client."""

    def __init__(
        self,
        *,
        max_credits_per_request: Optional[int],
        auto_check_credits: bool,
        credit_check_ttl_ms: int,
        rate_card_ttl_ms: int,
        guard_failure_mode: str,
        min_bandwidth_bytes: Optional[int],
        low_credit_warning_threshold: Optional[int],
        on_low_credit: Optional[Callable[[LowCreditEvent], Any]],
        logger: logging.Logger,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_credits_per_request = max_credits_per_request
        self.auto_check_credits = auto_check_credits
        self.credit_check_ttl = credit_check_ttl_ms / 1000.0
        self.rate_card_ttl = rate_card_ttl_ms / 1000.0
        self.guard_failure_mode = guard_failure_mode
        self.min_bandwidth_bytes = min_bandwidth_bytes
        self.threshold = low_credit_warning_threshold
        self.on_low_credit = on_low_credit
        self.logger = logger
        self._clock = clock
        self._lock = threading.Lock()
        self._rate_card: Optional[dict[str, int]] = None
        self._rate_card_at = 0.0
        self._snapshot: Optional[_Snapshot] = None
        self._spent_since_snapshot = 0
        self._armed = True

    # --- state queries ---------------------------------------------------------------

    def rate_card(self) -> Optional[Mapping[str, int]]:
        with self._lock:
            return dict(self._rate_card) if self._rate_card is not None else None

    def _rate_card_fresh(self, now: float) -> bool:
        return self._rate_card is not None and now - self._rate_card_at < self.rate_card_ttl

    def _snapshot_fresh(self, now: float) -> bool:
        return self._snapshot is not None and now - self._snapshot.taken_at < self.credit_check_ttl

    def needs_fetch(self, *, charged: bool, max_credits: Optional[int], budget: bool = False) -> bool:
        """Whether a ``GET /v1/credits`` is needed before this charged call."""
        if not charged:
            return False
        now = self._clock()
        with self._lock:
            if self.auto_check_credits and not self._snapshot_fresh(now):
                return True
            capped = max_credits is not None or self.max_credits_per_request is not None or budget
            return capped and not self._rate_card_fresh(now)

    def project(self, cost_key: Optional[str]) -> Optional[int]:
        """Projected credits for ``cost_key`` from the cached rate card (``None`` = unknown)."""
        if cost_key is None:
            return 0
        with self._lock:
            if self._rate_card is None:
                return None
            return self._rate_card.get(cost_key)

    # --- enforcement -----------------------------------------------------------------

    def unavailable(self, reason: str, *, operation: str, cost_key: Optional[str]) -> None:
        """Fail closed (raise) or open (log) when a projection cannot be made."""
        if self.guard_failure_mode == "open":
            self.logger.warning(
                "rightsfoundry: credit guard unavailable for %s (%s); proceeding because guard_failure_mode='open'",
                operation,
                reason,
            )
            return
        raise CreditGuardUnavailableError(
            f"{operation}: credit guard unavailable ({reason}); nothing was sent. "
            "Set guard_failure_mode='open' to proceed without a projection.",
            operation=operation,
            cost_key=cost_key,
        )

    def enforce(
        self,
        *,
        operation: str,
        cost_key: Optional[str],
        max_credits: Optional[int],
    ) -> Optional[int]:
        """Raise if the call must not be sent; return the projected cost (or ``None``)."""
        cap = max_credits if max_credits is not None else self.max_credits_per_request
        projected = self.project(cost_key)
        if cap is not None or self.auto_check_credits:
            if projected is None:
                self.unavailable(
                    f"the rate card has no price for {cost_key!r}"
                    if self.rate_card() is not None
                    else "the rate card could not be read",
                    operation=operation,
                    cost_key=cost_key,
                )
                return None
        if cap is not None and projected is not None and projected > cap:
            raise CreditCapExceededError(
                f"{operation} is projected to cost {projected} credits ({cost_key}), above the "
                f"cap of {cap}; nothing was sent. Pass max_credits= to allow this one call.",
                operation=operation,
                projected=projected,
                cap=cap,
                cost_key=cost_key,
            )
        if self.auto_check_credits and projected is not None:
            with self._lock:
                snap = self._snapshot
                spent = self._spent_since_snapshot
            if snap is None:
                self.unavailable("no balance snapshot", operation=operation, cost_key=cost_key)
                return projected
            estimate = snap.spendable - spent
            if estimate - projected < 0:
                raise PreflightInsufficientCreditsError(
                    f"{operation} needs {projected} credits but the estimated spendable balance is "
                    f"{estimate}; nothing was sent. Top up at https://therightsfoundry.com/dashboard/billing",
                    operation=operation,
                    projected=projected,
                    cost_key=cost_key,
                    api_credits=estimate,
                    resource="api_credits",
                )
            if (
                self.min_bandwidth_bytes is not None
                and snap.bandwidth_spendable is not None
                and snap.bandwidth_spendable < self.min_bandwidth_bytes
            ):
                raise PreflightInsufficientCreditsError(
                    f"{operation}: spendable bandwidth {snap.bandwidth_spendable} bytes is below "
                    f"min_bandwidth_bytes={self.min_bandwidth_bytes}; nothing was sent.",
                    operation=operation,
                    bandwidth_bytes=snap.bandwidth_spendable,
                    resource="bandwidth",
                )
        return projected

    def record_spend(self, credits: Optional[int]) -> None:
        """Account for a charged call that was sent (projected or actual credits)."""
        if not credits:
            return
        with self._lock:
            snap = self._snapshot
            self._spent_since_snapshot += credits
            estimate = None if snap is None else snap.spendable - self._spent_since_snapshot
        if self.auto_check_credits and estimate is not None and snap is not None:
            self._observe_spendable(
                estimate,
                snap.balance,
                snap.overdraft_limit,
                "estimate",
                snap.as_of,
                snap.public_code,
            )

    # --- observations ----------------------------------------------------------------

    def observe_credits(self, credits: Credits) -> None:
        now = self._clock()
        with self._lock:
            self._rate_card = dict(credits.costs)
            self._rate_card_at = now
            self._snapshot = _Snapshot(
                spendable=credits.api_credits.spendable,
                balance=credits.api_credits.balance,
                overdraft_limit=credits.api_credits.overdraft_limit,
                bandwidth_spendable=credits.bandwidth_bytes.spendable,
                as_of=credits.as_of,
                public_code=credits.token.public_code,
                taken_at=now,
            )
            self._spent_since_snapshot = 0
        self._observe_spendable(
            credits.api_credits.spendable,
            credits.api_credits.balance,
            credits.api_credits.overdraft_limit,
            "credits",
            credits.as_of,
            credits.token.public_code,
        )

    def observe_insufficient(self, api_credits: Optional[int]) -> None:
        """A ``402`` body: ``apiCredits`` is the balance before the refused charge."""
        if api_credits is None:
            return
        with self._lock:
            snap = self._snapshot
        overdraft = snap.overdraft_limit if snap is not None else None
        spendable = api_credits + (overdraft or 0)
        self._observe_spendable(
            spendable,
            api_credits,
            overdraft,
            "insufficient_credits",
            None,
            snap.public_code if snap is not None else None,
        )

    def observe_headers(self, headers: Mapping[str, str]) -> None:
        """Forward-compatible with API R-3: ``X-TRF-Credits-Spendable``."""
        raw = headers.get("x-trf-credits-spendable")
        if raw is None:
            return
        try:
            spendable = int(raw.strip())
        except ValueError:
            return
        now = self._clock()
        with self._lock:
            snap = self._snapshot
            self._snapshot = _Snapshot(
                spendable=spendable,
                balance=None,
                overdraft_limit=snap.overdraft_limit if snap is not None else None,
                bandwidth_spendable=snap.bandwidth_spendable if snap is not None else None,
                as_of=None,
                public_code=snap.public_code if snap is not None else None,
                taken_at=now,
            )
            self._spent_since_snapshot = 0
        self._observe_spendable(spendable, None, None, "response_header", None, None)

    def _observe_spendable(
        self,
        spendable: int,
        balance: Optional[int],
        overdraft: Optional[int],
        source: str,
        as_of: Optional[str],
        public_code: Optional[str],
    ) -> None:
        threshold = self.threshold
        if threshold is None:
            return
        fire = False
        with self._lock:
            if spendable < threshold:
                if self._armed:
                    self._armed = False
                    fire = True
            else:
                self._armed = True
        if not fire:
            return
        event = LowCreditEvent(
            spendable=spendable,
            balance=balance,
            overdraft_limit=overdraft,
            threshold=threshold,
            source=source,
            as_of=as_of,
            token_public_code=public_code,
        )
        if self.on_low_credit is None:
            self.logger.warning(
                "rightsfoundry: spendable API credits %s dropped below the warning threshold %s (source=%s)",
                spendable,
                threshold,
                source,
            )
            return
        try:
            result = self.on_low_credit(event)
            if hasattr(result, "__await__"):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                self.logger.warning("rightsfoundry: on_low_credit returned an awaitable; it is not awaited")
        except Exception:  # hooks must never fail a request
            self.logger.warning("rightsfoundry: on_low_credit callback raised", exc_info=True)
