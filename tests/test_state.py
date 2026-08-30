"""Canonical execution state: dedupe, versioning, and append-only guarantees."""

from __future__ import annotations

from context_orchestration.context.state import ExecutionState, create_state
from context_orchestration.core.contracts import Artifact, Assumption, Decision, FailedAttempt, Issue


def test_create_state_seeds_plan_and_cursor(plan):
    s = create_state("Objective", plan)
    assert s.objective == "Objective"
    assert s.pending_tasks == plan
    assert s.current_task == plan[0]
    assert s.next_action == plan[0]
    assert s.task_id.startswith("task-")


def test_completing_a_task_removes_it_from_pending(state, plan):
    assert state.add_completed_task(plan[0]) is True
    assert plan[0] in state.completed_tasks
    assert plan[0] not in state.pending_tasks


def test_completed_tasks_dedupe_case_and_punctuation_insensitively(state):
    assert state.add_completed_task("Design the database schema.") is True
    assert state.add_completed_task("design the DATABASE schema") is False
    assert len(state.completed_tasks) == 1


def test_blank_entries_are_rejected(state):
    assert state.add_completed_task("   ") is False
    assert state.add_decision(Decision(decision="  ")) is False
    assert state.add_issue(Issue(description="")) is False
    assert state.add_failed_attempt(FailedAttempt(attempt="")) is False
    assert state.add_assumption(Assumption(assumption="")) is False


def test_decisions_dedupe_but_preserve_the_first_reason(state):
    state.add_decision(Decision(decision="Use FastAPI", reason="Async", recorded_by="worker-1"))
    assert state.add_decision(Decision(decision="use fastapi", reason="Other", recorded_by="worker-2")) is False
    assert len(state.decisions) == 1
    assert state.decisions[0].reason == "Async"
    assert state.decisions[0].recorded_by == "worker-1"


def test_artifacts_are_versioned_not_duplicated(state):
    assert state.upsert_artifact(Artifact(name="schema.sql", created_by="worker-1")) == "created"
    assert state.upsert_artifact(
        Artifact(name="schema.sql", description="Now with indexes", created_by="worker-2")
    ) == "updated"

    assert len(state.artifacts) == 1
    art = state.artifacts[0]
    assert art.version == 2
    assert art.created_by == "worker-1"
    assert art.modified_by == ["worker-2"]
    assert art.description == "Now with indexes"


def test_artifact_lookup_is_normalized(state):
    state.upsert_artifact(Artifact(name="Schema.SQL", created_by="w"))
    assert state.artifact_by_name("schema.sql") is not None


def test_failed_attempts_are_append_only_and_deduped(state):
    state.add_failed_attempt(FailedAttempt(attempt="Offset pagination", reason="Unstable", recorded_by="worker-1"))
    assert state.add_failed_attempt(FailedAttempt(attempt="offset pagination", recorded_by="worker-2")) is False
    assert len(state.failed_attempts) == 1
    # nothing in the public surface removes a failed attempt
    assert not any(name.startswith("remove") or name.startswith("clear") for name in dir(state))


def test_open_issues_excludes_resolved(state):
    state.add_issue(Issue(description="A", raised_by="w"))
    state.add_issue(Issue(description="B", raised_by="w"))
    state.issues[0].resolved = True
    assert [i.description for i in state.open_issues] == ["B"]


def test_state_round_trips_through_json(rich_state):
    restored = ExecutionState.model_validate_json(rich_state.model_dump_json())
    assert restored.task_id == rich_state.task_id
    assert restored.decisions[0].decision == rich_state.decisions[0].decision
    assert restored.failed_attempts[0].attempt == rich_state.failed_attempts[0].attempt
    assert restored.artifacts[0].name == rich_state.artifacts[0].name
