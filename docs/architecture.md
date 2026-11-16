# Architecture

This document describes how `rightsfoundry` is built: the layers, the request pipeline, the declarative endpoint registry, and how to inject your own transport. For the security controls, see [security.md](security.md). For money and retry semantics, see [billing-and-retries.md](billing-and-retries.md).

## Layers

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Public surface                                                               │
│   Client / AsyncClient                                                       │
│     .tracks  .taxonomies  .credits  .health  .sync_links   .request()        │
├──────────────────────────────────────────────────────────────────────────────┤
│ Resources (resources/tracks.py, resources/misc.py)                           │
│   call ENDPOINTS[...].build(...) → a validated OperationSpec                 │
│   never touch the network                                                    │
├──────────────────────────────────────────────────────────────────────────────┤
│ Endpoint registry (_endpoints.py) — ENDPOINTS: method, path, auth, retry     │
│   class, cost-key rule, query params, body fields, decoder, build()          │
├──────────────────────────────────────────────────────────────────────────────┤
│ Request pipeline (_pipeline.py) — PipelineCore (pure decisions) driven by    │
│   SyncPipeline / AsyncPipeline (I/O and waiting only)                        │
│   + CreditGuard (_guard.py) + retry matrix (_retry.py) + redactor (_redact)  │
├──────────────────────────────────────────────────────────────────────────────┤
│ Transport (_transport.py) — send(PreparedRequest, timeouts) → RawResponse    │
│   default: HTTPTransport (stdlib http.client pool + strict SSLContext)       │
│   async default: AsyncExecutorTransport (same transport in a bounded pool)   │
│   injectable: Transport / AsyncTransport protocols                           │
├──────────────────────────────────────────────────────────────────────────────┤
│ Config (_config.py) — immutable ClientConfig, validated once, key in Secret  │
└──────────────────────────────────────────────────────────────────────────────┘
```

Two rules hold everywhere:

1. **Config is immutable.** `with_options(...)` builds a new validated config and a new client, which shares the connection pool when it safely can.
2. **Only two modules send bytes.** `_pipeline.py` talks to the API, and `_download.py` fetches signed asset URLs. Each security control therefore has exactly one place where it is implemented.

## The 13-step request pipeline

Every API call, including `client.request(...)`, goes through the same steps in the same order:

```
 1 validate inputs ─────────── EndpointSpec.build(): typed checks, ValidationError before anything is sent
 2 build URL ───────────────── validated base_url + constant path + allowlisted, fixed-order query
 3 credit guard ────────────── CreditGuard: cap / preflight / fail-closed (may fetch the free rate card)
 4 attach headers ──────────── Authorization (bearer ops only), User-Agent, Accept, custom X- headers,
                               Idempotency-Key and X-TRF-Max-Credits when the call's options ask for them
 5 on_request hook ─────────── immutable, redacted RequestEvent
 6 transport.send ──────────── connect / read / write timeouts; redirects are never followed
 7 bounded body read ───────── max_response_bytes, counted after gzip/deflate decompression
 8 classify ────────────────── success | typed error (exact message table) | retryable outcome
 9 retry loop ──────────────── gated by retry class × charged_retry_policy; CSPRNG jitter; Retry-After
10 on_response / on_error ──── redacted events; hook exceptions are logged and swallowed
11 decode JSON ─────────────── strict: no NaN/Infinity, depth ≤ 64, UTF-8 only
12 build model ─────────────── frozen dataclass with .raw
13 credit observations ─────── balance snapshot, local spend estimate, edge-triggered low-credit callback
```

Per-call settings travel with the operation as a `CallOptions` value (`timeout`, `max_credits`, `max_retries`, `idempotency_key`, `send_max_credits_header`). Typed methods build it from their keywords, and `client.request(..., options=...)` takes it directly. Step 3 reads `max_credits`, step 4 adds `Idempotency-Key` / `X-TRF-Max-Credits`, step 6 uses `timeout`, and step 9 uses `max_retries` and whether an idempotency key is present (a key lets ambiguous outcomes be retried; see [billing-and-retries.md](billing-and-retries.md#idempotency-keys-forward-compatible)). Health probes default to `max_retries=0`.

`PipelineCore` holds every decision as a pure, synchronous method: `prepare`, `timeouts`, `on_response`, `on_transport_error`, `_decide`, and the hook emitters. `SyncPipeline.execute` and `AsyncPipeline.execute` are loops of about 30 lines that call the core, the transport and either `time.sleep` or `asyncio.sleep`. The sync and async clients therefore share one implementation of retry, classification, redaction and guard logic, and cannot drift apart.

## Module map

| Module | What lives there |
|---|---|
| `__init__.py` | Public exports (`__all__`) |
| `_client.py` | `Client`, `AsyncClient`, `unauthenticated()`, `with_options()`, `request()` |
| `_config.py` | `build_config()`, `ClientConfig`, option validation, env-var resolution, `base_url` rules |
| `_endpoints.py` | `EndpointSpec` (with `build()`, decoders and cost-key rules), `OperationSpec`, `ENDPOINTS`, `PUBLIC_COST_KEYS`, `match_endpoint()` |
| `_params.py` | `QueryParam`, `BodyField`, `LIST_QUERY_PARAMS` (a leaf module shared by the registry and validation) |
| `_options.py` | `CallOptions`, `resolve_options()` |
| `resources/tracks.py` | `TracksResource` / `AsyncTracksResource`, iterators, single-flight `sync()` |
| `resources/misc.py` | Taxonomies (optional in-process cache), credits, health, sync-link downloads |
| `_pipeline.py` | `PipelineCore`, `SyncPipeline`, `AsyncPipeline`, `BodyReader`, `parse_json` |
| `_transport.py` | `HTTPTransport`, `AsyncExecutorTransport`, `Transport` / `AsyncTransport` protocols, `PreparedRequest`, `RawResponse`, `AsyncRawResponse`, `Headers`, `build_ssl_context()` |
| `_retry.py` | `backoff_ms`, `parse_retry_after`, `classify_status_outcome`, `should_retry` |
| `_guard.py` | `CreditGuard`: rate card, projection, caps, preflight, low-credit edge trigger |
| `_errors.py` | Error hierarchy, `MESSAGE_TABLE` (exact server messages), `classify_api_error` |
| `_models.py` | Response models and hook events |
| `_pagination.py` | `SyncPageIterator`, `AsyncPageIterator` (single-use: a second pass raises `IteratorConsumedError`) |
| `_download.py` | Signed asset URL validation and atomic streaming download; `resume=True` partials are keyed by a hash of the URL path |
| `_secret.py` | `Secret`, `fingerprint()` |
| `_redact.py` | `redact_text`, `redact_value`, header redaction, response-header allowlist |
| `_validation.py` | Track IDs, list query encoding, `file_url`, `request()` path and query rules |
| `_cli.py` | The `rightsfoundry` command |
| `_action.py` | The GitHub Action entry point |

## The declarative endpoint registry

`_endpoints.py` is the single source of truth for the wire contract. Each API operation is described once:

```python
EndpointSpec(
    operation="tracks.sync",            # name used in errors, hooks and logs
    method="POST",
    path="/v1/tracks/{id}/sync",
    auth="bearer",                      # "none" for health: the key is never sent
    retry_class="sync",                 # "charged" | "sync" | "free" (drives the retry matrix)
    cost_key=_sync_cost,                # args → public cost key ("sync" or "export_sync")
    body=(BodyField("external_export", "bool", default=False),),
    response_model="SyncLink",
    decoder=_sync_decoder,              # parsed JSON → SyncLink
    summary="Mint a signed 1-hour link to the WAV master (sync licence)",
)
```

- **The registry is executable.** `spec.build(track_id=..., query=..., body=..., options=...)` validates every argument against the spec, encodes the query in a fixed order, fills body defaults, picks the cost key and returns the `OperationSpec` the pipeline sends. Resources hold no wire logic, so a new `GET /v1/tracks` filter is one `QueryParam` line plus the typed keyword on the two `list`/`iter` pairs (the parity tests check both).
- **The pipeline reads nothing else.** Retry class, auth mode and cost key all come from the spec, so a new endpoint inherits the existing security and money behaviour automatically.
- **`cost_key` mirrors the server's own price selection.** For example, `tracks.list` chooses `tracks_list`, `tracks_search_first_page` or `tracks_search_next_page` depending on `search` and `page`. The guard projects the price from your live rate card using that key.
- **`client.request()` uses the registry too.** `match_endpoint(method, path)` gives a known path its real cost key and retry class. An unknown path gets cost key `"unknown"` and the conservative `"charged"` retry class. It accepts `HEAD` as well as `GET`/`POST`/`PUT`/`PATCH`/`DELETE`, returns `None` for a `204` or empty `2xx` body, and returns JSON arrays and primitives as they are, so a new endpoint is usable before an SDK release.
- **`ENDPOINTS` and `PUBLIC_COST_KEYS` are exported** as read-only metadata. `scripts/check_openapi_sync.py` checks them against the vendored contract `conformance/openapi_contract.json` in CI (and can be pointed at the published OpenAPI document).

### Adding an endpoint

1. **Registry.** Add an `EndpointSpec` to `_SPECS` in `_endpoints.py` with method, path, auth, retry class, a `cost_key` rule, any `QueryParam`s or `BodyField`s, a `decoder` and the `response_model` name. If the endpoint introduces a new cost key, add it to `PUBLIC_COST_KEYS`.
2. **Model.** Add a frozen `@model` dataclass to `_models.py` and declare its fields. Scalar fields decode from their annotations (`decode_model`), so a new response field is one line; only nested models are passed explicitly. Keep `.raw`, make every field the server may omit `Optional`, and keep enums open (`str`). Export it from `__init__.py`.
3. **Resource.** Add a sync **and** an async method that call `ENDPOINTS[...].build(...)` and `pipeline.execute(op)`. Every method takes the per-call options (`timeout=`, `max_retries=`, `idempotency_key=`, `send_max_credits_header=`, `options=`); charged methods also take `max_credits=`. `tests/test_architecture_parity.py` fails if the two drift.
4. **Verify.** Add tests and conformance fixtures, then run:

   ```bash
   python3 scripts/check_openapi_sync.py conformance/openapi_contract.json
   curl -fsSL https://docs.therightsfoundry.com/openapi.yaml -o /tmp/openapi.yaml
   python3 scripts/check_openapi_sync.py /tmp/openapi.yaml --write-contract conformance/openapi_contract.json
   uv run pytest && uv run mypy --strict src tests scripts
   ```

   Update `README.md`, `llms-full.txt` and `CHANGELOG.md`.

## Concurrency model

| | `Client` | `AsyncClient` |
|---|---|---|
| Safety | Thread-safe; the pool is guarded by a lock | Task-safe; use one per event loop |
| Connection reuse | Per-`(scheme, host, port)` idle pool, `max_connections` (default 10) | Same pool, driven by a `ThreadPoolExecutor` with `max_connections` workers |
| Cancellation | Timeouts only | `asyncio.CancelledError` propagates; the in-flight thread finishes in the background and its response is discarded |
| Limit in-flight requests | — | `max_concurrency=` |
| Close | `client.close()` / `with` | `await client.aclose()` / `async with` |
| Single-flight `sync()` | `concurrent.futures.Future` per `(id, external_export)` | shared `asyncio` task per `(id, external_export)` |

`AsyncClient` does not include its own HTTP parser. A second, from-scratch asyncio HTTP/1.1 implementation (chunked framing, header folding, gzip) would add attack surface to a package whose purpose is to minimise it. If you need native async I/O at very high concurrency, inject an async transport (below).

**Proxies.** `HTTPTransport` tunnels through an `http://` proxy with `CONNECT`, or through an `https://` proxy by opening TLS to the proxy, sending `CONNECT`, then opening TLS to the API inside the tunnel. Either way the API certificate is verified end to end, and `Proxy-Authorization` is sent only on `CONNECT`. Other schemes (for example `socks5://`) raise `ConfigurationError` naming where the value came from (`proxy_url` or the `HTTPS_PROXY` environment variable).

`os.register_at_fork` drops pooled connections in forked children, so gunicorn and Celery preforking never share a TLS session between processes.

## Transport injection

A transport is anything with a `send()` method that matches the protocol:

```python
class Transport(Protocol):
    def send(self, request: PreparedRequest, *, connect_timeout: float,
             read_timeout: float, write_timeout: float) -> RawResponse: ...

class AsyncTransport(Protocol):
    async def send(self, request: PreparedRequest, *, connect_timeout: float,
                   read_timeout: float, write_timeout: float) -> AsyncRawResponse: ...
```

- `PreparedRequest` has `method`, `url` (already validated: always `https://`, or a validated loopback URL in tests), `headers` (iterate it to get `(name, value)` pairs; its `repr` is redacted) and `body` (`bytes` or `None`).
- `RawResponse(status, headers, body, *, on_close=None)` takes `body` as `bytes` or an iterable of `bytes` chunks. `AsyncRawResponse` also accepts an async iterable. `headers` may be a `Headers`, a dict, or a list of pairs.
- **Return the raw, still-encoded body.** The pipeline decompresses gzip/deflate itself so that it can count decompressed bytes against `max_response_bytes`.
- **The SDK, not the transport, enforces** URL validation, size limits, redirect refusal (any 3xx raises `UnexpectedRedirectError`), JSON safety and redaction.
- **What a transport can weaken:** TLS. The SDK cannot inspect a custom client's TLS settings, so keep verification on. The SDK logs a warning when a custom transport is used with `environment="production"`.
- **Error mapping matters for money safety.** Raise `rightsfoundry.APIConnectionError(..., phase="connect")` only when the request was certainly never written; that outcome is always retried. Any other exception is treated as `phase="read"`, meaning the call may have been charged, and charged operations are not retried after it by default. Raise `TLSVerificationError` for certificate failures; it is never retried.

### Example: a fake transport for tests

```python
import json
import rightsfoundry
from rightsfoundry import PreparedRequest, RawResponse

class FakeTransport:
    def __init__(self) -> None:
        self.requests: list[PreparedRequest] = []

    def send(self, request: PreparedRequest, *, connect_timeout: float,
             read_timeout: float, write_timeout: float) -> RawResponse:
        self.requests.append(request)
        body = {"success": True, "status": "alive"}
        return RawResponse(200, {"content-type": "application/json", "x-request-id": "test-1"},
                           json.dumps(body).encode())

client = rightsfoundry.Client.unauthenticated(transport=FakeTransport())
assert client.health.live().alive
```

### Example: an `httpx` async transport (in your own code; not a dependency of this package)

```python
import httpx
import rightsfoundry
from rightsfoundry import AsyncRawResponse, PreparedRequest

class HttpxAsyncTransport:
    def __init__(self) -> None:
        # Keep TLS verification on (the default) and never follow redirects.
        self._client = httpx.AsyncClient(follow_redirects=False)

    async def send(self, request: PreparedRequest, *, connect_timeout: float,
                   read_timeout: float, write_timeout: float) -> AsyncRawResponse:
        timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout,
                                write=write_timeout, pool=connect_timeout)
        built = self._client.build_request(request.method, request.url, headers=list(request.headers),
                                           content=request.body, timeout=timeout)
        try:
            response = await self._client.send(built, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise rightsfoundry.APIConnectionError(f"connect failed: {type(exc).__name__}",
                                                   phase="connect", charge_state="not_charged") from None
        return AsyncRawResponse(response.status_code, response.headers.multi_items(),
                                response.aiter_raw(), on_close=response.aclose)

    async def aclose(self) -> None:
        await self._client.aclose()

client = rightsfoundry.AsyncClient(async_transport=HttpxAsyncTransport(), max_concurrency=100)
```

Publish adapters like this one as **separate packages** (for example `rightsfoundry-httpx`), never as an extra of the core package. An extra would put the dependency into the core package's metadata and its SBOM.

## Models

- Models are frozen dataclasses (with `slots=True` on Python 3.10+). Each has `.raw`, a deep read-only view of the original JSON, and `.to_dict()`.
- Models pickle and `copy.deepcopy` cleanly, so they can cross process boundaries (`multiprocessing`, Celery, caches). `.raw` is an immutable read-only mapping whose `str()` and `repr()` redact signed-URL `sig` values; indexing it still returns the real value.
- Unknown fields are kept in `.raw`. Unknown enum values never raise.
- Nullability follows the API: every AI musical analysis field, `asset_url` and `peak_data_url` can be `None`.
- Signed URLs stay intact on the model, because your application needs them, but every `repr` redacts their `sig`.
