#!/usr/bin/env python3
"""Portable ohmypi routing bridge. Reads a live tool_call snapshot on stdin.

The extension owns catalog discovery and the per-turn redirect guard. This
stdlib-only bridge uses the same shortlisting/MCP/MCTS flow as the Claude hook.
"""

from __future__ import annotations

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
from typing import Any


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
    if pending is None:
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

    others = [tool for tool in tools if tool["name"] != current and not is_router(tool["name"])]
    return [pending, *sorted(others, key=score, reverse=True)[: limit - 1]]


class MCPClient:
    """Line-delimited JSON-RPC with a deadline, including partial-line reads."""

    def __init__(self, binary: str, cwd: str, timeout: float) -> None:
        self.deadline = time.monotonic() + timeout
        self.proc = subprocess.Popen(
            [binary],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.next_id = 0
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self.messages.put(line)
        except (OSError, UnicodeError):
            pass
        finally:
            self.messages.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Harness Router timed out")
            try:
                line = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("Harness Router timed out") from exc
            if line is None:
                raise RuntimeError("Harness Router closed its output")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != self.next_id:
                continue
            if "error" in message:
                raise RuntimeError("Harness Router returned an MCP error")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

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
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=0.2)
        self.reader.join(timeout=0.2)
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout and not self.reader.is_alive():
            self.proc.stdout.close()


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
    client = MCPClient(binary, cwd, timeout)
    try:
        client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "harness-router-ohmypi-hook", "version": "1"},
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


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    current = payload.get("tool_name")
    if (
        payload.get("hook_event_name") != "tool_call"
        or not isinstance(current, str)
        or not current
        or is_router(current)
    ):
        return {}
    tools = normalize_tools(payload.get("tools"))
    goal = str(payload.get("goal") or "Choose the best next ohmypi tool for the current task.")
    goal = goal[:1600]
    candidates = shortlist(tools, current, goal)
    binary = os.environ.get("HARNESS_ROUTER_MCP_BIN") or shutil.which("harness-router-mcp")
    if len(candidates) < 2 or not binary:
        return {}
    observation = (
        f"ohmypi is about to call {current!r}. "
        f"tool_input={json.dumps(payload.get('tool_input'), ensure_ascii=False)[:600]}"
    )[:900]
    result, mode = route(
        binary, str(payload.get("cwd") or os.getcwd()), goal, observation, current, candidates
    )
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
    return {
        "block": True,
        "tool": selected,
        "reason": (
            f"Harness Router ({mode}) recommends {selected!r} instead of {current!r} "
            f"at confidence {confidence:.3f}. Re-plan once, keeping the user's goal and "
            "the tool's required arguments. Normal permissions still apply."
        ),
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = handle(payload) if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        result = {}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
