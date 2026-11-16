"""Repository hygiene for a public repo: examples compile, no key-shaped literals, pinned actions."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = sorted((ROOT / "examples").glob("*.py"))
SKIP_DIRS = {".venv", "dist", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__", "build"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".yml", ".yaml", ".toml", ".json", ".cff", ""}
HEX128 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{128}(?![0-9A-Fa-f])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@therightsfoundry\.com")


def repo_files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS) and path.suffix in TEXT_SUFFIXES:
            out.append(path)
    return out


@pytest.mark.parametrize("path", EXAMPLES, ids=[p.name for p in EXAMPLES])
def test_examples_compile(path: Path) -> None:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")


def test_no_key_shaped_literals_or_private_emails() -> None:
    for path in repo_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not HEX128.search(text), f"128-hex literal in {path.relative_to(ROOT)}"
        for email in EMAIL.findall(text):
            assert email in ("info@therightsfoundry.com", "security@therightsfoundry.com"), (path, email)


def test_workflow_actions_are_pinned_to_full_shas() -> None:
    workflows = [*(ROOT / ".github" / "workflows").glob("*.yml"), ROOT / "examples" / "github_workflow_soundtrack.yml"]
    for wf in workflows:
        if not wf.exists():
            continue
        for line in wf.read_text(encoding="utf-8").splitlines():
            match = re.search(r"uses:\s*([^\s#]+)", line)
            if not match:
                continue
            ref = match.group(1)
            if ref.startswith(("./", "therightsfoundry/trf-python")):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), f"{wf.name}: {ref} is not pinned to a SHA"
