"""Shared test doubles and canned API payloads (no network, no real keys)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Callable, Optional, Union

import rightsfoundry as rf
from rightsfoundry import AsyncRawResponse, PreparedRequest, RawResponse

#: A canary key: if these characters ever appear in a log, repr, hook event or
#: pickled error, the redaction layer has a hole.
CANARY_KEY = "c0ffee11" * 16
OTHER_KEY = "abcdef01" * 16
CANARY_SIG = "5ec2e7" + "ab" * 29
FUTURE_EXPIRES = 4102444800  # 2100-01-01
SIGNED_WAV = (
    f"https://assets.therightsfoundry.com/stream/AB12/29446060/29446060_track.wav?expires={FUTURE_EXPIRES}"
    f"&sig={CANARY_SIG}"
)
SIGNED_MP3 = (
    f"https://assets.therightsfoundry.com/stream/AB12/29446060/29446060_track.mp3?expires={FUTURE_EXPIRES}"
    f"&sig={CANARY_SIG}"
)

Body = Union[bytes, Mapping[str, Any], list[Any], str, None]


class Recorded:
    def __init__(self, request: PreparedRequest, timeouts: Mapping[str, float]) -> None:
        self.method = request.method
        self.url = request.url
        self.headers = {k.lower(): v for k, v in request.headers}
        self.header_names = request.headers.names()
        self.body = request.body
        self.timeouts = dict(timeouts)

    @property
    def path(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.url).path

    @property
    def query(self) -> list[tuple[str, str]]:
        from urllib.parse import parse_qsl, urlsplit

        return parse_qsl(urlsplit(self.url).query, keep_blank_values=True)

    def json(self) -> Any:
        return json.loads(self.body or b"null")


class Reply:
    """A canned response or an exception to raise from the transport."""

    def __init__(
        self,
        status: int = 200,
        body: Body = None,
        headers: Optional[Mapping[str, str]] = None,
        *,
        raises: Optional[BaseException] = None,
        chunks: Optional[Iterable[bytes]] = None,
        delay: float = 0.0,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.raises = raises
        self.delay = delay
        if chunks is not None:
            self.body: Union[bytes, Iterable[bytes]] = chunks
        elif body is None:
            self.body = b""
        elif isinstance(body, bytes):
            self.body = body
        elif isinstance(body, str):
            self.body = body.encode()
        else:
            self.body = json.dumps(body).encode()
            self.headers.setdefault("content-type", "application/json")
        self.headers.setdefault("x-request-id", "req-" + str(status))


ReplySource = Union[Reply, Callable[[Recorded], Reply]]


class MockTransport:
    """A thread-safe scripted :class:`rightsfoundry.Transport`."""

    def __init__(self, replies: Iterable[ReplySource] = ()) -> None:
        self.replies: list[ReplySource] = list(replies)
        self.calls: list[Recorded] = []
        self.lock = threading.Lock()
        self.default: Optional[ReplySource] = None

    def add(self, *replies: ReplySource) -> MockTransport:
        self.replies.extend(replies)
        return self

    def _next(self, rec: Recorded) -> Reply:
        with self.lock:
            self.calls.append(rec)
            if self.replies:
                source = self.replies.pop(0)
            elif self.default is not None:
                source = self.default
            else:
                raise AssertionError(f"unexpected request {rec.method} {rec.url}")
        return source(rec) if callable(source) else source

    def send(
        self,
        request: PreparedRequest,
        *,
        connect_timeout: float,
        read_timeout: float,
        write_timeout: float,
    ) -> RawResponse:
        rec = Recorded(
            request,
            {"connect_timeout": connect_timeout, "read_timeout": read_timeout, "write_timeout": write_timeout},
        )
        reply = self._next(rec)
        if reply.delay:
            time.sleep(reply.delay)
        if reply.raises is not None:
            raise reply.raises
        return RawResponse(reply.status, reply.headers, reply.body)


class AsyncMockTransport:
    """The async twin of :class:`MockTransport`."""

    def __init__(self, replies: Iterable[ReplySource] = ()) -> None:
        self.inner = MockTransport(replies)

    @property
    def calls(self) -> list[Recorded]:
        return self.inner.calls

    def add(self, *replies: ReplySource) -> AsyncMockTransport:
        self.inner.add(*replies)
        return self

    async def send(
        self,
        request: PreparedRequest,
        *,
        connect_timeout: float,
        read_timeout: float,
        write_timeout: float,
    ) -> AsyncRawResponse:
        import asyncio

        rec = Recorded(
            request,
            {"connect_timeout": connect_timeout, "read_timeout": read_timeout, "write_timeout": write_timeout},
        )
        reply = self.inner._next(rec)
        if reply.delay:
            await asyncio.sleep(reply.delay)
        if reply.raises is not None:
            raise reply.raises
        return AsyncRawResponse(reply.status, reply.headers, reply.body)


def client(transport: MockTransport, **options: Any) -> rf.Client:
    options.setdefault("api_key", CANARY_KEY)
    c = rf.Client(transport=transport, **options)
    c._pipeline._sleep = lambda _s: None
    return c


def sleeps(c: rf.Client) -> list[float]:
    recorded: list[float] = []
    c._pipeline._sleep = recorded.append
    return recorded


def async_client(transport: AsyncMockTransport, **options: Any) -> rf.AsyncClient:
    options.setdefault("api_key", CANARY_KEY)
    c = rf.AsyncClient(async_transport=transport, **options)

    async def no_sleep(_s: float) -> None:
        return None

    c._pipeline._sleep = no_sleep
    return c


# --- canned payloads -----------------------------------------------------------------


def track_json(track_id: int = 29446060, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": track_id,
        "title": "Golden Hour",
        "artist": "Ada Lane",
        "performed_by": "Ada Lane",
        "album": "Daylight",
        "duration": 142.5,
        "bpm": 96,
        "musical_key": "A minor",
        "decades": "2020s",
        "vocals": "instrumental",
        "explicit": False,
        "first_published": "2024-03-01T00:00:00.000Z",
        "release_date": None,
        "asset_url": SIGNED_MP3,
        "peak_data_url": None,
        "genres": ["acoustic", "folk"],
        "sub_genres": [],
        "moods": ["hopeful", "warm"],
        "instruments": ["acoustic guitar"],
        "sub_instruments": [],
        "themes": ["nature"],
        "sub_themes": [],
        "main_genre": ["folk"],
        "subgenre": None,
        "free_genre": None,
        "mood_simple": ["happy"],
        "mood_advanced": None,
        "character": ["warm"],
        "movement": None,
        "instruments_detected": ["acousticGuitar"],
        "music_for": ["documentary"],
        "vocals_detected": None,
        "vocal_presence": "none",
        "vocal_gender": None,
        "vocal_style": None,
        "voiceover_degree": 0.8,
        "voiceover_dominant": True,
        "bpm_detected": 96.2,
        "bpm_confidence": 0.9,
        "key": "A minor",
        "key_confidence": 0.7,
        "tempo": "medium",
        "time_signature": "4/4",
        "valence": 0.6,
        "arousal": 0.4,
        "energy_level": "medium",
        "emotion_profile": "positive",
        "musical_era": "2020s",
        "production_year": 2023,
        "auto_description": "A warm acoustic guitar piece.",
        "is_ai_music": False,
        "ai_music_score": 0.01,
        "tag_source": "direct",
        "tagged_at": "2026-09-03T00:00:00.000Z",
        "has_alternatives": True,
        "future_field": {"added": "later"},
    }
    data.update(overrides)
    return data


def page_json(
    ids: Iterable[int] = (1, 2), *, total: int = 2, page: int = 1, limit: int = 20, **extra: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "success": True,
        "data": [track_json(i) for i in ids],
        "total": total,
        "page": page,
        "limit": limit,
    }
    body.update(extra)
    return body


def costs(**overrides: int) -> dict[str, int]:
    card = {
        "tracks_list": 1,
        "tracks_search_first_page": 2,
        "tracks_search_next_page": 1,
        "track_detail": 1,
        "deep_search_first_page": 5,
        "deep_search_next_page": 1,
        "file_search_first_page": 8,
        "file_search_next_page": 1,
        "sync": 10,
        "export_sync": 25,
        "stream": 0,
    }
    card.update(overrides)
    return card


def credits_json(
    spendable: int = 1000, *, balance: Optional[int] = None, overdraft: int = 0, **kw: Any
) -> dict[str, Any]:
    bal = spendable - overdraft if balance is None else balance
    body: dict[str, Any] = {
        "success": True,
        "as_of": "2026-11-16T10:00:00.000Z",
        "balance_source": "live",
        "api_credits": {"balance": bal, "overdraft_limit": overdraft, "spendable": bal + overdraft},
        "bandwidth_bytes": {"balance": 10_000, "overdraft_limit": 0, "spendable": 10_000},
        "expiring_credits": [{"api_credits": 500, "bandwidth_bytes": 0, "expires_at": "2027-01-01T00:00:00.000Z"}],
        "costs": kw.pop("card", costs()),
        "standard_costs": costs(),
        "discounts": {"sync": {"standard": 12, "plan_id": 7, "plan": "Pro Pack"}},
        "subscription": None,
        "token": {
            "name": "Default",
            "public_code": "AB12",
            "api_calls_month": 42,
            "bandwidth_month_bytes": 0,
            "month_reset_at": "2026-12-01T00:00:00.000Z",
            "expires_at": None,
            "daily_call_limit": None,
        },
    }
    body.update(kw)
    return body


def sync_json(
    track_id: int = 29446060, *, charged: int = 10, reissued: bool = False, export: bool = False
) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "id": track_id,
            "title": "Golden Hour",
            "artist": "Ada Lane",
            "format": "wav",
            "file_name": "29446060_track.wav",
            "download_url": SIGNED_WAV,
            "expires_at": "2100-01-01T00:00:00.000Z",
            "expires_in": 3600,
            "credits_charged": charged,
            "reissued": reissued,
            "external_export": export,
            "billing": {"charged_on": "api_call", "note": "Credits are deducted for this API call."},
        },
    }


def error_json(error: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"success": False, "error": error}
    body.update(extra)
    return body


def chunks(data: bytes, size: int = 4096) -> Iterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]
