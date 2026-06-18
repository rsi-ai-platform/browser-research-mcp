#!/usr/bin/env python3
"""Smoke-test browser-research-mcp with a Sonnet-powered mini-agent.

This script:
1) connects to an MCP endpoint over JSON-RPC HTTP,
2) fetches tool schemas via tools/list,
3) runs an Anthropic tool-use loop,
4) executes MCP tools via tools/call until Sonnet returns a final answer.

Example:
  python scripts/test_agent_sonnet.py ^
    --query "Use today and strategy, then explain the recommended ladder."

Prereqs:
  - ANTHROPIC_API_KEY set
  - browser-research MCP running with HTTP transport, e.g.:
      uvx browser-research --transport streamable-http --port 7862
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from anthropic import AsyncAnthropic


DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MCP_URL = "http://127.0.0.1:7862/mcp"
MAX_ITERATIONS_HARD_CEILING = 60

END_OF_TURN_RULES = (
    "\n\n"
    "END-OF-TURN DISCIPLINE.\n"
    "Text emitted with tool_use is scratch, not final output.\n"
    "When you end_turn, provide a complete answer with concrete findings.\n"
    "If incomplete, include what you found and what is missing."
)

SYNTHESIS_NUDGE = (
    "This is your final turn; do not call more tools. "
    "Write the final answer now from the observed tool results."
)

FALLBACK_SYNTHESIS_NUDGE = (
    "Your previous response had no final answer text. "
    "Write the final answer now using the tool results above."
)


def _preview_text(text: str, limit: int = 800) -> str:
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    return t[:limit] + "…"


class MCPError(RuntimeError):
    """Raised when JSON-RPC/MCP requests fail."""


@dataclass
class MCPTool:
    name: str
    description: str
    input_schema: dict[str, Any]


class MCPHttpClient:
    """Minimal JSON-RPC client for streamable-http MCP endpoints."""

    def __init__(self, url: str, timeout_s: float = 900.0) -> None:
        self.url = url
        self._client = httpx.Client(
            timeout=timeout_s,
            follow_redirects=True,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        self._id = 0
        self.protocol_version = "2025-06-18"
        self._session_id: str | None = None

    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _rpc(self, method: str, params: dict[str, Any] | None = None, *, is_notification: bool = False) -> Any:
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        if not is_notification:
            payload["id"] = str(self._id)
        try:
            resp = self._client.post(self.url, json=payload, headers=self._headers())
            if method == "initialize":
                sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid.strip()
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MCPError(f"HTTP error calling {method}: {exc}") from exc

        if is_notification:
            return {}

        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            data = self._extract_sse_data(resp.text)
            try:
                body = json.loads(data)
            except json.JSONDecodeError as exc:
                raise MCPError(f"Invalid SSE JSON for {method}: {exc}") from exc
        else:
            try:
                body = resp.json()
            except json.JSONDecodeError as exc:
                raise MCPError(f"Invalid JSON response for {method}: {exc}") from exc

        if "error" in body:
            raise MCPError(f"{method} failed: {body['error']}")
        if "result" not in body:
            raise MCPError(f"{method} missing result field")
        return body["result"]

    @staticmethod
    def _extract_sse_data(sse_text: str) -> str:
        for raw in sse_text.splitlines():
            line = raw.strip()
            if not line or line.startswith(":"):
                continue
            if line.startswith("data:"):
                return line[5:].strip()
        raise MCPError("SSE response had no parseable data line")

    def initialize(self) -> None:
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                    "clientInfo": {
                        "name": "browser-research-agent-test",
                        "version": "0.1.0",
                    },
                },
            )
            if self._session_id:
                try:
                    self._rpc("notifications/initialized", is_notification=True)
                except MCPError:
                    pass
        except MCPError:
            return

    def list_tools(self) -> list[MCPTool]:
        try:
            self.initialize()
        except Exception:
            pass
        result = self._rpc("tools/list", {})
        tools: list[dict[str, Any]] = result.get("tools") or []
        out: list[MCPTool] = []
        for item in tools:
            out.append(
                MCPTool(
                    name=item.get("name", ""),
                    description=item.get("description") or "",
                    input_schema=item.get("inputSchema")
                    or item.get("input_schema")
                    or {"type": "object", "properties": {}},
                )
            )
        return out

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            self.initialize()
        except Exception:
            pass
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content", [])
        if not isinstance(content, list):
            return result

        # Flatten common MCP tool result shapes into JSON-serializable output.
        text_parts: list[str] = []
        normalized: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
            normalized.append(block)

        if text_parts:
            parsed = []
            for t in text_parts:
                try:
                    parsed.append(json.loads(t))
                except json.JSONDecodeError:
                    parsed.append(t)
            if len(parsed) == 1:
                return parsed[0]
            return {"parts": parsed}
        return {"content": normalized}


def sanitize(name: str) -> str:
    return name.replace(".", "__")


def build_system_prompt(today: str, connector_name: str) -> str:
    return (
        f"Today's date is {today}. Use it as the temporal reference for terms "
        f"like latest/current/today.\n\n"
        f"You are a focused agent with access ONLY to tools on '{connector_name}'. "
        f"Read tool descriptions and pick appropriate tools.\n"
        f"If a tool returns an error/no data, explain that clearly in the final answer."
        + END_OF_TURN_RULES
    )


async def run_agent(
    *,
    query: str,
    mcp: MCPHttpClient,
    model: str,
    max_iters: int,
    print_assistant_text: bool,
) -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2

    tool_catalog = mcp.list_tools()
    if not tool_catalog:
        print("ERROR: MCP returned no tools.", file=sys.stderr)
        return 2

    real_name_for: dict[str, str] = {}
    anthropic_tools: list[dict[str, Any]] = []
    for tool in tool_catalog:
        alias = sanitize(tool.name)
        real_name_for[alias] = tool.name
        anthropic_tools.append(
            {
                "name": alias,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
        )

    print(f"Connected to MCP: {mcp.url}")
    print(f"Loaded tools ({len(tool_catalog)}):")
    for t in tool_catalog:
        print(f"  - {t.name}")
    print()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system = build_system_prompt(today, "browser-research")
    messages: list[dict[str, Any]] = [{"role": "user", "content": query}]

    client = AsyncAnthropic(api_key=key)
    t0 = time.perf_counter()

    final_text = ""
    iterations = 0

    for i in range(1, max_iters + 1):
        iterations = i
        is_last = i == max_iters
        create_kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=20000,
            system=system,
            tools=anthropic_tools,
            messages=messages + ([{"role": "user", "content": SYNTHESIS_NUDGE}] if is_last else []),
        )
        if is_last:
            create_kwargs["tool_choice"] = {"type": "none"}
        resp = await client.messages.create(**create_kwargs)

        if resp.stop_reason == "end_turn" or is_last:
            final_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            final_text = "".join(final_chunks).strip()
            break

        assistant_blocks: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        tool_calls = 0
        assistant_text_blocks: list[str] = []

        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                text = block.text or ""
                assistant_blocks.append({"type": "text", "text": text})
                if text.strip():
                    assistant_text_blocks.append(text)
                continue
            if btype != "tool_use":
                continue

            tool_calls += 1
            alias_name = block.name
            real_name = real_name_for.get(alias_name, alias_name)
            args = block.input or {}
            print(f"[turn {i}] tool: {real_name} args={json.dumps(args, default=str)}")

            started = time.perf_counter()
            try:
                result = mcp.call_tool(real_name, dict(args))
            except Exception as exc:  # noqa: BLE001
                result = {"error": str(exc)}
            duration_ms = (time.perf_counter() - started) * 1000.0
            preview = json.dumps(result, default=str)[:500]
            print(f"[turn {i}] result ({duration_ms:.0f} ms): {preview}")

            assistant_blocks.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": alias_name,
                    "input": dict(args),
                }
            )
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str)[:60000],
                }
            )

        if print_assistant_text and assistant_text_blocks:
            for idx, text in enumerate(assistant_text_blocks, start=1):
                print(f"[turn {i}] reasoning {idx}: {_preview_text(text)}")

        messages.append({"role": "assistant", "content": assistant_blocks})
        if tool_calls == 0:
            text_chunks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            final_text = "".join(text_chunks).strip()
            break
        messages.append({"role": "user", "content": tool_results})

    if not final_text:
        try:
            fb = await client.messages.create(
                model=model,
                max_tokens=2048,
                system=system,
                tools=anthropic_tools,
                messages=messages + [{"role": "user", "content": FALLBACK_SYNTHESIS_NUDGE}],
                tool_choice={"type": "none"},
            )
            final_chunks = [b.text for b in fb.content if getattr(b, "type", "") == "text"]
            final_text = "".join(final_chunks).strip()
        except Exception as exc:  # noqa: BLE001
            final_text = f"(fallback synthesis failed: {exc})"

    elapsed = (time.perf_counter() - t0) * 1000.0
    print("=== FINAL ANSWER ===")
    print(final_text or "(no text returned)")
    print()
    print(f"Completed in {iterations} turns, {elapsed:.0f} ms.")
    return 0 if final_text else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Sonnet agent against browser-research MCP")
    parser.add_argument(
        "--mcp-url",
        default=os.environ.get("BROWSER_RESEARCH_MCP_URL", DEFAULT_MCP_URL),
        help=f"MCP endpoint URL (default: {DEFAULT_MCP_URL})",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
        help=f"Anthropic model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--max-iters",
        type=int,
        default=8,
        help="Maximum tool-use turns (default: 8)",
    )
    parser.add_argument(
        "--query",
        default=(
            "Extract the pib releases from PIB website for PM office for 13th June 2025 and summarize each"
        ),
        help="Prompt sent to the Sonnet agent",
    )
    parser.add_argument(
        "--print-assistant-text",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=("Print assistant text blocks each turn (default: on). "
              "These are visible rationale messages, not hidden chain-of-thought."),
    )
    return parser.parse_args()


async def _amain() -> int:
    args = parse_args()
    mcp = MCPHttpClient(args.mcp_url)
    try:
        return await run_agent(
            query=args.query,
            mcp=mcp,
            model=args.model,
            max_iters=max(1, min(MAX_ITERATIONS_HARD_CEILING, args.max_iters)),
            print_assistant_text=bool(args.print_assistant_text),
        )
    finally:
        mcp.close()


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
