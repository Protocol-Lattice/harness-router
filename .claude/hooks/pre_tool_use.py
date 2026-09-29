#!/usr/bin/env python3
"""Route Claude Code tool choices through harness-router-mcp; fail open on errors.

This portable adapter preserves the Codex hook's shortlisting and optional MCTS
flow without depending on .codex, an installed Python package, or an MCP SDK.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from discover_tools import normalize_tools, project_root, session_catalog

MAX_TRANSCRIPT_BYTES = 256_000


def emit(**fields: Any) -> None:
    # No permissionDecision means abstain, preserving Claude's normal permission checks.
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **fields}}))


def is_router(name: str) -> bool:
    return "harness_router" in name.lower().replace("-", "_")


def load_tools(root: Path, session_id: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(session_catalog(root, session_id).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("session_id") == session_id:
            return normalize_tools(data)
    except (OSError, ValueError):
        pass
    return []


def user_context(path_value: Any) -> tuple[str, str]:
    """Get the latest actual user text, never a tool_result inside a user message."""
    if not isinstance(path_value, str) or not path_value:
        return "", ""
    try:
        with Path(path_value).open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - MAX_TRANSCRIPT_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return "", ""

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("isMeta") or entry.get("isCompactSummary"):
            continue
        message = entry.get("message", entry)
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
        else:
            continue
        if text.strip():
            return text.strip()[:1600], str(entry.get("uuid") or "")
    return "", ""


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
                "clientInfo": {"name": "harness-router-claude-hook", "version": "1"},
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


def redirect_marker(root: Path, payload: dict[str, Any], turn: str) -> Path:
    key = json.dumps([payload.get("session_id"), payload.get("agent_id"), turn])
    digest = hashlib.sha256(key.encode()).hexdigest()
    return root / ".claude" / "harness-router" / "redirects" / f"{digest}.json"


def handle(payload: dict[str, Any]) -> None:
    current = payload.get("tool_name")
    if (
        payload.get("hook_event_name") != "PreToolUse"
        or not isinstance(current, str)
        or not current
        or is_router(current)
    ):
        emit()
        return
    root = project_root()
    tools = load_tools(root, str(payload.get("session_id") or "default"))
    goal, prompt_uuid = user_context(payload.get("transcript_path"))
    goal = goal or "Choose the best next Claude Code tool for the current task."
    # Older Claude versions lack prompt_id. Use the actual user message's UUID/text.
    marker = redirect_marker(root, payload, str(payload.get("prompt_id") or prompt_uuid or goal))
    candidates = shortlist(tools, current, goal)
    endpoint = os.environ.get("HARNESS_ROUTER_MCP_URL", "http://127.0.0.1:8765/mcp")
    if len(candidates) < 2 or marker.exists():
        emit()
        return
    observation = (
        f"Claude Code is about to call {current!r}. "
        f"tool_input={json.dumps(payload.get('tool_input'), ensure_ascii=False)[:600]}"
    )[:900]
    result, mode = route(
        binary, str(payload.get("cwd") or root), goal, observation, current, candidates
    )
    selected, confidence = result.get("tool"), result.get("confidence")
    minimum = env_number("HARNESS_ROUTER_PRETOOL_MIN_CONFIDENCE", 0.80)
    if (
        result.get("fallback")
        or selected == current
        or selected not in {tool["name"] for tool in candidates}
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= minimum <= confidence <= 1
    ):
        emit()
        return
    # Atomically claim the one redirect for this user turn, including parallel tool calls.
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        with marker.open("x", encoding="utf-8") as handle:
            json.dump({"current": current, "selected": selected}, handle)
    except FileExistsError:
        emit()
        return
    reason = (
        f"Harness Router ({mode}) recommends {selected!r} instead of {current!r} "
        f"at confidence {confidence:.3f}. Re-plan once, keeping the user's goal and "
        "the tool's required arguments. Normal permissions still apply."
    )
    emit(permissionDecision="deny", permissionDecisionReason=reason, additionalContext=reason)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            handle(payload)
        else:
            emit()
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        emit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
