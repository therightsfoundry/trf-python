# AGENTS.md: instructions for AI coding assistants

This file is for AI coding assistants (Claude Code, Codex, Cursor, Copilot and similar). It has two parts:

- **Part A** covers *using* `rightsfoundry` in someone else's project.
- **Part B** covers *contributing* to this repository (`trf-python`).

The complete API surface of the SDK is in [`llms-full.txt`](llms-full.txt). Read it before you write code against the SDK.

**About the platform.** [The Rights Foundry](https://therightsfoundry.com/) offers royalty-free music for commercial and creative sync: 85,000+ tracks of cleared, human-made music that developers and AI systems can search and license as WAV masters through one API.

| Resource | URL |
|---|---|
| Platform home | https://therightsfoundry.com/ |
| Builder overview | https://therightsfoundry.com/for-builders |
| API keys (create, reveal, key fingerprint) | https://therightsfoundry.com/dashboard/token |
| Licensing terms (authoritative) | https://therightsfoundry.com/licensing |
| API reference | https://docs.therightsfoundry.com/ |
| MCP connector (remote, OAuth) | https://mcp.therightsfoundry.com/mcp |

---

## Part A: using `rightsfoundry` in a customer project

### A1. Non-negotiable rules

1. **The API key never leaves the server.** It is an account-wide secret: one key can spend the whole balance.
   - Read it from the `TRF_API_KEY` environment variable or a secret manager. Never hard-code it, commit it, log it, put it in a prompt, or ship it in browser, mobile or desktop client code.
   - In web apps, the browser gets only `id`, `title`, `artist`, `duration` and the signed preview `asset_url`.
2. **Do not send credentials to the asset host.** Signed URLs on `assets.therightsfoundry.com` carry their own authorisation, and an `Authorization` header there would only leak the key. Use `client.sync_links.download(...)`, which handles this. Do not fetch `download_url` with `requests` plus your own headers.
3. **Store track IDs, not signed URLs.** Signed URLs expire after 1 hour. Persist `track.id` and request a fresh link when you need one.
4. **Do not log signed URLs.** For logging, use `link.redacted_download_url()` and `track.redacted_asset_url()`. A `download_url` is a bearer capability to a licensed WAV master.
5. **Never hard-code prices.** Read `client.credits.retrieve().costs`, which is free. The rate card changes without API version bumps.
6. **Cap spending.** Pass `max_credits=` on charged calls in loops and agents, or set `max_credits_per_request` on the client. Use `max_credits=` on iterators (`tracks.iter`, `deep_search_iter`, `search_similar_iter`).
   - Every typed method also accepts `timeout=`, `max_retries=`, `idempotency_key=` and `send_max_credits_header=`, bundled as the frozen dataclass `rightsfoundry.CallOptions`, which `client.request(..., options=...)` also takes.
7. **`tracks.sync()` is a licence transaction that costs money.** Do not call it speculatively. In agent workflows, get human approval first.
8. **Licensing scope.** Standard sync covers one-time synchronization and use on online third-party platforms without monetization. Use `external_export=True` (Export Sync) for offline use or for delivery and distribution outside the platform. Sync licences do **not** include AI/ML training rights. When a user asks about rights, link to https://therightsfoundry.com/licensing rather than inventing terms.

### A2. Canonical patterns

```python
import rightsfoundry

client = rightsfoundry.Client(max_credits_per_request=100)   # reads TRF_API_KEY once

results = client.tracks.deep_search("uplifting indie pop for a product launch", limit=20, instrumental=True)
track = results.data[0]
link = client.tracks.sync(track.id)                          # charged at costs.sync; free reissue inside 1 h
path = client.sync_links.download(link, "./masters/")        # free; no auth header sent
```

- **Async (FastAPI, aiohttp, etc.):** create one `rightsfoundry.AsyncClient()` per event loop, ideally in the app lifespan, and `await` every method. Use `async for` over iterators, with no `await` before `.iter(...)`.
- **Sync frameworks (Django, Flask, Celery):** use one module-level `rightsfoundry.Client()`. It is thread-safe. Clients cannot be pickled, so create them inside workers rather than passing them in task payloads.
- **Pagination:** `for t in client.tracks.iter(..., max_items=..., max_credits=...)`. Every page is charged, and the default page size of 100 means the fewest charged calls. Iterators are single-use: a second loop over the same iterator raises `rightsfoundry.IteratorConsumedError`. Call `client.tracks.iter(...)` again rather than reusing one.
- **AI search page 1 is charged every time it is sent.** Ask for one large page (for example `limit=50`) rather than re-sending page 1.
- **Acoustic Similarity Search:** `file_url` must be a public `https` `.mp3`/`.wav`. If the provider cannot fetch it, the call is **charged** (`UnreachableFileError`, `charge_state == "charged"`).
- **Health checks** need no key: `rightsfoundry.Client.unauthenticated().health.check()`. They are not retried by default (`max_retries=0`), so a degraded `503` returns `HealthStatus(status="degraded")` at once; pass `max_retries=2` to retry.
- **Models** pickle and `copy.deepcopy` cleanly, so they can go into task payloads and caches (clients cannot). `.raw` is read-only, and its `str()`/`repr()` redact signed-URL signatures.

### A3. Error handling

Catch specific classes. Every error has `.status`, `.request_id`, `.charge_state` and `.retry_after`.

```python
import rightsfoundry as rf

try:
    link = client.tracks.sync(track_id, max_credits=50)
except rf.CreditCapExceededError:          # client-side: nothing sent
    ...
except rf.InsufficientCreditsError:        # 402: tell the user to top up at /dashboard/billing
    ...
except rf.FeatureNotEnabledError:          # 403: the ability is off for this key; contact support
    ...
except (rf.TrackNotFoundError, rf.SyncMasterUnavailableError):   # free 404s
    ...
except rf.APIStatusError as e:
    logger.error("TRF error %s (request_id=%s, charge_state=%s)", e.status, e.request_id, e.charge_state)
    raise
```

- Do not wrap charged calls in your own retry loops. The SDK already retries safely according to the charge-safety matrix (README, *Retries and money safety*), and blind retries can charge twice.
- `idempotency_key=` (printable ASCII without spaces) is sent as `Idempotency-Key` and makes the SDK retry ambiguous outcomes (`408`, `502`, `504`, read-phase failures) on charged calls. The API does not honour the header yet, so until it does such a retry can still charge twice.
- `charge_state == "unknown"` means the call may or may not have been charged. Reconcile with `client.credits.retrieve()` before acting.

### A4. Do not

- Do not call `/v1/tracks/creative-brief` (retired) or any admin endpoint. `client.request()` refuses admin paths.
- Do not pass `dangerously_allow_browser=True`, disable TLS in a custom transport, or add `allow_insecure_localhost=True` outside tests.
- Do not add `requests` or `httpx` just to call this API. The SDK has no runtime dependencies and handles TLS, retries and redaction.
- Do not auto-play previews. Preview streams are intended to be billed per stream (`costs.stream`) from API credits in a future update (metered today, not yet deducted).

---

## Part B: contributing to `trf-python`

### B1. Hard constraints

- **Zero runtime dependencies.** `pyproject.toml` keeps `dependencies = []` and has no optional dependencies. Import only from the standard library under `src/`. Dev tools belong in `[dependency-groups] dev`. CI runs `scripts/assert_zero_deps.py` on the built artefacts.
- **Python 3.9 syntax.** Start every module with `from __future__ import annotations`. Use `Optional[X]` / `Union[X, Y]` anywhere an annotation is evaluated at runtime, such as dataclass fields used by `typing.get_type_hints` or `cast()` arguments. Do not use `match`, `ExceptionGroup`, `typing.Self`, PEP 604 unions evaluated at runtime, or `dataclass(slots=True)` without the version shim in `_models.py`.
- **`mypy --strict` clean**, ruff clean (line length 120, rules in `pyproject.toml`), formatted with `ruff format`.
- **Security invariants SEC-01…SEC-20 must hold** (see [`docs/security.md`](docs/security.md)). In particular:
  - The raw key is unwrapped only in `_pipeline._auth_header_value()` (and in config validation of an explicit `Secret`). Never bind it to a long-lived name, put it in an exception, or log it.
  - Every string that leaves the pipeline (exception messages, log lines, hook events, `body`) passes through `_redact.redact_text` / `redact_value`.
  - Resources never touch the transport. Only `_pipeline.py` (API calls) and `_download.py` (signed asset URLs) send bytes.
  - Never follow redirects. Never send `Authorization`, custom headers or cookies to asset hosts. Never add a `verify=False` path.
  - Response bodies are always bounded, with decompressed bytes counted. JSON is parsed with `pipeline.parse_json` (rejects NaN, depth ≤ 64).
  - No environment variable may enable `http://`, add an allowed host, relax `guard_failure_mode` or weaken TLS.
  - Changing a default that affects money (`charged_retry_policy`, `max_retries`, iterator `max_pages`) is a breaking change.
- **Never commit secrets.** Tests use obviously fake keys generated at runtime (for example `"ab" * 64` built in code). Never write a literal 128-hex string anywhere outside `tests/`, and never point tests at production.

### B2. Module map

| Module | Responsibility |
|---|---|
| `_client.py` | `Client` / `AsyncClient`: construction, resources, `with_options`, `request()` escape hatch |
| `_config.py` | `build_config()`: option resolution (kwarg → env → default) and validation (SEC-02, SEC-08, SEC-14) |
| `_endpoints.py` | **Executable endpoint registry** `ENDPOINTS`: method, path, auth, retry class, cost-key rule, query params, body fields, decoder, `build()` |
| `_params.py` / `_options.py` | `QueryParam`, `BodyField`, `LIST_QUERY_PARAMS` / `CallOptions`, `resolve_options()` |
| `resources/tracks.py`, `resources/misc.py` | Public resource classes (sync + async), single-flight `sync()` |
| `_pipeline.py` | The 13-step request pipeline; the only API sender |
| `_transport.py` | `HTTPTransport` (stdlib pool, TLS, proxy, fork safety), `AsyncExecutorTransport`, transport protocols |
| `_retry.py` | Backoff, `Retry-After`, the charge-safe retry matrix `should_retry()` |
| `_guard.py` | Credit guardrails: rate card, projection, caps, preflight, low-credit callback |
| `_errors.py` | Error hierarchy and the exact message classifier |
| `_models.py` | Frozen response models with `.raw` |
| `_pagination.py` | Lazy, budgeted iterators |
| `_download.py` | Signed asset download (SEC-13, SEC-18) |
| `_secret.py`, `_redact.py` | Key wrapper + fingerprint, central redactor |
| `_validation.py` | Track IDs, query params, `file_url`, `request()` paths |
| `_cli.py`, `_action.py` | CLI and GitHub Action entry points |

### B3. The 13-step pipeline (`_pipeline.py`)

1. validate inputs (`EndpointSpec.build`) → 2. build URL (validated base + constant path + allowlisted query) → 3. credit guard (cap / preflight; may fetch the free rate card) → 4. attach headers (auth, UA, custom) → 5. `on_request` hook (redacted) → 6. `transport.send` (timeouts, no redirects) → 7. bounded body read → 8. classify (success / typed error / retryable) → 9. retry loop (class-gated, jittered, `Retry-After`-aware) → 10. `on_response` / `on_error` hooks → 11. decode JSON → 12. build model → 13. credit observations (low-credit callback).

All decisions live in `PipelineCore` as pure, synchronous methods. `SyncPipeline` and `AsyncPipeline` differ only in how they wait and do I/O. Keep it that way, so the two clients cannot drift apart.

### B4. Adding an endpoint (4 steps)

1. **Registry:** add an `EndpointSpec` to `_SPECS` in `_endpoints.py`: `operation`, `method`, `path`, `auth` (`"bearer"` / `"none"`), `retry_class` (`"charged"` / `"sync"` / `"free"`), a `cost_key` rule returning the public cost key the server will charge, any `query_params` (`QueryParam`) and `body` fields (`BodyField`), a `decoder` and the `response_model` name. New cost key → add it to `PUBLIC_COST_KEYS`. The spec is executable: `spec.build(track_id=..., query=..., body=..., options=...)` validates the arguments and returns the `OperationSpec` the pipeline sends.
2. **Model:** add a frozen `@model` dataclass in `_models.py`. Declare its fields; scalar fields decode from their annotation (`Optional[str]`, `tuple[str, ...]`, …) through `decode_model`, so only nested models need passing explicitly. Use `field(metadata=STRICT)` for integers that must be present, `json_key=` for JSON names that are not Python names. Keep `.raw`, keep enums open (`str`) and export the model from `__init__.py`.
3. **Resource:** add a sync **and** an async method that call `ENDPOINTS[...].build(...)` and `pipeline.execute(op)`. Every method takes the per-call options (`timeout=`, `max_retries=`, `idempotency_key=`, `send_max_credits_header=`, `options=CallOptions(...)`), merged with `resolve_options`; charged methods also take `max_credits=`. `tests/test_architecture_parity.py` fails if the pair drifts.
4. **Verify:** add unit tests and conformance fixtures, regenerate the contract with `python3 scripts/check_openapi_sync.py path/to/openapi.yaml --write-contract conformance/openapi_contract.json`, then run the full suite below. Update `README.md`, `llms-full.txt` and `CHANGELOG.md`.

### B5. Verification commands (all must pass before a PR)

```bash
uv run pytest
uv run mypy --strict src tests scripts
uv run ruff check src tests scripts examples
uv run ruff format --check src tests scripts examples
uv run pytest --cov=rightsfoundry --cov-report=term-missing --cov-fail-under=95
uv run coverage json -o coverage.json && uv run python scripts/check_module_coverage.py coverage.json
uv build
python3 scripts/assert_zero_deps.py dist/*.whl dist/*.tar.gz
python3 scripts/check_openapi_sync.py conformance/openapi_contract.json
python3 scripts/generate_sbom.py
```

Tests run against in-process fakes and the conformance fixtures. CI never makes charged calls against production. CI also requires ≥ 95 % line coverage overall and in each of `_transport`, `_config`, `_redact`, `_retry` and `_guard`.

CI and release install tooling from hash-pinned files in `requirements/`. After changing `requirements/*.in`, regenerate them:

```bash
uv pip compile --generate-hashes --universal --python-version 3.12 requirements/dev.in -o requirements/dev.txt
uv pip compile --generate-hashes --universal --python-version 3.12 requirements/build.in -o requirements/build.txt
```

### B6. Style

- Use "The Rights Foundry" in prose, snake_case Python names, and wire names in code font (`bpmMin`).
- Docstrings say whether a method is **Charged** or free and name its cost key.
- Error messages name the option or parameter and the rule. They never echo a secret or a signed URL.
