"""The universal worker.

There is exactly one worker class in this system. A worker is a `WorkerConfig`
plus a gateway - nothing else. It has no memory between turns, no persistent
context, and no idea whether it is the first worker or the hundredth.

Each turn makes two model calls:

1. **Work call** - produce a ``WorkerResult`` for the assigned task.
2. **Handoff call** - produce a ``HandoffReport`` addressed to the next worker.

Both calls are built from scratch out of the compiled handoff package. The
worker never accumulates a message list across turns, which is the mechanical
reason no raw conversation can leak into the next worker.
"""

from __future__ import annotations

import json
import time

from context_orchestration.core.contracts import (
    HandoffPackage,
    HandoffReport,
    WorkerConfig,
    WorkerResult,
    WorkerRun,
)
from context_orchestration.gateway.llm_gateway import GatewayError, LLMGateway, extract_json

WORK_SYSTEM_PROMPT = """You are an interchangeable worker in a multi-worker orchestration engine.

You did not participate in any earlier conversation. Everything you know about \
this task is in the context package below - it was compiled from the engine's \
canonical execution state, not from a chat log.

Rules:
- Do the ASSIGNED TASK only. Do not redo work listed as already completed.
- Do not repeat anything listed under FAILED ATTEMPTS.
- Honour the decisions already recorded unless you have a concrete reason to \
overturn one, in which case record that reversal as a new decision with its reason.
- Record every consequential choice you make in "decisions", each with its reason. \
The next worker cannot see your reasoning - an unrecorded decision is a lost decision.
- Report only what you actually did. Do not claim completion of work outside your task.

Respond with a single JSON object and nothing else. Schema:
{
  "summary": "string - what you accomplished",
  "completed_tasks": ["string"],
  "decisions": [{"decision": "string", "reason": "string"}],
  "artifacts": [{"name": "string", "kind": "string", "description": "string"}],
  "issues": [{"description": "string", "severity": "low|medium|high|critical", "resolved": false}],
  "failed_attempts": [{"attempt": "string", "reason": "string"}],
  "assumptions": [{"assumption": "string", "reason": "string"}],
  "current_progress": "string - where the overall task now stands",
  "last_action": "string - the final concrete action you took",
  "next_action": "string - the single next action that should happen"
}"""

HANDOFF_SYSTEM_PROMPT = """You have finished your turn. Write a HANDOFF REPORT for the next worker.

The next worker is a different model with no memory of you and no access to \
your reasoning. This report plus the engine's execution state is all it will get.

Be specific and be honest about what is unfinished, uncertain, or broken. \
State anything the next worker would otherwise have to rediscover.

Respond with a single JSON object and nothing else. Schema:
{
  "work_completed": ["string"],
  "current_state": "string",
  "important_decisions": [{"decision": "string", "reason": "string"}],
  "artifacts_created_or_modified": ["string"],
  "problems_encountered": ["string"],
  "failed_attempts": ["string"],
  "assumptions": ["string"],
  "last_action": "string",
  "recommended_next_action": "string",
  "notes_for_next_worker": "string - what the next worker absolutely needs to know"
}"""


class UniversalWorker:
    """Provider-agnostic worker. Configuration is the only thing that varies."""

    def __init__(self, config: WorkerConfig, gateway: LLMGateway) -> None:
        self.config = config
        self.gateway = gateway

    # -- prompts --------------------------------------------------------

    @staticmethod
    def _work_messages(package: HandoffPackage) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": WORK_SYSTEM_PROMPT},
            {"role": "user", "content": package.rendered_text},
        ]

    @staticmethod
    def _handoff_messages(package: HandoffPackage, result: WorkerResult) -> list[dict[str, str]]:
        # The worker sees its own just-produced result - not any prior worker's
        # conversation. Nothing here crosses a worker boundary as raw messages.
        own_work = json.dumps(result.model_dump(mode="json"), indent=2)
        user = (
            f"YOUR ASSIGNED TASK\n\n{package.assigned_task}\n\n"
            f"YOUR WORKER RESULT\n\n{own_work}\n\n"
            "Now produce the HANDOFF REPORT JSON."
        )
        return [
            {"role": "system", "content": HANDOFF_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    # -- execution ------------------------------------------------------

    def execute(self, package: HandoffPackage) -> WorkerRun:
        started = time.monotonic()
        messages_sent = 0

        work_messages = self._work_messages(package)
        messages_sent += len(work_messages)
        result = self._call_validated(work_messages, WorkerResult)

        handoff_messages = self._handoff_messages(package, result)
        messages_sent += len(handoff_messages)
        report = self._call_validated(handoff_messages, HandoffReport)

        report = self._backfill_report(report, result)

        return WorkerRun(
            worker_id=self.config.id,
            model=self.config.model,
            assigned_task=package.assigned_task,
            result=result,
            report=report,
            duration_ms=int((time.monotonic() - started) * 1000),
            context_tokens_in=package.estimated_tokens,
            messages_sent=messages_sent,
            raw_conversation_transferred=package.contains_raw_conversation,
        )

    def _call_validated(self, messages: list[dict[str, str]], model_cls):
        """Call the gateway, parse JSON, validate. Retry with the error text."""
        schema = model_cls.model_json_schema()
        attempt_messages = list(messages)
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.gateway.complete(self.config, attempt_messages, json_schema=schema)
                payload = extract_json(response.text)
                return model_cls.model_validate(payload)
            except GatewayError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.config.max_retries:
                    break
                attempt_messages = list(messages) + [
                    {
                        "role": "user",
                        "content": (
                            f"Your previous response could not be used: {exc}. "
                            "Return ONLY a single valid JSON object matching the schema. "
                            "No prose, no markdown fences."
                        ),
                    }
                ]

        raise GatewayError(
            f"worker {self.config.id} ({self.config.model}) produced no valid "
            f"{model_cls.__name__} after {self.config.max_retries + 1} attempts: {last_error}"
        )

    @staticmethod
    def _backfill_report(report: HandoffReport, result: WorkerResult) -> HandoffReport:
        """A thin report still has to carry the essentials to the next worker."""
        if not report.work_completed:
            report.work_completed = list(result.completed_tasks)
        if not report.current_state:
            report.current_state = result.current_progress
        if not report.last_action:
            report.last_action = result.last_action
        if not report.recommended_next_action:
            report.recommended_next_action = result.next_action
        if not report.artifacts_created_or_modified:
            report.artifacts_created_or_modified = [a.name for a in result.artifacts if a.name]
        return report
