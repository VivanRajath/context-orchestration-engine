"""Who issued a key, and what that key is allowed to run.

Everything above the gateway layer speaks in terms of "a worker with a model
string" and must stay that way. This module is where the knowledge that
vendors exist is allowed to live, and it earns its place by answering three
questions the playground has to ask before a run can start:

1. Which vendor issued this key? Answered from the key's own prefix. Vendors
   brand their keys, so ``gsk_`` is a Groq key and nothing else is.
2. What can this key actually run? Answered by asking the vendor. Every
   provider here exposes a model list at a documented URL, so the catalogue
   below is a fallback for when that call fails, not the source of truth.
3. Which of those models suits the step about to be assigned? Answered by
   scoring the names the vendor returned. Hardcoding winners would rot within
   a release; scoring survives models this file has never heard of.

Nothing here imports a vendor SDK. The model list is one HTTPS GET made with
the standard library, which is what lets the deployed playground do all of
this inside a serverless function with three dependencies installed.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

TIMEOUT = 15

# Several vendors sit behind a CDN that refuses the default urllib agent
# outright, before the request ever reaches them, so identify honestly.
USER_AGENT = "context-orchestration-engine/0.1 (+https://github.com/VivanRajath/context-orchestration-engine)"


# --------------------------------------------------------------------------
# The catalogue
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    """One vendor, described only in terms of HTTP."""

    id: str
    label: str
    # Key prefixes, longest first at match time. A vendor with no distinctive
    # prefix is still usable; it just cannot be auto-detected.
    prefixes: tuple[str, ...] = ()
    base_url: str = ""
    models_path: str = "/models"
    # "bearer" is the OpenAI convention and covers almost everyone.
    auth: str = "bearer"
    # What LiteLLM would call this, so a roster built in the browser can still
    # be run through LiteLLM by anyone who prefers it.
    litellm_prefix: str = ""
    free_tier: bool = False
    signup: str = ""
    # Used only when the vendor's own model list cannot be reached.
    fallback_models: tuple[str, ...] = ()
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        id="groq",
        label="Groq",
        prefixes=("gsk_",),
        base_url="https://api.groq.com/openai/v1",
        litellm_prefix="groq",
        free_tier=True,
        signup="https://console.groq.com/keys",
        fallback_models=(
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "qwen/qwen3-32b",
        ),
        note="Fast, and free to start. Several model families behind one key.",
    ),
    "anthropic": Provider(
        id="anthropic",
        label="Anthropic",
        prefixes=("sk-ant-",),
        base_url="https://api.anthropic.com/v1",
        auth="anthropic",
        litellm_prefix="anthropic",
        signup="https://console.anthropic.com/settings/keys",
        fallback_models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"),
        note="Claude. Answers on a different wire format, which the gateway absorbs.",
    ),
    "openai": Provider(
        id="openai",
        label="OpenAI",
        prefixes=("sk-proj-", "sk-svcacct-"),
        base_url="https://api.openai.com/v1",
        litellm_prefix="openai",
        signup="https://platform.openai.com/api-keys",
        fallback_models=("gpt-4o", "gpt-4o-mini"),
    ),
    "cerebras": Provider(
        id="cerebras",
        label="Cerebras",
        prefixes=("csk-",),
        base_url="https://api.cerebras.ai/v1",
        litellm_prefix="cerebras",
        free_tier=True,
        signup="https://cloud.cerebras.ai/",
        fallback_models=("gpt-oss-120b", "llama-3.3-70b", "qwen-3-32b"),
        note="Someone else's silicon running open models, very fast.",
    ),
    "openrouter": Provider(
        id="openrouter",
        label="OpenRouter",
        prefixes=("sk-or-",),
        base_url="https://openrouter.ai/api/v1",
        litellm_prefix="openrouter",
        signup="https://openrouter.ai/keys",
        fallback_models=("openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"),
        note="A broker in front of many vendors. One key, hundreds of models.",
    ),
    "together": Provider(
        id="together",
        label="Together AI",
        prefixes=("tgp_v1_",),
        base_url="https://api.together.xyz/v1",
        litellm_prefix="together_ai",
        signup="https://api.together.ai/settings/api-keys",
        fallback_models=("meta-llama/Llama-3.3-70B-Instruct-Turbo",),
    ),
    "mistral": Provider(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        litellm_prefix="mistral",
        signup="https://console.mistral.ai/api-keys/",
        fallback_models=("mistral-large-latest", "mistral-small-latest"),
    ),
    "deepseek": Provider(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        litellm_prefix="deepseek",
        signup="https://platform.deepseek.com/api_keys",
        fallback_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "fireworks": Provider(
        id="fireworks",
        label="Fireworks",
        prefixes=("fw_",),
        base_url="https://api.fireworks.ai/inference/v1",
        litellm_prefix="fireworks_ai",
        signup="https://fireworks.ai/account/api-keys",
        fallback_models=("accounts/fireworks/models/llama-v3p3-70b-instruct",),
    ),
    "xai": Provider(
        id="xai",
        label="xAI",
        prefixes=("xai-",),
        base_url="https://api.x.ai/v1",
        litellm_prefix="xai",
        signup="https://console.x.ai/",
        fallback_models=("grok-3", "grok-3-mini"),
    ),
    "perplexity": Provider(
        id="perplexity",
        label="Perplexity",
        prefixes=("pplx-",),
        base_url="https://api.perplexity.ai",
        litellm_prefix="perplexity",
        signup="https://www.perplexity.ai/settings/api",
        fallback_models=("sonar", "sonar-pro"),
    ),
    "gemini": Provider(
        id="gemini",
        label="Google Gemini",
        prefixes=("AIza",),
        # Google publishes an OpenAI-shaped endpoint, so it needs no adapter
        # of its own here.
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        litellm_prefix="gemini",
        free_tier=True,
        signup="https://aistudio.google.com/apikey",
        fallback_models=("gemini-2.5-flash", "gemini-2.5-pro"),
    ),
    # Anything else that speaks the OpenAI shape: a self-hosted model, a
    # gateway product, a proxy in front of a vendor. The engine does not care,
    # and that is the whole argument being demonstrated.
    "custom": Provider(
        id="custom",
        label="Somewhere else",
        litellm_prefix="openai",
        note="Any endpoint that speaks the OpenAI shape. Give it a base URL.",
    ),
}

ORDER = [
    "groq",
    "anthropic",
    "openai",
    "cerebras",
    "openrouter",
    "gemini",
    "together",
    "mistral",
    "deepseek",
    "fireworks",
    "xai",
    "perplexity",
    "custom",
]


def catalogue() -> list[dict[str, Any]]:
    """The provider list the browser draws its dropdowns from."""
    return [
        {
            "id": p.id,
            "label": p.label,
            # The page names the vendor the moment a key is pasted rather than
            # after a round trip. The server checks it again regardless.
            "prefixes": list(p.prefixes),
            "free_tier": p.free_tier,
            "signup": p.signup,
            "note": p.note,
            "needs_base_url": not p.base_url,
        }
        for p in (PROVIDERS[i] for i in ORDER)
    ]


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def detect(key: str) -> str | None:
    """Which vendor issued this key, going by its prefix.

    Longest prefix wins, so ``sk-ant-`` is not mistaken for an OpenAI key and
    ``sk-or-`` is not mistaken for either. A bare ``sk-`` is genuinely
    ambiguous - several vendors copied OpenAI's format without changing it -
    so it resolves to OpenAI and the caller is expected to let the user say
    otherwise.
    """
    k = (key or "").strip()
    if not k:
        return None
    best: tuple[int, str] | None = None
    for p in PROVIDERS.values():
        for pre in p.prefixes:
            if k.startswith(pre) and (best is None or len(pre) > best[0]):
                best = (len(pre), p.id)
    if best:
        return best[1]
    if k.startswith("sk-"):
        return "openai"
    return None


def ambiguous(key: str) -> bool:
    """True when detection was a guess rather than a prefix match."""
    k = (key or "").strip()
    if not k.startswith("sk-"):
        return False
    return not any(k.startswith(pre) for p in PROVIDERS.values() for pre in p.prefixes)


def split_model(model: str) -> tuple[str | None, str]:
    """Split a LiteLLM-style ``vendor/model`` string into its two halves.

    Only the first segment is treated as a vendor, and only when it names one
    we know: ``groq/openai/gpt-oss-120b`` is Groq's copy of a model whose own
    name contains a slash, not a model belonging to OpenAI.
    """
    if "/" in model:
        head, rest = model.split("/", 1)
        if head in PROVIDERS:
            return head, rest
    return None, model


def _prefixes_for(provider_id: str) -> tuple[str, ...]:
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        return (provider_id,)
    return tuple(dict.fromkeys(p for p in (provider.litellm_prefix, provider.id) if p))


def qualify(provider_id: str, model: str) -> str:
    """The ``vendor/model`` string the rest of the engine passes around.

    When the caller knows the vendor, that answer wins outright. Guessing from
    the model string instead would mis-attribute the several models whose own
    names begin with a vendor's: Groq serves ``openai/gpt-oss-120b``, and it
    is Groq's to serve.
    """
    if not provider_id:
        return model
    prefixes = _prefixes_for(provider_id)
    if any(model.startswith(p + "/") for p in prefixes):
        return model
    return f"{prefixes[0]}/{model}"


def strip_vendor(model: str, provider_id: str | None = None) -> str:
    """The bare model name, as the vendor's own API expects to receive it.

    With a vendor named, only that vendor's prefix is removed, so
    ``groq/openai/gpt-oss-120b`` becomes ``openai/gpt-oss-120b`` and not
    ``gpt-oss-120b``, which Groq would not recognise.
    """
    if provider_id:
        for prefix in _prefixes_for(provider_id):
            if model.startswith(prefix + "/"):
                return model[len(prefix) + 1:]
        return model
    return split_model(model)[1]


# --------------------------------------------------------------------------
# Asking the vendor what the key can run
# --------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """The vendor refused, or could not be reached."""


@dataclass
class ModelInfo:
    """One model, described with whatever the vendor was willing to say.

    Most vendors publish far more than an id: which modalities the model reads
    and writes, how much context it holds, and whether it can be made to
    answer in a schema. All three matter here - a worker that cannot return
    structured output cannot take a turn - so they are kept rather than
    reduced to a name to pattern-match against later.
    """

    id: str
    label: str = ""
    context: int = 0
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    active: bool = True

    @classmethod
    def coerce(cls, row: Any) -> "ModelInfo | None":
        if isinstance(row, str):
            return cls(id=row.strip()) if row.strip() else None
        if not isinstance(row, dict):
            return None
        ident = row.get("id") or row.get("name") or row.get("model")
        if not isinstance(ident, str) or not ident.strip():
            return None
        return cls(
            id=ident.strip(),
            label=str(row.get("name") or "").strip(),
            context=_int(row.get("context_window") or row.get("context_length")),
            inputs=_strs(row.get("input_modalities")),
            outputs=_strs(row.get("output_modalities")),
            features=_strs(row.get("supported_features")),
            active=row.get("active", True) is not False,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label or self.id,
            "context": self.context,
            "structured": self.structured,
            "note": self.note,
        }

    @property
    def structured(self) -> bool:
        """Can this model be held to a schema, rather than asked nicely?"""
        return any(f in self.features for f in ("structured_outputs", "json_mode", "json_schema"))

    @property
    def note(self) -> str:
        """A short, honest line for the dropdown."""
        bits = []
        if self.context:
            bits.append(f"{self.context // 1000}k context" if self.context >= 1000
                        else f"{self.context} context")
        if self.features:
            bits.append("schema-enforced" if self.structured else "no schema support")
        return ", ".join(bits)


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _strs(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(v).lower() for v in value if isinstance(v, (str, int)))
    return ()


def _headers(provider: Provider, key: str) -> dict[str, str]:
    if provider.auth == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def _get(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(
        url, headers={**headers, "Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise ProviderError(_explain(exc.code, body)) from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"could not reach the provider: {exc.reason}") from exc
    except Exception as exc:
        raise ProviderError(" ".join(str(exc).split())[:300]) from exc


def _explain(status: int, body: str) -> str:
    """Say what the status code means before quoting the vendor at the user."""
    detail = ""
    try:
        data = json.loads(body)
        err = data.get("error", data)
        if isinstance(err, dict):
            detail = str(err.get("message") or err.get("type") or "")
        elif isinstance(err, str):
            detail = err
    except Exception:
        detail = " ".join(body.split())[:200]
    heads = {
        401: "the provider rejected this key",
        403: "this key is not allowed to do that",
        404: "no model list at that address; check the base URL",
        429: "this key is rate limited right now",
    }
    head = heads.get(status, f"the provider answered {status}")
    return f"{head}: {detail}"[:300] if detail else head


def list_models(provider_id: str, key: str, api_base: str | None = None) -> list[ModelInfo]:
    """Every model this key may call, straight from the vendor."""
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise ProviderError(f"unknown provider: {provider_id}")
    base = (api_base or provider.base_url or "").rstrip("/")
    if not base:
        raise ProviderError("this provider needs a base URL before it can be asked anything")

    data = _get(base + provider.models_path, _headers(provider, key))
    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ProviderError("the provider's model list was not in a shape we understand")

    seen: dict[str, ModelInfo] = {}
    for row in rows:
        info = ModelInfo.coerce(row)
        if info and info.active and info.id not in seen:
            seen[info.id] = info
    return sorted(seen.values(), key=lambda m: m.id)


# --------------------------------------------------------------------------
# Choosing one
# --------------------------------------------------------------------------
#
# A vendor's model list is not a list of workers. It mixes in transcription,
# speech, embedding and safety-classifier models, and the playground only ever
# wants something that reads text, writes text, and can be held to a schema.
#
# Where the vendor publishes that (Groq, OpenRouter and others do), it is used
# directly and no guessing happens at all. Where the vendor publishes bare ids
# and nothing else, the name is all there is, so it is read for the same
# facts: family, size and purpose are usually right there in the string. That
# fallback is deliberately cruder, and it degrades to "a reasonable order"
# rather than to "wrong".

_EXCLUDE = re.compile(
    r"whisper|tts|audio|speech|transcrib|embed|moderation|rerank|guard"
    r"|image|dall-?e|sora|video|clip|bge|nomic|voice|realtime|-ocr\b",
    re.I,
)

_STRONG = re.compile(r"opus|gpt-?5|gpt-?4|405b|120b|70b|72b|large|pro\b|maverick|r1|reasoner", re.I)
_MID = re.compile(r"sonnet|32b|30b|27b|20b|17b|medium|versatile|scout|qwen3|grok", re.I)
_FAST = re.compile(r"haiku|mini|instant|flash|small|8b|9b|7b|4b|lite|nano|turbo", re.I)
_DATED = re.compile(r"\d{4}-\d{2}-\d{2}|\d{8}")
_PREVIEW = re.compile(r"preview|beta|alpha|experimental|-exp\b|deprecated", re.I)

# Below this, a model cannot hold a briefing plus its own answer, whatever
# else it can do. Groq's prompt-guard models advertise 512 and Whisper 448.
MIN_CONTEXT = 4000


def _info(model: "ModelInfo | str") -> ModelInfo:
    return model if isinstance(model, ModelInfo) else ModelInfo(id=str(model))


def is_chat_model(model: "ModelInfo | str") -> bool:
    """Could this model take a worker's turn?"""
    info = _info(model)
    # The vendor's own answer, where there is one.
    if not info.active:
        return False
    if info.outputs and "text" not in info.outputs:
        return False
    if info.inputs and "text" not in info.inputs:
        return False
    if info.context and info.context < MIN_CONTEXT:
        return False
    return not _EXCLUDE.search(info.id)


def strength(model: "ModelInfo | str") -> int:
    """A rough capability score. Published facts first, the name second."""
    info = _info(model)
    name = info.id
    score = 50
    if _STRONG.search(name):
        score += 40
    elif _MID.search(name):
        score += 20
    if _FAST.search(name):
        score -= 25
    # A parameter count in the name is the most honest signal a name carries.
    m = re.search(r"(\d{1,4})\s*b\b", name, re.I)
    if m:
        try:
            score += min(30, int(m.group(1)) // 8)
        except ValueError:
            pass
    if _PREVIEW.search(name):
        score -= 15
    if _DATED.search(name):
        score -= 3  # prefer the rolling alias over a pinned snapshot

    # What the vendor published about it, which beats anything read off a name.
    if info.features:
        if "structured_outputs" in info.features:
            score += 18  # the engine asks every worker for a schema
        elif info.structured:
            score += 10
        else:
            score -= 20  # it would be answering on prompt discipline alone
        if "reasoning" in info.features:
            score += 10
        if "tools" in info.features:
            score += 4
    if info.context:
        score += min(20, info.context // 16000)
    return score


# What each step of a plan wants from a model. Early steps set the shape of
# everything after them and the last step judges the whole thing, so both get
# the strongest model available; the middle is throughput.
ROLE_WANTS = {
    "architecture": "strong",
    "review": "strong",
    "security": "strong",
    "planning": "strong",
    "data modelling": "mid",
    "api design": "mid",
    "implementation": "mid",
    "drafting": "fast",
    "summary": "fast",
}


def rank(models: Iterable["ModelInfo | str"], want: str = "strong") -> list[ModelInfo]:
    """Order candidate models best-first for a given appetite."""
    chat = [_info(m) for m in models]
    chat = [m for m in chat if is_chat_model(m)]
    if want == "fast":
        return sorted(chat, key=lambda m: (strength(m), m.id))
    if want == "mid":
        # Closest to the middle of the pack, ties broken toward the stronger.
        scored = sorted(((strength(m), m.id, m) for m in chat), key=lambda s: (s[0], s[1]))
        if not scored:
            return []
        mid = scored[len(scored) // 2][0]
        return [
            m for _, _, m in sorted(scored, key=lambda s: (abs(s[0] - mid), -s[0], s[1]))
        ]
    return sorted(chat, key=lambda m: (-strength(m), m.id))


def recommend(models: Iterable["ModelInfo | str"], role: str | None = None) -> str | None:
    """The one model to put in the dropdown by default."""
    ordered = rank(models, ROLE_WANTS.get((role or "").lower(), "strong"))
    return ordered[0].id if ordered else None


def spread(models: Iterable["ModelInfo | str"], roles: list[str]) -> list[str]:
    """Pick a model per role, preferring a different one for each.

    The demo's entire claim is that the workers are interchangeable and
    unrelated, which is far more convincing when they are visibly not the same
    model five times over. Where the key only exposes one usable model, they
    do all get the same one, and the run proves the point regardless.
    """
    chat = [m for m in (_info(m) for m in models) if is_chat_model(m)]
    if not chat:
        return []
    used: list[str] = []
    out: list[str] = []
    for role in roles:
        ordered = rank(chat, ROLE_WANTS.get((role or "").lower(), "strong"))
        pick = next((m.id for m in ordered if m.id not in used), ordered[0].id if ordered else None)
        if pick is None:
            break
        out.append(pick)
        used.append(pick)
        if len(used) >= len(chat):
            used = []
    return out


# --------------------------------------------------------------------------
# What the browser is told about a key
# --------------------------------------------------------------------------


@dataclass
class KeyReport:
    ok: bool
    provider: str | None = None
    provider_label: str = ""
    detected: bool = False
    models: list[ModelInfo] = field(default_factory=list)
    recommended: str | None = None
    error: str | None = None
    live: bool = False  # did the vendor answer, or is this the fallback list?

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "provider_label": self.provider_label,
            "detected": self.detected,
            "models": [m.as_dict() for m in self.models],
            "recommended": self.recommended,
            "error": self.error,
            "live": self.live,
        }


def inspect_key(key: str, provider_id: str | None = None, api_base: str | None = None) -> KeyReport:
    """Work out what a pasted key is, and what it can run.

    A failure here is a normal outcome, not an exception: a typo, an expired
    key and a rate-limited key are all things the person pasting it needs to
    read, so they come back as text rather than a stack trace.
    """
    key = (key or "").strip()
    if not key:
        return KeyReport(ok=False, error="paste a key first")

    pid = provider_id or detect(key)
    if pid is None:
        return KeyReport(
            ok=False,
            error="this key does not match any vendor we recognise. Pick the vendor by hand.",
        )
    provider = PROVIDERS.get(pid)
    if provider is None:
        return KeyReport(ok=False, error=f"unknown provider: {pid}")

    report = KeyReport(
        ok=False,
        provider=pid,
        provider_label=provider.label,
        detected=provider_id is None and not ambiguous(key),
    )
    try:
        models = list_models(pid, key, api_base)
        report.live = True
    except ProviderError as exc:
        report.error = str(exc)
        return report

    ordered = rank(models, "strong")
    if not ordered:
        report.error = "the key works, but exposes no model that could take a turn"
        return report

    report.ok = True
    report.models = ordered
    report.recommended = ordered[0].id
    return report
