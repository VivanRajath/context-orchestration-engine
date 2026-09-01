# Context Orchestration Engine

**N independent LLM workers. One continuous task. Structured context survives every handoff.**

[![CI](https://github.com/VivanRajath/context-orchestration-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/VivanRajath/context-orchestration-engine/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A model-agnostic orchestration system in which any number of independent LLM
workers work sequentially on the same task, without passing the conversation
history between them.

Context here is not something a model owns. It is external, portable execution
state owned by the engine.

> Models can change. Workers can change. The state of work should persist.

---

## The problem

An LLM's working context dies with its session. When one worker stops and
another takes over, the naive fix is to replay the entire conversation into the
next model. That approach degrades quickly:

- context windows fill up
- token cost grows with every handoff
- irrelevant history accumulates
- switching models becomes expensive
- the details that actually matter get buried in transcript

Summarization is the usual next attempt, and it is not enough either. A summary
is lossy in exactly the places that hurt most:

- decisions and the reasons behind them
- attempts that already failed
- dependencies between pieces of work
- the precise current execution state
- unresolved problems
- the exact next action

This project takes a different position: **treat the state of work as
infrastructure, independent of the model performing the work.**

---

## Full conversation transfer vs. structured execution handoff

```
FULL CONVERSATION TRANSFER                 STRUCTURED EXECUTION HANDOFF

Worker 1  ->  [ 8k tokens of chat ]        Worker 1  ->  writes a Handoff Report
Worker 2  ->  [ 16k tokens of chat ]                     engine reconciles claims
Worker 3  ->  [ 27k tokens of chat ]                     engine updates canonical state
Worker 4  ->  [ 41k tokens of chat ]       Worker 2  ->  receives a COMPILED PACKAGE
Worker 5  ->  [ context overflow  ]        Worker 3  ->  receives a COMPILED PACKAGE
                                           Worker N  ->  receives a COMPILED PACKAGE

Grows without bound.                       Bounded by an explicit token budget.
Model-specific and session-bound.          Model-independent and portable.
Detail buried in transcript.               Detail is addressable, typed state.
Nothing is verified.                       Every claim is reconciled.
```

In this engine every worker receives exactly two messages: a system prompt and
one compiled context package. Never a transcript. The run summary reports
`Raw conversation transfers: 0`, and a test asserts it.

---

## Architecture

```
                        USER TASK
                            |
                            v
                  CONTEXT ORCHESTRATOR                 core/orchestrator.py
                            |
                            v
                    EXECUTION STATE                    context/state.py
                            |
              +-------------+-------------+
              v             v             v
       STATE RECONCILER  CONTEXT      STATE STORE
       core/reconciler   COMPILER     storage/sqlite_store.py
                         context/compiler.py
              |             |
              +-------------+
                            |
                            v
                     UNIVERSAL WORKER                  core/worker.py
                            |
                            v
                       LLM GATEWAY                     gateway/llm_gateway.py
                            |
                            v
                    CONFIGURED MODEL                   config/workers.json
```

The engine owns the canonical state. Workers own nothing that outlives their turn.

### The loop

```
Worker N
   |
   v  receives its assigned task + a compiled context package
Performs the work
   |
   v  emits a structured Worker Result          (a claim)
   v  emits a mandatory Handoff Report          (a message to its successor)
   |
   v
STATE RECONCILER  -- validates, merges, dedupes, flags unverified claims
   |
   v
CANONICAL EXECUTION STATE  -- persisted to SQLite after every single turn
   |
   v
CONTEXT COMPILER  -- prioritizes and budgets the next package
   |
   v
Worker N+1
```

---

## Install

```bash
pip install -e ".[dev]"      # from a clone
```

Requires Python 3.11+. Everything runs locally against your own `.env`; the
engine never writes an API key to the database.

## Use it as a library

```python
from context_orchestration import Engine

with Engine.from_config("workers.json", db="run.db") as engine:
    result = engine.run(
        objective="Design a notification service.",
        plan=["Define requirements.", "Design delivery.", "Review the design."],
    )

    print(result.summary.workers_used)                 # 3
    print(result.summary.raw_conversation_transfers)   # 0
    print(result.state.decisions[0].decision)          # what worker 1 decided

    for package in engine.packages(result.task_id):
        print(package.target_worker_id, package.estimated_tokens)
```

`Engine.demo()` uses the packaged example roster if you have no config yet.

### Driving the loop yourself

`run()` executes every worker. If you want control between turns - inspect the
reconciled state, decide whether to continue, hand off to your own logic -
step it:

```python
state = engine.create(objective, plan)      # persisted, nothing run yet

engine.step(state.task_id)                  # exactly one worker turn
current = engine.state(state.task_id)       # status == "paused"
print(len(current.decisions), current.next_action)

engine.resume(state.task_id)                # finish the rest
```

### Using the context compiler on its own

The compiler needs no model call and no orchestrator - point it at any
`ExecutionState` to get a bounded, prioritized prompt context:

```python
package = engine.compile_context(my_state, "Design the schema.", "worker-1")
print(package.rendered_text)        # what a worker would receive
print(package.omitted_sections)     # what did not fit the budget
```

Everything the facade does is delegation - `ContextOrchestrator`,
`ContextCompiler`, `StateReconciler`, `WorkerRegistry`, `SQLiteStore` and the
gateways are all exported and swappable if you would rather wire them yourself.

## Command line

```bash
coe init                  # scaffold workers.json + .env in the current directory
coe run                   # the built-in five-worker demonstration
coe run --real            # use live models instead of the mock gateway
```

`python main.py run` still works, as does `python -m context_orchestration`.

| Command | What it shows |
|---|---|
| `coe init` | Scaffold `workers.json` and `.env` here |
| `coe run` | The built-in five-worker demonstration |
| `coe run --show-context` | Same, printing every compiled context package |
| `coe state <task_id>` | The canonical execution state |
| `coe history <task_id>` | Worker executions and handoff history |
| `coe handoffs <task_id>` | Handoff reports and the compiled packages |
| `coe step <task_id>` | Advance exactly one worker turn |
| `coe resume <task_id>` | Continue a persisted task |
| `coe tasks` | Every persisted task |
| `coe workers` | The configured roster and credential status |

Task IDs may be abbreviated to any unambiguous prefix.

Useful flags: `--mock` / `--real` force the gateway, `--budget N` sets the
context token budget per package, `--db PATH` selects the database, and
`--workers PATH` selects a worker configuration file. Without `--workers`, the
CLI uses `./workers.json` if present, otherwise the packaged example.

With no API keys configured, `run` executes the full demonstration against a
deterministic offline **mock gateway** - the entire architecture, persistence
and reconciliation path runs for real; only the model calls are simulated.

Run a custom task instead of the demo:

```bash
coe run   --objective "Design a real-time notification service."   --task "Define requirements and architecture."   --task "Design the message delivery pipeline."   --task "Review the design for failure modes."
```

---

## Web playground

```bash
coe serve                 # http://127.0.0.1:8000
```

A guided four-step demo that drives the same engine: credentials, the task,
the roster, then the run. Step three previews which worker will take which
plan step before anything executes. Step four streams every turn as it
happens - a live diagram of which component is working, the exact context
package each worker received, the reconciler's verdict on its claims, and
canonical state growing between turns. A mock run finishes in milliseconds, so
playback is paced by default to make it watchable.

The page also carries an interactive walkthrough that takes one turn apart
stage by stage, and a budget slider that recompiles a real package in the
browser.

Mock mode needs no credentials. For live models, either set the environment
variables `workers.json` names, or paste a key into the page - one key for
every worker, or one per worker. A key entered there is sent with the run that
uses it, attached to an in-memory `WorkerConfig` for that run alone, and is
never written to the database, logged, or returned to the page. **Verify
keys** sends one small call per worker and reports what the provider said.

---

## Deploying the playground

The repository is configured for Vercel: `vercel --prod`, or import it at
[vercel.com/new](https://vercel.com/new) and change nothing. `api/index.py`
exposes the app, `vercel.json` routes to it, and `requirements.txt` carries the
three packages a hosted run needs.

A hosted deployment runs the **mock gateway** only. Every other part of the
engine is real - the same compilation, the same reconciliation, the same SQLite
writes, the same audit - so a visitor watches the architecture work with no key
and no cost. Live model calls are closed there on purpose: a public URL is the
wrong place to invite someone to paste a provider key, and a five-worker live
run outlives any serverless request limit.

Serverless hosts also freeze an instance once it has answered, and route each
request wherever they like, so the local shape - a background thread started by
one request and watched by another - cannot survive there. With
`COE_SERVERLESS=1` the page instead uses `POST /api/runs/stream`, which starts a
run and streams it inside a single open request. Locally, nothing changes.

[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) has the environment variables, the
limits of the hosted version, and how to run it anywhere else.

---

## Configuring any number of workers

`config/workers.json` is read at runtime. The orchestrator behaves identically
with 2, 5, 20 or 100 workers, and adding one requires no code change:

```json
{
  "workers": [
    { "id": "worker-1", "model": "provider/model-name", "api_key_env": "WORKER_1_API_KEY" },
    { "id": "worker-2", "model": "provider/model-name", "api_key_env": "WORKER_2_API_KEY" }
  ]
}
```

Each entry accepts `id`, `model`, `api_key_env` (or a literal `api_key`),
`api_base`, `temperature`, `max_tokens`, `max_retries`, `role`, `enabled`, and a
free-form `provider_config` passed through to the gateway.

**The `model` prefix selects the provider, not the key.** `anthropic/...` goes to
Anthropic, `groq/...` goes to Groq, `openai/...` to OpenAI. A valid key paired
with the wrong prefix fails with `invalid x-api-key`, because the key is being offered
to a provider that never issued it. If you swap providers, change `model`, not
just the credential.

Note that `groq/openai/gpt-oss-120b` is Groq serving OpenAI's open-weight model:
the first segment is the provider you are billed by, the rest is the model id.

Plan steps and workers are matched automatically:

- more steps than workers -> the roster cycles
- more workers than steps -> surplus workers get continuation turns, so every
  configured worker gets a turn

The shipped demo config runs five workers over four distinct model families on
five separate org credentials, so every handoff crosses a real model boundary. The
orchestration layer never learns which is which: no module
above the gateway mentions any provider name.

---

## Canonical execution state

A Pydantic model (`context/state.py`) tracking objective, completed work,
current task, pending tasks, decisions and their reasons, artifacts, issues,
failed attempts, assumptions, current progress, last action, next action,
worker history and handoff history.

Its merge helpers are deliberately conservative:

- completed tasks and decisions dedupe on normalized text
- artifacts are **versioned**, never overwritten. `architecture.md v3` records
  that worker-2 and worker-5 both touched what worker-1 created
- failed attempts are **append-only**; nothing a later worker says removes them

---

## The trust model

A worker's output is a *claim about what happened*, not a fact. The State
Reconciler (`core/reconciler.py`) is the boundary between the two, and it
enforces:

- **Issues are never auto-closed by an unverified claim.** A worker asserting it
  fixed something gets `resolution_claimed_by` recorded on the issue, and the
  issue stays open. In the demo run, worker-5 claims the refresh-token issue is
  resolved; the engine keeps it open and says so.
- **Artifacts mentioned only in the handoff report** (never listed in the
  structured result) are recorded with `verified=False` and flagged, and the
  next worker sees them labelled `unverified`.
- **Unplanned completion claims** are recorded but warned about.
- **Provenance is stamped by the engine**, not self-reported by the model.
- Discrepancies between the result and the report (a `last_action` that does not
  match, missing handoff notes, a decision with no reason) raise warnings that
  persist alongside the execution record.

Artifact validation is intentionally basic in this MVP. `verified` is the seam
where real validation (file existence, content hashing, tool results) plugs in.

---

## The context compiler

`context/compiler.py` is the component that makes the whole thing work. It never
dumps state into a prompt. It scores every item for relevance against the
assigned task and objective, blends in a recency bias, applies per-category
caps, and then fills a configurable token budget in strict priority order:

1. assigned task 2. objective 3. current progress 4. current task state
5. relevant decisions 6. relevant completed work 7. relevant artifacts
8. unresolved issues 9. failed attempts not to repeat 10. assumptions
11. last action 12. recommended next action 13. notes from the previous worker

Anchoring keeps the earliest decisions and failed attempts regardless of score.
Without it, relevance-plus-recency quietly drops founding choices ("use FastAPI",
"use UUID primary keys") in favour of recent detail, and the final review worker
ends up auditing an architecture whose foundations it cannot see. This was a real
failure observed in a live five-model run, not a hypothetical.

Budget is measured against the *rendered* package rather than a sum of section
estimates. Anything that does not fit is reported in `omitted_sections` and
`dropped_items` instead of silently vanishing. The assigned task and objective
are never dropped; they are truncated as a last resort.

Token counting uses `tiktoken` when available and falls back to a character/word
heuristic. `TokenEstimator` is a protocol, so a per-provider tokenizer can
replace it without touching the compiler.

---

## Persistence

SQLite, written after every worker turn, not at the end of a run. Tables cover
tasks, per-turn state snapshots, worker executions, handoff reports, compiled
context packages (including their rendered text), and an append-only event log.

That is what makes `resume` real: kill the process between worker 3 and worker 4
and the next run compiles worker 4's package from what is on disk. A test
simulates exactly that.

---

## Rate limits

The gateway retries on a provider rate limit, honouring the delay the provider
itself suggests (`Please try again in 8.4s`) and falling back to exponential
backoff. A rate limit is a scheduling problem, not a failure, so the same worker
can simply wait.

If you are on a free tier, `max_tokens` is what gets you throttled: providers
count `prompt + max_tokens` against your per-minute budget, and each worker makes
two calls per turn. On Groq's free tier (8,000 TPM per org) a `max_tokens` of
4,000 exhausts the budget on the second call; 2,200 leaves comfortable headroom.
That is why the shipped config sets 2,200 rather than something larger.

---

## Worker switching

The MVP uses forced sequential switching. The seam for more is already in place:
`SwitchPolicy` decides which worker handles which step, and
`SequentialSwitchPolicy` is one implementation. Context-limit, cost, rate-limit,
availability and capability-based switching are all alternate implementations of
that one interface, and the orchestration loop does not change.

---

## Project layout

```
pyproject.toml           packaging; installs the `coe` console script
main.py                  compatibility shim -> context_orchestration.cli
requirements.txt         the lean dependency set a hosted playground installs
vercel.json              function, routing and env for a Vercel deployment
api/index.py             ASGI entry point for a serverless host
docs/DEPLOYMENT.md       hosting the playground, and its limits
.github/workflows/ci.yml tests on 3.11 and 3.12, plus a bundle import check
src/context_orchestration/
  __init__.py            the public API (44 exports)
  engine.py              Engine facade - what most users touch
  cli.py                 command line interface
  core/
    contracts.py         Pydantic contracts: raw claims vs canonical records
    orchestrator.py      the loop, worker registry, switch policy
    worker.py            the one and only worker class
    reconciler.py        the trust boundary
    planner.py           one-shot mode: a sentence into steps, a total into shares
  context/
    state.py             canonical execution state
    compiler.py          relevance scoring, prioritization, token budget
    handoff.py           handoff records, rendering, transfer audit
  gateway/
    llm_gateway.py       LiteLLM gateway + deterministic mock gateway
    http_gateway.py      the same contract over the standard library
    providers.py         which vendor issued a key, and what it can run
  storage/
    sqlite_store.py      persistence and resumability
  ui/
    console.py           Rich rendering (kept out of the engine)
  config/
    demo.py              the built-in demonstration task
    workers.example.json shipped starting roster
  web/
    server.py            FastAPI: config, key inspection, planning, runs
    static/
      index.html         markup only
      css/               base, walkthrough, playground
      js/                ES modules, no build step
tests/                   267 tests
```

Everything lives under the single `context_orchestration` namespace, so
installing this package claims no generic top-level import names - a test
asserts that.

---

## Tests

```bash
python -m pytest -q      # 290 tests
```

Beyond unit coverage, the suite asserts the architectural claims directly:

- no module above the gateway mentions any provider name
- exactly one worker class exists
- every model call carries exactly one system + one user message
- `raw_conversation_transfers == 0` across a full run
- context sent per worker stays inside the budget as the task grows
- a decision made by worker 1 reaches worker 5's package
- runs work identically with 1, 3, 5, 20 and 100 workers
- a killed run resumes from SQLite and finishes the task
- an issue a worker claims to have fixed stays open
- founding decisions survive a flood of thirty later ones
- the installed package claims no generic top-level import names

---

## What the demonstration proves

Running `python main.py run` executes five workers against one objective. At the
end the canonical state holds all five completed steps, decisions attributed to
several different workers, artifacts versioned across workers, every failed
attempt preserved, and open issues that no worker was allowed to close by
assertion alone.

No worker ever saw another worker's conversation.

---

## Project documents

- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) - hosting the playground
- [`CONTRIBUTING.md`](CONTRIBUTING.md) - setup, the one architectural rule, what
  a pull request needs
- [`CHANGELOG.md`](CHANGELOG.md) - what changed, and when
- [`LICENSE`](LICENSE) - MIT
