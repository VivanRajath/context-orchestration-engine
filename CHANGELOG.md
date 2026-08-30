# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `POST /api/runs/stream` — starts a run and streams its events inside one
  request, for hosts that freeze an instance between invocations and route each
  request independently. The local two-request path (`POST /api/runs` plus an
  `EventSource`) is unchanged and still the default.
- `COE_SERVERLESS` and `COE_ALLOW_LIVE` environment flags. The first switches
  the playground onto the single-request path and closes live model calls; the
  second reopens them for a private deployment that carries its own keys.
- Deployment support: `api/index.py`, `vercel.json`, `.vercelignore`, and
  [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).
- `LICENSE` (MIT), `CONTRIBUTING.md`, and a GitHub Actions workflow running the
  suite on Python 3.11 and 3.12 plus an import check against the lean
  deployment dependency set.

### Changed

- `/api/config` now reports `serverless`, `live_enabled` and
  `live_disabled_reason`, so the page can pick its transport and stop offering
  a key field a deployment will not accept.
- `requirements.txt` is now the deployment dependency set. Local installs use
  `pip install -e ".[dev]"`, as the README has always said.

## [0.1.0]

Initial release. N independent LLM workers, one continuous task, structured
context across every handoff, and zero raw conversation transfers.
