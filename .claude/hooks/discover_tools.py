#!/usr/bin/env python3
"""Expose Claude Code's live tool registry at session startup, like the Codex hook.

Query Claude's native server and SDK control interface, then publish the combined
registry for PreToolUse and session commands. --list-tools queries the same live
registry directly as JSON. Discovery needs no hard-coded tools or model turn.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def project_root() -> Path:
    # A tool may change cwd. The hook installation, not the current shell, owns state.
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def session_catalog(root: Path, session_id: str) -> Path:
    key = (
        session_id
        if re.fullmatch(r"[\w-]{1,128}", session_id)
        else hashlib.sha256(session_id.encode()).hexdigest()
    )
    return root / ".claude" / "harness-router" / "sessions" / f"{key}.json"


def normalize_tools(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("tools", [])
    if not isinstance(raw, list):
        return []

    tools: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        tool = dict(item)
        tool["name"] = name.strip()
        annotations = tool.get("annotations") or {}
        if not isinstance(annotations, dict):
            annotations = {}
        category, risk = "general", "medium"
        if annotations.get("destructiveHint", annotations.get("destructive")) is True:
            category, risk = "mutate", "high"
        elif annotations.get("readOnlyHint", annotations.get("readOnly")) is True:
            category, risk = "inspect", "low"
        tool.setdefault("description", "")
        tool.setdefault("category", category)
        tool.setdefault("risk", risk)
        tool.setdefault("input_schema", tool.get("inputSchema"))
        tool.setdefault("output_schema", tool.get("outputSchema"))
        tool.setdefault("source", "runtime")
        tools[tool["name"]] = tool
    return list(tools.values())


def tools_from_status(status: dict[str, Any]) -> list[dict[str, Any]]:
    tools = []
    servers = status.get("mcpServers", [])
    if not isinstance(servers, list):
        return tools
    for server in servers:
        if not isinstance(server, dict) or server.get("status") != "connected":
            continue
        name = server.get("name")
        if not isinstance(name, str) or not name:
            continue
        # Claude's tool namespace replaces punctuation in server names with underscores.
        namespace = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
        for definition in normalize_tools(server.get("tools", [])):
            raw_name = definition["name"]
            definition["name"] = (
                raw_name if raw_name.startswith("mcp__") else f"mcp__{namespace}__{raw_name}"
            )
            definition["source"] = "claude-mcp"
            definition["server"] = name
            tools.append(definition)
    return tools


class ClaudeMetadataClient:
    """Read-only requests over Claude's native MCP or SDK control transport."""

    def __init__(self, binary: str, root: Path, deadline: float, *, native: bool) -> None:
        self.native = native
        self.deadline = deadline
        self.counter = 0
        env = os.environ.copy()
        env["HARNESS_ROUTER_CLAUDE_DISCOVERY_ACTIVE"] = "1"
        env.pop("CLAUDECODE", None)
        command = [
            binary,
            "--setting-sources",
            "user,project,local",
            "--settings",
            '{"disableAllHooks":true}',
        ]
        command += (
            ["mcp", "serve"]
            if native
            else [
                "--print",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--no-session-persistence",
            ]
        )
        self.proc = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.messages: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self.messages.put(line)
        except (OSError, UnicodeError):
            pass
        finally:
            self.messages.put(None)

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        request_id = str(self.counter)
        if self.native:
            self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        else:
            self.send(
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": method, **params},
                }
            )
        while time.monotonic() < self.deadline:
            line = self.messages.get(timeout=max(0.001, self.deadline - time.monotonic()))
            if line is None:
                raise RuntimeError("Claude discovery closed")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if self.native:
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError("Claude tool inventory request failed")
                result = message.get("result")
            else:
                response = message.get("response")
                if (
                    message.get("type") != "control_response"
                    or not isinstance(response, dict)
                    or response.get("request_id") != request_id
                ):
                    continue
                if response.get("subtype") != "success":
                    raise RuntimeError("Claude discovery request failed")
                result = response.get("response")
            return result if isinstance(result, dict) else {}
        raise TimeoutError("Claude discovery timed out")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=0.3)
        self.reader.join(timeout=0.2)
        for handle in (self.proc.stdin, self.proc.stdout if not self.reader.is_alive() else None):
            if handle:
                with suppress(OSError):
                    handle.close()


def discover_tools(root: Path) -> list[dict[str, Any]]:
    """Discover all pages of native tools and all connected MCP servers' tools."""
    if os.environ.get("HARNESS_ROUTER_CLAUDE_DISCOVERY", "1") == "0":
        return []
    binary = os.environ.get("HARNESS_ROUTER_CLAUDE_BIN") or shutil.which("claude")
    if not binary:
        return []
    try:
        timeout = float(os.environ.get("HARNESS_ROUTER_DISCOVERY_TIMEOUT", "8"))
        if not math.isfinite(timeout) or timeout <= 0:
            return []
    except ValueError:
        return []
    deadline = time.monotonic() + timeout
    tools: list[dict[str, Any]] = []
    for native in (True, False):
        if time.monotonic() >= deadline:
            break
        client = None
        try:
            client = ClaudeMetadataClient(binary, root, deadline, native=native)
            if native:
                client.request(
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "harness-router-tool-discovery", "version": "1"},
                    },
                )
                client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                cursor = None
                seen_cursors: set[str] = set()
                while time.monotonic() < deadline:
                    page = client.request("tools/list", {"cursor": cursor} if cursor else {})
                    for tool in normalize_tools(page):
                        tool["source"] = "claude-native"
                        tools.append(tool)
                    cursor = page.get("nextCursor")
                    if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                        break
                    seen_cursors.add(cursor)
            else:
                client.request("initialize", {"hooks": None})
                while time.monotonic() < deadline:
                    status = client.request("mcp_status", {})
                    tools.extend(tools_from_status(status))
                    servers = status.get("mcpServers", [])
                    if not isinstance(servers, list) or not any(
                        isinstance(server, dict) and server.get("status") == "pending"
                        for server in servers
                    ):
                        break
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except (OSError, ValueError, TypeError, RuntimeError, queue.Empty):
            pass
        finally:
            if client:
                client.close()
    return normalize_tools(tools)


def write_catalog(path: Path, registry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as handle:
            temporary = handle.name
            json.dump(registry, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def live_registry(root: Path, session_id: str | None = None) -> dict[str, Any]:
    """Query the running discovery processes, then apply explicit descriptor overrides."""
    tools = discover_tools(root)
    extra_path = os.environ.get("HARNESS_ROUTER_CLAUDE_TOOLS_FILE", "").strip()
    if extra_path:
        path = Path(extra_path).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            tools.extend(normalize_tools(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            print(
                "Harness Router could not read the optional Claude tool catalog.", file=sys.stderr
            )

    extra_json = os.environ.get("HARNESS_ROUTER_CLAUDE_TOOLS_JSON", "").strip()
    if extra_json:
        try:
            tools.extend(normalize_tools(json.loads(extra_json)))
        except ValueError:
            print("Harness Router ignored invalid Claude tool catalog JSON.", file=sys.stderr)

    tools = normalize_tools(tools)
    return {
        "generated": True,
        "provider": "claude",
        "session_id": session_id,
        "discovered_at": datetime.now(UTC).isoformat(),
        "tool_count": len(tools),
        "tools": tools,
    }


def expose_registry(path: Path) -> None:
    # Claude loads CLAUDE_ENV_FILE into subsequent Bash commands in this session.
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        try:
            with Path(env_file).open("a", encoding="utf-8") as handle:
                handle.write(f"\nexport HARNESS_ROUTER_TOOL_REGISTRY={shlex.quote(str(path))}\n")
        except OSError:
            print("Harness Router could not export the registry path.", file=sys.stderr)


def main() -> int:
    if os.environ.get("HARNESS_ROUTER_CLAUDE_DISCOVERY_ACTIVE") == "1":
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-tools", action="store_true", help="query the live registry and print JSON"
    )
    parser.add_argument("--project", type=Path, help="project to discover tools for")
    args = parser.parse_args()
    root = (args.project or project_root()).resolve()
    if args.list_tools:
        print(json.dumps(live_registry(root), ensure_ascii=False, indent=2))
        return 0

    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return 0

    session_id = str(payload.get("session_id") or "default")
    registry = live_registry(root, session_id)
    path = session_catalog(root, session_id)
    try:
        write_catalog(path, registry)
        write_catalog(root / ".claude" / "harness-router-tools.json", registry)
    except OSError:
        print("Harness Router could not save the Claude tool catalog.", file=sys.stderr)
        return 0
    expose_registry(path)

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        f"Harness Router discovered {registry['tool_count']} tools at startup. "
                        f"The tool registry for this session is available at {path}. "
                        "Its PreToolUse hook may request one re-plan per user turn; "
                        "normal tool permissions still apply."
                    ),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
