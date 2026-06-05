"""Claude reasoning layer — invokes Anthropic API with playbook system prompt."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


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
) -> str:
    payload = {
        "cycle_id": cycle_id,
        "execution_mode": execution_mode,
        "pairs_to_evaluate": pairs,
        "warm_signal_reasons": warm_reasons,
        "veto_checks": veto_result,
        "market_state": market_state,
        "instructions": (
            "Evaluate the market state and produce ONE decision JSON object. "
            "If no action is warranted, return HOLD for the primary pair (EURUSD). "
            "Phase 0: EXECUTION_MODE is false — recommend only, no phantom trades. "
            "Respond with raw JSON only, no markdown fences."
        ),
    }
    return json.dumps(payload, indent=2, default=str)


async def invoke_claude(
    *,
    playbook: str,
    user_prompt: str,
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 4096,
) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY environment variable not set")

    try:
        import anthropic
    except ImportError as exc:
        raise LLMError("anthropic package not installed — run: pip install anthropic") from exc

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=playbook,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text_blocks = [block.text for block in message.content if hasattr(block, "text")]
    raw = "\n".join(text_blocks).strip()
    return _parse_json_response(raw)


def _parse_json_response(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse LLM response: %s", raw[:500])
        raise LLMError(f"Invalid JSON from Claude: {exc}") from exc
