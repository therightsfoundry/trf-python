# Billing and retries

How money moves through The Rights Foundry API, and how `rightsfoundry` keeps you from spending it by accident. The API reference at [docs.therightsfoundry.com](https://docs.therightsfoundry.com) is authoritative for server behaviour. This page describes the SDK's side of it.

## Prepaid credits

- Charged calls draw from your account's prepaid **API credit** balance. Top up at [/dashboard/billing](https://therightsfoundry.com/dashboard/billing), or add credits through an optional monthly plan.
- New accounts can claim free beta credits: promotional welcome credits, granted when you save a payment method on your account. If you use them up inside the promotion window, the account moves to the Starter Beta monthly plan unless you cancel that transition. See current plan and auto-conversion terms on [therightsfoundry.com](https://therightsfoundry.com/).
- Balances are **per account**: every key on the account draws from the same pool.
- `api_credits.spendable` = `balance + overdraft_limit`. The balance may go negative within your account's overdraft, and a top-up pays any debt first.
- If a call would take the account past its overdraft floor, the server answers `402`, raised as `InsufficientCreditsError`. Its `.api_credits` holds the balance before the refused charge, and nothing is deducted.
- **Prices are never printed in the SDK or its docs.** Read your own live rate card, which already includes plan discounts:

```python
credits = client.credits.retrieve()     # free and not counted
credits.costs                           # e.g. {"tracks_list": …, "sync": …, …}
credits.standard_costs                  # list prices before your plan's discounts
credits.discounts                       # which elements your plan discounts
credits.expiring_credits                # batches about to expire
```

## Sync vs stream: the two directions of charge

| Audio | Getting the link | Moving the bytes |
|---|---|---|
| **WAV master**: `POST /v1/tracks/{id}/sync` (`client.tracks.sync`) | **Charged** at `sync` (or `export_sync` with `external_export=True`) for a new window. Reissues within a paid window are free. | **Free.** Download a link you hold as many times as you like while it is valid. |
| **MP3 preview**: `asset_url` on every catalog response | **Free.** It comes with the listing, search or detail call you already paid for. | **Intended to be billed per stream (`costs.stream`) from API credits in a future update** (metered today, not yet deducted). |

**Sync pays when the link is issued, not when the file transfers.**

- The first `tracks.sync(id)` opens a 1-hour window and is charged.
- Any repeat for the same account, track and mode while the window is open returns `credits_charged == 0`, `reissued is True`, and the **original** `expires_at`. Asking again never extends access.
- Standard sync and Export Sync (`external_export=True`) have separate windows.
- After expiry, a new sync is charged again.

**Stream charges the other way: the link is free, and playback is what is intended to be billed.**

- Preview plays are metered today but not yet deducted. Stream billing from API credits is intended for a future update and will not need a new API version. `costs.stream` is already published.
- Plan for it now: treat every preview play as a metered event, and do not auto-play previews nobody asked for.

## Cost keys

`rightsfoundry.PUBLIC_COST_KEYS` lists every key `GET /v1/credits` publishes. The SDK projects a call's cost by picking the same key the server will charge:

| Operation | Condition | `costs` key |
|---|---|---|
| `tracks.list` / `tracks.iter` | `search` absent or whitespace-only (any page) | `tracks_list` |
| `tracks.list` / `tracks.iter` | non-empty `search`, page 1 | `tracks_search_first_page` |
| `tracks.list` / `tracks.iter` | non-empty `search`, page ≥ 2 | `tracks_search_next_page` |
| `tracks.get` | the track exists (a 404 is free) | `track_detail` |
| `tracks.deep_search` / `deep_search_iter` | page 1, **every time it is sent** | `deep_search_first_page` |
| `tracks.deep_search` / `deep_search_iter` | page ≥ 2, at the next-page rate from `GET /v1/credits` | `deep_search_next_page` |
| `tracks.search_similar` / `search_similar_iter` | page 1, **every time it is sent** | `file_search_first_page` |
| `tracks.search_similar` / `search_similar_iter` | page ≥ 2, at the next-page rate from `GET /v1/credits` | `file_search_next_page` |
| `tracks.sync` | new standard window | `sync` |
| `tracks.sync(..., external_export=True)` | new Export Sync window | `export_sync` |
| preview playback (`asset_url`) | every stream | `stream`: intended to be billed from API credits in a future update (metered today, not yet deducted) |
| `credits.retrieve`, `taxonomies.list`, `health.*`, `sync_links.download` | always | free |

Notes:

- `sync` is projected at full price even when a reissue would be free, because the client cannot know whether a window is live.
- AI searches charge page 1 again each time it is sent, even when the result is cached. Keep the search fixed and page through it, or ask for a large first page.
- If the rate card has no price for a key (an older API, or a key added later), the projection is *unknown*. A guard in `"closed"` mode then refuses the call (`CreditGuardUnavailableError`).

## Guardrails

| Option | Effect | Error (nothing is sent) |
|---|---|---|
| `max_credits_per_request` (client) / `max_credits=` (per call) | Refuses a call whose projected cost is above the cap | `CreditCapExceededError` |
| `auto_check_credits=True` | Preflight: estimated spendable minus the cost must stay ≥ 0 (snapshot refreshed every `credit_check_ttl_ms`) | `PreflightInsufficientCreditsError` |
| `min_bandwidth_bytes` | Preflight floor on `bandwidth_bytes.spendable` | `PreflightInsufficientCreditsError(resource="bandwidth")` |
| `guard_failure_mode="closed"` | Refuses when prices cannot be read (default) | `CreditGuardUnavailableError` |
| iterator `max_credits=` | Budget for a whole walk, projected page by page | `CreditBudgetExceededError` |
| `low_credit_warning_threshold` + `on_low_credit` | Edge-triggered callback when spendable drops below the threshold | — |

- **`send_max_credits_header=True`** (per call, or in `CallOptions`) with a cap in force also sends `X-TRF-Max-Credits: <cap>`, so the server can enforce the same cap once it supports the header. It is forward-compatible and ignored by the API today; the client-side cap above still applies.
- **Iterators are single-use.** A second loop over a consumed iterator raises `IteratorConsumedError` instead of paying for every page again. Call `tracks.iter(...)` again to start a new, separately budgeted walk.

When a cap, budget or preflight is active, the rate card is cached for `rate_card_ttl_ms` (60 s) and fetched with the free `GET /v1/credits`. These guards are advisory: the server's `402` is the authority, and other processes using the same account draw from the same balance.

## `charge_state`

Every error from the SDK carries `charge_state`:

| `charge_state` | Meaning | When |
|---|---|---|
| `not_charged` | Nothing was deducted | Client-side errors (`ValidationError`, `CreditGuardError`, `ConfigurationError`); `400` (except below), `401`, `402`, `403`, `404`, `405`, `410`, `429`; `BillingUnavailableError` (`503`, request not served; a pending deduction is cancelled, which the API docs call "deduction unconfirmed"); connect-phase failures; any error from a free operation; download errors |
| `refund_pending` | A charge was recorded and a best-effort refund is queued | `500 InternalServerError` on charged operations; `503 SearchUnavailableError` |
| `charged` | You paid and no refund is coming | `UnreachableFileError`: Acoustic Similarity Search could not download or decode `file_url`. Also a charged operation's `2xx` whose JSON could not be turned into a model (`UnexpectedResponseError`). |
| `unknown` | The call may or may not have been charged | Read timeouts, resets after the request was written, `408`, `502`, `504`, non-JSON error bodies (e.g. an edge HTML page), redirects |

When the state is `unknown` or `refund_pending` and the ledger matters, call `client.credits.retrieve()` to reconcile.

## Retry matrix

The API does **not honour idempotency keys yet**, and catalogue and AI-search routes charge **before** doing the work. Blindly retrying a call that succeeded on the server would charge twice. Every operation therefore has a retry class:

- **`free`**: `taxonomies.list`, `credits.retrieve`, `health.*`.
- **`sync`**: `tracks.sync`. It is safe to retry, because a repeat inside the window is free.
- **`charged`**: `tracks.list`, `tracks.get`, `deep_search`, `search_similar`, and any `client.request()` path the registry does not know.

The SDK retries only when the server guarantees the money outcome:

| Outcome | What the server did about money | `free` | `sync` | `charged`, `refund_backed` (default) | `charged`, `never` | `charged`, `always` |
|---|---|---|---|---|---|---|
| Connect-phase failure (request never written) | nothing | retry | retry | retry | retry | retry |
| `429` | not charged | retry* | retry* | retry* | retry* | retry* |
| `503` billing unavailable | not served; pending deduction cancelled | retry | retry | retry | no | retry |
| `503` search unavailable | refunded best-effort | — | — | retry | no | retry |
| `500` | refund queued best-effort | retry | retry | retry | no | retry |
| `408`, `502`, `504` | unknown | retry | retry | **no**† | no | retry |
| Read timeout / reset after send | unknown | retry | retry | **no**† | no | retry |

\* subject to `max_retry_after_ms`.

† retried when the call carries an `idempotency_key=` (see [Idempotency keys](#idempotency-keys-forward-compatible)).

- `max_retries=` on a single call replaces the client's `max_retries` for that call. It changes how many retries are allowed, never which outcomes may be retried.
- **Health probes default to `max_retries=0`.** A degraded `503` comes back at once as `HealthStatus(status="degraded")` (or a not-ready `ReadinessStatus`) instead of waiting out `Retry-After`. Pass `max_retries=2` to retry.

**Never retried:** `400`, `401`, `402`, `403`, `404`, `405`, `410`, `TLSVerificationError`, `ResponseTooLargeError`, `UnexpectedRedirectError`, and every client-side validation or guard error.

`refund_backed` retries `500`/`503` on charged operations because those statuses carry an explicit server-side refund or cancellation. Until the asynchronous refund lands, the retry may briefly show as a double deduction. The eventual success is marked `retried_after_refundable_failure=True` on `TrackPage` / `SyncLink`, so reconciliation code can explain it.

**`sync` retries ambiguous outcomes** because the server records each paid window under a per-(account, track, mode) lock. A retried sync either reissues for free or, if the first attempt rolled back, pays once.

`charged_retry_policy="always"` trades the risk of paying twice for availability. Choose it deliberately, for example for cheap listing calls in a batch job where a duplicate charge is acceptable.

## Backoff and `Retry-After`

```
attempt n (n = 1 for the first retry):
  base  = min(retry_max_delay_ms, retry_initial_delay_ms * 2^(n-1))    # defaults: 500 ms → 8 s
  full  : delay = uniform(0, base)               # default; CSPRNG
  equal : delay = base/2 + uniform(0, base/2)
  none  : delay = base
  if Retry-After is present and valid:
      if retry_after > max_retry_after_ms: stop and raise the typed error with .retry_after set
      delay = max(delay, retry_after)            # never sooner than the server asked
  if total_timeout would be exceeded: stop and raise the last error
```

- `Retry-After` is accepted as delta-seconds (`^\d{1,9}$`) or an IMF-fixdate HTTP date. A date in the past means 0, and anything else is ignored.
- The API sends `Retry-After: 5` on billing and health `503`s, so a retried billing `503` waits exactly 5 s with the defaults. Health probes are not retried unless you pass `max_retries=`.
- The daily cap on MCP connections (`429`, `Retry-After` = seconds until 00:00 UTC, at least 60) exceeds the 30 s default ceiling, so the SDK **does not sleep**. It raises `DailyCallLimitError` with `.retry_after`, `.limit` and `.reset_at` and lets your application decide. Dashboard API keys have no daily cap.
- With the defaults (2 retries, full jitter), the worst-case added latency before any `Retry-After` is about 1.5 s.
- `max_retries` (default 2, env `TRF_MAX_RETRIES`) bounds the total number of retries.

## Single-flight sync

Concurrent `client.tracks.sync(id, external_export=x)` calls for the same `(id, x)` on **one client** are merged into one HTTP request, and every caller receives the same `SyncLink` (or the same error). The sync client uses a `concurrent.futures.Future`; the async client uses a shared task, shielded so that cancelling one waiter does not cancel the others.

The server already serialises first requests under an advisory lock, so this is defence in depth that also saves round trips. Across processes or machines, the server-side lock is what protects you. To check whether a call was a free reissue, look at `link.reissued` and `link.credits_charged`.

## Idempotency keys (forward-compatible)

Every typed method accepts `idempotency_key=` (and `client.request()` takes it directly or in `CallOptions`). The key is 1–255 printable ASCII characters without spaces and is sent as an `Idempotency-Key` header.

- **When a key is supplied, ambiguous outcomes are retried** (`408`, `502`, `504`, and read-phase connection failures), even under `charged_retry_policy="refund_backed"`. The same key is sent on every attempt. As soon as the API honours the header, these retries become fully safe: the server will recognise the repeat and charge once.
- **The API ignores the header today.** Until it honours it, a retried key-carrying call whose first attempt actually succeeded can be charged twice. Use a key where that risk is acceptable, or reconcile with `client.credits.retrieve()` after an ambiguous failure.
- Use one key per logical operation (for example an order or job ID), never one key for different requests.

When the API documents support, the SDK's defaults may change in a major release, as described in the versioning policy in [CHANGELOG.md](../CHANGELOG.md).
