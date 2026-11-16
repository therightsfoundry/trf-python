#!/usr/bin/env python3
"""Generate a CycloneDX 1.5 JSON SBOM for the ``rightsfoundry`` package → ``dist/sbom.cdx.json``.

The SDK has zero runtime dependencies, so ``components`` is empty by
construction; the script refuses to write an SBOM if ``pyproject.toml`` ever
declares a dependency or extra. Artefacts already in ``dist/`` are listed with
their SHA-256. ``SOURCE_DATE_EPOCH`` makes the timestamp reproducible.
Standard library only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def read_version() -> str:
    text = (ROOT / "src" / "rightsfoundry" / "_version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:
        raise SystemExit("cannot read __version__")
    return match.group(1)


def assert_no_dependencies() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    if deps is None or deps.group(1).strip():
        raise SystemExit("pyproject.toml must declare `dependencies = []`")
    if re.search(r"^\[project\.optional-dependencies\]", text, re.MULTILINE):
        raise SystemExit("pyproject.toml must not declare [project.optional-dependencies]")


def timestamp() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = (
        dt.datetime.fromtimestamp(int(epoch), tz=dt.timezone.utc)
        if epoch and epoch.isdigit()
        else dt.datetime.now(dt.timezone.utc)
    )
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def artefact_properties(dist: Path) -> list[dict[str, str]]:
    props = []
    for path in sorted(dist.glob("rightsfoundry-*")):
        if path.suffix in (".whl", ".gz"):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            props.append({"name": f"artifact:sha256:{path.name}", "value": digest})
    return props


def build(version: str, dist: Path) -> dict[str, Any]:
    purl = f"pkg:pypi/rightsfoundry@{version}"
    component: dict[str, Any] = {
        "type": "library",
        "bom-ref": purl,
        "name": "rightsfoundry",
        "version": version,
        "description": "Official Python SDK for The Rights Foundry music licensing API (zero runtime dependencies).",
        "purl": purl,
        "licenses": [{"license": {"id": "MIT"}}],
        "supplier": {"name": "The Rights Foundry", "url": ["https://therightsfoundry.com"]},
        "externalReferences": [
            {"type": "website", "url": "https://therightsfoundry.com"},
            {"type": "documentation", "url": "https://docs.therightsfoundry.com"},
            {"type": "vcs", "url": "https://github.com/therightsfoundry/trf-python"},
            {"type": "distribution", "url": "https://pypi.org/project/rightsfoundry/"},
            {"type": "issue-tracker", "url": "https://github.com/therightsfoundry/trf-python/issues"},
        ],
    }
    props = artefact_properties(dist)
    if props:
        component["properties"] = props
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, purl)}",
        "version": 1,
        "metadata": {
            "timestamp": timestamp(),
            "tools": {
                "components": [
                    {"type": "application", "name": "trf-python/scripts/generate_sbom.py", "version": version}
                ]
            },
            "component": component,
        },
        "components": [],
        "dependencies": [{"ref": purl, "dependsOn": []}],
    }


def main(argv: list[str]) -> int:
    out = Path(argv[0]) if argv else ROOT / "dist" / "sbom.cdx.json"
    assert_no_dependencies()
    version = read_version()
    out.parent.mkdir(parents=True, exist_ok=True)
    bom = build(version, ROOT / "dist")
    out.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
    print(f"ok  wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out} (CycloneDX 1.5, 0 components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
