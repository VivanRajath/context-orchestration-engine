# Deploying the playground

The engine is a library and a CLI. The only thing worth hosting is the web
playground — a page that drives the real orchestrator and streams every turn,
so a reader can watch a handoff happen instead of reading about one.

This document covers Vercel, which the repository is configured for out of the
box, and then the general case.

---

## What the hosted playground offers a visitor

Three ways to run, and the page decides which to show from what the deployment
actually has:

| | What it does | What it needs |
| --- | --- | --- |
| **Use ours** | Borrows a key the deployment holds, one per worker | `COE_DEMO_KEYS`, or the `WORKER_n_API_KEY` set |
| **Use mine** | Visitor pastes keys, from as many vendors as they like | Nothing; live calls must not be closed |
| **No key** | The stand-in answers instead of a model | Nothing |

The stand-in is not a mock of the architecture. Every part of the engine is
real — the same context compilation, the same reconciliation, the same SQLite
writes, the same handoff audit — and only the model call is simulated.

### Real model calls, on a serverless host

Both live paths work on Vercel, which needs two things to be true.

**No LiteLLM in the bundle.** Real calls go through `HTTPGateway`, which speaks
the OpenAI request shape over the standard library and carries a small adapter
for Anthropic. Adding a vendor is a base URL in `gateway/providers.py`. Set
`COE_GATEWAY=litellm` to use LiteLLM instead, and add it to
`requirements.txt` if you do.

**Enough time.** A live run is two model calls per worker, one after another.
Five workers on a free tier is comfortably past sixty seconds, so
`vercel.json` asks for `maxDuration: 300`. A Hobby plan caps this at 60
regardless of what the file says, which is enough for two or three workers.

### Before you lend a key

The pool is a real credential spent by anyone who can reach the page. Put only
free-tier keys in it, keep them on their own account, and watch the usage. Set
`COE_NO_POOL=1` to lend nothing, or `COE_NO_LIVE=1` to close real calls
entirely and offer only the stand-in.

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
- **One request, one run.** A run must finish inside `maxDuration`
  (`vercel.json` asks for 300s; a Hobby plan caps it at 60). The stand-in
  finishes in milliseconds. A live run is roughly 5 to 90 seconds per worker
  depending on the model, so keep the plan short on a Hobby plan.
- **Free-tier rate limits are real.** Five workers against one vendor's free
  tier can exhaust a per-minute token allowance mid-run. The engine records the
  failure, keeps state intact, and carries on from the record — which is
  honest, and happens to demonstrate the point.

---

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `COE_SERVERLESS` | unset | Single-request streaming path. Set to `1` by `vercel.json`. |
| `COE_DEMO_KEYS` | unset | Comma-separated keys the page may lend, one per worker. Falls back to the `WORKER_n_API_KEY` set. |
| `COE_NO_POOL` | unset | Lend nothing. Visitors bring their own key or use the stand-in. |
| `COE_ALLOW_LIVE` | unset | Allow real calls on a serverless deployment that has no pool of its own. |
| `COE_NO_LIVE` | unset | Refuse real calls entirely. |
| `COE_GATEWAY` | unset | Set to `litellm` to route real calls through LiteLLM instead of the built-in HTTP gateway. |
| `COE_DB` | `/tmp/playground.db` | SQLite path for the deployed app. |
| `WORKER_1_API_KEY` … | unset | Per-worker credentials, named by `api_key_env` in the worker roster, and the default key pool. |

A key never reaches the browser. `/api/config` reports how many the pool holds
and which vendor issued them; the values themselves stay on the server, are
attached to an in-memory worker config for the length of one run, and are never
written to the database or logged.

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
