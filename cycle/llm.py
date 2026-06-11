"""Claude reasoning layer — invokes Anthropic API with playbook system prompt."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_MAX_TOOL_ROUNDS = 5

# JSON schema enforced via structured outputs (output_config.format) so the
# model cannot return malformed decision JSON. Mirrors decision.validate_decision,
# which still runs afterwards for semantic checks (R:R geometry, etc.).
DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["ENTER", "EXIT", "MODIFY", "HOLD", "SUSPEND"]},
        "pair": {"type": "string"},
        "direction": {"anyOf": [{"type": "string", "enum": ["LONG", "SHORT"]}, {"type": "null"}]},
        "order_type": {
            "type": "string",
            "enum": ["BUY_LIMIT", "SELL_LIMIT", "BUY_STOP", "SELL_STOP", "BUY", "SELL"],
        },
        "lot_size": {"type": "number"},
        "entry_price": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "entry_window": {
            "anyOf": [{"type": "array", "items": {"type": "number"}}, {"type": "null"}]
        },
        "stop_loss": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "take_profit": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "reasoning": {"type": "string"},
        "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
        "cycle_id": {"type": "string"},
    },
    "required": [
        "action", "pair", "direction", "order_type", "lot_size", "entry_price",
        "entry_window", "stop_loss", "take_profit", "reasoning", "confidence", "cycle_id",
    ],
    "additionalProperties": False,
}

# Read-only MT5 tools the model may call while reasoning. Write tools
# (place_order, modify_*, close_*, cancel_*) are NEVER exposed here — all
# writes go through the deterministic executor with risk guards.
READ_ONLY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_rates",
        "description": (
            "Fetch recent OHLCV bars for a symbol and timeframe. Use this to "
            "inspect price structure beyond the indicator snapshot — e.g. recent "
            "swing highs/lows, candle patterns, or a correlated pair's trend."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "e.g. EURUSD"},
                "timeframe": {"type": "string", "enum": ["M15", "H1", "H4", "D1"]},
                "count": {"type": "integer", "description": "Number of bars, max 200"},
            },
            "required": ["symbol", "timeframe"],
        },
    },
    {
        "name": "get_tick",
        "description": "Fetch the live bid/ask for a symbol to confirm current price.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_symbol_info",
        "description": "Fetch broker symbol specs (digits, lot limits, spread).",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_open_positions",
        "description": "List currently open positions.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": [],
        },
    },
    {
        "name": "get_pending_orders",
        "description": "List pending orders.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": [],
        },
    },
]
_READ_ONLY_TOOL_NAMES = {tool["name"] for tool in READ_ONLY_TOOLS}
_MAX_TOOL_RESULT_CHARS = 8000
DEFAULT_MAX_RETRIES = 3
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_RETRY_BASE_DELAY = 1.0

# Exception class names / substrings treated as transient (worth retrying).
_RETRYABLE_NAME_HINTS = (
    "ratelimit",
    "overloaded",
    "apiconnection",
    "apitimeout",
    "timeout",
    "internalserver",
    "serviceunavailable",
)
_RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMError(Exception):
    pass


def load_playbook(path, lessons_path=None) -> str:
    with open(path, encoding="utf-8") as handle:
        playbook = handle.read()
    if lessons_path:
        try:
            with open(lessons_path, encoding="utf-8") as handle:
                lessons = handle.read().strip()
            if lessons:
                playbook = f"{playbook.rstrip()}\n\n{lessons}\n"
        except OSError:
            pass
    return playbook


def build_user_prompt(
    cycle_id: str,
    pairs: list[str],
    market_state: dict[str, Any],
    veto_result: dict[str, Any],
    warm_reasons: dict[str, list[str]],
    execution_mode: bool,
    session: dict[str, Any] | None = None,
    exit_signals: dict[str, Any] | None = None,
    live_mode: bool = False,
) -> str:
    if live_mode:
        phase = 2
        exec_note = (
            "Phase 2: LIVE TRADING — decisions WILL be executed with REAL MONEY. "
            "Only ENTER with HIGH confidence and all 5 checklist items passing; "
            "lower-confidence entries are programmatically downgraded to HOLD. "
            "Default to BUY_LIMIT/SELL_LIMIT. Market orders require entry_window. "
            "When in doubt, HOLD — missing a trade costs nothing, a bad trade costs capital. "
            "Respect session lot_multiplier if consecutive_losses >= 3."
        )
    elif execution_mode:
        phase = 1
        exec_note = (
            "Phase 1: EXECUTION_MODE is true — decisions WILL be executed on the demo account. "
            "Default to BUY_LIMIT/SELL_LIMIT. Market orders require entry_window. "
            "Respect session lot_multiplier if consecutive_losses >= 3."
        )
    else:
        phase = 0
        exec_note = (
            "Phase 0: EXECUTION_MODE is false — recommend only, no trades will be placed."
        )

    payload = {
        "cycle_id": cycle_id,
        "phase": phase,
        "execution_mode": execution_mode,
        "session": session or {},
        "pairs_to_evaluate": pairs,
        "warm_signal_reasons": warm_reasons,
        "exit_signals": exit_signals or {},
        "veto_checks": veto_result,
        "market_state": market_state,
        "instructions": (
            "Evaluate the market state and produce ONE decision JSON object. "
            "If no action is warranted, return HOLD for the primary pair (EURUSD). "
            "If exit_signals lists a position with force_exit, prefer EXIT for that pair. "
            f"{exec_note} "
            "Respond with raw JSON only, no markdown fences."
        ),
    }
    return json.dumps(payload, indent=2, default=str)


def _build_request_kwargs(
    *,
    playbook: str,
    model: str,
    max_tokens: int,
    messages: list[dict[str, Any]],
    use_structured_output: bool,
    cache_playbook: bool,
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    system_block: dict[str, Any] = {"type": "text", "text": playbook}
    if cache_playbook:
        # The playbook is byte-stable across cycles; caching it cuts the per-cycle
        # input cost of the system prompt by ~90% once warm.
        system_block["cache_control"] = {"type": "ephemeral"}

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "thinking": {"type": "adaptive"},
        "system": [system_block],
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
    if use_structured_output:
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": DECISION_SCHEMA}
        }
    return kwargs


async def _execute_read_only_tool(mt5: Any, name: str, arguments: dict[str, Any]) -> Any:
    if name not in _READ_ONLY_TOOL_NAMES:
        return {"error": f"tool {name} is not permitted"}
    if name == "get_rates":
        arguments["count"] = min(int(arguments.get("count", 100)), 200)
    try:
        return await mt5.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001 — surface tool failure to the model
        return {"error": str(exc)}


async def invoke_claude(
    *,
    playbook: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    mt5: Any | None = None,
    enable_tools: bool = False,
    max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    use_structured_output: bool = True,
    cache_playbook: bool = True,
) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY environment variable not set")

    try:
        import anthropic
    except ImportError as exc:
        raise LLMError("anthropic package not installed — run: pip install anthropic") from exc

    # max_retries=0 disables the SDK's own retry loop so ours is the source of truth.
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    tools = READ_ONLY_TOOLS if (enable_tools and mt5 is not None) else None
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    if tools is None:
        def _create_message() -> Any:
            return client.messages.create(**_build_request_kwargs(
                playbook=playbook,
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                use_structured_output=use_structured_output,
                cache_playbook=cache_playbook,
                tools=None,
            ))

        return await _call_with_retries(
            _create_message,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
        )

    # Agentic loop: the model may investigate via read-only tools before deciding.
    rounds = 0
    while True:
        def _create_message() -> Any:
            return client.messages.create(**_build_request_kwargs(
                playbook=playbook,
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                use_structured_output=use_structured_output,
                cache_playbook=cache_playbook,
                tools=tools,
            ))

        message = await _raw_call_with_retries(
            _create_message,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
        )

        tool_uses = [
            block for block in getattr(message, "content", [])
            if getattr(block, "type", None) == "tool_use"
        ]
        if getattr(message, "stop_reason", None) != "tool_use" or not tool_uses:
            raw = _message_text(message)
            return _parse_json_response(raw)

        rounds += 1
        if rounds > max_tool_rounds + 1:
            raise LLMError(f"Tool loop exceeded {max_tool_rounds} rounds without a decision")

        messages.append({"role": "assistant", "content": message.content})
        results = []
        for block in tool_uses:
            if rounds > max_tool_rounds:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Tool budget exhausted — emit your final decision JSON now.",
                    "is_error": True,
                })
                continue
            payload = await _execute_read_only_tool(mt5, block.name, dict(block.input or {}))
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(payload, default=str)[:_MAX_TOOL_RESULT_CHARS],
            })
        messages.append({"role": "user", "content": results})


async def _raw_call_with_retries(
    create_message: Callable[[], Any],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
) -> Any:
    """Like _call_with_retries but returns the raw message (for the tool loop)."""
    attempts = max(1, max_retries)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(create_message)
        except Exception as exc:  # noqa: BLE001 — classify then re-raise as LLMError
            last_error = exc
            retryable = _is_retryable(exc)

        if not retryable or attempt == attempts:
            break

        delay = retry_base_delay * (2 ** (attempt - 1))
        logger.warning(
            "Claude call failed (attempt %d/%d): %s — retrying in %.1fs",
            attempt,
            attempts,
            last_error,
            delay,
        )
        await asyncio.sleep(delay)

    raise LLMError(f"Claude request failed after {attempts} attempt(s): {last_error}")


async def _call_with_retries(
    create_message: Callable[[], Any],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
) -> dict[str, Any]:
    """Run a blocking message-create callable off-thread with retry/backoff."""
    attempts = max(1, max_retries)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            message = await asyncio.to_thread(create_message)
            raw = _message_text(message)
            return _parse_json_response(raw)
        except LLMError as exc:
            # Includes invalid-JSON responses — a re-prompt may succeed.
            last_error = exc
            retryable = True
        except Exception as exc:  # noqa: BLE001 — classify then re-raise as LLMError
            last_error = exc
            retryable = _is_retryable(exc)

        if not retryable or attempt == attempts:
            break

        delay = retry_base_delay * (2 ** (attempt - 1))
        logger.warning(
            "Claude call failed (attempt %d/%d): %s — retrying in %.1fs",
            attempt,
            attempts,
            last_error,
            delay,
        )
        await asyncio.sleep(delay)

    raise LLMError(f"Claude request failed after {attempts} attempt(s): {last_error}")


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None:
        raise LLMError("Claude response had no content")
    text_blocks = [block.text for block in content if hasattr(block, "text")]
    return "\n".join(text_blocks).strip()


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if status is not None and int(status) in _RETRYABLE_STATUS_CODES:
            return True
    except (TypeError, ValueError):
        pass

    name = type(exc).__name__.lower()
    return any(hint in name for hint in _RETRYABLE_NAME_HINTS)


def _parse_json_response(raw: str) -> dict[str, Any]:
    if not raw or not raw.strip():
        raise LLMError("Empty response from Claude")

    candidates: list[str] = []
    cleaned = raw.strip()

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence_match:
        candidates.append(fence_match.group(1).strip())

    candidates.append(cleaned)

    embedded = _extract_json_object(cleaned)
    if embedded:
        candidates.append(embedded)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    logger.error("Failed to parse LLM response: %s", raw[:500])
    raise LLMError("Invalid JSON from Claude: no parseable object found")


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced top-level {...} object, ignoring braces in strings."""
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for idx in range(start, len(text)):
        char = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    return None
