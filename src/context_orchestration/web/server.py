"""Local web playground for the Context Orchestration Engine.

Runs on your machine, against your own credentials, driving the real engine.
There is no hosted component. Credentials reach a worker one of two ways: the
same environment the CLI reads, or a key typed into the playground for a single
run. Neither is ever written to the database, logged, or sent back to the
browser - a browser-supplied key lives only on the in-memory ``WorkerConfig``
that the run it belongs to is using.

The whole thing hangs off ``OrchestratorEvents``. The CLI plugs ``RichEvents``
into that seam to draw a terminal; this module plugs ``StreamEvents`` into the
same seam to push JSON over Server-Sent Events. The engine itself is unchanged
and unaware either exists.

    coe serve
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from context_orchestration.config.demo import DEMO_OBJECTIVE, DEMO_PLAN
from context_orchestration.context.compiler import ContextCompiler
from context_orchestration.core.orchestrator import (
    ContextOrchestrator,
    OrchestratorEvents,
    WorkerRegistry,
    load_registry,
    resolve_mock_mode,
)
from context_orchestration.gateway.llm_gateway import (
    LiteLLMGateway,
    build_gateway,
    missing_keys,
)
from context_orchestration.storage.sqlite_store import SQLiteStore

STATIC = Path(__file__).resolve().parent / "static"

_SENTINEL = object()


# --------------------------------------------------------------------------
# Event sink
# --------------------------------------------------------------------------


def _dump(model: Any) -> Any:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model


class StreamEvents(OrchestratorEvents):
    """Pushes every orchestrator event onto a queue as a JSON-ready dict."""

    def __init__(self, sink: "queue.Queue[Any]") -> None:
        self.sink = sink
        self.index = 0
        self.total = 0

    def _emit(self, kind: str, **payload) -> None:
        self.sink.put({"event": kind, "at": time.time(), **payload})

    def run_started(self, state, assignments, registry, mock) -> None:
        self.total = len(assignments)
        self.index = 0
        self._emit(
            "run_started",
            task_id=state.task_id,
            objective=state.objective,
            mock=mock,
            total=self.total,
            roster=[
                {
                    "seq": a.seq,
                    "worker_id": a.worker_id,
                    "model": registry.get(a.worker_id).model,
                    "task": a.task,
                }
                for a in assignments
            ],
        )

    def worker_started(self, assignment, config, package) -> None:
        self.index += 1
        self._emit(
            "worker_started",
            index=self.index,
            total=self.total,
            seq=assignment.seq,
            worker_id=assignment.worker_id,
            model=config.model,
            task=assignment.task,
            package={
                "package_id": package.package_id,
                "estimated_tokens": package.estimated_tokens,
                "token_budget": package.token_budget,
                "included_sections": package.included_sections,
                "omitted_sections": package.omitted_sections,
                "dropped_items": package.dropped_items[:40],
                "contains_raw_conversation": package.contains_raw_conversation,
                "rendered_text": package.rendered_text,
            },
        )

    def worker_completed(self, run) -> None:
        self._emit(
            "worker_completed",
            worker_id=run.worker_id,
            model=run.model,
            duration_ms=run.duration_ms,
            messages_sent=run.messages_sent,
            context_tokens_in=run.context_tokens_in,
            raw_conversation_transferred=run.raw_conversation_transferred,
            result=_dump(run.result),
            report=_dump(run.report),
        )

    def worker_failed(self, assignment, config, error) -> None:
        self._emit(
            "worker_failed",
            worker_id=assignment.worker_id,
            model=config.model,
            error=" ".join(str(error).split())[:600],
        )

    def reconciled(self, report, state) -> None:
        self._emit(
            "reconciled",
            worker_id=report.worker_id,
            summary=report.summary(),
            accepted=report.accepted,
            duplicates_skipped=report.duplicates_skipped,
            warnings=report.warnings,
            unverified_artifacts=report.unverified_artifacts,
            rejected_resolutions=report.rejected_resolutions,
            state=_state_snapshot(state),
        )

    def handoff(self, audit) -> None:
        self._emit("handoff", audit=audit)

    def run_finished(self, summary, state) -> None:
        self._emit(
            "run_finished",
            summary=_dump(summary),
            continuity=summary.continuity_maintained,
            state=_state_snapshot(state),
        )
        self.sink.put(_SENTINEL)


def _state_snapshot(state) -> dict:
    """A compact view of canonical state for the live panel."""
    return {
        "task_id": state.task_id,
        "status": state.status,
        "objective": state.objective,
        "completed_tasks": state.completed_tasks,
        "pending_tasks": state.pending_tasks,
        "current_task": state.current_task,
        "current_progress": state.current_progress,
        "last_action": state.last_action,
        "next_action": state.next_action,
        "decisions": [
            {"decision": d.decision, "reason": d.reason, "by": d.recorded_by} for d in state.decisions
        ],
        "artifacts": [
            {
                "name": a.name,
                "version": a.version,
                "by": a.created_by,
                "modified_by": a.modified_by,
                "verified": a.verified,
            }
            for a in state.artifacts
        ],
        "issues": [
            {
                "description": i.description,
                "severity": i.severity,
                "resolved": i.resolved,
                "claimed_by": i.resolution_claimed_by,
                "by": i.raised_by,
            }
            for i in state.issues
        ],
        "failed_attempts": [
            {"attempt": f.attempt, "reason": f.reason, "by": f.recorded_by} for f in state.failed_attempts
        ],
        "assumptions": [{"assumption": a.assumption, "by": a.recorded_by} for a in state.assumptions],
    }


# --------------------------------------------------------------------------
# Run registry
# --------------------------------------------------------------------------


class RunSession:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.sink: "queue.Queue[Any]" = queue.Queue()
        self.replay: list[dict] = []
        self.done = False
        self.thread: threading.Thread | None = None


RUNS: dict[str, RunSession] = {}
RUNS_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


SHARED_KEY = "*"


class RunRequest(BaseModel):
    objective: str = Field(default=DEMO_OBJECTIVE)
    plan: list[str] = Field(default_factory=lambda: list(DEMO_PLAN))
    mock: bool = True
    budget: int = 1600
    max_steps: int | None = None
    # worker_id -> key, plus the optional "*" entry meaning "use this for every
    # worker that has no key of its own".
    credentials: dict[str, str] = Field(default_factory=dict)


class CredentialTest(BaseModel):
    credentials: dict[str, str] = Field(default_factory=dict)


def apply_credentials(reg: WorkerRegistry, credentials: dict[str, str]) -> WorkerRegistry:
    """Attach browser-supplied keys to a registry for the lifetime of one run.

    ``load_registry`` builds fresh ``WorkerConfig`` objects per request, so a key
    set here cannot outlive or leak into another run. Nothing downstream
    persists a config: the store holds state, packages and reports only.
    """
    if not credentials:
        return reg
    shared = str(credentials.get(SHARED_KEY) or "").strip()
    for config in reg:
        key = str(credentials.get(config.id) or "").strip() or shared
        if key:
            config.api_key = key
    return reg


def _reason(exc: Exception, config) -> str:
    """The provider's own message, minus the prefix the UI already shows."""
    text = " ".join(str(exc).split())
    prefix = f"model call failed for {config.id} ({config.model}): "
    if text.startswith(prefix):
        text = text[len(prefix):]
    return text[:300]


def _has_credential(config) -> bool:
    if config.api_key:
        return True
    return bool(config.api_key_env and os.environ.get(config.api_key_env))


# --------------------------------------------------------------------------
# Deployment mode
# --------------------------------------------------------------------------
#
# The playground was written for a laptop: a background thread does the run and
# an EventSource watches it. A serverless host honours neither half - it freezes
# the instance once a response is sent, and routes the follow-up request
# wherever it likes. ``COE_SERVERLESS`` switches the page onto a single-request
# streaming path that makes no such assumptions, and closes live mode, because a
# public URL is the wrong place to type a provider key. ``COE_ALLOW_LIVE``
# reopens it for a private deployment that has its own credentials.


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def serverless() -> bool:
    return _flag("COE_SERVERLESS")


def live_enabled() -> bool:
    return not serverless() or _flag("COE_ALLOW_LIVE")


LIVE_CLOSED = (
    "Live model calls are disabled on this deployment. Mock mode demonstrates "
    "the full architecture offline; clone the repo and run `coe serve` to point "
    "it at your own providers."
)


def create_app(workers_path: str | Path | None = None, db: str = "playground.db") -> FastAPI:
    # Load .env here too, so the app works when mounted by an ASGI server
    # directly rather than launched through `coe serve`.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover
        pass

    app = FastAPI(title="Context Orchestration Engine", docs_url="/api/docs")

    def registry() -> WorkerRegistry:
        return load_registry(workers_path)

    def store() -> SQLiteStore:
        return SQLiteStore(db)

    # -- static ---------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # No caching: this is a local tool, and a stale UI against a fresh
        # engine is a confusing way to lose an afternoon.
        return FileResponse(
            STATIC / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    # -- config ---------------------------------------------------------

    @app.get("/api/config")
    def get_config() -> dict:
        reg = registry()
        missing = missing_keys(list(reg))
        return {
            "workers": [
                {
                    "id": c.id,
                    "model": c.model,
                    "role": c.role,
                    "api_key_env": c.api_key_env,
                    # Which provider the model string routes to. The UI labels
                    # the key field with it, because a key is only ever valid
                    # for the provider that issued it.
                    "provider": c.model.split("/")[0] if "/" in c.model else c.model,
                    "credential": c.id not in missing,
                    "max_tokens": c.max_tokens,
                }
                for c in reg
            ],
            "missing_credentials": missing,
            "can_run_live": bool(live_enabled() and not missing),
            # The page reads these to pick its transport and to decide whether
            # to offer live mode at all.
            "serverless": serverless(),
            "live_enabled": live_enabled(),
            "live_disabled_reason": None if live_enabled() else LIVE_CLOSED,
            "demo": {"objective": DEMO_OBJECTIVE, "plan": list(DEMO_PLAN)},
        }

    # -- runs -----------------------------------------------------------

    @app.post("/api/credentials/test")
    def test_credentials(req: CredentialTest) -> dict:
        """One tiny live call per worker, to answer 'is this key usable here?'.

        Keys are never echoed back; only a verdict and the provider's own error
        text, which is the part that actually tells you what went wrong.
        """
        if not live_enabled():
            raise HTTPException(403, LIVE_CLOSED)
        reg = apply_credentials(registry(), req.credentials)
        gateway = LiteLLMGateway(timeout=30, rate_limit_retries=0)
        results = []
        for config in reg:
            row = {"worker_id": config.id, "model": config.model}
            if not _has_credential(config):
                results.append({**row, "ok": False, "error": "no key supplied"})
                continue
            probe = config.model_copy(update={"max_tokens": 16, "temperature": 0.0})
            try:
                gateway.complete(probe, [{"role": "user", "content": "Reply with OK."}])
                results.append({**row, "ok": True, "error": None})
            except Exception as exc:
                results.append({**row, "ok": False, "error": _reason(exc, config)})
        return {"results": results}

    def prepare(req: RunRequest) -> tuple[str, WorkerRegistry, bool, int]:
        """Validate a request and write the task's opening state.

        Shared by both run endpoints, so a run behaves identically whichever
        transport carried it.
        """
        reg = apply_credentials(registry(), req.credentials)
        use_mock, missing = resolve_mock_mode(reg, "mock" if req.mock else "real")
        if not use_mock and not live_enabled():
            raise HTTPException(403, LIVE_CLOSED)
        if not use_mock and missing:
            raise HTTPException(
                400,
                f"No credential resolved for: {', '.join(missing)}. "
                "Paste a key for those workers, set them in .env, or run in mock mode.",
            )

        plan = [p.strip() for p in req.plan if p.strip()]
        if not plan:
            raise HTTPException(400, "Add at least one plan step.")
        if not req.objective.strip():
            raise HTTPException(400, "Objective cannot be empty.")

        # The store is opened inside the worker thread so the SQLite
        # connection is never shared across threads.
        setup_store = store()
        try:
            orchestrator = ContextOrchestrator(
                registry=reg,
                gateway=build_gateway(use_mock),
                store=setup_store,
                compiler=ContextCompiler(token_budget=req.budget),
                mock=use_mock,
            )
            state = orchestrator.create_task(req.objective.strip(), plan)
            task_id = state.task_id
        finally:
            setup_store.close()

        return task_id, reg, use_mock, len(plan)

    def begin(req: RunRequest, task_id: str, reg: WorkerRegistry, use_mock: bool) -> RunSession:
        """Register the run and start the thread that drives it."""
        session = RunSession(task_id)
        with RUNS_LOCK:
            RUNS[task_id] = session

        def worker() -> None:
            thread_store = SQLiteStore(db)
            try:
                ContextOrchestrator(
                    registry=reg,
                    gateway=build_gateway(use_mock),
                    store=thread_store,
                    compiler=ContextCompiler(token_budget=req.budget),
                    events=StreamEvents(session.sink),
                    mock=use_mock,
                ).resume(task_id, max_steps=req.max_steps)
            except Exception as exc:  # surface it in the UI, never hang the stream
                session.sink.put(
                    {"event": "run_error", "error": " ".join(str(exc).split())[:600]}
                )
                session.sink.put(_SENTINEL)
            finally:
                thread_store.close()

        session.thread = threading.Thread(target=worker, daemon=True)
        session.thread.start()
        return session

    def drain(session: RunSession) -> Iterator[str]:
        # Replay anything emitted before the browser connected.
        for item in list(session.replay):
            yield f"data: {json.dumps(item)}\n\n"
        while True:
            try:
                item = session.sink.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if item is _SENTINEL:
                session.done = True
                yield "data: {\"event\": \"stream_end\"}\n\n"
                return
            session.replay.append(item)
            yield f"data: {json.dumps(item)}\n\n"

    SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    @app.post("/api/runs")
    def start_run(req: RunRequest) -> dict:
        task_id, reg, use_mock, steps = prepare(req)
        begin(req, task_id, reg, use_mock)
        return {"task_id": task_id, "mock": use_mock, "steps": steps}

    @app.get("/api/runs/{task_id}/stream")
    def stream(task_id: str) -> StreamingResponse:
        with RUNS_LOCK:
            session = RUNS.get(task_id)
        if session is None:
            raise HTTPException(404, "No such run.")
        return StreamingResponse(
            drain(session), media_type="text/event-stream", headers=SSE_HEADERS
        )

    @app.post("/api/runs/stream")
    def run_and_stream(req: RunRequest) -> StreamingResponse:
        """Start a run and stream it inside one request.

        The pair above assumes the process that started a run is still alive,
        and still reachable, when the browser comes back to watch it. Neither
        holds on a serverless host: the instance freezes when the POST returns,
        and the follow-up GET is routed independently, so it can land somewhere
        that has never heard of the run - or on the same instance with the
        thread suspended mid-step. Doing both halves in one open request drops
        the assumption rather than working around it.
        """
        task_id, reg, use_mock, _ = prepare(req)
        session = begin(req, task_id, reg, use_mock)
        return StreamingResponse(
            drain(session), media_type="text/event-stream", headers=SSE_HEADERS
        )

    # -- inspection -----------------------------------------------------

    @app.get("/api/tasks")
    def list_tasks() -> list[dict]:
        s = store()
        try:
            return s.list_tasks(50)
        finally:
            s.close()

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        s = store()
        try:
            state = s.load_state(task_id)
            if state is None:
                raise HTTPException(404, "No such task.")
            return {
                "state": _state_snapshot(state),
                "packages": [
                    {
                        "package_id": p.package_id,
                        "worker_id": p.target_worker_id,
                        "assigned_task": p.assigned_task,
                        "estimated_tokens": p.estimated_tokens,
                        "token_budget": p.token_budget,
                        "included_sections": p.included_sections,
                        "omitted_sections": p.omitted_sections,
                        "contains_raw_conversation": p.contains_raw_conversation,
                        "rendered_text": p.rendered_text,
                    }
                    for p in s.load_packages(task_id)
                ],
                "handoffs": [
                    {
                        "seq": h.seq,
                        "from_worker": h.from_worker,
                        "to_worker": h.to_worker,
                        "raw_conversation_transferred": h.raw_conversation_transferred,
                        "report": _dump(h.report),
                    }
                    for h in s.load_handoffs(task_id)
                ],
                "executions": [_dump(e) for e in s.load_worker_executions(task_id)],
            }
        finally:
            s.close()

    return app


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    workers_path: str | Path | None = None,
    db: str = "playground.db",
    open_browser: bool = True,
) -> None:
    import uvicorn

    if open_browser:
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    uvicorn.run(create_app(workers_path, db), host=host, port=port, log_level="warning")
