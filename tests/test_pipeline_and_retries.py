"""Pipeline: retry matrix (docs/billing-and-retries.md), Retry-After, hooks, request ids, R-1/R-2/R-3, single-flight."""

from __future__ import annotations

import datetime as dt
import gzip
import logging
import threading
from email.utils import format_datetime
from typing import Any, Optional

import pytest

import rightsfoundry as rf
from helpers import (
    CANARY_KEY,
    MockTransport,
    Recorded,
    Reply,
    client,
    credits_json,
    error_json,
    page_json,
    sleeps,
    sync_json,
    track_json,
)
from rightsfoundry._retry import backoff_ms, parse_retry_after, should_retry

BILLING = "Billing is temporarily unavailable. Please retry shortly."
SEARCH = "Search is temporarily unavailable, please retry."


def connect_error() -> rf.APIConnectionError:
    return rf.APIConnectionError("refused", phase="connect", charge_state="not_charged")


def read_timeout() -> rf.APITimeoutError:
    return rf.APITimeoutError("read timed out", phase="read", charge_state="unknown")


# outcome → reply factory
OUTCOMES: dict[str, Any] = {
    "connect": lambda: Reply(raises=connect_error()),
    "429": lambda: Reply(429, error_json("slow down"), {"retry-after": "1"}),
    "503_billing": lambda: Reply(503, error_json(BILLING), {"retry-after": "5"}),
    "503_search": lambda: Reply(503, error_json(SEARCH)),
    "500": lambda: Reply(500, error_json("Internal server error")),
    "502": lambda: Reply(502, b"<html>bad gateway</html>", {"content-type": "text/html"}),
    "408": lambda: Reply(408, error_json("timeout")),
    "504": lambda: Reply(504, error_json("gateway timeout")),
    "read": lambda: Reply(raises=read_timeout()),
}

# expected retry decision: (free, sync, charged/refund_backed, charged/never, charged/always)
MATRIX: dict[str, tuple[bool, bool, bool, bool, bool]] = {
    "connect": (True, True, True, True, True),
    "429": (True, True, True, True, True),
    "503_billing": (True, True, True, False, True),
    "503_search": (True, True, True, False, True),
    "500": (True, True, True, False, True),
    "502": (True, True, False, False, True),
    "408": (True, True, False, False, True),
    "504": (True, True, False, False, True),
    "read": (True, True, False, False, True),
}


def _success_for(kind: str) -> Reply:
    if kind == "free":
        return Reply(200, {"success": True, "data": {"genre": []}})
    if kind == "sync":
        return Reply(200, sync_json())
    return Reply(200, page_json())


def _call(c: rf.Client, kind: str) -> Any:
    if kind == "free":
        return c.taxonomies.list()
    if kind == "sync":
        return c.tracks.sync(29446060)
    return c.tracks.list(genre="jazz")


@pytest.mark.parametrize("outcome", sorted(MATRIX))
@pytest.mark.parametrize(
    ("kind", "policy", "column"),
    [
        ("free", "refund_backed", 0),
        ("sync", "refund_backed", 1),
        ("charged", "refund_backed", 2),
        ("charged", "never", 3),
        ("charged", "always", 4),
    ],
)
def test_retry_matrix(outcome: str, kind: str, policy: str, column: int) -> None:
    expected = MATRIX[outcome][column]
    t = MockTransport([OUTCOMES[outcome](), _success_for(kind)])
    c = client(t, charged_retry_policy=policy)
    if expected:
        result = _call(c, kind)
        assert len(t.calls) == 2
        if kind == "charged" and outcome not in ("connect", "429"):
            assert result.retried_after_refundable_failure is True
    else:
        with pytest.raises(rf.RightsFoundryError) as info:
            _call(c, kind)
        assert len(t.calls) == 1
        assert info.value.attempts == 1
        assert info.value.operation is not None


def test_pure_matrix_function() -> None:
    assert should_retry("connect", "charged", "never")
    assert not should_retry("ambiguous", "charged", "refund_backed")
    assert should_retry("ambiguous", "sync", "never")
    assert should_retry("server_error", "charged", "refund_backed")


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 405, 410])
def test_never_retried_statuses(status: int) -> None:
    t = MockTransport([Reply(status, error_json("nope")), Reply(200, page_json())])
    c = client(t, charged_retry_policy="always", retryable_status_codes=[408, 429, 500, 502, 503, 504])
    with pytest.raises(rf.APIStatusError) as info:
        c.tracks.list()
    assert len(t.calls) == 1
    assert info.value.charge_state == "not_charged"


def test_max_retries_exhausted_reports_attempts() -> None:
    t = MockTransport([Reply(500, error_json("Internal server error"))] * 3)
    c = client(t, max_retries=2)
    with pytest.raises(rf.InternalServerError) as info:
        c.tracks.list()
    assert info.value.attempts == 3 and len(t.calls) == 3
    assert info.value.charge_state == "refund_pending" and info.value.refund_pending is True


def test_status_not_in_retryable_set_is_not_retried() -> None:
    t = MockTransport([Reply(500, error_json("Internal server error")), Reply(200, page_json())])
    c = client(t, retryable_status_codes=[429])
    with pytest.raises(rf.InternalServerError):
        c.tracks.list()


def test_tls_verification_never_retried() -> None:
    t = MockTransport([Reply(raises=rf.TLSVerificationError("bad cert", phase="connect")), Reply(200, page_json())])
    c = client(t)
    with pytest.raises(rf.TLSVerificationError) as info:
        c.tracks.list()
    assert len(t.calls) == 1 and info.value.charge_state == "not_charged"


def test_custom_transport_exception_is_wrapped_as_ambiguous() -> None:
    t = MockTransport([Reply(raises=ConnectionResetError("reset by peer")), Reply(200, page_json())])
    c = client(t)
    with pytest.raises(rf.APIConnectionError) as info:
        c.tracks.list()
    assert info.value.phase == "read" and info.value.charge_state == "unknown"


# --- Retry-After ---------------------------------------------------------------------------------


def test_parse_retry_after_forms() -> None:
    now = dt.datetime(2026, 11, 16, 10, 0, 0, tzinfo=dt.timezone.utc)
    assert parse_retry_after("5") == 5.0
    assert parse_retry_after(" 120 ") == 120.0
    future = format_datetime(now + dt.timedelta(seconds=30), usegmt=True)
    assert parse_retry_after(future, now=now) == pytest.approx(30.0)
    past = format_datetime(now - dt.timedelta(seconds=30), usegmt=True)
    assert parse_retry_after(past, now=now) == 0.0
    for garbage in ("-1", "1.5", "soon", "", "1e3", "Mon, 32 Foo 2026 00:00:00 GMT", None):
        assert parse_retry_after(garbage, now=now) is None


def test_retry_after_honoured_as_floor() -> None:
    t = MockTransport([Reply(503, error_json(BILLING), {"retry-after": "5"}), Reply(200, page_json())])
    c = client(t, jitter="none")
    delays = sleeps(c)
    c.tracks.list()
    assert delays == [5.0]


def test_retry_after_http_date() -> None:
    when = format_datetime(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3), usegmt=True)
    t = MockTransport([Reply(429, error_json("busy"), {"retry-after": when}), Reply(200, page_json())])
    c = client(t, jitter="none")
    delays = sleeps(c)
    c.tracks.list()
    assert 1.5 <= delays[0] <= 3.5


def test_retry_after_above_ceiling_raises_daily_limit_without_sleeping() -> None:
    body = error_json("Daily call limit reached for this connection. It resets at 00:00 UTC.", limit=500)
    t = MockTransport([Reply(429, body, {"retry-after": "3600"})])
    c = client(t)
    delays = sleeps(c)
    with pytest.raises(rf.DailyCallLimitError) as info:
        c.tracks.list()
    err = info.value
    assert delays == [] and err.retry_after == 3600.0 and err.limit == 500
    assert err.reset_at is not None and err.reset_at > dt.datetime.now(dt.timezone.utc)
    assert err.charge_state == "not_charged"


def test_backoff_bounds() -> None:
    for attempt in range(1, 8):
        base = min(8000, 500 * 2 ** (attempt - 1))
        assert 0 <= backoff_ms(attempt, initial_ms=500, max_ms=8000, jitter="full") <= base
        assert base / 2 <= backoff_ms(attempt, initial_ms=500, max_ms=8000, jitter="equal") <= base
        assert backoff_ms(attempt, initial_ms=500, max_ms=8000, jitter="none") == base


def test_total_timeout_stops_retrying() -> None:
    t = MockTransport([Reply(503, error_json(BILLING), {"retry-after": "5"}), Reply(200, page_json())])
    c = client(t, total_timeout=60.0, read_timeout=1.0, max_retry_after_ms=600_000)
    c._pipeline._sleep = lambda s: None
    t2 = MockTransport([Reply(503, error_json(BILLING), {"retry-after": "120"})])
    c2 = client(t2, total_timeout=60.0, read_timeout=1.0, max_retry_after_ms=600_000)
    with pytest.raises(rf.BillingUnavailableError):
        c2.tracks.list()
    assert len(t2.calls) == 1
    c.tracks.list()
    assert len(t.calls) == 2


def test_per_call_timeout_override() -> None:
    t = MockTransport([Reply(200, page_json())])
    c = client(t)
    c.tracks.list(timeout=5)
    assert t.calls[0].timeouts["read_timeout"] == 5.0
    assert t.calls[0].timeouts["connect_timeout"] == 5.0
    with pytest.raises(rf.ValidationError):
        c.tracks.list(timeout=0)


# --- headers & request ids ------------------------------------------------------------------------


def test_request_headers() -> None:
    t = MockTransport([Reply(200, page_json())])
    c = client(t, user_agent_suffix="acme-editor/4.2")
    c.tracks.list(search="dark epic")
    h = t.calls[0].headers
    assert h["authorization"] == f"Bearer {CANARY_KEY}"
    assert h["user-agent"].startswith(f"rightsfoundry-python/{rf.__version__} (") and h["user-agent"].endswith(
        " acme-editor/4.2"
    )
    assert h["accept"] == "application/json" and h["accept-encoding"] == "gzip"
    assert "idempotency-key" not in h and "x-admin-token" not in h and "cookie" not in h


def test_request_id_on_models_and_errors() -> None:
    t = MockTransport(
        [
            Reply(200, page_json(), {"x-request-id": "rid-1"}),
            Reply(404, error_json("Track not found"), {"x-request-id": "rid-2"}),
        ]
    )
    c = client(t)
    assert c.tracks.list().request_id == "rid-1"
    with pytest.raises(rf.TrackNotFoundError) as info:
        c.tracks.get(1)
    assert info.value.request_id == "rid-2" and "request_id=rid-2" in str(info.value)


def test_idempotency_key_r2() -> None:
    t = MockTransport([Reply(200, page_json()), Reply(200, page_json())])
    c = client(t)
    c.request("GET", "/v1/tracks", query={"genre": "jazz"}, idempotency_key="order-42")
    assert t.calls[0].headers["idempotency-key"] == "order-42"
    with pytest.raises(rf.ValidationError):
        c.request("GET", "/v1/tracks", idempotency_key="bad key\n")


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        ("token_invalid", rf.InvalidTokenError),
        ("token_inactive", rf.InactiveTokenError),
        ("token_expired", rf.ExpiredTokenError),
        ("feature_disabled", rf.FeatureNotEnabledError),
        ("insufficient_credits", rf.InsufficientCreditsError),
        ("track_not_found", rf.TrackNotFoundError),
        ("sync_master_unavailable", rf.SyncMasterUnavailableError),
        ("daily_limit", rf.DailyCallLimitError),
        ("billing_unavailable", rf.BillingUnavailableError),
        ("search_unavailable", rf.SearchUnavailableError),
        ("file_unreachable", rf.UnreachableFileError),
        ("validation_failed", rf.BadRequestError),
    ],
)
def test_r1_machine_codes_win_over_text(code: str, cls: type[rf.APIStatusError]) -> None:
    from rightsfoundry._errors import CODE_STATUSES

    status = min(CODE_STATUSES[code])
    t = MockTransport([Reply(status, error_json("some reworded message", code=code))])
    c = client(t, max_retries=0)
    with pytest.raises(cls) as info:
        c.tracks.list()
    assert info.value.code == code and info.value.status == status


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (500, "validation_failed", rf.InternalServerError),
        (500, "track_not_found", rf.InternalServerError),
        (400, "billing_unavailable", rf.BadRequestError),
        (503, "insufficient_credits", rf.ServiceUnavailableError),
        (404, "token_invalid", rf.NotFoundError),
    ],
)
def test_h3_codes_never_cross_status_families(status: int, code: str, expected: type[rf.APIStatusError]) -> None:
    t = MockTransport([Reply(status, error_json("reworded", code=code))])
    c = client(t, max_retries=0)
    with pytest.raises(expected) as info:
        c.tracks.list()
    assert type(info.value) is expected
    if status >= 500:
        assert info.value.charge_state != "not_charged"


@pytest.mark.parametrize("operation", ["search_similar", "deep_search"])
def test_h3_unrecognised_400_on_charged_search_is_unknown(operation: str) -> None:
    drifted = "We could not fetch the audio at file_url (reworded)."
    t = MockTransport([Reply(400, error_json(drifted))])
    c = client(t, max_retries=0)
    with pytest.raises(rf.BadRequestError) as info:
        if operation == "search_similar":
            c.tracks.search_similar("https://cdn.example.com/ref.mp3")
        else:
            c.tracks.deep_search("warm piano")
    assert type(info.value) is rf.BadRequestError
    assert info.value.charge_state == "unknown"


@pytest.mark.parametrize("message", ["file_url must point to an .mp3 or .wav file", "limit must be at most 100"])
def test_h3_known_validation_400_stays_not_charged(message: str) -> None:
    t = MockTransport([Reply(400, error_json(message))])
    c = client(t, max_retries=0)
    with pytest.raises(rf.BadRequestError) as info:
        c.tracks.search_similar("https://cdn.example.com/ref.mp3")
    assert info.value.charge_state == "not_charged"


def test_h3_unrecognised_400_on_list_stays_not_charged() -> None:
    t = MockTransport([Reply(400, error_json("some new validation wording"))])
    c = client(t, max_retries=0)
    with pytest.raises(rf.BadRequestError) as info:
        c.tracks.list()
    assert info.value.charge_state == "not_charged"


def test_r3_credit_headers_update_guard_and_fire_low_credit() -> None:
    events: list[rf.LowCreditEvent] = []
    t = MockTransport([Reply(200, page_json(), {"x-trf-credits-charged": "1", "x-trf-credits-spendable": "40"})])
    c = client(t, low_credit_warning_threshold=50, on_low_credit=events.append)
    c.tracks.list()
    assert len(events) == 1 and events[0].spendable == 40 and events[0].source == "response_header"


# --- hooks --------------------------------------------------------------------------------------------


def test_hooks_receive_redacted_copies_and_cannot_break_requests(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[Any] = []

    def boom(event: Any) -> None:
        seen.append(event)
        raise RuntimeError("hook bug")

    t = MockTransport([Reply(500, error_json("Internal server error")), Reply(200, sync_json())])
    c = client(t, on_request=boom, on_response=boom, on_error=boom, hook_include_bodies=True)
    with caplog.at_level(logging.WARNING, logger="rightsfoundry"):
        link = c.tracks.sync(29446060)
    assert link.credits_charged == 10
    kinds = [type(e).__name__ for e in seen]
    assert kinds == ["RequestEvent", "ResponseEvent", "ErrorEvent", "RequestEvent", "ResponseEvent"]
    req = seen[0]
    assert req.operation == "tracks.sync" and req.attempt == 1 and req.cost_key == "sync"
    assert req.headers["Authorization"].startswith("Bearer [REDACTED fp=")
    assert seen[3].attempt == 2 and seen[3].sdk_call_id == req.sdk_call_id
    assert seen[1].will_retry is True and seen[1].retry_in_ms is not None
    assert seen[2].charge_state == "refund_pending"
    final = seen[4]
    assert final.status == 200 and final.body["data"]["download_url"].endswith("sig=[REDACTED]")
    blob = repr(seen) + caplog.text
    assert CANARY_KEY not in blob and "5ec2e7" not in blob


def test_coroutine_hook_is_not_awaited(caplog: pytest.LogCaptureFixture) -> None:
    async def hook(_e: Any) -> None:
        raise AssertionError("must not run")

    t = MockTransport([Reply(200, page_json())])
    c = client(t, on_request=hook)
    with caplog.at_level(logging.WARNING, logger="rightsfoundry"):
        c.tracks.list()


def test_response_header_allowlist() -> None:
    events: list[rf.ResponseEvent] = []
    t = MockTransport([Reply(200, page_json(), {"set-cookie": "s=1", "x-secret": "nope", "cf-ray": "abc"})])
    c = client(t, on_response=events.append)
    c.tracks.list()
    assert set(events[0].headers) <= {"content-type", "x-request-id", "cf-ray"}
    assert "set-cookie" not in events[0].headers


def test_debug_logging_one_line_per_attempt(caplog: pytest.LogCaptureFixture) -> None:
    t = MockTransport([Reply(200, page_json())])
    c = client(t)
    with caplog.at_level(logging.DEBUG, logger="rightsfoundry"):
        c.tracks.list(key="A minor")
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("tracks.list GET" in line and "key=A+minor" in line for line in lines)


# --- bodies, decoding ---------------------------------------------------------------------------------


def test_gzip_body_is_decoded() -> None:
    t = MockTransport(
        [Reply(200, gzip.compress(b'{"success":true,"data":{"genre":[]}}'), {"content-encoding": "gzip"})]
    )
    assert client(t).taxonomies.list().genre == ()


def test_non_json_success_is_unexpected_response() -> None:
    t = MockTransport([Reply(200, b"<html>")])
    with pytest.raises(rf.UnexpectedResponseError) as info:
        client(t).tracks.list()
    assert info.value.status == 200 and info.value.charge_state == "unknown"


def test_schema_violation_is_unexpected_response() -> None:
    t = MockTransport([Reply(200, {"success": True, "data": "nope", "total": 1, "page": 1, "limit": 1})])
    with pytest.raises(rf.UnexpectedResponseError):
        client(t).tracks.list()


def test_track_get_returns_children_and_preserves_unknown_fields() -> None:
    child = track_json(2, title="Golden Hour (Instrumental)")
    t = MockTransport([Reply(200, {"success": True, "data": {**track_json(1), "child_tracks": [child]}})])
    track = client(t).tracks.get("1")
    assert track.child_tracks[0].title == "Golden Hour (Instrumental)"
    assert track.raw["future_field"]["added"] == "later"
    assert t.calls[0].path == "/v1/tracks/1"


# --- single-flight -------------------------------------------------------------------------------------


def test_single_flight_sync_coalesces_concurrent_calls() -> None:
    gate = threading.Event()

    def slow(_rec: Recorded) -> Reply:
        gate.wait(5)
        return Reply(200, sync_json())

    t = MockTransport([slow])
    c = client(t)
    results: list[rf.SyncLink] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(c.tracks.sync(29446060))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for th in threads:
        th.start()
    while not t.calls:
        pass
    gate.set()
    for th in threads:
        th.join(5)
    assert not errors and len(results) == 6 and len(t.calls) == 1
    assert all(r is results[0] for r in results)


def test_single_flight_is_per_mode_and_propagates_errors() -> None:
    t = MockTransport([Reply(200, sync_json()), Reply(200, sync_json(export=True, charged=25))])
    c = client(t)
    a = c.tracks.sync(29446060)
    b = c.tracks.sync(29446060, external_export=True)
    assert a.external_export is False and b.external_export is True and b.credits_charged == 25
    assert t.calls[0].json() == {"external_export": False}
    assert t.calls[1].json() == {"external_export": True}
    t.add(Reply(404, error_json("No sync master is available for this track")))
    with pytest.raises(rf.SyncMasterUnavailableError):
        c.tracks.sync(29446060)
    assert c.tracks._inflight == {}


def test_sync_reissue_is_free() -> None:
    t = MockTransport([Reply(200, sync_json(charged=0, reissued=True))])
    link = client(t).tracks.sync(29446060)
    assert link.reissued is True and link.credits_charged == 0


def test_request_escape_hatch() -> None:
    t = MockTransport([Reply(200, {"success": True, "new": "thing"}), Reply(410, error_json("moved"))])
    c = client(t)
    assert c.request("GET", "/v1/new-thing", query={"b": 2, "a": True}) == {"success": True, "new": "thing"}
    assert t.calls[0].query == [("a", "true"), ("b", "2")]
    with pytest.raises(rf.GoneError):
        c.request("POST", "/v1/tracks/creative-brief", json={"query": "x"})


@pytest.mark.parametrize(
    "path",
    [
        "/v2/tracks",
        "/v1/../admin",
        "/v1//tracks",
        "/v1/tracks%2f1",
        "/v1/%2e%2e",
        "/v1/tracks?x=1",
        "/v1/a\\b",
        "/v1/admin/health",
        "/v1/health/admin",
        "v1/tracks",
    ],
)
def test_request_path_validation(path: str) -> None:
    c = client(MockTransport())
    with pytest.raises(rf.ValidationError):
        c.request("GET", path)


def test_request_known_endpoint_projects_cost() -> None:
    t = MockTransport([Reply(200, credits_json()), Reply(200, page_json())])
    c = client(t, max_credits_per_request=1)
    with pytest.raises(rf.CreditCapExceededError) as info:
        c.request("GET", "/v1/tracks", query={"search": "jazz"})
    assert info.value.cost_key == "tracks_search_first_page"


def test_request_unknown_endpoint_with_cap_fails_closed() -> None:
    t = MockTransport([Reply(200, credits_json())])
    c = client(t, max_credits_per_request=5)
    with pytest.raises(rf.CreditGuardUnavailableError):
        c.request("POST", "/v1/brand-new", json={"a": 1})


def _nothing(_: Optional[int]) -> None:
    return None
