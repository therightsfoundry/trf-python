#!/usr/bin/env python3
"""Enforce per-module line coverage on the security- and money-critical modules.

Usage:
    python -m pytest --cov=rightsfoundry --cov-fail-under=95
    python -m coverage json -o coverage.json
    python scripts/check_module_coverage.py coverage.json [--min 95]

``--cov-fail-under`` only checks the total; a well-covered package can hide a
poorly tested transport. This script reads a ``coverage json`` report and fails
unless each module in :data:`CRITICAL` (and the total) reaches the minimum.
Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

#: Modules where an untested branch can leak the key or mis-charge an account.
CRITICAL: tuple[str, ...] = ("_transport.py", "_config.py", "_redact.py", "_retry.py", "_guard.py")
DEFAULT_MINIMUM = 95.0


def module_percentages(report: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for path, data in (report.get("files") or {}).items():
        summary = data.get("summary") or {}
        out[Path(str(path)).as_posix()] = float(summary.get("percent_covered", 0.0))
    return out


def check(report: dict[str, Any], minimum: float) -> list[str]:
    problems: list[str] = []
    percentages = module_percentages(report)
    for module in CRITICAL:
        matches = [pct for path, pct in percentages.items() if path.endswith(f"rightsfoundry/{module}")]
        if not matches:
            problems.append(f"{module}: not in the coverage report")
            continue
        if matches[0] < minimum:
            problems.append(f"{module}: {matches[0]:.1f}% < {minimum:g}%")
    total = float((report.get("totals") or {}).get("percent_covered", 0.0))
    if total < minimum:
        problems.append(f"total: {total:.1f}% < {minimum:g}%")
    return problems


def main(argv: list[str]) -> int:
    args = list(argv)
    minimum = DEFAULT_MINIMUM
    if "--min" in args:
        i = args.index("--min")
        try:
            minimum = float(args[i + 1])
        except (IndexError, ValueError):
            print(__doc__, file=sys.stderr)
            return 2
        del args[i : i + 2]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    report = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    problems = check(report, minimum)
    for module in CRITICAL:
        for path, pct in sorted(module_percentages(report).items()):
            if path.endswith(f"rightsfoundry/{module}"):
                print(f"    {module:<14} {pct:6.1f}%")
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    total = float((report.get("totals") or {}).get("percent_covered", 0.0))
    print(f"ok  total {total:.1f}% and every critical module >= {minimum:g}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
