"""Handoff construction and rendering.

The handoff report is the worker's own message to its successor. The compiler
decides how much of it survives; this module decides how it is shaped, and
gives the engine a place to record who handed off to whom.
"""

from __future__ import annotations

from context_orchestration.context.state import ExecutionState
from context_orchestration.core.contracts import HandoffPackage, HandoffRecord, HandoffReport


def build_handoff_record(
    seq: int,
    from_worker: str,
    to_worker: str | None,
    report: HandoffReport,
    package: HandoffPackage | None = None,
) -> HandoffRecord:
    return HandoffRecord(
        seq=seq,
        from_worker=from_worker,
        to_worker=to_worker,
        report=report,
        package_id=package.package_id if package else None,
        raw_conversation_transferred=False,
    )


def render_report(report: HandoffReport) -> str:
    """Human-readable rendering, used by the CLI and by `handoffs` inspection."""
    blocks: list[str] = []

    def block(title: str, lines: list[str]) -> None:
        lines = [ln for ln in lines if ln and str(ln).strip()]
        if not lines:
            lines = ["None."]
        blocks.append(title + "\n\n" + "\n".join(f"- {ln}" for ln in lines))

    block("WORK COMPLETED", report.work_completed)
    blocks.append("CURRENT STATE\n\n" + (report.current_state or "Not stated."))
    block(
        "IMPORTANT DECISIONS",
        [f"{d.decision} (reason: {d.reason})" if d.reason else d.decision for d in report.important_decisions],
    )
    block("ARTIFACTS CREATED OR MODIFIED", report.artifacts_created_or_modified)
    block("PROBLEMS ENCOUNTERED", report.problems_encountered)
    block("FAILED ATTEMPTS", report.failed_attempts)
    block("ASSUMPTIONS", report.assumptions)
    blocks.append("LAST ACTION\n\n" + (report.last_action or "Not stated."))
    blocks.append("RECOMMENDED NEXT ACTION\n\n" + (report.recommended_next_action or "Not stated."))
    blocks.append("NOTES FOR NEXT WORKER\n\n" + (report.notes_for_next_worker or "None."))
    return "\n\n".join(blocks)


def handoff_audit(state: ExecutionState, package: HandoffPackage, from_worker: str, to_worker: str) -> dict:
    """The evidence line of the experiment: what actually crossed the boundary."""
    return {
        "previous_worker": from_worker,
        "next_worker": to_worker,
        "raw_conversation_transferred": False,
        "canonical_state_transferred": True,
        "handoff_report_included": "previous_worker_notes" in package.included_sections,
        "included_sections": list(package.included_sections),
        "omitted_sections": list(package.omitted_sections),
        "package_tokens": package.estimated_tokens,
        "token_budget": package.token_budget,
        "state_items_available": {
            "completed_tasks": len(state.completed_tasks),
            "decisions": len(state.decisions),
            "artifacts": len(state.artifacts),
            "open_issues": len(state.open_issues),
            "failed_attempts": len(state.failed_attempts),
            "assumptions": len(state.assumptions),
        },
    }
