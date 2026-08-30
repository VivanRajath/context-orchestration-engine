"""Context Orchestration Engine - command line interface.

Installed as ``coe``::

    coe init                  # scaffold workers.json + .env in the current dir
    coe run                   # the built-in five-worker demonstration
    coe state <task_id>       # canonical execution state
    coe history <task_id>     # worker executions and handoff history
    coe handoffs <task_id>    # handoff reports and compiled packages
    coe resume <task_id>      # continue a persisted task
    coe step <task_id>        # advance exactly one worker turn
    coe tasks                 # list persisted tasks
    coe workers               # show the configured roster and credential status
    coe serve                 # local web playground at http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from context_orchestration.config.demo import DEMO_OBJECTIVE, DEMO_PLAN
from context_orchestration.context.compiler import ContextCompiler
from context_orchestration.core.orchestrator import (
    DEFAULT_WORKERS_PATH,
    ContextOrchestrator,
    build_assignments,
    load_registry,
    resolve_mock_mode,
)
from context_orchestration.gateway.llm_gateway import build_gateway, missing_keys
from context_orchestration.storage.sqlite_store import DEFAULT_DB, SQLiteStore
from context_orchestration.ui import console as ui

CWD_WORKERS = Path("workers.json")


def default_workers_path() -> Path:
    """Prefer the user's own workers.json; fall back to the packaged example."""
    return CWD_WORKERS if CWD_WORKERS.exists() else Path(DEFAULT_WORKERS_PATH)


def _mode(args) -> str:
    if getattr(args, "mock", False):
        return "mock"
    if getattr(args, "real", False):
        return "real"
    return "auto"


def _build(args) -> tuple[ContextOrchestrator, SQLiteStore, bool]:
    registry = load_registry(args.workers or default_workers_path())
    use_mock, missing = resolve_mock_mode(registry, _mode(args))

    if use_mock and _mode(args) == "auto" and missing:
        ui.console.print(
            ui.Text(
                f"No credentials found for: {', '.join(missing)} -> running on the MOCK gateway.\n"
                "Set the API key env vars in .env (see .env.example) and pass --real for live calls.",
                style=ui.WARN,
            )
        )
    if not use_mock and missing:
        ui.console.print(
            ui.Text(f"Warning: --real requested but no credential found for: {', '.join(missing)}", style=ui.WARN)
        )

    store = SQLiteStore(args.db)
    orchestrator = ContextOrchestrator(
        registry=registry,
        gateway=build_gateway(use_mock),
        store=store,
        compiler=ContextCompiler(token_budget=args.budget),
        events=ui.RichEvents(verbose_packages=args.show_context),
        mock=use_mock,
    )
    return orchestrator, store, use_mock


def _resolve(store: SQLiteStore, task_id: str) -> str:
    resolved = store.resolve_task_id(task_id)
    if resolved is None:
        ui.console.print(ui.Text(f"No task matching {task_id!r}. Try: coe tasks", style=ui.BAD))
        raise SystemExit(1)
    return resolved


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_run(args) -> int:
    orchestrator, store, _ = _build(args)
    try:
        objective = args.objective or DEMO_OBJECTIVE
        plan = args.task or DEMO_PLAN
        _, summary = orchestrator.run(objective, plan)
    finally:
        store.close()
    return 0 if summary.continuity_maintained else 1


def cmd_resume(args) -> int:
    orchestrator, store, _ = _build(args)
    try:
        task_id = _resolve(store, args.task_id)
        _, summary = orchestrator.resume(task_id)
        if summary.already_complete:
            ui.console.print(
                ui.Text(
                    f"\nNothing to resume: all {summary.previously_completed} assignment(s) for "
                    f"{task_id} already completed.\n"
                    f"Inspect it with: coe state {task_id}",
                    style=ui.DIM,
                )
            )
    finally:
        store.close()
    return 0


def cmd_state(args) -> int:
    with SQLiteStore(args.db) as store:
        task_id = _resolve(store, args.task_id)
        state = store.load_state(task_id)
        if state is None:
            ui.console.print(ui.Text("Task has no persisted state.", style=ui.BAD))
            return 1
        ui.show_state(state)
    return 0


def cmd_history(args) -> int:
    with SQLiteStore(args.db) as store:
        task_id = _resolve(store, args.task_id)
        ui.show_history(store.load_worker_executions(task_id), store.load_handoffs(task_id))
    return 0


def cmd_handoffs(args) -> int:
    with SQLiteStore(args.db) as store:
        task_id = _resolve(store, args.task_id)
        ui.show_handoffs(
            store.load_handoffs(task_id),
            store.load_packages(task_id),
            show_packages=not args.reports_only,
        )
    return 0


def cmd_tasks(args) -> int:
    with SQLiteStore(args.db) as store:
        ui.show_tasks(store.list_tasks())
    return 0


def cmd_workers(args) -> int:
    registry = load_registry(args.workers or default_workers_path())
    ui.show_workers(registry, missing_keys(list(registry)))

    plan = args.task or DEMO_PLAN
    assignments = build_assignments(plan, registry)
    ui.console.print(
        ui.Text(
            f"\n{len(registry)} worker(s) x {len(plan)} plan step(s) -> {len(assignments)} assignment(s).",
            style=ui.DIM,
        )
    )
    return 0


ENV_TEMPLATE = """# Context Orchestration Engine - worker credentials.
# Each worker in workers.json names the env var it reads via "api_key_env".
# Keys stay on this machine: the engine never writes them to the database.
WORKER_1_API_KEY=
WORKER_2_API_KEY=
WORKER_3_API_KEY=
WORKER_4_API_KEY=
WORKER_5_API_KEY=
"""


def cmd_init(args) -> int:
    """Scaffold a local setup in the current directory."""
    written, skipped = [], []

    for target, content in (
        (CWD_WORKERS, Path(DEFAULT_WORKERS_PATH).read_text(encoding="utf-8")),
        (Path(".env"), ENV_TEMPLATE),
    ):
        if target.exists() and not args.force:
            skipped.append(target.name)
            continue
        target.write_text(content, encoding="utf-8")
        written.append(target.name)

    for name in written:
        ui.console.print(ui.Text(f"  created {name}", style=ui.OK))
    for name in skipped:
        ui.console.print(ui.Text(f"  kept {name} (already exists; --force to overwrite)", style=ui.DIM))

    ui.console.print(
        ui.Text(
            "\nNext: edit workers.json to name your models, put the matching keys in .env,\n"
            "then run `coe run` (mock, no keys needed) or `coe run --real`.",
            style=ui.DIM,
        )
    )
    return 0


def cmd_step(args) -> int:
    orchestrator, store, _ = _build(args)
    try:
        task_id = _resolve(store, args.task_id)
        _, summary = orchestrator.resume(task_id, max_steps=args.steps)
        if summary.already_complete:
            ui.console.print(ui.Text(f"\nNothing to step: {task_id} is already complete.", style=ui.DIM))
    finally:
        store.close()
    return 0


def cmd_serve(args) -> int:
    """Run the local web playground."""
    try:
        from context_orchestration.web.server import serve
    except ImportError:
        ui.console.print(
            ui.Text(
                "The playground needs FastAPI and uvicorn:\n"
                '    pip install "context-orchestration-engine[web]"',
                style=ui.BAD,
            )
        )
        return 1

    ui.console.print(ui.Text("\nContext Orchestration Engine playground", style=ui.ACCENT))
    ui.console.print(ui.Text(f"  http://{args.host}:{args.port}\n", style=ui.OK))
    ui.console.print(ui.Text("  Ctrl+C to stop.\n", style=ui.DIM))

    serve(
        host=args.host,
        port=args.port,
        workers_path=args.workers or default_workers_path(),
        db=args.db,
        open_browser=not args.no_open,
    )
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py",
        description="Context Orchestration Engine - N independent LLM workers, one continuous task.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument(
        "--workers", default=None, help="worker configuration file (default: ./workers.json)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_run_flags(p):
        p.add_argument("--mock", action="store_true", help="force the offline mock gateway")
        p.add_argument("--real", action="store_true", help="force live provider calls via LiteLLM")
        p.add_argument("--budget", type=int, default=1200, help="context token budget per handoff package")
        p.add_argument("--show-context", action="store_true", help="print every compiled context package")

    p_run = sub.add_parser("run", help="run the demonstration (or a custom objective/plan)")
    add_run_flags(p_run)
    p_run.add_argument("--objective", help="override the demo objective")
    p_run.add_argument("--task", action="append", help="a plan step; repeat for multiple steps")
    p_run.set_defaults(func=cmd_run)

    p_resume = sub.add_parser("resume", help="resume a persisted task")
    add_run_flags(p_resume)
    p_resume.add_argument("task_id")
    p_resume.set_defaults(func=cmd_resume)

    p_state = sub.add_parser("state", help="show the canonical execution state")
    p_state.add_argument("task_id")
    p_state.set_defaults(func=cmd_state)

    p_hist = sub.add_parser("history", help="show worker executions and handoff history")
    p_hist.add_argument("task_id")
    p_hist.set_defaults(func=cmd_history)

    p_hand = sub.add_parser("handoffs", help="show handoff reports and compiled context packages")
    p_hand.add_argument("task_id")
    p_hand.add_argument("--reports-only", action="store_true", help="hide the compiled context packages")
    p_hand.set_defaults(func=cmd_handoffs)

    p_step = sub.add_parser("step", help="advance a task by a single worker turn")
    add_run_flags(p_step)
    p_step.add_argument("task_id")
    p_step.add_argument("--steps", type=int, default=1, help="how many worker turns to run")
    p_step.set_defaults(func=cmd_step)

    p_serve = sub.add_parser("serve", help="run the local web playground")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-open", action="store_true", help="do not open a browser")
    p_serve.set_defaults(func=cmd_serve)

    p_init = sub.add_parser("init", help="scaffold workers.json and .env here")
    p_init.add_argument("--force", action="store_true", help="overwrite existing files")
    p_init.set_defaults(func=cmd_init)

    p_tasks = sub.add_parser("tasks", help="list persisted tasks")
    p_tasks.set_defaults(func=cmd_tasks)

    p_workers = sub.add_parser("workers", help="show the configured worker roster")
    p_workers.add_argument("--task", action="append", help="plan step used to preview assignments")
    p_workers.set_defaults(func=cmd_workers)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
