#!/usr/bin/env python3
"""DeepSeek Harness command hook for Harness Router.

DeepSeek Harness exposes tools/pre-execute natively. The supported
@deepseek-ai/dsh-hooks-codex bridge maps Codex PreToolUse command hooks onto
that interception point, so this adapter keeps the existing routing protocol.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 4.0
MAX_CANDIDATES = 8


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def allow() -> None:
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse"}})


def deny(selected: str, current: str, confidence: float | None, count: int) -> None:
    confidence_text = f" at confidence {confidence:.3f}" if confidence is not None else ""
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"harness-router selected {selected!r}{confidence_text} instead of "
                f"the pending DeepSeek tool {current!r}"
            ),
            "additionalContext": (
                f"Harness Router evaluated {count} similar tools and recommends "
                f"{selected!r} instead of {current!r}. Re-plan once and preserve "
                "the original user goal."
            ),
        }
    })


def load_tools(cwd: Path) -> list[dict[str, Any]]:
    raw_json = os.environ.get("HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON", "").strip()
    raw_file = os.environ.get("HARNESS_ROUTER_DEEPSEEK_TOOLS_FILE", "").strip()
    values: Any
    if raw_json:
        try:
            values = json.loads(raw_json)
        except json.JSONDecodeError:
            return []
    elif raw_file:
        try:
            values = json.loads(Path(raw_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    else:
        try:
            values = json.loads(
                (cwd / ".dsh" / "harness-router-tools.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return []

    if isinstance(values, dict):
        values = values.get("tools", [])
    if not isinstance(values, list):
        return []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({
            "name": name,
            "description": str(item.get("description", "")).strip(),
            "category": item.get("category"),
            "risk": str(item.get("risk", "medium")).lower(),
        })
    return result


def tokens(value: str) -> set[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return {
        token.lower()
        for token in re.split(r"[^a-zA-Z0-9]+", value)
        if len(token) >= 2
    }


def similarity(candidate: dict[str, Any], current: dict[str, Any]) -> float:
    candidate_name = str(candidate["name"])
    current_name = str(current["name"])
    score = SequenceMatcher(None, current_name.lower(), candidate_name.lower()).ratio() * 10
    score += len(tokens(candidate_name) & tokens(current_name)) * 4
    score += len(
        tokens(str(candidate.get("description", "")))
        & tokens(str(current.get("description", "")))
    ) * 1.5
    if candidate.get("category") and candidate.get("category") == current.get("category"):
        score += 2
    return score


def shortlist(tools: list[dict[str, Any]], current: str) -> list[dict[str, Any]]:
    current_tool = next((tool for tool in tools if tool["name"] == current), None)
    if current_tool is None:
        return []
    others = [
        tool for tool in tools
        if tool["name"] != current and "harness-router" not in tool["name"].lower()
    ]
    others.sort(key=lambda tool: similarity(tool, current_tool), reverse=True)
    return [current_tool, *others[: MAX_CANDIDATES - 1]]


def extract_result(response: dict[str, Any]) -> dict[str, Any]:
    structured = response.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in response.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def route(endpoint: str, cwd: Path, current: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    import time
    import urllib.error
    import urllib.request

    timeout = float(os.environ.get("HARNESS_ROUTER_PRETOOL_TIMEOUT", DEFAULT_TIMEOUT))
    deadline = time.monotonic() + timeout
    next_id = 0

    def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal next_id
        next_id += 1
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": next_id,
            "method": method,
            "params": params,
        }).encode("utf-8")
        req = urllib.request.Request(
            endpoint.rstrip("/"),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "MCP-Protocol-Version": "2025-06-18",
            },
            method="POST",
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Harness Router timed out")
        try:
            with urllib.request.urlopen(req, timeout=remaining) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError("Harness Router HTTP endpoint unavailable") from exc
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            return {}
        if "error" in value:
            raise RuntimeError("Harness Router returned an MCP error")
        result = value.get("result")
        return result if isinstance(result, dict) else {}

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            return {}
        return extract_result(result)

    request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "harness-router-deepseek-hook", "version": "1"},
        },
    )

    compact_tools = [
        {
            "name": tool["name"],
            "description": tool["description"],
            "category": tool.get("category"),
            "risk": tool.get("risk", "medium"),
        }
        for tool in candidates
    ]
    result = call(
        "route",
        {
            "goal": "Choose the best next tool for the current DeepSeek Harness task.",
            "observation": (
                f"DeepSeek Harness is about to call {current!r}. "
                "Check whether another candidate is a better fit."
            ),
            "tools": compact_tools,
        },
    )

    graph_command = os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD", "").strip()
    threshold = float(os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD", "0.80"))
    confidence = result.get("confidence")
    confident = (
        isinstance(confidence, (float, int))
        and not isinstance(confidence, bool)
        and confidence >= threshold
        and not result.get("fallback")
    )
    remaining = deadline - time.monotonic()
    if not graph_command or confident or len(candidates) < 3 or remaining <= 0:
        return result

    graph_proc = subprocess.run(
        shlex.split(graph_command),
        cwd=cwd,
        input=json.dumps({
            "goal": "Choose the best next tool for the current DeepSeek Harness task.",
            "observation": f"DeepSeek Harness is about to call {current!r}.",
            "current_tool": current,
            "candidates": candidates,
            "route_result": result,
        }),
        capture_output=True,
        text=True,
        timeout=remaining,
        check=False,
    )
    if graph_proc.returncode:
        return result
    try:
        graph = json.loads(graph_proc.stdout)
    except json.JSONDecodeError:
        return result
    if (
        not isinstance(graph, dict)
        or not isinstance(graph.get("root_state"), str)
        or not isinstance(graph.get("states"), list)
        or not isinstance(graph.get("transitions"), list)
        or deadline <= time.monotonic()
    ):
        return result

    mcts = call(
        "route_mcts",
        {
            "root_state": graph["root_state"],
            "states": graph["states"],
            "transitions": graph["transitions"],
            "simulations": graph.get("simulations", 4096),
            "max_depth": graph.get("max_depth", 3),
            "use_jev_prior": graph.get("use_jev_prior", True),
        },
    )
    return mcts or result
def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        allow()
        return 0

    if payload.get("hook_event_name") != "PreToolUse":
        allow()
        return 0

    current = str(payload.get("tool_name", "")).strip()
    if not current or "harness-router" in current.lower():
        allow()
        return 0

    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    tools = load_tools(cwd)
    if len(tools) < 2 or not any(tool["name"] == current for tool in tools):
        allow()
        return 0

    candidates = shortlist(tools, current)
    if len(candidates) < 2:
        allow()
        return 0

    endpoint = os.environ.get("HARNESS_ROUTER_MCP_URL", "http://127.0.0.1:8765/mcp")
    if not endpoint:
        allow()
        return 0

    try:
        result = route(endpoint=endpoint, cwd=cwd, current=current, candidates=candidates)
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError):
        allow()
        return 0

    selected = result.get("tool")
    if result.get("fallback") or not isinstance(selected, str) or not selected or selected == current:
        allow()
        return 0

    raw_confidence = result.get("confidence")
    confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else None
    deny(selected, current, confidence, len(candidates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
