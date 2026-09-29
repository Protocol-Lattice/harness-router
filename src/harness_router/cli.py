from __future__ import annotations

import argparse
import asyncio
import json
import os
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Mapping, Sequence

from .adapters import GenericToolAdapter
from .config import OpenRouterConfig, RoutingConfig
from .models import HarnessState, RoutingMode
from .provider import OpenRouterJevProvider
from .router import JevToolRouter


def package_version() -> str:
    try:
        return version("harness-router")
    except PackageNotFoundError:
        return "0.1.1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-router",
        description="Route harness tools through OpenRouter Jev.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")

    subparsers = parser.add_subparsers(dest="command")
    route = subparsers.add_parser(
        "route",
        help="Choose the next tool using OpenRouter Jev.",
    )
    route.add_argument("--goal", required=True, help="Current harness/user goal.")
    route.add_argument("--observation", default=None, help="Latest compact observation.")
    route.add_argument("--last-action", default=None, help="Previously executed action.")
    route.add_argument("--tools-json", required=True, help="JSON array of tool descriptors.")
    route.add_argument(
        "--mode",
        choices=[mode.value for mode in RoutingMode],
        default=RoutingMode.HYBRID.value,
    )
    route.add_argument("--direct-threshold", type=float, default=0.85)
    route.add_argument("--fallback-threshold", type=float, default=0.60)
    route.add_argument("--hierarchical-threshold", type=int, default=24)
    route.add_argument(
        "--verbose",
        action="store_true",
        help="Include the full probability map and pretty-print the result.",
    )
    route.add_argument(
        "--no-daemon",
        action="store_true",
        help="Bypass the persistent local router daemon.",
    )
    route.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable route-decision caching for this request.",
    )
    return parser


def _load_tools(raw: str) -> list[Mapping[str, Any]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --tools-json: {exc}") from exc

    if not isinstance(value, list) or not value:
        raise SystemExit("--tools-json must be a non-empty JSON array")

    tools: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SystemExit(f"tool at index {index} must be a JSON object")
        tools.append(item)
    return tools


def _request_payload(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "goal": args.goal,
        "observation": args.observation,
        "last_action": args.last_action,
        "tools": _load_tools(args.tools_json),
        "mode": args.mode,
        "direct_threshold": args.direct_threshold,
        "fallback_threshold": args.fallback_threshold,
        "hierarchical_threshold": args.hierarchical_threshold,
        "route_cache_size": 0 if args.no_cache else 128,
    }


async def _run_direct(request: Mapping[str, Any]) -> tuple[dict[str, object], int]:
    adapter = GenericToolAdapter()
    tools = [adapter.normalize(tool) for tool in request["tools"]]

    provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
    router = JevToolRouter(
        provider,
        RoutingConfig(
            mode=RoutingMode(str(request["mode"])),
            direct_execution_threshold=float(request["direct_threshold"]),
            fallback_threshold=float(request["fallback_threshold"]),
            hierarchical_threshold=int(request["hierarchical_threshold"]),
            route_cache_size=int(request.get("route_cache_size", 128)),
        ),
    )

    try:
        decision = await router.route(
            HarnessState(
                goal=str(request["goal"]),
                observation=request["observation"],
                last_action=request["last_action"],
            ),
            tools,
        )
    finally:
        await provider.aclose()

    payload: dict[str, object] = {
        "tool": decision.tool,
        "category": decision.category,
        "confidence": round(decision.confidence, 4),
        "fallback": decision.fallback,
        "fallback_reason": decision.fallback_reason,
        "provider": "openrouter",
        "provider_requests": provider.requests_made,
    }
    return payload, (0 if provider.requests_made > 0 else 2)


async def _run_route(args: argparse.Namespace) -> int:
    request = _request_payload(args)
    response: dict[str, object] | None = None

    if not args.no_daemon and os.environ.get("HARNESS_ROUTER_NO_DAEMON") != "1":
        try:
            from .daemon import ensure_daemon, request_daemon

            if await ensure_daemon():
                response = await request_daemon(request)
        except (OSError, asyncio.TimeoutError, ValueError):
            response = None

    if response is None:
        response, status = await _run_direct(request)
    else:
        status = 0 if int(response.get("provider_requests", 0)) > 0 else 2

    if args.verbose:
        output = json.dumps(response, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        output = json.dumps(response, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(output)
    return status


async def _amain(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "route":
        return await _run_route(args)

    parser.print_help()
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        raise SystemExit(asyncio.run(_amain(args, parser)))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
