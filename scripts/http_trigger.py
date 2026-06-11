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
import dataclasses
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
from cycle.safety import (  # noqa: E402
    engage_kill_switch,
    heartbeat_age_seconds,
    kill_switch_engaged,
    kill_switch_reason,
)
from cycle.session_state import load_session  # noqa: E402
from scripts.session_summary import build_summary  # noqa: E402

logger = logging.getLogger(__name__)


class CycleHandler(BaseHTTPRequestHandler):
    config_path: str = "config/cycle.json"
    base_config: CycleConfig | None = None

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/killswitch":
            self._handle_engage_kill_switch()
            return

        if path != "/cycle":
            self._respond(404, {"error": "not found"})
            return

        try:
            config = self._resolve_config(parsed.query)
            summary = run_async(run_cycle(config))
            self._respond(200, summary)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cycle failed")
            self._respond(500, {"error": str(exc)})

    def _handle_engage_kill_switch(self) -> None:
        """Engage the kill switch remotely. Disengaging requires manual file removal —
        an operator must positively confirm conditions are safe before trading resumes."""
        cfg = self.base_config or load_config(self.config_path)
        length = int(self.headers.get("Content-Length") or 0)
        reason = "engaged via /killswitch"
        if length:
            try:
                body = json.loads(self.rfile.read(length))
                reason = str(body.get("reason", reason))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        engage_kill_switch(cfg.kill_switch_path, reason)
        self._respond(200, {"kill_switch": True, "reason": reason})

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
        engaged = kill_switch_engaged(cfg.kill_switch_path)
        self._respond(
            200,
            {
                "status": "halted" if engaged else "ok",
                "phase": cfg.phase,
                "execution_mode": cfg.execution_mode,
                "live_mode": cfg.live_mode,
                "mock_mode": cfg.mock_mode,
                "kill_switch": engaged,
                "kill_switch_reason": (
                    kill_switch_reason(cfg.kill_switch_path) if engaged else None
                ),
                "heartbeat_age_seconds": heartbeat_age_seconds(cfg.heartbeat_path),
                "session_date": session.session_date,
                "cycles_today": session.cycles_today,
                "trades_today": session.trades_today,
                "realized_pnl_today": session.realized_pnl_today,
                "consecutive_losses": session.consecutive_losses,
                "session_peak_equity": session.session_peak_equity,
                "last_equity": session.last_equity,
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
        if override_mock and cfg.live_mode:
            raise ValueError("mock override is not allowed while live_mode is active")
        return dataclasses.replace(
            cfg,
            mock_mode=override_mock,
            mock_llm=cfg.mock_llm if not override_mock else True,
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
