"""Adversarial entry verification — a second model tries to refute the entry thesis.

For live-mode ENTER decisions a cheap, independent model call is prompted to
find reasons the trade should NOT be taken. If it refutes the entry — or if
verification itself fails in live mode — the entry is blocked (fail closed).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_VERIFIER_MODEL = "claude-haiku-4-5"
DEFAULT_VERIFIER_MAX_TOKENS = 1024

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "refuted": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["refuted", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a risk officer reviewing a proposed FX trade before execution with "
    "real money. Your job is to REFUTE the entry if there is a credible reason "
    "not to take it: a veto check that failed, indicators contradicting the "
    "stated direction, stop-loss/take-profit geometry that does not match the "
    "stated ATR, a regime that forbids new entries, stale or missing data, or "
    "reasoning that does not follow from the numbers. You are not asked to "
    "find a better trade — only whether THIS trade should be blocked. If the "
    "entry is sound, do not refute it. Respond with JSON: "
    '{"refuted": bool, "reason": "one or two sentences"}.'
)


async def verify_entry(
    decision: dict[str, Any],
    market_state: dict[str, Any],
    veto: dict[str, Any],
    *,
    verifier_config: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Return {"approved": bool, "refuted": bool, "reason": str, "error": str|None}.

    Callers decide what a verification *error* means: live mode treats it as
    blocked (fail closed), paper mode may proceed with a warning.
    """
    cfg = verifier_config or {}
    model = cfg.get("model", DEFAULT_VERIFIER_MODEL)
    max_tokens = int(cfg.get("max_tokens", DEFAULT_VERIFIER_MAX_TOKENS))

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"approved": False, "refuted": False, "reason": "", "error": "no API key"}

    try:
        import anthropic
    except ImportError:
        return {
            "approved": False,
            "refuted": False,
            "reason": "",
            "error": "anthropic package not installed",
        }

    indicators = market_state.get("indicators", {}).get(decision.get("pair", ""), {})
    payload = json.dumps(
        {
            "proposed_decision": decision,
            "veto_checks": veto,
            "indicators": indicators,
            "tick": market_state.get("ticks", {}).get(decision.get("pair", "")),
            "open_positions": market_state.get("positions"),
        },
        indent=2,
        default=str,
    )

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=1)

    def _create() -> Any:
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            messages=[{"role": "user", "content": payload}],
        )

    try:
        message = await asyncio.to_thread(_create)
        text = "".join(
            getattr(block, "text", "") for block in getattr(message, "content", [])
        ).strip()
        verdict = json.loads(text)
        refuted = bool(verdict.get("refuted"))
        reason = str(verdict.get("reason", ""))
        return {"approved": not refuted, "refuted": refuted, "reason": reason, "error": None}
    except Exception as exc:  # noqa: BLE001 — caller decides fail-open vs fail-closed
        logger.warning("Entry verification failed: %s", exc)
        return {"approved": False, "refuted": False, "reason": "", "error": str(exc)}
