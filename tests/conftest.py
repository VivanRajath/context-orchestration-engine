from __future__ import annotations

import pytest

from context_orchestration.context.state import ExecutionState, create_state
from context_orchestration.core.contracts import (
    Artifact,
    Assumption,
    Decision,
    FailedAttempt,
    HandoffReport,
    Issue,
    WorkerConfig,
    WorkerResult,
)
from context_orchestration.core.orchestrator import WorkerRegistry
from context_orchestration.storage.sqlite_store import SQLiteStore

OBJECTIVE = "Build a Task Management REST API."
PLAN = [
    "Define requirements and architecture.",
    "Design the database schema.",
    "Design authentication.",
]


@pytest.fixture
def plan() -> list[str]:
    return list(PLAN)


@pytest.fixture
def state() -> ExecutionState:
    return create_state(OBJECTIVE, list(PLAN))


@pytest.fixture
def rich_state() -> ExecutionState:
    s = create_state(OBJECTIVE, list(PLAN))
    s.add_completed_task("Define requirements and architecture.")
    s.add_decision(Decision(decision="Use FastAPI", reason="Async and OpenAPI", recorded_by="worker-1"))
    s.add_decision(Decision(decision="Use PostgreSQL", reason="Relational data", recorded_by="worker-1"))
    s.upsert_artifact(Artifact(name="architecture.md", description="System architecture", created_by="worker-1"))
    s.add_issue(Issue(description="Task ordering unspecified", severity="medium", raised_by="worker-1"))
    s.add_failed_attempt(FailedAttempt(attempt="Single denormalised table", reason="Broke permissions", recorded_by="worker-1"))
    s.add_assumption(Assumption(assumption="Single tenant", recorded_by="worker-1"))
    s.current_progress = "Architecture complete."
    s.last_action = "Wrote architecture.md"
    s.next_action = "Design the database schema."
    return s


@pytest.fixture
def store(tmp_path) -> SQLiteStore:
    s = SQLiteStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def registry() -> WorkerRegistry:
    return WorkerRegistry.from_dicts(
        [
            {"id": "worker-1", "model": "provider-a/model-x", "api_key": "k1"},
            {"id": "worker-2", "model": "provider-b/model-y", "api_key": "k2"},
            {"id": "worker-3", "model": "provider-c/model-z", "api_key": "k3"},
        ]
    )


@pytest.fixture
def worker_config() -> WorkerConfig:
    return WorkerConfig(id="worker-1", model="provider-a/model-x", api_key="k")


def make_result(**overrides) -> WorkerResult:
    data = {
        "summary": "Did the work.",
        "completed_tasks": ["Define requirements and architecture."],
        "decisions": [{"decision": "Use FastAPI", "reason": "Async support"}],
        "artifacts": [{"name": "architecture.md", "description": "System architecture"}],
        "issues": [],
        "failed_attempts": [{"attempt": "Denormalised table", "reason": "Broke permissions"}],
        "assumptions": [{"assumption": "Single tenant"}],
        "current_progress": "Architecture complete.",
        "last_action": "Wrote architecture.md",
        "next_action": "Design the database schema.",
    }
    data.update(overrides)
    return WorkerResult.model_validate(data)


def make_report(**overrides) -> HandoffReport:
    data = {
        "work_completed": ["Requirements and architecture"],
        "current_state": "Architecture complete.",
        "important_decisions": [{"decision": "Use FastAPI", "reason": "Async support"}],
        "artifacts_created_or_modified": ["architecture.md"],
        "problems_encountered": [],
        "failed_attempts": [],
        "assumptions": [],
        "last_action": "Wrote architecture.md",
        "recommended_next_action": "Design the database schema.",
        "notes_for_next_worker": "Do not revisit the framework choice.",
    }
    data.update(overrides)
    return HandoffReport.model_validate(data)
