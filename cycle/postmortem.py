"""Trade post-mortems — closed trades become distilled lessons in the playbook.

After a trade closes, the original entry decision (from the cycle log) and the
realized outcome are reviewed by Claude, which distills a one-line lesson.
Lessons accumulate in playbook/lessons.md (capped) and are appended to the
system prompt on every future cycle, so the strategy compounds what it learns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_LESSONS = 20
DEFAULT_POSTMORTEM_MODEL = "claude-opus-4-8"

LESSONS_HEADER = (
    "# Lessons From Closed Trades\n"
    "\n"
    "Auto-generated post-mortems of realized trades, newest last. These are\n"
    "appended to the trading playbook — weigh them as evidence from THIS\n"
    "system's own track record.\n"
)

LESSON_SCHEMA = {
    "type": "object",
    "properties": {
        "lesson": {"type": "string"},
        "category": {
            "type": "string",
            "enum": ["entry_timing", "exit_management", "risk_sizing", "regime_read",
                     "news_handling", "execution", "good_process"],
        },
        "verdict": {"type": "string", "enum": ["good_trade", "bad_trade", "good_loss", "lucky_win"]},
    },
    "required": ["lesson", "category", "verdict"],
    "additionalProperties": False,
}

POSTMORTEM_SYSTEM = (
    "You are reviewing a closed FX trade made by an automated strategy. You get "
    "the entry decision (with its reasoning and the market state at entry) and "
    "the realized outcome. Judge the PROCESS, not just the result: a disciplined "
    "loss is a good trade, a sloppy win is a bad one. Distill ONE actionable "
    "lesson in a single sentence that would improve future decisions — specific "
    "to what the data shows, never generic advice. Respond with JSON: "
    '{"lesson": str, "category": str, "verdict": str}.'
)


# ---------------------------------------------------------------------------
# Lessons file
# ---------------------------------------------------------------------------

def load_lessons(path: Path | str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_lesson_lines(content: str) -> list[str]:
    return [line for line in content.splitlines() if line.startswith("- ")]


def append_lesson(
    path: Path | str,
    *,
    date: str,
    pair: str,
    verdict: str,
    category: str,
    lesson: str,
    max_lessons: int = DEFAULT_MAX_LESSONS,
) -> None:
    """Append a lesson bullet, keeping only the newest max_lessons entries."""
    target = Path(path)
    lesson_clean = re.sub(r"\s+", " ", lesson).strip()
    line = f"- [{date}] {pair} ({verdict}, {category}): {lesson_clean}"

    existing = _parse_lesson_lines(load_lessons(target))
    if any(lesson_clean in entry for entry in existing):
        logger.info("Skipping duplicate lesson: %s", lesson_clean[:80])
        return
    lines = (existing + [line])[-max_lessons:]

    content = LESSONS_HEADER + "\n" + "\n".join(lines) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".lessons.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, target)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Matching closed deals to decision logs
# ---------------------------------------------------------------------------

def load_decision_logs(logs_dir: Path | str) -> list[dict[str, Any]]:
    """Load all cycle logs (recursively) that contain an executed ENTER decision."""
    entries: list[dict[str, Any]] = []
    root = Path(logs_dir)
    if not root.exists():
        return entries
    for log_path in sorted(root.rglob("*_ENTER.json")):
        try:
            with log_path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        decision = payload.get("decision") or {}
        if decision.get("action") == "ENTER":
            payload["_path"] = str(log_path)
            entries.append(payload)
    return entries


def _comment_suffix(cycle_id: str) -> str:
    return cycle_id[-15:] if cycle_id else ""


def match_deal_to_log(
    deal: dict[str, Any],
    logs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Match a closed broker deal to the decision log that opened it.

    Primary key: the cycle-id suffix the executor embeds in the order comment
    ("CT <suffix>"). Fallback: latest ENTER log for the same symbol before the
    deal's close time.
    """
    symbol = str(deal.get("symbol", "")).upper()
    comment = str(deal.get("comment", ""))

    for log in logs:
        cycle_id = str((log.get("decision") or {}).get("cycle_id", ""))
        suffix = _comment_suffix(cycle_id)
        if suffix and suffix in comment:
            return log

    deal_ts = deal.get("time")
    candidates = [
        log for log in logs
        if str((log.get("decision") or {}).get("pair", "")).upper() == symbol
    ]
    if not candidates:
        return None
    if deal_ts:
        try:
            deal_time = float(deal_ts)
            def log_time(log: dict[str, Any]) -> float:
                stamp = str(log.get("meta", {}).get("logged_at", ""))
                try:
                    return datetime.fromisoformat(stamp).timestamp()
                except ValueError:
                    return 0.0
            before = [log for log in candidates if log_time(log) <= deal_time]
            if before:
                return max(before, key=log_time)
        except (TypeError, ValueError):
            pass
    return candidates[-1]


# ---------------------------------------------------------------------------
# Post-mortem generation
# ---------------------------------------------------------------------------

def mock_postmortem(deal: dict[str, Any], log: dict[str, Any] | None) -> dict[str, Any]:
    """Deterministic post-mortem for offline testing."""
    profit = float(deal.get("profit", 0) or 0)
    verdict = "good_trade" if profit > 0 else "good_loss"
    return {
        "lesson": (
            f"Mock review of {deal.get('symbol')} closed at "
            f"{profit:+.2f} — process followed the playbook."
        ),
        "category": "good_process",
        "verdict": verdict,
    }


async def generate_postmortem(
    deal: dict[str, Any],
    log: dict[str, Any] | None,
    *,
    model: str = DEFAULT_POSTMORTEM_MODEL,
    max_tokens: int = 1024,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Ask Claude for a post-mortem of one closed trade."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")

    import anthropic

    decision = (log or {}).get("decision") or {}
    pair = decision.get("pair", deal.get("symbol", ""))
    indicators = ((log or {}).get("market_state") or {}).get("indicators", {}).get(pair, {})

    payload = json.dumps(
        {
            "entry_decision": decision,
            "indicators_at_entry": indicators,
            "outcome": {
                "profit": deal.get("profit"),
                "volume": deal.get("volume"),
                "close_price": deal.get("price"),
                "close_time": deal.get("time"),
                "comment": deal.get("comment"),
            },
        },
        indent=2,
        default=str,
    )

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds, max_retries=1)

    def _create() -> Any:
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=POSTMORTEM_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": LESSON_SCHEMA}},
            messages=[{"role": "user", "content": payload}],
        )

    message = await asyncio.to_thread(_create)
    text = "".join(
        getattr(block, "text", "") for block in getattr(message, "content", [])
    ).strip()
    return json.loads(text)


async def review_closed_trades(
    deals: list[dict[str, Any]],
    logs_dir: Path | str,
    lessons_path: Path | str,
    *,
    mock: bool = False,
    model: str = DEFAULT_POSTMORTEM_MODEL,
    max_lessons: int = DEFAULT_MAX_LESSONS,
) -> list[dict[str, Any]]:
    """Review each closing deal, append lessons, return the review records."""
    logs = load_decision_logs(logs_dir)
    reviews: list[dict[str, Any]] = []

    for deal in deals:
        profit = deal.get("profit")
        if profit is None:
            continue
        log = match_deal_to_log(deal, logs)
        try:
            if mock:
                verdict = mock_postmortem(deal, log)
            else:
                verdict = await generate_postmortem(deal, log, model=model)
        except Exception as exc:  # noqa: BLE001 — one bad review must not stop the rest
            logger.warning("Post-mortem failed for deal %s: %s", deal.get("ticket"), exc)
            continue

        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        append_lesson(
            lessons_path,
            date=date,
            pair=str(deal.get("symbol", "")),
            verdict=verdict["verdict"],
            category=verdict["category"],
            lesson=verdict["lesson"],
            max_lessons=max_lessons,
        )
        reviews.append({
            "deal": deal.get("ticket"),
            "symbol": deal.get("symbol"),
            "profit": profit,
            "matched_log": (log or {}).get("_path"),
            **verdict,
        })

    return reviews
