"""Runs the shared, language-neutral conformance fixtures (conformance/fixtures.json) against the SDK."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import MockTransport, Reply, client
from rightsfoundry._errors import KNOWN_BAD_REQUEST_MESSAGES, MESSAGE_TABLE

FIXTURES = Path(__file__).resolve().parents[1] / "conformance" / "fixtures.json"
DOC = json.loads(FIXTURES.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = DOC["cases"]


def invoke(c: rf.Client, operation: str, args: dict[str, Any]) -> Any:
    a = dict(args)
    if operation == "tracks.list":
        return c.tracks.list(**a)
    if operation == "tracks.get":
        return c.tracks.get(a.pop("track_id"), **a)
    if operation == "tracks.deep_search":
        return c.tracks.deep_search(a.pop("query"), **a)
    if operation == "tracks.search_similar":
        return c.tracks.search_similar(a.pop("file_url"), **a)
    if operation == "tracks.sync":
        return c.tracks.sync(a.pop("track_id"), **a)
    if operation == "taxonomies.list":
        return c.taxonomies.list(**a)
    if operation == "credits.retrieve":
        return c.credits.retrieve(**a)
    if operation == "health.check":
        return c.health.check(**a)
    if operation == "health.live":
        return c.health.live(**a)
    if operation == "health.ready":
        return c.health.ready(**a)
    raise AssertionError(f"unknown operation {operation}")


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_conformance_case(case: dict[str, Any]) -> None:
    replies = [Reply(r["status"], r["body"], r.get("headers")) for r in case["canned_responses"]]
    transport = MockTransport(replies)
    c = client(transport, **case.get("client", {}))
    outcome = case["expected_outcome"]
    if "error_class" in outcome:
        expected_cls = getattr(rf, outcome["error_class"])
        with pytest.raises(rf.RightsFoundryError) as info:
            invoke(c, case["operation"], case["args"])
        err = info.value
        assert type(err) is expected_cls, f"{type(err).__name__} != {outcome['error_class']}"
        assert err.charge_state == outcome["charge_state"]
        assert err.attempts == outcome["attempts"]
        assert err.request_id is not None
    else:
        result = invoke(c, case["operation"], case["args"])
        assert type(result).__name__ == outcome["model"]
        for name, value in outcome.get("fields", {}).items():
            assert getattr(result, name) == value, name
    assert len(transport.calls) == outcome["attempts"]

    expected = case["expected_request"]
    for rec in transport.calls:
        assert rec.method == expected["method"]
        assert rec.path == expected["path"]
        assert dict(rec.query) == expected["query"]
        if expected["body"] is None:
            assert not rec.body
        else:
            assert rec.json() == expected["body"]
        for name in expected["headers_present"]:
            assert name.lower() in rec.headers, name
        for name in expected["headers_absent"]:
            assert name.lower() not in rec.headers, name


def test_every_appendix_c_message_has_a_fixture() -> None:
    covered = set()
    for case in CASES:
        for reply in case["canned_responses"]:
            body = reply["body"]
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                covered.add(body["error"])
    for _status, message in MESSAGE_TABLE:
        assert message in covered, message
    for message in KNOWN_BAD_REQUEST_MESSAGES:
        assert message in covered, message


def test_every_registered_operation_has_a_fixture() -> None:
    covered = {c["operation"] for c in CASES}
    assert set(rf.ENDPOINTS) <= covered
