#!/usr/bin/env python3
"""claude PostToolUse -> Harness Router next-decision precompute bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def _catalog(payload: dict) -> list[dict]:
    tools = payload.get("tools")
    if isinstance(tools, list):
        return tools
    path = ROOT / ".claude/harness-router-tools.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tools = value.get("tools", value) if isinstance(value, dict) else value
    return tools if isinstance(tools, list) else []


def _goal(session_id: str) -> str:
    path = ROOT / ".harness-router" / "sessions" / f"{session_id}.decision.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(value.get("goal") or "").strip()[:1600] if isinstance(value, dict) else ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        print("{}")
        return 0

    session_id = str(payload.get("session_id") or payload.get("conversationId") or "default")
    cwd = str(payload.get("cwd") or ROOT)
    tools = _catalog(payload)
    goal = str(payload.get("goal") or "").strip()[:1600] or _goal(session_id)
    if not goal or len(tools) < 2:
        print("{}")
        return 0

    tool_name = str(
        payload.get("tool_name")
        or (payload.get("toolCall") or {}).get("name")
        or ""
    ).strip()
    response = (
        payload.get("tool_response")
        or payload.get("tool_result")
        or payload.get("toolResult")
        or payload.get("error")
        or ""
    )
    if not isinstance(response, str):
        response = json.dumps(response, ensure_ascii=False)

    request = {
        "harness": "claude",
        "hook": "PostToolUse",
        "cwd": cwd,
        "session_id": session_id,
        "decision_id": str(payload.get("decision_id") or payload.get("turn_id") or payload.get("stepIdx") or ""),
        "goal": goal,
        "observation": response[:4000],
        "last_action": tool_name or None,
        "tools": tools,
    }

    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "post_tool_use.py")],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=float(os.environ.get("HARNESS_ROUTER_POSTTOOL_TIMEOUT", "4")),
            cwd=cwd,
            env=os.environ.copy(),
            check=False,
        )
        if proc.stderr:
            print(proc.stderr.rstrip(), file=sys.stderr)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
