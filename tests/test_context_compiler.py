"""Context compiler: prioritization, token budgeting, and the no-history rule."""

from __future__ import annotations

import pytest

from context_orchestration.context.compiler import (
    ContextCompiler,
    HeuristicTokenEstimator,
    relevance,
)
from context_orchestration.context.state import create_state
from context_orchestration.core.contracts import Artifact, Decision, FailedAttempt, Issue


def compile_for(state, task="Design the database schema.", worker="worker-2", budget=None):
    compiler = ContextCompiler(token_budget=budget or 1200)
    return compiler.compile(state, task, worker)


def test_package_carries_task_and_objective(rich_state):
    pkg = compile_for(rich_state)
    assert pkg.assigned_task == "Design the database schema."
    assert pkg.target_worker_id == "worker-2"
    assert "YOUR ASSIGNED TASK" in pkg.rendered_text
    assert rich_state.objective in pkg.rendered_text
    assert pkg.task_id == rich_state.task_id


def test_package_never_contains_raw_conversation(rich_state):
    pkg = compile_for(rich_state)
    assert pkg.contains_raw_conversation is False
    lowered = pkg.rendered_text.lower()
    for marker in ('"role"', "assistant:", "user:", "system:", "conversation history"):
        assert marker not in lowered


def test_failed_attempts_and_decisions_survive_into_the_package(rich_state):
    pkg = compile_for(rich_state)
    assert "Single denormalised table" in pkg.rendered_text
    assert "Use FastAPI" in pkg.rendered_text
    assert "Async and OpenAPI" in pkg.rendered_text


def test_previous_worker_notes_are_included(rich_state):
    from context_orchestration.context.handoff import build_handoff_record
    from tests.conftest import make_report

    rich_state.handoff_history.append(
        build_handoff_record(1, "worker-1", "worker-2", make_report(notes_for_next_worker="Keep the schema."))
    )
    pkg = compile_for(rich_state)
    assert "previous_worker_notes" in pkg.included_sections
    assert "Keep the schema." in pkg.rendered_text


def test_budget_is_respected_and_overflow_is_reported(rich_state):
    for i in range(60):
        rich_state.add_decision(Decision(decision=f"Irrelevant decision number {i} about deployment tooling", reason="x" * 80))
        rich_state.add_completed_task(f"Some unrelated completed chore number {i}")

    pkg = compile_for(rich_state, budget=200)
    assert pkg.estimated_tokens <= 200
    assert pkg.omitted_sections or pkg.dropped_items


@pytest.mark.parametrize("budget", [60, 100, 150, 250, 400, 800, 2000])
def test_budget_holds_across_the_range(rich_state, budget):
    """Budgeting must measure the rendered package, not a sum of section costs."""
    for i in range(40):
        rich_state.add_decision(Decision(decision=f"Decision {i} about deployment", reason="y" * 60))
        rich_state.add_completed_task(f"Completed chore {i}")
        rich_state.add_issue(Issue(description=f"Issue {i}", severity="low"))

    compiler = ContextCompiler(token_budget=budget)
    pkg = compiler.compile(rich_state, "Design the database schema.", "worker-2")

    assert pkg.estimated_tokens <= budget
    assert compiler.estimator.count(pkg.rendered_text) <= budget


def test_required_sections_survive_a_tiny_budget(rich_state):
    pkg = compile_for(rich_state, budget=40)
    assert "assigned_task" in pkg.included_sections
    assert "objective" in pkg.included_sections


def test_compiler_does_not_dump_everything(rich_state):
    """A state with far more items than the caps must yield a bounded package."""
    for i in range(50):
        rich_state.add_decision(Decision(decision=f"Decision {i}", reason="because"))
        rich_state.add_failed_attempt(FailedAttempt(attempt=f"Failed thing {i}"))
        rich_state.upsert_artifact(Artifact(name=f"file_{i}.md", created_by="w"))

    compiler = ContextCompiler(token_budget=100_000, max_decisions=5, max_failed=4, max_artifacts=3)
    pkg = compiler.compile(rich_state, "Design the database schema.", "worker-2")

    decisions = next(s for s in pkg.sections if s.key == "decisions")
    artifacts = next(s for s in pkg.sections if s.key == "artifacts")
    failed = next(s for s in pkg.sections if s.key == "failed_attempts")
    assert len(decisions.lines) == 5
    assert len(artifacts.lines) == 3
    assert len(failed.lines) == 4
    assert pkg.dropped_items


def test_relevant_items_outrank_irrelevant_ones(rich_state):
    rich_state.add_decision(Decision(decision="Use UUID primary keys in the database schema", reason="enumeration"))
    for i in range(20):
        rich_state.add_decision(Decision(decision=f"Pick logging vendor option {i}", reason="cost"))

    compiler = ContextCompiler(token_budget=100_000, max_decisions=3)
    pkg = compiler.compile(rich_state, "Design the PostgreSQL database schema.", "worker-2")
    decisions = next(s for s in pkg.sections if s.key == "decisions")
    assert any("UUID primary keys" in line for line in decisions.lines)


def test_founding_decisions_survive_a_flood_of_later_ones(rich_state):
    """Regression: a late reviewer must still see the framework choice.

    Without anchoring, relevance + recency drops the earliest architectural
    decisions in favour of recent detail, and the final review worker ends up
    reviewing an architecture whose foundations it cannot see.
    """
    founding = rich_state.decisions[0].decision  # "Use FastAPI"
    for i in range(30):
        rich_state.add_decision(Decision(decision=f"Later granular choice {i}", reason="detail"))

    compiler = ContextCompiler(token_budget=100_000, max_decisions=10, anchor_items=3)
    pkg = compiler.compile(rich_state, "Review the complete architecture.", "worker-5")

    decisions = next(s for s in pkg.sections if s.key == "decisions")
    assert any(founding in line for line in decisions.lines)
    assert len(decisions.lines) == 10


def test_anchors_never_exceed_the_category_limit(rich_state):
    for i in range(10):
        rich_state.add_decision(Decision(decision=f"Choice {i}", reason="r"))
    compiler = ContextCompiler(token_budget=100_000, max_decisions=2, anchor_items=5)
    pkg = compiler.compile(rich_state, "Anything.", "worker-2")
    decisions = next(s for s in pkg.sections if s.key == "decisions")
    assert len(decisions.lines) == 2


def test_earliest_failed_attempts_are_anchored_too(rich_state):
    first_failure = rich_state.failed_attempts[0].attempt
    for i in range(20):
        rich_state.add_failed_attempt(FailedAttempt(attempt=f"Later dead end {i}", reason="nope"))

    compiler = ContextCompiler(token_budget=100_000, max_failed=5, anchor_items=3)
    pkg = compiler.compile(rich_state, "Design the database schema.", "worker-2")
    failed = next(s for s in pkg.sections if s.key == "failed_attempts")
    assert any(first_failure in line for line in failed.lines)


def test_sections_are_ordered_by_priority(rich_state):
    pkg = compile_for(rich_state)
    priorities = [s.priority for s in pkg.sections]
    assert priorities == sorted(priorities)
    assert pkg.sections[0].key == "assigned_task"


def test_unverified_artifacts_are_labelled_for_the_next_worker(rich_state):
    rich_state.upsert_artifact(Artifact(name="ghost.md", created_by="worker-1", verified=False))
    pkg = compile_for(rich_state)
    assert "ghost.md" in pkg.rendered_text
    assert "unverified" in pkg.rendered_text


def test_empty_state_still_compiles(plan):
    pkg = compile_for(create_state("Objective", plan), task=plan[0], worker="worker-1")
    assert pkg.rendered_text
    assert "assigned_task" in pkg.included_sections


class TestTokenEstimation:
    def test_heuristic_is_monotonic_and_nonzero(self):
        est = HeuristicTokenEstimator()
        assert est.count("") == 0
        assert est.count("hello world") >= 2
        assert est.count("a b c d e f g h") > est.count("a b c")

    def test_estimator_is_pluggable(self, rich_state):
        class DoubleEstimator:
            def count(self, text: str) -> int:
                return len(text.split()) * 2

        compiler = ContextCompiler(token_budget=100_000, estimator=DoubleEstimator())
        pkg = compiler.compile(rich_state, "Design the schema.", "worker-2")
        assert pkg.estimated_tokens == len(pkg.rendered_text.split()) * 2


class TestRelevance:
    def test_overlap_scores_higher_than_no_overlap(self):
        focus = {"database", "schema", "postgresql"}
        assert relevance("Design the database schema", focus) > relevance("Choose a logging vendor", focus)

    def test_empty_inputs_score_zero(self):
        assert relevance("anything", set()) == 0.0
        assert relevance("", {"database"}) == 0.0
