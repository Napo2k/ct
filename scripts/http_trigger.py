#!/usr/bin/env python3
"""
HTTP trigger for n8n → Windows-native cycle runner.

n8n (WSL2 Docker) calls these endpoints; cycle script runs on Windows host.

    python scripts/http_trigger.py
    python scripts/http_trigger.py --config config/cycle.json --port 8787

Endpoints:
    POST /cycle       — run one evaluation cycle
    GET  /health      — liveness + phase/session snapshot
    GET  /summary     — end-of-session P&L report (same as session_summary.py)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import CycleConfig, load_config  # noqa: E402
from cycle.mcp_client import run_async  # noqa: E402
from cycle.runner import run_cycle  # noqa: E402
from cycle.session_state import load_session  # noqa: E402
from scripts.session_summary import build_summary  # noqa: E402

logger = logging.getLogger(__name__)


class CycleHandler(BaseHTTPRequestHandler):
    config_path: str = "config/cycle.json"
    base_config: CycleConfig | None = None

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/cycle", "/cycle/"}:
            self._respond(404, {"error": "not found"})
            return

        try:
            config = self._resolve_config(parsed.query)
            summary = run_async(run_cycle(config))
            self._respond(200, summary)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cycle failed")
            self._respond(500, {"error": str(exc)})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/health":
            self._handle_health()
            return

        if path == "/summary":
            self._handle_summary(parsed.query)
            return

        self._respond(404, {"error": "not found"})

    def _handle_health(self) -> None:
        cfg = self.base_config or load_config(self.config_path)
        session = load_session(cfg.session_state_dir)
        self._respond(
            200,
            {
                "status": "ok",
                "phase": cfg.phase,
                "execution_mode": cfg.execution_mode,
                "mock_mode": cfg.mock_mode,
                "session_date": session.session_date,
                "cycles_today": session.cycles_today,
                "consecutive_losses": session.consecutive_losses,
            },
        )

    def _handle_summary(self, query: str) -> None:
        cfg = self.base_config or load_config(self.config_path)
        params = parse_qs(query)
        session_date = (params.get("date") or [None])[0]
        summary = build_summary(cfg.logs_dir, session_date=session_date)
        self._respond(200, summary)

    def _resolve_config(self, query: str) -> CycleConfig:
        cfg = self.base_config or load_config(self.config_path)
        params = parse_qs(query)
        mock_flag = (params.get("mock") or [None])[0]
        if mock_flag is None:
            return cfg

        override_mock = mock_flag.lower() in {"1", "true", "yes"}
        return CycleConfig(
            phase=cfg.phase,
            execution_mode=cfg.execution_mode,
            mock_mode=override_mock,
            mock_llm=cfg.mock_llm if not override_mock else True,
            pairs=cfg.pairs,
            timezone=cfg.timezone,
            base_lot_size=cfg.base_lot_size,
            max_positions=cfg.max_positions,
            max_daily_drawdown_pct=cfg.max_daily_drawdown_pct,
            max_intraday_drawdown_pct=cfg.max_intraday_drawdown_pct,
            spread_limits_pips=cfg.spread_limits_pips,
            mt5_mcp=cfg.mt5_mcp,
            massive_mcp=cfg.massive_mcp,
            anthropic=cfg.anthropic,
            gitea=cfg.gitea,
            playbook_path=cfg.playbook_path,
            prefilter=cfg.prefilter,
            http_trigger=cfg.http_trigger,
            session_state_dir=cfg.session_state_dir,
            maintenance=cfg.maintenance,
            raw=cfg.raw,
        )

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), format % args)

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="ClaudeTrader HTTP trigger for n8n")
    parser.add_argument("--config", default="config/cycle.json", help="Cycle config path")
    parser.add_argument("--host", default=None, help="Bind host (default from config)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default from config)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Start even when http_trigger.enabled is false in config",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    config = load_config(args.config)
    trigger = config.http_trigger

    if not trigger.get("enabled", False) and not args.force:
        logger.error(
            "http_trigger.enabled is false in %s — pass --force to start anyway",
            args.config,
        )
        return 1

    host = args.host or trigger.get("host", "127.0.0.1")
    port = args.port or int(trigger.get("port", 8787))

    CycleHandler.config_path = str(args.config)
    CycleHandler.base_config = config

    server = HTTPServer((host, port), CycleHandler)
    logger.info(
        "HTTP trigger on http://%s:%d (POST /cycle, GET /health, GET /summary)",
        host,
        port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
