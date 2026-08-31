"""The Context Orchestrator.

This module owns the loop:

    compile context -> run a worker -> reconcile its claims -> persist -> repeat

It knows nothing about providers, and nothing about how many workers exist. It
walks a list of ``Assignment`` objects, and for each one it builds a fresh
context package from canonical state. No conversation is carried forward,
because no conversation is ever stored.

Worker switching is sequential and forced in this MVP. The seam for smarter
switching is ``SwitchPolicy`` - swap the policy, and context-limit / cost /
failure-driven switching drops in without touching this loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Protocol, Sequence

from pydantic import Field

from context_orchestration.context.compiler import ContextCompiler
from context_orchestration.context.handoff import build_handoff_record, handoff_audit
from context_orchestration.context.state import ExecutionState, create_state
from context_orchestration.core.contracts import (
    Assignment,
    Base,
    HandoffPackage,
    WorkerConfig,
    WorkerExecution,
    WorkerRun,
    WorkerStatus,
    utcnow,
)
from context_orchestration.core.reconciler import ReconciliationReport, StateReconciler
from context_orchestration.core.worker import UniversalWorker
from context_orchestration.gateway.llm_gateway import GatewayError, LLMGateway
from context_orchestration.storage.sqlite_store import SQLiteStore


# --------------------------------------------------------------------------
# Worker registry - dynamic, any count, no hardcoding
# --------------------------------------------------------------------------


class WorkerRegistry:
    """Loads worker definitions from configuration at runtime."""

    def __init__(self, configs: Sequence[WorkerConfig]) -> None:
        self.configs = [c for c in configs if c.enabled]
        if not self.configs:
            raise ValueError("no enabled workers configured")
        seen = set()
        for c in self.configs:
            if c.id in seen:
                raise ValueError(f"duplicate worker id: {c.id}")
            seen.add(c.id)

    def __len__(self) -> int:
        return len(self.configs)

    def __iter__(self):
        return iter(self.configs)

    def get(self, worker_id: str) -> WorkerConfig:
        for c in self.configs:
            if c.id == worker_id:
                return c
        raise KeyError(f"unknown worker: {worker_id}")

    @classmethod
    def from_file(cls, path: str | Path) -> "WorkerRegistry":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = data.get("workers", data) if isinstance(data, dict) else data
        return cls([WorkerConfig.model_validate(e) for e in entries])

    @classmethod
    def from_dicts(cls, entries: Sequence[dict]) -> "WorkerRegistry":
        return cls([WorkerConfig.model_validate(e) for e in entries])


# --------------------------------------------------------------------------
# Switching policy - the seam for future automatic switching
# --------------------------------------------------------------------------


class SwitchPolicy(Protocol):
    """Decides which worker handles step ``index`` of the plan."""

    def assign(self, index: int, registry: WorkerRegistry, state: ExecutionState) -> WorkerConfig: ...


class SequentialSwitchPolicy:
    """Forced round-robin: worker i handles step i, wrapping if the plan is longer.

    Deliberately dumb. Context-limit, cost, rate-limit and capability-driven
    switching are all implementations of this same interface.
    """

    def assign(self, index: int, registry: WorkerRegistry, state: ExecutionState) -> WorkerConfig:
        return registry.configs[index % len(registry.configs)]


def build_assignments(
    plan: Sequence[str],
    registry: WorkerRegistry,
    policy: SwitchPolicy | None = None,
    state: ExecutionState | None = None,
) -> list[Assignment]:
    """Map plan steps onto workers, covering every worker and every step.

    If there are more workers than plan steps, the surplus workers are given
    continuation turns rather than being dropped - the run should exercise
    every configured worker.
    """
    policy = policy or SequentialSwitchPolicy()
    steps = [s.strip() for s in plan if s.strip()]
    if not steps:
        raise ValueError("plan is empty")

    if len(steps) < len(registry):
        steps = steps + [
            "Continue the objective from the current execution state, "
            "acting on the recommended next action."
        ] * (len(registry) - len(steps))

    # A policy may consult state; at plan time there may not be one yet.
    state = state or ExecutionState(objective="")
    return [
        Assignment(seq=i + 1, worker_id=policy.assign(i, registry, state).id, task=step)
        for i, step in enumerate(steps)
    ]


# --------------------------------------------------------------------------
# Run reporting
# --------------------------------------------------------------------------


class RunSummary(Base):
    task_id: str
    workers_used: int = 0
    raw_conversation_transfers: int = 0
    structured_handoffs: int = 0
    reports_generated: int = 0
    packages_compiled: int = 0
    state_persisted: bool = False
    failures: list[str] = Field(default_factory=list)
    total_context_tokens: int = 0
    reconciliation_warnings: int = 0
    already_complete: bool = False
    previously_completed: int = 0

    @property
    def continuity_maintained(self) -> bool:
        return (
            self.raw_conversation_transfers == 0
            and self.reports_generated == self.workers_used
            and self.workers_used > 0
            and not self.failures
        )


# Hooks let the CLI render live output without the orchestrator importing Rich.
class OrchestratorEvents:
    def run_started(self, state: ExecutionState, assignments: list[Assignment], registry: WorkerRegistry, mock: bool) -> None: ...
    def package_compiled(self, package: HandoffPackage, audit: dict | None) -> None: ...
    def worker_started(self, assignment: Assignment, config: WorkerConfig, package: HandoffPackage) -> None: ...
    def worker_completed(self, run: WorkerRun) -> None: ...
    def worker_failed(self, assignment: Assignment, config: WorkerConfig, error: Exception) -> None: ...
    def reconciled(self, report: ReconciliationReport, state: ExecutionState) -> None: ...
    def handoff(self, audit: dict) -> None: ...
    def run_finished(self, summary: RunSummary, state: ExecutionState) -> None: ...


class ContextOrchestrator:
    """Owns the canonical execution state and drives workers across it."""

    def __init__(
        self,
        registry: WorkerRegistry,
        gateway: LLMGateway,
        store: SQLiteStore,
        compiler: ContextCompiler | None = None,
        reconciler: StateReconciler | None = None,
        events: OrchestratorEvents | None = None,
        policy: SwitchPolicy | None = None,
        worker_factory: Callable[[WorkerConfig, LLMGateway], UniversalWorker] | None = None,
        mock: bool = False,
        step_budgets: dict[int, int] | None = None,
    ) -> None:
        self.registry = registry
        self.gateway = gateway
        self.store = store
        self.compiler = compiler or ContextCompiler()
        self.reconciler = reconciler or StateReconciler()
        self.events = events or OrchestratorEvents()
        self.policy = policy or SequentialSwitchPolicy()
        self.worker_factory = worker_factory or UniversalWorker
        self.mock = mock
        # seq -> how much this one step's worker may be told. Absent means the
        # compiler's own budget applies, which is the ordinary case; a caller
        # that wants to spend a total across a plan rather than a fixed amount
        # per turn fills this in. Nothing else in the loop changes.
        self.step_budgets = dict(step_budgets or {})

    # -- task lifecycle --------------------------------------------------

    def create_task(self, objective: str, plan: Sequence[str], task_id: str | None = None) -> ExecutionState:
        state = create_state(objective, list(plan), task_id=task_id)
        assignments = build_assignments(plan, self.registry, self.policy, state)
        self.store.create_task(state, list(plan), list(self.registry), assignments)
        return state

    def run(self, objective: str, plan: Sequence[str], task_id: str | None = None) -> tuple[ExecutionState, RunSummary]:
        state = self.create_task(objective, plan, task_id=task_id)
        meta = self.store.load_task_meta(state.task_id)
        return self._execute(state, meta["assignments"], start_seq=1)

    def resume(self, task_id: str, max_steps: int | None = None) -> tuple[ExecutionState, RunSummary]:
        meta = self.store.load_task_meta(task_id)
        if meta is None:
            raise KeyError(f"unknown task: {task_id}")
        state = self.store.load_state(task_id)
        if state is None:
            raise KeyError(f"task has no persisted state: {task_id}")

        done = {e.seq for e in self.store.load_worker_executions(task_id) if e.status == WorkerStatus.COMPLETED}
        remaining = [a for a in meta["assignments"] if a.seq not in done]
        if not remaining:
            # Nothing ran in this invocation - say so rather than reporting
            # the already-completed workers as if they had just executed.
            return state, RunSummary(
                task_id=task_id,
                workers_used=0,
                state_persisted=True,
                already_complete=True,
                previously_completed=len(done),
            )
        return self._execute(
            state,
            meta["assignments"],
            start_seq=remaining[0].seq,
            completed=done,
            max_steps=max_steps,
        )

    def _has_remaining(self, state: ExecutionState, assignments: list[Assignment]) -> bool:
        done = {e.seq for e in state.worker_history if e.status == WorkerStatus.COMPLETED}
        return any(a.seq not in done for a in assignments)

    # -- the loop ---------------------------------------------------------

    def _execute(
        self,
        state: ExecutionState,
        assignments: list[Assignment],
        start_seq: int,
        completed: set[int] | None = None,
        max_steps: int | None = None,
    ) -> tuple[ExecutionState, RunSummary]:
        completed = completed or set()
        pending = [a for a in assignments if a.seq >= start_seq and a.seq not in completed]
        if max_steps is not None:
            # Single-stepping lets a host application drive the loop itself.
            pending = pending[:max_steps]

        summary = RunSummary(task_id=state.task_id)
        state.status = "running"
        self.store.save_state(state)
        self.events.run_started(state, pending, self.registry, self.mock)

        previous_worker: str | None = None

        for assignment in pending:
            config = self.registry.get(assignment.worker_id)
            state.current_task = assignment.task

            package = self.compiler.compile(
                state=state,
                assigned_task=assignment.task,
                target_worker_id=assignment.worker_id,
                token_budget=self.step_budgets.get(assignment.seq),
            )
            self.store.save_package(state.task_id, assignment.seq, package)
            summary.packages_compiled += 1

            audit = None
            if previous_worker is not None:
                audit = handoff_audit(state, package, previous_worker, assignment.worker_id)
                self.store.log_event(state.task_id, "context_handoff", audit)
                self.events.handoff(audit)
                if audit["raw_conversation_transferred"]:
                    summary.raw_conversation_transfers += 1

            self.events.package_compiled(package, audit)

            execution = WorkerExecution(
                seq=assignment.seq,
                worker_id=assignment.worker_id,
                model=config.model,
                assigned_task=assignment.task,
                status=WorkerStatus.RUNNING,
                context_tokens_in=package.estimated_tokens,
                context_package_id=package.package_id,
                raw_conversation_transferred=package.contains_raw_conversation,
            )
            self.events.worker_started(assignment, config, package)

            worker = self.worker_factory(config, self.gateway)
            try:
                run = worker.execute(package)
            except GatewayError as exc:
                execution.status = WorkerStatus.FAILED
                execution.error = str(exc)
                execution.finished_at = utcnow()
                state.worker_history.append(execution)
                self.store.save_worker_execution(state.task_id, execution)
                self.store.save_state(state, seq=assignment.seq)
                self.store.log_event(state.task_id, "worker_failed", {"worker": config.id, "error": str(exc)})
                summary.failures.append(f"{config.id}: {exc}")
                summary.state_persisted = True  # the failure itself was persisted
                self.events.worker_failed(assignment, config, exc)
                # previous_worker is deliberately NOT advanced: a worker that
                # never ran produced no handoff, so the next worker inherits
                # from the last worker that actually completed.
                continue

            self.events.worker_completed(run)
            summary.reports_generated += 1
            summary.workers_used += 1
            if audit is not None:
                # A handoff only counts once the receiving worker actually ran.
                summary.structured_handoffs += 1
            summary.total_context_tokens += run.context_tokens_in
            if run.raw_conversation_transferred:
                summary.raw_conversation_transfers += 1

            # --- trust boundary: reconcile before believing anything ---
            reconciliation = self.reconciler.reconcile(
                state=state,
                worker_id=assignment.worker_id,
                assigned_task=assignment.task,
                result=run.result,
                report=run.report,
            )
            summary.reconciliation_warnings += len(reconciliation.warnings)
            self.events.reconciled(reconciliation, state)

            execution.status = WorkerStatus.COMPLETED
            execution.summary = run.result.summary
            execution.finished_at = utcnow()
            execution.duration_ms = run.duration_ms
            execution.messages_sent = run.messages_sent
            execution.reconciliation_summary = reconciliation.summary()
            state.worker_history.append(execution)

            next_worker = next((a.worker_id for a in pending if a.seq == assignment.seq + 1), None)
            record = build_handoff_record(
                seq=assignment.seq,
                from_worker=assignment.worker_id,
                to_worker=next_worker,
                report=run.report,
                package=package,
            )
            state.handoff_history.append(record)

            # Persist after every single worker turn - this is what resume needs.
            self.store.save_worker_execution(state.task_id, execution, run.result)
            self.store.save_handoff(state.task_id, record)
            self.store.save_state(state, seq=assignment.seq)
            self.store.log_event(
                state.task_id,
                "worker_completed",
                {
                    "worker": assignment.worker_id,
                    "model": config.model,
                    "seq": assignment.seq,
                    "reconciliation": reconciliation.summary(),
                },
            )
            summary.state_persisted = True
            previous_worker = assignment.worker_id

        if summary.failures:
            state.status = "failed"
        elif max_steps is not None and self._has_remaining(state, assignments):
            state.status = "paused"  # single-stepped; more assignments remain
        else:
            state.status = "completed"
        if not state.pending_tasks:
            state.current_task = ""
        self.store.save_state(state)
        self.store.set_status(state.task_id, state.status)
        self.events.run_finished(summary, state)
        return state, summary


# --------------------------------------------------------------------------
# Config loading helpers
# --------------------------------------------------------------------------

# Shipped with the package as a starting point; users supply their own.
DEFAULT_WORKERS_PATH = Path(__file__).resolve().parent.parent / "config" / "workers.example.json"


def load_registry(path: str | Path | None = None) -> WorkerRegistry:
    return WorkerRegistry.from_file(path or DEFAULT_WORKERS_PATH)


def resolve_mock_mode(registry: WorkerRegistry, requested: str) -> tuple[bool, list[str]]:
    """``requested`` is 'auto' | 'mock' | 'real'. Returns (use_mock, missing_keys)."""
    from context_orchestration.gateway.llm_gateway import missing_keys

    missing = missing_keys(list(registry))
    if requested == "mock":
        return True, missing
    if requested == "real":
        return False, missing
    return bool(missing), missing
