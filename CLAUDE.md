# CLAUDE.md

Instructions for Claude Code in this repository. The full guide is [`AGENTS.md`](AGENTS.md): read it first. The complete SDK reference is in [`llms-full.txt`](llms-full.txt).

## What this repo is

`trf-python` is the official Python SDK (PyPI `rightsfoundry`) for The Rights Foundry API: programmable sync licensing across 85,000+ cleared, human-made tracks. It has zero runtime dependencies, supports Python 3.9–3.14, and passes `mypy --strict`. This repository is **public**.

## Rules

- **No secrets, ever.** Do not write API keys, connection strings, internal hostnames, IPs, account IDs or emails other than `info@` and `security@therightsfoundry.com`. Never write a 128-hex string outside `tests/`, where fake keys are built at runtime.
- **Zero runtime dependencies.** Under `src/`, use the standard library only. `dependencies = []` stays empty.
- **Python 3.9 syntax** with `from __future__ import annotations` in every module.
- **Keep the security invariants** (SEC-01…SEC-20, [`docs/security.md`](docs/security.md)): one key unwrap point, a redactor on every diagnostic string, no redirects, no credentials to the asset host, bounded bodies, and no env var that weakens TLS or the host allowlist.
- **Money safety.** Do not change retry, guard or pagination defaults without treating it as a breaking change. Do not make charged calls against production in tests or CI.
- **New endpoints** go through the declarative registry in `src/rightsfoundry/_endpoints.py`, using the 4-step recipe in AGENTS.md §B4.

## Before you finish

```bash
uv run pytest
uv run mypy --strict src tests scripts
uv run ruff check src tests scripts examples
uv run ruff format --check src tests scripts examples
uv build && python3 scripts/assert_zero_deps.py dist/*.whl dist/*.tar.gz
```

Update `README.md`, `llms-full.txt` and `CHANGELOG.md` when the public surface changes.

## When helping someone *use* the SDK

Follow AGENTS.md Part A:

- Keep `TRF_API_KEY` on the server.
- Cap spending with `max_credits`.
- Get human approval before `tracks.sync()`.
- Store track IDs, not signed URLs.
- Log only `redacted_download_url()` / `redacted_asset_url()`.
- Never hard-code prices; read `client.credits.retrieve().costs`.
