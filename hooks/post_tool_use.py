#!/usr/bin/env python3
"""Precompute the next Harness Router decision after a tool completes.

Provider adapters normalize their native PostToolUse payload into this contract.
The shared state-aware pre_decision hook performs the actual route/cache operation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def _repo_root(cwd: str) -> Path:
    current = Path(cwd).resolve()
    for parent in (current, *current.parents):
        if (parent / ".git").exists():
            return parent
    return current


def _load_latest_goal(root: Path, session_id: str) -> str:
    path = root / ".harness-router" / "sessions" / f"{session_id}.decision.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(value.get("goal") or "").strip()[:1600] if isinstance(value, dict) else ""


def _load_tools(root: Path, harness: str, session_id: str) -> list[dict[str, object]]:
    candidates = [
        root / f".{harness}" / "harness-router-tools.json",
        root / f".{harness}" / "harness-router" / "sessions" / f"{session_id}.json",
        root / ".harness-router" / "tools.json",
    ]
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tools = value.get("tools", value) if isinstance(value, dict) else value
        if isinstance(tools, list):
            return [item for item in tools if isinstance(item, dict)]
    return []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        print(json.dumps({}))
        return 0

    harness = str(payload.get("harness") or "").strip().lower()
    cwd = str(payload.get("cwd") or os.getcwd())
    root = _repo_root(cwd)
    session_id = str(payload.get("session_id") or "default")
    goal = str(payload.get("goal") or "").strip()[:1600] or _load_latest_goal(root, session_id)

    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = _load_tools(root, harness, session_id)

    if not goal or len(tools) < 2:
        print(json.dumps({}))
        return 0

    tool_name = str(payload.get("tool_name") or "").strip()
    response = payload.get("tool_response")
    if response in (None, ""):
        response = payload.get("tool_result")
    if response in (None, ""):
        response = payload.get("error")

    try:
        observation = (
            json.dumps(response, ensure_ascii=False)
            if not isinstance(response, str)
            else response
        )
    except (TypeError, ValueError):
        observation = str(response)

    request = {
        "harness": harness,
        "hook": "PostToolUse",
        "cwd": cwd,
        "session_id": session_id,
        "decision_id": str(
            payload.get("decision_id")
            or payload.get("turn_id")
            or payload.get("step_idx")
            or ""
        ),
        "goal": goal,
        "observation": observation[:4000],
        "last_action": tool_name or None,
        "tools": tools,
    }

    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().with_name("pre_decision.py"))],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=float(os.environ.get("HARNESS_ROUTER_POSTTOOL_TIMEOUT", "4")),
            cwd=cwd,
            env=os.environ.copy(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        print(json.dumps({}))
        return 0

    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)

    # Expose the computed decision to provider-specific PostToolUse adapters.
    # The decision is also persisted in the shared session store, so adapters
    # that cannot return model context can consume it at their next pre-invocation
    # checkpoint.
    try:
        decision = json.loads(proc.stdout.strip().splitlines()[0]) if proc.stdout.strip() else {}
    except (ValueError, TypeError):
        decision = {}
    if not isinstance(decision, dict):
        decision = {}
    print(json.dumps(decision, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
