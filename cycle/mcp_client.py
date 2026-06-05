"""Thin MCP stdio client for calling MT5 and Massive tool servers."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]


class MCPClientError(Exception):
    pass


class MCPClient:
    def __init__(self, name: str, server_config: dict[str, Any]) -> None:
        self.name = name
        self.server_config = server_config
        self._session = None
        self._context = None

    async def connect(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise MCPClientError(
                "mcp package not installed — run: pip install mcp"
            ) from exc

        command = self.server_config.get("command")
        if not command:
            raise MCPClientError(f"{self.name}: missing command in config")

        args = list(self.server_config.get("args", []))
        cwd = self.server_config.get("cwd", ".")
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            cwd_path = ROOT / cwd_path

        env = self.server_config.get("env")

        params = StdioServerParameters(
            command=command,
            args=args,
            cwd=str(cwd_path),
            env=env,
        )

        self._context = stdio_client(params)
        read, write = await self._context.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        logger.info("Connected to %s MCP server", self.name)

    async def disconnect(self) -> None:
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
            self._context = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        if self._session is None:
            raise MCPClientError(f"{self.name}: not connected")

        result = await self._session.call_tool(tool_name, arguments or {})
        if result.isError:
            raise MCPClientError(f"{self.name}.{tool_name} error: {result.content}")

        texts = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                texts.append(text)

        if not texts:
            return None

        combined = "\n".join(texts)
        try:
            return json.loads(combined)
        except json.JSONDecodeError:
            return combined

    async def list_tools(self) -> list[str]:
        if self._session is None:
            raise MCPClientError(f"{self.name}: not connected")
        response = await self._session.list_tools()
        return [tool.name for tool in response.tools]


@asynccontextmanager
async def mcp_session(name: str, server_config: dict[str, Any]):
    client = MCPClient(name, server_config)
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


def run_async(coro):
    return asyncio.run(coro)
