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
    return TestClient(create_app(db=str(tmp_path / "local.db")))


@pytest.fixture
def hosted(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("COE_SERVERLESS", "1")
    monkeypatch.delenv("COE_ALLOW_LIVE", raising=False)
    return TestClient(create_app(db=str(tmp_path / "hosted.db")))


RUN = {"objective": OBJECTIVE, "plan": PLAN, "mock": True, "budget": 1600}


# -- configuration ---------------------------------------------------------


def test_local_config_offers_live_mode(local):
    cfg = local.get("/api/config").json()
    assert cfg["serverless"] is False
    assert cfg["live_enabled"] is True
    assert cfg["live_disabled_reason"] is None
    assert cfg["workers"] and cfg["demo"]["plan"]


def test_serverless_config_closes_live_mode(hosted):
    cfg = hosted.get("/api/config").json()
    assert cfg["serverless"] is True
    assert cfg["live_enabled"] is False
    assert cfg["can_run_live"] is False
    assert cfg["live_disabled_reason"]


def test_allow_live_reopens_it(tmp_path, monkeypatch):
    monkeypatch.setenv("COE_SERVERLESS", "1")
    monkeypatch.setenv("COE_ALLOW_LIVE", "1")
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
        assert "disabled" in r.json()["detail"]


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
        ({"plan": []}, "plan step"),
        ({"plan": ["   "]}, "plan step"),
        ({"objective": "  "}, "Objective"),
    ],
)
def test_bad_requests_are_rejected(hosted, bad, detail):
    r = hosted.post("/api/runs/stream", json={**RUN, **bad})
    assert r.status_code == 400
    assert detail in r.json()["detail"]
