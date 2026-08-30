"""``Engine`` - the one class most users need.

The components underneath (registry, gateway, compiler, reconciler, store,
orchestrator) are all public and swappable, but wiring them by hand is
boilerplate. This facade does the wiring and nothing else: every method here
delegates, so anything you can do with ``Engine`` you can also do by
assembling the parts yourself.

    from context_orchestration import Engine

    with Engine.from_config("workers.json", db="run.db") as engine:
        result = engine.run(
            objective="Design a notification service.",
            plan=["Define requirements.", "Design delivery.", "Review it."],
        )
        print(result.summary.workers_used, "workers,",
              result.summary.raw_conversation_transfers, "raw transfers")

Credentials are read from the environment (or a local ``.env``) exactly as the
CLI reads them. The engine never writes an API key to disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from context_orchestration.context.compiler import ContextCompiler, TokenEstimator
from context_orchestration.context.state import ExecutionState
from context_orchestration.core.contracts import (
    Assignment,
    Base,
    HandoffPackage,
    HandoffRecord,
    WorkerConfig,
    WorkerExecution,
)
from context_orchestration.core.orchestrator import (
    ContextOrchestrator,
    OrchestratorEvents,
    RunSummary,
    SwitchPolicy,
    WorkerRegistry,
    resolve_mock_mode,
)
from context_orchestration.core.reconciler import StateReconciler
from context_orchestration.gateway.llm_gateway import LLMGateway, build_gateway
from context_orchestration.storage.sqlite_store import DEFAULT_DB, SQLiteStore


class RunResult(Base):
    """What a run produced: the canonical state plus the run's audit numbers."""

    state: ExecutionState
    summary: RunSummary

    @property
    def task_id(self) -> str:
        return self.state.task_id

    @property
    def continuity_maintained(self) -> bool:
        return self.summary.continuity_maintained


class Engine:
    """Facade over the Context Orchestration Engine."""

    def __init__(
        self,
        workers: Iterable[WorkerConfig | dict],
        *,
        db: str | Path = DEFAULT_DB,
        token_budget: int = 1600,
        mock: bool | str = "auto",
        gateway: LLMGateway | None = None,
        compiler: ContextCompiler | None = None,
        reconciler: StateReconciler | None = None,
        estimator: TokenEstimator | None = None,
        policy: SwitchPolicy | None = None,
        events: OrchestratorEvents | None = None,
    ) -> None:
        configs = [w if isinstance(w, WorkerConfig) else WorkerConfig.model_validate(w) for w in workers]
        self.registry = WorkerRegistry(configs)

        requested = mock if isinstance(mock, str) else ("mock" if mock else "real")
        self.use_mock, self.missing_credentials = resolve_mock_mode(self.registry, requested)

        self.store = SQLiteStore(db)
        self.orchestrator = ContextOrchestrator(
            registry=self.registry,
            gateway=gateway or build_gateway(self.use_mock),
            store=self.store,
            compiler=compiler or ContextCompiler(token_budget=token_budget, estimator=estimator),
            reconciler=reconciler,
            events=events,
            policy=policy,
            mock=self.use_mock,
        )

    # -- construction ---------------------------------------------------

    @classmethod
    def from_config(cls, path: str | Path, **kwargs) -> "Engine":
        """Build from a workers.json file (the same format the CLI reads)."""
        return cls(WorkerRegistry.from_file(path).configs, **kwargs)

    @classmethod
    def demo(cls, **kwargs) -> "Engine":
        """The packaged five-worker example roster. Handy for a first run."""
        from context_orchestration.core.orchestrator import DEFAULT_WORKERS_PATH

        return cls.from_config(DEFAULT_WORKERS_PATH, **kwargs)

    # -- execution ------------------------------------------------------

    def run(self, objective: str, plan: Sequence[str], *, task_id: str | None = None) -> RunResult:
        """Create a task and run every configured worker over it."""
        state, summary = self.orchestrator.run(objective, plan, task_id=task_id)
        return RunResult(state=state, summary=summary)

    def create(self, objective: str, plan: Sequence[str], *, task_id: str | None = None) -> ExecutionState:
        """Create and persist a task without running anything yet."""
        return self.orchestrator.create_task(objective, plan, task_id=task_id)

    def resume(self, task_id: str) -> RunResult:
        """Run every assignment that has not completed yet."""
        state, summary = self.orchestrator.resume(task_id)
        return RunResult(state=state, summary=summary)

    def step(self, task_id: str, steps: int = 1) -> RunResult:
        """Advance the task by ``steps`` worker turns and stop.

        Lets a host application drive the loop - run one worker, inspect the
        reconciled state, decide whether to continue.
        """
        state, summary = self.orchestrator.resume(task_id, max_steps=steps)
        return RunResult(state=state, summary=summary)

    # -- inspection -----------------------------------------------------

    def state(self, task_id: str) -> ExecutionState | None:
        return self.store.load_state(task_id)

    def history(self, task_id: str) -> list[WorkerExecution]:
        return self.store.load_worker_executions(task_id)

    def handoffs(self, task_id: str) -> list[HandoffRecord]:
        return self.store.load_handoffs(task_id)

    def packages(self, task_id: str) -> list[HandoffPackage]:
        """Every context package that crossed a worker boundary."""
        return self.store.load_packages(task_id)

    def events(self, task_id: str) -> list[dict]:
        return self.store.load_events(task_id)

    def tasks(self, limit: int = 50) -> list[dict]:
        return self.store.list_tasks(limit)

    def assignments(self, task_id: str) -> list[Assignment]:
        meta = self.store.load_task_meta(task_id)
        return meta["assignments"] if meta else []

    def resolve(self, prefix: str) -> str | None:
        """Expand an unambiguous task-id prefix to the full id."""
        return self.store.resolve_task_id(prefix)

    # -- standalone context compilation ---------------------------------

    def compile_context(
        self, state: ExecutionState, assigned_task: str, worker_id: str = "worker"
    ) -> HandoffPackage:
        """Compile a context package from any state, without running a worker.

        Useful on its own: point it at your own ``ExecutionState`` to get a
        bounded, prioritized prompt context with no model call involved.
        """
        return self.orchestrator.compiler.compile(state, assigned_task, worker_id)

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        mode = "mock" if self.use_mock else "live"
        return f"<Engine workers={len(self.registry)} mode={mode}>"
