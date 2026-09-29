#!/usr/bin/env python3
"""Codex PreToolUse bridge for harness-router.

Loads the generated tool catalog produced by discover_tools.py, locally
shortlists tools similar to the pending call, and sends only that compact
candidate set to the native harness-router MCP server. Failures are deliberately fail-open.
"""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
import subprocess
import sys
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
        (tool for tool in tools if tool.get("name") == current),
        None,
    )
    if current_tool is None:
        return []

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
        if name == current:
            continue
        scored.append((_tool_similarity(tool, current_tool, goal), tool))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [current_tool] + [
        tool for _, tool in scored[: max_candidates - 1]
    ]


def _mcp_endpoint() -> str:
    return os.environ.get(
        "HARNESS_ROUTER_MCP_URL",
        "http://127.0.0.1:8765/mcp",
    )


def _mcp_send(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("MCP stdin unavailable")
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def _mcp_read_response(
    proc: subprocess.Popen[str],
    request_id: int,
    deadline: float,
) -> dict[str, Any]:
    import select
    import time

    if proc.stdout is None:
        raise RuntimeError("MCP stdout unavailable")

    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            break

        line = proc.stdout.readline()
        if not line:
            break

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        if message.get("id") != request_id:
            continue
        if "error" in message:
            raise RuntimeError(str(message["error"]))

        result = message.get("result")
        return result if isinstance(result, dict) else {}

    raise TimeoutError("timed out waiting for harness-router-mcp")


def _extract_mcp_tool_result(response: dict[str, Any]) -> dict[str, Any]:
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
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return {}


def _build_mcts_graph(
    *,
    cwd: str,
    goal: str,
    observation: str,
    current: str,
    candidates: list[dict[str, Any]],
    route_result: dict[str, Any],
    timeout: float,
) -> dict[str, Any] | None:
    command = os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD", "").strip()
    if not command:
        return None

    import shlex

    payload = {
        "goal": goal,
        "observation": observation,
        "current_tool": current,
        "candidates": candidates,
        "route_result": route_result,
    }

    try:
        proc = subprocess.run(
            shlex.split(command),
            cwd=cwd,
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    if proc.returncode != 0:
        return None

    try:
        graph = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    if not isinstance(graph, dict):
        return None
    if not isinstance(graph.get("root_state"), str):
        return None
    if not isinstance(graph.get("states"), list):
        return None
    if not isinstance(graph.get("transitions"), list):
        return None
    return graph


def _should_try_mcts(
    result: dict[str, Any],
    candidate_count: int,
) -> bool:
    if candidate_count < 3:
        return False

    try:
        threshold = float(
            os.environ.get(
                "HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD",
                DEFAULT_MCTS_THRESHOLD,
            )
        )
    except ValueError:
        threshold = DEFAULT_MCTS_THRESHOLD

    confidence = result.get("confidence")
    confidence_value = (
        float(confidence)
        if isinstance(confidence, (int, float))
        else 0.0
    )

    return bool(result.get("fallback")) or confidence_value < threshold


def _mcp_route_hybrid(
    *,
    endpoint: str,
    cwd: str,
    goal: str,
    observation: str,
    current: str,
    candidates: list[dict[str, Any]],
    timeout: float,
) -> tuple[dict[str, Any], str]:
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    next_id = 0

    def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal next_id
        next_id += 1
        body = json.dumps({
            "jsonrpc": "2.0",
            "id": next_id,
            "method": method,
            "params": params,
        }).encode("utf-8")
        req = urllib.request.Request(
            endpoint.rstrip("/"),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "MCP-Protocol-Version": "2025-06-18",
            },
            method="POST",
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Harness Router timed out")
        try:
            with urllib.request.urlopen(req, timeout=remaining) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise RuntimeError("Harness Router HTTP endpoint unavailable") from exc
        if not raw:
            return {}
        message = json.loads(raw.decode("utf-8"))
        if not isinstance(message, dict):
            return {}
        if "error" in message:
            raise RuntimeError(str(message["error"]))
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = request("tools/call", {"name": name, "arguments": arguments})
        return _extract_mcp_tool_result(response)

    request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "harness-router-codex-hook", "version": "1"},
        },
    )

    route_tools = [
        {
            "name": tool["name"],
            "description": str(tool.get("description", "")),
            "category": tool.get("category"),
            "risk": str(tool.get("risk", "medium")),
        }
        for tool in candidates
    ]
    route_result = call(
        "route",
        {
            "goal": goal,
            "observation": observation,
            "tools": route_tools,
        },
    )

    if not _should_try_mcts(route_result, len(candidates)):
        return route_result, "route"

    remaining = max(0.1, deadline - time.monotonic())
    graph = _build_mcts_graph(
        cwd=cwd,
        goal=goal,
        observation=observation,
        current=current,
        candidates=candidates,
        route_result=route_result,
        timeout=remaining,
    )
    if graph is None or deadline <= time.monotonic():
        return route_result, "route"

    mcts_result = call(
        "route_mcts",
        {
            "root_state": graph["root_state"],
            "states": graph["states"],
            "transitions": graph["transitions"],
            "simulations": int(graph.get("simulations", DEFAULT_MCTS_SIMULATIONS)),
            "max_depth": int(graph.get("max_depth", DEFAULT_MCTS_MAX_DEPTH)),
            "use_jev_prior": bool(graph.get("use_jev_prior", True)),
        },
    )
    return (mcts_result, "route_mcts") if mcts_result else (route_result, "route")
def main() -> int:
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
    if not any(tool.get("name") == current for tool in tools):
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

    endpoint = _mcp_endpoint()
    if not endpoint:
        _allow()
        return 0

    timeout = float(
        os.environ.get(
            "HARNESS_ROUTER_PRETOOL_TIMEOUT",
            DEFAULT_TIMEOUT_SECONDS,
        )
    )

    try:
        result, routing_mode = _mcp_route_hybrid(
            endpoint=endpoint,
            cwd=cwd,
            goal=goal,
            observation=observation,
            current=current,
            candidates=candidates,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError, RuntimeError, ValueError):
        _allow()
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())
