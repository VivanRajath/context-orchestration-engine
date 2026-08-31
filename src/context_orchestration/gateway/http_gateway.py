"""A model gateway with no dependencies at all.

``LiteLLMGateway`` remains the right tool on a laptop: it speaks to roughly
every provider in existence and absorbs their differences for free. It is the
wrong tool inside a serverless function, where it and its transitive
dependencies are a large fraction of the whole deployment budget, and where
almost none of that breadth is ever exercised.

So this is the same contract over the standard library. Nearly every vendor
now serves the OpenAI request shape, which reduces "support a new provider" to
"know its base URL" - see ``providers.PROVIDERS``. Anthropic is the one that
does not, and it gets a small adapter below rather than a special case
anywhere upstream.

What matters architecturally is that this file is interchangeable with
``LiteLLMGateway``. Both satisfy ``LLMGateway``, so the orchestrator cannot
tell which one is running, which is exactly the property the playground is
built to demonstrate: swap the vendor, swap the transport, swap the model, and
the execution state crossing between workers is unchanged.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from context_orchestration.core.contracts import WorkerConfig
from context_orchestration.gateway import providers
from context_orchestration.gateway.llm_gateway import GatewayError, GatewayResponse


class HTTPGateway:
    """Real provider access over plain HTTPS."""

    def __init__(
        self,
        timeout: int = 120,
        rate_limit_retries: int = 3,
        sleep=time.sleep,
    ) -> None:
        self.timeout = timeout
        self.rate_limit_retries = rate_limit_retries
        self._sleep = sleep

    # -- resolution ------------------------------------------------------

    def _resolve_key(self, config: WorkerConfig) -> str | None:
        if config.api_key:
            return config.api_key
        if config.api_key_env:
            import os

            return os.environ.get(config.api_key_env)
        return None

    def _resolve_provider(self, config: WorkerConfig) -> tuple[providers.Provider, str]:
        """Which vendor to call, and under what model name.

        The worker config may name the vendor outright, or carry it in the
        model string the way LiteLLM writes it. Failing both, the key's own
        prefix decides, which is how a roster assembled in the browser from a
        single pasted key resolves without anyone naming a vendor at all.
        """
        pid = str(config.provider_config.get("provider") or "") or None
        exact = str(config.provider_config.get("model_id") or "")
        if pid and exact:
            # The id the vendor itself published, carried through untouched.
            # Deriving it back out of the display string cannot be done
            # safely: Groq publishes a model called "groq/compound", so the
            # leading segment of a model id is not reliably a vendor at all.
            return providers.PROVIDERS[pid], exact
        if pid:
            # A named vendor is authoritative: strip its prefix and nobody
            # else's, so a model whose own name begins with another vendor's
            # survives intact.
            bare = providers.strip_vendor(config.model, pid)
        else:
            vendor, bare = providers.split_model(config.model)
            pid = vendor or providers.detect(self._resolve_key(config) or "")
        if pid is None or pid not in providers.PROVIDERS:
            raise GatewayError(
                f"cannot tell which provider {config.id} belongs to. "
                f"Prefix the model with a vendor, as in 'groq/{config.model}'."
            )
        return providers.PROVIDERS[pid], bare

    def _base(self, config: WorkerConfig, provider: providers.Provider) -> str:
        base = (config.api_base or provider.base_url or "").rstrip("/")
        if not base:
            raise GatewayError(f"no endpoint configured for {config.id}. Give it a base URL.")
        return base

    # -- the contract ----------------------------------------------------

    def complete(
        self,
        config: WorkerConfig,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any] | None = None,
    ) -> GatewayResponse:
        provider, model = self._resolve_provider(config)
        key = self._resolve_key(config)
        if not key:
            raise GatewayError(f"no API key resolved for {config.id}")
        base = self._base(config, provider)

        if provider.auth == "anthropic":
            url, body, headers = self._anthropic(base, model, config, messages, key)
            unwrap = self._unwrap_anthropic
            variants: list[dict[str, Any]] = [body]
        else:
            url, body, headers = self._openai(base, model, config, messages, key)
            unwrap = self._unwrap_openai
            variants = self._schema_variants(body, json_schema)

        last: Exception | None = None
        for variant in variants:
            try:
                payload = self._post(url, variant, headers)
            except _RateLimited as exc:
                # Retrying a different response_format cannot fix a quota.
                last = exc
                break
            except GatewayError as exc:
                last = exc
                continue
            return unwrap(payload, config)

        raise GatewayError(f"model call failed for {config.id} ({config.model}): {last}")

    # -- request shapes --------------------------------------------------

    def _openai(self, base, model, config, messages, key):
        body = {
            "model": model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
        }
        # Anything else on provider_config is a passthrough for the vendor;
        # the two keys the gateway uses for routing are not.
        body.update(
            {
                k: v
                for k, v in config.provider_config.items()
                if k not in {"provider", "model_id"}
            }
        )
        return (
            base + "/chat/completions",
            body,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )

    def _anthropic(self, base, model, config, messages, key):
        # Anthropic takes the system prompt beside the conversation rather than
        # inside it. Everything else is close enough to fold in here.
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        turns = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in {"user", "assistant"}
        ]
        body: dict[str, Any] = {
            "model": model,
            "messages": turns or [{"role": "user", "content": " "}],
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        if system:
            body["system"] = system
        return (
            base + "/messages",
            body,
            {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )

    def _schema_variants(self, body: dict, json_schema: dict | None) -> list[dict]:
        """Ask for structured output, then settle for less.

        Support for these two response formats varies by vendor and by model
        within a vendor, and a model that does not support one answers with a
        400 rather than ignoring it. Trying the strictest first and falling
        back costs one wasted request on the models that refuse, and buys
        schema enforcement on every model that does not.
        """
        if json_schema is None:
            return [body]
        return [
            {
                **body,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "worker_output",
                        "schema": json_schema,
                        "strict": False,
                    },
                },
            },
            {**body, "response_format": {"type": "json_object"}},
            body,
        ]

    # -- transport -------------------------------------------------------

    def _post(self, url: str, body: dict, headers: dict[str, str]) -> dict:
        data = json.dumps(body).encode("utf-8")
        headers = {**headers, "User-Agent": providers.USER_AGENT}
        for attempt in range(self.rate_limit_retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                text = ""
                try:
                    text = exc.read().decode("utf-8", "replace")
                except Exception:
                    pass
                if exc.code == 429 and attempt < self.rate_limit_retries:
                    # A rate limit is a scheduling problem, not a failure. The
                    # provider usually says how long to wait; believe it.
                    self._sleep(_retry_after(exc, text, attempt))
                    continue
                if exc.code == 429:
                    raise _RateLimited(_message(text) or "rate limited") from exc
                raise GatewayError(_message(text) or f"HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise GatewayError(f"could not reach the provider: {exc.reason}") from exc
            except json.JSONDecodeError as exc:
                raise GatewayError("the provider did not answer with JSON") from exc
        raise AssertionError("unreachable")

    # -- responses -------------------------------------------------------

    def _unwrap_openai(self, payload: dict, config: WorkerConfig) -> GatewayResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise GatewayError(f"{config.id}: the provider returned no choices")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
        return GatewayResponse(
            text=text, model=config.model, usage=_usage(payload.get("usage")), raw=payload
        )

    def _unwrap_anthropic(self, payload: dict, config: WorkerConfig) -> GatewayResponse:
        blocks = payload.get("content") or []
        text = "".join(
            b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        usage = payload.get("usage") or {}
        return GatewayResponse(
            text=text,
            model=config.model,
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            },
            raw=payload,
        )


class _RateLimited(GatewayError):
    """Out of quota, as opposed to unable to answer."""


def _usage(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        "prompt_tokens": raw.get("prompt_tokens", 0),
        "completion_tokens": raw.get("completion_tokens", 0),
        "total_tokens": raw.get("total_tokens", 0),
    }


def _message(body: str) -> str:
    """The provider's own explanation, which is the useful half of an error."""
    try:
        data = json.loads(body)
    except Exception:
        return " ".join(body.split())[:300]
    err = data.get("error", data) if isinstance(data, dict) else data
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "")[:300]
    return str(err)[:300]


_TRY_AGAIN = re.compile(r"try again in ([0-9.]+)\s*(ms|s|m)?", re.I)


def _retry_after(exc: urllib.error.HTTPError, body: str, attempt: int) -> float:
    """How long to wait, preferring whatever the provider actually said.

    The header is the documented answer and several providers omit it, putting
    the same number in the error text instead. Backing off exponentially when
    the provider has told you 3.75 seconds is both slower and ruder.
    """
    header = exc.headers.get("retry-after") if exc.headers else None
    if header:
        try:
            return min(float(header) + 0.5, 60.0)
        except ValueError:
            pass
    match = _TRY_AGAIN.search(body or "")
    if match:
        try:
            seconds = float(match.group(1))
            unit = (match.group(2) or "s").lower()
            seconds = seconds / 1000 if unit == "ms" else seconds * 60 if unit == "m" else seconds
            return min(seconds + 0.5, 60.0)
        except ValueError:
            pass
    return min(2.0**attempt, 60.0)
