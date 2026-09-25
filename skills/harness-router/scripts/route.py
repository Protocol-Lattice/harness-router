#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from typing import Any

from harness_router import (
    GenericToolAdapter,
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RoutingConfig,
    RoutingMode,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Route a harness action through OpenRouter Jev."
    )
    parser.add_argument("--goal", required=True, help="Current user/harness goal.")
    parser.add_argument("--observation", default=None, help="Latest compact observation.")
    parser.add_argument("--last-action", default=None, help="Previous action/tool name.")
    parser.add_argument(
        "--tools-json",
        required=True,
        help="JSON array of tool objects with at least name and description.",
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in RoutingMode],
        default=RoutingMode.HYBRID.value,
    )
    parser.add_argument("--direct-threshold", type=float, default=0.72)
    parser.add_argument("--fallback-threshold", type=float, default=0.72)
    parser.add_argument("--hierarchical-threshold", type=int, default=48)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=2.0,
        help="Bound Jev provider latency for the Codex helper.",
    )
    parser.add_argument("--description-limit", type=int, default=96)
    parser.add_argument("--state-field-limit", type=int, default=400)
    parser.add_argument("--history-limit", type=int, default=1)
    parser.add_argument("--constraint-limit", type=int, default=2)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include the full probability map and pretty-print the result.",
    )
    return parser.parse_args()


def load_tools(raw: str) -> list[Mapping[str, Any]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid --tools-json: {exc}") from exc

    if not isinstance(value, list) or not value:
        raise SystemExit("--tools-json must be a non-empty JSON array")

    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SystemExit(f"tool at index {index} must be a JSON object")
        result.append(item)
    return result


async def run(args: argparse.Namespace) -> int:
    adapter = GenericToolAdapter()
    tools = [adapter.normalize(tool) for tool in load_tools(args.tools_json)]

    provider = OpenRouterJevProvider.from_config(\n        OpenRouterConfig(timeout_seconds=args.timeout_seconds)\n    )
    router = JevToolRouter(
        provider,
        RoutingConfig(
            mode=RoutingMode(args.mode),
            direct_execution_threshold=args.direct_threshold,
            fallback_threshold=args.fallback_threshold,
            hierarchical_threshold=args.hierarchical_threshold,
            description_limit=args.description_limit,
            state_field_limit=args.state_field_limit,
            history_limit=args.history_limit,
            constraint_limit=args.constraint_limit,
        ),
    )

    try:
        decision = await router.route(
            HarnessState(
                goal=args.goal,
                observation=args.observation,
                last_action=args.last_action,
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
    }
    if args.verbose:
        payload["probabilities"] = dict(decision.probabilities)
        output = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        output = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    print(output)
    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
