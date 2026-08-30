"""Universal contracts for the Context Orchestration Engine.

Nothing in this module knows about a specific LLM provider. Every model here
describes *execution state* or a *worker claim about execution state*, which is
what makes workers interchangeable.

Two families of models live here:

1. ``Raw*`` models - the loose shapes an LLM is asked to emit. They carry no
   provenance and no trust markers, because the model has no business
   asserting either.
2. Canonical models - what the engine stores. The State Reconciler is the only
   component allowed to turn a ``Raw*`` into a canonical record, and it stamps
   provenance (``recorded_by``, timestamps) and trust (``verified``) itself.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Normalize free text so near-identical claims dedupe against each other."""
    return _NORMALIZE_RE.sub(" ", (text or "").lower()).strip()


class Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


# --------------------------------------------------------------------------
# Raw worker-emitted shapes (untrusted claims)
# --------------------------------------------------------------------------


class RawDecision(Base):
    decision: str = ""
    reason: str = ""


class RawArtifact(Base):
    """Artifacts are accepted as either a bare string or an object."""

    name: str = ""
    kind: str = "document"
    description: str = ""
    content: str | None = None

    @classmethod
    def coerce(cls, value: Any) -> "RawArtifact":
        if isinstance(value, str):
            return cls(name=value.strip())
        if isinstance(value, dict):
            name = value.get("name") or value.get("artifact") or value.get("path") or ""
            return cls(
                name=str(name).strip(),
                kind=str(value.get("kind") or value.get("type") or "document"),
                description=str(value.get("description") or ""),
                content=value.get("content"),
            )
        return cls(name=str(value))


class RawIssue(Base):
    description: str = ""
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    resolved: bool = False

    @classmethod
    def coerce(cls, value: Any) -> "RawIssue":
        if isinstance(value, str):
            return cls(description=value.strip())
        if isinstance(value, dict):
            desc = value.get("description") or value.get("issue") or value.get("problem") or ""
            sev = str(value.get("severity") or "medium").lower()
            if sev not in {"low", "medium", "high", "critical"}:
                sev = "medium"
            return cls(
                description=str(desc).strip(),
                severity=sev,
                resolved=bool(value.get("resolved", False)),
            )
        return cls(description=str(value))


class RawFailedAttempt(Base):
    attempt: str = ""
    reason: str = ""

    @classmethod
    def coerce(cls, value: Any) -> "RawFailedAttempt":
        if isinstance(value, str):
            return cls(attempt=value.strip())
        if isinstance(value, dict):
            attempt = value.get("attempt") or value.get("what") or value.get("description") or ""
            reason = value.get("reason") or value.get("why") or ""
            return cls(attempt=str(attempt).strip(), reason=str(reason).strip())
        return cls(attempt=str(value))


class RawAssumption(Base):
    assumption: str = ""
    reason: str = ""

    @classmethod
    def coerce(cls, value: Any) -> "RawAssumption":
        if isinstance(value, str):
            return cls(assumption=value.strip())
        if isinstance(value, dict):
            a = value.get("assumption") or value.get("description") or ""
            return cls(assumption=str(a).strip(), reason=str(value.get("reason") or ""))
        return cls(assumption=str(value))


def _coerce_list(values: Any, coercer) -> list:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    out = []
    for v in values:
        try:
            out.append(coercer(v))
        except Exception:  # a malformed entry must not sink the whole result
            continue
    return out


def _coerce_decision(value: Any) -> RawDecision:
    if isinstance(value, str):
        return RawDecision(decision=value.strip())
    if isinstance(value, dict):
        return RawDecision(
            decision=str(value.get("decision") or value.get("choice") or "").strip(),
            reason=str(value.get("reason") or value.get("why") or "").strip(),
        )
    return RawDecision(decision=str(value))


class WorkerResult(Base):
    """The structured output every worker must return. This is a *claim*."""

    summary: str = ""
    completed_tasks: list[str] = Field(default_factory=list)
    decisions: list[RawDecision] = Field(default_factory=list)
    artifacts: list[RawArtifact] = Field(default_factory=list)
    issues: list[RawIssue] = Field(default_factory=list)
    failed_attempts: list[RawFailedAttempt] = Field(default_factory=list)
    assumptions: list[RawAssumption] = Field(default_factory=list)
    current_progress: str = ""
    last_action: str = ""
    next_action: str = ""

    @field_validator("completed_tasks", mode="before")
    @classmethod
    def _strings(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        return [str(x).strip() for x in v if str(x).strip()]

    @field_validator("artifacts", mode="before")
    @classmethod
    def _artifacts(cls, v):
        return _coerce_list(v, RawArtifact.coerce)

    @field_validator("issues", mode="before")
    @classmethod
    def _issues(cls, v):
        return _coerce_list(v, RawIssue.coerce)

    @field_validator("failed_attempts", mode="before")
    @classmethod
    def _failed(cls, v):
        return _coerce_list(v, RawFailedAttempt.coerce)

    @field_validator("assumptions", mode="before")
    @classmethod
    def _assumptions(cls, v):
        return _coerce_list(v, RawAssumption.coerce)

    @field_validator("decisions", mode="before")
    @classmethod
    def _decisions(cls, v):
        return _coerce_list(v, _coerce_decision)


def _flatten_text_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    out: list[str] = []
    for item in v:
        if isinstance(item, dict):
            item = (
                item.get("description")
                or item.get("name")
                or item.get("attempt")
                or item.get("assumption")
                or item.get("issue")
                or next((str(x) for x in item.values() if x), "")
            )
        text = str(item).strip()
        if text:
            out.append(text)
    return out


class HandoffReport(Base):
    """The mandatory report a worker writes *for the next worker*."""

    work_completed: list[str] = Field(default_factory=list)
    current_state: str = ""
    important_decisions: list[RawDecision] = Field(default_factory=list)
    artifacts_created_or_modified: list[str] = Field(default_factory=list)
    problems_encountered: list[str] = Field(default_factory=list)
    failed_attempts: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    last_action: str = ""
    recommended_next_action: str = ""
    notes_for_next_worker: str = ""

    @field_validator(
        "work_completed",
        "artifacts_created_or_modified",
        "problems_encountered",
        "failed_attempts",
        "assumptions",
        mode="before",
    )
    @classmethod
    def _flatten(cls, v):
        return _flatten_text_list(v)

    @field_validator("important_decisions", mode="before")
    @classmethod
    def _decisions(cls, v):
        return _coerce_list(v, _coerce_decision)


# --------------------------------------------------------------------------
# Canonical records (engine-owned, provenance-stamped)
# --------------------------------------------------------------------------


class Decision(Base):
    id: str = Field(default_factory=lambda: new_id("dec"))
    decision: str
    reason: str = ""
    recorded_by: str = "engine"
    recorded_at: datetime = Field(default_factory=utcnow)
    verified: bool = False

    @property
    def key(self) -> str:
        return normalize(self.decision)


class Artifact(Base):
    id: str = Field(default_factory=lambda: new_id("art"))
    name: str
    kind: str = "document"
    description: str = ""
    content: str | None = None
    version: int = 1
    created_by: str = "engine"
    modified_by: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    verified: bool = False

    @property
    def key(self) -> str:
        return normalize(self.name)


class Issue(Base):
    id: str = Field(default_factory=lambda: new_id("iss"))
    description: str
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    resolved: bool = False
    resolution_claimed_by: str | None = None
    raised_by: str = "engine"
    raised_at: datetime = Field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return normalize(self.description)


class FailedAttempt(Base):
    id: str = Field(default_factory=lambda: new_id("fail"))
    attempt: str
    reason: str = ""
    recorded_by: str = "engine"
    recorded_at: datetime = Field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return normalize(self.attempt)


class Assumption(Base):
    id: str = Field(default_factory=lambda: new_id("asm"))
    assumption: str
    reason: str = ""
    recorded_by: str = "engine"
    recorded_at: datetime = Field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return normalize(self.assumption)


class WorkerStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkerExecution(Base):
    """One worker's turn on the task, as recorded by the engine."""

    seq: int
    worker_id: str
    model: str
    assigned_task: str
    status: WorkerStatus = WorkerStatus.PENDING
    summary: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    duration_ms: int = 0
    error: str | None = None

    # Context-transfer audit: the whole point of the experiment.
    context_tokens_in: int = 0
    context_package_id: str | None = None
    raw_conversation_transferred: bool = False
    messages_sent: int = 0

    reconciliation_summary: dict[str, Any] = Field(default_factory=dict)


class HandoffRecord(Base):
    """A structured handoff from one worker to the next."""

    seq: int
    from_worker: str
    to_worker: str | None
    report: HandoffReport
    package_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    raw_conversation_transferred: bool = False


class ContextSection(Base):
    key: str
    title: str
    priority: int
    lines: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0
    truncated: bool = False


class HandoffPackage(Base):
    """The compiled context handed to the next worker. Never raw history."""

    package_id: str = Field(default_factory=lambda: new_id("pkg"))
    task_id: str
    target_worker_id: str
    assigned_task: str
    sections: list[ContextSection] = Field(default_factory=list)
    rendered_text: str = ""
    token_budget: int = 0
    estimated_tokens: int = 0
    included_sections: list[str] = Field(default_factory=list)
    omitted_sections: list[str] = Field(default_factory=list)
    dropped_items: list[str] = Field(default_factory=list)
    contains_raw_conversation: bool = False
    compiled_at: datetime = Field(default_factory=utcnow)


class WorkerConfig(Base):
    """Dynamic worker registration. No provider-specific subclassing, ever."""

    id: str
    model: str
    api_key_env: str | None = None
    api_key: str | None = None
    api_base: str | None = None
    provider_config: dict[str, Any] = Field(default_factory=dict)
    temperature: float = 0.2
    max_tokens: int = 2000
    max_retries: int = 2
    role: str | None = None
    enabled: bool = True


class Assignment(Base):
    """Binds a unit of work to whichever worker happens to be next."""

    seq: int
    worker_id: str
    task: str


class WorkerRun(Base):
    """Everything a single worker turn produced."""

    worker_id: str
    model: str
    assigned_task: str
    result: WorkerResult
    report: HandoffReport
    duration_ms: int = 0
    context_tokens_in: int = 0
    messages_sent: int = 0
    raw_conversation_transferred: bool = False
    error: str | None = None
