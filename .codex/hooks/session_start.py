#!/usr/bin/env python3
"""Start the persistent Harness Router HTTP MCP server for a Codex session."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time


HOST = os.environ.get("HARNESS_ROUTER_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("HARNESS_ROUTER_MCP_PORT", "8765"))
PATH = os.environ.get("HARNESS_ROUTER_MCP_PATH", "/mcp")
ENDPOINT = os.environ.get("HARNESS_ROUTER_MCP_URL", f"http://{HOST}:{PORT}{PATH}")
STARTUP_TIMEOUT = float(os.environ.get("HARNESS_ROUTER_MCP_STARTUP_TIMEOUT", "5"))


def _endpoint_ready() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.25):
            return True
    except OSError:
        return False


def _start_server() -> subprocess.Popen[str]:
    binary = shutil.which("harness-router-mcp")
    if binary is None:
        raise RuntimeError(
            "harness-router-mcp is not on PATH. "
            "Install harness-router before starting Codex."
        )

    cache_dir = Path.home() / ".cache" / "harness-router"
    cache_dir.mkdir(parents=True, exist_ok=True)
    log_path = cache_dir / "codex-mcp.log"
    log = log_path.open("a", encoding="utf-8")

    try:
        return subprocess.Popen(
            [
                binary,
                "--transport",
                "streamable-http",
                "--host",
                HOST,
                "--port",
                str(PORT),
                "--path",
                PATH,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except Exception:
        log.close()
        raise


def main() -> int:
    # A custom/remote endpoint is managed by the caller, not this local hook.
    if ENDPOINT.rstrip("/") != f"http://{HOST}:{PORT}{PATH}".rstrip("/"):
        return 0

    if _endpoint_ready():
        return 0

    try:
        process = _start_server()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Harness Router SessionStart: {exc}", file=sys.stderr)
        return 0

    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if _endpoint_ready():
            return 0
        if process.poll() is not None:
            print(
                "Harness Router SessionStart: MCP server exited during startup. "
                "See ~/.cache/harness-router/codex-mcp.log",
                file=sys.stderr,
            )
            return 0
        time.sleep(0.05)

    print(
        "Harness Router SessionStart: timed out waiting for "
        f"http://{HOST}:{PORT}{PATH}. "
        "PreToolUse will fail open.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
