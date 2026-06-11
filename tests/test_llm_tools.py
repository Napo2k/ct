"""Tests for the LLM tool loop, structured-output request shape, and verifier wiring."""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.llm import (
    DECISION_SCHEMA,
    READ_ONLY_TOOLS,
    LLMError,
    _build_request_kwargs,
    _execute_read_only_tool,
    invoke_claude,
)


class FakeMT5:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool, args=None):
        self.calls.append((tool, args or {}))
        return {"success": True, "tool": tool}


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------

def test_request_includes_structured_output_and_cache():
    kwargs = _build_request_kwargs(
        playbook="PLAYBOOK",
        model="claude-opus-4-8",
        max_tokens=1000,
        messages=[{"role": "user", "content": "hi"}],
        use_structured_output=True,
        cache_playbook=True,
        tools=None,
    )
    assert kwargs["output_config"]["format"]["schema"] == DECISION_SCHEMA
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "tools" not in kwargs


def test_request_without_cache_or_schema():
    kwargs = _build_request_kwargs(
        playbook="PLAYBOOK",
        model="m",
        max_tokens=10,
        messages=[],
        use_structured_output=False,
        cache_playbook=False,
        tools=READ_ONLY_TOOLS,
    )
    assert "output_config" not in kwargs
    assert "cache_control" not in kwargs["system"][0]
    assert len(kwargs["tools"]) == len(READ_ONLY_TOOLS)


def test_no_write_tools_exposed():
    names = {t["name"] for t in READ_ONLY_TOOLS}
    forbidden = {"place_order", "modify_position", "modify_order", "close_position", "cancel_order"}
    assert not (names & forbidden)


# ---------------------------------------------------------------------------
# Tool execution allowlist
# ---------------------------------------------------------------------------

def test_execute_rejects_unlisted_tool():
    mt5 = FakeMT5()
    result = asyncio.run(_execute_read_only_tool(mt5, "place_order", {"symbol": "EURUSD"}))
    assert "not permitted" in result["error"]
    assert mt5.calls == []


def test_execute_caps_get_rates_count():
    mt5 = FakeMT5()
    asyncio.run(_execute_read_only_tool(mt5, "get_rates", {"symbol": "EURUSD", "timeframe": "H1", "count": 99999}))
    assert mt5.calls[0][1]["count"] == 200


def test_execute_surfaces_tool_errors():
    class BoomMT5:
        async def call_tool(self, tool, args=None):
            raise RuntimeError("socket closed")

    result = asyncio.run(_execute_read_only_tool(BoomMT5(), "get_tick", {"symbol": "EURUSD"}))
    assert "socket closed" in result["error"]


# ---------------------------------------------------------------------------
# Tool loop (fake anthropic client)
# ---------------------------------------------------------------------------

def _block(**attrs):
    return types.SimpleNamespace(**attrs)


DECISION_JSON = (
    '{"action": "HOLD", "pair": "EURUSD", "direction": null, "order_type": "BUY_LIMIT", '
    '"lot_size": 0.0, "entry_price": null, "entry_window": null, "stop_loss": null, '
    '"take_profit": null, "reasoning": "No edge.", "confidence": "LOW", "cycle_id": "t"}'
)


def _install_fake_anthropic(monkeypatch, responses):
    """Install a fake anthropic module whose create() pops canned responses."""
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = types.SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    fake = types.ModuleType("anthropic")
    fake.Anthropic = FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    return calls


def test_tool_loop_executes_then_returns_decision(monkeypatch):
    tool_round = types.SimpleNamespace(
        stop_reason="tool_use",
        content=[_block(type="tool_use", name="get_rates", id="tu_1",
                        input={"symbol": "EURUSD", "timeframe": "H1", "count": 50})],
    )
    final = types.SimpleNamespace(
        stop_reason="end_turn",
        content=[_block(type="text", text=DECISION_JSON)],
    )
    calls = _install_fake_anthropic(monkeypatch, [tool_round, final])
    mt5 = FakeMT5()

    decision = asyncio.run(invoke_claude(
        playbook="PB", user_prompt="prompt", mt5=mt5, enable_tools=True,
        max_retries=1, retry_base_delay=0,
    ))
    assert decision["action"] == "HOLD"
    assert mt5.calls[0][0] == "get_rates"
    # Second API call carries the assistant turn + tool result
    assert len(calls) == 2
    roles = [m["role"] for m in calls[1]["messages"]]
    assert roles == ["user", "assistant", "user"]


def test_tool_loop_enforces_round_cap(monkeypatch):
    def tool_round(i):
        return types.SimpleNamespace(
            stop_reason="tool_use",
            content=[_block(type="tool_use", name="get_tick", id=f"tu_{i}",
                            input={"symbol": "EURUSD"})],
        )

    # Model never stops calling tools → loop must raise, not spin forever
    responses = [tool_round(i) for i in range(10)]
    _install_fake_anthropic(monkeypatch, responses)
    mt5 = FakeMT5()

    with pytest.raises(LLMError):
        asyncio.run(invoke_claude(
            playbook="PB", user_prompt="prompt", mt5=mt5, enable_tools=True,
            max_tool_rounds=2, max_retries=1, retry_base_delay=0,
        ))
    # After the cap, tool calls get error results instead of being executed
    assert len(mt5.calls) == 2


def test_single_shot_path_unchanged_without_tools(monkeypatch):
    final = types.SimpleNamespace(
        stop_reason="end_turn",
        content=[_block(type="text", text=DECISION_JSON)],
    )
    calls = _install_fake_anthropic(monkeypatch, [final])
    decision = asyncio.run(invoke_claude(
        playbook="PB", user_prompt="prompt", max_retries=1, retry_base_delay=0,
    ))
    assert decision["action"] == "HOLD"
    assert len(calls) == 1
    assert "tools" not in calls[0]


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

def test_verifier_refutes_blocks_entry(monkeypatch):
    import cycle.verifier as verifier_mod

    verdict_msg = types.SimpleNamespace(
        content=[_block(type="text", text='{"refuted": true, "reason": "ADX below 20"}')],
    )
    _install_fake_anthropic(monkeypatch, [verdict_msg])

    result = asyncio.run(verifier_mod.verify_entry(
        {"action": "ENTER", "pair": "EURUSD"}, {"indicators": {}}, {"blocked": False},
    ))
    assert result["refuted"] is True
    assert result["approved"] is False
    assert "ADX" in result["reason"]


def test_verifier_approves(monkeypatch):
    verdict_msg = types.SimpleNamespace(
        content=[_block(type="text", text='{"refuted": false, "reason": "Entry is consistent"}')],
    )
    _install_fake_anthropic(monkeypatch, [verdict_msg])

    import cycle.verifier as verifier_mod
    result = asyncio.run(verifier_mod.verify_entry(
        {"action": "ENTER", "pair": "EURUSD"}, {"indicators": {}}, {"blocked": False},
    ))
    assert result["approved"] is True


def test_verifier_error_reports_not_approved(monkeypatch):
    class FailingClient:
        def __init__(self, **kwargs):
            self.messages = types.SimpleNamespace(create=self._create)

        def _create(self, **kwargs):
            raise RuntimeError("api down")

    fake = types.ModuleType("anthropic")
    fake.Anthropic = FailingClient
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    import cycle.verifier as verifier_mod
    result = asyncio.run(verifier_mod.verify_entry(
        {"action": "ENTER", "pair": "EURUSD"}, {}, {},
    ))
    assert result["approved"] is False
    assert result["refuted"] is False
    assert "api down" in result["error"]
