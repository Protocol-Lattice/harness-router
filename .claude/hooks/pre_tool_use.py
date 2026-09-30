#!/usr/bin/env python3
"""Claude Code PreToolUse: validate the precomputed next-tool decision locally."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys


def emit(**fields: object) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **fields}},
        separators=(",", ":")))


def root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def load_decision(repo: Path, session_id: str) -> dict[str, object]:
    path = repo / ".harness-router" / "sessions" / f"{session_id}.decision.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def fresh(decision: dict[str, object]) -> bool:
    try:
        generated = datetime.fromisoformat(
            str(decision.get("generated_at", "")).replace("Z", "+00:00")
        )
        ttl = float(os.environ.get("HARNESS_ROUTER_PREDECISION_TTL", "30"))
        return (datetime.now(UTC) - generated).total_seconds() <= ttl
    except (TypeError, ValueError):
        return False


def marker(repo: Path, session_id: str, decision: dict[str, object], current: str) -> Path:
    key = f"{session_id}:{decision.get('state_key','')}:{current}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    return repo / ".harness-router" / "sessions" / f"{digest}.rerouted"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        emit()
        return 0
    if not isinstance(payload, dict):
        emit()
        return 0

    current = str(payload.get("tool_name") or "").strip()
    if not current or "harness_router" in current.lower().replace("-", "_"):
        emit()
        return 0

    repo = root()
    session = str(payload.get("session_id") or "default")
    decision = load_decision(repo, session)
    selected = decision.get("tool")
    confidence = decision.get("confidence")

    if not selected or not isinstance(selected, str) or not fresh(decision):
        emit()
        return 0

    if selected == current:
        emit()
        return 0

    try:
        threshold = float(os.environ.get("HARNESS_ROUTER_PREDECISION_THRESHOLD", "0.85"))
        value = float(confidence)
    except (TypeError, ValueError):
        emit()
        return 0

    gate = marker(repo, session, decision, current)
    if value < threshold or gate.exists():
        emit()
        return 0

    gate.parent.mkdir(parents=True, exist_ok=True)
    try:
        gate.write_text(json.dumps({"selected": selected, "current": current}), encoding="utf-8")
    except OSError:
        emit()
        return 0

    reason = (
        f"Harness Router precomputed {selected!r} at confidence {value:.3f}, "
        f"but Claude Code selected {current!r}. Re-plan once using the current goal."
    )
    emit(permissionDecision="deny", permissionDecisionReason=reason, additionalContext=reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
