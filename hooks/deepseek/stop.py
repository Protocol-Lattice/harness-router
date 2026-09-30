#!/usr/bin/env python3
"""Harness Router Stop hook: decide whether the agent should continue."""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def emit(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def load_decision(cwd: Path, session: str) -> dict:
    path = cwd / ".harness-router" / "sessions" / f"{session}.decision.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def fresh(value: dict) -> bool:
    try:
        generated = datetime.fromisoformat(
            str(value.get("generated_at", "")).replace("Z", "+00:00")
        )
        ttl = float(os.environ.get("HARNESS_ROUTER_PREDECISION_TTL", "15"))
        return (datetime.now(UTC) - generated).total_seconds() <= ttl
    except (TypeError, ValueError):
        return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # Native Stop hooks may expose this guard. Never recursively force a stop.
    if payload.get("stop_hook_active") is True:
        emit({"continue": False})
        return 0

    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    session = str(payload.get("session_id") or "default")
    decision = load_decision(cwd, session)

    try:
        confidence = float(decision.get("confidence"))
        threshold = float(
            os.environ.get("HARNESS_ROUTER_PREDECISION_THRESHOLD", "0.85")
        )
    except (TypeError, ValueError):
        emit({"continue": False})
        return 0

    selected = str(decision.get("tool") or "").strip()
    if not selected or not fresh(decision) or confidence < threshold:
        emit({"continue": False})
        return 0

    reason = (
        f"Harness Router selected '{selected}' as the next tool at "
        f"confidence {confidence:.3f}; continue the agent loop and route that tool."
    )
    emit({"continue": True, "reason": reason, "tool": selected})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
