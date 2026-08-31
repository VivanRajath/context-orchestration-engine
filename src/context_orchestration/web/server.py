"""Web playground for the Context Orchestration Engine.

Drives the real engine. Nothing here is a simulation of the architecture; it
is the architecture, with a browser attached to the ``OrchestratorEvents``
seam. The CLI plugs ``RichEvents`` into that seam to draw a terminal, this
module plugs ``StreamEvents`` into it to push JSON over Server-Sent Events,
and the engine is unaware either exists.

Credentials reach a worker one of three ways:

* the same environment the CLI reads, for someone running ``coe serve``;
* a key typed into the page, which is attached to an in-memory ``WorkerConfig``
  for the length of one run and then dropped;
* the demonstration pool, which is a set of free-tier keys held in the
  deployment's own environment and never sent to the browser at all.

No key is ever written to the database, logged, or echoed back. The store
holds execution state, packages and reports; there is no column for a secret.

    coe serve
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from context_orchestration.config.demo import DEMO_OBJECTIVE, DEMO_PLAN
from context_orchestration.context.compiler import ContextCompiler
from context_orchestration.core import planner
from context_orchestration.core.contracts import WorkerConfig
from context_orchestration.core.orchestrator import (
    ContextOrchestrator,
    OrchestratorEvents,
    WorkerRegistry,
    load_registry,
    resolve_mock_mode,
)
from context_orchestration.gateway import providers
from context_orchestration.gateway.http_gateway import HTTPGateway
from context_orchestration.gateway.llm_gateway import (
    GatewayError,
    LiteLLMGateway,
    MockGateway,
    extract_json,
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


def _vendor(config: WorkerConfig) -> str:
    """Which vendor is behind this worker, for the page to display.

    Shown because the demonstration is about heterogeneity: five workers on
    five vendors finishing one task is a much clearer claim than five
    anonymous workers. The engine itself never reads this.
    """
    pid = str(config.provider_config.get("provider") or "")
    if not pid:
        pid = providers.split_model(config.model)[0] or ""
    known = providers.PROVIDERS.get(pid)
    return known.label if known else (pid or "unknown")


class StreamEvents(OrchestratorEvents):
    """Pushes every orchestrator event onto a queue as a JSON-ready dict."""

    def __init__(self, sink: "queue.Queue[Any]") -> None:
        self.sink = sink
        self.index = 0
        self.total = 0
        self.mock = False

    def _emit(self, kind: str, **payload) -> None:
        self.sink.put({"event": kind, "at": time.time(), **payload})

    def _vendor_of(self, config: WorkerConfig) -> str:
        return "the stand-in" if self.mock else _vendor(config)

    def run_started(self, state, assignments, registry, mock) -> None:
        self.total = len(assignments)
        self.index = 0
        # No vendor was called on a stand-in run, and saying one was would be
        # the single most misleading thing this page could claim.
        self.mock = mock
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
                    "vendor": self._vendor_of(registry.get(a.worker_id)),
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
            vendor=self._vendor_of(config),
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
# Deployment mode
# --------------------------------------------------------------------------
#
# The playground was written for a laptop: a background thread does the run and
# an EventSource watches it. A serverless host honours neither half - it
# freezes the instance once a response is sent, and routes the follow-up
# request wherever it likes. ``COE_SERVERLESS`` switches the page onto a
# single-request streaming path that makes no such assumptions.


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def serverless() -> bool:
    return _flag("COE_SERVERLESS")


# --------------------------------------------------------------------------
# The demonstration key pool
# --------------------------------------------------------------------------
#
# A visitor should be able to watch five real models finish one task without
# opening an account first, so the deployment carries a small set of free-tier
# keys of its own. They are read from the environment and never leave the
# server: the page is told how many there are and which vendor issued them,
# and that is all it is ever told.
#
# One key per worker, in order, because the point being demonstrated is that
# five independent workers - separate credentials, separate quotas, no shared
# session - can finish one continuous task. Reusing a single key across all
# five would weaken the claim.

POOL_VARS = ("COE_DEMO_KEYS", "COE_POOL_KEYS")
POOL_PREFIX = "WORKER_"


def pool_keys() -> list[str]:
    """Free-tier keys this deployment lends to visitors, if it has any."""
    if _flag("COE_NO_POOL"):
        # A deployment that wants every visitor on their own key, and a test
        # that must not pick up whatever happens to be in the developer's .env.
        return []
    for var in POOL_VARS:
        raw = os.environ.get(var, "")
        if raw.strip():
            keys = [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]
            if keys:
                return keys
    # The same WORKER_n_API_KEY variables the CLI reads, so a laptop that can
    # already run the demo needs no extra configuration to serve it.
    numbered = []
    for i in range(1, 33):
        key = os.environ.get(f"{POOL_PREFIX}{i}_API_KEY", "").strip()
        if key:
            numbered.append(key)
    return numbered


def pool_info() -> dict:
    """What the browser is allowed to know about the pool. Never the keys."""
    keys = pool_keys()
    if not keys:
        return {"available": False, "count": 0, "provider": None, "label": None}
    pid = providers.detect(keys[0]) or "custom"
    prov = providers.PROVIDERS.get(pid)
    return {
        "available": True,
        "count": len(keys),
        "provider": pid,
        "label": prov.label if prov else pid,
        "free_tier": bool(prov and prov.free_tier),
    }


def live_enabled() -> bool:
    """Whether this deployment will place real model calls at all."""
    if _flag("COE_NO_LIVE"):
        return False
    if not serverless():
        return True
    return _flag("COE_ALLOW_LIVE") or bool(pool_keys())


LIVE_CLOSED = (
    "Real model calls are switched off on this deployment. The stand-in still "
    "runs the whole engine offline; clone the repo and run `coe serve` to point "
    "it at your own providers."
)


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------


SHARED_KEY = "*"
POOL_REF = "pool"


class KeySlot(BaseModel):
    """One credential the page is holding, and what it turned out to be."""

    id: str = "k1"
    key: str = ""
    # Empty means "work it out from the key's prefix", which is the usual case.
    provider: str = ""
    api_base: str | None = None


class WorkerSpec(BaseModel):
    """One worker, as assembled in the browser rather than in workers.json."""

    id: str
    # Which KeySlot pays for this worker, or "pool" for a borrowed key.
    key_ref: str = POOL_REF
    provider: str = ""
    model: str = ""
    role: str | None = None
    max_tokens: int = 2200
    temperature: float = 0.2


class RunRequest(BaseModel):
    objective: str = Field(default=DEMO_OBJECTIVE)
    plan: list[str] = Field(default_factory=lambda: list(DEMO_PLAN))
    mock: bool = True
    # Sequential: you set the ceiling and every briefing gets the same one.
    budget: int = 1600
    # One-shot: you set a total for the run and the engine divides it.
    mode: Literal["sequential", "oneshot"] = "sequential"
    step_budgets: list[int] = Field(default_factory=list)
    max_steps: int | None = None
    # A roster built in the page. Empty means workers.json, which is what the
    # CLI and every existing test use.
    workers: list[WorkerSpec] = Field(default_factory=list)
    keys: list[KeySlot] = Field(default_factory=list)
    use_pool: bool = False
    # The older shape: worker_id -> key, plus "*" meaning every worker.
    credentials: dict[str, str] = Field(default_factory=dict)


class CredentialTest(BaseModel):
    credentials: dict[str, str] = Field(default_factory=dict)


class KeyInspect(BaseModel):
    key: str = ""
    provider: str = ""
    api_base: str | None = None
    use_pool: bool = False
    # The plan's roles, so one model can be suggested per step rather than the
    # same model five times.
    roles: list[str] = Field(default_factory=list)


class PlanRequest(BaseModel):
    objective: str = ""
    steps: int = 5
    total_budget: int = 9000
    mock: bool = True
    key: str = ""
    provider: str = ""
    model: str = ""
    api_base: str | None = None
    use_pool: bool = False


def apply_credentials(reg: WorkerRegistry, credentials: dict[str, str]) -> WorkerRegistry:
    """Attach browser-supplied keys to a registry for the lifetime of one run.

    ``load_registry`` builds fresh ``WorkerConfig`` objects per request, so a
    key set here cannot outlive or leak into another run. Nothing downstream
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


def build_registry(req: RunRequest, workers_path) -> WorkerRegistry:
    """The roster for one run, from the page if it sent one, else from disk."""
    if not req.workers:
        return apply_credentials(load_registry(workers_path), req.credentials)

    slots = {s.id: s for s in req.keys}
    borrowed = pool_keys()
    configs: list[WorkerConfig] = []

    for i, spec in enumerate(req.workers):
        slot = slots.get(spec.key_ref)
        key = (slot.key.strip() if slot else "") or ""
        api_base = (slot.api_base if slot else None) or None
        provider = spec.provider or (slot.provider if slot else "") or providers.detect(key) or ""

        if not key and (spec.key_ref == POOL_REF or req.use_pool) and borrowed:
            # One key per worker, wrapping only if the plan outruns the pool.
            key = borrowed[i % len(borrowed)]
            provider = provider or providers.detect(key) or ""

        model = spec.model.strip()
        if not model:
            raise HTTPException(400, f"{spec.id} has no model selected.")

        configs.append(
            WorkerConfig(
                id=spec.id,
                model=providers.qualify(provider, model) if provider else model,
                api_key=key or None,
                api_base=api_base,
                # The display string and the id to call are kept apart on
                # purpose: some vendors publish model ids that begin with a
                # vendor name, so one cannot be recovered from the other.
                provider_config=(
                    {"provider": provider, "model_id": model} if provider else {}
                ),
                role=spec.role,
                max_tokens=max(256, min(8000, spec.max_tokens)),
                temperature=spec.temperature,
            )
        )

    try:
        return WorkerRegistry(configs)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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


def live_gateway():
    """The gateway used for real calls.

    ``HTTPGateway`` needs nothing installed, which is what lets the deployed
    playground place real calls inside a serverless function. Anyone who wants
    LiteLLM's much wider provider coverage sets ``COE_GATEWAY=litellm``.
    """
    if os.environ.get("COE_GATEWAY", "").strip().lower() == "litellm":
        return LiteLLMGateway()
    return HTTPGateway()


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
    #
    # The page is plain HTML, CSS and ES modules: no build step, no bundler,
    # nothing to keep in sync with the engine. The browser resolves the
    # imports itself, so these files are served exactly as they are written.

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        # No caching on the document: this is a local tool, and a stale UI
        # against a fresh engine is a confusing way to lose an afternoon. The
        # assets under /static revalidate by ETag instead, which is what
        # StaticFiles does on its own.
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
                    "provider": providers.split_model(c.model)[0] or c.model,
                    "credential": c.id not in missing,
                    "max_tokens": c.max_tokens,
                }
                for c in reg
            ],
            "missing_credentials": missing,
            "can_run_live": bool(live_enabled() and not missing),
            # The page reads these to pick its transport, and to decide what to
            # offer before anyone has typed anything.
            "serverless": serverless(),
            "live_enabled": live_enabled(),
            "live_disabled_reason": None if live_enabled() else LIVE_CLOSED,
            "pool": pool_info(),
            "providers": providers.catalogue(),
            "demo": {"objective": DEMO_OBJECTIVE, "plan": list(DEMO_PLAN)},
        }

    # -- credentials ----------------------------------------------------

    @app.post("/api/keys/inspect")
    def inspect_key(req: KeyInspect) -> dict:
        """Work out what a key is, and what it is allowed to run.

        This is the step that makes the rest of the page possible: until the
        vendor has told us which models this key can call, every model
        dropdown would be a guess. The key is used for exactly one GET to the
        vendor's own model list and is not stored, logged or echoed back.
        """
        if not live_enabled():
            raise HTTPException(403, LIVE_CLOSED)

        borrowed = pool_keys()
        if req.use_pool:
            if not borrowed:
                raise HTTPException(400, "This deployment has no keys to lend.")
            key, provider_id, api_base = borrowed[0], "", None
        else:
            key, provider_id, api_base = req.key, req.provider, req.api_base
            if not key.strip():
                raise HTTPException(400, "Paste a key first.")

        report = providers.inspect_key(key, provider_id or None, api_base)
        out = report.as_dict()
        out["source"] = "pool" if req.use_pool else "yours"
        out["pool_count"] = len(borrowed) if req.use_pool else 0
        # One model per step rather than the same one repeated, so the run
        # visibly crosses model families.
        # One suggestion per step, in step order. Blank roles still count: a
        # plan the user wrote by hand has no role names, and dropping them
        # here would return one suggestion and put every worker on the same
        # model, which is the opposite of what this page is demonstrating.
        roles = [r or "architecture" for r in req.roles] or ["architecture"]
        out["suggested"] = (
            [
                {"role": role, "model": model}
                for role, model in zip(roles, providers.spread(report.models, roles))
            ]
            if report.ok
            else []
        )
        return out

    @app.post("/api/credentials/test")
    def test_credentials(req: CredentialTest) -> dict:
        """One tiny live call per worker, to answer 'is this key usable here?'.

        Keys are never echoed back; only a verdict and the provider's own error
        text, which is the part that actually tells you what went wrong.
        """
        if not live_enabled():
            raise HTTPException(403, LIVE_CLOSED)
        reg = apply_credentials(registry(), req.credentials)
        gateway = live_gateway()
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

    # -- planning -------------------------------------------------------

    @app.post("/api/plan/split")
    def split_plan(req: PlanRequest) -> dict:
        """Turn one sentence into an editable plan, and divide the budget.

        This is one-shot mode. A model is asked to break the objective up when
        there is a key to ask with, and a template does it otherwise; the page
        says which happened, and every step it returns is editable. The plan
        is a suggestion, and the engine treats it as ordinary input either way.
        """
        objective = req.objective.strip()
        if not objective:
            raise HTTPException(400, "Say what you want built first.")
        count = max(planner.MIN_STEPS, min(planner.MAX_STEPS, req.steps))
        total = max(count * planner.MIN_BUDGET, min(120_000, req.total_budget))

        steps: list[dict] = []
        used_model = None
        note = ""

        key = ""
        provider_id = req.provider
        if req.use_pool:
            borrowed = pool_keys()
            if borrowed:
                key = borrowed[0]
        elif req.key.strip():
            key = req.key.strip()

        if not req.mock and key and live_enabled():
            model = req.model.strip()
            if not model:
                note = "No model was chosen to plan with, so a template wrote the steps."
            else:
                pid = provider_id or providers.detect(key) or ""
                config = WorkerConfig(
                    id="planner",
                    model=providers.qualify(pid, model) if pid else model,
                    api_key=key,
                    api_base=req.api_base,
                    provider_config=({"provider": pid, "model_id": model} if pid else {}),
                    temperature=0.3,
                    max_tokens=1400,
                )
                try:
                    response = live_gateway().complete(
                        config,
                        planner.planner_messages(objective, count),
                        planner.PLAN_SCHEMA,
                    )
                    steps = planner.parse_plan(extract_json(response.text), count)
                    if steps:
                        used_model = config.model
                    else:
                        note = "The planning model did not return a usable plan, so a template wrote the steps."
                except (GatewayError, ValueError) as exc:
                    note = f"Planning by model failed ({_reason(exc, config)}), so a template wrote the steps."

        if not steps:
            steps = planner.heuristic_plan(objective, count)

        budgets = planner.split_budget(total, len(steps))
        return {
            "steps": [
                {"task": s["task"], "role": s.get("role", ""), "budget": b}
                for s, b in zip(steps, budgets)
            ],
            "total_budget": sum(budgets),
            "planner": "model" if used_model else "template",
            "model": used_model,
            "note": note,
        }

    # -- runs -----------------------------------------------------------

    def prepare(req: RunRequest) -> tuple[str, WorkerRegistry, bool, int, dict[int, int]]:
        """Validate a request and write the task's opening state.

        Shared by both run endpoints, so a run behaves identically whichever
        transport carried it.
        """
        reg = build_registry(req, workers_path)
        use_mock, missing = resolve_mock_mode(reg, "mock" if req.mock else "real")
        if not use_mock and not live_enabled():
            raise HTTPException(403, LIVE_CLOSED)
        if not use_mock and missing:
            raise HTTPException(
                400,
                f"No credential resolved for: {', '.join(missing)}. "
                "Paste a key for those workers, borrow one from the pool, or use the stand-in.",
            )

        plan = [p.strip() for p in req.plan if p.strip()]
        if not plan:
            raise HTTPException(400, "Add at least one step.")
        if not req.objective.strip():
            raise HTTPException(400, "Say what you want done.")

        # Per-step ceilings, if the page divided a total rather than fixing one
        # amount per turn. Assignments are numbered from 1.
        budgets = {
            i + 1: max(planner.MIN_BUDGET, b)
            for i, b in enumerate(req.step_budgets)
            if b
        }

        # The store is opened inside the worker thread so the SQLite
        # connection is never shared across threads.
        setup_store = store()
        try:
            orchestrator = ContextOrchestrator(
                registry=reg,
                gateway=MockGateway() if use_mock else live_gateway(),
                store=setup_store,
                compiler=ContextCompiler(token_budget=req.budget),
                mock=use_mock,
            )
            state = orchestrator.create_task(req.objective.strip(), plan)
            task_id = state.task_id
        finally:
            setup_store.close()

        return task_id, reg, use_mock, len(plan), budgets

    def begin(
        req: RunRequest,
        task_id: str,
        reg: WorkerRegistry,
        use_mock: bool,
        budgets: dict[int, int],
    ) -> RunSession:
        """Register the run and start the thread that drives it."""
        session = RunSession(task_id)
        with RUNS_LOCK:
            RUNS[task_id] = session

        def worker() -> None:
            thread_store = SQLiteStore(db)
            try:
                ContextOrchestrator(
                    registry=reg,
                    gateway=MockGateway() if use_mock else live_gateway(),
                    store=thread_store,
                    compiler=ContextCompiler(token_budget=req.budget),
                    events=StreamEvents(session.sink),
                    mock=use_mock,
                    step_budgets=budgets,
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
        task_id, reg, use_mock, steps, budgets = prepare(req)
        begin(req, task_id, reg, use_mock, budgets)
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
        task_id, reg, use_mock, _, budgets = prepare(req)
        session = begin(req, task_id, reg, use_mock, budgets)
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
