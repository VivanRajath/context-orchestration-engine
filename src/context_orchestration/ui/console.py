"""Rich terminal rendering.

Kept entirely separate from the orchestration loop: the orchestrator emits
events, this module draws them. Swap it for JSON output or a web UI without
touching the engine.
"""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from context_orchestration.context.handoff import render_report
from context_orchestration.context.state import ExecutionState
from context_orchestration.core.contracts import (
    Assignment,
    HandoffPackage,
    HandoffRecord,
    WorkerConfig,
    WorkerExecution,
    WorkerRun,
)
from context_orchestration.core.orchestrator import OrchestratorEvents, RunSummary, WorkerRegistry
from context_orchestration.core.reconciler import ReconciliationReport

console = Console()

ACCENT = "bold cyan"
OK = "bold green"
WARN = "yellow"
BAD = "bold red"
DIM = "dim"


def rule(title: str = "", style: str = ACCENT) -> None:
    console.print(Rule(title, style=style))


def _one_line(text: str, width: int = 150) -> str:
    """Collapse a multi-line provider error into one readable line."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 3].rstrip() + "..."


def _bullets(items, empty: str = "None.", limit: int | None = None) -> Text:
    items = [str(i) for i in items if str(i).strip()]
    if not items:
        return Text(empty, style=DIM)
    shown = items if limit is None else items[:limit]
    text = Text()
    for i, item in enumerate(shown):
        text.append("* ", style=ACCENT)
        text.append(item + ("\n" if i < len(shown) - 1 else ""))
    if limit is not None and len(items) > limit:
        text.append(f"\n  ... {len(items) - limit} more", style=DIM)
    return text


class RichEvents(OrchestratorEvents):
    """Live rendering of a run, shaped for screen recording."""

    def __init__(self, verbose_packages: bool = False) -> None:
        self.verbose_packages = verbose_packages
        self._total = 0
        self._index = 0

    def run_started(
        self, state: ExecutionState, assignments: list[Assignment], registry: WorkerRegistry, mock: bool
    ) -> None:
        self._total = len(assignments)
        self._index = 0

        table = Table.grid(padding=(0, 2))
        table.add_column(style=DIM)
        table.add_column()
        table.add_row("Task ID", Text(state.task_id, style=ACCENT))
        table.add_row("Objective", state.objective)
        table.add_row("Configured Workers", str(len(registry)))
        table.add_row("Assignments this run", str(len(assignments)))
        table.add_row("Gateway", Text("MOCK (no API keys)" if mock else "LIVE (LiteLLM)", style=WARN if mock else OK))

        console.print()
        console.print(
            Panel(table, title="[bold]CONTEXT ORCHESTRATION ENGINE[/bold]", border_style=ACCENT, padding=(1, 2))
        )

        roster = Table(show_header=True, header_style=DIM, box=None, padding=(0, 2))
        roster.add_column("#", style=DIM)
        roster.add_column("Worker", style=ACCENT)
        roster.add_column("Model")
        roster.add_column("Assigned task")
        for a in assignments:
            cfg = registry.get(a.worker_id)
            roster.add_row(str(a.seq), a.worker_id, cfg.model, a.task)
        console.print(Panel(roster, title="WORKER ROSTER", border_style=DIM, padding=(1, 2)))

    def worker_started(self, assignment: Assignment, config: WorkerConfig, package: HandoffPackage) -> None:
        self._index += 1
        rule(f"WORKER {self._index}/{self._total} STARTED  |  {assignment.worker_id}")
        meta = Table.grid(padding=(0, 2))
        meta.add_column(style=DIM)
        meta.add_column()
        meta.add_row("Model", config.model)
        meta.add_row("Assigned Task", assignment.task)
        meta.add_row(
            "Context received",
            f"{package.estimated_tokens} tokens / budget {package.token_budget} "
            f"({len(package.included_sections)} sections)",
        )
        meta.add_row("Raw conversation received", Text("NO", style=OK))
        console.print(meta)
        if self.verbose_packages:
            console.print(
                Panel(package.rendered_text, title="COMPILED CONTEXT PACKAGE", border_style=DIM, padding=(1, 2))
            )
        console.print()

    def worker_completed(self, run: WorkerRun) -> None:
        console.print(Text("WORKER COMPLETED", style=OK))
        console.print(Text(f"  Worker Result generated   ({run.duration_ms} ms)", style=DIM))
        console.print(Text("  Handoff Report generated", style=DIM))
        console.print()

        body = Group(
            Text("Work completed", style=ACCENT),
            _bullets(run.report.work_completed, limit=6),
            Text("\nImportant decisions", style=ACCENT),
            _bullets(
                [f"{d.decision} - {d.reason}" if d.reason else d.decision for d in run.report.important_decisions],
                limit=5,
            ),
            Text("\nProblems encountered", style=ACCENT),
            _bullets(run.report.problems_encountered, limit=4),
            Text("\nRecommended next action", style=ACCENT),
            Text(run.report.recommended_next_action or "Not stated."),
            Text("\nNotes for next worker", style=ACCENT),
            Text(run.report.notes_for_next_worker or "None."),
        )
        console.print(Panel(body, title="HANDOFF REPORT", border_style=ACCENT, padding=(1, 2)))

    def worker_failed(self, assignment: Assignment, config: WorkerConfig, error: Exception) -> None:
        console.print(
            Panel(
                Text(_one_line(str(error), 400)),
                title=f"WORKER FAILED | {assignment.worker_id} ({config.model})",
                border_style=BAD,
                padding=(1, 2),
            )
        )

    def reconciled(self, report: ReconciliationReport, state: ExecutionState) -> None:
        lines = Table.grid(padding=(0, 2))
        lines.add_column(style=DIM)
        lines.add_column()
        lines.add_row("Structured output validated", Text("YES", style=OK))
        accepted = ", ".join(f"{k}={v}" for k, v in report.accepted.items() if v) or "nothing new"
        lines.add_row("Merged into canonical state", accepted)
        dupes = ", ".join(f"{k}={v}" for k, v in report.duplicates_skipped.items() if v) or "none"
        lines.add_row("Duplicates suppressed", dupes)
        lines.add_row(
            "Claims not taken at face value",
            Text(str(len(report.unverified_artifacts) + len(report.rejected_resolutions)), style=WARN)
            if (report.unverified_artifacts or report.rejected_resolutions)
            else Text("0", style=OK),
        )
        lines.add_row("State persisted", Text("YES", style=OK))

        body = [lines]
        if report.warnings:
            body.append(Text("\nReconciler warnings", style=WARN))
            body.append(_bullets(report.warnings, limit=5))
        console.print(Panel(Group(*body), title="STATE RECONCILIATION", border_style=WARN if report.warnings else OK, padding=(1, 2)))

    def handoff(self, audit: dict) -> None:
        t = Table.grid(padding=(0, 2))
        t.add_column(style=DIM)
        t.add_column()
        t.add_row("Previous Worker", Text(audit["previous_worker"], style=ACCENT))
        t.add_row("Next Worker", Text(audit["next_worker"], style=ACCENT))
        t.add_row("Raw conversation transferred", Text("NO", style=OK))
        t.add_row("Canonical execution state transferred", Text("YES", style=OK))
        t.add_row(
            "Worker handoff report included",
            Text("YES" if audit["handoff_report_included"] else "NO", style=OK if audit["handoff_report_included"] else WARN),
        )
        t.add_row("Package size", f"{audit['package_tokens']} tokens (budget {audit['token_budget']})")
        t.add_row("Included", ", ".join(audit["included_sections"]))
        if audit["omitted_sections"]:
            t.add_row("Omitted (over budget)", Text(", ".join(audit["omitted_sections"]), style=WARN))
        console.print(Panel(t, title="CONTEXT HANDOFF", border_style=ACCENT, padding=(1, 2)))

    def package_compiled(self, package: HandoffPackage, audit: dict | None) -> None:
        pass  # already surfaced by handoff() and worker_started()

    def run_finished(self, summary: RunSummary, state: ExecutionState) -> None:
        console.print()
        t = Table.grid(padding=(0, 2))
        t.add_column(style=DIM)
        t.add_column()
        t.add_row("Workers used", str(summary.workers_used))
        t.add_row(
            "Raw conversation transfers",
            Text(str(summary.raw_conversation_transfers), style=OK if summary.raw_conversation_transfers == 0 else BAD),
        )
        t.add_row("Structured handoffs", str(summary.structured_handoffs))
        t.add_row("Worker reports generated", str(summary.reports_generated))
        t.add_row("Context packages compiled", str(summary.packages_compiled))
        t.add_row("Total context tokens sent", str(summary.total_context_tokens))
        t.add_row("Reconciler warnings", str(summary.reconciliation_warnings))
        t.add_row("Canonical execution state persisted", Text("YES" if summary.state_persisted else "NO", style=OK if summary.state_persisted else BAD))
        t.add_row(
            "Task continuity maintained across workers",
            Text("YES" if summary.continuity_maintained else "NO", style=OK if summary.continuity_maintained else BAD),
        )
        if summary.failures:
            # Provider errors can be multi-kilobyte JSON; keep the panel readable.
            t.add_row("Failures", _bullets([_one_line(f) for f in summary.failures], limit=6))

        console.print(
            Panel(
                t,
                title="[bold]EXPERIMENT COMPLETE[/bold]",
                border_style=OK if summary.continuity_maintained else BAD,
                padding=(1, 2),
            )
        )
        console.print(
            Text(f"  Inspect: coe state {state.task_id}", style=DIM)
        )
        console.print(Text(f"           coe handoffs {state.task_id}", style=DIM))
        console.print()


# --------------------------------------------------------------------------
# Inspection views (state / history / handoffs commands)
# --------------------------------------------------------------------------


def show_state(state: ExecutionState) -> None:
    header = Table.grid(padding=(0, 2))
    header.add_column(style=DIM)
    header.add_column()
    header.add_row("Task ID", Text(state.task_id, style=ACCENT))
    header.add_row("Status", state.status)
    header.add_row("Objective", state.objective)
    header.add_row("Created", str(state.created_at))
    header.add_row("Updated", str(state.updated_at))
    console.print(Panel(header, title="CANONICAL EXECUTION STATE", border_style=ACCENT, padding=(1, 2)))

    sections = [
        ("COMPLETED WORK", _bullets(state.completed_tasks)),
        ("CURRENT TASK", Text(state.current_task or "None.", style=DIM if not state.current_task else "")),
        ("PENDING TASKS", _bullets(state.pending_tasks)),
        (
            "IMPORTANT DECISIONS",
            _bullets([f"{d.decision} - {d.reason} [{d.recorded_by}]" for d in state.decisions]),
        ),
        (
            "ARTIFACTS",
            _bullets(
                [
                    f"{a.name} (v{a.version}, by {a.created_by}"
                    + (f", modified by {', '.join(a.modified_by)}" if a.modified_by else "")
                    + (", unverified" if not a.verified else "")
                    + ")"
                    for a in state.artifacts
                ]
            ),
        ),
        (
            "ISSUES",
            _bullets(
                [
                    f"[{i.severity}] {i.description}"
                    + (" (resolution claimed, unverified)" if i.resolution_claimed_by else "")
                    + (" [RESOLVED]" if i.resolved else "")
                    for i in state.issues
                ]
            ),
        ),
        (
            "FAILED ATTEMPTS",
            _bullets([f"{f.attempt} - {f.reason} [{f.recorded_by}]" for f in state.failed_attempts]),
        ),
        ("ASSUMPTIONS", _bullets([f"{a.assumption} [{a.recorded_by}]" for a in state.assumptions])),
        ("CURRENT PROGRESS", Text(state.current_progress or "None.")),
        ("LAST ACTION", Text(state.last_action or "None.")),
        ("NEXT ACTION", Text(state.next_action or "None.")),
    ]
    for title, body in sections:
        console.print(Panel(body, title=title, border_style=DIM, padding=(0, 2)))


def show_history(executions: list[WorkerExecution], handoffs: list[HandoffRecord]) -> None:
    table = Table(title="WORKER EXECUTIONS", header_style=ACCENT, expand=True)
    table.add_column("#", style=DIM, width=3)
    table.add_column("Worker", style=ACCENT)
    table.add_column("Model")
    table.add_column("Status")
    table.add_column("ms", justify="right")
    table.add_column("Ctx tok", justify="right")
    table.add_column("Raw conv")
    table.add_column("Summary")
    for e in executions:
        status_style = OK if e.status.value == "completed" else BAD
        table.add_row(
            str(e.seq),
            e.worker_id,
            e.model,
            Text(e.status.value, style=status_style),
            str(e.duration_ms),
            str(e.context_tokens_in),
            Text("NO" if not e.raw_conversation_transferred else "YES", style=OK if not e.raw_conversation_transferred else BAD),
            (e.summary or e.error or "")[:60],
        )
    console.print(table)

    h = Table(title="HANDOFF HISTORY", header_style=ACCENT, expand=True)
    h.add_column("#", style=DIM, width=3)
    h.add_column("From", style=ACCENT)
    h.add_column("To", style=ACCENT)
    h.add_column("Raw conv")
    h.add_column("Recommended next action")
    for r in handoffs:
        h.add_row(
            str(r.seq),
            r.from_worker,
            r.to_worker or "-",
            Text("NO", style=OK),
            r.report.recommended_next_action[:70],
        )
    console.print(h)


def show_handoffs(handoffs: list[HandoffRecord], packages: list[HandoffPackage], show_packages: bool = True) -> None:
    for record in handoffs:
        console.print(
            Panel(
                render_report(record.report),
                title=f"HANDOFF REPORT #{record.seq} | {record.from_worker} -> {record.to_worker or 'END'}",
                border_style=ACCENT,
                padding=(1, 2),
            )
        )
    if not show_packages:
        return
    for package in packages:
        meta = (
            f"budget {package.token_budget} | estimated {package.estimated_tokens} tokens | "
            f"raw conversation: {'YES' if package.contains_raw_conversation else 'NO'}"
        )
        omitted = f"\nomitted sections: {', '.join(package.omitted_sections)}" if package.omitted_sections else ""
        console.print(
            Panel(
                Group(Text(meta, style=DIM), Text(omitted, style=WARN) if omitted else Text(""), Text(package.rendered_text)),
                title=f"CONTEXT PACKAGE -> {package.target_worker_id}",
                border_style=DIM,
                padding=(1, 2),
            )
        )


def show_tasks(rows: list[dict]) -> None:
    if not rows:
        console.print(Text("No tasks yet. Run: coe run", style=DIM))
        return
    table = Table(title="TASKS", header_style=ACCENT, expand=True)
    table.add_column("Task ID", style=ACCENT)
    table.add_column("Status")
    table.add_column("Updated", style=DIM)
    table.add_column("Objective")
    for r in rows:
        table.add_row(r["task_id"], r["status"], r["updated_at"][:19], r["objective"][:70])
    console.print(table)


def show_workers(registry: WorkerRegistry, missing: list[str]) -> None:
    table = Table(title="CONFIGURED WORKERS", header_style=ACCENT, expand=True)
    table.add_column("#", style=DIM, width=3)
    table.add_column("Worker ID", style=ACCENT)
    table.add_column("Model")
    table.add_column("API key env")
    table.add_column("Credential")
    for i, c in enumerate(registry, 1):
        has_key = c.id not in missing
        table.add_row(
            str(i),
            c.id,
            c.model,
            c.api_key_env or "-",
            Text("found" if has_key else "missing", style=OK if has_key else WARN),
        )
    console.print(table)
    if missing:
        console.print(
            Text(
                f"\n{len(missing)} worker(s) have no credential. `run` falls back to the mock gateway "
                "unless --real is passed.",
                style=DIM,
            )
        )
