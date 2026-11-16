## What and why

<!-- What does this change, and why? Link the issue: Fixes #123 -->

## Checklist

- [ ] **No secrets:** no API keys, signed URLs (`…&sig=…`), credentials, internal hostnames or account details anywhere in the diff, tests, fixtures or this description
- [ ] **Zero runtime dependencies:** `dependencies = []` is unchanged and `src/` imports only the standard library
- [ ] **Tests** added or updated (`uv run pytest` passes); bug fixes include a test that fails without the fix
- [ ] **Types:** `uv run mypy --strict src tests scripts` passes
- [ ] **Lint/format:** `uv run ruff check src tests scripts examples` and `uv run ruff format --check src tests scripts examples` pass
- [ ] **Python 3.9 syntax:** `from __future__ import annotations` in new modules; no runtime-evaluated `X | Y` unions
- [ ] **Security invariants** (SEC-01…SEC-20, `docs/security.md`) still hold: single key unwrap, redacted diagnostics, no redirects, no credentials to the asset host, bounded bodies
- [ ] **Money safety:** charged operations declare the right retry class and cost key; no change to spending defaults (or this is marked as breaking)
- [ ] **Sync/async parity:** both clients expose the change
- [ ] **Docs:** `CHANGELOG.md` (Unreleased), and `README.md` / `llms-full.txt` if the public surface changed
- [ ] **Actions** (if touched): pinned to full commit SHAs, minimal `permissions`, `persist-credentials: false`, no `${{ … }}` of untrusted values inside `run:`

## Notes for reviewers

<!-- Anything risky, follow-ups, or how you tested against a fake transport / conformance fixtures. -->
