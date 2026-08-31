"""Turning one sentence into a plan, and a budget into shares of itself.

Sequential mode is the honest one: you write the steps and you set the limit,
and the engine does exactly what you said. One-shot mode exists because the
first question anyone asks the playground is "can I just tell it what I want",
and the answer should be yes.

So this module answers two questions:

* What are the steps? Asked of a model when there is a key to ask with, and
  answered from a template when there is not. Either way the result is
  editable, because a plan nobody may correct is not a plan, it is a guess.
* How much may each step be told? Not an equal split. The first worker starts
  from an empty record and has almost nothing to be given; the last one
  inherits everything decided before it. The share therefore grows down the
  plan, which is also the shape the compiler's own drop behaviour assumes.

Neither answer is authoritative and nothing downstream treats them as such.
The orchestrator is handed ordinary steps and ordinary budgets, and cannot
tell whether a person or a model wrote them.
"""

from __future__ import annotations

import re
from typing import Any

MIN_STEPS = 2
MAX_STEPS = 12
MIN_BUDGET = 250

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "role": {"type": "string"},
                },
                "required": ["task"],
            },
        }
    },
    "required": ["steps"],
}

PLANNER_SYSTEM = (
    "You break one objective into a sequence of steps for a team of workers who "
    "cannot talk to each other. Each worker sees a written record of what the "
    "workers before it decided, and nothing else - no conversation, no history. "
    "Write steps that survive that. Each step must be self-contained, must "
    "depend only on what earlier steps would have written down, and must be one "
    "unit of work rather than a whole phase. Give each step a short role such as "
    "architecture, data modelling, security, api design, implementation, review. "
    "Answer with JSON only."
)


def planner_messages(objective: str, count: int) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": PLANNER_SYSTEM},
        {
            "role": "user",
            "content": (
                f"OBJECTIVE\n\n{objective.strip()}\n\n"
                f"Break this into exactly {count} steps, in the order they must happen. "
                f"The last step must review the whole thing for gaps and contradictions.\n\n"
                'Answer as {"steps": [{"task": "...", "role": "..."}]}'
            ),
        },
    ]


# --------------------------------------------------------------------------
# The offline planner
# --------------------------------------------------------------------------
#
# Used when there is no key, and as the fallback when the model planner
# returns something unusable. It reads the objective for the kind of work
# being asked for and fills the matching shape. It is a template, and the UI
# says so rather than passing it off as thinking.

_SHAPES: list[tuple[re.Pattern, str, list[tuple[str, str]]]] = [
    (
        re.compile(r"\b(api|backend|service|endpoint|rest|graphql|microservice|server)\b", re.I),
        "a backend",
        [
            ("architecture", "Set out what {what} has to do, and the overall shape of the system."),
            ("data modelling", "Design the data model and how it is stored."),
            ("security", "Design how people sign in and what each of them is allowed to do."),
            ("api design", "Design the endpoints, their inputs, and what they return."),
            ("implementation", "Work out the order things must be built in, and what blocks what."),
            ("review", "Review the whole design for gaps, contradictions and anything assumed but never decided."),
        ],
    ),
    (
        re.compile(r"\b(app|frontend|ui|interface|website|dashboard|mobile)\b", re.I),
        "the product",
        [
            ("architecture", "Set out who uses {what}, what they are trying to do, and the screens that implies."),
            ("api design", "Decide what data each screen needs and where it comes from."),
            ("implementation", "Design the components and how state moves between them."),
            ("security", "Work out sign-in, permissions and what happens when something fails."),
            ("review", "Review the whole thing for gaps and contradictions."),
        ],
    ),
    (
        re.compile(r"\b(research|compare|investigate|evaluate|survey|analy[sz]e|report|study)\b", re.I),
        "the question",
        [
            ("planning", "Set out exactly what {what} is asking, and what an answer would have to contain."),
            ("drafting", "Gather what is already known and write it down as findings, with sources."),
            ("data modelling", "Sort the findings into themes and note where they disagree."),
            ("implementation", "Draw conclusions from the findings, and say which ones the evidence does not support."),
            ("review", "Review the conclusions against the findings and flag anything overstated."),
        ],
    ),
    (
        re.compile(r"\b(write|article|essay|book|blog|copy|documentation|guide|course)\b", re.I),
        "the piece",
        [
            ("planning", "Decide who {what} is for, what it must leave them with, and the outline."),
            ("drafting", "Write the opening section against that outline."),
            ("implementation", "Write the body, continuing from what the opening established."),
            ("summary", "Write the closing section and tie it back to the opening."),
            ("review", "Read the whole thing for repetition, contradiction and anything promised but never delivered."),
        ],
    ),
]

_GENERIC = [
    ("planning", "Set out what {what} actually requires, and what finished looks like."),
    ("architecture", "Design the overall approach and record the decisions it rests on."),
    ("implementation", "Work through the substance of the task, continuing from the record."),
    ("data modelling", "Work through what the previous step left unfinished."),
    ("review", "Review everything decided so far for gaps and contradictions."),
]

_STOP = re.compile(
    r"^(build|make|create|design|write|plan|develop|produce|draft|set up|implement"
    r"|research|investigate|compare|evaluate|analy[sz]e|figure out|work out|help me)\s+"
    r"(me\s+|us\s+)?",
    re.I,
)


_DANGLES = re.compile(r"^(whether|if|how|why|what|when|where|which)\b", re.I)


def _subject(objective: str) -> str:
    """The objective with its imperative stripped, for use mid-sentence."""
    text = " ".join(objective.strip().split())
    stripped = _STOP.sub("", text).strip(" .")
    # "Research whether X" loses its verb and leaves a dangling clause, so keep
    # the whole sentence when what remains cannot stand as a noun phrase.
    if not _DANGLES.match(stripped):
        text = stripped
    text = text.strip(" .")
    if not text:
        return "this"
    words = text.split()
    text = " ".join(words[:12]) if len(words) > 12 else text
    # It is about to appear mid-sentence, so an ordinary word should not keep
    # the capital it only had for being first. An acronym keeps its own.
    first = text.split(" ", 1)[0]
    if first[1:].islower():
        text = text[0].lower() + text[1:]
    return text


def heuristic_plan(objective: str, count: int) -> list[dict[str, str]]:
    """A plan written from a template, with no model involved."""
    count = max(MIN_STEPS, min(MAX_STEPS, count))
    what = _subject(objective)
    shape = _GENERIC
    for pattern, _, steps in _SHAPES:
        if pattern.search(objective):
            shape = steps
            break

    # Always keep the first step and the review; stretch or squeeze the middle.
    head, review = shape[0], shape[-1]
    middle = shape[1:-1]
    if count <= 2:
        chosen = [head, review]
    elif count - 2 <= len(middle):
        chosen = [head] + middle[: count - 2] + [review]
    else:
        chosen = [head] + middle
        while len(chosen) < count - 1:
            chosen.append(
                (
                    "implementation",
                    "Carry the next unfinished part of {what} through to a decision.",
                )
            )
        chosen.append(review)

    out = []
    for i, (role, template) in enumerate(chosen):
        task = template.format(what=what)
        if i:
            task = "Continue from the record and " + task[0].lower() + task[1:]
        out.append({"task": task, "role": role})
    return out


# --------------------------------------------------------------------------
# Reading a model's answer
# --------------------------------------------------------------------------


def parse_plan(payload: Any, count: int) -> list[dict[str, str]]:
    """Pull steps out of whatever the planner model actually returned.

    Same posture as everywhere else in the engine: the model's answer is a
    claim about a plan, not a plan. Anything unusable is dropped rather than
    argued with, and the caller decides whether what survives is enough.
    """
    rows = payload.get("steps") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows[:MAX_STEPS]:
        if isinstance(row, str):
            task, role = row, ""
        elif isinstance(row, dict):
            task = str(row.get("task") or row.get("step") or row.get("description") or "")
            role = str(row.get("role") or "")
        else:
            continue
        task = " ".join(task.split())
        if len(task) < 8:
            continue
        out.append({"task": task, "role": " ".join(role.split()).lower()[:40]})
    return out[:count]


# --------------------------------------------------------------------------
# Dividing the budget
# --------------------------------------------------------------------------


def split_budget(total: int, steps: int, minimum: int = MIN_BUDGET) -> list[int]:
    """Share a total across steps, giving later steps more.

    The first worker is briefed on an empty record and cannot use a large
    allowance; the last inherits every decision before it and can. Weighting
    the shares that way spends the total where it buys something, instead of
    handing worker one an allowance it has nothing to put in.

    The shares are what each worker may be *told*. They are a ceiling, not a
    cost: a briefing that comes in under its share simply comes in under it.
    """
    if steps <= 0:
        return []
    if steps == 1:
        return [max(minimum, total)]

    weights = [1.0 + (i / (steps - 1)) for i in range(steps)]  # 1.0 -> 2.0
    scale = total / sum(weights)
    shares = [max(minimum, int(round(w * scale / 50.0)) * 50) for w in weights]

    # Rounding and the floor can push the total up; take the difference back
    # off the largest shares, which are the ones that can afford it.
    over = sum(shares) - total
    i = len(shares) - 1
    while over > 0 and i >= 0:
        take = min(over, shares[i] - minimum)
        if take > 0:
            shares[i] -= take
            over -= take
        i -= 1
    return shares
