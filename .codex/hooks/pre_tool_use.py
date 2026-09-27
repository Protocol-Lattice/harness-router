#!/usr/bin/env python3
"""Codex PreToolUse bridge for harness-router.

Loads the generated tool catalog produced by discover_tools.py, merges the
currently observed tool, and sends the complete candidate set to harness-router.
Failures are deliberately fail-open.
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


def _deny(*, selected: str, current: str, confidence: float | None, tool_count: int) -> None:
    confidence_text = f" at confidence {confidence:.3f}" if confidence is not None else ""
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"harness-router selected {selected!r}{confidence_text} instead of "
                    f"the pending Codex tool {current!r}"
                ),
                "additionalContext": (
                    f"Harness Router evaluated {tool_count} tools from the generated "
                    f"session catalog and recommends {selected!r} instead of {current!r}. "
                    "Re-plan once and preserve the original user goal."
                ),
            }
        }
    )


def _repo_root(cwd: str) -> Path:
    try:
        p = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        if p.stdout.strip():
            return Path(p.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd)


def _normalize(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("tools", [])
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "description": str(item.get("description", "")).strip(),
                "category": item.get("category"),
                "risk": str(item.get("risk", "medium")).lower(),
                "input_schema": item.get("input_schema") or item.get("parameters"),
            }
        )
    return out


def _load_tools(root: Path, session_id: str) -> tuple[list[dict[str, Any]], Path]:
    session = root / ".codex" / "harness-router" / "sessions" / f"{session_id}.json"
    generated = root / ".codex" / "harness-router-tools.json"

    for path in (session, generated):
        try:
            tools = _normalize(json.loads(path.read_text(encoding="utf-8")))
            if tools:
                return tools, session
        except (OSError, json.JSONDecodeError):
            pass
    return [], session


def _infer_current_tool(tool_name: str) -> dict[str, Any]:
    lowered = tool_name.lower()
    if tool_name == "Bash" or "exec" in lowered or "shell" in lowered:
        category, risk = "execute", "medium"
    elif "patch" in lowered or "write" in lowered or "edit" in lowered:
        category, risk = "mutate", "medium"
    elif "read" in lowered or "search" in lowered or "list" in lowered or "fetch" in lowered:
        category, risk = "inspect", "low"
    else:
        category, risk = "general", "medium"

    return {
        "name": tool_name,
        "description": f"Codex runtime tool observed by PreToolUse: {tool_name}",
        "category": category,
        "risk": risk,
        "input_schema": None,
    }


def _persist_session(path: Path, session_id: str, tools: list[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"generated": True, "session_id": session_id, "tools": tools},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(filter(None, (_extract_text(x) for x in value))).strip()
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

    try:
        path = Path(path_value)
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


def _compact_tool_input(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    return text[:600]


def _router_binary() -> str | None:
    return (
        os.environ.get("HARNESS_ROUTER_BIN")
        or shutil.which("har")
        or shutil.which("harness-router")
    )


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
    session_id = str(payload.get("session_id") or "default")
    tools, session_path = _load_tools(root, session_id)

    if not any(tool.get("name") == current for tool in tools):
        tools.append(_infer_current_tool(current))
        _persist_session(session_path, session_id, tools)

    if len(tools) < 2:
        _allow()
        return 0

    goal = (
        _goal_from_transcript(payload.get("transcript_path"))
        or "Choose the best next Codex tool for the current task."
    )
    observation = (
        f"Codex is about to call {current!r}. "
        f"tool_input={_compact_tool_input(payload.get('tool_input'))}"
    )[:MAX_OBSERVATION_CHARS]

    binary = _router_binary()
    if not binary:
        _allow()
        return 0

    timeout = float(
        os.environ.get(
            "HARNESS_ROUTER_PRETOOL_TIMEOUT",
            DEFAULT_TIMEOUT_SECONDS,
        )
    )

    try:
        p = subprocess.run(
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

    if p.returncode != 0:
        _allow()
        return 0

    try:
        result = json.loads(p.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        _allow()
        return 0

    selected = result.get("tool")
    if (
        result.get("fallback")
        or not isinstance(selected, str)
        or not selected
        or selected == current
    ):
        _allow()
        return 0

    confidence_value = result.get("confidence")
    confidence = (
        float(confidence_value)
        if isinstance(confidence_value, (int, float))
        else None
    )
    _deny(
        selected=selected,
        current=current,
        confidence=confidence,
        tool_count=len(tools),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
