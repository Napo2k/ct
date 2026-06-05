#!/usr/bin/env python3
"""
HTTP trigger for n8n → Windows-native cycle runner.

n8n (WSL2 Docker) calls this endpoint; cycle script runs on Windows host.

    python scripts/http_trigger.py
"""

from __future__ import annotations

import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cycle.config import load_config  # noqa: E402
from cycle.mcp_client import run_async  # noqa: E402
from cycle.runner import run_cycle  # noqa: E402

logger = logging.getLogger(__name__)


class CycleHandler(BaseHTTPRequestHandler):
    config_path = "config/cycle.json"

    def do_POST(self) -> None:
        if self.path not in {"/cycle", "/cycle/"}:
            self._respond(404, {"error": "not found"})
            return

        try:
            config = load_config(self.config_path)
            summary = run_async(run_cycle(config))
            self._respond(200, summary)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Cycle failed")
            self._respond(500, {"error": str(exc)})

    def do_GET(self) -> None:
        if self.path in {"/health", "/health/"}:
            self._respond(200, {"status": "ok"})
            return
        self._respond(404, {"error": "not found"})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), format % args)

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    config = load_config()
    trigger = config.http_trigger
    host = trigger.get("host", "127.0.0.1")
    port = int(trigger.get("port", 8787))

    server = HTTPServer((host, port), CycleHandler)
    logger.info("HTTP trigger listening on http://%s:%d/cycle", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
