"""Claude reasoning layer — invokes Anthropic API with playbook system prompt."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 4096
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


def load_playbook(path) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def build_user_prompt(
    cycle_id: str,
    pairs: list[str],
    market_state: dict[str, Any],
    veto_result: dict[str, Any],
    warm_reasons: dict[str, list[str]],
    execution_mode: bool,
    session: dict[str, Any] | None = None,
    exit_signals: dict[str, Any] | None = None,
) -> str:
    phase = 1 if execution_mode else 0
    if execution_mode:
        exec_note = (
            "Phase 1: EXECUTION_MODE is true — decisions WILL be executed on the demo account. "
            "Default to BUY_LIMIT/SELL_LIMIT. Market orders require entry_window. "
            "Respect session lot_multiplier if consecutive_losses >= 3."
        )
    else:
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


async def invoke_claude(
    *,
    playbook: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
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

    def _create_message() -> Any:
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=playbook,
            messages=[{"role": "user", "content": user_prompt}],
        )

    return await _call_with_retries(
        _create_message,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
    )


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
