"""The State Reconciler - the trust boundary of the engine.

A worker's result and handoff report are *claims*. They are generated text.
They may exaggerate, hallucinate an artifact, silently drop an open issue, or
declare a problem solved that nobody verified.

This module is where those claims meet the canonical state. Its job is not to
believe the worker; it is to merge what the worker said into what the engine
knows, while recording every discrepancy it noticed.

Trust rules enforced here:

* Artifacts a worker only mentions in its handoff report - but never listed in
  its structured result - are recorded as ``verified=False``.
* An issue is **never** auto-closed by an unverified claim. The claim is
  recorded on the issue (``resolution_claimed_by``) and the issue stays open.
* Failed attempts are append-only. Nothing a later worker says removes them.
* The engine, not the worker, decides what the current task is. A worker's
  ``next_action`` is advisory.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from context_orchestration.context.state import ExecutionState
from context_orchestration.core.contracts import (
    Artifact,
    Assumption,
    Base,
    Decision,
    FailedAttempt,
    HandoffReport,
    Issue,
    WorkerResult,
    normalize,
)


class ReconciliationReport(Base):
    """What the reconciler did, and what it refused to take at face value."""

    worker_id: str
    accepted: dict[str, int] = Field(default_factory=dict)
    duplicates_skipped: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    unverified_artifacts: list[str] = Field(default_factory=list)
    rejected_resolutions: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "accepted": {k: v for k, v in self.accepted.items() if v},
            "duplicates_skipped": {k: v for k, v in self.duplicates_skipped.items() if v},
            "warnings": self.warnings,
            "unverified_artifacts": self.unverified_artifacts,
            "rejected_resolutions": self.rejected_resolutions,
        }

    @property
    def total_accepted(self) -> int:
        return sum(self.accepted.values())

    @property
    def total_duplicates(self) -> int:
        return sum(self.duplicates_skipped.values())


class StateReconciler:
    """Merges an untrusted worker turn into trusted canonical state."""

    def reconcile(
        self,
        state: ExecutionState,
        worker_id: str,
        assigned_task: str,
        result: WorkerResult,
        report: HandoffReport,
    ) -> ReconciliationReport:
        rec = ReconciliationReport(worker_id=worker_id)
        for bucket in (
            "completed_tasks",
            "decisions",
            "artifacts",
            "issues",
            "failed_attempts",
            "assumptions",
        ):
            rec.accepted[bucket] = 0
            rec.duplicates_skipped[bucket] = 0

        self._merge_completed(state, rec, assigned_task, result)
        self._merge_decisions(state, rec, worker_id, result, report)
        self._merge_artifacts(state, rec, worker_id, result, report)
        self._merge_issues(state, rec, worker_id, result)
        self._merge_failed_attempts(state, rec, worker_id, result, report)
        self._merge_assumptions(state, rec, worker_id, result, report)
        self._update_progress(state, rec, result, report)
        self._advance_task(state)
        self._cross_check(rec, result, report)

        state.touch()
        return rec

    # -- merge steps ----------------------------------------------------

    def _merge_completed(
        self, state: ExecutionState, rec: ReconciliationReport, assigned_task: str, result: WorkerResult
    ) -> None:
        planned = {normalize(t) for t in state.pending_tasks} | {normalize(assigned_task)}
        for task in result.completed_tasks:
            if state.add_completed_task(task):
                rec.accepted["completed_tasks"] += 1
                if normalize(task) not in planned:
                    rec.warnings.append(
                        f"unplanned completion claimed and recorded: {task!r}"
                    )
            else:
                rec.duplicates_skipped["completed_tasks"] += 1

        if not result.completed_tasks:
            rec.warnings.append("worker claimed no completed tasks")

    def _merge_decisions(
        self,
        state: ExecutionState,
        rec: ReconciliationReport,
        worker_id: str,
        result: WorkerResult,
        report: HandoffReport,
    ) -> None:
        seen: dict[str, str] = {}
        for raw in list(result.decisions) + list(report.important_decisions):
            text = (raw.decision or "").strip()
            if not text:
                continue
            key = normalize(text)
            if key in seen:
                continue
            seen[key] = raw.reason
            if not raw.reason.strip():
                rec.warnings.append(f"decision recorded without a reason: {text!r}")
            if state.add_decision(
                Decision(decision=text, reason=raw.reason.strip(), recorded_by=worker_id)
            ):
                rec.accepted["decisions"] += 1
            else:
                rec.duplicates_skipped["decisions"] += 1

    def _merge_artifacts(
        self,
        state: ExecutionState,
        rec: ReconciliationReport,
        worker_id: str,
        result: WorkerResult,
        report: HandoffReport,
    ) -> None:
        structured = {normalize(a.name) for a in result.artifacts if a.name.strip()}

        for raw in result.artifacts:
            if not raw.name.strip():
                continue
            outcome = state.upsert_artifact(
                Artifact(
                    name=raw.name.strip(),
                    kind=raw.kind or "document",
                    description=raw.description,
                    content=raw.content,
                    created_by=worker_id,
                    verified=False,
                )
            )
            if outcome == "created":
                rec.accepted["artifacts"] += 1
            elif outcome == "updated":
                rec.duplicates_skipped["artifacts"] += 1

        # Mentioned in the report but absent from the structured result: the
        # weakest form of evidence, so it is recorded and flagged, not trusted.
        for name in report.artifacts_created_or_modified:
            if normalize(name) in structured:
                continue
            outcome = state.upsert_artifact(
                Artifact(
                    name=name.strip(),
                    description="Mentioned in handoff report only.",
                    created_by=worker_id,
                    verified=False,
                )
            )
            rec.unverified_artifacts.append(name.strip())
            rec.warnings.append(
                f"artifact {name.strip()!r} appears only in the handoff report, not in the structured result"
            )
            if outcome == "created":
                rec.accepted["artifacts"] += 1
            elif outcome == "updated":
                rec.duplicates_skipped["artifacts"] += 1

    def _merge_issues(
        self, state: ExecutionState, rec: ReconciliationReport, worker_id: str, result: WorkerResult
    ) -> None:
        for raw in result.issues:
            desc = (raw.description or "").strip()
            if not desc:
                continue
            existing = next((i for i in state.issues if i.key == normalize(desc)), None)

            if raw.resolved:
                # A resolution claim is evidence, not proof. The issue stays open.
                if existing is not None:
                    existing.resolution_claimed_by = worker_id
                    rec.rejected_resolutions.append(desc)
                    rec.warnings.append(
                        f"worker claimed issue resolved; kept open pending verification: {desc!r}"
                    )
                    rec.duplicates_skipped["issues"] += 1
                # A claim to have resolved an issue nobody ever raised is noise.
                else:
                    rec.warnings.append(
                        f"worker claimed resolution of an issue that was never raised: {desc!r}"
                    )
                continue

            if state.add_issue(
                Issue(description=desc, severity=raw.severity, raised_by=worker_id)
            ):
                rec.accepted["issues"] += 1
            else:
                rec.duplicates_skipped["issues"] += 1

    def _merge_failed_attempts(
        self,
        state: ExecutionState,
        rec: ReconciliationReport,
        worker_id: str,
        result: WorkerResult,
        report: HandoffReport,
    ) -> None:
        entries = [(f.attempt, f.reason) for f in result.failed_attempts]
        entries += [(text, "") for text in report.failed_attempts]
        for attempt, reason in entries:
            attempt = (attempt or "").strip()
            if not attempt:
                continue
            if state.add_failed_attempt(
                FailedAttempt(attempt=attempt, reason=(reason or "").strip(), recorded_by=worker_id)
            ):
                rec.accepted["failed_attempts"] += 1
            else:
                rec.duplicates_skipped["failed_attempts"] += 1

    def _merge_assumptions(
        self,
        state: ExecutionState,
        rec: ReconciliationReport,
        worker_id: str,
        result: WorkerResult,
        report: HandoffReport,
    ) -> None:
        entries = [(a.assumption, a.reason) for a in result.assumptions]
        entries += [(text, "") for text in report.assumptions]
        for text, reason in entries:
            text = (text or "").strip()
            if not text:
                continue
            if state.add_assumption(
                Assumption(assumption=text, reason=(reason or "").strip(), recorded_by=worker_id)
            ):
                rec.accepted["assumptions"] += 1
            else:
                rec.duplicates_skipped["assumptions"] += 1

    def _update_progress(
        self,
        state: ExecutionState,
        rec: ReconciliationReport,
        result: WorkerResult,
        report: HandoffReport,
    ) -> None:
        progress = result.current_progress.strip() or report.current_state.strip()
        if progress:
            state.current_progress = progress
        else:
            rec.warnings.append("no current_progress reported; previous progress retained")

        last = result.last_action.strip() or report.last_action.strip()
        if last:
            state.last_action = last
        else:
            rec.warnings.append("no last_action reported; previous last_action retained")

        nxt = result.next_action.strip() or report.recommended_next_action.strip()
        if nxt:
            state.next_action = nxt
        else:
            rec.warnings.append("no next_action reported; previous next_action retained")

    @staticmethod
    def _advance_task(state: ExecutionState) -> None:
        """The engine owns the plan cursor - not the worker's next_action."""
        state.current_task = state.pending_tasks[0] if state.pending_tasks else ""

    @staticmethod
    def _cross_check(rec: ReconciliationReport, result: WorkerResult, report: HandoffReport) -> None:
        if result.last_action.strip() and report.last_action.strip():
            if normalize(result.last_action) != normalize(report.last_action):
                rec.warnings.append(
                    "last_action differs between worker result and handoff report; "
                    "canonical state uses the worker result"
                )
        if not report.notes_for_next_worker.strip():
            rec.warnings.append("handoff report carries no notes for the next worker")
        if result.completed_tasks and not report.work_completed:
            rec.warnings.append("handoff report omits work the worker result claims to have completed")
