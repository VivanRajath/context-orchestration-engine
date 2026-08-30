"""Orchestrator: the end-to-end continuity experiment.

The central claim under test: N independent workers, one continuous task, zero
raw conversation transfers, and context that survives every handoff.
"""

from __future__ import annotations

import pytest

from context_orchestration.config.demo import DEMO_OBJECTIVE, DEMO_PLAN
from context_orchestration.context.compiler import ContextCompiler
from context_orchestration.core.contracts import WorkerStatus
from context_orchestration.core.orchestrator import (
    ContextOrchestrator,
    SequentialSwitchPolicy,
    WorkerRegistry,
    build_assignments,
    resolve_mock_mode,
)
from context_orchestration.core.worker import UniversalWorker
from context_orchestration.gateway.llm_gateway import GatewayError, MockGateway


def make_orchestrator(registry, store, gateway=None, **kwargs):
    return ContextOrchestrator(
        registry=registry,
        gateway=gateway or MockGateway(),
        store=store,
        compiler=ContextCompiler(token_budget=1500),
        mock=True,
        **kwargs,
    )


class TestWorkerRegistry:
    def test_loads_from_the_shipped_config_file(self):
        from context_orchestration.core.orchestrator import DEFAULT_WORKERS_PATH, load_registry

        registry = load_registry(DEFAULT_WORKERS_PATH)
        assert len(registry) == 5
        assert registry.get("worker-1").model

    def test_rejects_duplicate_ids(self):
        with pytest.raises(ValueError, match="duplicate worker id"):
            WorkerRegistry.from_dicts([{"id": "w", "model": "a"}, {"id": "w", "model": "b"}])

    def test_rejects_an_empty_roster(self):
        with pytest.raises(ValueError, match="no enabled workers"):
            WorkerRegistry.from_dicts([])

    def test_disabled_workers_are_excluded(self):
        registry = WorkerRegistry.from_dicts(
            [{"id": "a", "model": "m"}, {"id": "b", "model": "m", "enabled": False}]
        )
        assert len(registry) == 1

    def test_no_provider_specific_worker_classes_exist(self):
        """The architecture rule: one universal worker, configured at runtime."""
        import context_orchestration.core.worker as worker_module

        classes = [n for n in dir(worker_module) if n.endswith("Worker")]
        assert classes == ["UniversalWorker"]


class TestAssignments:
    @pytest.mark.parametrize("worker_count", [1, 2, 3, 5, 20, 100])
    def test_any_number_of_workers_is_supported_without_code_changes(self, worker_count):
        registry = WorkerRegistry.from_dicts(
            [{"id": f"worker-{i}", "model": f"provider/model-{i}"} for i in range(1, worker_count + 1)]
        )
        assignments = build_assignments(DEMO_PLAN, registry)
        assert len(assignments) == max(len(DEMO_PLAN), worker_count)
        assert len({a.worker_id for a in assignments}) == worker_count

    def test_more_plan_steps_than_workers_cycles_the_roster(self, registry):
        plan = [f"step {i}" for i in range(7)]
        assignments = build_assignments(plan, registry)
        assert len(assignments) == 7
        assert [a.worker_id for a in assignments[:4]] == ["worker-1", "worker-2", "worker-3", "worker-1"]

    def test_more_workers_than_steps_gives_everyone_a_turn(self, registry):
        assignments = build_assignments(["only step"], registry)
        assert len(assignments) == 3
        assert assignments[1].task.startswith("Continue the objective")

    def test_empty_plan_is_rejected(self, registry):
        with pytest.raises(ValueError, match="plan is empty"):
            build_assignments([], registry)

    def test_switch_policy_is_replaceable(self, registry):
        class AlwaysLast(SequentialSwitchPolicy):
            def assign(self, index, registry, state):
                return registry.configs[-1]

        assignments = build_assignments(DEMO_PLAN, registry, policy=AlwaysLast())
        assert {a.worker_id for a in assignments} == {"worker-3"}


class TestRun:
    def test_full_run_completes_every_assignment(self, registry, store):
        orchestrator = make_orchestrator(registry, store)
        state, summary = orchestrator.run(DEMO_OBJECTIVE, DEMO_PLAN)

        assert summary.workers_used == len(DEMO_PLAN)
        assert summary.reports_generated == len(DEMO_PLAN)
        assert summary.packages_compiled == len(DEMO_PLAN)
        assert summary.structured_handoffs == len(DEMO_PLAN) - 1
        assert state.status == "completed"
        assert not state.pending_tasks

    def test_no_raw_conversation_is_ever_transferred(self, registry, store):
        gateway = MockGateway()
        state, summary = make_orchestrator(registry, store, gateway).run(DEMO_OBJECTIVE, DEMO_PLAN)

        assert summary.raw_conversation_transfers == 0
        assert summary.continuity_maintained is True
        assert all(not e.raw_conversation_transferred for e in state.worker_history)
        assert all(not h.raw_conversation_transferred for h in state.handoff_history)

        # Every single model call carried exactly one system + one user message.
        for call in gateway.calls:
            assert len(call["messages"]) == 2

    def test_context_sent_per_worker_stays_bounded(self, registry, store):
        """Token cost must not grow the way a full-conversation transfer would."""
        state, _ = make_orchestrator(registry, store).run(DEMO_OBJECTIVE, DEMO_PLAN)
        sizes = [e.context_tokens_in for e in state.worker_history]
        assert all(s <= 1500 for s in sizes)

    def test_later_workers_inherit_earlier_decisions(self, registry, store):
        state, _ = make_orchestrator(registry, store).run(DEMO_OBJECTIVE, DEMO_PLAN)
        packages = store.load_packages(state.task_id)

        first_decision = state.decisions[0].decision
        assert first_decision not in packages[0].rendered_text
        assert first_decision in packages[-1].rendered_text

    def test_every_worker_produces_a_handoff_report(self, registry, store):
        state, _ = make_orchestrator(registry, store).run(DEMO_OBJECTIVE, DEMO_PLAN)
        handoffs = store.load_handoffs(state.task_id)
        assert len(handoffs) == len(DEMO_PLAN)
        assert all(h.report.recommended_next_action for h in handoffs)

    def test_state_is_persisted_after_every_worker(self, registry, store):
        state, _ = make_orchestrator(registry, store).run(DEMO_OBJECTIVE, DEMO_PLAN)
        snapshots = store.conn.execute(
            "SELECT seq FROM state_snapshots WHERE task_id=? ORDER BY seq", (state.task_id,)
        ).fetchall()
        assert [r["seq"] for r in snapshots] == list(range(1, len(DEMO_PLAN) + 1))

    def test_work_accumulates_across_workers(self, registry, store):
        state, _ = make_orchestrator(registry, store).run(DEMO_OBJECTIVE, DEMO_PLAN)
        assert len(state.completed_tasks) == len(DEMO_PLAN)
        assert len(state.decisions) > 3
        assert len(state.artifacts) > 3
        assert len(state.failed_attempts) > 0
        assert len({d.recorded_by for d in state.decisions}) > 1

    def test_run_works_with_a_single_worker(self, store):
        registry = WorkerRegistry.from_dicts([{"id": "solo", "model": "provider/model"}])
        _, summary = make_orchestrator(registry, store).run(DEMO_OBJECTIVE, DEMO_PLAN)
        assert summary.workers_used == len(DEMO_PLAN)
        assert summary.raw_conversation_transfers == 0

    def test_run_works_with_twenty_workers(self, store):
        registry = WorkerRegistry.from_dicts(
            [{"id": f"w{i}", "model": f"provider/model-{i}"} for i in range(20)]
        )
        state, summary = make_orchestrator(registry, store).run(DEMO_OBJECTIVE, DEMO_PLAN)
        assert summary.workers_used == 20
        assert len({e.worker_id for e in state.worker_history}) == 20


class TestFailureHandling:
    def test_a_failing_worker_is_recorded_and_the_run_continues(self, registry, store):
        class BreaksOnSecondWorker(MockGateway):
            def complete(self, config, messages, json_schema=None):
                if config.id == "worker-2":
                    raise GatewayError("rate limited")
                return super().complete(config, messages, json_schema)

        state, summary = make_orchestrator(registry, store, BreaksOnSecondWorker()).run(
            DEMO_OBJECTIVE, DEMO_PLAN
        )

        assert summary.failures
        assert summary.continuity_maintained is False
        failed = [e for e in state.worker_history if e.status == WorkerStatus.FAILED]
        assert failed and failed[0].worker_id == "worker-2"
        assert state.status == "failed"
        # later workers still ran
        assert any(e.status == WorkerStatus.COMPLETED and e.seq > 2 for e in state.worker_history)

    def test_a_failed_worker_does_not_corrupt_canonical_state(self, registry, store):
        class BreaksFirst(MockGateway):
            def complete(self, config, messages, json_schema=None):
                if config.id == "worker-1":
                    raise GatewayError("boom")
                return super().complete(config, messages, json_schema)

        state, _ = make_orchestrator(registry, store, BreaksFirst()).run(DEMO_OBJECTIVE, DEMO_PLAN)
        assert not any(d.recorded_by == "worker-1" for d in state.decisions)
        assert store.load_state(state.task_id) is not None


class TestResume:
    def test_a_task_survives_process_restart(self, registry, store, tmp_path):
        """Simulates a crash: only some assignments completed, new store object."""
        class StopsAfterTwo(MockGateway):
            def complete(self, config, messages, json_schema=None):
                if config.id == "worker-3":
                    raise GatewayError("process died")
                return super().complete(config, messages, json_schema)

        orchestrator = make_orchestrator(registry, store, StopsAfterTwo())
        state, summary = orchestrator.run(DEMO_OBJECTIVE, DEMO_PLAN)
        assert summary.failures

        # A brand-new orchestrator with a healthy gateway picks the task back up.
        from context_orchestration.storage.sqlite_store import SQLiteStore

        reopened = SQLiteStore(store.path)
        resumed_orchestrator = make_orchestrator(registry, reopened, MockGateway())
        resumed, resume_summary = resumed_orchestrator.resume(state.task_id)

        assert resume_summary.workers_used > 0
        assert resume_summary.raw_conversation_transfers == 0
        assert len(resumed.completed_tasks) >= len(state.completed_tasks)
        reopened.close()

    def test_resuming_a_finished_task_is_a_no_op(self, registry, store):
        orchestrator = make_orchestrator(registry, store)
        state, _ = orchestrator.run(DEMO_OBJECTIVE, DEMO_PLAN)
        _, summary = orchestrator.resume(state.task_id)
        assert summary.already_complete is True
        assert summary.workers_used == 0  # nothing ran in this invocation
        assert summary.previously_completed == len(DEMO_PLAN)
        assert summary.failures == []

    def test_resuming_an_unknown_task_raises(self, registry, store):
        with pytest.raises(KeyError):
            make_orchestrator(registry, store).resume("task-does-not-exist")


class TestMockModeResolution:
    def test_auto_uses_mock_when_credentials_are_missing(self, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        registry = WorkerRegistry.from_dicts([{"id": "w", "model": "m", "api_key_env": "MISSING_KEY"}])
        use_mock, missing = resolve_mock_mode(registry, "auto")
        assert use_mock is True
        assert missing == ["w"]

    def test_auto_uses_live_when_credentials_are_present(self, monkeypatch):
        monkeypatch.setenv("PRESENT_KEY", "sk-test")
        registry = WorkerRegistry.from_dicts([{"id": "w", "model": "m", "api_key_env": "PRESENT_KEY"}])
        use_mock, missing = resolve_mock_mode(registry, "auto")
        assert use_mock is False
        assert missing == []

    def test_explicit_flags_override_detection(self, monkeypatch):
        monkeypatch.setenv("PRESENT_KEY", "sk-test")
        registry = WorkerRegistry.from_dicts([{"id": "w", "model": "m", "api_key_env": "PRESENT_KEY"}])
        assert resolve_mock_mode(registry, "mock")[0] is True
        assert resolve_mock_mode(registry, "real")[0] is False


class TestProviderIndependence:
    def test_orchestration_layer_names_no_provider(self):
        """Critical rule 1: no provider is hardcoded above the gateway."""
        import inspect
        import re

        import context_orchestration.context.compiler as compiler_module
        import context_orchestration.context.state as state_module
        import context_orchestration.core.orchestrator as orchestrator_module
        import context_orchestration.core.reconciler as reconciler_module
        import context_orchestration.core.worker as worker_module

        banned = ("openai", "anthropic", "gemini", "claude", "gpt", "mistral", "cohere", "llama", "bedrock")
        pattern = re.compile(r"\b(" + "|".join(banned) + r")\b")
        for module in (orchestrator_module, worker_module, reconciler_module, compiler_module, state_module):
            found = pattern.findall(inspect.getsource(module).lower())
            assert not found, f"{module.__name__} mentions {set(found)}"

    def test_workers_are_interchangeable_regardless_of_model_string(self, store):
        registry = WorkerRegistry.from_dicts(
            [
                {"id": "a", "model": "some-provider/model-1"},
                {"id": "b", "model": "another-provider/model-2"},
                {"id": "c", "model": "third-provider/model-3"},
            ]
        )
        _, summary = make_orchestrator(registry, store).run(DEMO_OBJECTIVE, DEMO_PLAN)
        assert summary.continuity_maintained is True

    def test_worker_factory_is_injectable(self, registry, store):
        seen = []

        def factory(config, gateway):
            seen.append(config.id)
            return UniversalWorker(config, gateway)

        make_orchestrator(registry, store, worker_factory=factory).run(DEMO_OBJECTIVE, DEMO_PLAN)
        assert seen
