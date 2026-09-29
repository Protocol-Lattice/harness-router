#!/usr/bin/env python3
"""Portable Antigravity PreToolUse hook; --stop resets its redirect guard.

Use the native camelCase payload and named hook configuration. Abstaining emits
no permission decision: only a confident alternative can produce a denial.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_CATALOG_BYTES = 1_000_000


def is_router(name: str) -> bool:
    return "harness_router" in name.lower().replace("-", "_")


def normalize_tools(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("tools", [])
    if not isinstance(raw, list):
        return []

    tools: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        tool = dict(item)
        tool["name"] = name.strip()
        annotations = tool.get("annotations") or {}
        if not isinstance(annotations, dict):
            annotations = {}
        category, risk = "general", "medium"
        if annotations.get("destructiveHint", annotations.get("destructive")) is True:
            category, risk = "mutate", "high"
        elif annotations.get("readOnlyHint", annotations.get("readOnly")) is True:
            category, risk = "inspect", "low"
        tool.setdefault("description", "")
        tool.setdefault("category", category)
        tool.setdefault("risk", risk)
        tool.setdefault("input_schema", tool.get("inputSchema") or tool.get("parameters"))
        tool.setdefault("output_schema", tool.get("outputSchema"))
        tool.setdefault("source", "runtime")
        tools[tool["name"]] = tool
    return list(tools.values())


def tokens(value: Any) -> set[str]:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value))
    return {token.lower() for token in re.split(r"[^a-zA-Z0-9]+", text) if len(token) >= 2}


def shortlist(tools: list[dict[str, Any]], current: str, goal: str) -> list[dict[str, Any]]:
    pending = next((tool for tool in tools if tool["name"] == current), None)
    if pending is None or is_router(str(pending.get("serverName", ""))):
        return []
    try:
        limit = min(32, max(2, int(os.environ.get("HARNESS_ROUTER_PRETOOL_MAX_CANDIDATES", "8"))))
    except ValueError:
        limit = 8

    def score(tool: dict[str, Any]) -> float:
        return (
            SequenceMatcher(None, current.lower(), tool["name"].lower()).ratio() * 10
            + len(tokens(tool["name"]) & tokens(current)) * 4
            + len(tokens(tool.get("description")) & tokens(pending.get("description"))) * 1.5
            + len(tokens(goal) & (tokens(tool["name"]) | tokens(tool.get("description")))) * 0.75
            + (2 if tool.get("category") == pending.get("category") else 0)
        )

    others = [
        tool
        for tool in tools
        if tool["name"] != current
        and not is_router(tool["name"])
        and not is_router(str(tool.get("serverName", "")))
    ]
    return [pending, *sorted(others, key=score, reverse=True)[: limit - 1]]


class MCPClient:
    """HTTP JSON-RPC client for the persistent Harness Router MCP endpoint."""

    def __init__(self, endpoint: str, cwd: str, timeout: float) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.deadline = time.monotonic() + timeout
        self.next_id = 0

    def _post(self, message: dict[str, Any]) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        body = json.dumps({"jsonrpc": "2.0", **message}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "MCP-Protocol-Version": "2025-06-18",
            },
            method="POST",
        )
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Harness Router timed out")
        try:
            with urllib.request.urlopen(request, timeout=remaining) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError("Harness Router HTTP endpoint unavailable") from exc
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("Invalid Harness Router MCP response") from exc
        if not isinstance(value, dict):
            return {}
        if "error" in value:
            raise RuntimeError("Harness Router returned an MCP error")
        return value.get("result") if isinstance(value.get("result"), dict) else {}

    def send(self, message: dict[str, Any]) -> None:
        self._post(message)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        result = self._post({"id": self.next_id, "method": method, "params": params})
        return result

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            return {}
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for block in result.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            try:
                parsed = json.loads(block.get("text", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    def close(self) -> None:
        return
def env_number(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value):
        raise ValueError("Expected a finite setting")
    return value


def route(
    binary: str,
    cwd: str,
    goal: str,
    observation: str,
    current: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    timeout = env_number("HARNESS_ROUTER_PRETOOL_TIMEOUT", 4.0)
    if timeout <= 0:
        return {}, "route"
    client = MCPClient(endpoint, cwd, timeout)
    try:
        client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "harness-router-antigravity-hook", "version": "1"},
            },
        )
        client.send({"method": "notifications/initialized", "params": {}})
        compact_tools = [
            {key: tool.get(key) for key in ("name", "description", "category", "risk")}
            for tool in candidates
        ]
        result = client.call(
            "route",
            {
                "goal": goal,
                "observation": observation,
                "tools": compact_tools,
            },
        )
        graph_command = os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD", "").strip()
        threshold = env_number("HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD", 0.80)
        confidence = result.get("confidence")
        confident = (
            isinstance(confidence, (float, int))
            and confidence >= threshold
            and not result.get("fallback")
        )
        remaining = client.deadline - time.monotonic()
        if not graph_command or confident or len(candidates) < 3 or remaining <= 0:
            return result, "route"
        # Only an explicitly configured, side-effect-free simulator can supply this graph.
        graph_proc = subprocess.run(
            shlex.split(graph_command),
            cwd=cwd,
            input=json.dumps(
                {
                    "goal": goal,
                    "observation": observation,
                    "current_tool": current,
                    "candidates": candidates,
                    "route_result": result,
                }
            ),
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
        if graph_proc.returncode:
            return result, "route"
        graph = json.loads(graph_proc.stdout)
        if (
            not isinstance(graph, dict)
            or not isinstance(graph.get("root_state"), str)
            or not isinstance(graph.get("states"), list)
            or not isinstance(graph.get("transitions"), list)
        ):
            return result, "route"
        if client.deadline <= time.monotonic():
            return result, "route"
        mcts = client.call(
            "route_mcts",
            {
                "root_state": graph["root_state"],
                "states": graph["states"],
                "transitions": graph["transitions"],
                "simulations": graph.get("simulations", 64),
                "max_depth": graph.get("max_depth", 3),
                "use_jev_prior": graph.get("use_jev_prior", True),
            },
        )
        return (mcts, "route_mcts") if mcts else (result, "route")
    finally:
        client.close()


def load_tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Refresh the actual runtime inventory; never route from a stale snapshot."""
    inline = os.environ.get("HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON")
    if inline is not None:
        if len(inline.encode("utf-8")) > MAX_CATALOG_BYTES:
            raise ValueError("Tool catalog is too large")
        return normalize_tools(json.loads(inline))
    configured = os.environ.get("HARNESS_ROUTER_ANTIGRAVITY_TOOLS_FILE")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        with path.open("rb") as handle:
            data = handle.read(MAX_CATALOG_BYTES + 1)
        if len(data) > MAX_CATALOG_BYTES:
            raise ValueError("Tool catalog is too large")
        return normalize_tools(json.loads(data))
    timeout = env_number("HARNESS_ROUTER_DISCOVERY_TIMEOUT", 2.0)
    if timeout <= 0:
        return []
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / ".antigravity/hooks/discover_tools.py"),
            "--list-tools",
        ],
        cwd=PROJECT_ROOT,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=timeout + 0.5,
        check=True,
    )
    return normalize_tools(json.loads(result.stdout))


def redirect_marker(payload: dict[str, Any]) -> Path | None:
    conversation = payload.get("conversationId")
    if not isinstance(conversation, str) or not conversation.strip():
        return None
    key = hashlib.sha256(conversation.encode("utf-8")).hexdigest()
    directory = PROJECT_ROOT / ".antigravity/harness-router/sessions"
    if not directory.resolve().is_relative_to(PROJECT_ROOT.resolve()):
        raise ValueError("Redirect state must stay in the project")
    return directory / f"{key}.redirected"


def stop(payload: dict[str, Any]) -> dict[str, Any]:
    # A Stop with background work still running must not re-arm the same turn.
    if payload.get("fullyIdle") is True and (marker := redirect_marker(payload)):
        marker.unlink(missing_ok=True)
    return {}


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    call = payload.get("toolCall")
    if not isinstance(call, dict) or not isinstance(call.get("args"), dict):
        return {}
    current = call.get("name")
    if not isinstance(current, str) or not current or is_router(current):
        return {}
    marker = redirect_marker(payload)
    if marker is None or marker.exists():
        return {}
    tools = load_tools(payload)
    intent = call["args"].get("Description") or call["args"].get("Instruction") or ""
    goal = (
        f"Choose the best next Antigravity tool for the intent in the pending tool call. {intent}"
    )[:1600]
    candidates = shortlist(tools, current, goal)
    endpoint = os.environ.get("HARNESS_ROUTER_MCP_URL", "http://127.0.0.1:8765/mcp")
    if len(candidates) < 2:
        return {}
    observation = (
        f"Antigravity is about to call {current!r}. "
        f"tool_input={json.dumps(call['args'], ensure_ascii=False)[:600]}"
    )[:900]
    result, mode = route(endpoint, str(PROJECT_ROOT), goal, observation, current, candidates)
    selected, confidence = result.get("tool"), result.get("confidence")
    minimum = env_number("HARNESS_ROUTER_PRETOOL_MIN_CONFIDENCE", 0.80)
    if (
        result.get("fallback")
        or not isinstance(selected, str)
        or selected == current
        or selected not in {tool["name"] for tool in candidates}
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= minimum <= confidence <= 1
    ):
        return {}
    # Atomically claim the one redirect even when several tool hooks run at once.
    # If state cannot be saved, main abstains instead of risking a redirect loop.
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {}
    os.close(descriptor)
    return {
        "decision": "deny",
        "reason": (
            f"Harness Router ({mode}) recommends {selected!r} instead of {current!r} "
            f"at confidence {confidence:.3f}. Re-plan once, keeping the user's goal and "
            "the tool's required arguments. Normal permissions still apply."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="reset when Antigravity becomes idle")
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        handler = stop if args.stop else handle
        result = handler(payload) if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        result = {}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
