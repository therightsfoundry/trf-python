"""Regression tests for the second-pass audit (H2–H7) and the per-call options / escape-hatch work."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import pickle
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import (
    CANARY_KEY,
    CANARY_SIG,
    SIGNED_WAV,
    AsyncMockTransport,
    MockTransport,
    Reply,
    async_client,
    client,
    credits_json,
    error_json,
    page_json,
    sleeps,
    sync_json,
    track_json,
)
from rightsfoundry._config import build_config, normalize_host_pattern
from rightsfoundry._endpoints import ENDPOINTS, match_endpoint
from rightsfoundry._redact import redact_text
from rightsfoundry._retry import should_retry
from rightsfoundry._validation import (
    strip_text,
    utf16_length,
    validate_idempotency_key,
    validate_request_path,
    validate_request_query,
)

# --- H2: no validator accepts a trailing newline ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/v1/tracks\n", "/v1/credits\n"])
def test_h2_request_path_rejects_trailing_newline(path: str) -> None:
    with pytest.raises(rf.ValidationError):
        validate_request_path(path)


def test_h2_query_key_and_idempotency_key_reject_trailing_newline() -> None:
    with pytest.raises(rf.ValidationError):
        validate_request_query({"a\n": "1"})
    with pytest.raises(rf.ValidationError):
        validate_idempotency_key("abc\n")
    assert validate_idempotency_key("abc") == "abc"


@pytest.mark.parametrize(
    "headers",
    [{"X-Foo\n": "bar"}, {"X-Foo": "bar\n"}, {"traceparent\n": "00"}],
)
def test_h2_custom_headers_reject_trailing_newline(headers: dict[str, str]) -> None:
    with pytest.raises(rf.ConfigurationError):
        build_config(api_key=CANARY_KEY, custom_headers=headers)


def test_h2_user_agent_suffix_rejects_newline_in_code_but_env_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(rf.ConfigurationError):
        build_config(api_key=CANARY_KEY, user_agent_suffix="app/1.0\n")
    monkeypatch.setenv("TRF_USER_AGENT_SUFFIX", "app/1.0\n")  # e.g. mounted from a secret file
    assert build_config(api_key=CANARY_KEY).user_agent_suffix == "app/1.0"
    monkeypatch.setenv("TRF_ENVIRONMENT", " staging\n")
    assert build_config(api_key=CANARY_KEY).environment == "staging"


def test_h2_host_labels_gateway_path_and_endpoints_use_full_match() -> None:
    from rightsfoundry import _config, _validation

    for regex, good in (
        (_config._GATEWAY_PATH, "/api"),
        (_config._HOST_LABEL, "gw"),
        (_config._UA_SUFFIX, "app/1.0"),
        (_config._HEADER_NAME_X, "X-Foo"),
        (_config._HEADER_VALUE, "bar"),
        (_config._KEY_RE, CANARY_KEY),
        (_validation._HOST_LABEL, "gw"),
        (_validation._PRINTABLE_ASCII, "key"),
        (_validation._QUERY_KEY_RE, "a"),
        (_validation._REQUEST_PATH_RE, "/v1/tracks"),
    ):
        assert regex.fullmatch(good) and not regex.fullmatch(good + "\n"), regex.pattern
        assert not regex.match(good + "\n") or not regex.pattern.endswith("$"), regex.pattern
    # A base URL is stripped as a whole; an embedded newline is refused.
    with pytest.raises(rf.ConfigurationError):
        build_config(api_key=CANARY_KEY, base_url="https://gw.example.com/a\nb", allowed_hosts=["gw.example.com"])
    with pytest.raises(rf.ConfigurationError):
        build_config(api_key=CANARY_KEY, base_url="https://gw.example.com/a%0a", allowed_hosts=["gw.example.com"])
    assert match_endpoint("GET", "/v1/tracks/12\n") is None
    assert match_endpoint("GET", "/v1/tracks/12") is ENDPOINTS["tracks.get"]
    with pytest.raises(rf.ValidationError):
        rf.Client(api_key=CANARY_KEY, transport=MockTransport()).tracks.get("12\n")


def test_h2_retry_after_and_env_numbers() -> None:
    from rightsfoundry._retry import parse_retry_after

    assert parse_retry_after("5\n") == 5.0  # stripped, then matched in full
    assert parse_retry_after("5\nx") is None


# --- H4: models pickle, deepcopy and never print a live signature ---------------------------------------------


def _models() -> list[Any]:
    return [
        rf.Track.from_json(track_json(child_tracks=[track_json(2)])),
        rf.TrackPage.from_json(
            page_json(
                matched=[{"term": "piano", "field": "instruments", "value": "Piano"}],
                corrections=[{"from": "pianno", "to": "piano"}],
                coverage={"terms": 2, "levels": [{"matched": 2, "rows": 5}]},
                constraints={"vocals": "instrumental", "bpm": [90, 120], "duration": {"min": 30, "widened": True}},
            ),
            request_id="r1",
        ),
        rf.SyncLink.from_json(sync_json()["data"], request_id="r2"),
        rf.Credits.from_json(credits_json(subscription={"plan_id": 3, "plan": "Pro"}), request_id="r3"),
        rf.Taxonomies.from_json({"genre": [{"value": "Pop", "count": 5}], "brand_new": [{"value": "x", "count": 1}]}),
        rf.HealthStatus.from_json(
            {"success": True, "status": "healthy", "checks": {"database": {"status": "ok", "latency_ms": 3}}},
            http_status=200,
        ),
        rf.ReadinessStatus.from_json(
            {"success": False, "status": "not ready", "checks": {"billing": {"status": "missing", "required": True}}},
            http_status=503,
            retry_after=5,
        ),
        rf.LivenessStatus.from_json({"success": True, "status": "alive"}, http_status=200),
    ]


@pytest.mark.parametrize("model", _models(), ids=lambda m: type(m).__name__)
def test_h4_models_pickle_deepcopy_and_asdict(model: Any) -> None:
    restored = pickle.loads(pickle.dumps(model))
    assert restored == model and restored.raw == model.raw and type(restored.raw) is rf.FrozenMap
    assert copy.deepcopy(model) == model and copy.copy(model) == model
    assert isinstance(dataclasses.asdict(model), dict)
    assert json.loads(json.dumps(model.to_dict())) == model.to_dict()
    for text in (str(model), repr(model), str(model.raw), repr(model.raw)):
        assert CANARY_SIG not in text


def test_h4_raw_is_immutable_hashable_and_returns_real_values() -> None:
    link = rf.SyncLink.from_json(sync_json()["data"])
    assert link.raw["download_url"] == SIGNED_WAV  # the real URL, for the application
    assert "sig=[REDACTED]" in str(link.raw) and CANARY_SIG not in repr(link.raw)
    with pytest.raises(TypeError):
        link.raw["download_url"] = "x"  # type: ignore[index]
    with pytest.raises(AttributeError):
        link.raw._data = {}  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        del link.raw._data  # type: ignore[attr-defined]
    assert hash(link.raw) == hash(pickle.loads(pickle.dumps(link.raw)))
    assert link.raw == dict(link.raw) and link.raw.__eq__(5) is NotImplemented
    assert len(link.raw) == len(dict(link.raw))
    assert "download_url" in link.raw and "nope" not in link.raw
    assert rf.FrozenMap({"a": [1, {"b": 2}]}) != rf.FrozenMap({"a": 1})
    assert hash(rf.FrozenMap({"a": object()})) is not None  # unhashable leaves fall back to repr
    page = rf.TrackPage.from_json(page_json())
    pickle.loads(pickle.dumps(page.data[0].raw))
    creds = rf.Credits.from_json(credits_json())
    assert type(creds.costs) is rf.FrozenMap and creds.costs["sync"] == 10
    assert pickle.loads(pickle.dumps(creds)).discounts["sync"].plan == "Pro Pack"


def test_h4_declarative_fields_decode_and_keep_strict_ints() -> None:
    track = rf.Track.from_json(track_json(bpm_detected="fast", genres=["Pop", 3], main_genre="Rock"))
    assert track.bpm_detected is None and track.genres == ("Pop",) and track.main_genre == ("Rock",)
    with pytest.raises(rf.UnexpectedResponseError, match=r"track\.id"):
        rf.Track.from_json(track_json(id="x"))
    with pytest.raises(rf.UnexpectedResponseError, match=r"api_credits\.balance"):
        rf.Credits.from_json(credits_json(api_credits={"balance": "x", "spendable": 1}))
    data = sync_json()["data"]
    data.pop("credits_charged")
    data["format"] = ""
    link = rf.SyncLink.from_json(data)
    assert link.credits_charged == 0 and link.format == "wav"


# --- H6: sync single-flight followers keep their own limits -------------------------------------------------


class GatedTransport(MockTransport):
    """Holds every request until ``gate`` is set; signals ``entered`` when one arrives."""

    def __init__(self, replies: Any = (), *, raise_in_leader: BaseException | None = None) -> None:
        super().__init__(replies)
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.raise_in_leader = raise_in_leader

    def send(self, request: Any, **timeouts: float) -> Any:
        if request.url.endswith("/sync"):
            self.entered.set()
            assert self.gate.wait(10)
            if self.raise_in_leader is not None:
                with self.lock:
                    self.calls.append(request)
                raise self.raise_in_leader
        return super().send(request, **timeouts)


def _start_leader(c: rf.Client, results: list[Any]) -> threading.Thread:
    def run() -> None:
        try:
            results.append(c.tracks.sync(7))
        except BaseException as exc:
            results.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_h6_follower_enforces_its_own_max_credits() -> None:
    t = GatedTransport([Reply(200, credits_json()), Reply(200, sync_json(7))])
    c = client(t)
    results: list[Any] = []
    leader = _start_leader(c, results)
    assert t.entered.wait(5)
    with pytest.raises(rf.CreditCapExceededError):
        c.tracks.sync(7, max_credits=0)  # alone this raises; it must also raise while joined
    t.gate.set()
    leader.join(5)
    assert isinstance(results[0], rf.SyncLink)


def test_h6_follower_honours_its_own_timeout() -> None:
    t = GatedTransport([Reply(200, sync_json(7))])
    c = client(t)
    results: list[Any] = []
    leader = _start_leader(c, results)
    assert t.entered.wait(5)
    started = time.monotonic()
    with pytest.raises(rf.APITimeoutError) as info:
        c.tracks.sync(7, timeout=0.2)
    assert time.monotonic() - started < 3 and info.value.charge_state == "unknown"
    t.gate.set()
    leader.join(5)
    assert isinstance(results[0], rf.SyncLink) and c.tracks._inflight == {}


def test_h6_leader_keyboard_interrupt_is_not_raised_in_followers() -> None:
    t = GatedTransport(raise_in_leader=KeyboardInterrupt())
    c = client(t)
    results: list[Any] = []
    leader = _start_leader(c, results)
    assert t.entered.wait(5)
    follower: list[Any] = []

    def join() -> None:
        try:
            follower.append(c.tracks.sync(7))
        except BaseException as exc:
            follower.append(exc)

    thread = threading.Thread(target=join, daemon=True)
    thread.start()
    time.sleep(0.2)  # let the follower block on the shared future
    t.gate.set()
    leader.join(5)
    thread.join(5)
    assert isinstance(results[0], KeyboardInterrupt)
    assert isinstance(follower[0], rf.APIConnectionError) and not isinstance(follower[0], KeyboardInterrupt)
    assert "interrupted" in str(follower[0]) and c.tracks._inflight == {}


def test_h6_follower_gets_its_own_copy_of_the_leaders_error() -> None:
    t = GatedTransport([Reply(404, error_json("No sync master is available for this track"))])
    c = client(t)
    results: list[Any] = []
    leader = _start_leader(c, results)
    assert t.entered.wait(5)
    follower: list[Any] = []
    thread = threading.Thread(target=lambda: follower.append(_capture(lambda: c.tracks.sync(7))), daemon=True)
    thread.start()
    time.sleep(0.2)
    t.gate.set()
    leader.join(5)
    thread.join(5)
    assert isinstance(results[0], rf.SyncMasterUnavailableError)
    assert isinstance(follower[0], rf.SyncMasterUnavailableError) and follower[0] is not results[0]


def _capture(fn: Any) -> Any:
    try:
        return fn()
    except BaseException as exc:
        return exc


def test_h6_async_follower_guard_timeout_and_stale_loop() -> None:
    release: dict[str, asyncio.Event] = {}

    class Hanging(AsyncMockTransport):
        async def send(self, request: Any, **timeouts: float) -> Any:
            if request.url.endswith("/sync") and "hang" in release:
                await release["hang"].wait()
            return await super().send(request, **timeouts)

    t = Hanging()
    c = async_client(t)

    # 1. a task stranded on a closed loop never blocks later calls
    loop = asyncio.new_event_loop()

    async def start() -> Any:
        release["hang"] = asyncio.Event()
        return asyncio.ensure_future(c.tracks.sync(7))

    loop.run_until_complete(start())
    loop.run_until_complete(asyncio.sleep(0.05))
    assert c.tracks._inflight
    loop.close()  # the task is still pending on a loop that no longer runs
    del release["hang"]

    async def later() -> Any:
        return await c.tracks.sync(7)

    t.add(Reply(200, sync_json(7)))
    assert isinstance(asyncio.run(later()), rf.SyncLink)

    # 2. followers: own cap, own timeout
    async def followers() -> None:
        release["hang"] = asyncio.Event()
        leader = asyncio.ensure_future(c.tracks.sync(8))
        await asyncio.sleep(0.05)
        with pytest.raises(rf.CreditCapExceededError):
            await c.tracks.sync(8, max_credits=0)
        with pytest.raises(rf.APITimeoutError):
            await c.tracks.sync(8, timeout=0.05)
        release["hang"].set()
        assert isinstance(await leader, rf.SyncLink)

    t.add(Reply(200, credits_json()), Reply(200, sync_json(8)))
    asyncio.run(followers())


def test_h6_async_follower_sees_cancelled_leader_as_connection_error() -> None:
    gate: dict[str, asyncio.Event] = {}

    class Hanging(AsyncMockTransport):
        async def send(self, request: Any, **timeouts: float) -> Any:
            await gate["e"].wait()
            return await super().send(request, **timeouts)

    c = async_client(Hanging([Reply(200, sync_json(7))]))

    async def main() -> None:
        gate["e"] = asyncio.Event()
        leader = asyncio.ensure_future(c.tracks.sync(7))
        await asyncio.sleep(0.02)
        follower = asyncio.ensure_future(c.tracks.sync(7))
        await asyncio.sleep(0.02)
        inner = next(iter(c.tracks._inflight.values()))
        inner.cancel()
        with pytest.raises(rf.APIConnectionError, match="interrupted"):
            await follower
        with pytest.raises(asyncio.CancelledError):
            await leader

    asyncio.run(main())


# --- H7: resume never splices two tracks; refused ranges fall back once --------------------------------------


def test_h7_partial_name_is_keyed_by_asset_path(tmp_path: Path) -> None:
    from rightsfoundry._download import _Target

    a = _Target(tmp_path, "master.wav", overwrite=False, resume=True, source_key="/stream/AB12/1/master.wav")
    b = _Target(tmp_path, "master.wav", overwrite=False, resume=True, source_key="/stream/AB12/2/master.wav")
    assert a.partial != b.partial and a.partial.name.startswith(".master.wav.")


# --- H3 / guard: a charged error is counted as spend ---------------------------------------------------------


def test_charged_error_records_spend_in_the_guard() -> None:
    unreachable = (
        "Could not download or decode the audio at file_url. It must be a publicly reachable .mp3 or .wav under 120MB."
    )
    t = MockTransport([Reply(200, credits_json(1000)), Reply(400, error_json(unreachable))])
    c = client(t, auto_check_credits=True)
    with pytest.raises(rf.UnreachableFileError):
        c.tracks.search_similar("https://cdn.example.com/ref.mp3")
    assert c._guard._spent_since_snapshot == 8  # file_search_first_page from the rate card


def test_undecodable_success_on_charged_call_records_spend() -> None:
    t = MockTransport([Reply(200, credits_json(1000)), Reply(200, {"success": True})])
    c = client(t, auto_check_credits=True)
    with pytest.raises(rf.UnexpectedResponseError) as info:
        c.tracks.deep_search("warm piano")
    assert info.value.charge_state == "charged" and c._guard._spent_since_snapshot == 5


# --- CallOptions: per-call retries, idempotency, X-TRF-Max-Credits -----------------------------------------


def test_call_options_validation_and_merge() -> None:
    from rightsfoundry._options import resolve_options

    for bad in (
        {"timeout": 0},
        {"timeout": True},
        {"max_credits": -1},
        {"max_retries": 11},
        {"max_retries": True},
        {"idempotency_key": "a b"},
        {"send_max_credits_header": "yes"},
    ):
        with pytest.raises(rf.ValidationError):
            rf.CallOptions(**bad)
    base = rf.CallOptions(timeout=5, max_retries=1)
    merged = resolve_options(base, max_retries=3)
    assert merged.timeout == 5.0 and merged.max_retries == 3 and base.max_retries == 1
    assert resolve_options(base) is base
    with pytest.raises(rf.ValidationError):
        resolve_options({"timeout": 1})  # type: ignore[arg-type]


def test_per_call_max_retries_overrides_client() -> None:
    t = MockTransport([Reply(503, error_json("busy")), Reply(503, error_json("busy")), Reply(200, credits_json())])
    c = client(t, max_retries=0)
    assert c.credits.retrieve(max_retries=2).api_credits.spendable == 1000
    assert len(t.calls) == 3
    t2 = MockTransport([Reply(503, error_json("busy"))])
    c2 = client(t2, max_retries=5)
    with pytest.raises(rf.ServiceUnavailableError):
        c2.credits.retrieve(options=rf.CallOptions(max_retries=0))
    assert len(t2.calls) == 1


@pytest.mark.parametrize("status", [502, 504, 408])
def test_idempotency_key_makes_ambiguous_charged_outcomes_retryable(status: int) -> None:
    t = MockTransport([Reply(status, b"gateway"), Reply(200, page_json())])
    c = client(t)
    page = c.tracks.deep_search("warm piano", idempotency_key="brief-42")
    assert page.retried_after_refundable_failure is True
    assert [r.headers["idempotency-key"] for r in t.calls] == ["brief-42", "brief-42"]
    # without a key the same outcome is not retried under refund_backed
    t2 = MockTransport([Reply(status, b"gateway")])
    with pytest.raises(rf.RightsFoundryError):
        client(t2).tracks.deep_search("warm piano")
    assert len(t2.calls) == 1


def test_idempotency_retry_matrix() -> None:
    for outcome in ("ambiguous", "read", "write"):
        assert should_retry(outcome, "charged", "refund_backed", idempotent=True)
        assert not should_retry(outcome, "charged", "refund_backed")
        assert not should_retry(outcome, "charged", "never", idempotent=True)


def test_read_failure_with_idempotency_key_is_retried() -> None:
    t = MockTransport(
        [Reply(raises=rf.APIConnectionError("reset", phase="read", charge_state="unknown")), Reply(200, page_json())]
    )
    c = client(t)
    assert len(c.tracks.list(search="piano", idempotency_key="k1")) == 2
    assert len(t.calls) == 2


def test_x_trf_max_credits_header_is_opt_in() -> None:
    t = MockTransport([Reply(200, credits_json()), Reply(200, page_json()), Reply(200, page_json())])
    c = client(t)
    c.tracks.list(max_credits=3, send_max_credits_header=True)
    c.tracks.list(max_credits=3)
    sent = [r for r in t.calls if r.path == "/v1/tracks"]
    assert sent[0].headers["x-trf-max-credits"] == "3" and "x-trf-max-credits" not in sent[1].headers
    t2 = MockTransport([Reply(200, credits_json()), Reply(200, page_json())])
    c2 = client(t2, max_credits_per_request=40)
    c2.tracks.list(options=rf.CallOptions(send_max_credits_header=True))
    assert t2.calls[-1].headers["x-trf-max-credits"] == "40"
    t3 = MockTransport([Reply(200, credits_json())])
    client(t3).credits.retrieve(send_max_credits_header=True)  # free endpoint: never sent
    assert "x-trf-max-credits" not in t3.calls[0].headers


def test_every_typed_method_accepts_call_options() -> None:
    t = MockTransport()
    t.default = lambda rec: Reply(
        200,
        sync_json()
        if rec.path.endswith("/sync")
        else credits_json()
        if rec.path == "/v1/credits"
        else {"success": True, "data": {"genre": []}}
        if rec.path == "/v1/taxonomies"
        else {"success": True, "status": "healthy"}
        if rec.path.startswith("/v1/health")
        else {"success": True, "data": track_json()}
        if rec.path.startswith("/v1/tracks/") and rec.path.rsplit("/", 1)[-1].isdigit()
        else page_json(),
    )
    c = client(t)
    opts = rf.CallOptions(timeout=7, max_retries=1, idempotency_key="abc")
    c.tracks.list(options=opts)
    c.tracks.get(1, options=opts)
    c.tracks.deep_search("x", options=opts)
    c.tracks.search_similar("https://cdn.example.com/a.mp3", options=opts)
    c.tracks.sync(1, options=opts)
    c.taxonomies.list(options=opts)
    c.credits.retrieve(options=opts)
    c.health.check(options=opts)
    c.health.live(options=opts)
    c.health.ready(options=opts)
    c.request("GET", "/v1/credits", options=opts)
    for rec in t.calls:
        assert rec.timeouts["read_timeout"] == 7.0 and rec.headers.get("idempotency-key") == "abc"


def test_health_defaults_to_no_retries_but_can_opt_in() -> None:
    degraded = {"success": False, "status": "degraded"}
    t = MockTransport([Reply(503, degraded, {"retry-after": "5"})])
    c = client(t)
    slept = sleeps(c)
    assert c.health.check().status == "degraded" and len(t.calls) == 1 and slept == []
    t.add(Reply(503, degraded), Reply(200, {"success": True, "status": "healthy"}))
    assert c.health.check(max_retries=2).healthy and len(t.calls) == 3


# --- escape hatch ------------------------------------------------------------------------------------------


def test_request_supports_head_204_arrays_and_scalars() -> None:
    t = MockTransport(
        [
            Reply(200, b"", {"content-length": "12345"}),
            Reply(204),
            Reply(200, [1, 2, 3]),
            Reply(200, b"42"),
            Reply(200, b"not json"),
        ]
    )
    c = client(t)
    assert c.request("HEAD", "/v1/future") is None
    assert c.request("DELETE", "/v1/future/1") is None
    assert c.request("GET", "/v1/future/list") == [1, 2, 3]
    assert c.request("GET", "/v1/future/count") == 42
    with pytest.raises(rf.UnexpectedResponseError):
        c.request("GET", "/v1/future/bad")
    with pytest.raises(rf.ValidationError):
        c.request("GET", "/v1/credits", json={"a": 1})


def test_typed_methods_still_reject_empty_and_non_object_bodies() -> None:
    t = MockTransport([Reply(204), Reply(200, [1])])
    c = client(t, max_retries=0)
    with pytest.raises(rf.UnexpectedResponseError):
        c.credits.retrieve()
    with pytest.raises(rf.UnexpectedResponseError):
        c.credits.retrieve()


# --- single-use iterators ----------------------------------------------------------------------------------


def test_sync_iterators_are_single_use() -> None:
    t = MockTransport([Reply(200, page_json(total=2))])
    c = client(t)
    it = c.tracks.iter(limit=2)
    assert [tr.id for tr in it] == [1, 2]
    with pytest.raises(rf.IteratorConsumedError) as info:
        list(it)
    assert isinstance(info.value, RuntimeError) and info.value.charge_state == "not_charged"
    with pytest.raises(rf.IteratorConsumedError):
        it.pages()
    pages = c.tracks.iter(limit=2)
    t.add(Reply(200, page_json(total=2)))
    assert len(list(pages.pages())) == 1
    with pytest.raises(rf.IteratorConsumedError):
        iter(pages)
    assert len(t.calls) == 2


def test_async_iterators_are_single_use() -> None:
    t = AsyncMockTransport([Reply(200, page_json(total=2)), Reply(200, page_json(total=2))])
    c = async_client(t)

    async def main() -> None:
        it = c.tracks.iter(limit=2)
        assert [tr.id async for tr in it] == [1, 2]
        with pytest.raises(rf.IteratorConsumedError):
            it.__aiter__()
        pages = c.tracks.deep_search_iter("x", limit=2)
        assert [p.page async for p in pages.pages()] == [1]
        with pytest.raises(rf.IteratorConsumedError):
            pages.pages()

    asyncio.run(main())


# --- AsyncClient never closes an injected sync transport ------------------------------------------------------


def test_async_client_does_not_close_an_injected_sync_transport() -> None:
    closed: list[bool] = []

    class Owned(MockTransport):
        def close(self) -> None:
            closed.append(True)

    t = Owned([Reply(200, credits_json())])
    c = rf.AsyncClient(api_key=CANARY_KEY, transport=t)
    assert asyncio.run(c.credits.retrieve()).api_credits.spendable == 1000
    asyncio.run(c.aclose())
    assert closed == []
    own = rf.AsyncClient(api_key=CANARY_KEY)
    inner = own._sync_transport
    own.close()
    assert inner._closed is True


# --- config polish -----------------------------------------------------------------------------------------


def test_connect_timeout_zero_from_env_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRF_CONNECT_TIMEOUT", "0")
    with pytest.raises(rf.ConfigurationError, match="connect_timeout"):
        build_config(api_key=CANARY_KEY)
    monkeypatch.setenv("TRF_CONNECT_TIMEOUT", "2.5")
    monkeypatch.setenv("TRF_READ_TIMEOUT", "0")
    with pytest.raises(rf.ConfigurationError, match="read_timeout"):
        build_config(api_key=CANARY_KEY)
    monkeypatch.delenv("TRF_READ_TIMEOUT")
    assert build_config(api_key=CANARY_KEY).connect_timeout == 2.5


@pytest.mark.parametrize("pattern", ["*.com", "*.localhost", "*."])
def test_wildcards_need_two_labels(pattern: str) -> None:
    with pytest.raises(rf.ConfigurationError):
        normalize_host_pattern("allowed_hosts", pattern)
    assert normalize_host_pattern("allowed_hosts", "*.example.com") == "*.example.com"


# --- redaction polish --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("the bearer capability is gone", "the bearer capability is gone"),
        ("Bearer abc.def-ghi", "Bearer [REDACTED]"),
        ("Bearer AbCdEfGhIj", "Bearer [REDACTED]"),
        ("bearer abcdefghijklmnopqrstuvwxyz", "bearer [REDACTED]"),
        ("Authorization: Bearer x", "Authorization: Bearer [REDACTED]"),
        ("proxy-authorization=Basic dXNlcjpw", "proxy-authorization=Basic [REDACTED]"),
        ('{"Authorization": "short"}', '{"Authorization": "[REDACTED]"}'),
    ],
)
def test_bearer_redaction_needs_a_token_or_header_context(text: str, expected: str) -> None:
    assert redact_text(text) == expected


def test_any_real_key_after_bearer_is_always_redacted() -> None:
    for key in (CANARY_KEY, CANARY_KEY.upper(), "tok_example_" + "a1" * 12, "eyJhbGciOiJIUzI1NiJ9.e30.sig"):
        out = redact_text(f"server said: Bearer {key} was refused")
        assert key not in out


# --- validation polish -------------------------------------------------------------------------------------


def test_strings_are_measured_in_utf16_units_and_boms_stripped() -> None:
    emoji = "\U0001f3b9"  # one code point, two UTF-16 units
    assert utf16_length(emoji) == 2 and utf16_length("abc") == 3
    assert strip_text("﻿  piano ﻿\n") == "piano"
    c = client(MockTransport())
    with pytest.raises(rf.ValidationError, match="UTF-16"):
        c.tracks.deep_search(emoji * 251)  # 251 code points but 502 units > 500
    t = MockTransport([Reply(200, credits_json()), Reply(200, page_json())])
    c2 = client(t, max_credits_per_request=100)
    c2.tracks.list(search="﻿")
    listed = t.calls[-1]
    assert "search" not in dict(listed.query)  # priced and sent as a plain listing


# --- CLI and Action polish ---------------------------------------------------------------------------------


def test_cli_global_flags_before_or_after_the_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    from rightsfoundry import _cli

    parser = _cli._build_parser()
    for argv in (["--json", "health"], ["health", "--json"]):
        args = _cli._parse(parser, argv)
        assert args.json is True and args.environment is None
    args = _cli._parse(parser, ["--environment", "staging", "search", "piano"])
    assert args.environment == "staging"
    args = _cli._parse(parser, ["search", "piano", "--environment", "staging", "--featured", "--sort-by", "bpm"])
    assert args.environment == "staging" and args.featured is True and args.sort_by == "bpm"
    args = _cli._parse(parser, ["credits"])
    assert args.json is False


def test_cli_json_output_is_ascii_escaped() -> None:
    import io

    from rightsfoundry import _cli

    out = io.StringIO()
    _cli._dump(out, {"title": "evil‮txt.exe \x1b[31m"})
    text = out.getvalue()
    assert text.isascii() and "\\u202e" in text and "\x1b" not in text


def test_action_md_truncates_before_escaping_and_masks_every_sig() -> None:
    from rightsfoundry import _action

    text = _action._md("|" * 300)
    assert text == "\\|" * 200 and not text.endswith("\\\\")
    assert _action._signatures("https://a/x?sig=abc&expires=1") == ["abc"]
    assert _action._signatures("https://a/x?expires=1&SIG=def") == ["def"]
    assert _action._signatures("https://a/x?expires=1") == []


def test_action_search_filters_come_from_the_registry() -> None:
    from rightsfoundry import _action

    env = {
        "INPUT_BPM_MIN": "90",
        "INPUT_FEATURED": "true",
        "INPUT_ID": "12",
        "INPUT_SORT_BY": "bpm",
        "INPUT_GENRE": "Pop",
    }
    assert _action._list_filters(env) == {"bpm_min": 90.0, "featured": True, "id": 12, "sort_by": "bpm", "genre": "Pop"}


# --- extra coverage: download argument checks, async paths, caches ------------------------------------------


def test_download_argument_validation(tmp_path: Path) -> None:
    c = client(MockTransport())
    link = rf.SyncLink.from_json(sync_json()["data"])
    track = rf.Track.from_json(track_json(asset_url=None))
    cases: list[tuple[Any, Any, dict[str, Any]]] = [
        (link, tmp_path, {"kind": "stems"}),
        (link, tmp_path, {"kind": "preview"}),
        (track, tmp_path, {"kind": "preview"}),
        (12345, tmp_path, {}),
        (link, 5, {}),
        (link, "", {}),
        (link, tmp_path / "missing" / "x.wav", {}),
        (link, tmp_path, {"overwrite": "yes"}),
    ]
    for source, dest, kw in cases:
        with pytest.raises(rf.ValidationError):
            c.sync_links.download(source, dest, **kw)
    (tmp_path / "29446060_track.wav").mkdir()
    with pytest.raises(rf.ValidationError, match="directory"):
        c.sync_links.download(link, tmp_path, overwrite=True)  # the suggested name is a directory
    for bad in ("http://assets.therightsfoundry.com/stream/a.wav?expires=1&sig=ab", "https://[::1/x"):
        with pytest.raises(rf.SyncLinkInvalidError):
            c.sync_links.download(bad, tmp_path)


def test_async_downloads_retry_and_fail_cleanly(tmp_path: Path) -> None:
    from servers import RequestHandler, serve

    calls = {"n": 0}

    def flaky(h: RequestHandler) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            h.reply(503, b"busy", {"Retry-After": "0"})
        else:
            h.reply(200, b"RIFF" + b"\0" * 100)

    def gone(h: RequestHandler) -> None:
        h.reply(404, b"not here")

    def link(port: int) -> str:
        return f"http://127.0.0.1:{port}/stream/AB12/1/a.wav?expires=4102444800&sig={CANARY_SIG}"

    for handler, expected in ((flaky, None), (gone, rf.SyncMasterUnavailableError)):
        with serve(handler) as server:
            c = rf.AsyncClient(
                api_key=CANARY_KEY,
                environment="staging",
                base_url=f"http://127.0.0.1:{server.port}",
                allow_insecure_localhost=True,
                retry_initial_delay_ms=50,
            )

            async def run(c: rf.AsyncClient = c, port: int = server.port) -> Path:
                return await c.sync_links.download(link(port), tmp_path, overwrite=True)

            if expected is None:
                assert asyncio.run(run()).read_bytes().startswith(b"RIFF")
            else:
                with pytest.raises(expected):
                    asyncio.run(run())
            asyncio.run(c.aclose())
    assert [p.name for p in tmp_path.iterdir()] == ["a.wav"]


def test_async_iterators_for_both_search_kinds_and_taxonomy_cache() -> None:
    t = AsyncMockTransport(
        [
            Reply(200, page_json(total=2)),
            Reply(200, credits_json()),
            Reply(200, page_json(total=2)),
            Reply(200, {"success": True, "data": {"genre": [{"value": "Pop", "count": 3}]}}),
        ]
    )
    c = async_client(t, taxonomies_cache_ttl_ms=60_000)

    async def main() -> None:
        assert [tr.id async for tr in c.tracks.search_similar_iter("https://cdn.example.com/a.mp3", limit=2)] == [1, 2]
        budgeted = c.tracks.deep_search_iter("x", limit=2, max_credits=50)
        assert [tr.id async for tr in budgeted] == [1, 2] and budgeted.spent_projected == 5
        first = await c.taxonomies.list()
        assert await c.taxonomies.list() is first  # served from the cache
        assert (await c.health.live(options=rf.CallOptions(timeout=1))) is not None

    t.add(Reply(200, {"success": True, "status": "alive"}))
    asyncio.run(main())
    assert len(t.calls) == 5


def test_sync_iterators_for_both_search_kinds_and_taxonomy_cache() -> None:
    t = MockTransport(
        [
            Reply(200, page_json(total=2)),
            Reply(200, page_json(total=2)),
            Reply(200, {"success": True, "data": {"mood": [{"value": "Calm", "count": 3}]}}),
        ]
    )
    c = client(t, taxonomies_cache_ttl_ms=60_000)
    assert [tr.id for tr in c.tracks.search_similar_iter("http://cdn.example.com/a.mp3", limit=2)] == [1, 2]
    assert [tr.id for tr in c.tracks.deep_search_iter("x", limit=2, max_items=1)] == [1]
    first = c.taxonomies.list()
    assert c.taxonomies.list() is first and first.values("mood") == ["Calm"]


def test_async_health_failures_become_health_check_errors() -> None:
    t = AsyncMockTransport([Reply(raises=rf.APIConnectionError("refused", phase="connect"))])
    c = async_client(t)
    with pytest.raises(rf.HealthCheckError):
        asyncio.run(c.health.ready())
