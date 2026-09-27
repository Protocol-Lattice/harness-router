#!/usr/bin/env python3
"""Generate the Codex tool catalog consumed by harness-router.

Runs on SessionStart(source=startup), writes the generated catalog and a
per-session copy, and emits only valid SessionStart hook JSON on stdout.
Diagnostics go to stderr so Codex never mistakes them for hook output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any


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
        raw = raw.get("tools", raw.get("available_tools", raw.get("tool_definitions", [])))
    if not isinstance(raw, list):
        return []

    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"name": item}
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
                "input_schema": item.get("input_schema") or item.get("parameters"),
            }
        )
    return tools


def _merge(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for group in groups:
        for tool in group:
            name = tool["name"]
            if name not in merged:
                order.append(name)
                merged[name] = dict(tool)
                continue
            for key, value in tool.items():
                if value not in (None, "", [], {}):
                    merged[name][key] = value
    return [merged[name] for name in order]


def _discover_command() -> list[dict[str, Any]]:
    command = os.environ.get("HARNESS_ROUTER_CODEX_DISCOVER_CMD", "").strip()
    if not command:
        return []

    try:
        p = subprocess.run(
            shlex.split(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
            env=os.environ.copy(),
        )
        if p.returncode == 0:
            return _normalize(json.loads(p.stdout))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        pass
    return []


def _write_catalog(path: Path, *, session_id: str, tools: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated": True,
                "session_id": session_id,
                "tools": tools,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _emit_session_start(tool_count: int) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        f"Harness Router tool catalog ready with {tool_count} tools."
                    ),
                }
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}

    cwd = str(payload.get("cwd") or os.getcwd())
    root = _repo_root(cwd)
    session_id = str(payload.get("session_id") or "default")

    runtime_tools = _normalize(
        payload.get("tools")
        or payload.get("available_tools")
        or payload.get("tool_definitions")
        or []
    )

    env_tools: list[dict[str, Any]] = []
    raw_env = os.environ.get("HARNESS_ROUTER_CODEX_TOOLS_JSON", "").strip()
    if raw_env:
        try:
            env_tools = _normalize(json.loads(raw_env))
        except json.JSONDecodeError:
            pass

    discovered_tools = _discover_command()
    tools = _merge(discovered_tools, env_tools, runtime_tools)

    generated_catalog = root / ".codex" / "harness-router-tools.json"
    session_catalog = (
        root / ".codex" / "harness-router" / "sessions" / f"{session_id}.json"
    )

    _write_catalog(generated_catalog, session_id=session_id, tools=tools)
    _write_catalog(session_catalog, session_id=session_id, tools=tools)

    print(
        f"Harness Router discovered {len(tools)} tools for session {session_id}",
        file=sys.stderr,
    )
    _emit_session_start(len(tools))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
