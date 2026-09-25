#!/usr/bin/env python3
"""UTCP + harness-router example with filesystem and Bash tools.

Flow:
    UTCP discovery -> ToolDescriptor normalization -> OpenRouter Jev routing
    -> explicit policy gate -> optional UTCP execution

Install:
    pip install -e ".[utcp]"

Environment:
    export OPENROUTER_API_KEY="..."

Examples:
    python examples/utcp_filesystem_bash.py \
      --goal "List files in this repository" \
      --execute \
      --args-json '{"path":"."}'

    python examples/utcp_filesystem_bash.py \
      --goal "Read the README" \
      --execute \
      --args-json '{"path":"README.md"}'

    python examples/utcp_filesystem_bash.py \
      --goal "Write hello.txt" \
      --execute --allow-mutation \
      --args-json '{"path":"hello.txt","content":"hello\\n"}'

    python examples/utcp_filesystem_bash.py \
      --goal "Patch hello.txt" \
      --execute --allow-mutation \
      --args-json '{"path":"hello.txt","old_text":"hello","new_text":"hello world"}'

    python examples/utcp_filesystem_bash.py \
      --goal "Show git status using Bash" \
      --execute --allow-bash \
      --args-json '{"command":"git status --short"}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from harness_router import (
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RiskLevel,
    RoutingConfig,
    ToolDescriptor,
    UTCPToolAdapter,
)
from utcp.utcp_client import UtcpClient


PROVIDER = Path(__file__).with_name("utcp_local_tools_provider.py").resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Route UTCP filesystem/Bash tools through OpenRouter Jev."
    )
    parser.add_argument("--goal", required=True)
    parser.add_argument("--observation", default=None)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the selected UTCP tool after routing.",
    )
    parser.add_argument(
        "--args-json",
        default="{}",
        help="JSON object passed to the selected UTCP tool.",
    )
    parser.add_argument(
        "--allow-mutation",
        action="store_true",
        help="Allow fs_write/fs_patch execution.",
    )
    parser.add_argument(
        "--allow-bash",
        action="store_true",
        help="Allow bash_run execution.",
    )
    return parser


def normalize_utcp_tools(utcp_tools: list[Any]) -> list[ToolDescriptor]:
    adapter = UTCPToolAdapter()
    result: list[ToolDescriptor] = []

    for tool in utcp_tools:
        data = tool.model_dump(by_alias=True)
        short_name = tool.name.rsplit(".", 1)[-1]

        if short_name in {"fs_read", "fs_list"}:
            data["category"] = "inspect"
            data["risk"] = "low"
        elif short_name in {"fs_write", "fs_patch"}:
            data["category"] = "mutate"
            data["risk"] = "medium"
        elif short_name == "bash_run":
            data["category"] = "execute"
            data["risk"] = "high"

        result.append(adapter.normalize(data))

    return result


def check_execution_gate(
    selected: ToolDescriptor,
    *,
    allow_mutation: bool,
    allow_bash: bool,
) -> None:
    short_name = selected.name.rsplit(".", 1)[-1]

    if short_name in {"fs_write", "fs_patch"} and not allow_mutation:
        raise SystemExit(
            "selected tool mutates the filesystem; re-run with --allow-mutation"
        )

    if short_name == "bash_run" and not allow_bash:
        raise SystemExit(
            "selected tool executes arbitrary Bash; re-run with --allow-bash"
        )

    if selected.risk is RiskLevel.CRITICAL:
        raise SystemExit("critical-risk tools are not executable in this example")


async def main() -> None:
    args = build_parser().parse_args()
    workspace = Path.cwd().resolve()

    provider_command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(PROVIDER))}"
    )

    client = await UtcpClient.create(
        root_dir=str(workspace),
        config={
            "manual_call_templates": [
                {
                    "name": "local",
                    "call_template_type": "cli",
                    "commands": [{"command": provider_command}],
                    "working_dir": str(workspace),
                }
            ]
        },
    )

    jev = OpenRouterJevProvider.from_config(OpenRouterConfig())
    router = JevToolRouter(jev, RoutingConfig())

    try:
        discovered = await client.search_tools("", limit=0)
        tools = normalize_utcp_tools(discovered)

        print("UTCP tools:")
        for tool in tools:
            print(
                f"  - {tool.name} "
                f"(category={tool.category}, risk={tool.risk.value})"
            )

        decision = await router.route(
            HarnessState(
                goal=args.goal,
                observation=args.observation,
            ),
            tools,
        )

        print("\nJev decision:")
        print(
            json.dumps(
                {
                    **asdict(decision),
                    "probabilities": dict(decision.probabilities),
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )

        if decision.fallback or not args.execute:
            return

        selected = next(
            (tool for tool in tools if tool.name == decision.tool),
            None,
        )
        if selected is None:
            raise SystemExit(f"selected tool is no longer available: {decision.tool}")

        check_execution_gate(
            selected,
            allow_mutation=args.allow_mutation,
            allow_bash=args.allow_bash,
        )

        try:
            tool_args = json.loads(args.args_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"invalid --args-json: {exc}") from exc

        if not isinstance(tool_args, dict):
            raise SystemExit("--args-json must decode to a JSON object")

        result = await client.call_tool(selected.name, tool_args)

        print("\nUTCP result:")
        print(
            json.dumps(result, indent=2, ensure_ascii=False, default=str)
            if isinstance(result, (dict, list))
            else result
        )
    finally:
        await jev.aclose()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
