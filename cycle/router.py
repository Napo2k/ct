"""Agentic LLM routing — a triage agent picks which provider decides each cycle.

Structure:
1. A cheap, fast triage model classifies the cycle (routine/standard/critical)
   and proposes a provider from the configured registry.
2. The proposal is SANITIZED: unknown providers rejected, missing API keys
   skipped, rate-limited providers (persisted cooldown) skipped, and in live
   mode the choice is clamped to llm_router.live_approved (default: anthropic
   only — free tiers earn trust in paper mode, not with real money).
3. The decision call runs on the chosen provider. Anthropic goes through the
   full-featured cycle/llm.py path (structured outputs, tools, caching);
   other providers get the same playbook + prompt with JSON mode, parsed by
   the existing robust parser. Failures escalate through a fallback chain
   that always terminates at the default provider.

Whatever model decides, every deterministic guard downstream (veto override,
confidence gate, adversarial verifier, risk checks) applies unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from cycle.llm import _parse_json_response, invoke_claude
from cycle.providers import ProviderError, complete_text, provider_has_key

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_COOLDOWN_SECONDS = 900  # 15 min after a 429
DEFAULT_TRIAGE_PROVIDER = "groq"
DEFAULT_TRIAGE_MODEL = "llama-3.3-70b-versatile"
DEFAULT_PROVIDER = "anthropic"
COMPLEXITIES = {"routine", "standard", "critical"}

TRIAGE_SYSTEM = (
    "You are the routing agent of an automated FX trading system. Given a "
    "compact summary of the current evaluation cycle and a list of available "
    "LLM providers, decide which provider should make the trading decision.\n"
    "Classify complexity first:\n"
    "- routine: no open positions, weak/no warm signals, vetoes likely to "
    "block entries anyway — a fast cheap model suffices.\n"
    "- standard: warm entry signals worth real analysis.\n"
    "- critical: open positions with exit signals, conflicting indicators, "
    "drawdown pressure, or live mode — route to the most capable provider.\n"
    "Pick ONLY from the provided available_providers list. Respond with raw "
    'JSON: {"complexity": "routine|standard|critical", "provider": "...", '
    '"reason": "one sentence"}.'
)


# ---------------------------------------------------------------------------
# Provider health state (cooldowns persisted across cycles)
# ---------------------------------------------------------------------------

def _state_path(state_dir: Path | str) -> Path:
    base = Path(state_dir)
    if not base.is_absolute():
        base = ROOT / base
    base.mkdir(parents=True, exist_ok=True)
    return base / "provider_state.json"


def load_provider_state(state_dir: Path | str) -> dict[str, Any]:
    try:
        with _state_path(state_dir).open(encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_provider_state(state: dict[str, Any], state_dir: Path | str) -> None:
    path = _state_path(state_dir)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".provider_state.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def in_cooldown(state: dict[str, Any], provider: str, now: float | None = None) -> bool:
    until = (state.get(provider) or {}).get("cooldown_until", 0)
    return (now if now is not None else time.time()) < float(until)


def record_failure(
    state: dict[str, Any],
    provider: str,
    *,
    rate_limited: bool,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    now: float | None = None,
) -> None:
    entry = state.setdefault(provider, {})
    entry["consecutive_failures"] = int(entry.get("consecutive_failures", 0)) + 1
    if rate_limited:
        entry["cooldown_until"] = (now if now is not None else time.time()) + cooldown_seconds


def record_success(state: dict[str, Any], provider: str) -> None:
    entry = state.setdefault(provider, {})
    entry["consecutive_failures"] = 0
    entry["cooldown_until"] = 0


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------

def build_triage_summary(
    market_state: dict[str, Any],
    veto: dict[str, Any],
    warm_reasons: dict[str, list[str]],
    exit_signals: dict[str, Any] | None,
    *,
    live_mode: bool,
) -> dict[str, Any]:
    """Compact, credential-free view of the cycle for the routing agent."""
    failed_checks = [c["name"] for c in veto.get("checks", []) if not c.get("pass")]
    positions = market_state.get("positions") or []
    return {
        "live_mode": live_mode,
        "open_positions": len(positions),
        "position_symbols": sorted({p.get("symbol") for p in positions if p.get("symbol")}),
        "pending_orders": len(market_state.get("pending_orders") or []),
        "warm_signal_pairs": {k: len(v) for k, v in (warm_reasons or {}).items()},
        "exit_signals": {
            symbol: info.get("force_exit", False)
            for symbol, info in (exit_signals or {}).items()
        },
        "failed_veto_checks": failed_checks,
        "veto_blocked": veto.get("blocked", False),
    }


def _sanitize_choice(
    proposal: dict[str, Any],
    *,
    registry: dict[str, Any],
    live_mode: bool,
    live_approved: list[str],
    provider_state: dict[str, Any],
    default_provider: str,
) -> tuple[str, str]:
    """Validate the triage agent's pick; return (provider, rejection_reason or '')."""
    provider = str(proposal.get("provider", "")).lower().strip()
    if provider not in registry:
        return default_provider, f"provider {provider!r} not in registry"
    if live_mode and provider not in live_approved:
        return default_provider, f"{provider} not approved for live mode"
    if not provider_has_key(provider):
        return default_provider, f"no API key for {provider}"
    if in_cooldown(provider_state, provider):
        return default_provider, f"{provider} in rate-limit cooldown"
    return provider, ""


async def route_decision(
    triage_summary: dict[str, Any],
    *,
    router_config: dict[str, Any],
    provider_state: dict[str, Any],
    live_mode: bool,
) -> dict[str, Any]:
    """Run the triage agent and return sanitized routing metadata."""
    registry: dict[str, Any] = router_config.get("providers", {})
    default_provider = router_config.get("default_provider", DEFAULT_PROVIDER)
    live_approved = list(router_config.get("live_approved", [DEFAULT_PROVIDER]))
    triage_provider = router_config.get("triage_provider", DEFAULT_TRIAGE_PROVIDER)
    triage_model = router_config.get("triage_model", DEFAULT_TRIAGE_MODEL)

    candidates = [
        name for name in registry
        if provider_has_key(name)
        and not in_cooldown(provider_state, name)
        and (not live_mode or name in live_approved)
    ]

    routing: dict[str, Any] = {
        "complexity": "standard",
        "provider": default_provider,
        "reason": "default",
        "routed_by": "fallback",
    }

    if not provider_has_key(triage_provider) or in_cooldown(provider_state, triage_provider):
        routing["reason"] = f"triage provider {triage_provider} unavailable"
        return routing

    prompt = json.dumps(
        {"cycle_summary": triage_summary, "available_providers": candidates},
        indent=2,
    )
    try:
        raw = await complete_text(
            triage_provider,
            triage_model,
            TRIAGE_SYSTEM,
            prompt,
            max_tokens=512,
            timeout=float(router_config.get("triage_timeout_seconds", 20)),
        )
        proposal = _parse_json_response(raw)
        record_success(provider_state, triage_provider)
    except Exception as exc:  # noqa: BLE001 — triage failure must not stop trading
        logger.warning("Routing triage failed (%s) — using default provider", exc)
        if isinstance(exc, ProviderError) and exc.status_code == 429:
            record_failure(provider_state, triage_provider, rate_limited=True)
        routing["reason"] = f"triage failed: {exc}"
        return routing

    complexity = str(proposal.get("complexity", "standard")).lower()
    if complexity not in COMPLEXITIES:
        complexity = "standard"

    provider, rejection = _sanitize_choice(
        proposal,
        registry=registry,
        live_mode=live_mode,
        live_approved=live_approved,
        provider_state=provider_state,
        default_provider=default_provider,
    )

    routing.update({
        "complexity": complexity,
        "provider": provider,
        "reason": rejection or str(proposal.get("reason", ""))[:300],
        "routed_by": "triage" if not rejection else "sanitizer",
        "triage_pick": proposal.get("provider"),
    })
    return routing


# ---------------------------------------------------------------------------
# Decision execution with fallback chain
# ---------------------------------------------------------------------------

async def decide_with_routing(
    *,
    playbook: str,
    user_prompt: str,
    routing: dict[str, Any],
    router_config: dict[str, Any],
    anthropic_config: dict[str, Any],
    provider_state: dict[str, Any],
    mt5: Any | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the decision on the routed provider, falling back on errors.

    Returns (raw_decision, routing_meta). Raises only if every provider in
    the chain fails — the runner then degrades to HOLD as before.
    """
    registry: dict[str, Any] = router_config.get("providers", {})
    cooldown = float(router_config.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))

    chain: list[str] = [routing["provider"]]
    for name in router_config.get("fallback_order", [DEFAULT_PROVIDER]):
        if name not in chain:
            chain.append(name)
    if DEFAULT_PROVIDER not in chain:
        chain.append(DEFAULT_PROVIDER)

    attempts: list[dict[str, str]] = []
    last_error: Exception | None = None

    for provider in chain:
        if provider not in registry and provider != DEFAULT_PROVIDER:
            continue
        if not provider_has_key(provider):
            attempts.append({"provider": provider, "outcome": "skipped: no key"})
            continue
        if in_cooldown(provider_state, provider):
            attempts.append({"provider": provider, "outcome": "skipped: cooldown"})
            continue

        model = (registry.get(provider) or {}).get("model") or anthropic_config.get(
            "model", "claude-opus-4-8"
        )
        try:
            if provider == "anthropic":
                decision = await invoke_claude(
                    playbook=playbook,
                    user_prompt=user_prompt,
                    model=model,
                    max_tokens=int(anthropic_config.get("max_tokens", 8192)),
                    max_retries=int(anthropic_config.get("max_retries", 3)),
                    timeout_seconds=float(anthropic_config.get("timeout_seconds", 60.0)),
                    retry_base_delay=float(anthropic_config.get("retry_base_delay", 1.0)),
                    mt5=mt5,
                    enable_tools=bool(anthropic_config.get("enable_tools", False)),
                    use_structured_output=bool(anthropic_config.get("structured_output", True)),
                    cache_playbook=bool(anthropic_config.get("cache_playbook", True)),
                )
            else:
                raw = await complete_text(
                    provider,
                    model,
                    playbook,
                    user_prompt,
                    max_tokens=int((registry.get(provider) or {}).get("max_tokens", 4096)),
                    timeout=float(router_config.get("timeout_seconds", 60)),
                )
                decision = _parse_json_response(raw)

            record_success(provider_state, provider)
            attempts.append({"provider": provider, "outcome": "ok"})
            meta = dict(routing)
            meta.update({"decided_by": provider, "model": model, "attempts": attempts})
            return decision, meta

        except Exception as exc:  # noqa: BLE001 — try the next provider in the chain
            last_error = exc
            rate_limited = isinstance(exc, ProviderError) and exc.status_code == 429
            record_failure(
                provider_state, provider,
                rate_limited=rate_limited, cooldown_seconds=cooldown,
            )
            attempts.append({"provider": provider, "outcome": f"failed: {exc}"[:200]})
            logger.warning("Provider %s failed (%s) — trying next in chain", provider, exc)

    raise RuntimeError(
        f"All providers in routing chain failed (attempts: {attempts}): {last_error}"
    )
