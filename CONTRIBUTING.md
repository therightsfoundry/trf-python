# Contributing to rightsfoundry

Thanks for your interest in improving The Rights Foundry Python SDK. This guide covers how to report issues, set up a development environment, and get a pull request merged.

By participating you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Security issues** go to [SECURITY.md](SECURITY.md), never to public issues.
- **Never paste an API key, a signed URL (`…?expires=…&sig=…`), or account details** into issues, pull requests, test fixtures or logs. Redact them. Signed URLs are bearer links to licensed audio.
- **Bugs:** open a [bug report](https://github.com/therightsfoundry/trf-python/issues/new/choose). Include the SDK version, Python version, a minimal reproduction and the `request_id` from the error message.
- **Features:** open a [feature request](https://github.com/therightsfoundry/trf-python/issues/new/choose) before writing a large change, so we can agree on the design. The SDK wraps the public REST API, so features the API does not offer belong with The Rights Foundry (info@therightsfoundry.com), not here.
- **Account, billing and licensing questions:** info@therightsfoundry.com.

## Development setup

```bash
git clone https://github.com/therightsfoundry/trf-python
cd trf-python
uv sync                 # creates .venv from uv.lock with the dev group (pytest, pytest-cov, coverage, mypy, ruff)
```

Without `uv`: `python -m venv .venv && . .venv/bin/activate && pip install -e . pytest pytest-cov coverage mypy ruff`.

`uv.lock` is committed. If you change `[dependency-groups]` in `pyproject.toml`, run `uv lock` and commit the result.

You do **not** need an API key to develop. The test suite runs against in-process fakes and shared conformance fixtures, and never calls production.

## Checks (all must pass)

```bash
uv run pytest
uv run mypy --strict src tests scripts
uv run ruff check src tests scripts examples
uv run ruff format --check src tests scripts examples
uv run pytest --cov=rightsfoundry --cov-report=term-missing --cov-fail-under=95
uv run coverage json -o coverage.json && uv run python scripts/check_module_coverage.py coverage.json
python3 scripts/check_openapi_sync.py conformance/openapi_contract.json
uv build
python3 scripts/assert_zero_deps.py dist/*.whl dist/*.tar.gz
```

Plain `uv run pytest` works without pytest-cov; coverage is opt-in. CI requires at least 95 % line coverage overall and in each of `_transport`, `_config`, `_redact`, `_retry` and `_guard`.

If you change the endpoint registry, update `conformance/openapi_contract.json` and, when the API has changed, also check against the published OpenAPI document:

```bash
curl -fsSL https://docs.therightsfoundry.com/openapi.yaml -o /tmp/openapi.yaml
python3 scripts/check_openapi_sync.py /tmp/openapi.yaml
```

CI runs the tests on Python 3.9–3.14 on Linux and 3.11–3.14 on macOS and Windows, plus lint, the coverage gate, the contract check, the build and CodeQL. CI installs its tooling from the hash-pinned files in `requirements/`. If you change `requirements/*.in`, regenerate them:

```bash
uv pip compile --generate-hashes --universal --python-version 3.12 requirements/dev.in -o requirements/dev.txt
uv pip compile --generate-hashes --universal --python-version 3.12 requirements/build.in -o requirements/build.txt
```

## Design rules

These are enforced in review. [AGENTS.md](AGENTS.md) and [docs/architecture.md](docs/architecture.md) have the details.

1. **Zero runtime dependencies.** Use the standard library only under `src/`. Optional integrations (for example an `httpx` transport) belong in separate packages, never in extras.
2. **Python 3.9 compatible syntax.** Every module starts with `from __future__ import annotations`. Use `Optional[...]` / `Union[...]` wherever annotations are evaluated at runtime.
3. **Fully typed.** `mypy --strict` must pass, and the public API is re-exported from `rightsfoundry/__init__.py`.
4. **Security invariants (SEC-01…SEC-20)** in [docs/security.md](docs/security.md) must keep holding:
   - one place unwraps the key;
   - every diagnostic string is redacted;
   - redirects are never followed;
   - no credentials go to the asset host;
   - response bodies are bounded;
   - no environment variable can weaken TLS or the host allowlist.
5. **Money safety.** New charged operations declare the correct retry class and cost key in `_endpoints.py`, and accept `max_credits=` plus the per-call options (`timeout=`, `max_retries=`, `idempotency_key=`, `send_max_credits_header=`). Changing a default that affects spending is a breaking change.
6. **Sync/async parity.** Every resource method exists on both clients with the same semantics. Decision logic lives in `PipelineCore`, not in the drivers.
7. **Models are forward-compatible.** Fields the server may omit are `Optional`, enums stay open (`str`), and `.raw` keeps everything.
8. **Adding an endpoint** follows the 4-step recipe in [docs/architecture.md](docs/architecture.md#adding-an-endpoint).

## Tests

- Every bug fix comes with a test that fails before the fix.
- Security-relevant code (`_transport`, `_config`, `_redact`, `_secret`, `_retry`, `_guard`, `_download`) needs tests for both the accept and the reject path.
- Use fake keys built at runtime in test code. Never commit a literal 128-character hex string, even a fake one, outside `tests/`.
- Never point tests at production or make charged calls in CI.

## Pull requests

- Keep each PR focused on one change. Describe what changed and why, and link the issue.
- Update `CHANGELOG.md` under **Unreleased**, plus `README.md` and `llms-full.txt` if the public surface changes.
- Fill in the PR checklist (zero dependencies, no secrets, tests, mypy, ruff).
- Changes to `.github/`, `action.yml`, the transport, the redactor, the config, `Secret` or downloads also need a review from the security owners (see `.github/CODEOWNERS`).
- New or updated GitHub Actions must be pinned to a full commit SHA, with the version in a comment.

## Releases

Maintainers release by pushing a signed `vX.Y.Z` tag on a commit that is already on `main`. The release workflow first verifies that the tagged commit is on `main` and re-runs lint, `mypy --strict`, the tests and the contract check. It then builds the sdist and wheel reproducibly with hash-pinned tooling, checks that they have zero dependencies, generates an SBOM, attests build provenance, publishes to PyPI through Trusted Publishing, creates the GitHub release, and moves the floating major tag (`v1`) that GitHub Action users reference. Do not push `v1` by hand; it does not trigger a release. Versioning follows [Semantic Versioning](https://semver.org); the policy is summarised in [CHANGELOG.md](CHANGELOG.md).

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
