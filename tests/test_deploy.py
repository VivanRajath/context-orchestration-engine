"""The deployment, checked the way the engine is.

A hosted build is assembled by someone else's packer from someone else's
index of releases, and the two things that differ from a laptop are which
files arrive and which versions get installed. Neither is visible from a test
run, so what is checked here is the behaviour when they go wrong: the app has
to start anyway and say so, because an exception while the module is being
imported is not a failed request, it is a deployment with nothing to call.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from context_orchestration.web import server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def client(tmp_path) -> TestClient:
    return TestClient(
        server.create_app(db=str(tmp_path / "t.db")), raise_server_exceptions=False
    )


def test_health_reports_what_arrived(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["static_dir_exists"] is True
    assert "index.html" in body["assets"]
    assert body["packages"]["fastapi"] != "absent"


def test_health_never_reports_a_key(client, monkeypatch):
    monkeypatch.setenv("COE_DEMO_KEYS", "gsk_averysecretvalue,gsk_anotherone")
    text = client.get("/api/health").text
    assert "averysecretvalue" not in text and "anotherone" not in text


def test_the_app_starts_even_with_no_page_to_serve(tmp_path, monkeypatch):
    """A missing asset must cost one route, not the whole deployment.

    StaticFiles checks its directory in the constructor, and the constructor
    runs during import on a serverless host. Before check_dir was turned off,
    a build that dropped web/static answered every URL with the host's own
    crash page.
    """
    monkeypatch.setattr(server, "STATIC", tmp_path / "not-here")
    app = server.create_app(db=str(tmp_path / "t.db"))  # must not raise
    client = TestClient(app, raise_server_exceptions=False)

    # One route per missing asset fails, and says why on the document.
    assert client.get("/").status_code == 503
    assert "did not travel" in client.get("/").text
    assert client.get("/static/js/main.js").status_code >= 400
    # The engine is still there, which is the point of not dying.
    assert client.get("/api/config").status_code == 200
    assert client.get("/api/health").json()["ok"] is False


def test_the_host_can_find_the_app_without_importing_the_file():
    """`app` has to be assigned at the top level, and this is how it is read.

    The host locates the application by parsing this file rather than by
    importing it, so an `app` indented inside a `try` is an `app` it cannot
    see. Wrapping the construction in one, to keep a broken build from
    answering every URL with a crash page, is what broke the build itself:
    "Found main.py, api/index.py but none define a top-level app".
    """
    tree = ast.parse((ROOT / "api" / "index.py").read_text(encoding="utf-8"))
    top_level = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    assert "app" in top_level, "the host will not find the application"


def test_a_broken_boot_answers_with_the_reason(monkeypatch):
    """The entry point's fallback: a page that names the failure."""
    source = (ROOT / "api" / "index.py").read_text(encoding="utf-8")
    assert "except Exception:" in source, "the boot guard is gone"

    def refuse(**_kwargs):
        raise RuntimeError("the build is missing something")

    monkeypatch.setattr(server, "create_app", refuse)
    spec = importlib.util.spec_from_file_location("_entry", ROOT / "api" / "index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():  # pragma: no cover - never called
        return {}

    import asyncio

    asyncio.run(module.app({"type": "http", "method": "GET", "path": "/"}, receive, send))
    assert sent[0]["status"] == 500
    body = sent[1]["body"]
    assert b"The engine did not start" in body
    assert b"the build is missing something" in body


def test_the_deployment_pins_its_dependencies():
    """`>=` means the bundle is assembled differently every deploy."""
    for name in ("requirements.txt", "api/requirements.txt"):
        text = (ROOT / name).read_text(encoding="utf-8")
        pins = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        assert pins, name
        for pin in pins:
            assert re.fullmatch(r"[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*", pin), f"{name}: {pin}"


def test_both_requirement_files_say_the_same_thing():
    a = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    b = (ROOT / "api" / "requirements.txt").read_text(encoding="utf-8")
    assert a == b, "the build reads api/requirements.txt; they must not drift"


def test_the_build_ships_the_page():
    """includeFiles is the only reason the static tree reaches the function."""
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    include = config["functions"]["api/index.py"]["includeFiles"]
    assert include.startswith("src/"), include
    assert include.rstrip("/*").rstrip("/") == "src"


# -- the visit count -------------------------------------------------------


def test_a_visit_is_counted_once_and_reading_does_not_count(client):
    assert client.get("/api/visits").json()["views"] == 0
    assert client.post("/api/visits").json()["views"] == 1
    assert client.post("/api/visits").json()["views"] == 2
    assert client.get("/api/visits").json()["views"] == 2
    assert client.get("/api/visits").json()["views"] == 2


def test_the_count_survives_the_process_that_wrote_it(tmp_path):
    """It is a total, so it has to outlive the app that incremented it."""
    db = str(tmp_path / "v.db")
    TestClient(server.create_app(db=db)).post("/api/visits")
    TestClient(server.create_app(db=db)).post("/api/visits")
    fresh = TestClient(server.create_app(db=db))
    assert fresh.get("/api/visits").json()["views"] == 2


def test_a_count_that_cannot_be_a_total_says_so(client, monkeypatch):
    """A serverless instance gets a fresh /tmp and then takes it away.

    The number it reports is real and is not the total, so the page is told
    not to show it rather than left to present an undercount as a fact.
    """
    monkeypatch.setenv("COE_SERVERLESS", "1")
    assert client.post("/api/visits").json()["durable"] is False


def test_a_redis_over_http_makes_it_a_total(client, monkeypatch):
    monkeypatch.setenv("COE_SERVERLESS", "1")
    monkeypatch.setenv("COE_COUNTER_URL", "https://example.upstash.io")
    monkeypatch.setenv("COE_COUNTER_TOKEN", "t")
    assert client.get("/api/visits").json()["durable"] is True


@pytest.mark.parametrize(
    "url_var,token_var",
    [
        ("COE_COUNTER_URL", "COE_COUNTER_TOKEN"),
        ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"),
        ("KV_REST_API_URL", "KV_REST_API_TOKEN"),
    ],
)
def test_the_store_is_found_under_whichever_name_the_host_uses(
    url_var, token_var, monkeypatch
):
    """Upstash and Vercel's KV integration are the same service, named twice."""
    from context_orchestration.web import counter

    for name in counter.URL_VARS + counter.TOKEN_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(url_var, "https://example.upstash.io/")
    monkeypatch.setenv(token_var, "token")
    assert counter.redis_config() == ("https://example.upstash.io", "token")


def test_an_unreachable_store_does_not_take_the_page_down(tmp_path, monkeypatch):
    """A counter is not worth failing a request over."""
    monkeypatch.setenv("COE_COUNTER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("COE_COUNTER_TOKEN", "t")
    client = TestClient(
        server.create_app(db=str(tmp_path / "v.db")), raise_server_exceptions=False
    )
    body = client.post("/api/visits")
    assert body.status_code == 200
    assert body.json()["views"] == 1
