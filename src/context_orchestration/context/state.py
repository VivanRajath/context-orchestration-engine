"""The canonical execution state - the thing that actually owns the work.

No worker holds this. No model holds this. The engine holds it, persists it,
and hands compiled slices of it to whichever worker runs next.

The mutation helpers here are deliberately dumb and side-effect-free beyond the
state object itself. All *judgement* about what to merge lives in
``core.reconciler``; this module only knows how to store things without
duplicating them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from context_orchestration.core.contracts import (
    Artifact,
    Assumption,
    Base,
    Decision,
    FailedAttempt,
    HandoffRecord,
    Issue,
    WorkerExecution,
    new_id,
    normalize,
    utcnow,
)


class ExecutionState(Base):
    """Everything known about a task, independent of any model or session."""

    task_id: str = Field(default_factory=lambda: new_id("task"))
    objective: str

    completed_tasks: list[str] = Field(default_factory=list)
    current_task: str = ""
    pending_tasks: list[str] = Field(default_factory=list)

    decisions: list[Decision] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    failed_attempts: list[FailedAttempt] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)

    current_progress: str = ""
    last_action: str = ""
    next_action: str = ""

    worker_history: list[WorkerExecution] = Field(default_factory=list)
    handoff_history: list[HandoffRecord] = Field(default_factory=list)

    status: str = "created"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # ---- queries -----------------------------------------------------

    @property
    def open_issues(self) -> list[Issue]:
        return [i for i in self.issues if not i.resolved]

    def artifact_by_name(self, name: str) -> Artifact | None:
        key = normalize(name)
        return next((a for a in self.artifacts if a.key == key), None)

    def has_completed(self, task: str) -> bool:
        key = normalize(task)
        return any(normalize(t) == key for t in self.completed_tasks)

    def last_handoff(self) -> HandoffRecord | None:
        return self.handoff_history[-1] if self.handoff_history else None

    # ---- mutations (dedupe-aware, never destructive) ------------------

    def touch(self) -> None:
        self.updated_at = utcnow()

    def add_completed_task(self, task: str) -> bool:
        task = (task or "").strip()
        if not task or self.has_completed(task):
            return False
        self.completed_tasks.append(task)
        key = normalize(task)
        self.pending_tasks = [p for p in self.pending_tasks if normalize(p) != key]
        return True

    def add_decision(self, decision: Decision) -> bool:
        if not decision.decision.strip():
            return False
        if any(d.key == decision.key for d in self.decisions):
            return False
        self.decisions.append(decision)
        return True

    def upsert_artifact(self, artifact: Artifact) -> str:
        """Returns "created" or "updated". Artifacts are versioned, never lost."""
        if not artifact.name.strip():
            return "skipped"
        existing = self.artifact_by_name(artifact.name)
        if existing is None:
            self.artifacts.append(artifact)
            return "created"
        existing.version += 1
        existing.updated_at = utcnow()
        if artifact.description:
            existing.description = artifact.description
        if artifact.content is not None:
            existing.content = artifact.content
        if artifact.kind and artifact.kind != "document":
            existing.kind = artifact.kind
        if artifact.created_by not in existing.modified_by and artifact.created_by != existing.created_by:
            existing.modified_by.append(artifact.created_by)
        existing.verified = existing.verified or artifact.verified
        return "updated"

    def add_issue(self, issue: Issue) -> bool:
        if not issue.description.strip():
            return False
        if any(i.key == issue.key for i in self.issues):
            return False
        self.issues.append(issue)
        return True

    def add_failed_attempt(self, attempt: FailedAttempt) -> bool:
        """Failed attempts are never removed - that is their whole purpose."""
        if not attempt.attempt.strip():
            return False
        if any(f.key == attempt.key for f in self.failed_attempts):
            return False
        self.failed_attempts.append(attempt)
        return True

    def add_assumption(self, assumption: Assumption) -> bool:
        if not assumption.assumption.strip():
            return False
        if any(a.key == assumption.key for a in self.assumptions):
            return False
        self.assumptions.append(assumption)
        return True


def create_state(objective: str, plan: list[str] | None = None, task_id: str | None = None) -> ExecutionState:
    plan = [p.strip() for p in (plan or []) if p.strip()]
    state = ExecutionState(objective=objective.strip(), pending_tasks=list(plan))
    if task_id:
        state.task_id = task_id
    if plan:
        state.current_task = plan[0]
        state.next_action = plan[0]
    state.current_progress = "Not started."
    state.last_action = "Task created."
    return state
