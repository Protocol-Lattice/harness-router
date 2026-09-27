#!/usr/bin/env python3
"""Codex PreToolUse bridge for harness-router.

Codex sends the hook the currently selected tool, but not the whole runtime tool
registry. This bridge loads the candidate registry from
.codex/harness-router-tools.json (or HARNESS_ROUTER_CODEX_TOOLS_FILE), adds the
currently observed tool if necessary, asks harness-router to route the choice,
and blocks only when the router confidently selects a different tool.

Failures are deliberately fail-open: Codex keeps its original tool choice.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any


MAX_GOAL_CHARS = 1600
MAX_OBSERVATION_CHARS = 900
MAX_TRANSCRIPT_BYTES = 256_000
DEFAULT_TIMEOUT_SECONDS = 4.0


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _allow() -> None:
    _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse"}})


def _deny(*, selected: str, current: str, confidence: float | None) -> None:
    confidence_text = f" at confidence {confidence:.3f}" if confidence is not None else ""
    reason = (
        f"harness-router selected {selected!r}{confidence_text} instead of "
        f"the pending Codex tool {current!r}"
    )
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
                "additionalContext": (
                    f"Harness-router recommends using {selected!r} instead of "
                    f"{current!r} for the next step. Re-plan once, preserve the "
                    "original user goal, and keep normal Codex sandbox/approval rules."
                ),
            }
        }
    )


def _repo_root(cwd: str) -> Path:
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        root = completed.stdout.strip()
        if root:
            return Path(root)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd)


def _load_tools(root: Path) -> list[dict[str, Any]]:
    configured = os.environ.get("HARNESS_ROUTER_CODEX_TOOLS_FILE")
    manifest = Path(configured).expanduser() if configured else root / ".codex" / "harness-router-tools.json"

    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    items = raw.get("tools", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []

    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        tools.append(
            {
                "name": name,
                "description": str(item.get("description", "")).strip(),
                "category": item.get("category"),
                "risk": str(item.get("risk", "medium")).lower(),
            }
        )
    return tools


def _infer_current_tool(tool_name: str) -> dict[str, Any]:
    lowered = tool_name.lower()
    if tool_name == "Bash" or "exec" in lowered or "shell" in lowered:
        category, risk = "execute", "medium"
    elif "patch" in lowered or "write" in lowered or "edit" in lowered:
        category, risk = "mutate", "medium"
    elif "read" in lowered or "search" in lowered or "list" in lowered:
        category, risk = "inspect", "low"
    elif "spawn" in lowered or "agent" in lowered:
        category, risk = "execute", "medium"
    else:
        category, risk = "general", "medium"

    return {
        "name": tool_name,
        "description": f"Codex runtime tool observed by PreToolUse: {tool_name}",
        "category": category,
        "risk": risk,
    }


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_extract_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "input_text", "content"):
            if key in value:
                text = _extract_text(value[key])
                if text:
                    return text
    return ""


def _find_user_text(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("role") == "user":
            text = _extract_text(value.get("content"))
            if text:
                return text
        payload = value.get("payload")
        if payload is not None:
            text = _find_user_text(payload)
            if text:
                return text
        for child in value.values():
            text = _find_user_text(child)
            if text:
                return text
    elif isinstance(value, list):
        for child in reversed(value):
            text = _find_user_text(child)
            if text:
                return text
    return ""


def _goal_from_transcript(path_value: Any) -> str:
    if not isinstance(path_value, str) or not path_value:
        return ""

    path = Path(path_value)
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - MAX_TRANSCRIPT_BYTES))
            chunk = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""

    for line in reversed(chunk.splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _find_user_text(item)
        if text:
            return text[:MAX_GOAL_CHARS]
    return ""


def _compact_tool_input(tool_input: Any) -> str:
    try:
        rendered = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = repr(tool_input)
    return rendered[:600]


def _router_binary() -> str | None:
    override = os.environ.get("HARNESS_ROUTER_BIN")
    if override:
        return override
    return shutil.which("har") or shutil.which("harness-router")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        _allow()
        return 0

    if payload.get("hook_event_name") != "PreToolUse":
        _allow()
        return 0

    current = str(payload.get("tool_name", "")).strip()
    if not current:
        _allow()
        return 0

    lowered = current.lower()
    if "harness-router" in lowered and ("route" in lowered or "mcts" in lowered):
        _allow()
        return 0

    cwd = str(payload.get("cwd") or os.getcwd())
    root = _repo_root(cwd)
    tools = _load_tools(root)
    if not any(tool.get("name") == current for tool in tools):
        tools.append(_infer_current_tool(current))

    if len(tools) < 2:
        _allow()
        return 0

    goal = _goal_from_transcript(payload.get("transcript_path"))
    if not goal:
        goal = "Choose the best next Codex tool for the current task."

    observation = (
        f"Codex is about to call {current!r}. "
        f"tool_input={_compact_tool_input(payload.get('tool_input'))}"
    )[:MAX_OBSERVATION_CHARS]

    binary = _router_binary()
    if not binary:
        _allow()
        return 0

    timeout = float(os.environ.get("HARNESS_ROUTER_PRETOOL_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
    try:
        completed = subprocess.run(
            [
                binary,
                "route",
                "--goal",
                goal,
                "--observation",
                observation,
                "--tools-json",
                json.dumps(tools, ensure_ascii=False, separators=(",", ":")),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        _allow()
        return 0

    if completed.returncode != 0:
        _allow()
        return 0

    try:
        result = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        _allow()
        return 0

    selected = result.get("tool")
    if result.get("fallback") or not isinstance(selected, str) or not selected:
        _allow()
        return 0

    if selected == current:
        _allow()
        return 0

    confidence_value = result.get("confidence")
    confidence = (
        float(confidence_value)
        if isinstance(confidence_value, (int, float))
        else None
    )
    _deny(selected=selected, current=current, confidence=confidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
