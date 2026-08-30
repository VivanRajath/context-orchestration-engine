"""SQLite persistence: durability and resumability."""

from __future__ import annotations

from context_orchestration.context.compiler import ContextCompiler
from context_orchestration.context.handoff import build_handoff_record
from context_orchestration.core.contracts import Assignment, WorkerConfig, WorkerExecution, WorkerStatus
from context_orchestration.storage.sqlite_store import SQLiteStore
from tests.conftest import make_report, make_result


def seed(store, state, plan):
    workers = [WorkerConfig(id="worker-1", model="provider/model")]
    assignments = [Assignment(seq=1, worker_id="worker-1", task=plan[0])]
    store.create_task(state, plan, workers, assignments)
    return assignments


def test_task_and_state_round_trip(store, rich_state, plan):
    seed(store, rich_state, plan)
    loaded = store.load_state(rich_state.task_id)

    assert loaded is not None
    assert loaded.objective == rich_state.objective
    assert [d.decision for d in loaded.decisions] == [d.decision for d in rich_state.decisions]
    assert loaded.failed_attempts[0].attempt == rich_state.failed_attempts[0].attempt


def test_task_survives_reopening_the_database(tmp_path, rich_state, plan):
    path = tmp_path / "persist.db"
    with SQLiteStore(path) as store:
        seed(store, rich_state, plan)
        rich_state.add_completed_task("Something new")
        store.save_state(rich_state, seq=1)

    with SQLiteStore(path) as reopened:
        loaded = reopened.load_state(rich_state.task_id)
        assert loaded.has_completed("Something new")
        meta = reopened.load_task_meta(rich_state.task_id)
        assert meta["plan"] == plan
        assert meta["assignments"][0].worker_id == "worker-1"
        assert isinstance(meta["workers"][0], WorkerConfig)


def test_worker_executions_and_results_persist(store, rich_state, plan):
    seed(store, rich_state, plan)
    execution = WorkerExecution(
        seq=1,
        worker_id="worker-1",
        model="provider/model",
        assigned_task=plan[0],
        status=WorkerStatus.COMPLETED,
        summary="Did the thing",
    )
    result = make_result()
    store.save_worker_execution(rich_state.task_id, execution, result)

    loaded = store.load_worker_executions(rich_state.task_id)
    assert len(loaded) == 1
    assert loaded[0].status == WorkerStatus.COMPLETED
    assert store.load_worker_result(rich_state.task_id, 1)["summary"] == result.summary


def test_handoffs_persist_with_their_reports(store, rich_state, plan):
    seed(store, rich_state, plan)
    record = build_handoff_record(1, "worker-1", "worker-2", make_report())
    store.save_handoff(rich_state.task_id, record)

    loaded = store.load_handoffs(rich_state.task_id)
    assert len(loaded) == 1
    assert loaded[0].report.notes_for_next_worker == "Do not revisit the framework choice."
    assert loaded[0].raw_conversation_transferred is False


def test_context_packages_persist_with_rendered_text(store, rich_state, plan):
    seed(store, rich_state, plan)
    package = ContextCompiler().compile(rich_state, plan[0], "worker-1")
    store.save_package(rich_state.task_id, 1, package)

    loaded = store.load_packages(rich_state.task_id)
    assert len(loaded) == 1
    assert loaded[0].rendered_text == package.rendered_text
    assert loaded[0].contains_raw_conversation is False


def test_saving_the_same_sequence_twice_replaces_not_duplicates(store, rich_state, plan):
    seed(store, rich_state, plan)
    for summary in ("first", "second"):
        store.save_worker_execution(
            rich_state.task_id,
            WorkerExecution(seq=1, worker_id="w", model="m", assigned_task="t", summary=summary),
        )
    loaded = store.load_worker_executions(rich_state.task_id)
    assert len(loaded) == 1
    assert loaded[0].summary == "second"


def test_events_form_an_ordered_audit_trail(store, rich_state, plan):
    seed(store, rich_state, plan)
    store.log_event(rich_state.task_id, "context_handoff", {"from": "worker-1", "to": "worker-2"})
    events = store.load_events(rich_state.task_id)

    assert [e["kind"] for e in events] == ["task_created", "context_handoff"]
    assert events[1]["payload"]["to"] == "worker-2"


def test_task_id_prefix_resolution(store, rich_state, plan):
    seed(store, rich_state, plan)
    prefix = rich_state.task_id[:9]
    assert store.resolve_task_id(prefix) == rich_state.task_id
    assert store.resolve_task_id(rich_state.task_id) == rich_state.task_id
    assert store.resolve_task_id("nope") is None


def test_listing_tasks(store, rich_state, plan):
    seed(store, rich_state, plan)
    rows = store.list_tasks()
    assert rows[0]["task_id"] == rich_state.task_id
    assert rows[0]["objective"] == rich_state.objective


def test_unknown_task_returns_none(store):
    assert store.load_state("task-missing") is None
    assert store.load_task_meta("task-missing") is None


def test_api_keys_are_never_written_to_the_database(tmp_path, rich_state, plan):
    """A key may reach a WorkerConfig from workers.json or from the playground.

    Neither belongs in a file that outlives the run, so the roster is stored
    without it - while everything else about the worker survives.
    """
    path = tmp_path / "keys.db"
    secret = "sk-must-never-be-persisted"
    workers = [
        WorkerConfig(id="worker-1", model="provider/model", api_key=secret),
        WorkerConfig(id="worker-2", model="other/model", api_key_env="WORKER_2_API_KEY"),
    ]
    assignments = [Assignment(seq=1, worker_id="worker-1", task=plan[0])]

    with SQLiteStore(path) as store:
        store.create_task(rich_state, plan, workers, assignments)

    assert secret not in path.read_bytes().decode("utf-8", "ignore")

    with SQLiteStore(path) as reopened:
        meta = reopened.load_task_meta(rich_state.task_id)
        assert [w.id for w in meta["workers"]] == ["worker-1", "worker-2"]
        assert meta["workers"][0].api_key is None
        assert meta["workers"][0].model == "provider/model"
        assert meta["workers"][1].api_key_env == "WORKER_2_API_KEY"
