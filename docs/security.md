# Security

`rightsfoundry` handles two kinds of asset worth stealing:

- **Your API key.** It is account-wide: every key on an account draws from the same credit balance.
- **Signed URLs.** A `download_url` is a bearer capability: anyone holding it can fetch a licensed WAV master until it expires.

This document lists the 20 security controls (SEC-01 to SEC-20) and how this SDK implements each one. To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Threat model

**Adversaries considered:**

- a compromised or typosquatted dependency;
- a poisoned environment variable or config file;
- a network attacker (MITM, DNS spoofing);
- a malicious or compromised upstream response (oversized, deeply nested, redirecting, path-traversing file names);
- log and telemetry pipelines that collect too much;
- developers' own mistakes: pasting keys, logging clients, disabling TLS to "fix" a proxy.

**Not in scope:** the security of the API service itself. Client-side guards are advisory; the server's `402` is the authority on spending.

## Controls

| ID | Control | How this SDK implements it |
|---|---|---|
| **SEC-01** | Zero third-party runtime dependencies | `pyproject.toml` has `dependencies = []` and no optional extras. Only the standard library is imported. CI runs `scripts/assert_zero_deps.py` on the built wheel **and** sdist, then installs the wheel with `--no-deps` into a fresh venv and imports it. The SDK's own vulnerability surface is therefore its own code plus the interpreter. |
| **SEC-02** | Refuse browser execution contexts | Construction raises `ConfigurationError` on `sys.platform == "emscripten"` (Pyodide/WebAssembly) unless `dangerously_allow_browser=True`. Only code can set that option; no env var can. The README and docs say plainly: keep the key on a server. |
| **SEC-03** | API key held in a redacting `Secret` | The key is validated (exactly 128 hex characters after trimming; a pasted 4-character public code is recognised and rejected with a hint) and wrapped in `rightsfoundry.Secret` immediately. `Secret` uses `__slots__`, is immutable, and its `repr`, `str` and `format` show `[REDACTED fp=1a2b3c4d]`. Pickling raises `TypeError`, and equality is constant-time (`hmac.compare_digest`). |
| **SEC-04** | Redacted `repr` of clients, configs and errors | `Client`, `AsyncClient` and `ClientConfig` show `api_key=[REDACTED fp=…]`. Clients and configs cannot be pickled or copied, which stops a key from being cached in Redis, Celery or similar payloads. Errors carry only redacted state, so pickling an error (for `multiprocessing`) is safe. |
| **SEC-05** | Errors and stack traces never contain the key or a signature | The raw key is unwrapped in exactly one function, `_pipeline._auth_header_value()`, whose result goes straight into the outgoing header container. That container (`_RedactingHeaders`) has a redacted `repr`, so an error tracker that captures frame locals sees `[REDACTED]`. Exceptions are built from redacted strings and a redacted copy of the response body, never from the request object. Server-provided `error` and `message` text is redacted before it goes into an exception message. |
| **SEC-06** | Central redactor | Every exception message, log line, hook event and `body` attribute passes through `_redact.redact_text` / `redact_value`. These replace: `Authorization` / `Proxy-Authorization` values; the configured key (replaced by its fingerprint); any run of 128+ hex characters, including someone else's token (`[REDACTED_TOKEN]`); `sig=<value>` in any URL; userinfo in any URL; and `Bearer <token>` fragments in free text. The musical `key` query parameter is catalogue data and is left alone. |
| **SEC-07** | Signed URLs treated as secrets | `SyncLink` and `Track` keep `download_url` / `asset_url` intact because your application needs them, but their `repr` shows `sig=[REDACTED]`. `link.redacted_download_url()` and `track.redacted_asset_url()` give log-safe forms. A model's `.raw` mapping is read-only, and its `str()` and `repr()` redact `sig` values too (indexing returns the real value). The CLI prints only redacted links unless you ask for `--json`, whose output is ASCII-escaped so catalogue text cannot write control or bidirectional characters to a terminal. The GitHub Action masks the signature and redacts `result-json`. The docs tell you to store track IDs, not URLs. |
| **SEC-08** | Strict `base_url` validation | Scheme must be `https`. `http` is allowed only to `localhost` / `127.0.0.1` / `[::1]`, only with `allow_insecure_localhost=True` set in code, and only outside production. Userinfo, query and fragment are refused. Hosts must be ASCII (use punycode, which defeats homograph hosts) and are lowercased with any trailing dot stripped. IP literals are refused after normalising legacy forms (decimal, octal, hex). The host must be `api.therightsfoundry.com`, `api-staging.therightsfoundry.com` or an `allowed_hosts` entry, which can be set in code only. A poisoned `TRF_BASE_URL` therefore cannot send your key anywhere else. First-party hosts use port 443 with no path prefix. If `environment` and `base_url` disagree about a first-party host, construction fails rather than guessing. |
| **SEC-09** | Track IDs validated and path-encoded | Accepted: an `int` in 1…999,999,999,999,999 or a `str` of 1–15 **ASCII** digits. Booleans, signs, whitespace and Unicode digits are rejected (`InvalidTrackIdError`), which makes `../`, `%2e%2e`, `?`, `#` and `/` impossible in the path. The canonical string is still passed through `urllib.parse.quote(..., safe="")`. |
| **SEC-10** | Query parameters allowlisted and typed | Only the documented `GET /v1/tracks` parameters can be serialised; unknown keywords raise `TypeError`. Values are typed: booleans become `"true"`/`"false"`, numbers must be finite decimals, strings are trimmed, length-capped and free of C0/C1 control characters. Parameters are emitted in a fixed order. JSON bodies (`deep_search`, `search_similar`, `sync`) are built from typed fields, never from caller-supplied dicts, so no extra keys can be smuggled in. |
| **SEC-11** | `file_url` validated before a charged call | An unreachable reference file is **charged and not refunded**, so the SDK checks before sending. The URL must be ≤ 2000 characters, absolute `http(s)`, with a path ending `.mp3`/`.wav`. It must have no userinfo (credentials would be forwarded to the search provider), an ASCII host, no loopback/private/link-local/CGNAT/unique-local IP literals and no numeric IP encodings, must not be `localhost` or a single-label name, and must use port 80, 443, 8080 or 8443. `http://` is allowed but logs one warning per client. |
| **SEC-12** | Redirects never followed | The API never redirects and the asset host serves files directly, so any 3xx from either raises `UnexpectedRedirectError` with the redacted `Location`. This removes the classic leak in which a redirect carries `Authorization` to another host. Custom transports must not follow redirects either, and the pipeline raises on any 3xx they report. |
| **SEC-13** | Asset URLs validated; no credentials to the asset host | Before any download: `https` only, host must match `asset_hosts` (default `assets.therightsfoundry.com`), port 443, path under `/stream/` with no `.`/`..` segments or encoded dots/slashes, query keys exactly `{expires, sig}`, no userinfo or fragment, and `expires` more than 5 s in the future. The request carries **no** `Authorization`, custom headers or cookies. A URL that fails any check raises `SyncLinkInvalidError`; the SDK never "tries anyway". |
| **SEC-14** | Header-injection prevention | The key is guaranteed to be hex. `custom_headers` accepts only `X-[A-Za-z0-9-]{1,64}` names (except `X-Admin-Token`, `X-API-Key`, `X-Forwarded-*`, `X-Real-IP`, `X-Request-Id`, `X-Original-*`, `X-Http-Method-Override`, `X-Middleware-*`) and `traceparent` / `tracestate` / `baggage`. Values must be printable ASCII (no CR, LF or NUL), ≤ 1 KiB each and ≤ 8 KiB in total. An `idempotency_key` must be printable ASCII without spaces (1–255 characters). `Authorization`, `Cookie`, `Host`, `User-Agent` and similar are always refused. Custom headers are sent only to the API host. `user_agent_suffix` must be printable product tokens. |
| **SEC-15** | TLS 1.2+ with verification; no bypass | `build_ssl_context()` sets `minimum_version = TLSv1_2`, `CERT_REQUIRED` and `check_hostname = True` (asserted after construction) and disables TLS compression. The SDK uses its own context, so `PYTHONHTTPSVERIFY=0` and a monkey-patched `ssl._create_default_https_context` have no effect; the latter is detected and logged as an error. There is no `verify=False`. `ca_bundle` replaces the system trust store with the given bundle and keeps verification on. Certificate failures raise `TLSVerificationError` and are never retried. Proxies use `CONNECT`, so TLS to the API stays end to end: `http://` proxies get a plain `CONNECT`; `https://` proxies get TLS to the proxy (verified), then `CONNECT`, then TLS to the API inside the tunnel. Any other proxy scheme (for example `socks5://`) raises `ConfigurationError` naming its source (`proxy_url` or `HTTPS_PROXY`), rather than being ignored. |
| **SEC-16** | Bounded response bodies and decompression | `max_response_bytes` (default 10 MiB) is checked against `Content-Length` before reading, then against **decompressed** bytes chunk by chunk. Gzip and deflate are inflated with `max_length`, so a decompression bomb cannot allocate past the limit. Over-limit responses raise `ResponseTooLargeError`. Downloads stream in 64 KiB chunks under `max_download_bytes` (2 GiB), so memory use stays constant. Read timeouts reset per chunk, and `total_timeout` bounds a slow trickle response. |
| **SEC-17** | Safe deserialisation | `json.loads` with a `parse_constant` that rejects `NaN` and `Infinity`; strict UTF-8; nesting depth ≤ 64, checked iteratively. There is no `pickle`, `yaml` or `eval`. Models are built by explicit field extraction with type checks, and unknown fields are kept read-only in `.raw`. |
| **SEC-18** | Safe file writes for downloads | The temp file is created exclusively (`O_CREAT \| O_EXCL`, plus `O_NOFOLLOW` where available) with mode `0600` in the destination directory, then `fsync`ed and atomically renamed. Symlinks at the destination, the directory or the partial file are refused. An existing file is never overwritten unless `overwrite=True`. Server file names are sanitised to a basename of `[A-Za-z0-9._-]` with no leading dot and ≤ 255 bytes, falling back to `<track_id>.wav`. The SDK never writes anywhere you did not name. With `resume=True`, the hidden partial file's name includes a hash of the URL path, so a partial from one track is never extended with another track's bytes, and if a ranged resume fails the partial is truncated and the download retried once in full. |
| **SEC-19** | Fork safety, resource cleanup, CSPRNG jitter | `os.register_at_fork(after_in_child=…)` drops pooled connections in forked children, so TLS sessions are never shared across processes. Retry jitter uses `secrets.SystemRandom`, so retries from many processes are not correlated. Deadlines use `time.monotonic()`; only `expires` and `Retry-After` dates use wall-clock time. Idle pooled connections expire. There is no telemetry: the SDK talks only to the configured API host and, for downloads, the asset host. |
| **SEC-20** | CI and release supply chain | See [Supply chain](#supply-chain) below. |

## Secret fingerprinting

```
fp = sha256("rightsfoundry-key-fp:" + api_key).hexdigest()[:8]
```

The fingerprint is stable and cannot be reversed: it is 32 bits of a hash of a 512-bit secret, and it differs from any server-side token hash. It appears wherever the key would otherwise be shown (`[REDACTED fp=1a2b3c4d]`). The same fingerprint is shown next to the key's public code at [therightsfoundry.com/dashboard/token](https://therightsfoundry.com/dashboard/token), so you can confirm which key a production process uses without revealing it:

```python
client = rightsfoundry.Client()
print(client.api_key_fingerprint)   # e.g. "1a2b3c4d"
```

## Supply chain

- **Zero runtime dependencies** (SEC-01), enforced on every pull request, every Dependabot PR and every release.
- **GitHub Actions hardening:**
  - every action is pinned to a full 40-character commit SHA, with the version in a trailing comment;
  - workflows start from `permissions: {}`, and each job grants only what it needs;
  - `persist-credentials: false` on every checkout;
  - no `pull_request_target`;
  - no `${{ github.event.* }}` or `${{ inputs.* }}` inside `run:` scripts (values pass through `env:`).
- **Hash-pinned tooling:** CI and release install ruff, mypy, pytest, coverage, `build` and hatchling from `requirements/*.txt` with `pip install --require-hashes`, and build with `python -m build --no-isolation`, so no unpinned build backend is downloaded.
- **Static analysis:** CodeQL (`security-extended`, skipped on fork pull requests, which cannot upload results), ruff including the Bandit-derived `S` rules, `mypy --strict`.
- **Coverage gate:** at least 95 % line coverage overall and in each of `_transport`, `_config`, `_redact`, `_retry` and `_guard`.
- **Contract check:** `scripts/check_openapi_sync.py` compares the endpoint registry with the vendored `conformance/openapi_contract.json` on every pull request.
- **Release:**
  - triggered only by an exact version tag (`vX.Y.Z` or `vX.Y.Z-…`); the floating major tag never matches;
  - a `verify` job refuses a tag whose commit is not on `main`, then runs ruff, `mypy --strict`, the test suite and the contract check;
  - the build job runs with `contents: read` and builds the sdist and wheel with `SOURCE_DATE_EPOCH` set to the tagged commit's time;
  - the publish job runs in the protected `release` environment and uploads to PyPI through **Trusted Publishing** (OIDC; no stored API tokens), with **PEP 740 attestations**;
  - **GitHub build provenance** is attested for both files;
  - a CycloneDX SBOM (expected to list zero runtime components) is attached to the GitHub release;
  - for a final release, the floating major tag (for example `v1`, used by the GitHub Action) is moved to the released commit.
- **Verify a release:**

  ```bash
  pip download rightsfoundry==1.0.0 --no-deps -d dist/
  gh attestation verify dist/<file> --repo therightsfoundry/trf-python
  ```

- **Dependabot** updates the GitHub Actions SHAs (grouped weekly) and the development tooling. There are no runtime dependencies to update.

## Residual risks (documented, not solved)

- **The key is plaintext in process memory** and in the environment variable it came from. A memory dump, or a co-tenant reading `/proc/<pid>/environ`, exposes it. For long-running services, read the key from a secret manager and pass `api_key=` explicitly, so it is not inherited by child processes through the environment.
- **Frame-locals capture.** Error trackers configured to capture local variables could, in principle, capture the transport frame. The SDK's header container reprs as redacted and the transport does not keep the header value bound after sending; keep your tracker's default scrubbing of `authorization` turned on anyway.
- **Injected transports** may log headers or disable TLS verification. That is the integrator's responsibility, and the SDK warns when a custom transport is used in production.
- **Client-side guards are advisory.** Rate cards can change between the projection and the charge, and other processes share the same balance. The server's `402` is the authority.
- **Signed URLs you hand out** (for example preview URLs sent to browsers) can be shared until they expire, and preview plays are metered.

## Using the SDK securely

1. Keep `TRF_API_KEY` on the server. Never put it in browser, mobile or desktop client code, logs, issues or prompts.
2. Hand browsers only metadata and preview `asset_url`s. Never hand them `download_url`s you do not intend to give away.
3. Store track IDs, not signed URLs.
4. Cap spending with `max_credits_per_request` / `max_credits=` and iterator budgets.
5. Do not inject a transport that disables TLS verification, and never pass `dangerously_allow_browser=True`.
6. If a key may have leaked, replace it from [/dashboard/token](https://therightsfoundry.com/dashboard/token) or contact info@therightsfoundry.com.
