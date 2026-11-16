# GitHub Action: `therightsfoundry/trf-python`

This action lets a workflow search The Rights Foundry catalogue, check credits, and license WAV sync masters. It runs the same SDK as the Python package, from the action's own checkout, with no `pip install` and no third-party code. The runner needs `python3` 3.9 or newer, which GitHub-hosted runners include; the action stops with a clear error on an older interpreter. Self-hosted runners can add [`actions/setup-python`](https://github.com/actions/setup-python).

```yaml
- id: search
  uses: therightsfoundry/trf-python@v1   # pin to a full commit SHA in production
  with:
    action: deep-search
    api-key: ${{ secrets.TRF_API_KEY }}
    query: "warm hopeful acoustic guitar for a nature documentary"
```

## Inputs

| Input | Used by | Default | Description |
|---|---|---|---|
| `action` | all | — (required) | `deep-search` \| `search` \| `search-similar` \| `track` \| `sync` \| `credits` \| `health` |
| `api-key` | all except `health` | `""` | Your API key. **Always** pass it from a secret: `${{ secrets.TRF_API_KEY }}`. |
| `environment` | all | `production` | `production` or `staging` (staging keys work only on staging) |
| `query` | `search`, `deep-search` | `""` | Free text for `search` (lexical) or the natural-language brief for `deep-search` (required there) |
| `track-id` | `track`, `sync` | `""` | Track ID (1–15 digits) |
| `file-url` | `search-similar` | `""` | Public `https` URL of a reference `.mp3`/`.wav`. **An unreachable file is charged.** |
| `genre`, `mood`, `instrument` | `search` | `""` | Exact tag filters (see `rightsfoundry taxonomies`) |
| `vocals` | `search` | `""` | `instrumental` \| `male` \| `female` \| `any` |
| `bpm-min`, `bpm-max` | `search` | `""` | BPM range |
| `duration-min`, `duration-max` | `search` | `""` | Duration range in seconds |
| `instrumental` | `deep-search`, `search-similar` | `false` | `true` to return instrumental tracks only |
| `limit` | searches | `20` | Results per page, 1–100 |
| `external-export` | `sync` | `false` | `true` for an Export Sync licence (offline use / outside the platform), charged at `export_sync` |
| `download-path` | `sync` | `""` | File or directory to save the WAV master to (a path ending in `/` is created). An existing file is overwritten. |
| `max-credits` | charged actions | `""` | Refuse the call client-side if its projected cost exceeds this many credits |

## Outputs

| Output | Set by | Description |
|---|---|---|
| `result-json` | all | The API response as JSON, **with signed-URL signatures redacted** |
| `track-id` | searches, `track`, `sync` | The licensed track, or the top search result |
| `track-title` | searches, `track`, `sync` | Title of that track (untrusted catalogue text; see below) |
| `track-artist` | searches, `track`, `sync` | Artist of that track (untrusted catalogue text) |
| `downloaded-file` | `sync` | Path of the downloaded WAV master (empty without `download-path`) |
| `credits-charged` | `sync` | Credits charged (`0` on a free reissue inside the 1-hour window) |
| `credits-spendable` | `credits` | Spendable API credits (balance + overdraft) |
| `request-id` | all | `X-Request-Id` of the API response; quote it to support |

Exit status:

- `0`: success.
- `1`: API or network error. For API errors, the `::error::` line includes `[charge_state=…]`.
- `2`: invalid input.
- `health`: fails the step when the API is not healthy.

A summary table is written to the job's step summary.

## Billing

| Action | Charged at |
|---|---|
| `credits`, `health` | free |
| `search` | `tracks_list`, or `tracks_search_first_page` with a `query` |
| `deep-search` | `deep_search_first_page` |
| `search-similar` | `file_search_first_page` (also charged when the file is unreachable) |
| `track` | `track_detail` |
| `sync` | `sync`, or `export_sync` with `external-export: true`. Free if this account already holds a live link for the same track and mode. The download is free. |

Use `max-credits` on every charged step. Read your live prices with the `credits` action; the action never hard-codes them.

## Recipes

### Search, license and keep the master as an artifact

A complete version is in [`examples/github_workflow_soundtrack.yml`](../examples/github_workflow_soundtrack.yml).

```yaml
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
          query: ${{ inputs.brief }}          # fine in `with:`; never put it in `run:`
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

      - uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4
        with:
          name: soundtrack-master
          path: ${{ steps.sync.outputs.downloaded-file }}
          retention-days: 3
```

### Fail a pipeline early when credits run low

```yaml
- id: credits
  uses: therightsfoundry/trf-python@v1
  with:
    action: credits
    api-key: ${{ secrets.TRF_API_KEY }}

- name: Require a minimum balance
  env:
    SPENDABLE: ${{ steps.credits.outputs.credits-spendable }}
  run: |
    if [ "${SPENDABLE}" -lt 500 ]; then
      echo "::error::Only ${SPENDABLE} credits left; top up at https://therightsfoundry.com/dashboard/billing"
      exit 1
    fi
```

### Scheduled health probe (no key, free)

```yaml
on:
  schedule:
    - cron: "*/30 * * * *"
permissions: {}
jobs:
  probe:
    runs-on: ubuntu-24.04
    steps:
      - uses: therightsfoundry/trf-python@v1
        with:
          action: health
```

### Use a result in a later script

Pass outputs through `env:`. Never interpolate them into `run:`.

```yaml
- name: Use the licensed track
  env:
    TRACK_ID: ${{ steps.sync.outputs.track-id }}
    MASTER: ${{ steps.sync.outputs.downloaded-file }}
  run: ./scripts/render.sh --audio "${MASTER}" --track-id "${TRACK_ID}"
```

## Security notes

- **Secrets.** Store the key as an encrypted repository or environment secret and pass it with `api-key: ${{ secrets.TRF_API_KEY }}`. Never hard-code it in a workflow file. Prefer a GitHub **environment** with required reviewers for workflows that run `sync`, because `sync` spends money. Workflows triggered from forks do not receive secrets; keep it that way, and do not use `pull_request_target` to work around it.
- **Masking.** The action masks the API key with `::add-mask::` as soon as it reads it, and masks the signature of every sync link it mints. `result-json` and the step summary pass through the SDK's redactor, so signed URLs appear as `…&sig=[REDACTED]`.
- **Stop-commands.** Everything the action prints runs inside a `::stop-commands::<random token>` block, so a track title or artist containing `::set-output::` or `::add-mask::` cannot issue workflow commands.
- **Untrusted outputs.** `track-title`, `track-artist` and `result-json` contain catalogue text. Outputs are written with random heredoc delimiters, so that text cannot inject other output keys. When you consume them, pass them through `env:` and quote them (`"${TRACK_TITLE}"`); never write `${{ steps.x.outputs.track-title }}` inside a `run:` script.
- **Inputs.** Inside the action, every input reaches the program through environment variables only; nothing is interpolated into a shell. Python runs in isolated mode (`python3 -I`), which ignores `PYTHON*` environment variables and keeps the working directory (an untrusted checkout) off `sys.path` on every supported version, so a file in your repository named like a stdlib module or `rightsfoundry/` cannot be imported by the action. The action adds only its own `src/` directory to the path.
- **Pin by SHA.** `@v1` is a moving tag that each final 1.x release moves to its own commit. In production, pin the full commit SHA of a release, and let Dependabot (`package-ecosystem: github-actions`) propose updates:

  ```yaml
  uses: therightsfoundry/trf-python@<40-character-commit-sha> # v1.0.0
  ```

- **Least privilege.** The action needs no `GITHUB_TOKEN` permissions. Use `permissions: contents: read`, or `permissions: {}` when you do not check out code.
- **Licensed files.** A downloaded WAV master is licensed content. Keep artifacts short-lived and private, and do not commit masters to the repository.
