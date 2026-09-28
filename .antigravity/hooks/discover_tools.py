#!/usr/bin/env python3
"""Read Antigravity's current conversation tool definitions without a model turn.

The local runtime uses Connect JSON RPC. Its generator metadata contains the
actual tool definitions passed to the model, including built-ins and MCP tools.
This is an internal runtime API, verified against Antigravity 2.11.0's schemas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = "/exa.language_server_pb.LanguageServerService/"
MAX_RESPONSE_BYTES = 8_000_000


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Antigravity inventory timed out")
    return seconds


def local_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not parsed.port
    ):
        raise ValueError("Antigravity inventory requires a loopback runtime URL with a port")
    return url.rstrip("/")


def runtime_flags(command: str) -> dict[str, str]:
    words = shlex.split(command)
    flags = {}
    for index, word in enumerate(words):
        if not word.startswith("--"):
            continue
        key, separator, value = word[2:].partition("=")
        if not separator and index + 1 < len(words) and not words[index + 1].startswith("--"):
            value = words[index + 1]
        flags[key] = value
    return flags


def runtime_endpoint(deadline: float) -> tuple[str, str]:
    configured = os.environ.get("HARNESS_ROUTER_ANTIGRAVITY_URL")
    if configured:
        return local_url(configured), os.environ.get("HARNESS_ROUTER_ANTIGRAVITY_CSRF_TOKEN", "")
    # Hooks inherit the owning runtime's process tree. Do not pick an unrelated
    # IDE/session by scanning all of the user's running processes.
    pid = os.getppid()
    visited = set()
    for _ in range(16):
        if pid <= 1 or pid in visited:
            break
        visited.add(pid)
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=,args="],
            capture_output=True,
            text=True,
            timeout=remaining(deadline),
            check=True,
        )
        parent, _, command = result.stdout.strip().partition(" ")
        if not parent.isdecimal():
            break
        if re.search(r"(?:^|[/\\])language_server(?:_[\w]+)?(?:\.exe)?(?:\s|$)", command):
            flags = runtime_flags(command)
            for scheme in ("https", "http"):
                port = flags.get(f"{scheme}_server_port", "")
                if port.isdecimal() and 0 < int(port) <= 65535:
                    return f"{scheme}://127.0.0.1:{port}", flags.get("csrf_token", "")
            # A runtime started with port 0 has no usable endpoint in its argv.
            # An explicit URL is preferable to guessing among its other listeners.
            break
        pid = int(parent)
    raise RuntimeError("No Antigravity runtime endpoint in the hook's process ancestry")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Antigravity inventory does not follow redirects")


def request(url: str, token: str, method: str, payload: dict[str, Any], deadline: float) -> dict:
    url = local_url(url)
    # Antigravity's local runtime uses a self-signed certificate, as does its
    # desktop client. This trust exception is restricted to literal loopback URLs.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    opener = build_opener(ProxyHandler({}), NoRedirects(), HTTPSHandler(context=context))
    headers = {"Content-Type": "application/json", "Connect-Protocol-Version": "1"}
    if token:
        headers["X-Codeium-Csrf-Token"] = token
    query = Request(url + SERVICE + method, data=json.dumps(payload).encode(), headers=headers)
    with opener.open(query, timeout=remaining(deadline)) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("Antigravity inventory response is too large")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("Expected an Antigravity response object")
    return result


def session_path(root: Path, conversation: str) -> Path:
    key = hashlib.sha256(conversation.encode("utf-8")).hexdigest()
    path = root / ".antigravity/harness-router/sessions" / f"{key}.json"
    if not path.parent.resolve().is_relative_to(root.resolve()):
        raise ValueError("Inventory state must stay in the project")
    return path


def descriptor(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str) or not raw["name"]:
        return None
    tool = {
        key: raw[key]
        for key in (
            "name",
            "description",
            "serverName",
            "originalName",
            "strict",
            "annotations",
        )
        if key in raw
    }
    for source, target in (
        ("jsonSchemaString", "input_schema"),
        ("responseJsonSchemaString", "output_schema"),
    ):
        schema = raw.get(source)
        if isinstance(schema, str) and schema:
            try:
                parsed = json.loads(schema)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                tool[target] = parsed
    tool["source"] = "antigravity-mcp" if raw.get("serverName") else "antigravity-native"
    return tool


def active_mcp_tools(tools: list[dict], response: dict) -> list[dict]:
    states = response.get("states", [])
    if not isinstance(states, list):
        raise ValueError("Invalid MCP server inventory")
    active = {}
    for state in states:
        if not isinstance(state, dict) or state.get("status") not in {2, "MCP_SERVER_STATUS_READY"}:
            continue
        spec = state.get("spec")
        if isinstance(spec, dict) and not spec.get("disabled"):
            name = spec.get("serverName")
            if isinstance(name, str):
                active[name] = spec
    result = []
    for tool in tools:
        server = tool.get("serverName")
        if not server:
            result.append(tool)
            continue
        spec = active.get(server)
        if spec is None:
            continue
        name = tool.get("originalName") or tool["name"]
        if name in spec.get("disabledTools", []):
            continue
        if spec.get("enabledTools") and name not in spec["enabledTools"]:
            continue
        result.append(tool)
    return result


def discover(payload: dict[str, Any], root: Path = PROJECT_ROOT) -> dict[str, Any]:
    conversation = payload.get("conversationId")
    if not isinstance(conversation, str) or not conversation.strip():
        raise ValueError("A conversationId is required for live inventory")
    timeout = float(os.environ.get("HARNESS_ROUTER_DISCOVERY_TIMEOUT", "2"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Expected a positive inventory timeout")
    deadline = time.monotonic() + timeout
    url, token = runtime_endpoint(deadline)
    offset = 0
    try:
        saved = json.loads(session_path(root, conversation).read_text())
        if isinstance(saved, dict) and saved.get("conversation_id") == conversation:
            candidate = saved.get("generator_metadata_offset")
            if type(candidate) is int and candidate >= 0:
                offset = candidate
    except (OSError, ValueError):
        pass
    response = request(
        url,
        token,
        "GetCascadeTrajectoryGeneratorMetadata",
        {
            "cascadeId": conversation,
            "generatorMetadataOffset": offset,
            "includeMessages": False,
        },
        deadline,
    )
    metadata = response.get("generatorMetadata", [])
    if not metadata and offset:
        offset = 0
        response = request(
            url,
            token,
            "GetCascadeTrajectoryGeneratorMetadata",
            {
                "cascadeId": conversation,
                "generatorMetadataOffset": 0,
                "includeMessages": False,
            },
            deadline,
        )
        metadata = response.get("generatorMetadata", [])
    if not isinstance(metadata, list):
        raise ValueError("Invalid generator metadata")
    tools = []
    for index in range(len(metadata) - 1, -1, -1):
        record = metadata[index]
        if not isinstance(record, dict) or not isinstance(record.get("chatModel"), dict):
            continue
        indices = record.get("stepIndices", [])
        step = payload.get("stepIdx")
        if (
            type(step) is int
            and isinstance(indices, list)
            and indices
            and all(type(value) is int and value > step for value in indices)
        ):
            continue
        raw_tools = record["chatModel"].get("tools", [])
        if not isinstance(raw_tools, list):
            raise ValueError("Invalid model tool inventory")
        tools = [tool for raw in raw_tools if (tool := descriptor(raw)) is not None]
        offset += index
        break
    # Current conversation definitions determine membership, so tools disabled
    # for a subagent are never added from the global MCP registry.
    if any(tool.get("serverName") for tool in tools):
        states = request(url, token, "GetMcpServerStates", {}, deadline)
        tools = active_mcp_tools(tools, states)
    return {
        "provider": "antigravity",
        "conversation_id": conversation,
        "discovered_at": datetime.now(UTC).isoformat(),
        "generator_metadata_offset": offset,
        "count": len(tools),
        "tools": tools,
    }


def publish(snapshot: dict[str, Any], root: Path = PROJECT_ROOT) -> None:
    data = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    paths = [
        session_path(root, snapshot["conversation_id"]),
        root / ".antigravity/harness-router-tools.json",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as handle:
                temporary = handle.name
                handle.write(data)
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list-tools", action="store_true", help="print the live inventory as JSON"
    )
    parser.add_argument(
        "--conversation-id", help="query this conversation instead of reading stdin"
    )
    args = parser.parse_args(argv)
    try:
        payload = (
            {"conversationId": args.conversation_id}
            if args.conversation_id
            else json.load(sys.stdin)
        )
        if not isinstance(payload, dict):
            raise ValueError("Expected a hook payload")
        snapshot = discover(payload)
        publish(snapshot)
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
        # Do not publish credentials, raw responses or stale tools on failure.
        print(
            json.dumps(
                {
                    "provider": "antigravity",
                    "tools": [],
                    "count": 0,
                    "error": "live_inventory_unavailable",
                }
            )
        )
        return 0
    print(json.dumps(snapshot) if args.list_tools else "{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
