# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **A gateway with no dependencies.** `gateway/http_gateway.py` speaks the
  OpenAI request shape over the standard library, with a small adapter for
  Anthropic's. It satisfies the same `LLMGateway` protocol as `LiteLLMGateway`,
  so the orchestrator cannot tell which is running, and it makes real model
  calls possible inside a serverless function. `COE_GATEWAY=litellm` selects
  the other one.
- **Provider detection and model discovery.** `gateway/providers.py` names the
  vendor that issued a key from its prefix, asks that vendor which models the
  key may call, discards the ones that could not take a worker's turn, and
  ranks the rest on published metadata: modalities, context window and whether
  the model can be held to a schema. Thirteen vendors, plus any endpoint that
  speaks the OpenAI shape.
- **A key pool.** A deployment may lend visitors free-tier keys of its own, one
  per worker, so a first run needs no account anywhere. `COE_DEMO_KEYS`, or the
  existing `WORKER_n_API_KEY` set. The keys never reach the browser.
- **One-shot mode.** `core/planner.py` turns an objective into an editable plan
  and divides a total token budget across it, weighted so later workers - which
  inherit more - may be told more. A model writes the plan where there is a key
  to ask with, and a template does it otherwise; the page says which.
- `POST /api/keys/inspect` and `POST /api/plan/split`.
- Per-step context budgets: `ContextOrchestrator(step_budgets={seq: tokens})`.

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

- **The playground is a setup flow rather than a form.** Bring a key (ours,
  yours, or none), say what you want done, see who will do it, run it. Each
  worker card now carries the checklist as it stood after that turn and the
  document handed to the next worker, verbatim.
- The run request accepts a roster assembled in the browser: per-worker vendor,
  model and credential, so one run can cross several vendors at once.
- `/api/config` reports `serverless`, `live_enabled`, `live_disabled_reason`,
  the provider catalogue, and how many keys the pool holds - never the keys.
- A finished turn folds itself away, and its header carries the result: how
  far through the plan the run is, what went into the record, and whether
  anything was refused. The turn in progress stays open, and so does a failed
  one. Five open worker cards was several thousand pixels of scrolling.
- Playground layout uses the width it is given (up to 1460px, with the setup
  side by side above 1080px and the run output in two columns above 1240px)
  instead of a fixed 1000px column.
- `vercel.json` asks for `maxDuration: 300`, because a live run is two model
  calls per worker and does not fit in sixty seconds.

- **The page is files rather than one file.** 4,200 lines of markup, style
  and behaviour in a single document became `index.html`, three stylesheets
  and nine ES modules with a one-way dependency graph. Still no build step:
  the browser resolves the imports and FastAPI serves the files.
- `tests/test_static.py` checks the page the way the rest of the suite checks
  the Python: every element the script reaches for exists, every import
  resolves, the module graph has no cycles, nothing is exported that nobody
  imports, and only the entry point touches the page at import time.
- One content width. There were eight caps stacked inside one container, so
  every section began and ended somewhere different; 26 of them are gone and
  the container decides. Headings and prose wrap on `text-wrap` instead.

### Fixed

- Three couplings the single file had hidden, all found by the split: the
  budget widget read a value it never declared, the stage list called a
  function belonging to the card renderer, and the run view wired a control
  the moment it was imported.
- A model id beginning with its own vendor's name (Groq publishes
  `groq/compound`) was reduced to `compound` before the call. The id the vendor
  published is now carried separately from the display string and never
  re-derived.
- `lvSwitchInit` referenced two elements a previous rewrite had deleted, so the
  playground threw on every page load.
- A grid track minimum wider than a phone made the whole document scroll
  sideways at 375px.
- Rate-limit backoff now reads the delay out of the provider's error text when
  the `Retry-After` header is absent, which is the common case.
- `requirements.txt` is now the deployment dependency set. Local installs use
  `pip install -e ".[dev]"`, as the README has always said.

## [0.1.0]

Initial release. N independent LLM workers, one continuous task, structured
context across every handoff, and zero raw conversation transfers.
