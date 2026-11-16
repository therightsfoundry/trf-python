# Security policy

Thank you for helping keep `rightsfoundry` and its users safe.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues, discussions or pull requests.**

Report privately through either channel:

- **GitHub private vulnerability reporting:** [Report a vulnerability](https://github.com/therightsfoundry/trf-python/security/advisories/new) (Security tab → *Report a vulnerability*)
- **Email:** security@therightsfoundry.com

The Rights Foundry's machine-readable contact policy is at https://therightsfoundry.com/.well-known/security.txt.

Please include:

- the affected version (`python -c "import rightsfoundry; print(rightsfoundry.__version__)"`) and Python version;
- a description of the issue and its impact;
- steps or a minimal proof of concept to reproduce it;
- any `request_id` values involved, if the issue involves API calls.

**Never include a real API key or a live signed URL in a report.** If a key may have been exposed, replace it from https://therightsfoundry.com/dashboard/token and tell us which key fingerprint (`[REDACTED fp=…]`) was affected.

## What to expect

We aim to:

| Step | Target |
|---|---|
| Acknowledge your report | within 3 business days |
| Give an initial assessment and severity | within 10 business days |
| Ship a fix for critical or high severity issues | as fast as practical |
| Disclose publicly, in coordination with you | within **90 days** of the report, or earlier once a fix is released |

We will keep you informed while we work on the issue, credit you in the advisory if you wish, and publish a GitHub Security Advisory (requesting a CVE where appropriate) together with the fixed release. If a fix needs more time, we will agree a revised disclosure date with you before the 90 days are up.

When researching, please act in good faith:

- only test against accounts and data you own;
- do not degrade the service for others;
- do not access, modify or keep other users' data;
- give us reasonable time to fix the issue before disclosing it.

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | ✅ security fixes |
| < 1.0 | ❌ |

After a new minor release, the previous minor line receives security fixes for 12 months. Python versions are supported while they are supported upstream, plus a 6-month grace period announced in the [changelog](CHANGELOG.md). Python 3.9 is already past upstream end-of-life and support for it will be dropped in a future minor release.

## Scope

**In scope:** this repository and the `rightsfoundry` package on PyPI, including:

- leakage of the API key or of signed URLs through `repr`, logs, hooks, exceptions, pickles, CLI or GitHub Action output;
- TLS verification bypasses, redirect following, or credentials sent to the wrong host;
- SSRF, path traversal, header injection or unsafe file writes in downloads;
- unbounded memory or CPU use from hostile responses (size, compression, nesting);
- retry or guard behaviour that charges credits contrary to the documented rules;
- workflow-command or output injection in the GitHub Action;
- supply-chain issues in the build and release workflows.

**Report elsewhere (still to security@therightsfoundry.com):** vulnerabilities in The Rights Foundry API, the website, the dashboard or the MCP connector. These are outside this repository but in scope for The Rights Foundry.

**Out of scope:**

- issues that require a compromised machine, a malicious interpreter, or an integrator deliberately disabling protections (for example a custom transport that turns off TLS, or `dangerously_allow_browser=True`);
- the documented residual risks in [docs/security.md](docs/security.md);
- findings from automated scanners without a demonstrated impact.

See [docs/security.md](docs/security.md) for the controls this SDK implements (SEC-01 to SEC-20) and how to verify release provenance.
