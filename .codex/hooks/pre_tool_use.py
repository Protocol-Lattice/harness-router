#!/usr/bin/env python3
"""Codex PreToolUse bridge for harness-router.

Loads the generated tool catalog produced by discover_tools.py, locally
shortlists tools similar to the pending call, and sends only that compact
candidate set to the native harness-router MCP server. Failures are deliberately fail-open.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any


MAX_GOAL_CHARS = 1600
MAX_OBSERVATION_CHARS = 900
MAX_TRANSCRIPT_BYTES = 256_000
DEFAULT_TIMEOUT_SECONDS = 4.0
DEFAULT_MAX_CANDIDATES = 8
DEFAULT_MCTS_THRESHOLD = 0.80
DEFAULT_MCTS_SIMULATIONS = 64
DEFAULT_MCTS_MAX_DEPTH = 3


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


def _allow() -> None:
    _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse"}})


def _deny(
    *,
    selected: str,
    current: str,
    confidence: float | None,
    tool_count: int,
    routing_mode: str = "route",
) -> None:
    confidence_text = f" at confidence {confidence:.3f}" if confidence is not None else ""
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"harness-router selected {selected!r}{confidence_text} instead of "
                    f"the pending Codex tool {current!r}"
                ),
                "additionalContext": (
                    f"Harness Router ({routing_mode}) evaluated {tool_count} similar tools "
                    f"shortlisted from the generated session catalog and recommends {selected!r} "
                    f"instead of {current!r}. Re-plan once and preserve the original user goal."
                ),
            }
        }
    )


def _repo_root(cwd: str) -> Path:
    current = Path(cwd)
    try:
        current = current.resolve()
        for parent in (current, *current.parents):
            # Git worktrees use a .git file, so exists() covers both layouts.
            if (parent / ".git").exists():
                return parent
    except OSError:
        pass
    return Path(cwd)


def _append_hook_timing(event: dict[str, Any]) -> None:
    try:
        root = _repo_root(os.getcwd())
        log_path = root / "hook-timings.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        # Timing telemetry must never affect the pending tool call.
        pass


def _normalize(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("tools", [])
    if not isinstance(raw, list):
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
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


def _load_tools(root: Path, session_id: str) -> tuple[list[dict[str, Any]], Path]:
    session = root / ".codex" / "harness-router" / "sessions" / f"{session_id}.json"
    generated = root / ".codex" / "harness-router-tools.json"

    for path in (session, generated):
        try:
            tools = _normalize(json.loads(path.read_text(encoding="utf-8")))
            if tools:
                return tools, session
        except (OSError, json.JSONDecodeError):
            pass
    return [], session


def _infer_current_tool(tool_name: str) -> dict[str, Any]:
    lowered = tool_name.lower()
    if tool_name == "Bash" or "exec" in lowered or "shell" in lowered:
        category, risk = "execute", "medium"
    elif "patch" in lowered or "write" in lowered or "edit" in lowered:
        category, risk = "mutate", "medium"
    elif "read" in lowered or "search" in lowered or "list" in lowered or "fetch" in lowered:
        category, risk = "inspect", "low"
    else:
        category, risk = "general", "medium"

    return {
        "name": tool_name,
        "description": f"Codex runtime tool observed by PreToolUse: {tool_name}",
        "category": category,
        "risk": risk,
        "input_schema": None,
    }


def _persist_session(path: Path, session_id: str, tools: list[dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"generated": True, "session_id": session_id, "tools": tools},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(filter(None, (_extract_text(x) for x in value))).strip()
    if isinstance(value, dict):
        for key in ("text", "input_text", "content"):
            if key in value:
                text = _extract_text(value[key])
                if text:
                    return text
    return ""


def _find_user_text(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("role") == "user":
            text = _extract_text(value.get("content"))
            if text:
                return text
        for child in value.values():
            text = _find_user_text(child)
            if text:
                return text
    elif isinstance(value, list):
        for child in reversed(value):
            text = _find_user_text(child)
            if text:
                return text
    return ""


def _goal_from_transcript(path_value: Any) -> str:
    if not isinstance(path_value, str) or not path_value:
        return ""

    try:
        path = Path(path_value)
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - MAX_TRANSCRIPT_BYTES))
            chunk = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""

    for line in reversed(chunk.splitlines()):
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _find_user_text(item)
        if text:
            return text[:MAX_GOAL_CHARS]
    return ""


def _compact_tool_input(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = repr(value)
    return text[:600]


def _tool_key(value: str) -> str:
    """Compare MCP tool names independent of dash/underscore spelling."""
    return re.sub(r"[-_]+", "_", value.strip().lower())


def _align_tool_name(name: str, runtime_name: str) -> str:
    """Use the naming convention exposed by the current runtime tool."""
    if "-" in runtime_name and "_" not in runtime_name:
        return name.replace("_", "-")
    if "_" in runtime_name and "-" not in runtime_name:
        return name.replace("-", "_")
    return name


def _tokens(value: str) -> set[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\\1 \\2", value)
    return {
        token.lower()
        for token in re.split(r"[^a-zA-Z0-9]+", value)
        if len(token) >= 2
    }


def _tool_similarity(
    candidate: dict[str, Any],
    current_tool: dict[str, Any],
    goal: str,
) -> float:
    candidate_name = str(candidate.get("name", ""))
    current_name = str(current_tool.get("name", ""))

    candidate_name_tokens = _tokens(candidate_name)
    current_name_tokens = _tokens(current_name)
    candidate_desc_tokens = _tokens(str(candidate.get("description", "")))
    current_desc_tokens = _tokens(str(current_tool.get("description", "")))
    goal_tokens = _tokens(goal)

    score = SequenceMatcher(
        None,
        current_name.lower(),
        candidate_name.lower(),
    ).ratio() * 10.0
    score += len(candidate_name_tokens & current_name_tokens) * 4.0
    score += len(candidate_desc_tokens & current_desc_tokens) * 1.5
    score += len(
        goal_tokens & (candidate_name_tokens | candidate_desc_tokens)
    ) * 0.75

    if (
        candidate.get("category")
        and candidate.get("category") == current_tool.get("category")
    ):
        score += 2.0

    return score


def _similar_tools(
    tools: list[dict[str, Any]],
    current: str,
    goal: str,
) -> list[dict[str, Any]]:
    current_tool = next(
        (tool for tool in tools if _tool_key(str(tool.get("name", ""))) == _tool_key(current)),
        None,
    )
    if current_tool is None:
        return []
    current_tool = dict(current_tool)
    current_tool["name"] = current

    try:
        max_candidates = max(
            2,
            int(
                os.environ.get(
                    "HARNESS_ROUTER_PRETOOL_MAX_CANDIDATES",
                    DEFAULT_MAX_CANDIDATES,
                )
            ),
        )
    except ValueError:
        max_candidates = DEFAULT_MAX_CANDIDATES

    scored: list[tuple[float, dict[str, Any]]] = []
    for tool in tools:
        name = str(tool.get("name", ""))
        lowered = name.lower()
        if "harness-router" in lowered and (
            "route" in lowered or "mcts" in lowered
        ):
            continue
        if _tool_key(name) == _tool_key(current):
            continue
        aligned = dict(tool)
        aligned["name"] = _align_tool_name(name, current)
        scored.append((_tool_similarity(aligned, current_tool, goal), aligned))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [current_tool] + [
        tool for _, tool in scored[: max_candidates - 1]
    ]


def _router_binary() -> str | None:
    return os.environ.get("HARNESS_ROUTER_BIN") or shutil.which("harness-router")


def _route_via_daemon(
    goal: str,
    observation: str,
    candidates: list[dict[str, Any]],
    timeout: float,
) -> dict[str, Any] | None:
    """Use the already-running daemon without starting a second Python process."""
    if os.name == "nt" or os.environ.get("HARNESS_ROUTER_NO_DAEMON") == "1":
        return None

    configured = os.environ.get("HARNESS_ROUTER_SOCKET")
    if configured:
        path = configured
    else:
        uid = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
        path = str(Path(tempfile.gettempdir()) / f"harness-router-{uid}.sock")

    request = {
        "goal": goal,
        "observation": observation,
        "last_action": None,
        "tools": [
            {key: tool.get(key) for key in ("name", "description", "category", "risk")}
            for tool in candidates
        ],
        "mode": "jev_only",
        "direct_threshold": 0.85,
        "fallback_threshold": 0.60,
        "hierarchical_threshold": 24,
        "route_cache_size": 0,
    }
    wire = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(path)
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

    if not isinstance(result, dict) or result.get("provider_requests", 0) < 1:
        return None
    return result


def _route_hybrid(
    binary: str | None,
    cwd: str | Path,
    goal: str,
    observation: str,
    current: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    timeout = float(os.environ.get("HARNESS_ROUTER_PRETOOL_TIMEOUT", "4.0"))
    if timeout <= 0:
        return {}, "route"
    daemon_result = _route_via_daemon(goal, observation, candidates, timeout)
    if daemon_result is not None:
        return daemon_result, "route"
    if not binary:
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


def _run_hook(hook_stats: dict[str, Any] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        _allow()
        return 0

    if payload.get("hook_event_name") != "PreToolUse":
        _allow()
        return 0

    current = str(payload.get("tool_name", "")).strip()
    if not current:
        _allow()
        return 0

    lowered = current.lower()
    if "harness-router" in lowered and ("route" in lowered or "mcts" in lowered):
        _allow()
        return 0

    cwd = str(payload.get("cwd") or os.getcwd())
    root = _repo_root(cwd)
    session_id = str(payload.get("session_id") or "default")
    tools, session_path = _load_tools(root, session_id)

    # The catalog is immutable for the session. Discovery happens once at
    # SessionStart; PreToolUse must not learn tools or invent descriptions.
    if not any(_tool_key(str(tool.get("name", ""))) == _tool_key(current) for tool in tools):
        _allow()
        return 0

    if len(tools) < 2:
        _allow()
        return 0

    goal = (
        _goal_from_transcript(payload.get("transcript_path"))
        or "Choose the best next Codex tool for the current task."
    )
    candidates = _similar_tools(tools, current=current, goal=goal)
    if len(candidates) < 2:
        _allow()
        return 0

    observation = (
        f"Codex is about to call {current!r}. "
        f"tool_input={_compact_tool_input(payload.get('tool_input'))}"
    )[:MAX_OBSERVATION_CHARS]

    binary = _router_binary()
    decision_started_at = datetime.now(timezone.utc)
    decision_started = time.monotonic()
    decision_outcome = "error"
    routing_mode: str | None = None
    selected: str | None = None
    try:
        result, routing_mode = _route_hybrid(
            binary=binary,
            cwd=cwd,
            goal=goal,
            observation=observation,
            current=current,
            candidates=candidates,
        )
        selected_value = result.get("tool")
        selected = selected_value if isinstance(selected_value, str) else None
        decision_outcome = (
            "recommendation"
            if not result.get("fallback") and selected and selected != current
            else "fallback"
        )
    except (OSError, subprocess.SubprocessError, TimeoutError, RuntimeError, ValueError):
        _allow()
        return 0
    finally:
        if hook_stats is not None:
            hook_stats.update(
                {
                    "decision_started_at": decision_started_at.isoformat(
                        timespec="milliseconds"
                    ).replace("+00:00", "Z"),
                    "decision_duration_ms": round(
                        (time.monotonic() - decision_started) * 1000, 3
                    ),
                    "decision_outcome": decision_outcome,
                    "routing_mode": routing_mode,
                    "selected_tool": selected,
                }
            )

    selected = result.get("tool")
    if (
        result.get("fallback")
        or not isinstance(selected, str)
        or not selected
        or selected == current
    ):
        _allow()
        return 0

    confidence_value = result.get("confidence")
    confidence = (
        float(confidence_value)
        if isinstance(confidence_value, (int, float))
        else None
    )
    _deny(
        selected=selected,
        current=current,
        confidence=confidence,
        tool_count=len(candidates),
        routing_mode=routing_mode,
    )
    return 0


def main() -> int:
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    hook_stats: dict[str, Any] = {
        "decision_started_at": None,
        "decision_duration_ms": None,
        "decision_outcome": "not_invoked",
    }
    try:
        return _run_hook(hook_stats)
    finally:
        event = {
            "event": "harness_router.hook_timing",
            "hook": "codex",
            "started_at": started_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            **hook_stats,
        }
        print(json.dumps(event, separators=(",", ":")), file=sys.stderr)
        _append_hook_timing(event)


if __name__ == "__main__":
    raise SystemExit(main())
