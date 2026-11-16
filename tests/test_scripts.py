"""The maintainer scripts catch drift and dependency leaks."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


openapi_sync = load("check_openapi_sync")
zero_deps = load("assert_zero_deps")
sbom = load("generate_sbom")

GOOD_YAML = """
openapi: 3.1.0
paths:
  /v1/tracks:
    get:
      parameters:
{params}
  /v1/tracks/deep-search:
    post:
      summary: x
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [query]
              properties:
                query: {{ type: string, maxLength: 500 }}
                page: {{ type: integer }}
                limit:
                  type: integer
                  description: >-
                    Results per page, 1-100.
                instrumental: {{ type: boolean }}
  /v1/tracks/search:
    post:
      summary: x
      requestBody:
        content:
          application/json:
            schema:
              $ref: "#/components/schemas/SearchBody"
  /v1/taxonomies:
    get:
      summary: x
  /v1/credits:
    get:
      summary: x
  /v1/tracks/{{id}}:
    get:
      parameters:
        - name: id
          in: path
  /v1/tracks/{{id}}/sync:
    post:
      summary: x
      requestBody:
        required: false
        content:
          application/json:
            schema:
              type: object
              properties:
                external_export:
                  type: boolean
                  description: |
                    Export Sync: # not a comment
                    offline use.
components:
  schemas:
    SearchBody:
      type: object
      properties:
        file_url: {{ type: string, format: uri }}
        page: {{ type: integer }}
        limit: {{ type: integer }}
        instrumental: {{ type: boolean }}
"""


def yaml_with(params: list[str], extra: str = "") -> str:
    lines = "\n".join(f"        - name: {p}\n          in: query" for p in params)
    return GOOD_YAML.format(params=lines) + extra


ALL_PARAMS = [p.wire for p in openapi_sync.LIST_QUERY_PARAMS]


def test_openapi_in_sync(tmp_path: Path) -> None:
    path = tmp_path / "openapi.yaml"
    path.write_text(yaml_with(ALL_PARAMS))
    assert openapi_sync.main([str(path)]) == 0


def test_openapi_drift_detected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "openapi.yaml"
    text = yaml_with([*ALL_PARAMS[:-1], "mood2"]).replace("  /v1/credits:\n    get:\n      summary: x\n", "")
    text = text.replace("components:", "  /v1/brand-new:\n    get:\n      summary: x\ncomponents:")
    path.write_text(text)
    assert openapi_sync.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert "mood2" in err and ALL_PARAMS[-1] in err and "/v1/credits" in err and "/v1/brand-new" in err


def test_openapi_json_and_health_promotion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = {
        "paths": {
            "/v1/tracks": {"get": {"parameters": [{"name": p, "in": "query"} for p in ALL_PARAMS]}},
            "/v1/tracks/deep-search": {"post": {}},
            "/v1/tracks/search": {"post": {}},
            "/v1/taxonomies": {"get": {}},
            "/v1/credits": {"get": {}},
            "/v1/tracks/{id}": {"get": {}},
            "/v1/tracks/{id}/sync": {"post": {}},
            "/v1/health": {"get": {}},
        }
    }
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(doc))
    assert openapi_sync.main([str(path)]) == 1
    assert "health.check is now documented" in capsys.readouterr().err


def _wheel(tmp_path: Path, metadata: str, extra: dict[str, bytes] | None = None) -> Path:
    path = tmp_path / "rightsfoundry-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("rightsfoundry/__init__.py", "")
        zf.writestr("rightsfoundry-1.0.0.dist-info/METADATA", metadata)
        zf.writestr(
            "rightsfoundry-1.0.0.dist-info/entry_points.txt",
            "[console_scripts]\nrightsfoundry = rightsfoundry._cli:main\n",
        )
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


META = "Metadata-Version: 2.4\nName: rightsfoundry\nVersion: 1.0.0\nRequires-Python: >=3.9\n"


def test_zero_deps_accepts_clean_wheel(tmp_path: Path) -> None:
    assert zero_deps.main([str(_wheel(tmp_path, META))]) == 0


@pytest.mark.parametrize(
    ("metadata", "extra"),
    [
        (META + "Requires-Dist: httpx\n", None),
        (META + "Provides-Extra: fast\n", None),
        (META, {"rightsfoundry/.env": b"X=1"}),
        (META, {"rightsfoundry/notes.txt": b"x"}),
        (META, {"rightsfoundry/leak.py": b"KEY = '" + b"ab" * 64 + b"'"}),
    ],
)
def test_zero_deps_rejects(tmp_path: Path, metadata: str, extra: dict[str, bytes] | None) -> None:
    assert zero_deps.main([str(_wheel(tmp_path, metadata, extra))]) == 1


def test_zero_deps_rejects_sdist_symlink(tmp_path: Path) -> None:
    path = tmp_path / "rightsfoundry-1.0.0.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        data = META.encode()
        info = tarfile.TarInfo("rightsfoundry-1.0.0/PKG-INFO")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("rightsfoundry-1.0.0/README.md")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    assert zero_deps.main([str(path)]) == 1


def test_sbom_shape(tmp_path: Path) -> None:
    out = tmp_path / "sbom.cdx.json"
    assert sbom.main([str(out)]) == 0
    bom = json.loads(out.read_text())
    assert bom["bomFormat"] == "CycloneDX" and bom["specVersion"] == "1.5"
    assert bom["components"] == [] and bom["metadata"]["component"]["name"] == "rightsfoundry"
    assert bom["serialNumber"].startswith("urn:uuid:")
    assert bom["dependencies"] == [{"ref": "pkg:pypi/rightsfoundry@1.0.0", "dependsOn": []}]


def test_openapi_body_cost_and_schema_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = yaml_with(ALL_PARAMS)
    text = text.replace("                instrumental: { type: boolean }\n", "", 1)  # deep-search body
    text = text.replace(
        "        instrumental: { type: boolean }\n",
        "        instrumental: { type: boolean }\n        seed: { type: integer }\n",
    )
    text += """    CreditBalance:
      type: object
      properties:
        success: { type: boolean }
        as_of: { type: string }
        costs:
          type: object
          properties:
            tracks_list: { type: integer }
            brand_new_rate: { type: integer }
    Track:
      allOf:
        - $ref: "#/components/schemas/TrackBase"
        - type: object
          properties:
            mood_score: { type: number }
    TrackBase:
      type: object
      properties:
        id: { type: integer }
        title: { type: string }
"""
    path = tmp_path / "openapi.yaml"
    path.write_text(text)
    assert openapi_sync.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert "'instrumental' is sent by tracks.deep_search but not documented" in err
    assert "'seed' is documented but not sent by tracks.search_similar" in err
    assert "cost key 'brand_new_rate' is documented" in err and "cost key 'sync' is in PUBLIC_COST_KEYS" in err
    assert "schema Track property 'mood_score' has no field on Track" in err
    assert "'success'" not in err and "'title'" not in err


def test_openapi_contract_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "openapi.yaml"
    source.write_text(yaml_with(ALL_PARAMS))
    contract = tmp_path / "contract.json"
    assert openapi_sync.main([str(source), "--write-contract", str(contract)]) == 0
    data = json.loads(contract.read_text())
    assert data["contract_version"] == 1
    assert data["operations"]["POST /v1/tracks/search"]["body"] == ["file_url", "instrumental", "limit", "page"]
    assert openapi_sync.main([str(contract)]) == 0
    assert openapi_sync.main([str(source), "--write-contract"]) == 2
    assert openapi_sync.main([]) == 2


def test_vendored_openapi_contract_matches_the_sdk(capsys: pytest.CaptureFixture[str]) -> None:
    contract = ROOT / "conformance" / "openapi_contract.json"
    assert openapi_sync.main([str(contract)]) == 0
    out = capsys.readouterr().out
    assert "11 cost keys" in out and "3 response schemas" in out


def test_yaml_subset_parser() -> None:
    doc = openapi_sync.parse_yaml(
        """
a: 1
b: "two # not a comment"   # a comment
c: [x, "y, z", 3]
d: { k: v, n: null, t: true }
e:
  - name: one
    in: query
  - plain
  -
    nested: yes
f: |
  block
  text
g: 1.5
h: 'it''s'
"""
    )
    assert doc["a"] == 1 and doc["b"] == "two # not a comment"
    assert doc["c"] == ["x", "y, z", 3]
    assert doc["d"] == {"k": "v", "n": None, "t": True}
    assert doc["e"] == [{"name": "one", "in": "query"}, "plain", {"nested": "yes"}]
    assert doc["f"] == "block text" and doc["g"] == 1.5 and doc["h"] == "it's"
    assert openapi_sync.parse_yaml("") is None


module_coverage = load("check_module_coverage")


def _coverage_report(pct: dict[str, float], total: float) -> dict[str, object]:
    files = {f"src/rightsfoundry/{name}": {"summary": {"percent_covered": value}} for name, value in pct.items()}
    return {"files": files, "totals": {"percent_covered": total}}


def test_module_coverage_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = dict.fromkeys(module_coverage.CRITICAL, 99.0)
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_coverage_report(good, 97.0)))
    assert module_coverage.main([str(path)]) == 0
    weak = {**good, "_transport.py": 80.0}
    weak.pop("_guard.py")
    path.write_text(json.dumps(_coverage_report(weak, 94.0)))
    assert module_coverage.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert "_transport.py: 80.0%" in err and "_guard.py: not in the coverage report" in err and "total" in err
    assert module_coverage.main([str(path), "--min", "70"]) == 1  # _guard.py is still missing
    assert module_coverage.main([str(path), "--min"]) == 2 and module_coverage.main([]) == 2
