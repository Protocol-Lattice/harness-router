#!/usr/bin/env python3
"""Stop-hook decision gate for Harness Router.

Uses the next-tool decision already computed by PostToolUse/UserPromptSubmit.
If a fresh high-confidence next tool exists, the harness is asked to continue
instead of ending the current decision loop. The hook is fail-open.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def repo_root(cwd: str) -> Path:
    p = Path(cwd).resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists():
            return candidate
    return p


def load_decision(cwd: str, session_id: str) -> dict:
    path = repo_root(cwd) / ".harness-router" / "sessions" / f"{session_id}.decision.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def should_continue(payload: dict) -> tuple[bool, str]:
    if payload.get("stop_hook_active"):
        return False, ""

    cwd = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or "default")
    decision = load_decision(cwd, session)
    if not decision:
        return False, ""

    try:
        age = time.time() - float(
            __import__("datetime").datetime.fromisoformat(
                str(decision.get("generated_at", "")).replace("Z", "+00:00")
            ).timestamp()
        )
        ttl = float(os.environ.get("HARNESS_ROUTER_STOP_DECISION_TTL", "15"))
        confidence = float(decision.get("confidence"))
        threshold = float(
            os.environ.get("HARNESS_ROUTER_STOP_CONFIDENCE", "0.85")
        )
    except (TypeError, ValueError, OverflowError):
        return False, ""

    tool = decision.get("tool")
    if (
        age < 0
        or age > max(1.0, ttl)
        or not isinstance(tool, str)
        or not tool
        or confidence < threshold
    ):
        return False, ""

    reason = (
        f"Harness Router selected {tool!r} as the next action "
        f"with confidence {confidence:.3f}. Continue the agent loop before stopping."
    )
    return True, reason


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    proceed, reason = should_continue(payload)
    result = {"continue": proceed}
    if reason:
        result["reason"] = reason
        result["tool"] = reason.split("'")[1] if "'" in reason else None
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
