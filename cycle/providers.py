"""Multi-provider LLM abstraction — one text-completion interface, six backends.

Anthropic remains the full-featured path (structured outputs, tools, caching —
see cycle/llm.py); the providers here give the router cheaper/faster/free-tier
alternatives for paper-mode decisions. All use stdlib urllib (no new deps).
API keys come exclusively from the environment (.env supported); they are
never logged, never echoed in errors, and never read from config files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_TOKENS = 4096

PROVIDER_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "cohere": "COHERE_API_KEY",
}


class ProviderError(Exception):
    """Provider call failed. status_code (if any) drives rate-limit handling."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def provider_has_key(provider: str) -> bool:
    env_name = PROVIDER_ENV_KEYS.get(provider)
    return bool(env_name and os.environ.get(env_name))


def available_providers() -> list[str]:
    return [name for name in PROVIDER_ENV_KEYS if provider_has_key(name)]


def _post_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # Read the error body for diagnostics but never include headers
        # (which carry the API key) in the raised message.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except OSError:
            pass
        raise ProviderError(
            f"HTTP {exc.code} from provider: {detail}", status_code=exc.code
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise ProviderError(f"Provider request failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Per-provider request builders (system + user prompt → text)
# ---------------------------------------------------------------------------

def _openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
    *,
    post: Callable[..., dict[str, Any]],
    tokens_field: str = "max_tokens",
) -> str:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        tokens_field: max_tokens,
        "response_format": {"type": "json_object"},
    }
    payload = post(
        f"{base_url}/chat/completions",
        {"Authorization": f"Bearer {api_key}"},
        body,
        timeout,
    )
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Unexpected response shape: {exc}") from exc


def _complete_openai(model, system, user, max_tokens, timeout, post) -> str:
    return _openai_compatible(
        "https://api.openai.com/v1",
        os.environ["OPENAI_API_KEY"],
        model, system, user, max_tokens, timeout,
        post=post,
        tokens_field="max_completion_tokens",
    )


def _complete_groq(model, system, user, max_tokens, timeout, post) -> str:
    return _openai_compatible(
        "https://api.groq.com/openai/v1",
        os.environ["GROQ_API_KEY"],
        model, system, user, max_tokens, timeout,
        post=post,
    )


def _complete_mistral(model, system, user, max_tokens, timeout, post) -> str:
    return _openai_compatible(
        "https://api.mistral.ai/v1",
        os.environ["MISTRAL_API_KEY"],
        model, system, user, max_tokens, timeout,
        post=post,
    )


def _complete_gemini(model, system, user, max_tokens, timeout, post) -> str:
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    payload = post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        # Header auth — keeps the key out of URLs, logs, and error traces.
        {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        body,
        timeout,
    )
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(part.get("text", "") for part in parts)
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Unexpected response shape: {exc}") from exc


def _complete_cohere(model, system, user, max_tokens, timeout, post) -> str:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    payload = post(
        "https://api.cohere.com/v2/chat",
        {"Authorization": f"Bearer {os.environ['COHERE_API_KEY']}"},
        body,
        timeout,
    )
    try:
        blocks = payload["message"]["content"]
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"Unexpected response shape: {exc}") from exc


def _complete_anthropic(model, system, user, max_tokens, timeout, post) -> str:
    """Plain-text Anthropic completion (used for triage; decisions use cycle/llm.py)."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    payload = post(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
        body,
        timeout,
    )
    try:
        return "".join(
            block.get("text", "")
            for block in payload["content"]
            if block.get("type") == "text"
        )
    except (KeyError, TypeError) as exc:
        raise ProviderError(f"Unexpected response shape: {exc}") from exc


_COMPLETERS: dict[str, Callable[..., str]] = {
    "openai": _complete_openai,
    "groq": _complete_groq,
    "mistral": _complete_mistral,
    "gemini": _complete_gemini,
    "cohere": _complete_cohere,
    "anthropic": _complete_anthropic,
}


async def complete_text(
    provider: str,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    post: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Run a system+user completion on any supported provider, returning text.

    `post` is injectable for tests; production uses the stdlib HTTP layer.
    """
    completer = _COMPLETERS.get(provider)
    if completer is None:
        raise ProviderError(f"Unknown provider: {provider}")
    if not provider_has_key(provider):
        raise ProviderError(
            f"No API key for provider {provider} "
            f"(set {PROVIDER_ENV_KEYS[provider]})"
        )

    return await asyncio.to_thread(
        completer, model, system, user, max_tokens, timeout, post or _post_json
    )
