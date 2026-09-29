#!/usr/bin/env python3
"""Generate the Harness Router tool catalog once at Codex session startup.

Codex SessionStart does not include tool definitions. To obtain real MCP tool
metadata up front, this hook starts a short-lived Codex app-server instance and
calls mcpServerStatus/list. The returned MCP names, descriptions, annotations,
and schemas are written immediately to .codex/harness-router-tools.json.

Core Codex tools are added from a small canonical catalog. PreToolUse never
learns/persists tools, so descriptions are not guessed from first use.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
from typing import Any


APP_SERVER_TIMEOUT = float(os.environ.get("HARNESS_ROUTER_DISCOVERY_TIMEOUT", "8"))
BACKGROUND_ENV = "HARNESS_ROUTER_DISCOVERY_BACKGROUND"


CORE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "Bash",
        "description": "Run a shell command in the Codex workspace.",
        "category": "execute",
        "risk": "medium",
        "input_schema": None,
        "source": "codex-core",
    },
    {
        "name": "apply_patch",
        "description": "Apply a patch to files in the Codex workspace.",
        "category": "mutate",
        "risk": "medium",
        "input_schema": None,
        "source": "codex-core",
    },
]


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


def _risk_from_annotations(annotations: Any) -> tuple[str, str]:
    if not isinstance(annotations, dict):
        return "general", "medium"

    read_only = annotations.get("readOnlyHint") is True
    destructive = annotations.get("destructiveHint") is True

    if read_only:
        return "inspect", "low"
    if destructive:
        return "mutate", "high"
    return "general", "medium"


class AppServerClient:
    def __init__(self, cwd: str) -> None:
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("codex executable not found")

        self.proc = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        self.next_id = 1

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=1)

    def _send(self, message: dict[str, Any]) -> None:
        if self.proc.stdin is None:
            raise RuntimeError("app-server stdin unavailable")
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = APP_SERVER_TIMEOUT,
    ) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1

        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

        if self.proc.stdout is None:
            raise RuntimeError("app-server stdout unavailable")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready:
                break

            line = self.proc.stdout.readline()
            if not line:
                break

            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue

            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            result = response.get("result")
            return result if isinstance(result, dict) else {}

        raise TimeoutError(f"timed out waiting for {method}")


def _discover_mcp_tools(cwd: str) -> list[dict[str, Any]]:
    client = AppServerClient(cwd)
    try:
        client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "harness-router-tool-discovery",
                    "title": "Harness Router tool discovery",
                    "version": "1",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                },
            },
        )
        client.notify("initialized")

        result = client.request(
            "mcpServerStatus/list",
            {"detail": "full"},
        )

        tools: list[dict[str, Any]] = []
        for server in result.get("data", []):
            if not isinstance(server, dict):
                continue
            server_name = str(server.get("name", "")).strip()
            server_tools = server.get("tools", {})
            if not server_name or not isinstance(server_tools, dict):
                continue

            for raw_name, definition in server_tools.items():
                if not isinstance(definition, dict):
                    continue

                tool_name = str(definition.get("name") or raw_name).strip()
                if not tool_name:
                    continue

                annotations = definition.get("annotations")
                category, risk = _risk_from_annotations(annotations)

                tools.append(
                    {
                        "name": f"mcp__{server_name}__{tool_name}",
                        "description": str(definition.get("description") or ""),
                        "title": definition.get("title"),
                        "category": category,
                        "risk": risk,
                        "input_schema": definition.get("inputSchema"),
                        "output_schema": definition.get("outputSchema"),
                        "annotations": annotations,
                        "source": "mcp",
                        "server": server_name,
                    }
                )

        return tools
    finally:
        client.close()


def _normalize_extra_tools(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("tools", [])
    if not isinstance(raw, list):
        return []

    result: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict) and str(item.get("name", "")).strip():
            tool = dict(item)
            tool["name"] = str(tool["name"]).strip()
            tool.setdefault("description", "")
            tool.setdefault("category", "general")
            tool.setdefault("risk", "medium")
            tool.setdefault("source", "runtime")
            result.append(tool)
    return result


def _merge(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for group in groups:
        for tool in group:
            name = str(tool.get("name", "")).strip()
            if not name:
                continue
            if name not in merged:
                order.append(name)
                merged[name] = dict(tool)
                continue
            for key, value in tool.items():
                if value not in (None, "", [], {}):
                    merged[name][key] = value

    return [merged[name] for name in order]


def _write_catalog(path: Path, session_id: str, tools: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generated": True,
                "session_id": session_id,
                "tool_count": len(tools),
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
                        f"Harness Router discovered {tool_count} tools at session startup."
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

    extra_tools: list[dict[str, Any]] = []
    raw_extra = os.environ.get("HARNESS_ROUTER_CODEX_TOOLS_JSON", "").strip()
    if raw_extra:
        try:
            extra_tools = _normalize_extra_tools(json.loads(raw_extra))
        except json.JSONDecodeError:
            pass

    generated_catalog = root / ".codex" / "harness-router-tools.json"
    session_catalog = (
        root / ".codex" / "harness-router" / "sessions" / f"{session_id}.json"
    )

    # SessionStart is on the critical path. Never block the harness on MCP
    # discovery: seed a usable catalog immediately and refresh it in a detached
    # worker. This keeps the hook well below Codex's hook timeout.
    if os.environ.get(BACKGROUND_ENV) != "1":
        seed_tools = _merge(CORE_TOOLS, extra_tools)
        _write_catalog(generated_catalog, session_id, seed_tools)
        _write_catalog(session_catalog, session_id, seed_tools)

        try:
            env = os.environ.copy()
            env[BACKGROUND_ENV] = "1"
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve())],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except OSError:
            pass

        _emit_session_start(len(seed_tools))
        return 0

    try:
        mcp_tools = _discover_mcp_tools(cwd)
    except Exception:
        mcp_tools = []

    tools = _merge(CORE_TOOLS, mcp_tools, extra_tools)
    _write_catalog(generated_catalog, session_id, tools)
    _write_catalog(session_catalog, session_id, tools)

    print(
        f"Harness Router wrote {len(tools)} tools at SessionStart",
        file=sys.stderr,
    )
    _emit_session_start(len(tools))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
