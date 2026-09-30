#!/usr/bin/env python3
"""Antigravity PreInvocation: expose the current Harness Router precomputed decision."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def _read_decision(conversation: str) -> dict:
    path = ROOT / ".harness-router" / "sessions" / f"{conversation}.decision.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_tools() -> list[dict]:
    try:
        value = json.loads(
            (ROOT / ".antigravity" / "harness-router-tools.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return []
    tools = value.get("tools", value) if isinstance(value, dict) else value
    return tools if isinstance(tools, list) else []


def _transcript_goal(path_value: object) -> str:
    if not isinstance(path_value, str) or not path_value:
        return ""
    try:
        text = Path(path_value).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        message = value.get("message", value)
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()[:1600]
        if isinstance(content, list):
            parts = [
                str(item.get("text"))
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if parts:
                return " ".join(parts).strip()[:1600]
    return ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        print("{}")
        return 0

    conversation = str(payload.get("conversationId") or "default")
    decision = _read_decision(conversation)
    goal = str(decision.get("goal") or "").strip() or _transcript_goal(payload.get("transcriptPath"))
    tools = _read_tools()

    # The first invocation has no PostToolUse state yet. Seed it here by calling
    # the shared pre-decision primitive once with the latest user goal.
    if goal and len(tools) >= 2 and not decision.get("tool"):
        request = {
            "harness": "antigravity",
            "hook": "PreInvocation",
            "cwd": str(ROOT),
            "session_id": conversation,
            "decision_id": str(payload.get("invocationNum") or ""),
            "goal": goal,
            "observation": "Before model invocation; no tool result yet.",
            "last_action": None,
            "tools": tools,
        }
        try:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "hooks" / "pre_decision.py")],
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=float(os.environ.get("HARNESS_ROUTER_PREDECISION_TIMEOUT", "4")),
                cwd=str(ROOT),
                env=os.environ.copy(),
                check=False,
            )
            if proc.stderr:
                print(proc.stderr.rstrip(), file=sys.stderr)
            if proc.stdout.strip():
                decision = json.loads(proc.stdout.strip().splitlines()[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    selected = decision.get("tool")
    confidence = decision.get("confidence")
    if isinstance(selected, str) and selected:
        try:
            confidence_text = f"{float(confidence):.3f}"
        except (TypeError, ValueError):
            confidence_text = "unknown"
        print(json.dumps({
            "injectSteps": [{
                "ephemeralMessage": (
                    f"Harness Router precomputed {selected!r} as the preferred next tool "
                    f"(confidence {confidence_text}). Use it when applicable; continue normal reasoning."
                )
            }]
        }, separators=(",", ":")))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
