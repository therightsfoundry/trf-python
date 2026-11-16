"""The 13-step request pipeline shared by ``Client`` and ``AsyncClient``.

1 validate inputs (resources) ─► 2 build URL ─► 3 credit guard ─► 4 headers
─► 5 ``on_request`` ─► 6 ``transport.send`` ─► 7 bounded body read ─► 8 classify
─► 9 retry loop ─► 10 ``on_response`` / ``on_error`` ─► 11 decode JSON
─► 12 build model ─► 13 credit observations (low-credit callback).

All decisions live in :class:`PipelineCore` as pure, synchronous methods. The
sync and async drivers differ only in how they wait and do I/O, so the two
clients cannot drift apart.
"""

from __future__ import annotations

import asyncio
import json
import platform
import sys
import threading
import time
import uuid
import zlib
from collections.abc import Mapping
from typing import Any, Callable, Optional, cast
from urllib.parse import urlencode

from ._config import ClientConfig
from ._endpoints import ENDPOINTS, OperationSpec
from ._errors import (
    APIConnectionError,
    APITimeoutError,
    InsufficientCreditsError,
    ResponseTooLargeError,
    RightsFoundryError,
    TLSVerificationError,
    UnexpectedRedirectError,
    UnexpectedResponseError,
    classify_api_error,
)
from ._guard import CreditGuard
from ._models import Credits, ErrorEvent, RequestEvent, ResponseEvent, SyncLink
from ._options import CallOptions
from ._redact import _RedactingHeaders, redact_response_headers, redact_text, redact_value
from ._retry import Outcome, backoff_ms, classify_status_outcome, parse_retry_after, should_retry
from ._secret import _reveal
from ._transport import AsyncRawResponse, Headers, PreparedRequest, RawResponse
from ._version import __version__

__all__ = ["AsyncPipeline", "BodyReader", "PipelineCore", "SyncPipeline", "parse_json"]

_MAX_DEPTH = 64
_HOOK_BODY_LIMIT = 64 * 1024
_coroutine_warned = False


def user_agent(suffix: Optional[str]) -> str:
    base = (
        f"rightsfoundry-python/{__version__} ({platform.python_implementation()} "
        f"{platform.python_version()}; {sys.platform}; {platform.machine() or 'unknown'})"
    )
    return f"{base} {suffix}" if suffix else base


# --- SEC-16: bounded bodies ---------------------------------------------------------------


class BodyReader:
    """Accumulates a response body, counting **decoded** bytes against a hard limit.

    Gzip is inflated with ``zlib.decompressobj(16 + MAX_WBITS)`` and ``max_length``
    per call, so a decompression bomb can never allocate past the limit.
    """

    def __init__(self, limit: int, headers: Headers) -> None:
        self.limit = limit
        self._parts: list[bytes] = []
        self._size = 0
        encoding = (headers.get("content-encoding") or "").strip().lower()
        if encoding in ("", "identity"):
            self._inflater: Optional[Any] = None
        elif encoding in ("gzip", "x-gzip"):
            self._inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._inflater = zlib.decompressobj()
        else:
            raise UnexpectedResponseError(f"unsupported Content-Encoding {encoding!r}")
        length = headers.get("content-length")
        if self._inflater is None and length is not None and length.strip().isdigit():
            declared = int(length.strip())
            if declared > limit:
                raise ResponseTooLargeError(
                    f"response declares {declared} bytes, above max_response_bytes={limit}",
                    max_bytes=limit,
                    observed_bytes=declared,
                )

    def _add(self, data: bytes) -> None:
        if not data:
            return
        self._size += len(data)
        if self._size > self.limit:
            raise ResponseTooLargeError(
                f"response exceeded max_response_bytes={self.limit}",
                max_bytes=self.limit,
                observed_bytes=self._size,
            )
        self._parts.append(data)

    def feed(self, chunk: bytes) -> None:
        if self._inflater is None:
            self._add(chunk)
            return
        data = chunk
        while data:
            room = self.limit - self._size + 1
            try:
                out = self._inflater.decompress(data, room)
            except zlib.error as exc:
                raise UnexpectedResponseError(f"invalid compressed body: {exc}") from None
            self._add(out)
            data = self._inflater.unconsumed_tail

    def finish(self) -> bytes:
        if self._inflater is not None:
            try:
                self._add(self._inflater.flush(self.limit - self._size + 1))
            except zlib.error as exc:
                raise UnexpectedResponseError(f"invalid compressed body: {exc}") from None
        return b"".join(self._parts)


# --- SEC-17: safe JSON -------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _check_depth(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            if depth > _MAX_DEPTH:
                raise UnexpectedResponseError("response JSON nesting exceeds 64 levels")
            stack.extend((v, depth + 1) for v in current.values())
        elif isinstance(current, list):
            if depth > _MAX_DEPTH:
                raise UnexpectedResponseError("response JSON nesting exceeds 64 levels")
            stack.extend((v, depth + 1) for v in current)


def parse_json(body: bytes) -> Any:
    """``json.loads`` that rejects NaN/Infinity, invalid UTF-8 and nesting deeper than 64."""
    try:
        text = body.decode("utf-8")
        value = json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise UnexpectedResponseError("response JSON nesting exceeds 64 levels") from None
    except (UnicodeDecodeError, ValueError) as exc:
        raise UnexpectedResponseError(f"response is not valid JSON ({type(exc).__name__})") from None
    _check_depth(value)
    return value


# --- per-call state ------------------------------------------------------------------------


class _Call:
    __slots__ = (
        "attempt",
        "body",
        "cost_key",
        "deadline",
        "headers",
        "method",
        "op",
        "projected",
        "redacted_url",
        "retried_after_refundable",
        "sdk_call_id",
        "url",
    )

    def __init__(self, op: OperationSpec) -> None:
        self.op = op
        self.sdk_call_id = str(uuid.uuid4())
        self.attempt = 0
        self.method = op.endpoint.method
        self.url = ""
        self.redacted_url = ""
        self.headers = _RedactingHeaders()
        self.body: Optional[bytes] = None
        self.cost_key = op.cost_key
        self.projected: Optional[int] = None
        self.deadline: Optional[float] = None
        self.retried_after_refundable = False


class _Decision:
    __slots__ = ("delay_ms", "error", "retry", "value")

    def __init__(
        self,
        *,
        retry: bool = False,
        delay_ms: float = 0.0,
        error: Optional[RightsFoundryError] = None,
        value: Any = None,
    ) -> None:
        self.retry = retry
        self.delay_ms = delay_ms
        self.error = error
        self.value = value


class PipelineCore:
    """Pure decision logic. No sleeping, no sockets."""

    def __init__(self, config: ClientConfig, guard: CreditGuard) -> None:
        self.config = config
        self.guard = guard
        self.log = config.logger
        self._user_agent = user_agent(config.user_agent_suffix)
        self._http_warned = False
        self._warn_lock = threading.Lock()

    # step 2 + 4 ------------------------------------------------------------------------

    def prepare(self, op: OperationSpec) -> _Call:
        call = _Call(op)
        cfg = self.config
        url = f"{cfg.base_url}{op.path}"
        if op.query:
            url = f"{url}?{urlencode(list(op.query))}"
        call.url = url
        call.redacted_url = redact_text(url, cfg.api_key)
        headers = _RedactingHeaders(secret=cfg.api_key)
        headers.add("User-Agent", self._user_agent)
        headers.add("Accept", "application/json")
        headers.add("Accept-Encoding", "gzip")
        for name, value in cfg.custom_headers:
            headers.add(name, value)
        options = op.options
        if options.idempotency_key is not None:
            headers.add("Idempotency-Key", options.idempotency_key)
        if options.send_max_credits_header and op.endpoint.charged:
            cap = options.max_credits if options.max_credits is not None else cfg.max_credits_per_request
            if cap is not None:
                headers.add("X-TRF-Max-Credits", str(cap))
        if op.json_body is not None:
            call.body = json.dumps(op.json_body, allow_nan=False, separators=(",", ":")).encode("utf-8")
            headers.add("Content-Type", "application/json")
        elif op.endpoint.method in ("POST", "PUT", "PATCH"):
            call.body = b""
        if op.endpoint.auth == "bearer":
            if cfg.api_key is None:
                raise APIConnectionError(
                    f"{op.operation} requires an API key",
                    phase="connect",
                    charge_state="not_charged",
                )
            headers.add("Authorization", _auth_header_value(cfg))
        call.headers = headers
        if cfg.total_timeout is not None:
            call.deadline = time.monotonic() + cfg.total_timeout
        return call

    def timeouts(self, call: _Call) -> dict[str, float]:
        cfg = self.config
        connect = cfg.connect_timeout
        read = call.op.timeout if call.op.timeout is not None else cfg.read_timeout
        write = cfg.write_timeout
        if call.op.timeout is not None:
            connect = min(connect, call.op.timeout)
        if call.deadline is not None:
            remaining = max(0.05, call.deadline - time.monotonic())
            connect, read, write = (
                min(connect, remaining),
                min(read, remaining),
                min(write, remaining),
            )
        return {"connect_timeout": connect, "read_timeout": read, "write_timeout": write}

    def check_deadline(self, call: _Call) -> None:
        if call.deadline is not None and time.monotonic() >= call.deadline:
            raise self._finalize(
                APITimeoutError(
                    f"{call.op.operation}: total_timeout of {self.config.total_timeout:g}s elapsed",
                    phase="total",
                    charge_state="unknown" if call.attempt > 1 else "not_charged",
                ),
                call,
            )

    def request_for(self, call: _Call) -> PreparedRequest:
        return PreparedRequest(call.method, call.url, call.headers, call.body)

    # hooks ---------------------------------------------------------------------------

    def _hook_body(self, value: Any) -> Any:
        redacted = redact_value(value, self.config.api_key)
        try:
            text = json.dumps(redacted)
        except (TypeError, ValueError):
            return None
        if len(text) > _HOOK_BODY_LIMIT:
            return {"truncated": True, "bytes": len(text)}
        return redacted

    def _dispatch(self, hook: Optional[Callable[[Any], Any]], event: Any, name: str) -> None:
        global _coroutine_warned
        if hook is None:
            return
        try:
            result = hook(event)
        except Exception:  # a hook must never fail the request
            self.log.warning("rightsfoundry: %s hook raised; ignoring", name, exc_info=True)
            return
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            if not _coroutine_warned:
                _coroutine_warned = True
                self.log.warning("rightsfoundry: %s hook returned an awaitable; hooks are not awaited", name)

    def emit_request(self, call: _Call) -> None:
        cfg = self.config
        if cfg.on_request is None:
            return
        body = None
        if cfg.hook_include_bodies and call.op.json_body is not None:
            body = self._hook_body(dict(call.op.json_body))
        self._dispatch(
            cfg.on_request,
            RequestEvent(
                operation=call.op.operation,
                sdk_call_id=call.sdk_call_id,
                attempt=call.attempt,
                method=call.method,
                url=call.redacted_url,
                headers=call.headers.redacted(),
                cost_key=call.cost_key,
                projected_credits=call.projected,
                body=body,
            ),
            "on_request",
        )

    def _emit_response(
        self,
        call: _Call,
        status: int,
        headers: Headers,
        duration_ms: float,
        decision: _Decision,
        parsed: Any,
    ) -> None:
        cfg = self.config
        request_id = headers.get("x-request-id")
        self.log.debug(
            "rightsfoundry %s %s %s -> %s request_id=%s %.0fms attempt=%s%s",
            call.op.operation,
            call.method,
            call.redacted_url,
            status,
            request_id,
            duration_ms,
            call.attempt,
            f" retry_in={decision.delay_ms:.0f}ms" if decision.retry else "",
        )
        if cfg.on_response is None:
            return
        body = self._hook_body(parsed) if cfg.hook_include_bodies and parsed is not None else None
        self._dispatch(
            cfg.on_response,
            ResponseEvent(
                operation=call.op.operation,
                sdk_call_id=call.sdk_call_id,
                attempt=call.attempt,
                method=call.method,
                url=call.redacted_url,
                status=status,
                headers=redact_response_headers(headers.items(), cfg.api_key),
                request_id=request_id,
                duration_ms=duration_ms,
                will_retry=decision.retry,
                retry_in_ms=decision.delay_ms if decision.retry else None,
                cost_key=call.cost_key,
                projected_credits=call.projected,
                body=body,
            ),
            "on_response",
        )

    def _emit_error(
        self,
        call: _Call,
        error: RightsFoundryError,
        decision: _Decision,
        duration_ms: Optional[float],
    ) -> None:
        cfg = self.config
        if error.status is None:
            self.log.debug(
                "rightsfoundry %s %s %s failed: %s attempt=%s%s",
                call.op.operation,
                call.method,
                call.redacted_url,
                error.message,
                call.attempt,
                f" retry_in={decision.delay_ms:.0f}ms" if decision.retry else "",
            )
        if cfg.on_error is None:
            return
        self._dispatch(
            cfg.on_error,
            ErrorEvent(
                operation=call.op.operation,
                sdk_call_id=call.sdk_call_id,
                attempt=call.attempt,
                method=call.method,
                url=call.redacted_url,
                error=error,
                charge_state=error.charge_state,
                status=error.status,
                request_id=error.request_id,
                duration_ms=duration_ms,
                will_retry=decision.retry,
                retry_in_ms=decision.delay_ms if decision.retry else None,
                cost_key=call.cost_key,
                projected_credits=call.projected,
            ),
            "on_error",
        )

    # retry decisions -------------------------------------------------------------------

    def _finalize(self, error: RightsFoundryError, call: _Call) -> RightsFoundryError:
        # Errors raised by an injected transport may carry anything: redact them too.
        error.message = redact_text(error.message, self.config.api_key)
        error.args = (error.message,)
        error.operation = error.operation or call.op.operation
        error.attempts = call.attempt
        retry_class = call.op.endpoint.retry_class
        if retry_class == "free" and error.charge_state in ("unknown", "refund_pending", None):
            error.charge_state = "not_charged"
        if error.charge_state is None:
            error.charge_state = "unknown"
        if not error.message.startswith(call.op.operation) and error.status is None:
            error.message = f"{call.op.operation}: {error.message}"
            error.args = (error.message,)
        return error

    def _retry_delay(self, call: _Call, retry_after: Optional[float]) -> Optional[float]:
        cfg = self.config
        delay = backoff_ms(
            call.attempt,
            initial_ms=cfg.retry_initial_delay_ms,
            max_ms=cfg.retry_max_delay_ms,
            jitter=cfg.jitter,
        )
        if retry_after is not None:
            retry_after_ms = retry_after * 1000.0
            if retry_after_ms > cfg.max_retry_after_ms:
                return None
            delay = max(delay, retry_after_ms)
        if call.deadline is not None and time.monotonic() + delay / 1000.0 >= call.deadline:
            return None
        return delay

    def max_retries(self, op: OperationSpec) -> int:
        """Per-call ``max_retries`` → the endpoint's default (0 for health probes) → the client's."""
        if op.options.max_retries is not None:
            return op.options.max_retries
        if op.endpoint.default_max_retries is not None:
            return op.endpoint.default_max_retries
        return self.config.max_retries

    def _decide(
        self,
        call: _Call,
        outcome: Optional[Outcome],
        retry_after: Optional[float],
        status: Optional[int],
    ) -> _Decision:
        cfg = self.config
        if outcome is None or call.attempt > self.max_retries(call.op):
            return _Decision()
        if status is not None and status not in cfg.retryable_status_codes:
            return _Decision()
        idempotent = call.op.options.idempotency_key is not None
        if not should_retry(outcome, call.op.endpoint.retry_class, cfg.charged_retry_policy, idempotent=idempotent):
            return _Decision()
        delay = self._retry_delay(call, retry_after)
        if delay is None:
            return _Decision()
        if call.op.endpoint.retry_class == "charged" and outcome in (
            "server_error",
            "search_unavailable",
            "billing_unavailable",
            "ambiguous",
            "read",
            "write",
        ):
            call.retried_after_refundable = True
        return _Decision(retry=True, delay_ms=delay)

    def on_transport_error(self, call: _Call, exc: BaseException, started: float) -> _Decision:
        duration_ms = (time.monotonic() - started) * 1000.0
        if isinstance(exc, RightsFoundryError):
            error = exc
        else:
            error = APIConnectionError(
                f"transport failed: {redact_text(f'{type(exc).__name__}: {exc}', self.config.api_key)}",
                phase="read",
                charge_state="unknown",
            )
        outcome: Optional[Outcome] = None
        if isinstance(error, (TLSVerificationError, ResponseTooLargeError, UnexpectedRedirectError)):
            outcome = None
        elif isinstance(error, APIConnectionError):
            phase = error.phase or "read"
            if phase == "connect":
                outcome = "connect"
                error.charge_state = "not_charged"
            elif phase == "write":
                outcome = "write"
            else:
                outcome = "read"
        decision = self._decide(call, outcome, None, None)
        self._finalize(error, call)
        self._emit_error(call, error, decision, duration_ms)
        decision.error = error
        return decision

    def check_redirect(self, call: _Call, status: int, headers: Headers) -> None:
        if 300 <= status < 400:
            location = headers.get("location")
            redacted = redact_text(location, self.config.api_key) if location else None
            raise UnexpectedRedirectError(
                f"the API answered HTTP {status}; redirects are never followed"
                + (f" (Location: {redacted})" if redacted else ""),
                status=status,
                location=redacted,
                request_id=headers.get("x-request-id"),
                charge_state="unknown",
            )

    def body_reader(self, headers: Headers, method: str = "GET") -> BodyReader:
        if method == "HEAD":
            # A HEAD answer's Content-Length describes a body that is never sent.
            headers = Headers([(k, v) for k, v in headers.items() if k.lower() != "content-length"])
        return BodyReader(self.config.max_response_bytes, headers)

    def on_response(self, call: _Call, status: int, headers: Headers, body: bytes, started: float) -> _Decision:
        cfg = self.config
        duration_ms = (time.monotonic() - started) * 1000.0
        request_id = headers.get("x-request-id")
        retry_after = parse_retry_after(headers.get("retry-after"))
        lower = headers.lower_dict()
        self.guard.observe_headers(lower)
        endpoint = call.op.endpoint
        ok = 200 <= status < 300

        raw_json = call.op.decoder is None  # client.request: any JSON value, or None for no body
        parsed: Any = None
        parse_error: Optional[UnexpectedResponseError] = None
        if body:
            try:
                parsed = parse_json(body)
            except UnexpectedResponseError as exc:
                parse_error = exc
        elif (ok or status in endpoint.allow_statuses) and not (raw_json and ok):
            parse_error = UnexpectedResponseError("empty response body")

        if ok or status in endpoint.allow_statuses:
            decision = _Decision()
            if not ok:
                outcome = classify_status_outcome(status, None, None)
                decision = self._decide(call, outcome, retry_after, status)
            if decision.retry:
                self._emit_response(call, status, headers, duration_ms, decision, parsed)
                return decision
            if parse_error is not None or not (isinstance(parsed, dict) or (raw_json and ok)):
                shape_error = parse_error or UnexpectedResponseError("response body is not a JSON object")
                shape_error.status = status
                shape_error.request_id = request_id
                self._emit_response(call, status, headers, duration_ms, decision, parsed)
                self._finalize(shape_error, call)
                if endpoint.charged:
                    shape_error.charge_state = "unknown"
                return _Decision(error=shape_error)
            self._emit_response(call, status, headers, duration_ms, decision, parsed)
            decoder = call.op.decoder
            meta = {
                "request_id": request_id,
                "retried": call.retried_after_refundable,
                "http_status": status,
                "retry_after": retry_after,
            }
            try:
                value = decoder(parsed, meta) if decoder is not None else parsed
            except UnexpectedResponseError as exc:
                exc.status = status
                exc.request_id = request_id
                self._finalize(exc, call)
                if endpoint.charged:
                    exc.charge_state = "charged"
                    self.guard.record_spend(call.projected)
                return _Decision(error=exc)
            self._observe_success(call, value, lower)
            return _Decision(value=value)

        # --- error answers
        if parse_error is not None or not isinstance(parsed, dict):
            error: RightsFoundryError = UnexpectedResponseError(
                f"{call.op.operation} failed with HTTP {status} and a non-JSON body",
                status=status,
                request_id=request_id,
                retry_after=retry_after,
                charge_state="not_charged"
                if endpoint.retry_class == "free"
                else ("not_charged" if 400 <= status < 500 and status not in (408, 429) else "unknown"),
            )
            error_text: Optional[str] = None
            code: Optional[str] = None
        else:
            raw_error = parsed.get("error")
            raw_detail = parsed.get("message")
            error_text = raw_error if isinstance(raw_error, str) else None
            code = parsed.get("code") if isinstance(parsed.get("code"), str) else None
            error = classify_api_error(
                status,
                parsed,
                operation=call.op.operation,
                request_id=request_id,
                retry_after=retry_after,
                attempts=call.attempt,
                retry_class=endpoint.retry_class,
                redacted_message=redact_text(error_text, cfg.api_key) if error_text else None,
                redacted_detail=redact_text(raw_detail, cfg.api_key) if isinstance(raw_detail, str) else None,
                redacted_body=cast(Mapping[str, Any], redact_value(parsed, cfg.api_key)),
            )
        outcome = classify_status_outcome(status, error_text, code)
        decision = self._decide(call, outcome, retry_after, status)
        if isinstance(error, InsufficientCreditsError):
            self.guard.observe_insufficient(error.api_credits)
        if error.charge_state == "charged" and not decision.retry:
            # e.g. UnreachableFileError: the call was paid for although it failed.
            self.guard.record_spend(call.projected)
        self._emit_response(call, status, headers, duration_ms, decision, parsed)
        self._finalize(error, call)
        self._emit_error(call, error, decision, duration_ms)
        decision.error = error
        return decision

    def _observe_success(self, call: _Call, value: Any, lower_headers: Mapping[str, str]) -> None:
        if isinstance(value, Credits):
            self.guard.observe_credits(value)
            return
        if call.op.endpoint.retry_class == "free":
            return
        charged_header = lower_headers.get("x-trf-credits-charged")
        actual: Optional[int] = None
        if charged_header is not None and charged_header.strip().lstrip("-").isdigit():
            actual = int(charged_header.strip())
        elif isinstance(value, SyncLink):
            actual = value.credits_charged
        if "x-trf-credits-spendable" in lower_headers:
            return  # the server told us the post-charge balance already
        self.guard.record_spend(actual if actual is not None else call.projected)

    def warn_plain_http_file_url(self) -> None:
        with self._warn_lock:
            if self._http_warned:
                return
            self._http_warned = True
        self.log.warning(
            "rightsfoundry: file_url uses plain http://; the reference audio can be swapped in transit. Prefer https://"
        )


def _require_error(decision: _Decision) -> RightsFoundryError:
    if decision.error is None:  # pragma: no cover - a non-retry decision always carries an error
        return APIConnectionError("request failed", phase="read", charge_state="unknown")
    return decision.error


def _auth_header_value(config: ClientConfig) -> str:
    """The one place the raw key is unwrapped (SEC-05)."""
    if config.api_key is None:
        raise APIConnectionError("no API key configured", phase="connect", charge_state="not_charged")
    return "Bearer " + _reveal(config.api_key)


# --- guard + credits plumbing shared by both drivers ------------------------------------------


def credits_operation(options: Optional[CallOptions] = None) -> OperationSpec:
    return ENDPOINTS["credits.retrieve"].build(options=options)


# --- sync driver ----------------------------------------------------------------------------


class SyncPipeline:
    def __init__(self, config: ClientConfig, transport: Any, guard: CreditGuard) -> None:
        self.core = PipelineCore(config, guard)
        self.config = config
        self.transport = transport
        self.guard = guard
        self._sleep: Callable[[float], None] = time.sleep

    def refresh_credits(self, options: Optional[CallOptions] = None) -> Credits:
        return cast(Credits, self.execute(credits_operation(options)))

    def preflight(self, op: OperationSpec) -> None:
        """Run the credit guard for ``op`` without sending it (single-flight followers)."""
        self._guard(self.core.prepare(op))

    def ensure_rate_card(self, operation: str, cost_key: Optional[str]) -> Optional[int]:
        """Projected cost for a budgeted iterator page (fetches the rate card if stale)."""
        if self.guard.needs_fetch(charged=True, max_credits=None, budget=True):
            try:
                self.refresh_credits()
            except RightsFoundryError as exc:
                self.guard.unavailable(f"GET /v1/credits failed: {exc.message}", operation=operation, cost_key=cost_key)
        return self.guard.project(cost_key)

    def _guard(self, call: _Call) -> None:
        op = call.op
        if not op.endpoint.charged:
            call.projected = 0
            return
        if self.guard.needs_fetch(charged=True, max_credits=op.max_credits):
            try:
                self.refresh_credits()
            except RightsFoundryError as exc:
                self.guard.unavailable(
                    f"GET /v1/credits failed: {exc.message}",
                    operation=op.operation,
                    cost_key=call.cost_key,
                )
        call.projected = self.guard.enforce(operation=op.operation, cost_key=call.cost_key, max_credits=op.max_credits)

    def execute(self, op: OperationSpec) -> Any:
        core = self.core
        call = core.prepare(op)
        self._guard(call)
        while True:
            call.attempt += 1
            core.check_deadline(call)
            core.emit_request(call)
            started = time.monotonic()
            raw: Optional[RawResponse] = None
            failure: Optional[Exception] = None
            body = b""
            try:
                raw = self.transport.send(core.request_for(call), **core.timeouts(call))
                if raw is None:
                    raise APIConnectionError("transport returned no response", phase="read", charge_state="unknown")
                core.check_redirect(call, raw.status, raw.headers)
                reader = core.body_reader(raw.headers, call.method)
                for chunk in raw.iter_bytes():
                    reader.feed(chunk)
                body = reader.finish()
            except Exception as exc:  # noqa: BLE001 - custom transports may raise anything; classified below
                failure = exc
            finally:
                # Also on KeyboardInterrupt: an unclosed response would hold a pool slot forever.
                if raw is not None:
                    raw.close()
            if failure is not None or raw is None:
                decision = core.on_transport_error(call, failure or APIConnectionError("no response"), started)
                failure = None
                if decision.retry:
                    self._sleep(decision.delay_ms / 1000.0)
                    continue
                raise _require_error(decision) from None
            decision = core.on_response(call, raw.status, raw.headers, body, started)
            if decision.retry:
                self._sleep(decision.delay_ms / 1000.0)
                continue
            if decision.error is not None:
                raise decision.error from None
            return decision.value


# --- async driver ---------------------------------------------------------------------------


class AsyncPipeline:
    def __init__(self, config: ClientConfig, transport: Any, guard: CreditGuard) -> None:
        self.core = PipelineCore(config, guard)
        self.config = config
        self.transport = transport
        self.guard = guard
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._sleep: Callable[[float], Any] = asyncio.sleep

    def _get_semaphore(self) -> Optional[asyncio.Semaphore]:
        if self.config.max_concurrency is None:
            return None
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        return self._semaphore

    async def refresh_credits(self, options: Optional[CallOptions] = None) -> Credits:
        return cast(Credits, await self.execute(credits_operation(options)))

    async def preflight(self, op: OperationSpec) -> None:
        """Run the credit guard for ``op`` without sending it (single-flight followers)."""
        await self._guard(self.core.prepare(op))

    async def ensure_rate_card(self, operation: str, cost_key: Optional[str]) -> Optional[int]:
        if self.guard.needs_fetch(charged=True, max_credits=None, budget=True):
            try:
                await self.refresh_credits()
            except RightsFoundryError as exc:
                self.guard.unavailable(f"GET /v1/credits failed: {exc.message}", operation=operation, cost_key=cost_key)
        return self.guard.project(cost_key)

    async def _guard(self, call: _Call) -> None:
        op = call.op
        if not op.endpoint.charged:
            call.projected = 0
            return
        if self.guard.needs_fetch(charged=True, max_credits=op.max_credits):
            try:
                await self.refresh_credits()
            except RightsFoundryError as exc:
                self.guard.unavailable(
                    f"GET /v1/credits failed: {exc.message}",
                    operation=op.operation,
                    cost_key=call.cost_key,
                )
        call.projected = self.guard.enforce(operation=op.operation, cost_key=call.cost_key, max_credits=op.max_credits)

    async def _attempt(self, call: _Call) -> tuple[Optional[AsyncRawResponse], Any, float]:
        core = self.core
        started = time.monotonic()
        raw: Optional[AsyncRawResponse] = None
        try:
            raw = await self.transport.send(core.request_for(call), **core.timeouts(call))
            if raw is None:
                raise APIConnectionError("transport returned no response", phase="read", charge_state="unknown")
            core.check_redirect(call, raw.status, raw.headers)
            reader = core.body_reader(raw.headers, call.method)
            async for chunk in raw.aiter_bytes():
                reader.feed(chunk)
            return raw, reader.finish(), started
        except asyncio.CancelledError:
            if raw is not None:
                await raw.aclose()
            raise
        except Exception as exc:  # noqa: BLE001 - custom transports may raise anything; classified below
            if raw is not None:
                await raw.aclose()
            return None, exc, started

    async def execute(self, op: OperationSpec) -> Any:
        core = self.core
        call = core.prepare(op)
        await self._guard(call)
        while True:
            call.attempt += 1
            core.check_deadline(call)
            core.emit_request(call)
            semaphore = self._get_semaphore()
            if semaphore is not None:
                async with semaphore:
                    raw, payload, started = await self._attempt(call)
            else:
                raw, payload, started = await self._attempt(call)
            if raw is None:
                decision = core.on_transport_error(call, cast(BaseException, payload), started)
                if decision.retry:
                    await self._sleep(decision.delay_ms / 1000.0)
                    continue
                raise _require_error(decision) from None
            await raw.aclose()
            decision = core.on_response(call, raw.status, raw.headers, cast(bytes, payload), started)
            if decision.retry:
                await self._sleep(decision.delay_ms / 1000.0)
                continue
            if decision.error is not None:
                raise decision.error from None
            return decision.value
