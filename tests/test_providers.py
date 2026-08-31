"""Working out what a key is, and which model to point it at.

None of this talks to a provider. The HTTP call is stubbed, because what needs
guarding is the reasoning around it: that a key is attributed to the vendor
that actually issued it, that a model list is filtered down to models the
engine could really assign, and that five workers do not all end up on the
same model when the key offers a choice.
"""

from __future__ import annotations

import pytest

from context_orchestration.gateway import providers


# -- detection -------------------------------------------------------------


@pytest.mark.parametrize(
    "key, expected",
    [
        ("gsk_abcdef0123", "groq"),
        ("sk-ant-api03-xyz", "anthropic"),
        ("sk-or-v1-abc", "openrouter"),
        ("csk-abcdef", "cerebras"),
        ("sk-proj-abcdef", "openai"),
        ("AIzaSyABCDEF", "gemini"),
        ("fw_abcdef", "fireworks"),
        ("xai-abcdef", "xai"),
        ("", None),
        ("nonsense", None),
    ],
)
def test_a_key_is_attributed_from_its_own_prefix(key, expected):
    assert providers.detect(key) == expected


def test_the_longest_matching_prefix_wins():
    """``sk-ant-`` and ``sk-or-`` both start with a prefix OpenAI also uses."""
    assert providers.detect("sk-ant-api03-x") == "anthropic"
    assert providers.detect("sk-or-v1-x") == "openrouter"


def test_a_bare_sk_key_is_a_guess_and_says_so():
    assert providers.detect("sk-abcdef") == "openai"
    assert providers.ambiguous("sk-abcdef") is True
    assert providers.ambiguous("gsk_abcdef") is False
    assert providers.ambiguous("sk-ant-abc") is False


# -- model strings ---------------------------------------------------------


def test_only_a_known_vendor_counts_as_a_prefix():
    assert providers.split_model("groq/openai/gpt-oss-120b") == ("groq", "openai/gpt-oss-120b")
    assert providers.split_model("llama-3.3-70b-versatile") == (None, "llama-3.3-70b-versatile")


def test_qualifying_is_idempotent():
    once = providers.qualify("groq", "llama-3.3-70b-versatile")
    assert once == "groq/llama-3.3-70b-versatile"
    assert providers.qualify("groq", once) == once


def test_a_named_vendor_beats_a_model_name_that_looks_like_one():
    """Groq serves a model called ``openai/gpt-oss-120b``. It is still Groq's.

    Guessing the vendor from the model string would send Groq a request for
    ``gpt-oss-120b``, a name it does not answer to, having first decided the
    call belonged to OpenAI.
    """
    assert providers.qualify("groq", "openai/gpt-oss-120b") == "groq/openai/gpt-oss-120b"
    assert providers.strip_vendor("groq/openai/gpt-oss-120b", "groq") == "openai/gpt-oss-120b"
    # Told it is Groq, it removes Groq's prefix and no other.
    assert providers.strip_vendor("openai/gpt-oss-120b", "groq") == "openai/gpt-oss-120b"


def test_a_vendor_whose_litellm_name_differs_strips_either_form():
    assert providers.qualify("together", "llama-3") == "together_ai/llama-3"
    assert providers.strip_vendor("together_ai/llama-3", "together") == "llama-3"
    assert providers.strip_vendor("together/llama-3", "together") == "llama-3"


# -- choosing a model ------------------------------------------------------
#
# Two catalogues, because vendors differ in how much they say. Groq publishes
# modalities, context windows and feature flags; others publish bare ids. The
# ranking has to hold up on both.

CATALOGUE = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "whisper-large-v3",
    "text-embedding-3-large",
    "playai-tts",
]


def described(**kw):
    return providers.ModelInfo(**kw)


DESCRIBED = [
    described(id="openai/gpt-oss-120b", context=131072, inputs=("text",), outputs=("text",),
              features=("tools", "json_mode", "structured_outputs", "reasoning")),
    described(id="openai/gpt-oss-20b", context=131072, inputs=("text",), outputs=("text",),
              features=("tools", "json_mode", "structured_outputs", "reasoning")),
    described(id="qwen/qwen3.8-27b", context=131042, inputs=("text", "image"), outputs=("text",),
              features=("tools", "json_mode", "reasoning")),
    described(id="canopylabs/orpheus-v1-english", context=4000, inputs=("text",),
              outputs=("speech",)),
    described(id="whisper-large-v3", context=448, inputs=("audio",), outputs=("transcription",)),
    described(id="meta-llama/llama-prompt-guard-2-86m", context=512, inputs=("text",),
              outputs=("text",)),
    described(id="retired-model", context=131072, outputs=("text",), active=False),
]


def ids(models):
    return [m.id for m in models]


def test_models_the_engine_cannot_assign_are_filtered_out():
    kept = [m for m in CATALOGUE if providers.is_chat_model(m)]
    assert "whisper-large-v3" not in kept
    assert "text-embedding-3-large" not in kept
    assert "playai-tts" not in kept
    assert "llama-3.3-70b-versatile" in kept


def test_a_model_that_does_not_write_text_is_excluded_on_the_vendors_own_word():
    """Its name says nothing; its declared output modality says everything."""
    speech = described(id="canopylabs/orpheus-v1-english", outputs=("speech",))
    assert providers.is_chat_model(speech) is False
    assert providers.is_chat_model(described(id="canopylabs/orpheus-v1-english")) is True


def test_a_model_that_cannot_read_text_is_excluded():
    assert providers.is_chat_model(described(id="x", inputs=("audio",), outputs=("text",))) is False


def test_a_context_window_too_small_to_hold_a_briefing_is_excluded():
    assert providers.is_chat_model(described(id="tiny", context=512, outputs=("text",))) is False
    assert providers.is_chat_model(described(id="big", context=131072, outputs=("text",))) is True


def test_ranking_drops_everything_that_could_not_take_a_turn():
    kept = ids(providers.rank(DESCRIBED, "strong"))
    assert kept == ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]


def test_schema_support_breaks_a_tie_between_comparable_models():
    """Every worker is asked for JSON matching a schema, so this counts.

    It is a thumb on the scale rather than the whole scale: a much larger
    model that answers on prompt discipline alone is still the better worker,
    and the gateway falls back to asking nicely when it has to.
    """
    enforced = described(id="model-a-70b", context=131072, outputs=("text",),
                         features=("structured_outputs", "json_mode"))
    prompted = described(id="model-b-70b", context=131072, outputs=("text",),
                         features=("tools",))
    assert ids(providers.rank([prompted, enforced], "strong"))[0] == "model-a-70b"
    assert providers.strength(enforced) > providers.strength(prompted)


def test_ranking_puts_the_larger_model_first_for_hard_steps():
    ordered = ids(providers.rank(CATALOGUE, "strong"))
    assert ordered[0] in {"openai/gpt-oss-120b", "llama-3.3-70b-versatile"}
    assert ordered.index("openai/gpt-oss-120b") < ordered.index("llama-3.1-8b-instant")


def test_ranking_inverts_for_cheap_steps():
    assert ids(providers.rank(CATALOGUE, "fast"))[0] == "llama-3.1-8b-instant"


def test_a_model_reports_what_the_dropdown_should_say_about_it():
    row = DESCRIBED[0].as_dict()
    assert row["id"] == "openai/gpt-oss-120b"
    assert row["structured"] is True
    assert "131k context" in row["note"]
    assert "schema-enforced" in row["note"]


def test_a_review_step_is_recommended_a_strong_model():
    assert providers.recommend(CATALOGUE, "review") in {
        "openai/gpt-oss-120b",
        "llama-3.3-70b-versatile",
    }


def test_workers_are_spread_across_different_models():
    roles = ["architecture", "data modelling", "security", "api design", "review"]
    picked = providers.spread(CATALOGUE, roles)
    assert len(picked) == len(roles)
    # Four chat models for five workers: exactly one repeat, not five of one.
    assert len(set(picked)) == 4


def test_a_bare_list_of_ids_still_ranks_when_the_vendor_says_nothing_else():
    """Not every vendor publishes metadata. The name is then all there is."""
    ordered = ids(providers.rank(["llama-3.1-8b-instant", "llama-3.3-70b-versatile"], "strong"))
    assert ordered[0] == "llama-3.3-70b-versatile"


def test_spread_copes_with_a_key_that_offers_one_model():
    picked = providers.spread(["only-model"], ["architecture", "review"])
    assert picked == ["only-model", "only-model"]


def test_spread_on_an_empty_catalogue_returns_nothing():
    assert providers.spread([], ["architecture"]) == []


# -- inspecting a key ------------------------------------------------------


def test_an_unrecognised_key_is_reported_not_raised():
    report = providers.inspect_key("nonsense-key")
    assert report.ok is False
    assert "vendor" in (report.error or "")


def test_an_empty_key_is_reported_not_raised():
    assert providers.inspect_key("").ok is False


def test_a_refused_key_carries_the_vendors_own_words(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise providers.ProviderError("the provider rejected this key: bad token")

    monkeypatch.setattr(providers, "list_models", refuse)
    report = providers.inspect_key("gsk_whatever")
    assert report.ok is False
    assert report.provider == "groq"
    assert "rejected" in report.error


def test_a_working_key_comes_back_with_models_and_a_default(monkeypatch):
    monkeypatch.setattr(providers, "list_models", lambda *a, **k: list(DESCRIBED))
    report = providers.inspect_key("gsk_whatever")
    assert report.ok is True
    assert report.provider_label == "Groq"
    assert report.live is True
    assert report.recommended == report.models[0].id
    assert "whisper-large-v3" not in ids(report.models)
    # A model the vendor has retired is not offered either.
    assert "retired-model" not in ids(report.models)


def test_a_key_with_no_usable_model_is_a_failure(monkeypatch):
    monkeypatch.setattr(providers, "list_models", lambda *a, **k: ["whisper-large-v3"])
    report = providers.inspect_key("gsk_whatever")
    assert report.ok is False
    assert "take a turn" in report.error


def test_a_named_vendor_overrides_the_prefix(monkeypatch):
    """A third-party endpoint may hand out keys in OpenAI's format."""
    seen = {}

    def capture(pid, key, base):
        seen["pid"], seen["base"] = pid, base
        return ["some-model"]

    monkeypatch.setattr(providers, "list_models", capture)
    report = providers.inspect_key("sk-looks-like-openai", "custom", "https://example.test/v1")
    assert seen == {"pid": "custom", "base": "https://example.test/v1"}
    assert report.ok is True
    assert report.detected is False


# -- error text ------------------------------------------------------------


def test_a_401_is_explained_before_the_vendor_is_quoted():
    text = providers._explain(401, '{"error": {"message": "Invalid API Key"}}')
    assert text.startswith("the provider rejected this key")
    assert "Invalid API Key" in text


def test_an_unparseable_body_still_yields_something_readable():
    assert "429" not in providers._explain(429, "<html>rate limited</html>")
    assert providers._explain(500, "")
