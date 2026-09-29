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
    cwd: str | Path,
    goal: str,
    observation: str,
    current: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    timeout = float(os.environ.get("HARNESS_ROUTER_PRETOOL_TIMEOUT", "4.0"))
    if timeout <= 0:
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


def load_tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_json = os.environ.get("HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON", "").strip()
    if raw_json:
        try:
            return normalize_tools(json.loads(raw_json))
        except json.JSONDecodeError:
            return []
    conversation = payload.get("conversationId")
    if not isinstance(conversation, str) or not conversation.strip():
        return []
    snapshot = PROJECT_ROOT / ".antigravity/harness-router-tools.json"
    try:
        data = json.loads(snapshot.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return normalize_tools(data)


def redirect_marker(payload: dict[str, Any]) -> Path:
    conversation = str(payload.get("conversationId") or "default")
    digest = hashlib.sha256(conversation.encode("utf-8")).hexdigest()
    return PROJECT_ROOT / ".antigravity/harness-router/sessions" / f"{digest}.redirected"


def stop(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("fullyIdle") is not True:
        return {}
    marker = redirect_marker(payload)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return {}
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
    binary = os.environ.get("HARNESS_ROUTER_BIN") or shutil.which("harness-router")
    if len(candidates) < 2 or not binary:
        return {}
    observation = (
        f"Antigravity is about to call {current!r}. "
        f"tool_input={json.dumps(call['args'], ensure_ascii=False)[:600]}"
    )[:900]
    result, mode = route(binary, str(PROJECT_ROOT), goal, observation, current, candidates)
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
