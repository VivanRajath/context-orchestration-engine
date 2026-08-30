"""The Context Compiler.

Canonical state is large and grows forever. A prompt is small and must not.
This module is the bridge: it turns the full execution state plus the next
assigned task into a bounded, prioritized ``HandoffPackage``.

Two rules define the design:

1. **It never dumps state.** Items are scored for relevance against the task
   being assigned, and low-value items are dropped before high-value ones.
2. **It never includes conversation.** There is no message history here to
   include - the compiler only ever reads structured state.

Priority order (highest first) follows the engine spec: assigned task,
objective, current progress, current task state, decisions, completed work,
artifacts, open issues, failed attempts, assumptions, last action, next action,
notes from the previous worker.
"""

from __future__ import annotations

import re
from typing import Protocol, Sequence

from context_orchestration.context.state import ExecutionState
from context_orchestration.core.contracts import (
    Artifact,
    Assumption,
    ContextSection,
    Decision,
    FailedAttempt,
    HandoffPackage,
    HandoffRecord,
    Issue,
)

# --------------------------------------------------------------------------
# Token estimation
# --------------------------------------------------------------------------


class TokenEstimator(Protocol):
    """Swap in a real tokenizer later without touching the compiler."""

    def count(self, text: str) -> int: ...


class HeuristicTokenEstimator:
    """Character/word blend. Good enough for budgeting, cheap, dependency-free.

    Roughly tracks BPE behaviour: ~4 chars per token for prose, with a floor of
    one token per whitespace-separated word so short-word text is not
    under-counted.
    """

    def count(self, text: str) -> int:
        if not text:
            return 0
        words = len(text.split())
        return max(words, (len(text) + 3) // 4)


class TiktokenEstimator:
    """Optional exact-ish counting when ``tiktoken`` is installed."""

    def __init__(self, encoding: str = "cl100k_base") -> None:
        import tiktoken  # imported lazily so it stays an optional dependency

        self._enc = tiktoken.get_encoding(encoding)

    def count(self, text: str) -> int:
        return len(self._enc.encode(text or ""))


def default_estimator() -> TokenEstimator:
    try:
        return TiktokenEstimator()
    except Exception:
        return HeuristicTokenEstimator()


# --------------------------------------------------------------------------
# Relevance scoring
# --------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "is",
    "are", "be", "was", "were", "it", "this", "that", "as", "at", "by", "from",
    "using", "use", "design", "create", "build", "should", "will", "must",
    "task", "work", "then", "than", "into", "not", "no", "all", "any", "its",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS and len(w) > 2}


def relevance(text: str, focus_terms: set[str]) -> float:
    """Overlap between an item and the task focus, in [0, 1]."""
    if not focus_terms:
        return 0.0
    item_terms = _terms(text)
    if not item_terms:
        return 0.0
    return len(item_terms & focus_terms) / len(focus_terms)


# --------------------------------------------------------------------------
# Compiler
# --------------------------------------------------------------------------


class ContextCompiler:
    """Builds a bounded handoff package from canonical state."""

    def __init__(
        self,
        token_budget: int = 1600,
        estimator: TokenEstimator | None = None,
        max_completed: int = 8,
        max_decisions: int = 12,
        max_artifacts: int = 12,
        max_issues: int = 8,
        max_failed: int = 8,
        max_assumptions: int = 8,
        anchor_items: int = 3,
    ) -> None:
        self.token_budget = token_budget
        self.estimator = estimator or default_estimator()
        self.max_completed = max_completed
        self.max_decisions = max_decisions
        self.max_artifacts = max_artifacts
        self.max_issues = max_issues
        self.max_failed = max_failed
        self.max_assumptions = max_assumptions
        # How many of the earliest decisions / failed attempts always survive.
        self.anchor_items = anchor_items

    # -- item selection ------------------------------------------------

    def _rank(
        self,
        items: Sequence,
        text_of,
        focus: set[str],
        limit: int,
        recency_weight: float = 0.35,
        anchors: int = 0,
    ):
        """Score by relevance, tie-break toward recent entries, keep the top N.

        ``anchors`` reserves slots for the *earliest* entries. Foundational
        decisions ("use FastAPI", "use UUID primary keys") constrain every
        later piece of work, so a pure relevance-plus-recency ranking will
        happily drop them in favour of recent detail - which is how a late
        worker ends up reviewing an architecture whose framework choice it
        cannot see. Anchoring prevents that.

        Returns ``(kept, dropped_labels)`` with ``kept`` restored to the
        original chronological order so the reader sees a coherent narrative.
        """
        if not items:
            return [], []
        n = len(items)
        anchor_idx = set(range(min(anchors, n, limit)))

        scored = []
        for idx, item in enumerate(items):
            if idx in anchor_idx:
                continue
            rec = (idx + 1) / n
            scored.append((relevance(text_of(item), focus) + recency_weight * rec, idx, item))
        scored.sort(key=lambda s: (-s[0], -s[1]))

        keep_idx = anchor_idx | {idx for _, idx, _ in scored[: max(limit - len(anchor_idx), 0)]}
        kept = [item for idx, item in enumerate(items) if idx in keep_idx]
        dropped = [text_of(item) for idx, item in enumerate(items) if idx not in keep_idx]
        return kept, dropped

    def _focus(self, state: ExecutionState, assigned_task: str) -> set[str]:
        return _terms(assigned_task) | _terms(state.objective)

    # -- section builders ----------------------------------------------

    def _sections(
        self, state: ExecutionState, assigned_task: str, dropped: list[str]
    ) -> list[ContextSection]:
        focus = self._focus(state, assigned_task)
        sections: list[ContextSection] = []

        def add(key: str, title: str, priority: int, lines: list[str]) -> None:
            lines = [ln for ln in lines if ln and ln.strip()]
            if not lines:
                return
            sections.append(ContextSection(key=key, title=title, priority=priority, lines=lines))

        add("assigned_task", "YOUR ASSIGNED TASK", 1, [assigned_task])
        add("objective", "TASK OBJECTIVE", 2, [state.objective])
        add("current_progress", "CURRENT PROGRESS", 3, [state.current_progress])
        add("current_task", "CURRENT TASK STATE", 4, [state.current_task])

        decisions, drop = self._rank(
            state.decisions,
            lambda d: f"{d.decision} {d.reason}",
            focus,
            self.max_decisions,
            anchors=self.anchor_items,
        )
        dropped += [f"decision: {d}" for d in drop]
        add(
            "decisions",
            "IMPORTANT DECISIONS",
            5,
            [self._decision_line(d) for d in decisions],
        )

        completed, drop = self._rank(state.completed_tasks, lambda t: t, focus, self.max_completed)
        dropped += [f"completed: {d}" for d in drop]
        add("completed_work", "COMPLETED WORK", 6, list(completed))

        artifacts, drop = self._rank(
            state.artifacts, lambda a: f"{a.name} {a.description}", focus, self.max_artifacts
        )
        dropped += [f"artifact: {d}" for d in drop]
        add("artifacts", "ARTIFACTS", 7, [self._artifact_line(a) for a in artifacts])

        open_issues = state.open_issues
        issues, drop = self._rank(open_issues, lambda i: i.description, focus, self.max_issues)
        dropped += [f"issue: {d}" for d in drop]
        add(
            "issues",
            "UNRESOLVED ISSUES",
            8,
            [self._issue_line(i) for i in issues] or (["No unresolved issues."] if not open_issues else []),
        )

        failed, drop = self._rank(
            state.failed_attempts,
            lambda f: f"{f.attempt} {f.reason}",
            focus,
            self.max_failed,
            anchors=self.anchor_items,
        )
        dropped += [f"failed_attempt: {d}" for d in drop]
        add(
            "failed_attempts",
            "FAILED ATTEMPTS (DO NOT REPEAT)",
            9,
            [self._failed_line(f) for f in failed] or (["None."] if not state.failed_attempts else []),
        )

        assumptions, drop = self._rank(
            state.assumptions, lambda a: a.assumption, focus, self.max_assumptions
        )
        dropped += [f"assumption: {d}" for d in drop]
        add("assumptions", "ASSUMPTIONS", 10, [self._assumption_line(a) for a in assumptions])

        add("last_action", "LAST ACTION", 11, [state.last_action])
        add("next_action", "RECOMMENDED NEXT ACTION", 12, [state.next_action])

        handoff = state.last_handoff()
        if handoff is not None:
            add(
                "previous_worker_notes",
                f"NOTES FROM PREVIOUS WORKER ({handoff.from_worker})",
                13,
                self._notes_lines(handoff),
            )

        return sections

    @staticmethod
    def _decision_line(d: Decision) -> str:
        base = d.decision if d.decision.endswith(".") else d.decision + "."
        return f"{base} Reason: {d.reason}" if d.reason else base

    @staticmethod
    def _artifact_line(a: Artifact) -> str:
        parts = [a.name]
        if a.description:
            parts.append(f"- {a.description}")
        flags = []
        if a.version > 1:
            flags.append(f"v{a.version}")
        if not a.verified:
            flags.append("unverified")
        if flags:
            parts.append(f"[{', '.join(flags)}]")
        return " ".join(parts)

    @staticmethod
    def _issue_line(i: Issue) -> str:
        line = f"[{i.severity}] {i.description}"
        if i.resolution_claimed_by:
            line += f" (resolution claimed by {i.resolution_claimed_by}, unverified)"
        return line

    @staticmethod
    def _failed_line(f: FailedAttempt) -> str:
        return f"{f.attempt} - {f.reason}" if f.reason else f.attempt

    @staticmethod
    def _assumption_line(a: Assumption) -> str:
        return f"{a.assumption} ({a.reason})" if a.reason else a.assumption

    @staticmethod
    def _notes_lines(handoff: HandoffRecord) -> list[str]:
        lines: list[str] = []
        if handoff.report.notes_for_next_worker:
            lines.append(handoff.report.notes_for_next_worker)
        if handoff.report.current_state:
            lines.append(f"State at handoff: {handoff.report.current_state}")
        return lines

    # -- budget enforcement --------------------------------------------

    ALWAYS_KEEP = ("assigned_task", "objective")

    def compile(
        self,
        state: ExecutionState,
        assigned_task: str,
        target_worker_id: str,
        token_budget: int | None = None,
    ) -> HandoffPackage:
        budget = token_budget or self.token_budget
        dropped: list[str] = []
        sections = self._sections(state, assigned_task, dropped)

        for section in sections:
            section.estimated_tokens = self.estimator.count(self._render_section(section))

        included: list[ContextSection] = []
        omitted: list[str] = []

        # Cost is always measured against the *rendered* package, never as a sum
        # of per-section estimates - separators and headers are real tokens too.
        for section in sorted(sections, key=lambda s: s.priority):
            if self._cost(included + [section]) <= budget:
                included.append(section)
                continue

            if section.key in self.ALWAYS_KEEP:
                # Required context: truncate rather than drop.
                self._truncate(section, budget, self._cost(included))
                included.append(section)
                continue

            trimmed = self._trim_lines(included, section, budget)
            if trimmed is not None:
                included.append(trimmed)
                dropped += [f"{section.key}: {ln}" for ln in section.lines[len(trimmed.lines) :]]
                continue

            omitted.append(section.key)
            dropped += [f"{section.key}: {ln}" for ln in section.lines]

        included.sort(key=lambda s: s.priority)
        rendered = self._render(included)

        return HandoffPackage(
            task_id=state.task_id,
            target_worker_id=target_worker_id,
            assigned_task=assigned_task,
            sections=included,
            rendered_text=rendered,
            token_budget=budget,
            estimated_tokens=self.estimator.count(rendered),
            included_sections=[s.key for s in included],
            omitted_sections=omitted,
            dropped_items=dropped,
            contains_raw_conversation=False,
        )

    def _cost(self, sections: list[ContextSection]) -> int:
        return self.estimator.count(self._render(sections))

    def _render(self, sections: list[ContextSection]) -> str:
        return "\n\n".join(self._render_section(s) for s in sections)

    def _trim_lines(
        self, included: list[ContextSection], section: ContextSection, budget: int
    ) -> ContextSection | None:
        """Keep as many leading lines as fit; return None if even one does not."""
        kept: list[str] = []
        for line in section.lines:
            candidate = ContextSection(
                key=section.key,
                title=section.title,
                priority=section.priority,
                lines=kept + [line],
                truncated=True,
            )
            if self._cost(included + [candidate]) > budget:
                break
            kept.append(line)
        if not kept:
            return None
        trimmed = ContextSection(
            key=section.key,
            title=section.title,
            priority=section.priority,
            lines=kept,
            truncated=len(kept) < len(section.lines),
        )
        trimmed.estimated_tokens = self.estimator.count(self._render_section(trimmed))
        return trimmed

    def _truncate(self, section: ContextSection, budget: int, already_used: int) -> None:
        """Shrink a required section's text until the whole package fits."""
        original = "\n".join(section.lines)
        text = original
        while text:
            section.lines = [text.rstrip() + (" ..." if text != original else "")]
            section.truncated = text != original
            if already_used + self.estimator.count(self._render_section(section)) <= budget:
                break
            if len(text) <= 24:
                break
            text = text[: int(len(text) * 0.8)]
        section.estimated_tokens = self.estimator.count(self._render_section(section))

    @staticmethod
    def _render_section(section: ContextSection) -> str:
        body = "\n".join(section.lines)
        suffix = "\n(truncated to fit token budget)" if section.truncated else ""
        return f"{section.title}\n\n{body}{suffix}"
