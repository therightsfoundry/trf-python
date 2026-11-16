#!/usr/bin/env python3
"""SEC-01: assert that built artefacts declare zero runtime dependencies and ship no unexpected files.

Usage: python3 scripts/assert_zero_deps.py dist/*.whl dist/*.tar.gz

Fails (exit 1) if any wheel or sdist:
  * declares ``Requires-Dist`` or ``Provides-Extra`` (extras would leak into SBOMs),
  * contains a file outside the allowlist (credentials, caches, VCS data, …),
  * contains a 128-hex-character literal (the shape of a Rights Foundry API key).
Standard library only.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

WHEEL_ALLOWED = [
    re.compile(r"^rightsfoundry/(?:resources/)?[A-Za-z0-9_]+\.py$"),
    re.compile(r"^rightsfoundry/py\.typed$"),
    re.compile(r"^rightsfoundry-[0-9][^/]*\.dist-info/(?:METADATA|WHEEL|RECORD|entry_points\.txt)$"),
    re.compile(r"^rightsfoundry-[0-9][^/]*\.dist-info/licenses/LICENSE$"),
]
#: The sdist carries everything the test suite reads, so ``pytest`` passes in an
#: unpacked sdist: sources, tests, scripts, the conformance data, docs, examples,
#: pinned requirements, the workflows and the top-level documents.
SDIST_ALLOWED = [
    re.compile(r"^src/rightsfoundry/(?:resources/)?[A-Za-z0-9_]+\.py\Z"),
    re.compile(r"^src/rightsfoundry/py\.typed\Z"),
    re.compile(r"^tests/[A-Za-z0-9_]+\.py\Z"),
    re.compile(r"^scripts/[A-Za-z0-9_]+\.py\Z"),
    re.compile(r"^conformance/[A-Za-z0-9_]+\.json\Z"),
    re.compile(r"^docs/[A-Za-z0-9_-]+\.md\Z"),
    re.compile(r"^examples/[A-Za-z0-9_-]+\.(?:py|yml)\Z"),
    re.compile(r"^requirements/[A-Za-z0-9_-]+\.(?:in|txt)\Z"),
    re.compile(r"^\.github/(?:[A-Za-z0-9_-]+/)?[A-Za-z0-9_-]+\.(?:yml|md)\Z"),
    re.compile(r"^\.github/CODEOWNERS\Z"),
    re.compile(
        r"^(?:README\.md|LICENSE|CHANGELOG\.md|SECURITY\.md|AGENTS\.md|CLAUDE\.md|llms\.txt|llms-full\.txt"
        r"|action\.yml|pyproject\.toml|PKG-INFO|\.gitignore)\Z"
    ),
]
FORBIDDEN = re.compile(
    r"(?:^|/)(?:\.env(?:\..*)?|\.creds|\.dev\.vars|\.test\.env|\.git|__pycache__)(?:/|$)|\.(?:pem|key|pyc)$"
)
TOKEN_LITERAL = re.compile(rb"(?<![0-9A-Fa-f])[0-9A-Fa-f]{128}(?![0-9A-Fa-f])")


class Failure(Exception):
    pass


def check_metadata(text: str, where: str) -> None:
    meta = Parser().parsestr(text)
    if meta.get_all("Requires-Dist"):
        raise Failure(f"{where}: declares Requires-Dist {meta.get_all('Requires-Dist')}")
    if meta.get_all("Provides-Extra"):
        raise Failure(f"{where}: declares Provides-Extra {meta.get_all('Provides-Extra')} (extras are forbidden)")
    if meta.get("Name", "").lower() != "rightsfoundry":
        raise Failure(f"{where}: unexpected Name {meta.get('Name')!r}")
    if not (meta.get("Requires-Python") or "").startswith(">=3.9"):
        raise Failure(f"{where}: Requires-Python must be >=3.9")


def check_names(names: list[str], allowed: list[re.Pattern[str]], where: str) -> None:
    for name in names:
        if FORBIDDEN.search(name):
            raise Failure(f"{where}: forbidden file {name}")
        if not any(p.match(name) for p in allowed):
            raise Failure(f"{where}: unexpected file {name}")


def check_content(name: str, data: bytes, where: str) -> None:
    if TOKEN_LITERAL.search(data):
        raise Failure(f"{where}: {name} contains a 128-hex literal (API-key shaped)")


def check_wheel(path: Path) -> int:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        check_names(names, WHEEL_ALLOWED, path.name)
        metadata = [n for n in names if n.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise Failure(f"{path.name}: expected one METADATA file")
        check_metadata(zf.read(metadata[0]).decode("utf-8"), path.name)
        for name in names:
            check_content(name, zf.read(name), path.name)
        entry_points = [n for n in names if n.endswith("entry_points.txt")]
        if not entry_points or b"rightsfoundry._cli:main" not in zf.read(entry_points[0]):
            raise Failure(f"{path.name}: console script rightsfoundry missing")
    return len(names)


def check_sdist(path: Path) -> int:
    with tarfile.open(path, "r:gz") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        for m in tf.getmembers():
            if m.issym() or m.islnk():
                raise Failure(f"{path.name}: contains a link {m.name}")
        roots = {m.name.split("/", 1)[0] for m in members}
        if len(roots) != 1:
            raise Failure(f"{path.name}: expected a single top-level directory, got {sorted(roots)}")
        names = [m.name.split("/", 1)[1] for m in members]
        check_names(names, SDIST_ALLOWED, path.name)
        for member, name in zip(members, names):
            handle = tf.extractfile(member)
            data = handle.read() if handle is not None else b""
            check_content(name, data, path.name)
            if name == "PKG-INFO":
                check_metadata(data.decode("utf-8"), path.name)
    return len(names)


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv]
    if not paths:
        print("usage: assert_zero_deps.py dist/*.whl dist/*.tar.gz", file=sys.stderr)
        return 2
    kinds = set()
    try:
        for path in paths:
            if not path.is_file():
                raise Failure(f"{path}: not found")
            if path.suffix == ".whl":
                count = check_wheel(path)
                kinds.add("wheel")
            elif path.name.endswith(".tar.gz"):
                count = check_sdist(path)
                kinds.add("sdist")
            else:
                raise Failure(f"{path}: not a wheel or sdist")
            print(f"ok  {path.name}: {count} files, zero runtime dependencies")
    except Failure as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    if kinds != {"wheel", "sdist"}:
        print("note: pass both the wheel and the sdist to check both", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
