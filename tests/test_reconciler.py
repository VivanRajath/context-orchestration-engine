"""State reconciler: the trust boundary.

These tests exist to prove the engine does not simply believe the model.
"""

from __future__ import annotations

from context_orchestration.core.contracts import Issue
from context_orchestration.core.reconciler import StateReconciler
from tests.conftest import make_report, make_result


def reconcile(state, result=None, report=None, worker_id="worker-1", task="Define requirements and architecture."):
    return StateReconciler().reconcile(
        state=state,
        worker_id=worker_id,
        assigned_task=task,
        result=result or make_result(),
        report=report or make_report(),
    )


class TestMerging:
    def test_valid_output_is_merged_into_canonical_state(self, state):
        rec = reconcile(state)
        assert state.completed_tasks == ["Define requirements and architecture."]
        assert any(d.decision == "Use FastAPI" for d in state.decisions)
        assert state.artifact_by_name("architecture.md") is not None
        assert any(f.attempt == "Denormalised table" for f in state.failed_attempts)
        assert rec.accepted["completed_tasks"] == 1

    def test_provenance_is_stamped_by_the_engine(self, state):
        reconcile(state, worker_id="worker-7")
        assert state.decisions[0].recorded_by == "worker-7"
        assert state.artifacts[0].created_by == "worker-7"
        assert state.failed_attempts[0].recorded_by == "worker-7"

    def test_progress_fields_are_updated(self, state):
        reconcile(state)
        assert state.current_progress == "Architecture complete."
        assert state.last_action == "Wrote architecture.md"
        assert state.next_action == "Design the database schema."

    def test_a_second_worker_does_not_duplicate_the_first(self, state):
        reconcile(state, worker_id="worker-1")
        rec = reconcile(state, worker_id="worker-2")
        assert len(state.decisions) == 1
        assert len(state.completed_tasks) == 1
        assert rec.duplicates_skipped["decisions"] == 1
        assert rec.duplicates_skipped["completed_tasks"] == 1

    def test_decisions_from_the_report_are_merged_too(self, state):
        result = make_result(decisions=[])
        report = make_report(important_decisions=[{"decision": "Use Redis", "reason": "Caching"}])
        reconcile(state, result, report)
        assert any(d.decision == "Use Redis" for d in state.decisions)

    def test_the_engine_owns_the_plan_cursor_not_the_worker(self, state, plan):
        """A worker's next_action must not move the plan cursor."""
        result = make_result(next_action="Go do something entirely different")
        reconcile(state, result)
        assert state.current_task == plan[1]
        assert state.next_action == "Go do something entirely different"


class TestTrustBoundary:
    def test_an_issue_is_never_auto_closed_by_an_unverified_claim(self, state):
        state.add_issue(Issue(description="Refresh token rotation unresolved", severity="high", raised_by="worker-1"))
        result = make_result(
            issues=[{"description": "Refresh token rotation unresolved", "severity": "high", "resolved": True}]
        )
        rec = reconcile(state, result, worker_id="worker-2")

        issue = state.issues[0]
        assert issue.resolved is False
        assert issue.resolution_claimed_by == "worker-2"
        assert "Refresh token rotation unresolved" in rec.rejected_resolutions
        assert any("kept open pending verification" in w for w in rec.warnings)

    def test_report_only_artifacts_are_recorded_but_flagged_unverified(self, state):
        result = make_result(artifacts=[])
        report = make_report(artifacts_created_or_modified=["ghost.md"])
        rec = reconcile(state, result, report)

        ghost = state.artifact_by_name("ghost.md")
        assert ghost is not None
        assert ghost.verified is False
        assert "ghost.md" in rec.unverified_artifacts
        assert any("only in the handoff report" in w for w in rec.warnings)

    def test_artifacts_present_in_the_structured_result_are_not_flagged(self, state):
        rec = reconcile(state)
        assert rec.unverified_artifacts == []

    def test_unplanned_completion_claims_are_recorded_and_warned_about(self, state):
        result = make_result(completed_tasks=["Deployed to production"])
        rec = reconcile(state, result)
        assert state.has_completed("Deployed to production")
        assert any("unplanned completion" in w for w in rec.warnings)

    def test_resolution_claim_for_an_issue_never_raised_is_noted_not_stored(self, state):
        result = make_result(issues=[{"description": "Imaginary problem", "resolved": True}])
        rec = reconcile(state, result)
        assert state.issues == []
        assert any("never raised" in w for w in rec.warnings)

    def test_failed_attempts_survive_later_workers(self, state):
        reconcile(state, worker_id="worker-1")
        before = len(state.failed_attempts)
        reconcile(state, make_result(failed_attempts=[]), worker_id="worker-2")
        assert len(state.failed_attempts) == before
        assert state.failed_attempts[0].attempt == "Denormalised table"


class TestWarnings:
    def test_missing_reason_on_a_decision_warns(self, state):
        result = make_result(decisions=[{"decision": "Use MongoDB", "reason": ""}])
        rec = reconcile(state, result)
        assert any("without a reason" in w for w in rec.warnings)

    def test_conflicting_last_action_between_result_and_report_warns(self, state):
        rec = reconcile(state, make_result(last_action="Did X"), make_report(last_action="Did Y"))
        assert any("last_action differs" in w for w in rec.warnings)
        assert state.last_action == "Did X"  # the result wins

    def test_empty_progress_fields_retain_previous_values(self, state):
        state.current_progress = "Previously known progress"
        result = make_result(current_progress="", last_action="", next_action="")
        report = make_report(current_state="", last_action="", recommended_next_action="")
        rec = reconcile(state, result, report)
        assert state.current_progress == "Previously known progress"
        assert any("previous progress retained" in w for w in rec.warnings)

    def test_empty_handoff_notes_warn(self, state):
        rec = reconcile(state, report=make_report(notes_for_next_worker=""))
        assert any("no notes for the next worker" in w for w in rec.warnings)

    def test_no_completed_tasks_warns(self, state):
        rec = reconcile(state, make_result(completed_tasks=[]))
        assert any("claimed no completed tasks" in w for w in rec.warnings)

    def test_summary_is_serializable(self, state):
        import json

        rec = reconcile(state)
        json.dumps(rec.summary())
        assert rec.total_accepted > 0
