"""One-shot mode: turning a sentence into a plan, and a total into shares.

The plan itself is a suggestion and the tests treat it as one. What has to
hold is the shape: the right number of steps, a review at the end, every step
readable, and a budget division that adds up to what was asked for and never
starves a worker.
"""

from __future__ import annotations

import pytest

from context_orchestration.core import planner


# -- the offline planner ---------------------------------------------------


@pytest.mark.parametrize("count", [2, 3, 5, 7, 12])
def test_a_plan_has_exactly_the_steps_asked_for(count):
    steps = planner.heuristic_plan("Build a booking backend with FastAPI", count)
    assert len(steps) == count
    assert all(len(s["task"]) > 20 for s in steps)


def test_a_plan_always_ends_by_reviewing_the_whole_thing():
    for objective in [
        "Build a booking backend with FastAPI",
        "Write a guide to sourdough",
        "Research whether we should move off Postgres",
        "Reorganise the warehouse rota",
    ]:
        assert planner.heuristic_plan(objective, 5)[-1]["role"] == "review"


def test_every_step_after_the_first_continues_from_the_record():
    """No step may assume it saw the conversation, because none of them did."""
    steps = planner.heuristic_plan("Build a booking backend with FastAPI", 5)
    assert not steps[0]["task"].startswith("Continue")
    assert all(s["task"].startswith("Continue from the record") for s in steps[1:])


def test_the_prefix_is_never_doubled_when_the_plan_is_stretched():
    steps = planner.heuristic_plan("Research whether to move off Postgres", 9)
    assert all(s["task"].count("Continue from the record") <= 1 for s in steps)


def test_the_shape_follows_the_kind_of_work_asked_for():
    roles = lambda o: [s["role"] for s in planner.heuristic_plan(o, 5)]  # noqa: E731
    assert "security" in roles("Build a REST API for invoices")
    assert "drafting" in roles("Write a blog post about caching")


def test_the_objective_is_quoted_back_readably():
    task = planner.heuristic_plan("Build me a booking backend with FastAPI", 3)[0]["task"]
    assert "a booking backend with FastAPI" in task
    # The imperative it was given is stripped, not repeated mid-sentence.
    assert "Build me" not in task


def test_a_step_count_outside_the_range_is_clamped_not_rejected():
    assert len(planner.heuristic_plan("Do a thing", 0)) == planner.MIN_STEPS
    assert len(planner.heuristic_plan("Do a thing", 99)) == planner.MAX_STEPS


def test_an_objective_that_is_only_a_verb_still_plans():
    assert planner.heuristic_plan("build", 3)


# -- reading a model's answer ----------------------------------------------


def test_a_well_formed_plan_is_taken_as_written():
    steps = planner.parse_plan(
        {"steps": [{"task": "Design the schema for the API", "role": "Data Modelling"}]}, 5
    )
    assert steps == [{"task": "Design the schema for the API", "role": "data modelling"}]


def test_a_bare_list_of_strings_is_accepted():
    steps = planner.parse_plan(["Design the schema for the API"], 5)
    assert steps[0]["task"] == "Design the schema for the API"


def test_unusable_rows_are_dropped_rather_than_argued_with():
    steps = planner.parse_plan(
        {"steps": ["ok", 7, None, {"task": "Design the schema for the API"}, {}]}, 5
    )
    assert [s["task"] for s in steps] == ["Design the schema for the API"]


def test_a_planner_that_overruns_is_trimmed():
    payload = {"steps": [{"task": f"Do the {i}th part of the work"} for i in range(30)]}
    assert len(planner.parse_plan(payload, 4)) == 4


def test_nonsense_yields_no_plan_so_the_caller_can_fall_back():
    assert planner.parse_plan("not a plan", 5) == []
    assert planner.parse_plan({"nope": 1}, 5) == []


# -- dividing the budget ---------------------------------------------------


@pytest.mark.parametrize("total, steps", [(9000, 5), (4000, 3), (20000, 8), (1500, 2)])
def test_the_shares_add_up_to_the_total(total, steps):
    assert sum(planner.split_budget(total, steps)) == total


def test_later_workers_get_more_because_they_inherit_more():
    shares = planner.split_budget(9000, 5)
    assert shares == sorted(shares)
    assert shares[-1] > shares[0]


def test_no_worker_is_starved_even_when_the_total_is_too_small():
    shares = planner.split_budget(100, 5)
    assert all(s >= planner.MIN_BUDGET for s in shares)


def test_a_single_step_gets_the_lot():
    assert planner.split_budget(9000, 1) == [9000]


def test_no_steps_means_no_shares():
    assert planner.split_budget(9000, 0) == []


# -- the prompt ------------------------------------------------------------


def test_the_planning_prompt_states_the_constraint_the_plan_must_satisfy():
    messages = planner.planner_messages("Build a booking backend", 5)
    system = messages[0]["content"]
    # A plan whose steps assume a shared conversation cannot be executed by
    # this engine, so the planner is told that up front.
    assert "cannot talk to each other" in system
    assert "no conversation" in system
    assert "exactly 5 steps" in messages[1]["content"]
