#!/usr/bin/env python3
"""Verify that the SDK's endpoint registry and models match the published API contract.

Usage:
    python3 scripts/check_openapi_sync.py path/to/openapi.yaml
    python3 scripts/check_openapi_sync.py path/to/openapi.json
    python3 scripts/check_openapi_sync.py https://docs.therightsfoundry.com/openapi.yaml
    python3 scripts/check_openapi_sync.py conformance/openapi_contract.json
    python3 scripts/check_openapi_sync.py path/to/openapi.yaml --write-contract conformance/openapi_contract.json

Checked, in both directions unless noted:

* every public ``/v1/*`` path + method against ``ENDPOINTS`` (operations marked
  ``in_openapi=False`` — the health probes — are reported if the document starts
  listing them, so the flag can be flipped);
* every ``GET /v1/tracks`` query parameter against ``LIST_QUERY_PARAMS``;
* every JSON request-body property of ``deep-search``, ``search`` and ``sync``
  against the registry's ``BodyField`` specs;
* the ``costs`` keys of ``GET /v1/credits`` against ``PUBLIC_COST_KEYS``;
* every documented property of the ``Track``, ``SyncLink`` and ``CreditBalance``
  schemas exists on the SDK model (``Track``, ``SyncLink``, ``Credits``). Model
  fields the document does not list are reported as notes, not failures.

The input may be a full OpenAPI document (YAML or JSON) or the small contract
snapshot that ``--write-contract`` produces; CI checks the vendored snapshot on
every pull request. Standard library only: YAML is read by a small parser for
the subset OpenAPI files use (block mappings and lists, flow lists and maps,
quoted and block scalars).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from dataclasses import fields
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rightsfoundry._endpoints import ENDPOINTS, LIST_QUERY_PARAMS, PUBLIC_COST_KEYS
from rightsfoundry._models import Credits, SyncLink, Track

METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
CONTRACT_VERSION = 1

#: OpenAPI schema → SDK model, plus documented wrapper properties the model does not carry.
SCHEMA_MODELS: dict[str, tuple[type[Any], frozenset[str]]] = {
    "Track": (Track, frozenset()),
    "SyncLink": (SyncLink, frozenset()),
    "CreditBalance": (Credits, frozenset({"success"})),
}

Contract = dict[str, Any]


# --- a YAML subset parser --------------------------------------------------------------


class _Line:
    __slots__ = ("indent", "text")

    def __init__(self, indent: int, text: str) -> None:
        self.indent = indent
        self.text = text


def _strip_comment(text: str) -> str:
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'" and (i == 0 or text[i - 1] in " :[{,"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] == " "):
            return text[:i].rstrip()
    return text


def _split_top(text: str, sep: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _split_key(text: str) -> tuple[str, str] | None:
    """``key: value`` → ``(key, value)``; ``None`` when the text is not a mapping entry."""
    if text[:1] in "\"'":
        end = text.find(text[0], 1)
        if end > 0 and text[end + 1 : end + 2] == ":":
            return text[1:end], text[end + 2 :].strip()
        return None
    match = re.match(r"^([^\s:\[\]{},][^:]*?):(?:\s+(.*))?$", text)
    if match is None:
        return None
    return match.group(1).strip(), (match.group(2) or "").strip()


def _scalar(text: str) -> Any:
    if not text:
        return None
    if text[0] == '"':
        try:
            return json.loads(text)
        except ValueError:
            return text.strip('"')
    if text[0] == "'":
        return text[1:-1].replace("''", "'")
    if text[0] == "[" and text.endswith("]"):
        return [_scalar(p) for p in _split_top(text[1:-1], ",")]
    if text[0] == "{" and text.endswith("}"):
        out: dict[str, Any] = {}
        for part in _split_top(text[1:-1], ","):
            kv = _split_key(part)
            if kv is not None:
                out[kv[0]] = _scalar(kv[1])
        return out
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?[0-9]+", text):
        return int(text)
    if re.fullmatch(r"-?[0-9]*\.[0-9]+(?:[eE][-+]?[0-9]+)?", text):
        return float(text)
    return text


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines: list[_Line] = []
        raw_lines = text.splitlines()
        i = 0
        while i < len(raw_lines):
            raw = raw_lines[i]
            i += 1
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
                continue
            indent = len(raw) - len(raw.lstrip(" "))
            text = _strip_comment(stripped)
            if re.search(r"(?::|^-)\s+[|>][+-]?$", text) or text in ("|", ">", "|-", ">-"):
                # block scalar: keep the header, swallow the more-indented body
                body: list[str] = []
                while i < len(raw_lines):
                    nxt = raw_lines[i]
                    if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= indent:
                        break
                    body.append(nxt.strip())
                    i += 1
                head = re.sub(r"[|>][+-]?$", "", text).rstrip()
                self.lines.append(_Line(indent, f"{head} {json.dumps(' '.join(body).strip())}".strip()))
                continue
            self.lines.append(_Line(indent, text))
        self.pos = 0

    def parse(self) -> Any:
        if not self.lines:
            return None
        return self._block(self.lines[0].indent)

    def _block(self, indent: int) -> Any:
        if self.lines[self.pos].text.startswith("- ") or self.lines[self.pos].text == "-":
            return self._sequence(indent)
        return self._mapping(indent)

    def _mapping(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent < indent or (line.indent == indent and line.text.startswith("- ")):
                break
            if line.indent > indent:  # continuation of a multi-line plain scalar
                self.pos += 1
                continue
            kv = _split_key(line.text)
            self.pos += 1
            if kv is None:
                continue
            key, value = kv
            if value:
                out[key] = _scalar(value)
            elif self.pos < len(self.lines) and (
                self.lines[self.pos].indent > indent
                or (self.lines[self.pos].indent == indent and self.lines[self.pos].text.startswith("- "))
            ):
                out[key] = self._block(self.lines[self.pos].indent)
            else:
                out[key] = None
        return out

    def _sequence(self, indent: int) -> list[Any]:
        out: list[Any] = []
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent != indent or not (line.text.startswith("- ") or line.text == "-"):
                break
            rest = line.text[2:].strip() if line.text != "-" else ""
            if not rest:
                self.pos += 1
                out.append(self._block(self.lines[self.pos].indent) if self.pos < len(self.lines) else None)
            elif _split_key(rest) is not None and not rest.startswith(("[", "{", '"', "'")):
                # "- key: value" opens a mapping whose keys sit two columns in
                self.lines[self.pos] = _Line(indent + 2, rest)
                out.append(self._mapping(indent + 2))
            else:
                self.pos += 1
                out.append(_scalar(rest))
        return out


def parse_yaml(text: str) -> Any:
    return _Parser(text).parse()


# --- contract extraction ---------------------------------------------------------------


def _resolve(doc: dict[str, Any], node: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(node, dict) or depth > 20:
        return {}
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target: Any = doc
        for part in ref[2:].split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return _resolve(doc, target, depth + 1)
    merged = dict(node)
    for key in ("allOf",):
        for sub in node.get(key) or []:
            resolved = _resolve(doc, sub, depth + 1)
            props = {**(resolved.get("properties") or {}), **(merged.get("properties") or {})}
            merged = {**resolved, **merged, "properties": props}
    return merged


def _property_names(doc: dict[str, Any], schema: Any) -> list[str]:
    return sorted(str(k) for k in (_resolve(doc, schema).get("properties") or {}))


def _find_costs(doc: dict[str, Any]) -> list[str]:
    schema = ((doc.get("components") or {}).get("schemas") or {}).get("CreditBalance")
    costs = (_resolve(doc, schema).get("properties") or {}).get("costs")
    return _property_names(doc, costs)


def contract_from_openapi(doc: dict[str, Any]) -> Contract:
    operations: dict[str, dict[str, list[str]]] = {}
    for path, item in (doc.get("paths") or {}).items():
        if not isinstance(item, dict) or not str(path).startswith("/v1/"):
            continue
        shared = item.get("parameters") or []
        for method, op in item.items():
            if method not in METHODS or not isinstance(op, dict):
                continue
            params = [_resolve(doc, p) for p in [*shared, *(op.get("parameters") or [])] if isinstance(p, dict)]
            query = sorted({str(p.get("name")) for p in params if p.get("in") == "query"})
            body_schema = (
                (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
                if isinstance(op.get("requestBody"), dict)
                else None
            )
            operations[f"{method.upper()} {path}"] = {"query": query, "body": _property_names(doc, body_schema)}
    schemas = (doc.get("components") or {}).get("schemas") or {}
    return {
        "contract_version": CONTRACT_VERSION,
        "source": "openapi " + str((doc.get("info") or {}).get("version", "")),
        "operations": dict(sorted(operations.items())),
        "cost_keys": _find_costs(doc),
        "schemas": {name: _property_names(doc, schemas.get(name)) for name in SCHEMA_MODELS if name in schemas},
    }


def load_text(source: str) -> str:
    if source.startswith(("http://", "https://")):
        if not source.startswith("https://"):
            raise SystemExit("refusing to fetch an OpenAPI document over plain http")
        request = urllib.request.Request(source, headers={"User-Agent": "rightsfoundry-openapi-sync"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return str(response.read(20 * 1024 * 1024).decode("utf-8"))
    return Path(source).read_text(encoding="utf-8")


def load_contract(source: str) -> Contract:
    text = load_text(source)
    doc = json.loads(text) if text.lstrip().startswith("{") else parse_yaml(text)
    if not isinstance(doc, dict):
        raise SystemExit(f"{source}: not an OpenAPI document or contract")
    if "operations" in doc and "contract_version" in doc:
        return doc
    return contract_from_openapi(doc)


# --- comparison ------------------------------------------------------------------------


def _model_json_names(model: type[Any]) -> set[str]:
    return {str(f.metadata.get("json_key", f.name)) for f in fields(model)} - {"raw"}


def compare(contract: Contract) -> tuple[list[str], list[str]]:
    """Return ``(problems, notes)``."""
    problems: list[str] = []
    notes: list[str] = []
    documented: dict[str, dict[str, list[str]]] = contract.get("operations", {})
    registry = {f"{spec.method} {spec.path}": spec for spec in ENDPOINTS.values()}

    for key in sorted(documented):
        spec = registry.get(key)
        if spec is None:
            problems.append(f"documented but not in the SDK registry: {key}")
        elif not spec.in_openapi:
            problems.append(f"{spec.operation} is now documented: set in_openapi=True in _endpoints.py")
    for key, spec in sorted(registry.items()):
        if spec.in_openapi and key not in documented:
            problems.append(f"in the SDK registry but not documented: {key} ({spec.operation})")

    list_key = "GET /v1/tracks"
    if list_key in documented:
        doc_params = set(documented[list_key].get("query", []))
        sdk_params = {p.wire for p in LIST_QUERY_PARAMS}
        for name in sorted(doc_params - sdk_params):
            problems.append(f"GET /v1/tracks query parameter {name!r} is documented but not accepted by tracks.list")
        for name in sorted(sdk_params - doc_params):
            problems.append(f"GET /v1/tracks query parameter {name!r} is accepted by tracks.list but not documented")

    for key, spec in sorted(registry.items()):
        if not spec.in_openapi or key not in documented:
            continue
        doc_body = set(documented[key].get("body", []))
        sdk_body = set(spec.body_fields)
        for name in sorted(doc_body - sdk_body):
            problems.append(f"{key} body property {name!r} is documented but not sent by {spec.operation}")
        for name in sorted(sdk_body - doc_body):
            problems.append(f"{key} body property {name!r} is sent by {spec.operation} but not documented")

    cost_keys = contract.get("cost_keys")
    if cost_keys:
        for name in sorted(set(cost_keys) - set(PUBLIC_COST_KEYS)):
            problems.append(f"cost key {name!r} is documented but missing from PUBLIC_COST_KEYS")
        for name in sorted(set(PUBLIC_COST_KEYS) - set(cost_keys)):
            problems.append(f"cost key {name!r} is in PUBLIC_COST_KEYS but not documented")

    for schema_name, props in sorted((contract.get("schemas") or {}).items()):
        if schema_name not in SCHEMA_MODELS:
            continue
        model, wrapper = SCHEMA_MODELS[schema_name]
        names = _model_json_names(model)
        for prop in sorted(set(props) - names - wrapper):
            problems.append(f"schema {schema_name} property {prop!r} has no field on {model.__name__}")
        extra = sorted(names - set(props) - {"request_id", "retried_after_refundable_failure"})
        if extra:
            notes.append(f"{model.__name__} fields not in schema {schema_name}: {', '.join(extra)}")
    return problems, notes


def main(argv: list[str]) -> int:
    args = list(argv)
    out_path: str | None = None
    if "--write-contract" in args:
        i = args.index("--write-contract")
        if i + 1 >= len(args):
            print(__doc__, file=sys.stderr)
            return 2
        out_path = args[i + 1]
        del args[i : i + 2]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    contract = load_contract(args[0])
    if out_path is not None:
        Path(out_path).write_text(json.dumps(contract, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        print(f"wrote {out_path}")
    problems, notes = compare(contract)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    registry = [spec for spec in ENDPOINTS.values() if spec.in_openapi]
    print(
        f"ok  {len(registry)} documented operations, "
        f"{len(contract['operations'].get('GET /v1/tracks', {}).get('query', []))} GET /v1/tracks query parameters, "
        f"request bodies, {len(contract.get('cost_keys') or [])} cost keys and "
        f"{len(contract.get('schemas') or {})} response schemas match"
    )
    undocumented = sorted(spec.operation for spec in ENDPOINTS.values() if not spec.in_openapi)
    if undocumented:
        print(f"    SDK-only (not yet in OpenAPI): {', '.join(undocumented)}")
    for note in notes:
        print(f"    note: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
