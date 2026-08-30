"""Handoff reports: contract validation, coercion, and the worker's two calls."""

from __future__ import annotations

import json

import pytest

from context_orchestration.context.compiler import ContextCompiler
from context_orchestration.context.handoff import build_handoff_record, handoff_audit, render_report
from context_orchestration.core.contracts import HandoffReport, WorkerResult
from context_orchestration.core.worker import UniversalWorker
from context_orchestration.gateway.llm_gateway import GatewayError, GatewayResponse, MockGateway, extract_json
from tests.conftest import make_report, make_result


class TestContractCoercion:
    def test_artifacts_accept_bare_strings(self):
        r = WorkerResult.model_validate({"artifacts": ["schema.sql", {"name": "auth.py", "kind": "code"}]})
        assert [a.name for a in r.artifacts] == ["schema.sql", "auth.py"]
        assert r.artifacts[1].kind == "code"

    def test_issues_accept_strings_and_alternate_keys(self):
        r = WorkerResult.model_validate({"issues": ["something broke", {"problem": "another", "severity": "high"}]})
        assert r.issues[0].description == "something broke"
        assert r.issues[1].severity == "high"

    def test_invalid_severity_falls_back_to_medium(self):
        r = WorkerResult.model_validate({"issues": [{"description": "x", "severity": "catastrophic"}]})
        assert r.issues[0].severity == "medium"

    def test_report_lists_flatten_dicts_to_text(self):
        rep = HandoffReport.model_validate(
            {"problems_encountered": [{"description": "db down"}], "failed_attempts": ["tried X"]}
        )
        assert rep.problems_encountered == ["db down"]
        assert rep.failed_attempts == ["tried X"]

    def test_missing_fields_default_to_empty(self):
        assert WorkerResult.model_validate({}).completed_tasks == []
        assert HandoffReport.model_validate({}).work_completed == []

    def test_malformed_entries_are_skipped_not_fatal(self):
        r = WorkerResult.model_validate({"decisions": ["ok", 42, None]})
        assert [d.decision for d in r.decisions] == ["ok", "42", "None"]


class TestJSONExtraction:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_surrounding_prose(self):
        assert extract_json('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}

    def test_nested_braces_and_strings(self):
        text = 'prose {"a": {"b": "}"}, "c": 2} trailing'
        assert extract_json(text) == {"a": {"b": "}"}, "c": 2}

    def test_empty_and_unparseable_raise(self):
        with pytest.raises(ValueError):
            extract_json("")
        with pytest.raises(ValueError):
            extract_json("no json at all")


class TestRendering:
    def test_render_report_covers_every_section(self):
        text = render_report(make_report())
        for title in (
            "WORK COMPLETED",
            "CURRENT STATE",
            "IMPORTANT DECISIONS",
            "ARTIFACTS CREATED OR MODIFIED",
            "PROBLEMS ENCOUNTERED",
            "FAILED ATTEMPTS",
            "ASSUMPTIONS",
            "LAST ACTION",
            "RECOMMENDED NEXT ACTION",
            "NOTES FOR NEXT WORKER",
        ):
            assert title in text

    def test_empty_sections_render_as_none(self):
        text = render_report(HandoffReport())
        assert "None." in text

    def test_handoff_record_marks_no_raw_conversation(self):
        record = build_handoff_record(1, "worker-1", "worker-2", make_report())
        assert record.raw_conversation_transferred is False
        assert record.from_worker == "worker-1"
        assert record.to_worker == "worker-2"

    def test_audit_reports_what_crossed_the_boundary(self, rich_state):
        pkg = ContextCompiler().compile(rich_state, "Design the schema.", "worker-2")
        audit = handoff_audit(rich_state, pkg, "worker-1", "worker-2")
        assert audit["raw_conversation_transferred"] is False
        assert audit["canonical_state_transferred"] is True
        assert audit["state_items_available"]["decisions"] == len(rich_state.decisions)


class TestWorkerExecution:
    def test_worker_produces_both_a_result_and_a_report(self, worker_config, rich_state):
        pkg = ContextCompiler().compile(rich_state, "Design the database schema.", "worker-1")
        run = UniversalWorker(worker_config, MockGateway()).execute(pkg)

        assert isinstance(run.result, WorkerResult)
        assert isinstance(run.report, HandoffReport)
        assert run.report.work_completed
        assert run.raw_conversation_transferred is False

    def test_worker_sends_only_the_compiled_package_no_history(self, worker_config, rich_state):
        gateway = MockGateway()
        pkg = ContextCompiler().compile(rich_state, "Design the database schema.", "worker-1")
        UniversalWorker(worker_config, gateway).execute(pkg)

        work_call = gateway.calls[0]
        assert len(work_call["messages"]) == 2
        assert [m["role"] for m in work_call["messages"]] == ["system", "user"]
        assert work_call["messages"][1]["content"] == pkg.rendered_text

    def test_worker_holds_no_state_between_turns(self, worker_config, rich_state):
        """Two executions of the same worker object must not accumulate context."""
        gateway = MockGateway()
        worker = UniversalWorker(worker_config, gateway)
        pkg = ContextCompiler().compile(rich_state, "Design the database schema.", "worker-1")
        worker.execute(pkg)
        worker.execute(pkg)

        first, third = gateway.calls[0], gateway.calls[2]
        assert first["messages"] == third["messages"]
        assert not hasattr(worker, "history")

    def test_worker_retries_on_unparseable_output_then_succeeds(self, worker_config, rich_state):
        class FlakyGateway(MockGateway):
            def __init__(self):
                super().__init__()
                self.attempts = 0

            def complete(self, config, messages, json_schema=None):
                self.attempts += 1
                if self.attempts == 1:
                    return GatewayResponse(text="I am afraid I cannot do that.", model=config.model)
                return super().complete(config, messages, json_schema)

        gateway = FlakyGateway()
        pkg = ContextCompiler().compile(rich_state, "Design the database schema.", "worker-1")
        run = UniversalWorker(worker_config, gateway).execute(pkg)
        assert gateway.attempts >= 2
        assert run.result.summary

    def test_worker_gives_up_after_max_retries(self, worker_config, rich_state):
        class DeadGateway:
            def complete(self, config, messages, json_schema=None):
                return GatewayResponse(text="never json", model=config.model)

        worker_config.max_retries = 1
        pkg = ContextCompiler().compile(rich_state, "Design the schema.", "worker-1")
        with pytest.raises(GatewayError, match="no valid WorkerResult"):
            UniversalWorker(worker_config, DeadGateway()).execute(pkg)

    def test_thin_report_is_backfilled_from_the_result(self, worker_config):
        report = UniversalWorker._backfill_report(HandoffReport(), make_result())
        assert report.work_completed == ["Define requirements and architecture."]
        assert report.last_action == "Wrote architecture.md"
        assert report.recommended_next_action == "Design the database schema."
        assert report.artifacts_created_or_modified == ["architecture.md"]

    def test_report_call_shows_the_worker_its_own_result_only(self, worker_config, rich_state):
        gateway = MockGateway()
        pkg = ContextCompiler().compile(rich_state, "Design the database schema.", "worker-1")
        UniversalWorker(worker_config, gateway).execute(pkg)

        report_call = gateway.calls[1]
        content = report_call["messages"][1]["content"]

        # It sees its assigned task and its own structured result - nothing else.
        assert "YOUR WORKER RESULT" in content
        own_result = json.loads(content[content.index("{") : content.rindex("}") + 1])
        assert set(own_result) >= {"summary", "completed_tasks", "decisions"}
        assert pkg.rendered_text not in content
