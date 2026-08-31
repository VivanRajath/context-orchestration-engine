"""The dependency-free gateway, checked against what it puts on the wire.

No network. ``urlopen`` is replaced, so every test here is about the request
that would have been sent and the reply that would have been read: the right
URL for the vendor, the right auth header, the right model name, and the same
``GatewayResponse`` out of two wire formats that look nothing like each other.

That last property is the one worth protecting. The orchestrator is supposed
to be unable to tell Anthropic from Groq, and the only place that can stop
being true is here.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from context_orchestration.core.contracts import WorkerConfig
from context_orchestration.gateway.http_gateway import HTTPGateway
from context_orchestration.gateway.llm_gateway import GatewayError


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def transport(replies, sent):
    """A urlopen that records what it was given and replays canned answers."""
    queue = list(replies)

    def urlopen(req, timeout=None):
        sent.append(
            {
                "url": req.full_url,
                "headers": {k.lower(): v for k, v in req.headers.items()},
                "body": json.loads(req.data.decode("utf-8")) if req.data else None,
            }
        )
        reply = queue.pop(0) if queue else replies[-1]
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(json.dumps(reply).encode("utf-8"))

    return urlopen


OPENAI_REPLY = {
    "choices": [{"message": {"content": "  hello  "}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
}

ANTHROPIC_REPLY = {
    "content": [{"type": "text", "text": "hello"}, {"type": "thinking", "text": "ignored"}],
    "usage": {"input_tokens": 11, "output_tokens": 3},
}

MESSAGES = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "the package"},
]


def gateway(monkeypatch, replies):
    sent: list[dict] = []
    monkeypatch.setattr(
        "context_orchestration.gateway.http_gateway.urllib.request.urlopen",
        transport(replies, sent),
    )
    return HTTPGateway(sleep=lambda _s: None), sent


# -- the OpenAI shape ------------------------------------------------------


def test_a_groq_worker_is_called_at_groqs_address(monkeypatch):
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_x")

    response = gw.complete(config, MESSAGES)

    assert sent[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert sent[0]["headers"]["authorization"] == "Bearer gsk_x"
    assert sent[0]["body"]["model"] == "llama-3.3-70b-versatile"
    assert sent[0]["body"]["messages"] == MESSAGES
    assert response.text == "hello"
    assert response.usage["total_tokens"] == 14
    # The model reported back is the qualified one the engine holds, not the
    # bare name the vendor was asked for.
    assert response.model == "groq/llama-3.3-70b-versatile"


def test_a_model_whose_name_contains_a_vendor_is_sent_intact(monkeypatch):
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(
        id="w1",
        model="groq/openai/gpt-oss-120b",
        api_key="gsk_x",
        provider_config={"provider": "groq"},
    )
    gw.complete(config, MESSAGES)
    assert sent[0]["url"].startswith("https://api.groq.com")
    assert sent[0]["body"]["model"] == "openai/gpt-oss-120b"


def test_a_model_id_that_begins_with_its_own_vendor_is_sent_whole(monkeypatch):
    """Groq publishes a model whose id is literally ``groq/compound``.

    Found in a live run: the display string and the id to call had been the
    same field, so stripping the vendor prefix off ``groq/compound`` asked
    Groq for ``compound``, which it does not serve. The id the vendor
    published is now carried separately and never re-derived.
    """
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(
        id="w1",
        model="groq/compound",
        api_key="gsk_x",
        provider_config={"provider": "groq", "model_id": "groq/compound"},
    )
    gw.complete(config, MESSAGES)
    assert sent[0]["url"].startswith("https://api.groq.com")
    assert sent[0]["body"]["model"] == "groq/compound"


def test_routing_keys_are_not_forwarded_to_the_provider(monkeypatch):
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(
        id="w1",
        model="groq/compound",
        api_key="gsk_x",
        provider_config={"provider": "groq", "model_id": "groq/compound", "top_p": 0.9},
    )
    gw.complete(config, MESSAGES)
    body = sent[0]["body"]
    assert "provider" not in body and "model_id" not in body
    assert body["top_p"] == 0.9  # anything else is a genuine passthrough


def test_a_custom_endpoint_is_honoured(monkeypatch):
    """Any third party speaking the OpenAI shape, named only by its URL."""
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(
        id="w1",
        model="some-model",
        api_key="sk-third-party",
        api_base="https://gateway.example.test/v1",
        provider_config={"provider": "custom"},
    )
    gw.complete(config, MESSAGES)
    assert sent[0]["url"] == "https://gateway.example.test/v1/chat/completions"
    assert sent[0]["body"]["model"] == "some-model"


def test_the_vendor_is_inferred_from_the_key_when_nothing_says(monkeypatch):
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(id="w1", model="llama-3.1-8b-instant", api_key="gsk_x")
    gw.complete(config, MESSAGES)
    assert sent[0]["url"].startswith("https://api.groq.com")


def test_an_unattributable_worker_is_refused_before_any_request(monkeypatch):
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(id="w1", model="mystery", api_key="not-a-known-prefix")
    with pytest.raises(GatewayError, match="which provider"):
        gw.complete(config, MESSAGES)
    assert sent == []


def test_a_worker_with_no_key_is_refused_before_any_request(monkeypatch):
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(id="w1", model="groq/llama-3.1-8b-instant")
    with pytest.raises(GatewayError, match="no API key"):
        gw.complete(config, MESSAGES)
    assert sent == []


# -- the Anthropic shape ---------------------------------------------------


def test_anthropic_gets_its_own_wire_format(monkeypatch):
    gw, sent = gateway(monkeypatch, [ANTHROPIC_REPLY])
    config = WorkerConfig(id="w2", model="anthropic/claude-sonnet-5", api_key="sk-ant-x")

    response = gw.complete(config, MESSAGES)

    call = sent[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-ant-x"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    # The system prompt sits beside the conversation, not inside it.
    assert call["body"]["system"] == "be brief"
    assert call["body"]["messages"] == [{"role": "user", "content": "the package"}]
    assert "authorization" not in call["headers"]

    # Two unrelated wire formats, one answer shape. This is the property the
    # orchestrator depends on without knowing it does.
    assert response.text == "hello"
    assert response.usage["total_tokens"] == 14


def test_anthropic_is_not_asked_for_a_response_format(monkeypatch):
    """It does not support the field, and sending it would waste a request."""
    gw, sent = gateway(monkeypatch, [ANTHROPIC_REPLY])
    config = WorkerConfig(id="w2", model="anthropic/claude-sonnet-5", api_key="sk-ant-x")
    gw.complete(config, MESSAGES, {"type": "object"})
    assert len(sent) == 1
    assert "response_format" not in sent[0]["body"]


# -- structured output -----------------------------------------------------


def http_error(code, body):
    return urllib.error.HTTPError(
        "https://x.test", code, "err", {}, io.BytesIO(json.dumps(body).encode())
    )


def test_the_strictest_schema_is_asked_for_first(monkeypatch):
    gw, sent = gateway(monkeypatch, [OPENAI_REPLY])
    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_x")
    gw.complete(config, MESSAGES, {"type": "object"})
    assert sent[0]["body"]["response_format"]["type"] == "json_schema"


def test_a_model_that_refuses_a_schema_falls_back(monkeypatch):
    """Support varies by model, and a model that lacks it answers 400."""
    refuse = http_error(400, {"error": {"message": "response_format not supported"}})
    gw, sent = gateway(monkeypatch, [refuse, OPENAI_REPLY])
    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_x")

    response = gw.complete(config, MESSAGES, {"type": "object"})

    assert [c["body"]["response_format"]["type"] for c in sent] == ["json_schema", "json_object"]
    assert response.text == "hello"


def test_the_last_resort_is_no_response_format_at_all(monkeypatch):
    refuse = http_error(400, {"error": {"message": "nope"}})
    gw, sent = gateway(monkeypatch, [refuse, refuse, OPENAI_REPLY])
    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_x")
    gw.complete(config, MESSAGES, {"type": "object"})
    assert len(sent) == 3
    assert "response_format" not in sent[2]["body"]


# -- failure -------------------------------------------------------------


def test_a_rate_limit_is_waited_out_not_reported(monkeypatch):
    slept = []
    limited = urllib.error.HTTPError(
        "https://x.test", 429, "slow down", {"retry-after": "2"}, io.BytesIO(b"{}")
    )
    sent: list[dict] = []
    monkeypatch.setattr(
        "context_orchestration.gateway.http_gateway.urllib.request.urlopen",
        transport([limited, OPENAI_REPLY], sent),
    )
    gw = HTTPGateway(sleep=slept.append)

    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_x")
    assert gw.complete(config, MESSAGES).text == "hello"
    # The provider said two seconds, so two seconds it is.
    assert slept == [2.5]


def test_a_persistent_rate_limit_does_not_burn_the_schema_fallbacks(monkeypatch):
    """Retrying a different response_format cannot fix an exhausted quota."""
    limited = urllib.error.HTTPError("https://x.test", 429, "slow", {}, io.BytesIO(b"{}"))
    gw, sent = gateway(monkeypatch, [limited])
    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_x")
    with pytest.raises(GatewayError):
        gw.complete(config, MESSAGES, {"type": "object"})
    # Four attempts at the first variant, and then it stops rather than
    # working through json_object and bare as well.
    assert len(sent) == 4


def test_the_providers_own_message_survives_into_the_error(monkeypatch):
    refused = http_error(401, {"error": {"message": "Invalid API Key"}})
    gw, _ = gateway(monkeypatch, [refused])
    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_bad")
    with pytest.raises(GatewayError, match="Invalid API Key"):
        gw.complete(config, MESSAGES)


def test_an_answer_with_no_choices_is_an_error_not_an_index_crash(monkeypatch):
    gw, _ = gateway(monkeypatch, [{"choices": []}])
    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_x")
    with pytest.raises(GatewayError, match="no choices"):
        gw.complete(config, MESSAGES)


def test_an_unreachable_provider_is_an_ordinary_error(monkeypatch):
    gw, _ = gateway(monkeypatch, [urllib.error.URLError("no route to host")])
    config = WorkerConfig(id="w1", model="groq/llama-3.3-70b-versatile", api_key="gsk_x")
    with pytest.raises(GatewayError, match="could not reach"):
        gw.complete(config, MESSAGES)
