"""Minimal .env loader — stdlib only, real environment always wins.

Loads KEY=VALUE pairs from a .env file at the repo root into os.environ so
credentials (OANDA_API_TOKEN, ANTHROPIC_API_KEY, ...) don't need exporting in
every shell. Variables already set in the environment are never overridden,
so CI/cron/systemd-provided values take precedence over the file.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_PATH = ROOT / ".env"

_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$"
)


def parse_dotenv(content: str) -> dict[str, str]:
    """Parse .env content into a dict. Comments and malformed lines are skipped."""
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        key = match.group("key")
        value = match.group("value").strip()
        # Strip one matching pair of surrounding quotes; otherwise drop an
        # unquoted trailing comment.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_dotenv(path: Path | str | None = None) -> int:
    """Load .env into os.environ (no overrides). Returns variables set."""
    env_path = Path(path) if path else DEFAULT_ENV_PATH
    try:
        content = env_path.read_text(encoding="utf-8")
    except OSError:
        return 0  # missing .env is the normal case

    loaded = 0
    for key, value in parse_dotenv(content).items():
        if key in os.environ:
            continue  # real environment always wins
        os.environ[key] = value
        loaded += 1
    if loaded:
        logger.debug("Loaded %d variable(s) from %s", loaded, env_path)
    return loaded
