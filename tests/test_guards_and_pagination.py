"""Credit guardrails and pagination."""

from __future__ import annotations

import logging
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import MockTransport, Recorded, Reply, client, costs, credits_json, error_json, page_json

# --- projection ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "key"),
    [
        (lambda c: c.tracks.list(genre="jazz"), "tracks_list"),
        (lambda c: c.tracks.list(search="   "), "tracks_list"),
        (lambda c: c.tracks.list(search="jazz"), "tracks_search_first_page"),
        (lambda c: c.tracks.list(search="jazz", page=1), "tracks_search_first_page"),
        (lambda c: c.tracks.list(search="jazz", page=2), "tracks_search_next_page"),
        (lambda c: c.tracks.get(5), "track_detail"),
        (lambda c: c.tracks.deep_search("dark"), "deep_search_first_page"),
        (lambda c: c.tracks.deep_search("dark", page=3), "deep_search_next_page"),
        (lambda c: c.tracks.search_similar("https://cdn.example.com/a.mp3"), "file_search_first_page"),
        (lambda c: c.tracks.search_similar("https://cdn.example.com/a.mp3", page=2), "file_search_next_page"),
        (lambda c: c.tracks.sync(5), "sync"),
        (lambda c: c.tracks.sync(5, external_export=True), "export_sync"),
    ],
)
def test_projection_matches_server_element(call: Any, key: str) -> None:
    card = {k: 100 + i for i, k in enumerate(costs())}
    t = MockTransport([Reply(200, credits_json(card=card))])
    c = client(t, max_credits_per_request=0)
    with pytest.raises(rf.CreditCapExceededError) as info:
        call(c)
    err = info.value
    assert err.cost_key == key and err.projected == card[key] and err.cap == 0
    assert err.charge_state == "not_charged"
    assert len(t.calls) == 1 and t.calls[0].path == "/v1/credits"  # nothing charged was sent


def test_free_operations_are_never_guarded() -> None:
    t = MockTransport([Reply(200, {"success": True, "data": {}}), Reply(200, credits_json())])
    c = client(t, max_credits_per_request=0, auto_check_credits=True)
    c.taxonomies.list()
    c.credits.retrieve()
    assert [r.path for r in t.calls] == ["/v1/taxonomies", "/v1/credits"]


def test_cap_allows_cheap_calls_and_caches_rate_card() -> None:
    t = MockTransport([Reply(200, credits_json())])
    t.default = Reply(200, page_json())
    c = client(t, max_credits_per_request=5)
    for _ in range(3):
        c.tracks.list(genre="jazz")
    assert [r.path for r in t.calls].count("/v1/credits") == 1


def test_rate_card_ttl_expiry_refetches(monkeypatch: pytest.MonkeyPatch) -> None:
    t = MockTransport(
        [Reply(200, credits_json()), Reply(200, page_json()), Reply(200, credits_json()), Reply(200, page_json())]
    )
    c = client(t, max_credits_per_request=5, rate_card_ttl_ms=1000)
    now = [1000.0]
    c._guard._clock = lambda: now[0]
    c.tracks.list()
    now[0] += 2
    c.tracks.list()
    assert [r.path for r in t.calls] == ["/v1/credits", "/v1/tracks", "/v1/credits", "/v1/tracks"]


def test_per_call_max_credits_overrides_global_cap() -> None:
    t = MockTransport([Reply(200, credits_json()), Reply(200, {"success": True, "data": {}})])
    t.replies[1] = Reply(200, __import__("helpers").sync_json())
    c = client(t, max_credits_per_request=2)
    with pytest.raises(rf.CreditCapExceededError):
        c.tracks.sync(5)
    link = c.tracks.sync(5, max_credits=10)
    assert link.credits_charged == 10


def test_missing_cost_key_fail_closed_and_open(caplog: pytest.LogCaptureFixture) -> None:
    card = costs()
    del card["export_sync"]
    t = MockTransport([Reply(200, credits_json(card=card))])
    c = client(t, max_credits_per_request=100)
    with pytest.raises(rf.CreditGuardUnavailableError, match="export_sync"):
        c.tracks.sync(5, external_export=True)
    t2 = MockTransport([Reply(200, credits_json(card=card)), Reply(200, __import__("helpers").sync_json(export=True))])
    c2 = client(t2, max_credits_per_request=100, guard_failure_mode="open")
    with caplog.at_level(logging.WARNING, logger="rightsfoundry"):
        c2.tracks.sync(5, external_export=True)
    assert "guard_failure_mode='open'" in caplog.text


def test_rate_card_fetch_failure_fail_closed() -> None:
    t = MockTransport([Reply(403, error_json("Invalid API token"))])
    c = client(t, max_credits_per_request=10)
    with pytest.raises(rf.CreditGuardUnavailableError) as info:
        c.tracks.list()
    assert "Invalid API token" in str(info.value)
    t2 = MockTransport([Reply(500, error_json("x"))] * 3 + [Reply(200, page_json())])
    c2 = client(t2, max_credits_per_request=10, guard_failure_mode="open")
    c2.tracks.list()
    assert t2.calls[-1].path == "/v1/tracks"


# --- auto_check_credits -----------------------------------------------------------------------------------


def test_preflight_insufficient() -> None:
    t = MockTransport([Reply(200, credits_json(spendable=4))])
    c = client(t, auto_check_credits=True)
    with pytest.raises(rf.PreflightInsufficientCreditsError) as info:
        c.tracks.deep_search("dark")  # 5 credits
    assert info.value.resource == "api_credits" and info.value.projected == 5
    assert len(t.calls) == 1


def test_preflight_uses_local_estimate_between_refreshes() -> None:
    t = MockTransport([Reply(200, credits_json(spendable=12))])
    t.default = Reply(200, page_json())
    c = client(t, auto_check_credits=True)
    c.tracks.deep_search("a")  # 5 → est 7
    c.tracks.deep_search("b")  # 5 → est 2
    with pytest.raises(rf.PreflightInsufficientCreditsError):
        c.tracks.deep_search("c")
    assert [r.path for r in t.calls].count("/v1/credits") == 1


def test_preflight_bandwidth_floor() -> None:
    t = MockTransport([Reply(200, credits_json())])
    c = client(t, auto_check_credits=True, min_bandwidth_bytes=1_000_000)
    with pytest.raises(rf.PreflightInsufficientCreditsError) as info:
        c.tracks.list()
    assert info.value.resource == "bandwidth"


def test_server_402_remains_authoritative() -> None:
    body = error_json("Insufficient credits. Please top up to continue.", apiCredits=3, bandwidthBytes=0)
    t = MockTransport([Reply(200, credits_json(spendable=1000)), Reply(402, body)])
    c = client(t, auto_check_credits=True)
    with pytest.raises(rf.InsufficientCreditsError) as info:
        c.tracks.list()
    assert info.value.api_credits == 3 and info.value.bandwidth_bytes == 0
    assert info.value.charge_state == "not_charged"


# --- low credit edge trigger ------------------------------------------------------------------------------


def test_low_credit_edge_triggered_and_rearmed() -> None:
    events: list[rf.LowCreditEvent] = []
    t = MockTransport(
        [
            Reply(200, credits_json(spendable=40)),
            Reply(200, credits_json(spendable=30)),
            Reply(200, credits_json(spendable=60)),
            Reply(200, credits_json(spendable=10)),
        ]
    )
    c = client(t, low_credit_warning_threshold=50, on_low_credit=events.append)
    for _ in range(4):
        c.credits.retrieve()
    assert [e.spendable for e in events] == [40, 10]
    assert events[0].source == "credits" and events[0].threshold == 50 and events[0].token_public_code == "AB12"


def test_low_credit_from_402_and_estimate() -> None:
    events: list[rf.LowCreditEvent] = []
    body = error_json("Insufficient credits. Please top up to continue.", apiCredits=2, bandwidthBytes=0)
    t = MockTransport([Reply(402, body)])
    c = client(t, low_credit_warning_threshold=5, on_low_credit=events.append)
    with pytest.raises(rf.InsufficientCreditsError):
        c.tracks.list()
    assert events[0].source == "insufficient_credits" and events[0].spendable == 2

    est: list[rf.LowCreditEvent] = []
    t2 = MockTransport([Reply(200, credits_json(spendable=20))])
    t2.default = Reply(200, page_json())
    c2 = client(t2, auto_check_credits=True, low_credit_warning_threshold=12, on_low_credit=est.append)
    c2.tracks.deep_search("x")  # 20 - 5 = 15
    c2.tracks.deep_search("y")  # 10 < 12 → estimate fires
    assert [e.source for e in est] == ["estimate"] and est[0].spendable == 10


def test_low_credit_default_logs_and_callback_errors_are_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    t = MockTransport(
        [
            Reply(200, credits_json(spendable=1)),
            Reply(200, credits_json(spendable=100)),
            Reply(200, credits_json(spendable=1)),
        ]
    )
    c = client(t, low_credit_warning_threshold=5)
    with caplog.at_level(logging.WARNING, logger="rightsfoundry"):
        c.credits.retrieve()
    assert "below the warning threshold" in caplog.text

    def bad(_e: rf.LowCreditEvent) -> None:
        raise RuntimeError("alerting down")

    c2 = client(
        MockTransport([Reply(200, credits_json(spendable=1))]), low_credit_warning_threshold=5, on_low_credit=bad
    )
    c2.credits.retrieve()  # does not raise


def test_credits_model() -> None:
    t = MockTransport([Reply(200, credits_json(spendable=3700, balance=1200, overdraft=2500))])
    credits = client(t).credits.retrieve()
    assert credits.api_credits.spendable == 3700 and credits.api_credits.overdraft_limit == 2500
    assert credits.costs["sync"] == 10 and credits.discounts["sync"].plan == "Pro Pack"
    assert credits.expiring_credits[0].api_credits == 500 and credits.token.public_code == "AB12"
    assert credits.subscription is None and credits.balance_source == "live"
    with pytest.raises(TypeError):
        credits.costs["sync"] = 1  # type: ignore[index]


# --- pagination ---------------------------------------------------------------------------------------------


def _pages(total: int, limit: int, sizes: list[int]) -> list[Reply]:
    replies = []
    next_id = 1
    for i, size in enumerate(sizes, start=1):
        ids = list(range(next_id, next_id + size))
        next_id += size
        replies.append(Reply(200, page_json(ids, total=total, page=i, limit=limit)))
    return replies


def test_iter_walks_until_total() -> None:
    t = MockTransport(_pages(250, 100, [100, 100, 50]))
    c = client(t)
    ids = [track.id for track in c.tracks.iter(genre="jazz")]
    assert ids == list(range(1, 251))
    assert [dict(r.query)["page"] for r in t.calls] == ["1", "2", "3"]
    assert all(dict(r.query)["limit"] == "100" and dict(r.query)["genre"] == "jazz" for r in t.calls)


def test_short_pages_do_not_stop_iteration() -> None:
    t = MockTransport(_pages(300, 100, [97, 100, 100]))
    pages = list(client(t).tracks.deep_search_iter("tense heist build-up").pages())
    assert [len(p) for p in pages] == [97, 100, 100]
    assert [r.json()["page"] for r in t.calls] == [1, 2, 3]
    assert all(r.json()["limit"] == 100 and r.json()["query"] == "tense heist build-up" for r in t.calls)


def test_empty_page_stops() -> None:
    t = MockTransport(_pages(1000, 100, [100, 0]))
    assert len(list(client(t).tracks.iter())) == 100
    assert len(t.calls) == 2


def test_max_pages_and_max_items() -> None:
    t = MockTransport(_pages(10_000, 100, [100, 100, 100]))
    assert len(list(client(t).tracks.iter(max_pages=2))) == 200
    assert len(t.calls) == 2
    t2 = MockTransport(_pages(10_000, 100, [100, 100]))
    assert len(list(client(t2).tracks.iter(max_items=150))) == 150
    assert len(t2.calls) == 2


def test_budget_stops_before_overspending() -> None:
    # search pages: first 2 credits, next 1 credit each → budget 4 allows pages 1-3 (2+1+1)
    t = MockTransport([Reply(200, credits_json()), *_pages(10000, 100, [100, 100, 100])])
    it = client(t).tracks.iter(search="jazz", max_credits=4)
    seen = 0
    with pytest.raises(rf.CreditBudgetExceededError) as info:
        for _ in it:
            seen += 1
    err = info.value
    assert seen == 300 and err.spent_projected == 4 and err.next_cost == 1 and err.cap == 4
    assert [r.path for r in t.calls] == ["/v1/credits"] + ["/v1/tracks"] * 3
    assert it.spent_projected == 4


def test_budget_first_page_too_expensive() -> None:
    t = MockTransport([Reply(200, credits_json())])
    with pytest.raises(rf.CreditBudgetExceededError) as info:
        list(client(t).tracks.search_similar_iter("https://cdn.example.com/ref.wav", max_credits=7))
    assert info.value.next_cost == 8 and len(t.calls) == 1


def test_budget_without_rate_card_fails_closed() -> None:
    t = MockTransport([Reply(500, error_json("x"))] * 3)
    with pytest.raises(rf.CreditGuardUnavailableError):
        list(client(t).tracks.iter(max_credits=10))


def test_iterator_validation() -> None:
    c = client(MockTransport())
    with pytest.raises(rf.ValidationError):
        c.tracks.iter(limit=101)
    with pytest.raises(rf.ValidationError):
        c.tracks.iter(max_pages=0)
    with pytest.raises(rf.ValidationError):
        c.tracks.iter(max_credits=-1)
    with pytest.raises(rf.ValidationError):
        c.tracks.deep_search_iter("")
    with pytest.raises(rf.ValidationError):
        c.tracks.search_similar_iter("https://cdn.example.com/a.ogg")


def test_has_next_page() -> None:
    t = MockTransport(
        [Reply(200, page_json([1, 2], total=5, page=1, limit=2)), Reply(200, page_json([], total=5, page=3, limit=2))]
    )
    c = client(t)
    assert c.tracks.list(limit=2).has_next_page() is True
    assert c.tracks.list(limit=2, page=3).has_next_page() is False


def _record(_r: Recorded) -> Reply:
    return Reply(200, page_json())
