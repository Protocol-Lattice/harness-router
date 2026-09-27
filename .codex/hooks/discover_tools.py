#!/usr/bin/env python3
"""Build a per-Codex-session tool catalog for harness-router.

The hook accepts tool definitions from any of these sources, in priority order:
1. tool definitions present in the SessionStart payload (future/runtime compatible),
2. HARNESS_ROUTER_CODEX_TOOLS_JSON,
3. HARNESS_ROUTER_CODEX_DISCOVER_CMD stdout,
4. .codex/harness-router-tools.json as the repository fallback.

The resulting registry is stored per session and consumed by PreToolUse.
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

    out: list[dict[str, Any]] = []
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


def _merge(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for group in groups:
        for tool in group:
            name = tool["name"]
            if name not in merged:
                order.append(name)
                merged[name] = dict(tool)
            else:
                for key, value in tool.items():
                    if value not in (None, "", [], {}):
                        merged[name][key] = value
    return [merged[name] for name in order]


def _load_json_file(path: Path) -> list[dict[str, Any]]:
    try:
        return _normalize(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return []


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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}

    cwd = str(payload.get("cwd") or os.getcwd())
    root = _repo_root(cwd)
    session_id = str(payload.get("session_id") or "default")

    payload_tools = _normalize(
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

    command_tools = _discover_command()
    fallback_tools = _load_json_file(root / ".codex" / "harness-router-tools.json")
    tools = _merge(fallback_tools, command_tools, env_tools, payload_tools)

    registry_dir = root / ".codex" / "harness-router" / "sessions"
    registry_dir.mkdir(parents=True, exist_ok=True)
    registry = registry_dir / f"{session_id}.json"
    registry.write_text(
        json.dumps({"session_id": session_id, "tools": tools}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(json.dumps({"tool_count": len(tools), "registry": str(registry)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
