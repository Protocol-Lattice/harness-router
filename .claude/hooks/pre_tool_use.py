#!/usr/bin/env python3
"""Route Claude Code tool choices through harness-router; fail open on errors.

This portable adapter preserves the Codex hook's shortlisting and optional MCTS
flow without depending on .codex, an installed Python package, or an MCP SDK.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
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


def _route_via_daemon(
    goal: str,
    observation: str,
    candidates: list[dict[str, Any]],
    timeout: float,
) -> dict[str, Any] | None:
    """Use an existing local router daemon before starting the CLI subprocess."""
    if os.name == "nt" or os.environ.get("HARNESS_ROUTER_NO_DAEMON") == "1":
        return None
    configured = os.environ.get("HARNESS_ROUTER_SOCKET")
    if configured:
        path = configured
    else:
        uid = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
        path = str(Path(tempfile.gettempdir()) / f"harness-router-{uid}.sock")
    request = {
        "goal": goal,
        "observation": observation,
        "last_action": None,
        "tools": [
            {key: tool.get(key) for key in ("name", "description", "category", "risk")}
            for tool in candidates
        ],
        "mode": "jev_only",
        "direct_threshold": 0.85,
        "fallback_threshold": 0.60,
        "hierarchical_threshold": 24,
        "route_cache_size": 0,
    }
    wire = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(path)
            client.sendall(wire)
            response = bytearray()
            while len(response) <= 64 * 1024:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if b"\n" in chunk:
                    break
        result = json.loads(response.split(b"\n", 1)[0])
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict) or result.get("provider_requests", 0) < 1:
        return None
    return result


def route(
    binary: str | None,
    cwd: str | Path,
    goal: str,
    observation: str,
    current: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    timeout = float(os.environ.get("HARNESS_ROUTER_PRETOOL_TIMEOUT", "4.0"))
    if timeout <= 0:
        return {}, "route"
    daemon_result = _route_via_daemon(goal, observation, candidates, timeout)
    if daemon_result is not None:
        return daemon_result, "route"
    if not binary:
        return {}, "route"
    tools_json = json.dumps(
        [
            {key: tool.get(key) for key in ("name", "description", "category", "risk")}
            for tool in candidates
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    try:
        proc = subprocess.run(
            [
                binary,
                "route",
                "--mode", "jev_only",
                "--verbose",
                "--no-cache",
                "--goal", goal,
                "--observation", observation,
                "--tools-json", tools_json,
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}, "route"
    if proc.returncode != 0:
        return {}, "route"
    try:
        result = json.loads(proc.stdout.strip())
    except json.JSONDecodeError:
        return {}, "route"
    if not isinstance(result, dict) or result.get("provider_requests", 0) < 1:
        return {}, "route"
    return result, "route"


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
    binary = os.environ.get("HARNESS_ROUTER_BIN") or shutil.which("harness-router")
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


def _run_hook() -> int:
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict):
            handle(payload)
        else:
            emit()
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        emit()
    return 0


def _append_hook_timing(event: dict[str, Any], root: Path) -> None:
    try:
        path = root / "hook-timings.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        pass


def main() -> int:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    try:
        return _run_hook()
    finally:
        event = {
            "event": "harness_router.hook_timing",
            "hook": "claude",
            "started_at": started_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }
        print(json.dumps(event, separators=(",", ":")), file=sys.stderr)
        _append_hook_timing(event, project_root())


if __name__ == "__main__":
    raise SystemExit(main())
