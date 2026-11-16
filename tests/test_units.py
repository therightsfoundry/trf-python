"""Unit tests for validators, transport primitives, the redactor, Secret and models."""

from __future__ import annotations

import asyncio
import ipaddress
import pickle
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import CANARY_KEY, SIGNED_MP3, MockTransport, Reply, client, credits_json, page_json, track_json
from rightsfoundry._redact import _RedactingHeaders, redact_response_headers, redact_text, redact_value
from rightsfoundry._transport import AsyncExecutorTransport, Headers, HTTPTransport, PreparedRequest
from rightsfoundry._validation import (
    build_list_query,
    encode_number,
    encode_query_value,
    parse_legacy_ipv4,
    validate_file_url,
    validate_idempotency_key,
    validate_page_limit,
    validate_query_text,
    validate_request_query,
)

# --- tracks.list parameters (SEC-10) ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"unknown": 1},
        {"limit": 101},
        {"limit": 0},
        {"limit": "20"},
        {"limit": True},
        {"page": 0},
        {"page": 1.5},
        {"bpm_max": 1000},
        {"bpm_min": -1},
        {"bpm_min": float("nan")},
        {"bpm_min": float("inf")},
        {"bpm_min": "90"},
        {"bpm_min": True},
        {"duration_max": 100_000},
        {"bpm_min": 120, "bpm_max": 90},
        {"duration_min": 60, "duration_max": 30},
        {"vocals": "choir"},
        {"sort_by": "random"},
        {"sort_order": "up"},
        {"featured": "true"},
        {"explicit": 1},
        {"search": "x" * 201},
        {"genre": "g" * 101},
        {"artist": "a" * 201},
        {"key": "C sharp minor!"},
        {"decade": "the nineteen-nineties"},
        {"search": "bad\x00null"},
        {"mood": "da\u0085rk"},
        {"genre": 5},
        {"id": 0},
        {"id": "12"},
        {"id": 10**16},
    ],
)
def test_list_param_rejections(params: dict[str, Any]) -> None:
    with pytest.raises(rf.ValidationError):
        build_list_query(params)


def test_list_query_encoding_is_typed_trimmed_and_sorted() -> None:
    query = build_list_query(
        {
            "search": "  dark epic ",
            "genre": "",
            "mood": "   ",
            "bpm_min": 90.0,
            "bpm_max": 120.25,
            "duration_min": 0,
            "featured": False,
            "include_children": True,
            "id": 29446060,
            "sort_by": "first_published",
        }
    )
    assert query == (
        ("bpmMax", "120.25"),
        ("bpmMin", "90"),
        ("durationMin", "0"),
        ("featured", "false"),
        ("id", "29446060"),
        ("includeChildren", "true"),
        ("search", "dark epic"),
        ("sortBy", "first_published"),
    )


def test_encode_number() -> None:
    assert encode_number(90) == "90"
    assert encode_number(90.0) == "90"
    assert encode_number(0.1 + 0.2) == "0.3"
    assert encode_number(1e-9) == "0"
    assert "e" not in encode_number(99999.5)


def test_page_limit_and_query_text() -> None:
    assert validate_page_limit(2, 100) == (2, 100)
    for page, limit in ((0, 1), (1, 0), (1, 101), (True, 1), (1, "5")):
        with pytest.raises(rf.ValidationError):
            validate_page_limit(page, limit)
    assert validate_query_text("  brief  ") == "brief"
    for bad in ("", "   ", "x" * 501, 5, "a\x07b"):
        with pytest.raises(rf.ValidationError):
            validate_query_text(bad)


# --- file_url (SEC-11) ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        5,
        "",
        "   ",
        "https://cdn.example.com/" + "a" * 2000 + ".mp3",
        "https://cdn.example.com/a b.mp3",
        "https://cdn.example.com/a\\b.mp3",
        "ftp://cdn.example.com/a.mp3",
        "file:///etc/passwd.mp3",
        "https:///a.mp3",
        "https://user:pw@cdn.example.com/a.mp3",
        "https://cdn.example.com/a.ogg",
        "https://cdn.example.com/a.mp3/",
        "https://cdn.example.com/a.mp3.exe",
        "https://cdn.example.com:22/a.mp3",
        "https://cdn.example.com:99999/a.mp3",
        "https://localhost/a.mp3",
        "https://foo.localhost/a.mp3",
        "https://intranet/a.mp3",
        "https://127.0.0.1/a.mp3",
        "https://10.1.2.3/a.wav",
        "https://172.16.0.1/a.wav",
        "https://192.168.1.1/a.wav",
        "https://169.254.169.254/a.wav",
        "https://100.64.0.1/a.wav",
        "https://0.0.0.0/a.wav",
        "https://224.0.0.1/a.wav",
        "https://[::1]/a.wav",
        "https://[fd00::1]/a.wav",
        "https://[fe80::1]/a.wav",
        "https://[::ffff:10.0.0.1]/a.wav",
        "https://2130706433/a.wav",
        "https://0x7f000001/a.wav",
        "https://0177.0.0.1/a.wav",
        "https://1.2.3.999/a.wav",
        "https://bücher.example/a.wav",
        "https://-bad-.example.com/a.wav",
    ],
)
def test_file_url_rejections(url: Any) -> None:
    with pytest.raises(rf.ValidationError):
        validate_file_url(url)


@pytest.mark.parametrize(
    ("url", "plain"),
    [
        ("https://cdn.example.com/ref/temp-track.mp3", False),
        ("https://cdn.example.com/ref/TEMP.WAV?token=abc#t=1", False),
        ("http://cdn.example.com:8080/a.mp3", True),
        ("https://8.8.8.8/a.mp3", False),
        ("https://xn--bcher-kva.example/a.wav", False),
    ],
)
def test_file_url_accepted(url: str, plain: bool) -> None:
    assert validate_file_url(url) == (url, plain)


def test_plain_http_file_url_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    t = MockTransport([Reply(200, page_json()), Reply(200, page_json())])
    c = client(t)
    with caplog.at_level("WARNING", logger="rightsfoundry"):
        c.tracks.search_similar("http://cdn.example.com/a.mp3")
        c.tracks.search_similar("http://cdn.example.com/b.mp3")
    assert caplog.text.count("plain http://") == 1


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("2130706433", "127.0.0.1"),
        ("0x7f000001", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("10.0.1", "10.0.0.1"),
        ("169.254.43518", "169.254.169.254"),
        ("1.2.3.4.", "1.2.3.4"),
        ("0x", "0.0.0.0"),
    ],
)
def test_parse_legacy_ipv4(host: str, expected: str) -> None:
    assert parse_legacy_ipv4(host) == ipaddress.IPv4Address(expected)


@pytest.mark.parametrize("host", ["", "1.2.3.4.5", "256.1.1.1", "1..2", "08.1.1.1", "4294967296", "a.b", "1.2.3.256"])
def test_parse_legacy_ipv4_rejects(host: str) -> None:
    assert parse_legacy_ipv4(host) is None


# --- client.request helpers --------------------------------------------------------------------------------


def test_request_query_and_idempotency_validation() -> None:
    assert validate_request_query(None) == ()
    assert validate_request_query({"b": 1.5, "a": "x", "c": None}) == (("a", "x"), ("b", "1.5"))
    bad_queries: list[Any] = [{"a b": 1}, {"a": [1]}, {"a": "x\n"}, {"a": float("nan")}, {"a": "x" * 2001}, ["a"]]
    for bad_query in bad_queries:
        with pytest.raises(rf.ValidationError):
            validate_request_query(bad_query)
    assert encode_query_value("x", True) == "true"
    assert validate_idempotency_key(None) is None
    assert validate_idempotency_key("abc-123") == "abc-123"
    bad_keys: list[Any] = ["", "a b", "x" * 256, 5, "é"]
    for bad_key in bad_keys:
        with pytest.raises(rf.ValidationError):
            validate_idempotency_key(bad_key)


def test_request_method_and_body_validation() -> None:
    c = client(MockTransport())
    with pytest.raises(rf.ValidationError):
        c.request("TRACE", "/v1/tracks")
    with pytest.raises(rf.ValidationError):
        c.request("POST", "/v1/x", json=["not", "an", "object"])  # type: ignore[arg-type]
    with pytest.raises(rf.ValidationError):
        c.request("POST", "/v1/x", json={"a": float("nan")})
    with pytest.raises(rf.ValidationError):
        c.request("GET", "/v1/tracks", max_credits=-1)


# --- transport primitives -----------------------------------------------------------------------------------


def test_headers_container() -> None:
    h = Headers([("Content-Type", "a"), ("X-A", "1"), ("x-a", "2")])
    assert h.get("content-type") == "a" and h.get("missing", "d") == "d"
    assert h.get_all("X-A") == ["1", "2"] and "X-A" in h and 5 not in h
    assert len(h) == 3 and next(iter(h)) == ("Content-Type", "a")
    assert h.lower_dict() == {"content-type": "a", "x-a": "1"}
    assert Headers({"A": "b"}).items() == [("A", "b")] and len(Headers()) == 0
    assert "3 headers" in repr(h)


def test_raw_response_bodies() -> None:
    closed: list[bool] = []
    raw = rf.RawResponse(200, {"a": "b"}, [b"x", b"", b"y"], on_close=lambda: closed.append(True))
    assert list(raw.iter_bytes()) == [b"x", b"y"] and "200" in repr(raw)
    raw.close()
    raw.close()
    assert closed == [True]
    big = rf.RawResponse(200, [], b"z" * 100_000)
    assert b"".join(big.iter_bytes()) == b"z" * 100_000

    async def main() -> None:
        async def agen() -> Any:
            yield b"a"
            yield b"b"

        for body in (b"ab", [b"a", b"b"], agen()):
            ar = rf.AsyncRawResponse(200, {}, body, on_close=lambda: None)
            assert b"".join([c async for c in ar.aiter_bytes()]) == b"ab"
            await ar.aclose()
            await ar.aclose()
        assert "200" in repr(rf.AsyncRawResponse(200, {}))

        async def async_close() -> None:
            closed.append(False)

        ar2 = rf.AsyncRawResponse(200, {}, b"", on_close=async_close)
        await ar2.aclose()

    asyncio.run(main())
    assert closed == [True, False]


def test_prepared_request_repr_is_redacted() -> None:
    headers = _RedactingHeaders([("Authorization", f"Bearer {CANARY_KEY}")], rf.Secret(CANARY_KEY))
    req = PreparedRequest("GET", SIGNED_MP3, headers)
    assert CANARY_KEY not in repr(req) and "sig=[REDACTED]" in repr(req)
    assert "Authorization" in headers and headers.get("authorization") is not None and len(headers) == 1
    assert headers.get("nope") is None


def test_http_transport_refuses_plain_http_to_non_loopback() -> None:
    t = HTTPTransport()
    req = PreparedRequest("GET", "http://example.com/v1/x", _RedactingHeaders())
    with pytest.raises(rf.APIConnectionError, match="plain http"):
        t.send(req, connect_timeout=1, read_timeout=1, write_timeout=1)
    t.close()
    with pytest.raises(rf.APIConnectionError, match="closed"):
        t.send(req, connect_timeout=1, read_timeout=1, write_timeout=1)


def test_async_executor_transport_wraps_sync_transport() -> None:
    inner = MockTransport([Reply(200, b"hello world")])
    executor = AsyncExecutorTransport(inner, max_workers=2)

    async def main() -> bytes:
        resp = await executor.send(
            PreparedRequest("GET", "https://api.therightsfoundry.com/v1/x", _RedactingHeaders()),
            connect_timeout=1,
            read_timeout=1,
            write_timeout=1,
        )
        data = b"".join([c async for c in resp.aiter_bytes()])
        await resp.aclose()
        return data

    assert asyncio.run(main()) == b"hello world"
    assert executor.transport is inner
    assert executor.run_sync(lambda: 41 + 1).result(5) == 42
    executor.close()


# --- redactor & secret --------------------------------------------------------------------------------------


def test_redact_edge_cases() -> None:
    assert redact_text("") == ""
    secret = rf.Secret(CANARY_KEY)
    assert redact_text(f"key={CANARY_KEY.upper()}", secret) == f"key={secret.redacted()}"
    deep: Any = "x"
    for _ in range(80):
        deep = [deep]
    assert "[TRUNCATED]" in repr(redact_value(deep))
    assert redact_value(5) == 5 and redact_value((SIGNED_MP3,)) == [redact_text(SIGNED_MP3)]
    headers = redact_response_headers([("Set-Cookie", "a"), ("X-Request-Id", "r"), ("Date", "x" * 9000)])
    assert headers == {"x-request-id": "r"}
    with pytest.raises(TypeError):
        pickle.dumps(_RedactingHeaders())
    assert "Bearer [REDACTED]" in str(_RedactingHeaders([("Authorization", "Bearer x")]))


def test_secret_misc() -> None:
    s = rf.Secret(CANARY_KEY)
    with pytest.raises(TypeError):
        rf.Secret(5)  # type: ignore[arg-type]
    with pytest.raises(AttributeError):
        del s._fp
    assert hash(s) == hash(rf.Secret(CANARY_KEY)) and len(s) == 128 and bool(s)
    assert (s == 5) is False
    import copy

    assert copy.copy(s) is s
    with pytest.raises(TypeError):
        s.__getstate__()
    with pytest.raises(TypeError):
        s.__reduce__()


# --- models --------------------------------------------------------------------------------------------------


def test_models_are_frozen_and_forward_compatible() -> None:
    track = rf.Track.from_json(track_json(1, main_genre="folk", tag_source="brand-new-source", term_matches=3.0))
    assert track.main_genre == ("folk",) and track.tag_source == "brand-new-source" and track.term_matches == 3
    assert track.redacted_asset_url() is not None and "sig=[REDACTED]" in (track.redacted_asset_url() or "")
    with pytest.raises(AttributeError):
        track.title = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        track.raw["title"] = "x"  # type: ignore[index]
    assert track.to_dict()["future_field"] == {"added": "later"}
    with pytest.raises(rf.UnexpectedResponseError):
        rf.Track.from_json({"id": True})
    with pytest.raises(rf.UnexpectedResponseError):
        rf.Track.from_json([])


def test_sync_link_helpers_and_taxonomies() -> None:
    from helpers import sync_json

    link = rf.SyncLink.from_json(sync_json()["data"])
    assert link.expires_at_datetime is not None and link.expires_at_datetime.year == 2100
    bad = rf.SyncLink.from_json({**sync_json()["data"], "expires_at": "soon"})
    assert bad.expires_at_datetime is None
    with pytest.raises(rf.UnexpectedResponseError):
        rf.SyncLink.from_json({"id": 1})
    tax = rf.Taxonomies.from_json(
        {
            "genre": [{"value": "jazz", "count": 3}, {"bad": 1}],
            "brand_new_facet": [{"value": "x", "count": 1}],
            "meta": 5,
        }
    )
    assert tax.values("genre") == ["jazz"] and tax.get("brand_new_facet")[0].value == "x" and tax.get("nope") == ()
    assert "brand_new_facet=1" in repr(tax)


def test_credits_tolerates_partial_payloads() -> None:
    body = credits_json()
    body["discounts"] = {"sync": "weird", "x": {"standard": 2}}
    body["subscription"] = {"plan_id": 3, "plan": "Studio"}
    body["expiring_credits"] = [5, {"api_credits": 1}]
    body["costs"] = {"sync": 10, "weird": "x"}
    credits = rf.Credits.from_json(body)
    assert credits.subscription is not None and credits.subscription.plan == "Studio"
    assert set(credits.discounts) == {"x"} and credits.costs == {"sync": 10}
    assert len(credits.expiring_credits) == 1 and "spendable=1000" in repr(credits)
    with pytest.raises(rf.UnexpectedResponseError):
        rf.Credits.from_json({"api_credits": {"balance": "1"}, "bandwidth_bytes": {}})


def test_health_models() -> None:
    ready = rf.ReadinessStatus.from_json(
        {"success": False, "status": "not ready", "checks": {"billing": {"status": "unconfigured", "required": True}}},
        http_status=503,
    )
    assert ready.ready is False and ready.billing is not None and ready.billing.required is True
    health = rf.HealthStatus.from_json(
        {"success": True, "status": "healthy", "checks": {"database": 5}}, http_status=200
    )
    assert health.healthy and health.database is None


def test_error_repr_and_unknown_attribute() -> None:
    err = rf.TrackNotFoundError("gone", status=404, request_id="r", operation="tracks.get")
    assert "status=404" in repr(err) and "request_id=r" in str(err)
    with pytest.raises(TypeError):
        rf.ValidationError("x", not_an_attribute=1)
