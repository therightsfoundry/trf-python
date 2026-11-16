# Changelog

All notable changes to `rightsfoundry` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Versioning policy:**

- SDK `1.x` targets API `v1`. Additive API changes (new fields, cost keys or endpoints) ship as **minor** releases, and older SDKs keep working because models tolerate unknown fields.
- Renamed or removed methods, removed options, and **any change to a default that affects spending** (`charged_retry_policy`, `max_retries`, iterator `max_pages`) require a **major** release.
- Dropping a Python version is a minor release, announced at least one minor release in advance.

## [Unreleased]

## [1.0.0] - 2026-11-16

Initial public release of the official Python SDK, CLI and GitHub Action for The Rights Foundry API (`https://docs.therightsfoundry.com/`).

### Added

- **Clients:** `rightsfoundry.Client` (synchronous, thread-safe) and `rightsfoundry.AsyncClient` (asyncio, task-safe) with identical methods, models and errors. `Client.unauthenticated()` and `AsyncClient.unauthenticated()` provide health checks and signed-link downloads without an API key. `with_options()` returns a re-validated copy sharing the connection pool.
- **Per-call `CallOptions`:** Every typed method accepts `timeout=`, `max_retries=`, `idempotency_key=` (`Idempotency-Key` header) and `send_max_credits_header=` (`X-TRF-Max-Credits` header), and charged methods also accept `max_credits=`, individually or bundled as `options=CallOptions(...)`. `client.request()` supports `HEAD`, `204` / empty `2xx` responses, JSON objects, arrays and scalars.
- **Catalogue:** `tracks.list()` (filters, full-text search, sorting; snake_case keywords such as `bpm_min`, `include_children`, `sort_by`), `tracks.get()` with alternate versions (`child_tracks`), and `taxonomies.list()` with optional TTL caching.
- **AI & Acoustic Search:** Deep Search (`tracks.deep_search()`, natural-language briefs) and Acoustic Similarity Search (`tracks.search_similar()`, reference `.mp3`/`.wav` URL validated client-side before sending).
- **Sync Licensing & Downloads:** `tracks.sync()` mints a signed 1-hour link to the WAV master (standard sync, or Export Sync with `external_export=True`), with free reissues inside the window and single-flight coalescing of concurrent calls (preserving per-caller credit caps and timeouts). `sync_links.download()` streams WAV masters and MP3 previews atomically to `0600` files with per-path `Range` resume support and zero credentials sent to the asset host.
- **Credits & Guardrails:** `credits.retrieve()` returns live balance, overdraft, expiring batches and per-account `costs` (with plan discounts applied). Client-side guardrails include `max_credits_per_request`, per-call `max_credits=`, `auto_check_credits` preflight, `min_bandwidth_bytes`, fail-closed `guard_failure_mode`, and edge-triggered `low_credit_warning_threshold` / `on_low_credit` callbacks.
- **Health Probes:** `health.check()`, `.live()`, `.ready()` (default `max_retries=0`, never sending `Authorization`, returning `503` degraded states as typed values).
- **Single-use Auto-pagination:** `tracks.iter()`, `deep_search_iter()` and `search_similar_iter()` (plus `.pages()`), defaulting to 100 items per page and bounded by `max_pages` (50), `max_items` and `max_credits` (`CreditBudgetExceededError`), raising `IteratorConsumedError` on accidental re-iteration.
- **Declarative Models & `FrozenMap`:** Frozen dataclasses decoded declaratively with immutable, hashable, picklable `.raw` (`FrozenMap`) preserving forward-compatible server fields while redacting URL signatures in `repr`/`str`.
- **Typed Error Hierarchy:** Complete hierarchy under `RightsFoundryError` carrying `status`, `request_id`, `operation`, `charge_state` (`not_charged` | `refund_pending` | `charged` | `unknown`), `retry_after` and `attempts`, with status-family-guarded machine `code` support.
- **Executable Endpoint Registry:** Single-sourced `ENDPOINTS` registry (`EndpointSpec.build()`, `LIST_QUERY_PARAMS`, `PUBLIC_COST_KEYS`) verified in CI against the OpenAPI contract (`conformance/openapi_contract.json`) and sync/async parity tests.
- **CLI & GitHub Action:** `rightsfoundry` command-line interface and root `action.yml` (`therightsfoundry/trf-python@v1`) running in `python3 -I` isolated mode with masked secrets, delimiter-safe outputs and redacted step summaries.
- **Security & Supply Chain (SEC-01 to SEC-20):** Zero runtime dependencies, TLS 1.2+ with certificate verification (including `https://` CONNECT proxies), redirect refusal, strict `base_url` allowlist, traceback-safe `Secret` wrapper with key fingerprinting, central redactor, bounded decompression, fork-safe connection pooling, SHA-pinned workflows, PyPI Trusted Publishing (OIDC + PEP 740), GitHub build provenance and CycloneDX SBOM.
- **LLM & Developer Documentation:** `README.md`, `llms.txt`, `llms-full.txt`, `AGENTS.md`, `CITATION.cff`, `codemeta.json`, `docs/` guides and runnable `examples/`.

### Deprecated

- Python 3.9 is past upstream end-of-life (October 2025). It is supported in 1.0 with a one-time `DeprecationWarning`, and support will be removed in a future minor release.

[Unreleased]: https://github.com/therightsfoundry/trf-python/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/therightsfoundry/trf-python/releases/tag/v1.0.0
