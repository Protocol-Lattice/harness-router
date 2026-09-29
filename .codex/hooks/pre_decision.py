#!/usr/bin/env python3
"""Precompute the next tool choice before Codex asks the model to use a tool.

This is the router-first bridge. UserPromptSubmit is the earliest local hook in
the Codex tool loop that can see the user goal. It asks the long-lived router
daemon for a cheap next-tool prediction, stores it for PreToolUse, and exposes
the prediction as compact developer context.

It does not replace Codex's model decision: Codex still owns planning and may
ignore the recommendation. The important optimization is that a later
PreToolUse hit can be served from this precomputed decision without another Jev
request.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time
from typing import Any


MAX_PROMPT_CHARS = 1600
MAX_CANDIDATES = 8
DEFAULT_TIMEOUT = 4.0


def _repo_root(cwd: str) -> Path:
    current = Path(cwd)
    try:
        current = current.resolve()
        for parent in (current, *current.parents):
            if (parent / ".git").exists():
                return parent
    except OSError:
        pass
    return Path(cwd)


def _load_tools(root: Path, session_id: str) -> list[dict[str, Any]]:
    paths = (
        root / ".codex" / "harness-router" / "sessions" / f"{session_id}.json",
        root / ".codex" / "harness-router-tools.json",
    )
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            tools = raw.get("tools", raw) if isinstance(raw, dict) else raw
            if isinstance(tools, list):
                return [
                    item for item in tools
                    if isinstance(item, dict) and str(item.get("name", "")).strip()
                ]
        except (OSError, json.JSONDecodeError):
            pass
    return []


def _tokens(value: str) -> set[str]:
    return {
        token.lower()
        for token in re.split(r"[^a-zA-Z0-9]+", value)
        if len(token) >= 2
    }


def _shortlist(tools: list[dict[str, Any]], goal: str) -> list[dict[str, Any]]:
    goal_tokens = _tokens(goal)
    scored: list[tuple[float, dict[str, Any]]] = []

    for tool in tools:
        name = str(tool.get("name", ""))
        if "harness-router" in name.lower():
            continue

        name_tokens = _tokens(name)
        desc_tokens = _tokens(str(tool.get("description", "")))
        score = (
            len(goal_tokens & name_tokens) * 4.0
            + len(goal_tokens & desc_tokens) * 1.5
        )

        category = str(tool.get("category") or "").lower()
        if category == "inspect" and any(
            word in goal_tokens for word in {"read", "inspect", "find", "search", "look"}
        ):
            score += 1.0

        scored.append((score, tool))

    scored.sort(key=lambda item: item[0], reverse=True)

    # Keep a small, representative candidate set. When lexical matching is
    # weak, retain the first tools from the live catalog rather than inventing
    # a tool description.
    selected = [tool for score, tool in scored if score > 0][:MAX_CANDIDATES]
    if len(selected) < 2:
        selected = [tool for _, tool in scored[:MAX_CANDIDATES]]
    return selected


def _route(
    goal: str,
    candidates: list[dict[str, Any]],
    timeout: float,
) -> dict[str, Any] | None:
    if os.name == "nt" or os.environ.get("HARNESS_ROUTER_NO_DAEMON") == "1":
        return None

    configured = os.environ.get("HARNESS_ROUTER_SOCKET")
    if configured:
        socket_path = configured
    else:
        uid = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
        socket_path = str(Path(tempfile.gettempdir()) / f"harness-router-{uid}.sock")

    request = {
        "goal": goal,
        "observation": "Pre-tool-selection: choose the best next tool for the user task.",
        "last_action": None,
        "tools": [
            {key: tool.get(key) for key in ("name", "description", "category", "risk")}
            for tool in candidates
        ],
        "mode": "jev_only",
        "direct_threshold": 0.85,
        "fallback_threshold": 0.60,
        "hierarchical_threshold": 24,
        "route_cache_size": int(os.environ.get("HARNESS_ROUTER_PRETOOL_ROUTE_CACHE_SIZE", "256")),
        "obvious_cache_size": int(os.environ.get("HARNESS_ROUTER_PRETOOL_OBVIOUS_CACHE_SIZE", "512")),
        "obvious_cache_ttl_seconds": float(os.environ.get("HARNESS_ROUTER_PRETOOL_OBVIOUS_CACHE_TTL", "300")),
    }

    wire = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall(wire)
            response = bytearray()
            while len(response) <= 64 * 1024:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
                if b"\n" in chunk:
                    break
        result = json.loads(response.split(b"\n", 1)[0])
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    return result if isinstance(result, dict) and result.get("daemon") else None


def _write_decision(
    root: Path,
    session_id: str,
    goal: str,
    result: dict[str, Any],
    duration_ms: float,
) -> None:
    path = root / ".codex" / "harness-router" / "sessions" / f"{session_id}.decision.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "goal": goal,
                "tool": result.get("tool"),
                "confidence": result.get("confidence"),
                "fallback": bool(result.get("fallback")),
                "provider_requests": result.get("provider_requests", 0),
                "duration_ms": duration_ms,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def main() -> int:
    started = time.monotonic()

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        payload = {}

    if payload.get("hook_event_name") != "UserPromptSubmit":
        return 0

    prompt = str(payload.get("prompt") or "").strip()[:MAX_PROMPT_CHARS]
    if not prompt:
        return 0

    cwd = str(payload.get("cwd") or os.getcwd())
    root = _repo_root(cwd)
    session_id = str(payload.get("session_id") or "default")

    tools = _load_tools(root, session_id)
    candidates = _shortlist(tools, prompt)
    if len(candidates) < 2:
        return 0

    timeout = float(os.environ.get("HARNESS_ROUTER_PREDECISION_TIMEOUT", DEFAULT_TIMEOUT))
    result = _route(prompt, candidates, timeout)
    if not result:
        return 0

    duration_ms = round((time.monotonic() - started) * 1000, 3)
    selected = result.get("tool")
    confidence = result.get("confidence")

    _write_decision(root, session_id, prompt, result, duration_ms)

    if not isinstance(selected, str) or not selected or result.get("fallback"):
        return 0

    confidence_text = (
        f"{float(confidence):.3f}"
        if isinstance(confidence, (int, float))
        else "unknown"
    )

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": (
                        f"Harness Router precomputed the next-tool recommendation: "
                        f"{selected!r} (confidence {confidence_text}). "
                        "Use this as the default next tool when it matches the task; "
                        "keep normal planning and argument generation."
                    ),
                }
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    print(
        json.dumps(
            {
                "event": "harness_router.predecision",
                "hook": "codex",
                "session_id": session_id,
                "tool": selected,
                "confidence": confidence,
                "provider_requests": result.get("provider_requests", 0),
                "duration_ms": duration_ms,
            },
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
