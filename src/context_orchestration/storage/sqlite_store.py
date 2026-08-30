"""SQLite persistence for the canonical execution state.

The engine writes after *every* worker turn, not at the end of a run. That is
what makes a task resumable: if the process dies between worker 3 and worker 4,
worker 4 can still be handed a correctly compiled context package built from
what is on disk.

Stored per task:

* the full ``ExecutionState`` snapshot (latest, plus one snapshot per turn)
* every worker execution record
* every handoff report
* every compiled context package, including its rendered text
* an append-only event log for auditing the run
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from context_orchestration.context.state import ExecutionState
from context_orchestration.core.contracts import (
    Assignment,
    HandoffPackage,
    HandoffRecord,
    WorkerConfig,
    WorkerExecution,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    objective   TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    plan_json   TEXT NOT NULL DEFAULT '[]',
    workers_json TEXT NOT NULL DEFAULT '[]',
    assignments_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS state_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    state_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS worker_executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    worker_id   TEXT NOT NULL,
    model       TEXT NOT NULL,
    execution_json TEXT NOT NULL,
    result_json TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS handoffs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    from_worker TEXT NOT NULL,
    to_worker   TEXT,
    record_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (task_id, seq)
);

CREATE TABLE IF NOT EXISTS context_packages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL,
    package_id    TEXT NOT NULL UNIQUE,
    seq           INTEGER NOT NULL,
    worker_id     TEXT NOT NULL,
    package_json  TEXT NOT NULL,
    rendered_text TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exec_task ON worker_executions (task_id, seq);
CREATE INDEX IF NOT EXISTS idx_handoff_task ON handoffs (task_id, seq);
CREATE INDEX IF NOT EXISTS idx_pkg_task ON context_packages (task_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_task ON events (task_id, id);
"""

DEFAULT_DB = "orchestration.db"


def _json(model: Any) -> str:
    if hasattr(model, "model_dump"):
        return json.dumps(model.model_dump(mode="json"))
    return json.dumps(model)


def _ts(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


def _roster(workers: list[WorkerConfig]) -> str:
    """Serialize the roster for the task record, minus any literal key.

    A ``WorkerConfig`` may carry an inline ``api_key`` - from workers.json, or
    from a key typed into the playground. The roster is stored so a resumed run
    knows which workers a task was created with; it is not a credential store,
    and a database that outlives the run has no business holding one.
    """
    return json.dumps(
        [w.model_dump(mode="json", exclude={"api_key"}) for w in workers]
    )


class SQLiteStore:
    """Small, explicit persistence layer. No ORM, no magic."""

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- tasks ----------------------------------------------------------

    def create_task(
        self,
        state: ExecutionState,
        plan: list[str],
        workers: list[WorkerConfig],
        assignments: list[Assignment],
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO tasks
               (task_id, objective, status, created_at, updated_at,
                state_json, plan_json, workers_json, assignments_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                state.task_id,
                state.objective,
                state.status,
                _ts(state.created_at),
                _ts(state.updated_at),
                _json(state),
                json.dumps(plan),
                _roster(workers),
                json.dumps([a.model_dump(mode="json") for a in assignments]),
            ),
        )
        self.conn.commit()
        self.log_event(state.task_id, "task_created", {"objective": state.objective, "plan": plan})

    def save_state(self, state: ExecutionState, seq: int | None = None) -> None:
        state.touch()
        self.conn.execute(
            "UPDATE tasks SET status=?, updated_at=?, state_json=? WHERE task_id=?",
            (state.status, _ts(state.updated_at), _json(state), state.task_id),
        )
        if seq is not None:
            self.conn.execute(
                """INSERT OR REPLACE INTO state_snapshots (task_id, seq, state_json, created_at)
                   VALUES (?,?,?,?)""",
                (state.task_id, seq, _json(state), _ts()),
            )
        self.conn.commit()

    def load_state(self, task_id: str) -> ExecutionState | None:
        row = self.conn.execute(
            "SELECT state_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return ExecutionState.model_validate_json(row["state_json"])

    def load_task_meta(self, task_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return {
            "task_id": row["task_id"],
            "objective": row["objective"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "plan": json.loads(row["plan_json"]),
            "workers": [WorkerConfig.model_validate(w) for w in json.loads(row["workers_json"])],
            "assignments": [Assignment.model_validate(a) for a in json.loads(row["assignments_json"])],
        }

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT task_id, objective, status, created_at, updated_at FROM tasks "
            "ORDER BY datetime(updated_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_task_id(self, prefix: str) -> str | None:
        """Accept an unambiguous task-id prefix so the CLI stays typeable."""
        row = self.conn.execute(
            "SELECT task_id FROM tasks WHERE task_id = ?", (prefix,)
        ).fetchone()
        if row:
            return row["task_id"]
        rows = self.conn.execute(
            "SELECT task_id FROM tasks WHERE task_id LIKE ?", (prefix + "%",)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["task_id"]
        return None

    def set_status(self, task_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?", (status, _ts(), task_id)
        )
        self.conn.commit()

    # -- worker executions ----------------------------------------------

    def save_worker_execution(
        self, task_id: str, execution: WorkerExecution, result: Any = None
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO worker_executions
               (task_id, seq, worker_id, model, execution_json, result_json, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                task_id,
                execution.seq,
                execution.worker_id,
                execution.model,
                _json(execution),
                _json(result) if result is not None else None,
                _ts(),
            ),
        )
        self.conn.commit()

    def load_worker_executions(self, task_id: str) -> list[WorkerExecution]:
        rows = self.conn.execute(
            "SELECT execution_json FROM worker_executions WHERE task_id=? ORDER BY seq",
            (task_id,),
        ).fetchall()
        return [WorkerExecution.model_validate_json(r["execution_json"]) for r in rows]

    def load_worker_result(self, task_id: str, seq: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT result_json FROM worker_executions WHERE task_id=? AND seq=?", (task_id, seq)
        ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return json.loads(row["result_json"])

    # -- handoffs --------------------------------------------------------

    def save_handoff(self, task_id: str, record: HandoffRecord) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO handoffs
               (task_id, seq, from_worker, to_worker, record_json, created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                task_id,
                record.seq,
                record.from_worker,
                record.to_worker,
                _json(record),
                _ts(record.created_at),
            ),
        )
        self.conn.commit()

    def load_handoffs(self, task_id: str) -> list[HandoffRecord]:
        rows = self.conn.execute(
            "SELECT record_json FROM handoffs WHERE task_id=? ORDER BY seq", (task_id,)
        ).fetchall()
        return [HandoffRecord.model_validate_json(r["record_json"]) for r in rows]

    # -- context packages -------------------------------------------------

    def save_package(self, task_id: str, seq: int, package: HandoffPackage) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO context_packages
               (task_id, package_id, seq, worker_id, package_json, rendered_text, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                task_id,
                package.package_id,
                seq,
                package.target_worker_id,
                _json(package),
                package.rendered_text,
                _ts(package.compiled_at),
            ),
        )
        self.conn.commit()

    def load_packages(self, task_id: str) -> list[HandoffPackage]:
        rows = self.conn.execute(
            "SELECT package_json FROM context_packages WHERE task_id=? ORDER BY seq", (task_id,)
        ).fetchall()
        return [HandoffPackage.model_validate_json(r["package_json"]) for r in rows]

    # -- events -----------------------------------------------------------

    def log_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO events (task_id, kind, payload_json, created_at) VALUES (?,?,?,?)",
            (task_id, kind, json.dumps(payload, default=str), _ts()),
        )
        self.conn.commit()

    def load_events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT kind, payload_json, created_at FROM events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [
            {"kind": r["kind"], "payload": json.loads(r["payload_json"]), "created_at": r["created_at"]}
            for r in rows
        ]
