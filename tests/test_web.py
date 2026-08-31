"""The web playground, including the shape a serverless deployment needs.

The engine is not exercised here beyond one mock run - the other files do that.
What matters is the transport: that a run started and watched by two requests
and a run streamed inside one produce the same event sequence, and that a
deployment which has closed live mode actually refuses one.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # what fastapi.testclient runs on
from fastapi.testclient import TestClient  # noqa: E402

from context_orchestration.web.server import create_app  # noqa: E402

OBJECTIVE = "Build a Task Management REST API."
PLAN = ["Define requirements.", "Design the schema.", "Review it."]


def frames(text: str) -> list[dict]:
    """Parse an SSE body into the events it carried, keepalives dropped."""
    out = []
    for block in text.split("\n\n"):
        payload = "".join(
            line[len("data:"):].strip()
            for line in block.split("\n")
            if line.startswith("data:")
        )
        if payload:
            out.append(json.loads(payload))
    return out


@pytest.fixture
def local(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.delenv("COE_SERVERLESS", raising=False)
    monkeypatch.delenv("COE_ALLOW_LIVE", raising=False)
    monkeypatch.setenv("COE_NO_POOL", "1")
    return TestClient(create_app(db=str(tmp_path / "local.db")))


@pytest.fixture
def hosted(tmp_path, monkeypatch) -> TestClient:
    """A public deployment carrying no keys of its own."""
    monkeypatch.setenv("COE_SERVERLESS", "1")
    monkeypatch.delenv("COE_ALLOW_LIVE", raising=False)
    # create_app loads .env, so the pool has to be closed by flag rather than
    # by unsetting variables the loader would put straight back.
    monkeypatch.setenv("COE_NO_POOL", "1")
    return TestClient(create_app(db=str(tmp_path / "hosted.db")))


@pytest.fixture
def lending(tmp_path, monkeypatch) -> TestClient:
    """A public deployment that lends visitors a key of its own."""
    monkeypatch.setenv("COE_SERVERLESS", "1")
    monkeypatch.delenv("COE_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("COE_NO_POOL", raising=False)
    monkeypatch.setenv("COE_DEMO_KEYS", "gsk_one, gsk_two, gsk_three")
    return TestClient(create_app(db=str(tmp_path / "lending.db")))


RUN = {"objective": OBJECTIVE, "plan": PLAN, "mock": True, "budget": 1600}


# -- configuration ---------------------------------------------------------


def test_local_config_offers_live_mode(local):
    cfg = local.get("/api/config").json()
    assert cfg["serverless"] is False
    assert cfg["live_enabled"] is True
    assert cfg["live_disabled_reason"] is None
    assert cfg["workers"] and cfg["demo"]["plan"]


def test_serverless_config_closes_live_mode_with_no_pool(hosted):
    cfg = hosted.get("/api/config").json()
    assert cfg["serverless"] is True
    assert cfg["live_enabled"] is False
    assert cfg["can_run_live"] is False
    assert cfg["live_disabled_reason"]
    assert cfg["pool"]["available"] is False


def test_a_lent_key_opens_live_mode_without_exposing_itself(lending):
    cfg = lending.get("/api/config").json()
    assert cfg["live_enabled"] is True
    assert cfg["pool"] == {
        "available": True,
        "count": 3,
        "provider": "groq",
        "label": "Groq",
        "free_tier": True,
    }
    # The whole config document, not just the pool block: a lent key must not
    # reach the browser by any route.
    assert "gsk_one" not in json.dumps(cfg)


def test_allow_live_reopens_it(tmp_path, monkeypatch):
    monkeypatch.setenv("COE_SERVERLESS", "1")
    monkeypatch.setenv("COE_ALLOW_LIVE", "1")
    monkeypatch.setenv("COE_NO_POOL", "1")
    client = TestClient(create_app(db=str(tmp_path / "private.db")))
    cfg = client.get("/api/config").json()
    assert cfg["serverless"] is True and cfg["live_enabled"] is True


# -- the local two-request path --------------------------------------------


def test_start_then_stream(local):
    started = local.post("/api/runs", json=RUN)
    assert started.status_code == 200
    task_id = started.json()["task_id"]
    assert started.json()["mock"] is True

    events = frames(local.get(f"/api/runs/{task_id}/stream").text)
    assert events[0]["event"] == "run_started"
    assert events[-1]["event"] == "stream_end"
    assert [e for e in events if e["event"] == "run_finished"]

    stored = local.get(f"/api/tasks/{task_id}").json()
    assert stored["state"]["status"] == "completed"
    assert stored["packages"] and stored["handoffs"]


def test_stream_of_an_unknown_run_is_404(local):
    assert local.get("/api/runs/task-nope/stream").status_code == 404


# -- the single-request path -----------------------------------------------


def test_one_request_starts_and_streams(hosted):
    r = hosted.post("/api/runs/stream", json=RUN)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    events = frames(r.text)
    assert events[0]["event"] == "run_started"
    assert events[0]["task_id"]
    assert events[-1]["event"] == "stream_end"

    finished = [e for e in events if e["event"] == "run_finished"]
    assert len(finished) == 1
    # The whole point of the engine, asserted over the wire.
    assert finished[0]["summary"]["raw_conversation_transfers"] == 0
    assert finished[0]["continuity"] is True


def test_both_paths_emit_the_same_events(local, hosted):
    one = [e["event"] for e in frames(hosted.post("/api/runs/stream", json=RUN).text)]
    task_id = local.post("/api/runs", json=RUN).json()["task_id"]
    two = [e["event"] for e in frames(local.get(f"/api/runs/{task_id}/stream").text)]
    assert one == two


def test_a_run_persists_within_its_own_request(hosted):
    events = frames(hosted.post("/api/runs/stream", json=RUN).text)
    task_id = events[0]["task_id"]
    stored = hosted.get(f"/api/tasks/{task_id}").json()
    assert stored["state"]["status"] == "completed"
    assert len(stored["handoffs"]) == len(
        [e for e in events if e["event"] == "worker_completed"]
    )


# -- what a closed deployment refuses --------------------------------------


def test_live_run_refused_when_live_is_closed(hosted):
    for path in ("/api/runs", "/api/runs/stream"):
        r = hosted.post(path, json={**RUN, "mock": False})
        assert r.status_code == 403, path
        assert "switched off" in r.json()["detail"]


def test_credential_probe_refused_when_live_is_closed(hosted):
    r = hosted.post("/api/credentials/test", json={"credentials": {"*": "sk-whatever"}})
    assert r.status_code == 403


def test_a_pasted_key_cannot_reopen_live_mode(hosted):
    r = hosted.post("/api/runs/stream", json={**RUN, "mock": False, "credentials": {"*": "sk-x"}})
    assert r.status_code == 403


# -- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "bad, detail",
    [
        ({"plan": []}, "step"),
        ({"plan": ["   "]}, "step"),
        ({"objective": "  "}, "what you want done"),
    ],
)
def test_bad_requests_are_rejected(hosted, bad, detail):
    r = hosted.post("/api/runs/stream", json={**RUN, **bad})
    assert r.status_code == 400
    assert detail in r.json()["detail"]


# -- inspecting a key ------------------------------------------------------


@pytest.fixture
def catalogue(monkeypatch):
    """Stand in for the vendor's model list, so no test touches the network."""
    from context_orchestration.gateway import providers

    models = [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "whisper-large-v3",
    ]
    monkeypatch.setattr(providers, "list_models", lambda *a, **k: list(models))
    return models


def test_a_pasted_key_comes_back_with_the_models_it_can_run(local, catalogue):
    r = local.post(
        "/api/keys/inspect",
        json={"key": "gsk_pasted", "roles": ["architecture", "review"]},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["provider"] == "groq"
    assert body["provider_label"] == "Groq"
    assert body["detected"] is True
    assert body["source"] == "yours"
    # Filtered to what a worker could actually be assigned, and each model
    # carries the facts the dropdown needs to describe it.
    offered = [m["id"] for m in body["models"]]
    assert "whisper-large-v3" not in offered
    assert body["recommended"] in offered
    assert all("note" in m and "structured" in m for m in body["models"])
    # One model per step rather than the same one twice.
    assert [s["role"] for s in body["suggested"]] == ["architecture", "review"]
    assert len({s["model"] for s in body["suggested"]}) == 2


def test_inspecting_a_lent_key_never_reveals_it(lending, catalogue):
    body = lending.post("/api/keys/inspect", json={"use_pool": True}).json()
    assert body["ok"] is True
    assert body["source"] == "pool"
    assert body["pool_count"] == 3
    assert "gsk_one" not in json.dumps(body)


def test_borrowing_where_live_is_closed_is_refused(hosted):
    assert hosted.post("/api/keys/inspect", json={"use_pool": True}).status_code == 403


def test_an_empty_key_is_refused_before_any_provider_is_called(local):
    assert local.post("/api/keys/inspect", json={"key": "   "}).status_code == 400


def test_a_rejected_key_is_reported_with_the_vendors_reason(local, monkeypatch):
    from context_orchestration.gateway import providers

    def refuse(*_a, **_k):
        raise providers.ProviderError("the provider rejected this key: bad token")

    monkeypatch.setattr(providers, "list_models", refuse)
    body = local.post("/api/keys/inspect", json={"key": "gsk_bad"}).json()
    assert body["ok"] is False
    assert "rejected" in body["error"]
    assert body["suggested"] == []


# -- one-shot planning -----------------------------------------------------


def test_one_shot_splits_an_objective_and_its_budget(local):
    body = local.post(
        "/api/plan/split",
        json={
            "objective": "Build me a booking backend with FastAPI",
            "steps": 5,
            "total_budget": 9000,
            "mock": True,
        },
    ).json()
    assert body["planner"] == "template"
    assert len(body["steps"]) == 5
    assert body["total_budget"] == 9000
    assert sum(s["budget"] for s in body["steps"]) == 9000
    # Later steps inherit more, so they are allowed to be told more.
    budgets = [s["budget"] for s in body["steps"]]
    assert budgets == sorted(budgets)
    assert body["steps"][-1]["role"] == "review"


def test_planning_needs_something_to_plan(local):
    assert local.post("/api/plan/split", json={"objective": "   "}).status_code == 400


def test_a_model_written_plan_says_it_was_written_by_a_model(local, monkeypatch):
    from context_orchestration.gateway.llm_gateway import GatewayResponse

    plan = {
        "steps": [
            {"task": "Design the booking data model", "role": "data modelling"},
            {"task": "Review the whole design", "role": "review"},
        ]
    }

    class Planner:
        def complete(self, config, messages, json_schema=None):
            assert json_schema is not None  # it is asked for a schema
            return GatewayResponse(text=json.dumps(plan), model=config.model)

    monkeypatch.setattr("context_orchestration.web.server.live_gateway", Planner)
    body = local.post(
        "/api/plan/split",
        json={
            "objective": "Build a booking backend",
            "steps": 2,
            "total_budget": 4000,
            "mock": False,
            "key": "gsk_x",
            "model": "llama-3.3-70b-versatile",
        },
    ).json()
    assert body["planner"] == "model"
    assert body["model"] == "groq/llama-3.3-70b-versatile"
    assert [s["task"] for s in body["steps"]] == [s["task"] for s in plan["steps"]]


def test_a_planner_that_fails_falls_back_and_says_so(local, monkeypatch):
    from context_orchestration.gateway.llm_gateway import GatewayError

    class Broken:
        def complete(self, *_a, **_k):
            raise GatewayError("model call failed for planner (x): out of credit")

    monkeypatch.setattr("context_orchestration.web.server.live_gateway", Broken)
    body = local.post(
        "/api/plan/split",
        json={
            "objective": "Build a booking backend",
            "steps": 3,
            "mock": False,
            "key": "gsk_x",
            "model": "llama-3.3-70b-versatile",
        },
    ).json()
    assert body["planner"] == "template"
    assert "out of credit" in body["note"]
    assert len(body["steps"]) == 3


# -- a roster assembled in the browser -------------------------------------


ROSTER = [
    {"id": "w-1", "provider": "groq", "model": "llama-3.3-70b-versatile", "key_ref": "k1"},
    {"id": "w-2", "provider": "anthropic", "model": "claude-sonnet-5", "key_ref": "k2"},
    {"id": "w-3", "provider": "cerebras", "model": "gpt-oss-120b", "key_ref": "k3"},
]


def test_a_run_can_use_workers_defined_in_the_page(hosted):
    """Three vendors, three keys, one continuous task."""
    events = frames(
        hosted.post(
            "/api/runs/stream",
            json={
                **RUN,
                "workers": ROSTER,
                "keys": [
                    {"id": "k1", "key": "gsk_a"},
                    {"id": "k2", "key": "sk-ant-b"},
                    {"id": "k3", "key": "csk-c"},
                ],
            },
        ).text
    )
    roster = events[0]["roster"]
    assert [r["worker_id"] for r in roster] == ["w-1", "w-2", "w-3"]
    assert [r["model"] for r in roster] == [
        "groq/llama-3.3-70b-versatile",
        "anthropic/claude-sonnet-5",
        "cerebras/gpt-oss-120b",
    ]
    # This one is mocked, and a stand-in run must never claim a vendor was
    # called. Which vendor each worker would reach is asserted below against
    # the registry the request builds.
    assert {r["vendor"] for r in roster} == {"the stand-in"}
    finished = [e for e in events if e["event"] == "run_finished"][0]
    # Three vendors, and still nothing but the record crossed between them.
    assert finished["summary"]["raw_conversation_transfers"] == 0
    assert finished["continuity"] is True


def test_the_exact_model_id_survives_into_the_worker_config(hosted):
    """What the vendor listed is what the vendor gets asked for.

    ``groq/compound`` is a real Groq model id. Reconstructing the call from
    the display string would ask Groq for ``compound``.
    """
    from context_orchestration.web.server import RunRequest, build_registry

    req = RunRequest(
        workers=[{"id": "w-1", "provider": "groq", "model": "groq/compound", "key_ref": "k1"}],
        keys=[{"id": "k1", "key": "gsk_a"}],
    )
    config = list(build_registry(req, None))[0]
    assert config.provider_config["model_id"] == "groq/compound"
    assert config.model == "groq/compound"


def test_the_roster_routes_each_worker_to_its_own_vendor(hosted):
    from context_orchestration.web.server import RunRequest, build_registry, _vendor

    req = RunRequest(
        workers=ROSTER,
        keys=[
            {"id": "k1", "key": "gsk_a"},
            {"id": "k2", "key": "sk-ant-b"},
            {"id": "k3", "key": "csk-c"},
        ],
    )
    reg = list(build_registry(req, None))
    assert [_vendor(c) for c in reg] == ["Groq", "Anthropic", "Cerebras"]
    assert [c.api_key for c in reg] == ["gsk_a", "sk-ant-b", "csk-c"]


def test_a_borrowed_key_is_dealt_out_one_per_worker(lending):
    """The demonstration is independent credentials, not one key reused."""
    from context_orchestration.web.server import RunRequest, build_registry

    req = RunRequest(
        workers=[
            {"id": "w-%d" % i, "provider": "groq", "model": "m", "key_ref": "pool"}
            for i in range(1, 4)
        ]
    )
    reg = build_registry(req, None)
    assert [c.api_key for c in reg] == ["gsk_one", "gsk_two", "gsk_three"]


def test_a_worker_with_no_model_is_rejected(hosted):
    r = hosted.post(
        "/api/runs/stream",
        json={**RUN, "workers": [{"id": "w-1", "provider": "groq", "model": ""}]},
    )
    assert r.status_code == 400
    assert "model" in r.json()["detail"]


def test_two_workers_cannot_share_an_id(hosted):
    r = hosted.post(
        "/api/runs/stream",
        json={
            **RUN,
            "workers": [
                {"id": "w-1", "provider": "groq", "model": "m"},
                {"id": "w-1", "provider": "groq", "model": "m"},
            ],
        },
    )
    assert r.status_code == 400
    assert "duplicate" in r.json()["detail"]


# -- per-step budgets ------------------------------------------------------


def test_each_step_can_be_given_its_own_ceiling(hosted):
    """One-shot mode divides a total, so the ceiling changes down the plan."""
    events = frames(
        hosted.post(
            "/api/runs/stream",
            json={**RUN, "mode": "oneshot", "step_budgets": [400, 900, 2000]},
        ).text
    )
    starts = [e for e in events if e["event"] == "worker_started"]
    assert [s["package"]["token_budget"] for s in starts][:3] == [400, 900, 2000]


def test_turns_beyond_the_shares_fall_back_to_the_flat_ceiling(hosted):
    """A plan shorter than the roster is padded with continuation turns.

    Three steps against the five configured workers means five turns, and the
    two the caller did not divide a share for are not left without one.
    """
    events = frames(
        hosted.post(
            "/api/runs/stream",
            json={**RUN, "budget": 1600, "step_budgets": [400, 900, 2000]},
        ).text
    )
    starts = [e for e in events if e["event"] == "worker_started"]
    assert len(starts) == 5
    assert [s["package"]["token_budget"] for s in starts] == [400, 900, 2000, 1600, 1600]


def test_without_step_budgets_every_briefing_shares_one_ceiling(hosted):
    events = frames(hosted.post("/api/runs/stream", json={**RUN, "budget": 800}).text)
    starts = [e for e in events if e["event"] == "worker_started"]
    assert {s["package"]["token_budget"] for s in starts} == {800}


def test_a_tighter_ceiling_produces_a_smaller_briefing(hosted):
    def tokens(budget):
        events = frames(hosted.post("/api/runs/stream", json={**RUN, "budget": budget}).text)
        starts = [e for e in events if e["event"] == "worker_started"]
        return starts[-1]["package"]["estimated_tokens"]

    assert tokens(400) < tokens(2400)
