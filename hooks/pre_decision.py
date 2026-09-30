#!/usr/bin/env python3
"""State-aware pre-decision hook.

The hook is designed to run before every harness decision iteration.  Each
invocation receives the current goal, observation, last action, and live tool
catalog.  The cache is therefore a state -> next-tool cache, never a
prompt -> all-tools prediction.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def root(cwd: str) -> Path:
    p = Path(cwd).resolve()
    for x in (p, *p.parents):
        if (x / ".git").exists():
            return x
    return Path(cwd)


def _normalize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {k: t.get(k) for k in ("name", "description", "category", "risk")}
        for t in tools
        if isinstance(t, dict) and str(t.get("name", "")).strip()
    ]


def _state_key(
    goal: str,
    observation: str,
    last_action: Any,
    tools: list[dict[str, Any]],
) -> str:
    payload = {
        "goal": goal[:1600],
        "observation": observation[:4000],
        "last_action": last_action,
        "tools": _normalize_tools(tools),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cache_path(repo: Path, session: str) -> Path:
    return repo / ".harness-router" / "sessions" / f"{session}.predecision-cache.json"


def _cache_get(path: Path, key: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        item = data.get("entries", {}).get(key)
        if not isinstance(item, dict):
            return None
        ttl = float(os.environ.get("HARNESS_ROUTER_PREDECISION_CACHE_TTL", "300"))
        if time.time() - float(item.get("cached_at", 0)) > ttl:
            return None
        if item.get("fallback") or not isinstance(item.get("tool"), str):
            return None
        return item
    except (OSError, ValueError, TypeError):
        return None


def _cache_put(path: Path, key: str, result: dict[str, Any]) -> None:
    if result.get("fallback") or not isinstance(result.get("tool"), str):
        return
    try:
        max_entries = max(16, int(os.environ.get("HARNESS_ROUTER_PREDECISION_CACHE_SIZE", "512")))
    except ValueError:
        max_entries = 512
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entries = data.get("entries", {}) if isinstance(data, dict) else {}
        if not isinstance(entries, dict):
            entries = {}
        entries[key] = {
            "cached_at": time.time(),
            "tool": result.get("tool"),
            "confidence": result.get("confidence"),
            "routing_mode": result.get("routing_mode", "route"),
            "principal_variation": result.get("principal_variation", []),
        }
        while len(entries) > max_entries:
            oldest = min(entries, key=lambda k: float(entries[k].get("cached_at", 0)))
            entries.pop(oldest, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"entries": entries}, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except (OSError, ValueError, TypeError):
        pass


def route(
    goal: str,
    observation: str,
    last_action: Any,
    tools: list[dict[str, Any]],
    cwd: str,
    timeout: float,
) -> dict[str, Any]:
    normalized = _normalize_tools(tools)
    req = {
        "goal": goal[:1600],
        "observation": observation[:4000],
        "last_action": last_action,
        "tools": normalized,
        "mode": "jev_only",
        "direct_threshold": 0.85,
        "fallback_threshold": 0.60,
        "hierarchical_threshold": 24,
        "route_cache_size": 256,
        "obvious_cache_size": 512,
        "obvious_cache_ttl_seconds": 300,
    }
    wire = (json.dumps(req, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    uid = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
    sock = os.environ.get("HARNESS_ROUTER_SOCKET") or str(
        Path(tempfile.gettempdir()) / f"harness-router-{uid}.sock"
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock)
            s.sendall(wire)
            data = bytearray()
            while len(data) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
        result = json.loads(data.split(b"\n", 1)[0])
        if isinstance(result, dict):
            # Do not accept a stale/broken daemon response as a routing result.
            # A daemon error can otherwise mask the direct OpenRouter path.
            if result.get("daemon_error") or str(result.get("fallback_reason", "")).startswith("daemon_error:"):
                result = {}
            if result:
                return result
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    binary = os.environ.get("HARNESS_ROUTER_BIN") or shutil.which("harness-router")
    module_env = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1] / "src"
    if source_root.is_dir():
        current_pythonpath = module_env.get("PYTHONPATH", "")
        module_env["PYTHONPATH"] = (
            str(source_root)
            if not current_pythonpath
            else str(source_root) + os.pathsep + current_pythonpath
        )
    command = (
        [binary, "route", "--no-daemon"]
        if binary
        else [sys.executable, "-m", "harness_router.cli", "route", "--no-daemon"]
    )
    try:
        p = subprocess.run(
            [
                *command,
                "--mode", "jev_only",
                "--goal", goal[:1600],
                "--observation", observation[:4000],
                "--tools-json", json.dumps(normalized, separators=(",", ":")),
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=module_env,
            check=False,
        )
        if p.returncode == 0:
            result = json.loads(p.stdout.strip())
            return result if isinstance(result, dict) else {}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    goal = str(payload.get("goal") or os.environ.get("HARNESS_ROUTER_GOAL") or "").strip()
    observation = str(
        payload.get("observation")
        or payload.get("tool_result")
        or os.environ.get("HARNESS_ROUTER_OBSERVATION")
        or "No new observation; choose the best next tool."
    ).strip()
    last_action = payload.get("last_action")
    tools = payload.get("tools")

    if not isinstance(tools, list):
        catalog = os.environ.get("HARNESS_ROUTER_TOOLS_FILE")
        if catalog:
            try:
                raw = json.loads(Path(catalog).read_text(encoding="utf-8"))
                tools = raw.get("tools", raw) if isinstance(raw, dict) else raw
            except (OSError, ValueError):
                tools = []

    if not isinstance(tools, list):
        harness = str(
            payload.get("harness") or os.environ.get("HARNESS_ROUTER_HARNESS") or ""
        ).lower()
        if harness:
            candidate = Path(str(payload.get("cwd") or os.getcwd())) / f".{harness}" / "harness-router-tools.json"
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                tools = raw.get("tools", raw) if isinstance(raw, dict) else raw
            except (OSError, ValueError):
                tools = []

    if not goal or not isinstance(tools, list) or len(tools) < 2:
        return 0

    cwd = str(payload.get("cwd") or os.getcwd())
    session = str(payload.get("session_id") or "default")
    state_key = _state_key(goal, observation, last_action, tools)
    repo = root(cwd)
    cache = _cache_path(repo, session)
    started = time.monotonic()

    bypass_cache = os.environ.get("HARNESS_ROUTER_PREDECISION_BYPASS_CACHE") == "1"
    cached = None if bypass_cache else _cache_get(cache, state_key)
    if cached:
        result = dict(cached)
        result["cache_hit"] = True
        result["provider_requests"] = 0
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
    else:
        deadline = started + float(
            os.environ.get("HARNESS_ROUTER_PREDECISION_TIMEOUT", "4")
        )
        result = route(
            goal, observation, last_action, tools, cwd,
            max(0.05, deadline - time.monotonic()),
        )
        result["cache_hit"] = False

        # MCTS remains an optional escalation for an uncertain fast decision.
        try:
            threshold = float(os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD", "0.80"))
        except ValueError:
            threshold = 0.80
        try:
            confidence_value = float(result.get("confidence"))
        except (TypeError, ValueError):
            confidence_value = 0.0

        graph_cmd = os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD", "").strip()
        if (
            isinstance(result.get("tool"), str)
            and result.get("tool")
            and len(tools) >= 3
            and graph_cmd
            and (result.get("fallback") or confidence_value < threshold)
        ):
            graph_input = {
                "goal": goal[:1600],
                "observation": observation[:4000],
                "last_action": last_action,
                "current_tool": result.get("tool"),
                "candidates": tools[:32],
                "route_result": result,
            }
            try:
                p = subprocess.run(
                    shlex.split(graph_cmd),
                    cwd=cwd,
                    input=json.dumps(graph_input, separators=(",", ":")),
                    capture_output=True,
                    text=True,
                    timeout=max(0.05, deadline - time.monotonic()),
                    check=False,
                )
                graph = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else {}
                if (
                    isinstance(graph, dict)
                    and graph.get("root_state")
                    and isinstance(graph.get("states"), list)
                    and isinstance(graph.get("transitions"), list)
                ):
                    graph.setdefault("simulations", max(1, min(4096, int(
                        os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_SIMULATIONS", "64")
                    ))))
                    graph.setdefault("max_depth", max(1, min(8, int(
                        os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_MAX_DEPTH", "3")
                    ))))
                    graph.setdefault("use_jev_prior", True)
                    binary = os.environ.get("HARNESS_ROUTER_BIN") or shutil.which("harness-router")
                    command = [binary, "route-mcts"] if binary else [
                        sys.executable, "-m", "harness_router.cli", "route-mcts"
                    ]
                    p = subprocess.run(
                        command,
                        cwd=cwd,
                        input=json.dumps(graph, separators=(",", ":")),
                        capture_output=True,
                        text=True,
                        timeout=max(0.05, deadline - time.monotonic()),
                        check=False,
                    )
                    if p.returncode == 0:
                        candidate = json.loads(p.stdout.strip())
                        if (
                            isinstance(candidate, dict)
                            and isinstance(candidate.get("tool"), str)
                            and candidate.get("tool")
                            and not candidate.get("fallback")
                        ):
                            result = {**result, **candidate}
                            result["routing_mode"] = "route_mcts"
            except (OSError, ValueError, TypeError, SubprocessError):
                pass

        if isinstance(result, dict):
            _cache_put(cache, state_key, result)

    duration_ms = round((time.monotonic() - started) * 1000, 3)
    selected = result.get("tool")
    confidence = result.get("confidence")
    if not isinstance(selected, str) or not selected:
        return 0
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return 0

    decision_dir = repo / ".harness-router" / "sessions"
    decision_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "harness": payload.get("harness"),
        "session_id": session,
        "decision_id": str(payload.get("decision_id") or payload.get("turn_id") or state_key[:16]),
        "state_key": state_key,
        "goal": goal,
        "observation": observation,
        "last_action": last_action,
        "tool": selected,
        "confidence": confidence,
        "routing_mode": result.get("routing_mode", "cache" if result.get("cache_hit") else "route"),
        "cache_hit": bool(result.get("cache_hit")),
        "duration_ms": duration_ms,
        "principal_variation": result.get("principal_variation", []),
    }
    (decision_dir / f"{session}.decision.json").write_text(
        json.dumps(record, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    with (decision_dir / f"{session}.decisions.jsonl").open("a", encoding="utf-8") as events:
        events.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")

    print(json.dumps({
        "tool": selected,
        "confidence": confidence,
        "routing_mode": record["routing_mode"],
        "cache_hit": record["cache_hit"],
        "duration_ms": duration_ms,
        "state_key": state_key,
    }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
