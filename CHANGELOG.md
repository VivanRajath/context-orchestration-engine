# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The visit count.** The page says how many people have opened it, counted
  on the server, once per browser session rather than once per page load. It
  is not a row in the run database: that database lives under `/tmp` on a
  serverless host, which is per-instance and vanishes with the instance, so a
  total kept there would restart at zero several times a day. Point
  `COE_COUNTER_URL` and `COE_COUNTER_TOKEN` at a Redis over HTTP (or add
  Vercel's KV integration, whose own variable names are read too) and the
  figure is durable. Without one the endpoint reports `durable: false` and the
  page hides the badge rather than showing an undercount as the total.
- **`GET /api/health`.** What an instance is, from inside it: Python version,
  installed package versions, whether the static tree arrived and which files
  are in it, the deployment flags, and which store the counter found. No key.
- **The questions fold away.** Fourteen answers laid out flat is a wall, and
  the part a reader scans is the questions, so closed, the section is exactly
  that list. They open independently, and the list runs single file: opening
  an item in a two-column grid shoves the other column down, which moves the
  thing you were not reading.
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

- `POST /api/runs/stream` starts a run and streams its events inside one
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

### Changed

- The page no longer names the source tree. Which file a class lives in, how
  many tests cover it and which database happens to be underneath are all true
  and all somebody else's business on a page whose job is to explain a way of
  working. The roadmap went with them.
- No em dashes anywhere in the project. One mark standing in for a colon, a
  comma, a semicolon, a full stop and a pair of brackets makes the reader work
  out which was meant; 83 of them are now the punctuation they stood in for.
- The deploy requirements are pinned rather than floored. `>=` resolves against
  whatever the index holds that morning, so the bundle is assembled differently
  on every deploy and an overnight release can break a site nobody touched.
  `pyproject.toml` keeps the open ranges for people installing the library.

### Fixed

- **Every URL answered 404, including the page.** `vercel.json` rewrote
  `/(.*)` to `/api/index` so that every path reached the function. The host
  used to pass the app the path the browser asked for and now passes the
  rewritten one, so the app saw `/api/index` for every request and matched no
  route. The rewrite is gone: a destination that is one fixed string discards
  the path, so there is nothing to recover inside the app, and routing belongs
  to the framework. A test fails any catch-all rewrite whose destination cannot
  carry the path through.
- **The site went down without a commit being pushed.** `fastapi` was declared
  in the `web` extra rather than in `[project] dependencies`, which was fine for
  as long as the host installed from `requirements.txt`. Vercel's builder began
  reading `pyproject.toml` instead, an extra is not a dependency, and every
  request became `ModuleNotFoundError: No module named 'fastapi'`. The installed
  set is now what the playground actually needs, `requirements.txt` pins the
  same set for hosts that read it, and a test derives the requirement from the
  source: every module-level third-party import has to appear in
  `[project] dependencies`, and the web path has to be installable from either
  file. It fails against the declaration that broke the site.
- **LiteLLM moved to an extra.** It was in the installed set, so a serverless
  build carried boto3, botocore and aiohttp for a code path taken only by
  someone who asked for it. The built-in gateway is written on the standard
  library precisely so this can be a choice:
  `pip install "context-orchestration-engine[litellm]"`.
- **The host could not find the application.** Guarding the app's
  construction put `app = ...` inside a `try`, and the host reads this file to
  locate the application rather than importing it, so an indented assignment is
  one it cannot see: `Found main.py, api/index.py but none define a top-level
  app`. The guard now lives in a function and the assignment is back at the top
  level, checked by parsing the file the same way the host does.
- **One missing file could take the whole deployment down.** `StaticFiles`
  checks its directory in the constructor, and the constructor runs while the
  module is being imported, so a build that did not carry `web/static` did not
  lose its stylesheet, it lost the site: every URL answered with the host's own
  crash page. `check_dir` is off, a missing asset costs the routes that serve
  assets, and the document explains itself instead of throwing. The entry point
  now wraps the whole construction as well: if the app cannot be built, a plain
  ASGI app serves the traceback, the installed packages and the project's own
  files, because a dead deployment should say why rather than leaving the reason
  in a log the person looking at the site cannot read. That page is what found
  the missing `fastapi`. Its first version listed every file under the root, and
  the answer was buried under four hundred lines of a vendored boto3; it now
  reports installed distributions first and skips the vendored tree.
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
