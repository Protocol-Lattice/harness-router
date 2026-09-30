#!/usr/bin/env python3
"""Codex bridge for the shared state-aware Harness Router pre-decision hook."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve()
    catalog = cwd / ".codex" / "harness-router-tools.json"
    tools = payload.get("tools")
    if not isinstance(tools, list):
        try:
            raw = json.loads(catalog.read_text(encoding="utf-8"))
            tools = raw.get("tools", raw) if isinstance(raw, dict) else raw
        except (OSError, ValueError):
            tools = []

    request = {
        "harness": "codex",
        "hook": payload.get("hook_event_name", "UserPromptSubmit"),
        "cwd": str(cwd),
        "session_id": str(payload.get("session_id") or "default"),
        "decision_id": str(payload.get("decision_id") or payload.get("turn_id") or ""),
        "goal": str(payload.get("goal") or payload.get("prompt") or "").strip(),
        "observation": payload.get("observation") or payload.get("tool_result") or "",
        "last_action": payload.get("last_action"),
        "tools": tools,
    }

    try:
        proc = subprocess.run(
            [sys.executable, str(cwd / "hooks" / "pre_decision.py")],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=float(os.environ.get("HARNESS_ROUTER_PREDECISION_TIMEOUT", "4")),
            cwd=str(cwd),
            env=os.environ.copy(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0

    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)

    if not proc.stdout.strip():
        return 0

    try:
        decision = json.loads(proc.stdout.strip().splitlines()[0])
    except ValueError:
        return 0

    if not isinstance(decision, dict) or not decision.get("tool"):
        return 0

    confidence = decision.get("confidence")
    try:
        confidence_text = f"{float(confidence):.3f}"
    except (TypeError, ValueError):
        confidence_text = "unknown"

    event_name = str(payload.get("hook_event_name") or "UserPromptSubmit")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": (
                f"Harness Router selected {decision['tool']!r} as the next tool "
                f"(confidence {confidence_text}). Prefer it when applicable."
            ),
        }
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
