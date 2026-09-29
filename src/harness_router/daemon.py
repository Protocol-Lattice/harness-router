from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .adapters import GenericToolAdapter
from .config import OpenRouterConfig, RoutingConfig
from .models import HarnessState, RoutingMode
from .provider import OpenRouterJevProvider
from .router import JevToolRouter

_MAX_REQUEST = 512 * 1024
_CONNECT_TIMEOUT = 0.35
_START_TIMEOUT = 1.5


def socket_path() -> Path | None:
    if os.name == "nt":
        return None
    configured = os.environ.get("HARNESS_ROUTER_SOCKET")
    if configured:
        return Path(configured)
    uid = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
    return Path(tempfile.gettempdir()) / f"harness-router-{uid}.sock"


async def ensure_daemon() -> bool:
    path = socket_path()
    if path is None:
        return False
    if _is_alive(path):
        return True

    env = os.environ.copy()
    env["HARNESS_ROUTER_DAEMON"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "harness_router.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
    except OSError:
        return False

    deadline = asyncio.get_running_loop().time() + _START_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        if _is_alive(path):
            return True
        await asyncio.sleep(0.02)
    return False


async def request_daemon(request: Mapping[str, object]) -> dict[str, object] | None:
    path = socket_path()
    if path is None:
        return None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)),
            timeout=_CONNECT_TIMEOUT,
        )
        wire = (json.dumps(dict(request), ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        writer.write(wire)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=30.0)
        writer.close()
        await writer.wait_closed()
        if not line:
            return None
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
        return None


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    provider: OpenRouterJevProvider,
    routers: dict[tuple[object, ...], JevToolRouter],
) -> None:
    try:
        line = await reader.readline()
        if len(line) > _MAX_REQUEST:
            raise ValueError("request too large")
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError("request must be an object")

        key = (
            request.get("mode", "hybrid"),
            float(request.get("direct_threshold", 0.85)),
            float(request.get("fallback_threshold", 0.60)),
            int(request.get("hierarchical_threshold", 24)),
        )
        router = routers.get(key)
        if router is None:
            router = JevToolRouter(
                provider,
                RoutingConfig(
                    mode=RoutingMode(str(key[0])),
                    direct_execution_threshold=float(key[1]),
                    fallback_threshold=float(key[2]),
                    hierarchical_threshold=int(key[3]),
                ),
            )
            routers[key] = router

        adapter = GenericToolAdapter()
        raw_tools = request.get("tools", [])
        if not isinstance(raw_tools, list):
            raise ValueError("tools must be a list")
        tools = [adapter.normalize(item) for item in raw_tools if isinstance(item, Mapping)]

        before = provider.requests_made
        decision = await router.route(
            HarnessState(
                goal=str(request.get("goal", "")),
                observation=request.get("observation"),
                last_action=request.get("last_action"),
            ),
            tools,
        )
        payload: dict[str, object] = {
            "tool": decision.tool,
            "category": decision.category,
            "confidence": round(decision.confidence, 4),
            "fallback": decision.fallback,
            "fallback_reason": decision.fallback_reason,
            "provider": "openrouter",
            "provider_requests": provider.requests_made - before,
            "daemon": True,
        }
    except Exception as exc:
        payload = {
            "tool": None,
            "category": None,
            "confidence": 0.0,
            "fallback": True,
            "fallback_reason": f"daemon_error:{type(exc).__name__}",
            "provider": "openrouter",
            "provider_requests": 0,
            "daemon": True,
        }
    writer.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    try:
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _serve() -> None:
    path = socket_path()
    if path is None:
        raise SystemExit("persistent daemon is only supported on Unix-like systems")
    try:
        path.unlink()
    except FileNotFoundError:
        pass

    provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
    routers: dict[tuple[object, ...], JevToolRouter] = {}
    server = await asyncio.start_unix_server(
        lambda reader, writer: _handle(reader, writer, provider, routers),
        path=str(path),
        limit=_MAX_REQUEST,
    )
    try:
        os.chmod(path, 0o600)
        async with server:
            await server.serve_forever()
    finally:
        server.close()
        await server.wait_closed()
        await provider.aclose()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _is_alive(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.05)
            sock.connect(str(path))
        return True
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass
        return False


if __name__ == "__main__":
    if os.environ.get("HARNESS_ROUTER_DAEMON") != "1":
        os.environ["HARNESS_ROUTER_DAEMON"] = "1"
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
