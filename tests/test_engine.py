"""The public library surface: `Engine` and the package's exported API.

These are the things an external user actually touches, so they get their own
tests - a refactor that breaks the facade breaks every downstream consumer.
"""

from __future__ import annotations

import pytest

import context_orchestration as co
from context_orchestration import Engine, ExecutionState, HandoffPackage, MockGateway

OBJECTIVE = "Design a notification service."
PLAN = ["Define requirements.", "Design the delivery pipeline.", "Review the design."]


@pytest.fixture
def engine(tmp_path):
    e = Engine(
        workers=[
            {"id": "worker-1", "model": "provider-a/model"},
            {"id": "worker-2", "model": "provider-b/model"},
            {"id": "worker-3", "model": "provider-c/model"},
        ],
        db=tmp_path / "engine.db",
        gateway=MockGateway(),
        mock=True,
    )
    yield e
    e.close()


class TestPackageSurface:
    def test_version_is_exposed(self):
        assert co.__version__

    def test_everything_in_all_is_importable(self):
        missing = [name for name in co.__all__ if not hasattr(co, name)]
        assert missing == []

    def test_key_types_are_exported(self):
        for name in ("Engine", "ExecutionState", "ContextCompiler", "StateReconciler", "WorkerConfig"):
            assert name in co.__all__

    def test_package_does_not_claim_generic_top_level_names(self):
        """Regression: `core`, `context`, `gateway` etc. must not be top-level."""
        import importlib.metadata as md

        files = md.files("context-orchestration-engine") or []
        tops = {str(f).split("/")[0].split("\\")[0] for f in files}
        polluting = tops & {"core", "context", "gateway", "storage", "ui", "config", "tests"}
        assert polluting == set(), f"package claims top-level names: {polluting}"


class TestEngineConstruction:
    def test_accepts_dicts_or_worker_configs(self, tmp_path):
        from context_orchestration import WorkerConfig

        with Engine(
            workers=[{"id": "a", "model": "p/m"}, WorkerConfig(id="b", model="p/m")],
            db=tmp_path / "x.db",
            mock=True,
        ) as e:
            assert len(e.registry) == 2

    def test_from_config_reads_a_workers_file(self, tmp_path):
        path = tmp_path / "workers.json"
        path.write_text('{"workers":[{"id":"w1","model":"p/m"},{"id":"w2","model":"p/m"}]}', encoding="utf-8")
        with Engine.from_config(path, db=tmp_path / "y.db", mock=True) as e:
            assert [c.id for c in e.registry] == ["w1", "w2"]

    def test_demo_roster_loads_from_packaged_data(self, tmp_path):
        with Engine.demo(db=tmp_path / "z.db", mock=True) as e:
            assert len(e.registry) >= 2

    def test_repr_reports_mode(self, engine):
        assert "mock" in repr(engine)

    def test_context_manager_closes_the_store(self, tmp_path):
        e = Engine(workers=[{"id": "a", "model": "p/m"}], db=tmp_path / "c.db", mock=True)
        with e:
            pass
        with pytest.raises(Exception):
            e.store.conn.execute("SELECT 1")


class TestEngineRun:
    def test_run_returns_state_and_summary(self, engine):
        result = engine.run(OBJECTIVE, PLAN)
        assert isinstance(result.state, ExecutionState)
        assert result.task_id == result.state.task_id
        assert result.summary.workers_used == 3
        assert result.summary.raw_conversation_transfers == 0
        assert result.continuity_maintained is True

    def test_inspection_methods_return_persisted_data(self, engine):
        result = engine.run(OBJECTIVE, PLAN)
        tid = result.task_id

        assert engine.state(tid).objective == OBJECTIVE
        assert len(engine.history(tid)) == 3
        assert len(engine.handoffs(tid)) == 3
        assert len(engine.packages(tid)) == 3
        assert len(engine.assignments(tid)) == 3
        assert engine.tasks()[0]["task_id"] == tid
        assert any(e["kind"] == "task_created" for e in engine.events(tid))

    def test_task_id_prefix_resolution(self, engine):
        tid = engine.run(OBJECTIVE, PLAN).task_id
        assert engine.resolve(tid[:9]) == tid

    def test_unknown_task_returns_none(self, engine):
        assert engine.state("task-nope") is None
        assert engine.resolve("task-nope") is None


class TestSingleStepping:
    def test_create_does_not_run_anything(self, engine):
        state = engine.create(OBJECTIVE, PLAN)
        assert state.worker_history == []
        assert engine.state(state.task_id).status == "created"

    def test_step_advances_exactly_one_worker(self, engine):
        state = engine.create(OBJECTIVE, PLAN)
        engine.step(state.task_id)

        after = engine.state(state.task_id)
        assert len(after.worker_history) == 1
        assert after.status == "paused"

    def test_stepping_accumulates_state_across_turns(self, engine):
        state = engine.create(OBJECTIVE, PLAN)
        engine.step(state.task_id)
        first = len(engine.state(state.task_id).decisions)
        engine.step(state.task_id)
        second = engine.state(state.task_id).decisions

        assert len(engine.state(state.task_id).worker_history) == 2
        assert len(second) >= first
        assert len({d.recorded_by for d in second}) == 2

    def test_steps_then_resume_finishes_the_task(self, engine):
        state = engine.create(OBJECTIVE, PLAN)
        engine.step(state.task_id)
        result = engine.resume(state.task_id)

        assert result.state.status == "completed"
        assert len(result.state.worker_history) == 3
        assert result.summary.raw_conversation_transfers == 0

    def test_multi_step_runs_several_turns(self, engine):
        state = engine.create(OBJECTIVE, PLAN)
        engine.step(state.task_id, steps=2)
        assert len(engine.state(state.task_id).worker_history) == 2


class TestStandaloneCompilation:
    def test_compile_context_needs_no_model_call(self, engine):
        """The compiler is usable on its own, against any state."""
        from context_orchestration import Decision, create_state

        state = create_state("Build an API.", ["Design the schema."])
        state.add_decision(Decision(decision="Use PostgreSQL", reason="Relational data"))

        package = engine.compile_context(state, "Design the schema.", "worker-1")

        assert isinstance(package, HandoffPackage)
        assert "Use PostgreSQL" in package.rendered_text
        assert package.contains_raw_conversation is False
        assert package.estimated_tokens > 0
