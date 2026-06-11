"""Tests for the multi-provider layer and the agentic LLM router (no network)."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cycle.providers as providers
import cycle.router as router
from cycle.providers import ProviderError, complete_text
from cycle.router import (
    build_triage_summary,
    decide_with_routing,
    in_cooldown,
    record_failure,
    record_success,
    route_decision,
)

ALL_KEYS = {
    "ANTHROPIC_API_KEY": "k1", "OPENAI_API_KEY": "k2", "GROQ_API_KEY": "k3",
    "MISTRAL_API_KEY": "k4", "GEMINI_API_KEY": "k5", "COHERE_API_KEY": "k6",
}

ROUTER_CONFIG = {
    "enabled": True,
    "triage_provider": "groq",
    "triage_model": "test-triage",
    "default_provider": "anthropic",
    "fallback_order": ["anthropic"],
    "live_approved": ["anthropic"],
    "providers": {
        "anthropic": {"model": "claude-opus-4-8"},
        "groq": {"model": "test-groq"},
        "gemini": {"model": "test-gemini"},
    },
}

HOLD_JSON = (
    '{"action": "HOLD", "pair": "EURUSD", "direction": null, "order_type": "BUY_LIMIT", '
    '"lot_size": 0.0, "entry_price": null, "entry_window": null, "stop_loss": null, '
    '"take_profit": null, "reasoning": "flat", "confidence": "LOW", "cycle_id": "t"}'
)


@pytest.fixture
def all_keys(monkeypatch):
    for name, value in ALL_KEYS.items():
        monkeypatch.setenv(name, value)


# ---------------------------------------------------------------------------
# Provider request/response shapes
# ---------------------------------------------------------------------------

def _post_capture(response):
    calls = []

    def post(url, headers, body, timeout):
        calls.append({"url": url, "headers": headers, "body": body})
        return response

    post.calls = calls
    return post


def test_openai_compatible_shape(all_keys):
    post = _post_capture({"choices": [{"message": {"content": "{\"x\": 1}"}}]})
    text = asyncio.run(complete_text(
        "groq", "test-model", "SYS", "USER", post=post,
    ))
    assert text == '{"x": 1}'
    call = post.calls[0]
    assert call["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer k3"
    assert call["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert call["body"]["response_format"] == {"type": "json_object"}


def test_openai_uses_max_completion_tokens(all_keys):
    post = _post_capture({"choices": [{"message": {"content": "{}"}}]})
    asyncio.run(complete_text("openai", "test", "S", "U", max_tokens=123, post=post))
    assert post.calls[0]["body"]["max_completion_tokens"] == 123
    assert "max_tokens" not in post.calls[0]["body"]


def test_gemini_shape_uses_header_auth(all_keys):
    post = _post_capture(
        {"candidates": [{"content": {"parts": [{"text": "{\"y\": 2}"}]}}]}
    )
    text = asyncio.run(complete_text("gemini", "test-gem", "SYS", "USER", post=post))
    assert text == '{"y": 2}'
    call = post.calls[0]
    assert call["headers"]["x-goog-api-key"] == "k5"
    assert "key=" not in call["url"]  # key never in URL
    assert call["body"]["generationConfig"]["responseMimeType"] == "application/json"


def test_cohere_shape(all_keys):
    post = _post_capture(
        {"message": {"content": [{"type": "text", "text": "{\"z\": 3}"}]}}
    )
    text = asyncio.run(complete_text("cohere", "cmd", "SYS", "USER", post=post))
    assert text == '{"z": 3}'
    assert post.calls[0]["url"] == "https://api.cohere.com/v2/chat"


def test_anthropic_rest_shape(all_keys):
    post = _post_capture({"content": [{"type": "text", "text": "ok"}]})
    text = asyncio.run(complete_text("anthropic", "claude-opus-4-8", "S", "U", post=post))
    assert text == "ok"
    assert post.calls[0]["headers"]["x-api-key"] == "k1"


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="No API key"):
        asyncio.run(complete_text("groq", "m", "s", "u"))


def test_unknown_provider_raises(all_keys):
    with pytest.raises(ProviderError, match="Unknown provider"):
        asyncio.run(complete_text("notreal", "m", "s", "u"))


# ---------------------------------------------------------------------------
# Cooldown state
# ---------------------------------------------------------------------------

def test_cooldown_lifecycle():
    state = {}
    now = time.time()
    record_failure(state, "groq", rate_limited=True, cooldown_seconds=900, now=now)
    assert in_cooldown(state, "groq", now=now + 100)
    assert not in_cooldown(state, "groq", now=now + 1000)
    record_success(state, "groq")
    assert not in_cooldown(state, "groq", now=now + 100)
    # non-rate-limit failures count but don't cool down
    record_failure(state, "gemini", rate_limited=False, now=now)
    assert not in_cooldown(state, "gemini", now=now)
    assert state["gemini"]["consecutive_failures"] == 1


# ---------------------------------------------------------------------------
# Triage routing
# ---------------------------------------------------------------------------

def _triage_response(monkeypatch, payload):
    async def fake_complete(provider, model, system, user, **kwargs):
        fake_complete.prompts.append(json.loads(user))
        return json.dumps(payload)

    fake_complete.prompts = []
    monkeypatch.setattr(router, "complete_text", fake_complete)
    return fake_complete


def test_route_accepts_valid_pick(all_keys, monkeypatch):
    _triage_response(monkeypatch, {
        "complexity": "routine", "provider": "gemini", "reason": "quiet cycle",
    })
    routing = asyncio.run(route_decision(
        {"open_positions": 0}, router_config=ROUTER_CONFIG,
        provider_state={}, live_mode=False,
    ))
    assert routing["provider"] == "gemini"
    assert routing["complexity"] == "routine"
    assert routing["routed_by"] == "triage"


def test_route_live_mode_clamps_to_approved(all_keys, monkeypatch):
    fake = _triage_response(monkeypatch, {
        "complexity": "critical", "provider": "groq", "reason": "fast",
    })
    routing = asyncio.run(route_decision(
        {"open_positions": 1}, router_config=ROUTER_CONFIG,
        provider_state={}, live_mode=True,
    ))
    assert routing["provider"] == "anthropic"
    assert routing["routed_by"] == "sanitizer"
    assert "not approved for live mode" in routing["reason"]
    # The triage agent was only offered live-approved candidates
    assert fake.prompts[0]["available_providers"] == ["anthropic"]


def test_route_skips_cooldown_provider(all_keys, monkeypatch):
    _triage_response(monkeypatch, {
        "complexity": "standard", "provider": "gemini", "reason": "x",
    })
    state = {}
    record_failure(state, "gemini", rate_limited=True)
    routing = asyncio.run(route_decision(
        {}, router_config=ROUTER_CONFIG, provider_state=state, live_mode=False,
    ))
    assert routing["provider"] == "anthropic"
    assert "cooldown" in routing["reason"]


def test_route_rejects_unknown_provider(all_keys, monkeypatch):
    _triage_response(monkeypatch, {
        "complexity": "standard", "provider": "skynet", "reason": "x",
    })
    routing = asyncio.run(route_decision(
        {}, router_config=ROUTER_CONFIG, provider_state={}, live_mode=False,
    ))
    assert routing["provider"] == "anthropic"
    assert "not in registry" in routing["reason"]


def test_route_triage_failure_falls_back(all_keys, monkeypatch):
    async def boom(*args, **kwargs):
        raise ProviderError("rate limited", status_code=429)

    monkeypatch.setattr(router, "complete_text", boom)
    state = {}
    routing = asyncio.run(route_decision(
        {}, router_config=ROUTER_CONFIG, provider_state=state, live_mode=False,
    ))
    assert routing["provider"] == "anthropic"
    assert routing["routed_by"] == "fallback"
    assert in_cooldown(state, "groq")  # triage 429 recorded


def test_route_without_triage_key_uses_default(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    routing = asyncio.run(route_decision(
        {}, router_config=ROUTER_CONFIG, provider_state={}, live_mode=False,
    ))
    assert routing["provider"] == "anthropic"
    assert "unavailable" in routing["reason"]


# ---------------------------------------------------------------------------
# Decision execution + fallback chain
# ---------------------------------------------------------------------------

def test_decide_on_alternate_provider(all_keys, monkeypatch):
    async def fake_complete(provider, model, system, user, **kwargs):
        assert provider == "gemini"
        return HOLD_JSON

    monkeypatch.setattr(router, "complete_text", fake_complete)
    decision, meta = asyncio.run(decide_with_routing(
        playbook="PB", user_prompt="UP",
        routing={"provider": "gemini", "complexity": "routine", "reason": "r"},
        router_config=ROUTER_CONFIG, anthropic_config={}, provider_state={},
    ))
    assert decision["action"] == "HOLD"
    assert meta["decided_by"] == "gemini"
    assert meta["attempts"][-1]["outcome"] == "ok"


def test_decide_falls_back_to_anthropic_on_failure(all_keys, monkeypatch):
    async def failing_complete(provider, model, system, user, **kwargs):
        raise ProviderError("boom", status_code=429)

    async def fake_invoke(**kwargs):
        return json.loads(HOLD_JSON)

    monkeypatch.setattr(router, "complete_text", failing_complete)
    monkeypatch.setattr(router, "invoke_claude", fake_invoke)

    state = {}
    decision, meta = asyncio.run(decide_with_routing(
        playbook="PB", user_prompt="UP",
        routing={"provider": "gemini", "complexity": "standard", "reason": "r"},
        router_config=ROUTER_CONFIG, anthropic_config={"model": "claude-opus-4-8"},
        provider_state=state,
    ))
    assert decision["action"] == "HOLD"
    assert meta["decided_by"] == "anthropic"
    assert any("failed" in a["outcome"] for a in meta["attempts"])
    assert in_cooldown(state, "gemini")  # 429 → cooldown recorded


def test_decide_raises_when_all_fail(all_keys, monkeypatch):
    async def failing_complete(provider, model, system, user, **kwargs):
        raise ProviderError("down")

    async def failing_invoke(**kwargs):
        raise RuntimeError("anthropic down too")

    monkeypatch.setattr(router, "complete_text", failing_complete)
    monkeypatch.setattr(router, "invoke_claude", failing_invoke)

    with pytest.raises(RuntimeError, match="All providers"):
        asyncio.run(decide_with_routing(
            playbook="PB", user_prompt="UP",
            routing={"provider": "gemini", "complexity": "standard", "reason": "r"},
            router_config=ROUTER_CONFIG, anthropic_config={}, provider_state={},
        ))


# ---------------------------------------------------------------------------
# Triage summary
# ---------------------------------------------------------------------------

def test_triage_summary_is_compact_and_credential_free():
    market_state = {
        "account": {"login": 12345, "balance": 10_000, "equity": 9_990},
        "positions": [{"symbol": "EURUSD", "ticket": 1}],
        "pending_orders": [],
        "ticks": {"EURUSD": {"tick": {"bid": 1.08}}},
    }
    veto = {"blocked": True, "checks": [
        {"name": "Trading hours", "pass": True},
        {"name": "Spread EURUSD <= 2.0 pips", "pass": False},
    ]}
    summary = build_triage_summary(
        market_state, veto, {"EURUSD": ["rsi"]},
        {"EURUSD": {"force_exit": True}}, live_mode=False,
    )
    assert summary["open_positions"] == 1
    assert summary["failed_veto_checks"] == ["Spread EURUSD <= 2.0 pips"]
    assert summary["exit_signals"] == {"EURUSD": True}
    text = json.dumps(summary)
    assert "12345" not in text and "balance" not in text  # no account details
