"""AsyncClient: lifecycle, iterators, concurrency, cancellation, single-flight."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import (
    CANARY_KEY,
    AsyncMockTransport,
    MockTransport,
    Recorded,
    Reply,
    async_client,
    credits_json,
    error_json,
    page_json,
    sync_json,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_lifecycle_and_basic_calls() -> None:
    async def main() -> None:
        t = AsyncMockTransport(
            [
                Reply(200, credits_json()),
                Reply(200, page_json()),
                Reply(200, {"success": True, "data": {"genre": [{"value": "jazz", "count": 3}]}}),
                Reply(200, {"success": True, "status": "alive"}),
            ]
        )
        async with async_client(t) as c:
            assert (await c.credits.retrieve()).api_credits.spendable == 1000
            assert len(await c.tracks.list(genre="jazz")) == 2
            assert (await c.taxonomies.list()).values("genre") == ["jazz"]
            assert (await c.health.live()).alive
        assert "authorization" not in t.calls[3].headers

    run(main())


def test_async_errors_and_retries() -> None:
    async def main() -> None:
        t = AsyncMockTransport([Reply(500, error_json("Internal server error")), Reply(200, page_json())])
        c = async_client(t)
        page = await c.tracks.list()
        assert page.retried_after_refundable_failure is True and len(t.calls) == 2
        t.add(Reply(403, error_json("Sync licensing is not enabled for this API token. Contact support to enable it.")))
        with pytest.raises(rf.FeatureNotEnabledError) as info:
            await c.tracks.sync(1)
        assert info.value.feature == "sync"

    run(main())


def test_async_iterators_items_and_pages() -> None:
    async def main() -> None:
        replies = [
            Reply(200, page_json(range(1, 101), total=150, page=1, limit=100)),
            Reply(200, page_json(range(101, 151), total=150, page=2, limit=100)),
        ]
        c = async_client(AsyncMockTransport(replies))
        ids = [t.id async for t in c.tracks.iter(mood="uplifting")]
        assert ids == list(range(1, 151))

        c2 = async_client(AsyncMockTransport([Reply(200, page_json(range(1, 101), total=900, page=1, limit=100))] * 2))
        pages = [p async for p in c2.tracks.deep_search_iter("brief", max_pages=2).pages()]
        assert [p.page for p in pages] == [1, 1]

        c3 = async_client(AsyncMockTransport([Reply(200, page_json(range(1, 101), total=900, page=1, limit=100))]))
        items = [t async for t in c3.tracks.search_similar_iter("https://cdn.example.com/x.mp3", max_items=10)]
        assert len(items) == 10

    run(main())


def test_async_budget() -> None:
    async def main() -> None:
        t = AsyncMockTransport(
            [Reply(200, credits_json()), Reply(200, page_json(range(1, 101), total=900, page=1, limit=100))]
        )
        c = async_client(t)
        with pytest.raises(rf.CreditBudgetExceededError):
            async for _ in c.tracks.deep_search_iter("x", max_credits=5):
                pass
        assert [r.path for r in t.calls] == ["/v1/credits", "/v1/tracks/deep-search"]

    run(main())


def test_async_single_flight_sync() -> None:
    async def main() -> None:
        t = AsyncMockTransport([Reply(200, sync_json(), delay=0.05)])
        c = async_client(t)
        links = await asyncio.gather(*(c.tracks.sync(29446060) for _ in range(8)))
        assert len(t.calls) == 1 and all(link is links[0] for link in links)
        assert c.tracks._inflight == {}

    run(main())


def test_async_single_flight_leader_cancellation_does_not_cancel_followers() -> None:
    async def main() -> None:
        t = AsyncMockTransport([Reply(200, sync_json(), delay=0.1)])
        c = async_client(t)
        leader = asyncio.ensure_future(c.tracks.sync(7))
        await asyncio.sleep(0.01)
        follower = asyncio.ensure_future(c.tracks.sync(7))
        await asyncio.sleep(0.01)
        leader.cancel()
        link = await follower
        assert link.id == 29446060 and len(t.calls) == 1
        with pytest.raises(asyncio.CancelledError):
            await leader

    run(main())


def test_async_single_flight_error_propagates() -> None:
    async def main() -> None:
        t = AsyncMockTransport([Reply(404, error_json("Track not found"), delay=0.02)])
        c = async_client(t)
        results = await asyncio.gather(c.tracks.sync(9), c.tracks.sync(9), return_exceptions=True)
        assert all(isinstance(r, rf.TrackNotFoundError) for r in results) and len(t.calls) == 1

    run(main())


def test_max_concurrency_bounds_in_flight() -> None:
    async def main() -> None:
        active = 0
        peak = 0

        class Counting:
            async def send(self, request: rf.PreparedRequest, **_: float) -> rf.AsyncRawResponse:
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1
                return rf.AsyncRawResponse(200, {"content-type": "application/json"}, b'{"success":true,"data":{}}')

        c = rf.AsyncClient(api_key=CANARY_KEY, async_transport=Counting(), max_concurrency=2)
        await asyncio.gather(*(c.taxonomies.list() for _ in range(8)))
        assert peak == 2

    run(main())


def test_executor_transport_runs_sync_transport_and_propagates_cancellation() -> None:
    gate = threading.Event()
    closed = threading.Event()

    def slow(_r: Recorded) -> Reply:
        gate.wait(5)
        return Reply(200, page_json())

    class Tracking(MockTransport):
        def send(self, request: rf.PreparedRequest, **kw: float) -> rf.RawResponse:
            raw = super().send(request, **kw)
            return rf.RawResponse(raw.status, raw.headers, b"".join(raw.iter_bytes()), on_close=closed.set)

    sync_t = Tracking([slow, Reply(200, page_json())])

    async def main() -> None:
        c = rf.AsyncClient(api_key=CANARY_KEY, transport=sync_t)
        try:
            task = asyncio.ensure_future(c.tracks.list())
            while not sync_t.calls:
                await asyncio.sleep(0.005)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            gate.set()
            # the abandoned response is closed in the background
            for _ in range(200):
                if closed.is_set():
                    break
                await asyncio.sleep(0.005)
            assert closed.is_set()
            page = await c.tracks.list()
            assert len(page) == 2
        finally:
            await c.aclose()

    run(main())


def test_async_unauthenticated_and_with_options() -> None:
    async def main() -> None:
        t = AsyncMockTransport(
            [Reply(503, {"success": False, "status": "degraded", "checks": {}}, {"retry-after": "5"})]
        )
        c = rf.AsyncClient.unauthenticated(async_transport=t, max_retries=0)
        status = await c.health.check()
        assert status.http_status == 503 and status.status == "degraded" and status.retry_after == 5.0
        with pytest.raises(rf.ConfigurationError):
            _ = c.tracks
        d = async_client(AsyncMockTransport([Reply(200, page_json())])).with_options(max_retries=0)
        assert d.config.max_retries == 0

    run(main())


def test_async_request_escape_hatch() -> None:
    async def main() -> None:
        t = AsyncMockTransport([Reply(200, {"success": True, "x": 1})])
        c = async_client(t)
        assert await c.request("GET", "/v1/whatever") == {"success": True, "x": 1}

    run(main())


def test_async_timeout_error_is_typed() -> None:
    async def main() -> None:
        t = AsyncMockTransport([Reply(raises=rf.APITimeoutError("slow", phase="read", charge_state="unknown"))])
        c = async_client(t)
        with pytest.raises(rf.APITimeoutError) as info:
            await c.tracks.list()
        assert info.value.phase == "read" and info.value.charge_state == "unknown"

    run(main())


def test_async_total_runtime_is_concurrent() -> None:
    async def main() -> float:
        t = AsyncMockTransport([Reply(200, page_json(), delay=0.05) for _ in range(5)])
        c = async_client(t)
        start = time.monotonic()
        await asyncio.gather(*(c.tracks.list() for _ in range(5)))
        return time.monotonic() - start

    assert run(main()) < 0.2
