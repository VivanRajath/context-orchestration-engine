# Deploying the playground

The engine is a library and a CLI. The only thing worth hosting is the web
playground — a page that drives the real orchestrator and streams every turn,
so a reader can watch a handoff happen instead of reading about one.

This document covers Vercel, which the repository is configured for out of the
box, and then the general case.

---

## What the hosted playground is, and is not

The deployed page runs the **mock gateway**. Every part of the engine is real —
the same context compilation, the same reconciliation, the same SQLite writes,
the same handoff audit — and only the model call is simulated. A visitor sees
the architecture work, with no key and no cost.

Live model calls are **off** on a serverless deployment, deliberately:

- a public URL is the wrong place to invite someone to paste a provider key;
- a five-worker live run outlives any serverless request limit;
- LiteLLM's dependency tree is most of a bundle on its own, and it is never
  imported when the mock gateway is in use.

For live models, clone the repo and run `coe serve` against your own `.env`.
A private deployment that carries its own credentials can reopen live mode with
`COE_ALLOW_LIVE=1` — see [Environment variables](#environment-variables).

---

## Vercel

```bash
npm i -g vercel
vercel            # preview deployment
vercel --prod     # production
```

Or import the GitHub repository at [vercel.com/new](https://vercel.com/new).
No build settings need changing: `vercel.json` already declares the function,
and the framework preset should stay **Other**.

### The files that make it work

| File | What it does |
| --- | --- |
| `api/index.py` | Exposes the FastAPI app as `app`, puts `src/` on the path, points SQLite at `/tmp` |
| `vercel.json` | Declares the Python function, rewrites every path to it, sets `COE_SERVERLESS=1` |
| `requirements.txt` | The lean runtime set: FastAPI, Pydantic, python-dotenv |
| `api/requirements.txt` | The same list, next to the entry point, whichever the builder resolves first |
| `.vercelignore` | Keeps the venv, tests, databases and docs out of the bundle |

### Why the run path changes on a serverless host

Locally, starting a run and watching it are two requests: a `POST /api/runs`
kicks off a background thread, and an `EventSource` on
`GET /api/runs/{id}/stream` watches it. That shape assumes the process holding
the run is still running, and still reachable, when the browser comes back.

Neither assumption holds on a serverless host. The instance is frozen once a
response is sent, so the thread stops the moment the POST returns; and the
follow-up GET is routed independently, so it can arrive at an instance that has
never heard of the run.

`COE_SERVERLESS=1` therefore switches the page onto `POST /api/runs/stream`,
which starts the run and streams its events inside a single open request. The
assumption is removed rather than worked around. `EventSource` cannot issue a
POST, so the page reads the SSE frames off the response body itself.

### Known limits of the hosted version

- **Run history is per-instance.** SQLite lives in `/tmp`, which belongs to one
  instance and vanishes with it. A run persists and reloads correctly inside
  the request that produced it; the history panel may be empty on a later page
  load. Point `COE_DB` at a mounted volume, or run locally, for durable state.
- **One request, one run.** A run must finish inside `maxDuration` (60s in
  `vercel.json`; a Hobby plan allows up to 300). A mock run finishes in
  milliseconds, so this only binds if you reopen live mode.
- **No live models** unless you set `COE_ALLOW_LIVE=1` *and* add `litellm` to
  `requirements.txt` *and* supply the worker keys as environment variables.

---

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `COE_SERVERLESS` | unset | Single-request streaming path; closes live model calls. Set to `1` by `vercel.json`. |
| `COE_ALLOW_LIVE` | unset | Reopens live model calls on a serverless deployment. Only for a private one that carries its own keys. |
| `COE_DB` | `/tmp/playground.db` | SQLite path for the deployed app. |
| `WORKER_1_API_KEY` … | unset | Per-worker credentials, named by `api_key_env` in the worker roster. |

Set them under **Project → Settings → Environment Variables**, never in a
committed file. `.env` is gitignored and `.vercelignore`d.

---

## The worker roster on a deployment

`workers.json` is gitignored, because it is yours. With no such file the engine
falls back to the roster packaged at
`src/context_orchestration/config/workers.example.json` — five workers across
four model families — which is what the deployed page shows.

To deploy a different roster, commit your own file and point the app at it:

```python
# api/index.py
app = create_app(
    workers_path=Path(__file__).resolve().parent.parent / "workers.deploy.json",
    db=os.environ.get("COE_DB", "/tmp/playground.db"),
)
```

---

## Anywhere else

The app is a plain ASGI application, so any host that runs one will do — and a
host with a persistent process is a *better* fit than a serverless one, because
the local two-request path and durable SQLite both work unmodified.

```bash
pip install -e ".[web]"
uvicorn "context_orchestration.web.server:create_app" --factory --host 0.0.0.0 --port $PORT
```

Leave `COE_SERVERLESS` unset there. A container needs `python:3.11-slim` or
newer, the repository, and a writable volume for the database.
