#!/usr/bin/env python3
"""DeepSeek Harness command hook for Harness Router.

DeepSeek Harness exposes tools/pre-execute natively. The supported
@deepseek-ai/dsh-hooks-codex bridge maps Codex PreToolUse command hooks onto
that interception point, so this adapter keeps the existing routing protocol.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 4.0
MAX_CANDIDATES = 8


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def allow() -> None:
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse"}})


def deny(selected: str, current: str, confidence: float | None, count: int) -> None:
    confidence_text = f" at confidence {confidence:.3f}" if confidence is not None else ""
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"harness-router selected {selected!r}{confidence_text} instead of "
                f"the pending DeepSeek tool {current!r}"
            ),
            "additionalContext": (
                f"Harness Router evaluated {count} similar tools and recommends "
                f"{selected!r} instead of {current!r}. Re-plan once and preserve "
                "the original user goal."
            ),
        }
    })


def load_tools(cwd: Path) -> list[dict[str, Any]]:
    raw_json = os.environ.get("HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON", "").strip()
    raw_file = os.environ.get("HARNESS_ROUTER_DEEPSEEK_TOOLS_FILE", "").strip()
    values: Any
    if raw_json:
        try:
            values = json.loads(raw_json)
        except json.JSONDecodeError:
            return []
    elif raw_file:
        try:
            values = json.loads(Path(raw_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
    else:
        try:
            values = json.loads(
                (cwd / ".dsh" / "harness-router-tools.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return []

    if isinstance(values, dict):
        values = values.get("tools", [])
    if not isinstance(values, list):
        return []

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({
            "name": name,
            "description": str(item.get("description", "")).strip(),
            "category": item.get("category"),
            "risk": str(item.get("risk", "medium")).lower(),
        })
    return result


def tokens(value: str) -> set[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return {
        token.lower()
        for token in re.split(r"[^a-zA-Z0-9]+", value)
        if len(token) >= 2
    }


def similarity(candidate: dict[str, Any], current: dict[str, Any]) -> float:
    candidate_name = str(candidate["name"])
    current_name = str(current["name"])
    score = SequenceMatcher(None, current_name.lower(), candidate_name.lower()).ratio() * 10
    score += len(tokens(candidate_name) & tokens(current_name)) * 4
    score += len(
        tokens(str(candidate.get("description", "")))
        & tokens(str(current.get("description", "")))
    ) * 1.5
    if candidate.get("category") and candidate.get("category") == current.get("category"):
        score += 2
    return score


def shortlist(tools: list[dict[str, Any]], current: str) -> list[dict[str, Any]]:
    current_tool = next((tool for tool in tools if tool["name"] == current), None)
    if current_tool is None:
        return []
    others = [
        tool for tool in tools
        if tool["name"] != current and "harness-router" not in tool["name"].lower()
    ]
    others.sort(key=lambda tool: similarity(tool, current_tool), reverse=True)
    return [current_tool, *others[: MAX_CANDIDATES - 1]]


def extract_result(response: dict[str, Any]) -> dict[str, Any]:
    structured = response.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in response.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        allow()
        return 0

    if payload.get("hook_event_name") != "PreToolUse":
        allow()
        return 0

    current = str(payload.get("tool_name", "")).strip()
    if not current or "harness-router" in current.lower():
        allow()
        return 0

    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    tools = load_tools(cwd)
    if len(tools) < 2 or not any(tool["name"] == current for tool in tools):
        allow()
        return 0

    candidates = shortlist(tools, current)
    if len(candidates) < 2:
        allow()
        return 0

    binary = os.environ.get("HARNESS_ROUTER_BIN") or shutil.which("harness-router")
    if not binary:
        allow()
        return 0

    goal = str(payload.get("goal") or "Choose the best next DeepSeek tool for the current task.")[:1600]
    observation = (
        f"DeepSeek is about to call {current!r}. "
        f"tool_input={json.dumps(payload.get('tool_input'), ensure_ascii=False)[:600]}"
    )[:900]
    import time
    started = time.monotonic()
    try:
        result, mode = route(
            binary=binary,
            cwd=cwd,
            goal=goal,
            observation=observation,
            current=current,
            candidates=candidates,
        )
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError):
        allow()
        return 0

    elapsed_ms = (time.monotonic() - started) * 1000
    selected = result.get("tool")
    if result.get("fallback") or not isinstance(selected, str) or not selected or selected == current:
        allow()
        return 0

    raw_confidence = result.get("confidence")
    confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else None
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"harness-router ({mode}) selected {selected!r} instead of {current!r}"
            ),
            "additionalContext": (
                f"Harness Router evaluated {len(candidates)} similar tools; "
                f"decision latency: {elapsed_ms:.2f} ms."
            ),
        }
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
