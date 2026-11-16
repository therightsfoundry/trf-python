# rightsfoundry: the official Python SDK for The Rights Foundry

**The Rights Foundry** (https://therightsfoundry.com/) is music rights infrastructure operated by AudioSocket: royalty-free music for commercial and creative sync, with programmable licensing across **85,000+ tracks of cleared, human-made music** (61,784 original recordings plus instrumental and alternate versions). Developers and AI systems search the catalog, stream signed MP3 previews and license broadcast-quality WAV masters in one API call. Calls are paid from a prepaid fiat credit balance, topped up directly or through an optional monthly plan.

`rightsfoundry` is the official Python client for that API. It is typed, has zero runtime dependencies, and is built so that a bug in your code does not spend credits you did not mean to spend.

[![Python 3.9–3.14](https://img.shields.io/badge/python-3.9%E2%80%933.14-3776AB?logo=python&logoColor=white)](https://pypi.org/project/rightsfoundry/)
[![Zero dependencies](https://img.shields.io/badge/runtime%20dependencies-0-success)](https://github.com/therightsfoundry/trf-python/blob/main/pyproject.toml)
[![mypy --strict](https://img.shields.io/badge/mypy-strict-2A6DB2)](https://github.com/therightsfoundry/trf-python/blob/main/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/therightsfoundry/trf-python/blob/main/LICENSE)
[![API Docs](https://img.shields.io/badge/API%20docs-docs.therightsfoundry.com-6E40C9)](https://docs.therightsfoundry.com)
[![Platform](https://img.shields.io/badge/platform-therightsfoundry.com-111111)](https://therightsfoundry.com)

**[Website](https://therightsfoundry.com/)** · **[API Reference](https://docs.therightsfoundry.com/)** · **[Get an API Key](https://therightsfoundry.com/dashboard/token)** · **[Claim Free Beta Credits](https://therightsfoundry.com/register)** · **[For Builders](https://therightsfoundry.com/for-builders)** · **[Licensing Terms](https://therightsfoundry.com/licensing)** · **[MCP Connector](https://mcp.therightsfoundry.com/mcp)**

---

## Contents

- [Why rightsfoundry?](#why-rightsfoundry)
- [Install](#install)
- [60-second quickstart](#60-second-quickstart)
- [Three ways to search](#three-ways-to-search)
- [Understanding billing: sync vs stream](#understanding-billing-sync-vs-stream)
- [Credit guardrails, budgets and auto-pagination](#credit-guardrails-budgets-and-auto-pagination)
- [Configuration](#configuration)
- [Per-call options](#per-call-options)
- [AsyncIO and web backends](#asyncio-and-web-backends)
- [Typed error handling and charge state](#typed-error-handling-and-charge-state)
- [Retries and money safety](#retries-and-money-safety)
- [Hooks and logging](#hooks-and-logging)
- [Command line](#command-line)
- [GitHub Action](#github-action)
- [Building AI agents?](#building-ai-agents)
- [Security and supply chain](#security-and-supply-chain)
- [FAQ](#faq)

---

## Why rightsfoundry?

- **License in one call.** `client.tracks.sync(track_id)` returns a signed, 1-hour link to the track's WAV master, and that call is the licence transaction. You do not open a ticket or wait for an approval email. An AI agent can do the same through the [MCP connector](#building-ai-agents).
- **Prepaid fiat credits.** You top up a balance and every charged call draws from it. Your live prices come from `GET /v1/credits`, and the SDK reads them from there. It never hard-codes a price.
- **Reissues inside the 1-hour sync window are free.** If you ask again for the same track and mode while the link is valid, you get `credits_charged == 0`, `reissued is True` and the original expiry. Concurrent `sync()` calls for the same track on one client are merged into a single HTTP request.
- **Zero runtime dependencies.** The SDK uses only the standard library (`http.client`, `ssl`, `json`, `zlib`, `asyncio`, `hashlib`). The CI build fails if the built wheel or sdist declares a dependency.
- **Money-safe defaults.** Charged calls are not retried when the outcome is ambiguous. Each call can carry a credit cap that is checked before anything is sent. Iterators accept a budget. Every error tells you whether the call was charged (`charge_state`).
- **Secure by construction.** The SDK requires TLS 1.2+ with certificate verification, never follows redirects, reads response bodies up to a size limit, and validates `base_url` against an allowlist. The API key is wrapped in a type that refuses to print or pickle it, and logs show only a fingerprint (`[REDACTED fp=1a2b3c4d]`) that matches the one on [/dashboard/token](https://therightsfoundry.com/dashboard/token).
- **Sync and async clients.** `Client` is thread-safe and `AsyncClient` is task-safe. They expose the same methods, models and errors, and iterators work with both `for` and `async for`.
- **Fully typed.** The package ships `py.typed` and passes `mypy --strict`. Responses decode into frozen dataclasses that keep the original JSON in `.raw`, so fields the API adds later are still available. Models pickle and `copy.deepcopy` cleanly, so they can travel through `multiprocessing`, Celery or a cache.

## Install

```bash
pip install rightsfoundry
# or
uv add rightsfoundry
```

Python 3.9–3.14 (CPython). Python 3.9 is past upstream end-of-life, so the SDK emits a one-time `DeprecationWarning` on 3.9.

Create an API key at [therightsfoundry.com/dashboard/token](https://therightsfoundry.com/dashboard/token). New accounts can [claim free beta credits](https://therightsfoundry.com/register): promotional welcome credits, granted when you save a payment method on your account (not at registration). If you use them up inside the promotion window, the account moves to the Starter Beta monthly plan unless you cancel that transition from your billing dashboard. See current plan and auto-conversion terms on [therightsfoundry.com](https://therightsfoundry.com/). Keep the key in your environment or a secret manager:

```bash
export TRF_API_KEY="…"   # paste your key; never commit it
```

## 60-second quickstart

The path from a creative brief to a licensed WAV master:

```python
import rightsfoundry

client = rightsfoundry.Client()  # reads TRF_API_KEY from the environment, once

# 1. Describe what you need in plain language. Charged at costs.deep_search_first_page.
results = client.tracks.deep_search(
    "warm hopeful acoustic guitar for a nature documentary",
    limit=10,
    instrumental=True,
)
if not results.data:
    raise SystemExit("No matches; try a broader brief")
track = results.data[0]
print(track.id, track.title, "by", track.artist, f"{track.duration}s")

# 2. License it. Returns a signed 1-hour link to the WAV master, charged at costs.sync.
link = client.tracks.sync(track.id)
print("charged:", link.credits_charged, "reissued:", link.reissued, "expires:", link.expires_at)

# 3. Download the master. This is free and atomic, and no Authorization header goes to the asset host.
path = client.sync_links.download(link, "./masters/")
print("saved", path)
```

This spends one `deep_search_first_page` and one `sync`; the download is free. A runnable version is in [`examples/01_quickstart_brief_to_wav.py`](https://github.com/therightsfoundry/trf-python/blob/main/examples/01_quickstart_brief_to_wav.py).

> **Store track IDs, not signed URLs.** Every response signs its URLs afresh. A stored `asset_url` or `download_url` stops working after an hour. Keep `track.id` and fetch a new link when you need one.

## Three ways to search

| Mode | SDK method | HTTP | Use it when | Charged at (`costs` key) |
|---|---|---|---|---|
| **Filter** | `client.tracks.list(...)` / `.iter(...)` | `GET /v1/tracks` | You know which fields to constrain: genre, mood, instrument, BPM, duration, key, vocals, or full-text `search` | `tracks_list`; with a non-empty `search`, page 1 at `tracks_search_first_page` and later pages at `tracks_search_next_page` |
| **Deep Search** | `client.tracks.deep_search(query, ...)` / `.deep_search_iter(...)` | `POST /v1/tracks/deep-search` | You have a natural-language brief ("tense heist build-up, no vocals") | page 1 at `deep_search_first_page`, later pages at `deep_search_next_page`, the next-page rate from `GET /v1/credits` |
| **Acoustic Similarity Search** | `client.tracks.search_similar(file_url, ...)` / `.search_similar_iter(...)` | `POST /v1/tracks/search` | You have a reference recording or temp track (public `.mp3`/`.wav`) | page 1 at `file_search_first_page`, later pages at `file_search_next_page`, the next-page rate from `GET /v1/credits` |

```python
# Filter: deterministic, exact tag matches, stable pagination.
page = client.tracks.list(
    mood="uplifting",
    vocals="instrumental",
    bpm_min=90,
    bpm_max=120,          # sent as bpmMin / bpmMax
    duration_max=150,     # seconds
    sort_by="bpm",
    sort_order="asc",
    limit=20,
)
for t in page:
    print(t.id, t.title, t.bpm, t.genres)

# Full-text search adds match metadata.
page = client.tracks.list(search="dark epic orchestral", limit=20)
print(page.total, page.relaxed, [c.to for c in page.corrections])

# Deep Search. Page 1 is charged every time it is sent, so ask for a large page once.
brief = client.tracks.deep_search("tense heist build-up with pizzicato strings", limit=50)

# Acoustic Similarity Search. An unreachable file is CHARGED, so the SDK validates the URL first.
similar = client.tracks.search_similar("https://cdn.example.com/temp-track.mp3", limit=20)

# One track with every alternate version (instrumental, acoustic, …). Charged at track_detail; a 404 is free.
full = client.tracks.get(track.id)
for alt in full.child_tracks:
    print("alt:", alt.id, alt.title, alt.vocals)

# Every facet value you can filter on. Free.
taxonomies = client.taxonomies.list()
print(taxonomies.values("mood")[:10])
```

`limit` is 1–100. The server silently caps larger values, so the SDK rejects them before sending, which means you never get fewer rows than you asked for without knowing it. String limits (`search` ≤ 200, a Deep Search `query` ≤ 500, …) are measured in UTF-16 code units, the way the server measures them, after surrounding whitespace and a leading byte-order mark are stripped. `tracks.list` accepts `page, limit, search, genre, mood, instrument, theme, artist, album, vocals, key, bpm_min, bpm_max, duration_min, duration_max, featured, explicit, decade, id, include_children, sort_by, sort_order`. Unknown keyword arguments are a `TypeError`.

## Understanding billing: sync vs stream

Audio leaves the API by two routes, and they are charged in opposite directions on purpose:

| Audio | Getting the link | Moving the bytes |
|---|---|---|
| **WAV master**: `POST /v1/tracks/{id}/sync` (`client.tracks.sync`) | **Charged** at `sync` (or `export_sync` with `external_export=True`) for a new window. Reissues within a paid window are free. | **Free.** Download a link you hold as many times as you like while it is valid. |
| **MP3 preview**: `asset_url` on every catalog response | **Free.** It comes with the listing, search or detail call you already paid for. | **Intended to be billed per stream (`costs.stream`) from API credits in a future update** (metered today, not yet deducted). |

- **Sync pays when the link is issued, not when the file transfers.** A repeat request for the same account, track and mode while the link is valid is free (`credits_charged == 0`, `reissued is True`) and keeps the original `expires_at`. Asking again does not extend access. Standard sync and Export Sync (`external_export=True`) each have their own window.
- **Stream charges the other way: the link is free and playback is what is intended to be billed**, from API credits in a future update (metered today, not yet deducted). Plan for it now. Treat every preview play as a metered event, do not auto-play previews nobody asked for, and do not build on the assumption that streaming stays free. `GET /v1/credits` already publishes `costs.stream`.
- **Other charged calls:** listings (`tracks_list`), detail (`track_detail`) and the searches above. `credits.retrieve()`, `taxonomies.list()`, `health.*`, downloads of signed URLs, and sync reissues inside the window are free.
- **The rate card is per account and can change at any time.** Read it with `client.credits.retrieve().costs`; never hard-code a price. Those `costs` already include your plan discounts.

```python
credits = client.credits.retrieve()            # free and unmetered
need = credits.costs["deep_search_first_page"] + credits.costs["sync"]
if credits.api_credits.spendable < need:
    raise SystemExit(f"Need {need} credits, have {credits.api_credits.spendable}")
for batch in credits.expiring_credits:
    print(f"{batch.api_credits} credits expire at {batch.expires_at}")
```

`api_credits.spendable` is `balance + overdraft_limit`, and your balance may go negative within your account's overdraft. Balances are per account, so every key on the account draws from the same pool. The full set of cost keys and the retry rules are in [`docs/billing-and-retries.md`](https://github.com/therightsfoundry/trf-python/blob/main/docs/billing-and-retries.md).

## Credit guardrails, budgets and auto-pagination

These guards run on the client side. They are meant to stop a runaway loop before it spends your balance. The server's `402` is still the final word on what an account can spend.

```python
import rightsfoundry

def alert(event: rightsfoundry.LowCreditEvent) -> None:
    # Edge-triggered: fires once per drop below the threshold. Keep it fast (enqueue, don't email inline).
    print(f"low credits: {event.spendable} < {event.threshold} (source={event.source})")

client = rightsfoundry.Client(
    max_credits_per_request=25,           # refuse any single call projected above 25 credits
    low_credit_warning_threshold=1_000,   # call on_low_credit when spendable drops below this
    on_low_credit=alert,
    auto_check_credits=True,              # preflight: refresh balance (free) at most every 30 s
    guard_failure_mode="closed",          # default: if prices can't be read, refuse rather than guess
)

# Per-call override: allow this one sync above the client-wide cap.
link = client.tracks.sync(track_id, max_credits=200)
```

| Guard | What it does | Error (nothing is sent) |
|---|---|---|
| `max_credits_per_request` / per-call `max_credits=` | Projects the call's cost from your live rate card (`GET /v1/credits`, cached `rate_card_ttl_ms` = 60 s) and refuses if it is above the cap. `sync` is projected at full price. | `CreditCapExceededError` (`.projected`, `.cap`, `.cost_key`) |
| `auto_check_credits=True` | Before each charged call, estimates `spendable − cost` from a balance snapshot (refreshed every `credit_check_ttl_ms` = 30 s) and refuses if it would go below zero. `min_bandwidth_bytes` adds a bandwidth floor. | `PreflightInsufficientCreditsError` (`.resource` = `api_credits` or `bandwidth`) |
| `guard_failure_mode` | `"closed"` (default) refuses when the rate card cannot be read or has no price for the call. `"open"` logs a warning and sends anyway. | `CreditGuardUnavailableError` |
| `low_credit_warning_threshold` + `on_low_credit` | Watches `spendable` from `credits.retrieve()`, `402` bodies and, with `auto_check_credits`, local estimates. Without a callback it logs a warning. | — (callback, never raises) |
| Iterator `max_credits=` | A budget for a whole walk. The next page's cost is projected before it is sent. | `CreditBudgetExceededError` (`.spent_projected`, `.next_cost`, `.cap`) |

**Auto-pagination.** Every page is charged, so iterators are lazy and bounded by default:

```python
# Walk a filtered catalogue: at most 40 credits and 1,000 tracks, 100 per page (the fewest charged calls).
it = client.tracks.iter(genre="jazz", vocals="instrumental", max_items=1_000, max_credits=40)
try:
    for track in it:
        index(track.id, track.title, track.genres)
except rightsfoundry.CreditBudgetExceededError as e:
    print(f"stopped before page costing {e.next_cost}; ~{e.spent_projected} credits spent")

# Page by page, for example to checkpoint progress:
for page in client.tracks.iter(mood="calm").pages():
    checkpoint(page.page, [t.id for t in page])

# AI searches paginate too (candidate set ≤ 1,000 → at most 10 pages at limit 100).
for t in client.tracks.deep_search_iter("lo-fi study beats with vinyl crackle", max_credits=15):
    ...
```

Iterators stop at the end of the result set (`page * limit >= total`), on an empty page, at `max_pages` (default 50), at `max_items`, or before going over `max_credits`. A short page does not end the walk.

**Iterators are single-use.** Looping over an iterator (or its `.pages()`) a second time raises `rightsfoundry.IteratorConsumedError` (a `RightsFoundryError` and a `RuntimeError`) instead of silently sending, and paying for, every page again. To walk the results again, call `client.tracks.iter(...)` again.

## Configuration

Values are resolved in this order: keyword argument, then environment variable (read **once**, when the client is constructed), then the default. Every option is checked at construction, and an invalid value raises `ConfigurationError` straight away rather than on the first request.

| Option | Default | Env var | Notes |
|---|---|---|---|
| `api_key` | — (required) | `TRF_API_KEY` | 128 hex characters. Passing `api_key=None` explicitly does **not** fall back to the env var. |
| `environment` | `"production"` | `TRF_ENVIRONMENT` | `"production"` or `"staging"` (staging keys work only on staging) |
| `base_url` | from `environment` | `TRF_BASE_URL` | HTTPS only; host must be a Rights Foundry API host or in `allowed_hosts` (code only) |
| `connect_timeout` / `read_timeout` | 10 s / 60 s | `TRF_CONNECT_TIMEOUT` / `TRF_READ_TIMEOUT` | Every method also takes `timeout=` ([per-call options](#per-call-options)) |
| `total_timeout` | `None` | — | Wall-clock limit across all attempts and backoff |
| `max_retries` | 2 | `TRF_MAX_RETRIES` | 0–10, gated by [money safety](#retries-and-money-safety) |
| `charged_retry_policy` | `"refund_backed"` | — | `"never"`, `"refund_backed"` or `"always"` |
| `max_retry_after_ms` | 30 000 | — | A longer `Retry-After` is raised as an error instead of slept on |
| `max_credits_per_request` | `None` | `TRF_MAX_CREDITS_PER_REQUEST` | Client-side cap per charged call |
| `low_credit_warning_threshold` / `on_low_credit` | `None` | `TRF_LOW_CREDIT_WARNING_THRESHOLD` | Edge-triggered low-balance callback |
| `auto_check_credits` | `False` | `TRF_AUTO_CHECK_CREDITS` | Preflight balance check before charged calls |
| `guard_failure_mode` | `"closed"` | — | Code only: no env var can make a guard fail open |
| `on_request` / `on_response` / `on_error` | `None` | — | [Hooks](#hooks-and-logging) receive redacted, immutable events |
| `logger` | silent `rightsfoundry` logger | `TRF_LOG` | `off`, `error`, `warn`, `info`, `debug` |
| `user_agent_suffix` | `None` | `TRF_USER_AGENT_SUFFIX` | e.g. `"acme-editor/4.2"` |
| `custom_headers` | `{}` | — | `X-…` and W3C trace headers only; sent to the API host, never to the asset host |
| `ca_bundle` | system trust store | `TRF_CA_BUNDLE` | Trusts this PEM bundle instead of the system store; verification stays on |
| `proxy_url` | none | `HTTPS_PROXY`, `NO_PROXY` | `http://` or `https://` CONNECT proxy (with `https://`, TLS to the proxy, then CONNECT, then TLS to the API); TLS to the API is always end to end. Other schemes such as `socks5://` raise `ConfigurationError` naming the source (`proxy_url` or `HTTPS_PROXY`) |
| `max_connections` | 10 | — | Per-host pool size (also the `AsyncClient` worker count) |
| `max_concurrency` | `None` | — | Limit on in-flight requests per `AsyncClient` |
| `max_response_bytes` / `max_download_bytes` | 10 MiB / 2 GiB | — | Hard body limits |
| `taxonomies_cache_ttl_ms` | 0 (off) | — | In-process cache for the free `taxonomies.list()` |

Use `client.with_options(...)` to get a client with a few options changed. It shares the original's connection pool and re-checks the merged configuration:

```python
batch_client = client.with_options(max_credits_per_request=5, charged_retry_policy="never")
```

The full option list (`write_timeout`, `jitter`, `retryable_status_codes`, `asset_hosts`, `allow_insecure_localhost`, `transport`, …) is in [`llms-full.txt`](https://github.com/therightsfoundry/trf-python/blob/main/llms-full.txt).

## Per-call options

Every typed method (`tracks.list`, `.iter`, `.get`, `.deep_search`, `.search_similar`, `.sync`, `taxonomies.list`, `credits.retrieve`, `health.check`, `.live`, `.ready`) accepts these keywords for a single call:

| Keyword | Effect |
|---|---|
| `timeout=` | Seconds for this call (0 < t ≤ 600) |
| `max_retries=` | Retry budget for this call (0–10), replacing the client's `max_retries`; the [money-safety rules](#retries-and-money-safety) still apply |
| `idempotency_key=` | Sent as `Idempotency-Key` (printable ASCII without spaces, 1–255 characters). With a key, ambiguous outcomes are retried even under `"refund_backed"`; see [Retries and money safety](#retries-and-money-safety) |
| `send_max_credits_header=` | With a cap in force, also sends `X-TRF-Max-Credits: <cap>` so the server can enforce it once it supports the header (forward-compatible; ignored today) |
| `max_credits=` | Charged methods only: a client-side cap on the projected cost; nothing is sent if it would be exceeded |

The same fields are bundled in `rightsfoundry.CallOptions`, a frozen dataclass (`timeout`, `max_credits`, `max_retries`, `idempotency_key`, `send_max_credits_header`) that `client.request()` also accepts:

```python
import uuid
import rightsfoundry

link = client.tracks.sync(track_id, max_credits=200, timeout=30, idempotency_key=f"order-{order_id}")
page = client.tracks.list(mood="calm", max_retries=0)

opts = rightsfoundry.CallOptions(timeout=20, max_credits=10, idempotency_key=str(uuid.uuid4()))
data = client.request("GET", "/v1/tracks", query={"mood": "calm", "limit": 5}, options=opts)
```

`client.request()` is the escape hatch for endpoints added to the API before an SDK release. It runs the full pipeline (auth, guards, retries, redaction, bounded reads), supports `GET`, `POST`, `PUT`, `PATCH`, `DELETE` and `HEAD`, returns `None` for a `204` or an empty `2xx` body, and returns JSON arrays and primitives as they are.

## AsyncIO and web backends

`AsyncClient` has the same surface as `Client`: every method can be awaited, and iterators are consumed with `async for`.

```python
import asyncio
import rightsfoundry

async def main() -> None:
    async with rightsfoundry.AsyncClient() as client:
        credits = await client.credits.retrieve()
        print(credits.api_credits.spendable)

        page = await client.tracks.list(mood="uplifting", limit=10)
        async for track in client.tracks.deep_search_iter("cinematic sunrise swell", max_credits=10):
            print(track.id, track.title)

asyncio.run(main())
```

Create **one `AsyncClient` per event loop**, for example in your app's lifespan. By default it runs the same hardened `http.client` transport in a bounded thread pool; set `max_concurrency=` to limit in-flight requests.

**The API key is an account secret and stays on your server.** Browsers get only what they need: track metadata and the signed preview `asset_url`. Never send the key to a browser, mobile app or client bundle.

```python
# FastAPI (pip install fastapi uvicorn). The full runnable version is in examples/03.
from contextlib import asynccontextmanager
from fastapi import FastAPI
import rightsfoundry

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with rightsfoundry.AsyncClient(max_credits_per_request=10) as client:
        app.state.trf = client
        yield

app = FastAPI(lifespan=lifespan)

@app.get("/api/tracks")
async def tracks(mood: str):
    page = await app.state.trf.tracks.list(mood=mood, limit=20)
    return [
        {"id": t.id, "title": t.title, "artist": t.artist, "duration": t.duration, "preview_url": t.asset_url}
        for t in page.data
    ]
```

- **Django / Flask:** create one module-level `rightsfoundry.Client()` (it is thread-safe) or put it in your app factory, and call it from views. Return the same small dicts.
- **Every proxied browser request spends your credits.** Put authentication and rate limiting in front of these endpoints, and set `max_credits_per_request`.
- **Previews are metered** and intended to be billed from API credits in a future update (not yet deducted). Do not auto-play, and do not hand out preview URLs nobody asked for.
- **Cloudflare Workers:** Python Workers run on Pyodide/WebAssembly, where `http.client` sockets are unavailable, and the SDK refuses that platform. From a Worker, call the REST API with `fetch` and keep the key in a Worker secret, or run this SDK on a server and have the Worker call it.

## Typed error handling and charge state

Every error is a subclass of `rightsfoundry.RightsFoundryError` and carries `.status`, `.request_id`, `.operation`, `.charge_state`, `.retry_after` and `.attempts`. `str(err)` always includes the `request_id`, so quote it when you contact support.

```python
import rightsfoundry as rf

try:
    link = client.tracks.sync(track_id)
except rf.InsufficientCreditsError as e:          # 402
    notify_billing(e.api_credits)                 # balance before the refused charge
except rf.FeatureNotEnabledError as e:            # 403; e.feature == "sync" or "deep_search"
    show("Sync licensing is not enabled for this key. Contact support.")
except (rf.TrackNotFoundError, rf.SyncMasterUnavailableError):
    mark_unlicensable(track_id)                   # free 404s, never retried
except rf.CreditCapExceededError as e:            # client-side, nothing sent
    log.warning("blocked: %s would cost %s > cap %s", e.cost_key, e.projected, e.cap)
except rf.APIStatusError as e:
    log.error("TRF %s %s (request %s, charge %s)", e.status, e, e.request_id, e.charge_state)
    raise
```

`charge_state` tells you what the server did with your money:

| `charge_state` | Meaning | Raised for |
|---|---|---|
| `"not_charged"` | Nothing was deducted | All client-side errors (`ValidationError`, `CreditGuardError`, `ConfigurationError`); 400 (except below), 401, 402, 403, 404, 405, 410, 429; `BillingUnavailableError` (request not served, pending deduction cancelled); connect-phase failures |
| `"refund_pending"` | A charge was recorded and a best-effort refund is queued | 500 `InternalServerError` on charged calls; 503 `SearchUnavailableError` |
| `"charged"` | You paid, and no refund will come | **`UnreachableFileError`**: Acoustic Similarity Search could not fetch or decode your `file_url` |
| `"unknown"` | The call may or may not have been charged | Read timeouts or resets after the request was sent; 408, 502, 504 |

The main families are `ValidationError` (incl. `InvalidTrackIdError`), `CreditGuardError`, `APIConnectionError` (incl. `APITimeoutError` with `.phase`, and `TLSVerificationError`, which is never retried), `APIStatusError` (`BadRequestError`, `AuthenticationError`, `InsufficientCreditsError`, `PermissionDeniedError` → `InvalidTokenError`/`InactiveTokenError`/`ExpiredTokenError`/`FeatureNotEnabledError`, `NotFoundError`, `RateLimitError` → `DailyCallLimitError`, `InternalServerError`, `ServiceUnavailableError` → `BillingUnavailableError`/`SearchUnavailableError`), `SyncLinkError` (`SyncLinkExpiredError`, `SyncLinkInvalidError`), `ResponseTooLargeError`, `UnexpectedRedirectError`, `UnexpectedResponseError` and `HealthCheckError`. The full table of exact server messages is in [`llms-full.txt`](https://github.com/therightsfoundry/trf-python/blob/main/llms-full.txt).

## Retries and money safety

Catalogue and AI-search calls are charged **before** the work is done, and the API does not honour idempotency keys yet. Retrying a call that actually succeeded would charge you twice. The SDK therefore decides whether to retry based on what the server guarantees about money for each outcome:

| Outcome | `free` ops | `sync` | charged ops, `refund_backed` (default) | charged, `never` | charged, `always` |
|---|---|---|---|---|---|
| Connect failure (request never written) | retry | retry | retry | retry | retry |
| `429` | retry* | retry* | retry* | retry* | retry* |
| `503` billing unavailable (not served) | retry | retry | retry | no | retry |
| `503` search unavailable (refunded) | — | — | retry | no | retry |
| `500` (refund queued) | retry | retry | retry | no | retry |
| `408`, `502`, `504` (outcome unknown) | retry | retry | **no**† | no | retry |
| Read timeout / reset after send | retry | retry | **no**† | no | retry |

† Retried when the call carries `idempotency_key=` (see below).

\* Only if `Retry-After` ≤ `max_retry_after_ms` (30 s by default). A daily-cap `429` on an MCP connection asks you to wait until 00:00 UTC, so it is raised immediately as `DailyCallLimitError` with `.reset_at`.

- **`sync` can be retried even when the outcome is ambiguous.** The server records each paid window under a per-(account, track, mode) lock, so a retry either reissues for free or pays once.
- **With `idempotency_key=`, ambiguous outcomes are retried** (`408`, `502`, `504`, read-phase connection failures) even under `"refund_backed"`, because once the API honours the key the server will recognise a repeat and charge once. This is forward-compatible: the SDK sends the key today, and your calls get full retry safety as soon as the API honours it. Until then, a retried key-carrying call can still be charged twice, so use a key only where that risk is acceptable or where you reconcile with `credits.retrieve()`.
- **Health probes are not retried by default** (`max_retries=0`): a degraded `503` comes back at once as `HealthStatus(status="degraded")`. Pass `max_retries=2` to retry.
- Backoff is exponential with CSPRNG full jitter (500 ms → 8 s) and never shorter than the server's `Retry-After`.
- A success that followed a refundable failure is flagged with `retried_after_refundable_failure=True` on `TrackPage` and `SyncLink`, so your reconciliation code can explain a temporary double deduction.
- `charged_retry_policy="always"` buys availability at the risk of paying twice. Choose it deliberately.

## Hooks and logging

Hooks receive immutable, **already redacted** events. They cannot change the outgoing request. If a hook raises, the exception is logged and swallowed, and it never fails your call.

```python
client = rightsfoundry.Client(
    on_request=lambda e: print("→", e.operation, e.method, e.url, e.cost_key, e.projected_credits),
    on_response=lambda e: metrics.timing("trf.call", e.duration_ms, tags={"op": e.operation, "status": e.status}),
    on_error=lambda e: print("✗", e.operation, e.status, e.charge_state, "retry" if e.will_retry else "final"),
)
print(client)
# Client(environment='production', base_url='https://api.therightsfoundry.com', api_key=[REDACTED fp=1a2b3c4d], ...)
```

- URLs in events and logs have `sig=[REDACTED]`. `Authorization` appears as `Bearer [REDACTED fp=…]`, and any 128-hex run becomes `[REDACTED_TOKEN]`. Response headers are allowlisted, and `set-cookie` is never exposed.
- `client.api_key_fingerprint` returns the same 8-character fingerprint that [/dashboard/token](https://therightsfoundry.com/dashboard/token) shows, so you can tell which key a process is using without revealing the key.
- The library logger `rightsfoundry` is silent by default. Set `TRF_LOG=debug` to get one redacted line per attempt, or pass `logger=`.
- Use `SyncLink.redacted_download_url()` and `Track.redacted_asset_url()` whenever a link goes to a log.

## Command line

The package installs a `rightsfoundry` command (also `python -m rightsfoundry`). It reads the key from `TRF_API_KEY` only, never from a flag, so the key does not end up in shell history or `ps` output.

```bash
rightsfoundry health                         # no key needed, free
rightsfoundry credits                        # balance + your rate card, free
rightsfoundry taxonomies --facet mood        # free
rightsfoundry search "dark epic" --vocals instrumental --bpm-min 90 --bpm-max 120 --limit 10
rightsfoundry deep-search "warm hopeful acoustic guitar" --instrumental --limit 10 --max-credits 20
rightsfoundry similar https://cdn.example.com/temp-track.mp3 --limit 10
rightsfoundry track 123456
rightsfoundry sync 123456 --download ./masters/            # standard sync
rightsfoundry sync 123456 --external-export --download ./masters/   # Export Sync
```

Add `--json` for machine-readable output and `--environment staging` for staging. Global flags such as `--json` and `--environment` work before or after the subcommand. JSON output is ASCII-escaped, so catalogue text cannot put control or bidirectional characters on your terminal. Exit codes: `0` ok, `1` API or network error, `2` usage, validation or guard error, `3` health not healthy. Human-readable output redacts signed URLs.

## GitHub Action

You can search the catalogue and license masters from a workflow. Save this as `.github/workflows/soundtrack.yml` and add a repository secret named `TRF_API_KEY`:

```yaml
name: soundtrack

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  soundtrack:
    runs-on: ubuntu-24.04
    steps:
      - id: search
        uses: therightsfoundry/trf-python@v1
        with:
          action: deep-search
          api-key: ${{ secrets.TRF_API_KEY }}
          query: "upbeat corporate background, no vocals"
          instrumental: "true"
          max-credits: "50"

      - id: sync
        if: steps.search.outputs.track-id != ''
        uses: therightsfoundry/trf-python@v1
        with:
          action: sync
          api-key: ${{ secrets.TRF_API_KEY }}
          track-id: ${{ steps.search.outputs.track-id }}
          download-path: masters/
          max-credits: "200"
```

Actions: `deep-search`, `search`, `search-similar`, `track`, `sync`, `credits`, `health`. Outputs: `result-json` (with signatures redacted), `track-id`, `track-title`, `track-artist`, `downloaded-file`, `credits-charged`, `credits-spendable`, `request-id`. The key and every signature are masked in the log, and catalogue text runs inside `::stop-commands::` so it cannot issue workflow commands. The action needs Python 3.9 or newer on the runner and runs it in isolated mode (`python3 -I`), so nothing in the checked-out workspace can be imported in its place. `@v1` follows the latest 1.x release; for production, pin the action to a commit SHA. The full reference and recipes are in [`docs/github-action.md`](https://github.com/therightsfoundry/trf-python/blob/main/docs/github-action.md), and a complete workflow is in [`examples/github_workflow_soundtrack.yml`](https://github.com/therightsfoundry/trf-python/blob/main/examples/github_workflow_soundtrack.yml).

## Building AI agents?

- **MCP connector:** [`https://mcp.therightsfoundry.com/mcp`](https://mcp.therightsfoundry.com/mcp). It works with any MCP client that supports remote servers and OAuth: the user signs in with their Rights Foundry account, and the agent never handles an API key. Its tools let the agent browse the catalogue, read a track, list taxonomies, read the licensing terms and license a track. Directory metadata is at the [server card](https://mcp.therightsfoundry.com/.well-known/mcp/server-card.json).
- **Writing your own agent loop?** Wrap this SDK in tools. [`docs/llm-and-mcp-guide.md`](https://github.com/therightsfoundry/trf-python/blob/main/docs/llm-and-mcp-guide.md) has OpenAI- and Anthropic-style tool schemas for `deep_search`, `list`, `sync` and `credits`, plus the safety rules: cap credits per tool call, require human approval before `sync`, and never give the model the API key.
- **Machine-readable docs:** [`llms.txt`](https://github.com/therightsfoundry/trf-python/blob/main/llms.txt) (index) and [`llms-full.txt`](https://github.com/therightsfoundry/trf-python/blob/main/llms-full.txt) (the complete SDK reference in one file). Coding assistants should read [`AGENTS.md`](https://github.com/therightsfoundry/trf-python/blob/main/AGENTS.md).

## Security and supply chain

- **Zero runtime dependencies.** `dependencies = []`. CI asserts it on the built wheel and sdist, then imports the wheel in a fresh venv installed with `--no-deps`.
- **TLS 1.2+ with certificate and hostname verification**, using the SDK's own `SSLContext`, so `PYTHONHTTPSVERIFY=0` and patched default contexts do not affect it. There is no `verify=False` option.
- **Redirects are never followed**, so your `Authorization` header cannot be carried to another host. Signed downloads go only to `assets.therightsfoundry.com` (`https`, port 443, `/stream/…?expires=…&sig=…`) and are sent **no** credentials, custom headers or cookies.
- **SSRF and injection defence:** strict `base_url` allowlist (no IP literals, no userinfo, no env-var override of the host allowlist), ASCII track IDs only, an allowlist of typed query parameters, `file_url` checks, and header values that cannot contain CR, LF or NUL.
- **Secret fingerprint.** The key is held in `rightsfoundry.Secret`. Its `repr`, `str` and `format` show `[REDACTED fp=1a2b3c4d]`, which is the same fingerprint as [/dashboard/token](https://therightsfoundry.com/dashboard/token). Pickling a key, config or client raises `TypeError`.
- **Bounded everything:** 10 MiB response limit (counted after gzip decompression, so a decompression bomb is stopped too), JSON nesting ≤ 64, no NaN/Infinity, 2 GiB download limit. Downloads are written to an exclusively created `0600` temp file, `fsync`ed, and atomically renamed; symlinks are refused. With `resume=True`, the hidden partial file is keyed by a hash of the URL path, so one track never appends onto another's partial, and if a ranged resume fails the SDK truncates the partial and retries once with a full download.
- **Models are safe to log and share.** Models pickle and `copy.deepcopy` cleanly. `.raw` is an immutable, read-only mapping whose `str()` and `repr()` redact signed-URL `sig` values; indexing it still returns the real value.
- **Provenance.** Releases are built in GitHub Actions and published to PyPI through Trusted Publishing (OIDC, no stored tokens), with PEP 740 attestations and GitHub build provenance. To verify a downloaded file:

  ```bash
  pip download rightsfoundry==1.0.0 --no-deps -d dist/
  gh attestation verify dist/<file> --repo therightsfoundry/trf-python
  ```

- Report vulnerabilities privately: see [`SECURITY.md`](https://github.com/therightsfoundry/trf-python/blob/main/SECURITY.md). Every control (SEC-01 to SEC-20) is described in [`docs/security.md`](https://github.com/therightsfoundry/trf-python/blob/main/docs/security.md).

**Residual risks you own:** the key sits in plaintext in process memory and in the environment variable it came from, and child processes inherit that variable. For long-running services, read the key from a secret manager and pass `api_key=` explicitly. Error trackers that capture frame locals (e.g. Sentry with `include_local_variables`) should keep their default scrubbing of `authorization` turned on.

## FAQ

**How do I license music programmatically via API in Python?**
Install `rightsfoundry`, set `TRF_API_KEY`, find a track (`client.tracks.list`, `.deep_search` or `.search_similar`), then call `client.tracks.sync(track_id)`. That single call is the licence transaction: it charges `costs.sync` and returns a signed 1-hour link to the WAV master, which `client.sync_links.download(link, "./masters/")` saves to disk. See the [quickstart](#60-second-quickstart).

**Can I use licensed music in YouTube videos, commercials, apps, or AI-generated videos?**
It depends on where the finished content is used, not on how the video was made. A **standard sync** licence (`tracks.sync(id)`) allows one-time synchronization of the track and use of the resulting content on online third-party platforms, without monetization. An **Export Sync** licence (`tracks.sync(id, external_export=True)`, charged at `export_sync`) adds use of the synced output or audio file offline, and placing, exporting or distributing it outside the platform (for example client delivery, broadcast, social media or offline playback). A track synced into an AI-generated video is licensed the same way as any other video. For monetized uploads, paid advertising, or any use not described here, read the [licensing terms](https://therightsfoundry.com/licensing) before you publish; they govern, and this README only summarises them. If you are unsure, ask info@therightsfoundry.com.

**Are the tracks human-made or AI-generated?**
The catalogue is cleared, human-made music. Each `Track` also carries AI musical analysis fields, including `is_ai_music` and `ai_music_score`. These come from an automated audio classifier and are `None` for tracks that were never analysed.

**Can I use The Rights Foundry catalog to train AI models?**
No. Sync licences cover synchronizing music into videos, apps, games, podcasts and similar productions. They do **not** grant AI or machine-learning training rights. Licensing training data is a separate agreement: see [Licensed AI training data](https://therightsfoundry.com/licensed-ai-training-data) and the [licensing terms](https://therightsfoundry.com/licensing).

**Is there a free tier? How is it priced?**
Usage is paid from prepaid credits, topped up directly or through an optional monthly plan. New accounts can [claim free beta credits](https://therightsfoundry.com/register): promotional welcome credits, granted when you save a payment method on your account. If they are used up inside the promotion window, the account moves to the Starter Beta monthly plan unless you cancel that transition; current plan and auto-conversion terms are on [therightsfoundry.com](https://therightsfoundry.com/). Prices are per call and are read live from `GET /v1/credits` (`client.credits.retrieve().costs`), which is free. Do not hard-code prices, because the rate card can change without a new API version. Health, taxonomies, the credits call itself, signed-URL downloads and sync reissues inside the window are free.

**Does the SDK work in the browser?**
No, by design. The API key can spend your whole account's balance. The SDK refuses Pyodide/WebAssembly runtimes unless you pass `dangerously_allow_browser=True`, and you should not pass it. Call the API from your server and send the browser only track metadata and signed preview URLs.

**Which Python versions are supported?**
CPython 3.9–3.14. CI runs every version on Linux and 3.11–3.14 on macOS and Windows. Python 3.9 is past upstream end-of-life and triggers a one-time `DeprecationWarning`. Support for it will be dropped in a future minor release, announced in the [changelog](https://github.com/therightsfoundry/trf-python/blob/main/CHANGELOG.md).

**Where do I get help?**
API reference: [docs.therightsfoundry.com](https://docs.therightsfoundry.com/). Builder overview: [therightsfoundry.com/for-builders](https://therightsfoundry.com/for-builders). Bugs and feature requests: [GitHub issues](https://github.com/therightsfoundry/trf-python/issues), but never paste your API key or a signed URL there. Account and licensing questions: info@therightsfoundry.com.

---

## License

MIT. See [LICENSE](https://github.com/therightsfoundry/trf-python/blob/main/LICENSE). The SDK is MIT-licensed; the music is licensed under The Rights Foundry's [licensing terms](https://therightsfoundry.com/licensing).

[Website](https://therightsfoundry.com) · [API Reference](https://docs.therightsfoundry.com) · [OpenAPI](https://docs.therightsfoundry.com/openapi.yaml) · [PyPI](https://pypi.org/project/rightsfoundry/) · [Changelog](https://github.com/therightsfoundry/trf-python/blob/main/CHANGELOG.md) · [Security](https://github.com/therightsfoundry/trf-python/blob/main/SECURITY.md) · [Privacy](https://therightsfoundry.com/privacy) · [Terms](https://therightsfoundry.com/terms)
