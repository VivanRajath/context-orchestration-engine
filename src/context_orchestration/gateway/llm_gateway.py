"""The universal LLM gateway.

This is the *only* module in the project allowed to know that providers exist,
and even here the knowledge is delegated to LiteLLM. Everything above this
layer speaks in terms of "a worker with a model string".

Two implementations ship:

* ``LiteLLMGateway`` - real calls, any provider LiteLLM supports.
* ``MockGateway``    - deterministic offline responses so the architecture can
  be demonstrated and tested with no API keys and no network.

Both satisfy the same ``LLMGateway`` protocol, which is what lets the
orchestrator stay provider-blind.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from context_orchestration.core.contracts import WorkerConfig


class GatewayError(RuntimeError):
    """Raised when a model call fails in a way the worker cannot recover from."""


@dataclass
class GatewayResponse:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


class LLMGateway(Protocol):
    def complete(
        self,
        config: WorkerConfig,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
    ) -> GatewayResponse: ...


# --------------------------------------------------------------------------
# JSON extraction helpers
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response, fences and prose included."""
    if not text or not text.strip():
        raise ValueError("empty response")

    candidates: list[str] = []
    stripped = text.strip()
    candidates.append(stripped)
    candidates.extend(m.strip() for m in _FENCE_RE.findall(text))

    brace = _first_balanced_object(text)
    if brace:
        candidates.append(brace)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]

    raise ValueError(f"no JSON object found in response: {text[:200]!r}")


def _first_balanced_object(text: str) -> str | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


# --------------------------------------------------------------------------
# LiteLLM-backed gateway
# --------------------------------------------------------------------------


_RETRY_AFTER_RE = re.compile(r"try again in ([0-9.]+)\s*s", re.IGNORECASE)


def is_rate_limit(exc: Exception) -> bool:
    return type(exc).__name__ == "RateLimitError" or "rate limit" in str(exc).lower()


def retry_delay(exc: Exception, attempt: int, cap: float = 60.0) -> float:
    """Honour the provider's own suggested delay; fall back to exponential."""
    match = _RETRY_AFTER_RE.search(str(exc))
    if match:
        return min(float(match.group(1)) + 0.5, cap)
    return min(2.0**attempt, cap)


class LiteLLMGateway:
    """Real provider access through LiteLLM's common interface."""

    def __init__(
        self,
        timeout: int = 120,
        drop_params: bool = True,
        rate_limit_retries: int = 4,
        sleep=time.sleep,
    ) -> None:
        self.timeout = timeout
        self._drop_params = drop_params
        self.rate_limit_retries = rate_limit_retries
        self._sleep = sleep
        self._litellm = None

    def _client(self):
        if self._litellm is None:
            try:
                import litellm
            except ImportError as exc:  # pragma: no cover - install-time issue
                raise GatewayError(
                    "litellm is not installed. It is optional: the built-in HTTP "
                    "gateway needs nothing. Run: "
                    'pip install "context-orchestration-engine[litellm]"'
                ) from exc
            litellm.drop_params = self._drop_params
            litellm.suppress_debug_info = True
            self._litellm = litellm
        return self._litellm

    def _resolve_key(self, config: WorkerConfig) -> str | None:
        if config.api_key:
            return config.api_key
        if config.api_key_env:
            return os.environ.get(config.api_key_env)
        return None

    def complete(
        self,
        config: WorkerConfig,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
    ) -> GatewayResponse:
        litellm = self._client()

        kwargs: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "timeout": self.timeout,
        }
        key = self._resolve_key(config)
        if key:
            kwargs["api_key"] = key
        if config.api_base:
            kwargs["api_base"] = config.api_base
        kwargs.update(config.provider_config)

        attempts: list[dict[str, Any]] = []
        if json_schema is not None:
            # Best case: the provider enforces the schema for us.
            attempts.append(
                {
                    **kwargs,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {"name": "worker_output", "schema": json_schema, "strict": False},
                    },
                }
            )
            attempts.append({**kwargs, "response_format": {"type": "json_object"}})
        attempts.append(kwargs)  # last resort: prompt-only JSON discipline

        last_error: Exception | None = None
        for attempt in attempts:
            try:
                response = self._invoke(litellm, attempt)
            except Exception as exc:  # provider/schema support varies wildly
                last_error = exc
                if is_rate_limit(exc):
                    # Not a schema problem - trying another response_format
                    # would just burn more of an already-exhausted budget.
                    break
                continue
            text = (response.choices[0].message.content or "").strip()
            usage = {}
            raw_usage = getattr(response, "usage", None)
            if raw_usage is not None:
                usage = {
                    "prompt_tokens": getattr(raw_usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(raw_usage, "completion_tokens", 0),
                    "total_tokens": getattr(raw_usage, "total_tokens", 0),
                }
            return GatewayResponse(text=text, model=config.model, usage=usage, raw=response)

        raise GatewayError(f"model call failed for {config.id} ({config.model}): {last_error}")

    def _invoke(self, litellm, kwargs: dict[str, Any]):
        """One request, retrying while the provider reports a rate limit.

        Rate limits are a scheduling problem, not a failure - the same worker
        can simply wait. (A future SwitchPolicy could instead hand the turn to
        a different worker; the engine loop would not change.)
        """
        for attempt in range(self.rate_limit_retries + 1):
            try:
                return litellm.completion(**kwargs)
            except Exception as exc:
                if not is_rate_limit(exc) or attempt >= self.rate_limit_retries:
                    raise
                self._sleep(retry_delay(exc, attempt))
        raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# Mock gateway
# --------------------------------------------------------------------------


class MockGateway:
    """Offline gateway that reads the compiled context and answers from it.

    This is not a stub that returns canned text regardless of input. It parses
    the handoff package it was given and builds its reply out of that package -
    which is exactly what makes the mock demo meaningful: if context continuity
    were broken, the mock's output would visibly lose the thread too.
    """

    def __init__(self, scripts: dict[str, dict] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.scripts = scripts or {}

    def complete(
        self,
        config: WorkerConfig,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
    ) -> GatewayResponse:
        prompt = "\n\n".join(m.get("content", "") for m in messages)
        self.calls.append({"worker": config.id, "model": config.model, "messages": messages})

        kind = self._kind(messages, json_schema)
        ctx = _MockContext.parse(prompt)

        scripted = self.scripts.get(f"{config.id}:{kind}")
        payload = scripted if scripted is not None else _mock_payload(kind, config, ctx)

        return GatewayResponse(
            text=json.dumps(payload, indent=2),
            model=config.model,
            usage={"prompt_tokens": len(prompt) // 4, "completion_tokens": 180, "total_tokens": len(prompt) // 4 + 180},
        )

    @staticmethod
    def _kind(messages: list[dict[str, str]], json_schema: dict[str, Any] | None) -> str:
        """Which of the worker's two calls this is.

        Keyed off the requested schema, not off prompt text - context packages
        legitimately contain phrases like "handoff report" and must not be
        mistaken for a request to write one.
        """
        title = (json_schema or {}).get("title")
        if title == "HandoffReport":
            return "report"
        if title == "WorkerResult":
            return "result"
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        return "report" if "HANDOFF REPORT" in system.upper() else "result"


@dataclass
class _MockContext:
    """What the mock worker can see - only the compiled package, nothing else."""

    assigned_task: str = ""
    objective: str = ""
    decisions: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    stage: str = "generic"

    @classmethod
    def parse(cls, prompt: str) -> "_MockContext":
        ctx = cls()
        ctx.assigned_task = _section(prompt, "YOUR ASSIGNED TASK")
        ctx.objective = _section(prompt, "TASK OBJECTIVE")
        ctx.decisions = _bullet_lines(_section(prompt, "IMPORTANT DECISIONS"))
        ctx.artifacts = _bullet_lines(_section(prompt, "ARTIFACTS"))
        ctx.completed = _bullet_lines(_section(prompt, "COMPLETED WORK"))
        ctx.stage = _classify(ctx.assigned_task)

        # On the handoff-report call the worker is shown its own result rather
        # than a context package; read the inherited decisions back out of it.
        if "YOUR WORKER RESULT" in prompt and not ctx.decisions:
            marker = prompt.index("YOUR WORKER RESULT")
            blob = _first_balanced_object(prompt[marker:])
            if blob:
                try:
                    own = json.loads(blob)
                except json.JSONDecodeError:
                    own = {}
                ctx.decisions = [
                    d.get("decision", "") for d in own.get("decisions", []) if isinstance(d, dict)
                ]
                ctx.completed = list(own.get("completed_tasks", []))
        return ctx


def _section(prompt: str, title: str) -> str:
    """Read one section body out of a rendered handoff package."""
    lines = prompt.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == title)
    except StopIteration:
        return ""
    body: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped and stripped == stripped.upper() and len(stripped) > 3 and not stripped.startswith("-"):
            if re.match(r"^[A-Z0-9 ()\-,.:]+$", stripped) and body:
                break
        body.append(line)
    return "\n".join(body).strip()


def _bullet_lines(text: str) -> list[str]:
    return [ln.strip("- ").strip() for ln in text.splitlines() if ln.strip()]


_STAGES = [
    ("schema", ("schema", "database", "table", "postgres", "migration", "erd")),
    ("auth", ("auth", "jwt", "login", "permission", "token", "rbac")),
    ("endpoints", ("endpoint", "route", "crud", "api surface", "rest")),
    ("review", ("review", "inconsisten", "audit", "gap", "verify", "missing")),
    ("architecture", ("requirement", "architecture", "scope", "overall")),
]


def _classify(task: str) -> str:
    low = (task or "").lower()
    for stage, keywords in _STAGES:
        if any(k in low for k in keywords):
            return stage
    return "generic"


def _mock_payload(kind: str, config: WorkerConfig, ctx: _MockContext) -> dict:
    stage = ctx.stage
    builder = _MOCK_STAGES.get(stage, _mock_generic)
    result, report = builder(config, ctx)
    return report if kind == "report" else result


def _carried(ctx: _MockContext) -> str:
    """A line proving the worker actually read inherited state."""
    if ctx.decisions:
        return f"Building on {len(ctx.decisions)} inherited decision(s), including: {ctx.decisions[0]}"
    return "No inherited decisions were present in the handoff package."


def _mock_architecture(config: WorkerConfig, ctx: _MockContext):
    result = {
        "summary": "Defined functional requirements and the overall service architecture.",
        "completed_tasks": [ctx.assigned_task or "Define requirements and overall architecture."],
        "decisions": [
            {"decision": "Use FastAPI as the API framework", "reason": "Async support and automatic OpenAPI generation"},
            {"decision": "Use PostgreSQL as the primary datastore", "reason": "Relational task/user data with transactional integrity"},
            {"decision": "Adopt a layered architecture (router / service / repository)", "reason": "Keeps business logic testable and independent of the web layer"},
        ],
        "artifacts": [
            {"name": "requirements.md", "kind": "document", "description": "Functional and non-functional requirements"},
            {"name": "architecture.md", "kind": "document", "description": "Layered service architecture and component boundaries"},
        ],
        "issues": [],
        "failed_attempts": [
            {"attempt": "Modelling tasks and projects in a single denormalised table", "reason": "Made per-project permission checks impossible to express cleanly"}
        ],
        "assumptions": [
            {"assumption": "Single-tenant deployment for the MVP", "reason": "No multi-tenant requirement was stated"}
        ],
        "current_progress": "Requirements and high-level architecture are complete. No schema or endpoints exist yet.",
        "last_action": "Wrote architecture.md describing the layered service structure.",
        "next_action": "Design the PostgreSQL database schema for users, projects and tasks.",
    }
    report = {
        "work_completed": ["Functional and non-functional requirements", "Layered service architecture"],
        "current_state": "Architecture agreed. Nothing implemented. Schema not designed.",
        "important_decisions": result["decisions"],
        "artifacts_created_or_modified": ["requirements.md", "architecture.md"],
        "problems_encountered": [],
        "failed_attempts": ["Single denormalised table for tasks and projects - broke permission modelling"],
        "assumptions": ["Single-tenant deployment for the MVP"],
        "last_action": "Wrote architecture.md describing the layered service structure.",
        "recommended_next_action": "Design the PostgreSQL schema for users, projects and tasks.",
        "notes_for_next_worker": (
            "The layered split (router/service/repository) is load-bearing - keep persistence logic out of routers. "
            "Do not revisit the FastAPI or PostgreSQL choice; both are settled."
        ),
    }
    return result, report


def _mock_schema(config: WorkerConfig, ctx: _MockContext):
    result = {
        "summary": "Designed the relational schema on top of the inherited architecture.",
        "completed_tasks": [ctx.assigned_task or "Design the database schema."],
        "decisions": [
            {"decision": "Use UUID primary keys", "reason": "Avoids sequential-ID enumeration on public endpoints"},
            {"decision": "Soft-delete tasks via deleted_at", "reason": "Audit history is a stated requirement"},
            {"decision": "Use PostgreSQL as the primary datastore", "reason": "Confirmed the inherited decision; no change needed"},
        ],
        "artifacts": [
            {"name": "schema.sql", "kind": "sql", "description": "users, projects, tasks, task_assignments with FK constraints"},
            {"name": "architecture.md", "kind": "document", "description": "Updated with the data-model section"},
        ],
        "issues": [
            {"description": "Task ordering within a project is unspecified (lexical vs explicit rank)", "severity": "medium", "resolved": False}
        ],
        "failed_attempts": [
            {"attempt": "Storing task tags as a comma-separated text column", "reason": "Cannot be indexed for tag filtering; replaced with a join table"}
        ],
        "assumptions": [
            {"assumption": "A task belongs to exactly one project", "reason": "Simplifies permission inheritance"}
        ],
        "current_progress": "Schema complete with four tables and FK constraints. Authentication not yet designed.",
        "last_action": "Wrote schema.sql including the task_tags join table.",
        "next_action": "Design authentication and authorization for the API.",
    }
    report = {
        "work_completed": ["PostgreSQL schema for users, projects, tasks", "task_tags join table"],
        "current_state": "Schema is complete and consistent with the layered architecture. Auth is untouched.",
        "important_decisions": result["decisions"],
        "artifacts_created_or_modified": ["schema.sql", "architecture.md"],
        "problems_encountered": ["Task ordering within a project is still unspecified"],
        "failed_attempts": ["Comma-separated tag column - unindexable, replaced by a join table"],
        "assumptions": ["A task belongs to exactly one project"],
        "last_action": "Wrote schema.sql including the task_tags join table.",
        "recommended_next_action": "Design JWT-based authentication and per-project authorization.",
        "notes_for_next_worker": (
            "The schema already contains the user/project/task relationships. Do not redesign it. "
            "Permissions should hang off project membership, not off individual tasks."
        ),
    }
    return result, report


def _mock_auth(config: WorkerConfig, ctx: _MockContext):
    result = {
        "summary": "Designed authentication and project-scoped authorization.",
        "completed_tasks": [ctx.assigned_task or "Design authentication."],
        "decisions": [
            {"decision": "Use JWT access tokens with refresh tokens", "reason": "Stateless verification suits the layered service design"},
            {"decision": "Hash passwords with argon2id", "reason": "Current best-practice KDF with tuned memory cost"},
            {"decision": "Authorize on project membership", "reason": "Matches the inherited schema where tasks belong to one project"},
        ],
        "artifacts": [
            {"name": "auth.py", "kind": "code", "description": "JWT issuing, verification and the FastAPI dependency"},
            {"name": "schema.sql", "kind": "sql", "description": "Added refresh_tokens and project_members tables"},
        ],
        "issues": [
            {"description": "Refresh-token rotation and revocation strategy not finalised", "severity": "high", "resolved": False}
        ],
        "failed_attempts": [
            {"attempt": "Session cookies held in application memory", "reason": "Breaks under multi-instance deployment; no shared session store"}
        ],
        "assumptions": [
            {"assumption": "Access tokens live 15 minutes, refresh tokens 14 days", "reason": "No requirement was stated; conventional defaults"}
        ],
        "current_progress": "Auth design complete. Endpoints still undesigned.",
        "last_action": "Specified the get_current_user FastAPI dependency in auth.py.",
        "next_action": "Design the REST API endpoints for task and project CRUD.",
    }
    report = {
        "work_completed": ["JWT authentication design", "Project-membership authorization model"],
        "current_state": "Auth designed and reflected in the schema. Endpoint surface not yet defined.",
        "important_decisions": result["decisions"],
        "artifacts_created_or_modified": ["auth.py", "schema.sql", "auth_design.md"],
        "problems_encountered": ["Refresh-token rotation strategy unresolved"],
        "failed_attempts": ["In-memory session cookies - fails under horizontal scaling"],
        "assumptions": ["15-minute access tokens, 14-day refresh tokens"],
        "last_action": "Specified the get_current_user FastAPI dependency in auth.py.",
        "recommended_next_action": "Design task and project CRUD endpoints using get_current_user for authorization.",
        "notes_for_next_worker": (
            "Every endpoint must depend on get_current_user and check project membership. "
            "Refresh-token rotation is still open - do not assume it is solved."
        ),
    }
    return result, report


def _mock_endpoints(config: WorkerConfig, ctx: _MockContext):
    result = {
        "summary": "Designed the REST endpoint surface consistent with the inherited auth and schema.",
        "completed_tasks": [ctx.assigned_task or "Design the API endpoints."],
        "decisions": [
            {"decision": "Cursor-based pagination for task listing", "reason": "Stable ordering under concurrent inserts"},
            {"decision": "Return 404 rather than 403 for tasks outside a user's projects", "reason": "Avoids leaking existence of other projects' tasks"},
        ],
        "artifacts": [
            {"name": "api_endpoints.md", "kind": "document", "description": "Full REST surface with request/response schemas"},
            {"name": "routers/tasks.py", "kind": "code", "description": "Task CRUD router skeleton"},
        ],
        "issues": [
            {"description": "Bulk task update endpoint has no defined transactional semantics", "severity": "medium", "resolved": False}
        ],
        "failed_attempts": [
            {"attempt": "Offset pagination for the task list", "reason": "Produces duplicate and skipped rows when tasks are inserted mid-pagination"}
        ],
        "assumptions": [
            {"assumption": "Task list defaults to 50 items per page", "reason": "No page-size requirement was given"}
        ],
        "current_progress": "Endpoint surface designed for tasks and projects. Review not yet performed.",
        "last_action": "Documented POST /tasks and GET /tasks with cursor pagination.",
        "next_action": "Review the whole architecture for inconsistencies and missing dependencies.",
    }
    report = {
        "work_completed": ["Task CRUD endpoints", "Project endpoints", "Pagination design"],
        "current_state": "All layers now have a design. Nothing has been reviewed end to end.",
        "important_decisions": result["decisions"],
        "artifacts_created_or_modified": ["api_endpoints.md", "routers/tasks.py", "openapi_notes.md"],
        "problems_encountered": ["Bulk update transactional semantics undefined"],
        "failed_attempts": ["Offset pagination - unstable under concurrent inserts"],
        "assumptions": ["Default page size of 50"],
        "last_action": "Documented POST /tasks and GET /tasks with cursor pagination.",
        "recommended_next_action": "Review requirements, schema, auth and endpoints together for gaps.",
        "notes_for_next_worker": (
            "Two issues are still open: refresh-token rotation and bulk-update semantics. "
            "The 404-instead-of-403 rule must hold across every task endpoint."
        ),
    }
    return result, report


def _mock_review(config: WorkerConfig, ctx: _MockContext):
    inherited = _carried(ctx)
    result = {
        "summary": "Reviewed the full design for inconsistencies and missing dependencies.",
        "completed_tasks": [ctx.assigned_task or "Review the complete architecture."],
        "decisions": [
            {"decision": "Add an explicit rank column for task ordering", "reason": "Closes the ordering gap raised during schema design"},
            {"decision": "Use FastAPI as the API framework", "reason": "Re-affirming the inherited decision - no change"},
        ],
        "artifacts": [
            {"name": "review_findings.md", "kind": "document", "description": "Gap analysis across all four design layers"}
        ],
        "issues": [
            {"description": "Refresh-token rotation and revocation strategy not finalised", "severity": "high", "resolved": True},
            {"description": "No rate limiting is specified for the auth endpoints", "severity": "high", "resolved": False},
        ],
        "failed_attempts": [],
        "assumptions": [
            {"assumption": "Deployment targets a single PostgreSQL primary", "reason": "No replication requirement stated"}
        ],
        "current_progress": "Design reviewed end to end. Two gaps closed, one new gap raised.",
        "last_action": "Wrote review_findings.md listing four cross-layer gaps.",
        "next_action": "Implement the design, starting with schema migrations.",
    }
    report = {
        "work_completed": ["Cross-layer consistency review", "Gap analysis of schema, auth and endpoints"],
        "current_state": "The design is coherent. One high-severity gap (auth rate limiting) remains open.",
        "important_decisions": result["decisions"],
        "artifacts_created_or_modified": ["review_findings.md", "architecture.md"],
        "problems_encountered": ["Auth endpoints have no rate limiting"],
        "failed_attempts": [],
        "assumptions": ["Single PostgreSQL primary"],
        "last_action": "Wrote review_findings.md listing four cross-layer gaps.",
        "recommended_next_action": "Begin implementation with schema migrations, then auth, then routers.",
        "notes_for_next_worker": f"{inherited}. Rate limiting on auth endpoints is the only open blocker.",
    }
    return result, report


def _mock_generic(config: WorkerConfig, ctx: _MockContext):
    task = ctx.assigned_task or "the assigned task"
    result = {
        "summary": f"Worked on: {task}",
        "completed_tasks": [task],
        "decisions": [{"decision": f"Proceeded with {task} as specified", "reason": _carried(ctx)}],
        "artifacts": [{"name": f"{_slug(task)}.md", "kind": "document", "description": f"Output of {task}"}],
        "issues": [],
        "failed_attempts": [],
        "assumptions": [],
        "current_progress": f"Completed: {task}",
        "last_action": f"Finished {task}",
        "next_action": "Continue with the next pending task.",
    }
    report = {
        "work_completed": [task],
        "current_state": f"{task} is complete.",
        "important_decisions": result["decisions"],
        "artifacts_created_or_modified": [f"{_slug(task)}.md"],
        "problems_encountered": [],
        "failed_attempts": [],
        "assumptions": [],
        "last_action": f"Finished {task}",
        "recommended_next_action": "Continue with the next pending task.",
        "notes_for_next_worker": _carried(ctx),
    }
    return result, report


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "output").lower()).strip("_")[:40] or "output"


_MOCK_STAGES = {
    "architecture": _mock_architecture,
    "schema": _mock_schema,
    "auth": _mock_auth,
    "endpoints": _mock_endpoints,
    "review": _mock_review,
    "generic": _mock_generic,
}


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


def build_gateway(mock: bool) -> LLMGateway:
    return MockGateway() if mock else LiteLLMGateway()


def missing_keys(configs: list[WorkerConfig]) -> list[str]:
    """Which workers have no resolvable credentials. Drives auto-mock."""
    missing = []
    for c in configs:
        if c.api_key:
            continue
        if c.api_key_env and os.environ.get(c.api_key_env):
            continue
        missing.append(c.id)
    return missing
