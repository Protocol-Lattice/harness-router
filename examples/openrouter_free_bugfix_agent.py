#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness_router import (
    ActionSummary,
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RiskLevel,
    RoutingConfig,
    ToolDescriptor,
)
from openrouter_free_agent import OpenRouterFreeAgent
from utcp.utcp_client import UtcpClient
from utcp_code_mode import CodeModeUtcpClient


PROVIDER = Path(__file__).with_name("utcp_local_tools_provider.py").resolve()


class ClosableCodeModeUtcpClient(CodeModeUtcpClient):
    async def close(self) -> None:
        await self._base_client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find a bug, fix it, and verify it with Jev routing, "
            "OpenRouter Free planning, and UTCP CodeMode execution."
        )
    )
    parser.add_argument(
        "--goal",
        default="Find a real bug in this repository, fix it, and verify the fix.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENROUTER_AGENT_MODEL", "openrouter/free"),
    )
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute generated CodeMode programs.",
    )
    parser.add_argument(
        "--allow-mutation",
        action="store_true",
        help="Allow filesystem write/patch operations.",
    )
    parser.add_argument(
        "--allow-bash",
        action="store_true",
        help="Allow Bash commands for reproduction and verification.",
    )
    parser.add_argument(
        "--show-code",
        action="store_true",
        help="Print each CodeMode program before execution.",
    )
    return parser


def phase_tool(name: str, description: str, category: str, risk: RiskLevel) -> ToolDescriptor:
    return ToolDescriptor(
        name=f"phase.{name}",
        description=description,
        category=category,
        risk=risk,
    )


def available_phases(history: list[ActionSummary]) -> list[ToolDescriptor]:
    names = [item.tool for item in history]
    fixed = "phase.fix" in names
    verified = fixed and "phase.verify" in names[names.index("phase.fix") + 1 :]

    if not history:
        return [
            phase_tool(
                "inspect",
                "Inspect repository structure, source code, and tests to locate suspicious behavior.",
                "inspect",
                RiskLevel.LOW,
            )
        ]

    if not fixed:
        return [
            phase_tool(
                "inspect",
                "Read more relevant source or tests when the bug location is still unclear.",
                "inspect",
                RiskLevel.LOW,
            ),
            phase_tool(
                "reproduce",
                "Run a targeted test or check to demonstrate a suspected bug before editing.",
                "verify",
                RiskLevel.LOW,
            ),
            phase_tool(
                "fix",
                "Apply a minimal patch only when current observations identify a concrete bug.",
                "mutate",
                RiskLevel.MEDIUM,
            ),
        ]

    if not verified:
        return [
            phase_tool(
                "verify",
                "Run targeted and broader tests after the fix and inspect failures if any.",
                "verify",
                RiskLevel.LOW,
            ),
            phase_tool(
                "fix",
                "Revise the previous fix if verification shows it is incomplete or incorrect.",
                "mutate",
                RiskLevel.MEDIUM,
            ),
            phase_tool(
                "inspect",
                "Inspect the modified code or failing verification output before another fix.",
                "inspect",
                RiskLevel.LOW,
            ),
        ]

    return [
        phase_tool(
            "finish",
            "Finish because a fix was applied and a later verification phase completed.",
            "finish",
            RiskLevel.LOW,
        ),
        phase_tool(
            "verify",
            "Run another verification if confidence in the previous verification is insufficient.",
            "verify",
            RiskLevel.LOW,
        ),
        phase_tool(
            "fix",
            "Apply another correction only if verification exposed a remaining defect.",
            "mutate",
            RiskLevel.MEDIUM,
        ),
    ]


def phase_name(tool_name: str) -> str:
    return tool_name.rsplit(".", 1)[-1]


def gate_phase(phase: str, *, allow_mutation: bool, allow_bash: bool) -> None:
    if phase == "fix" and not allow_mutation:
        raise SystemExit(
            "The agent reached the fix phase. Re-run with --allow-mutation."
        )
    if phase in {"reproduce", "verify"} and not allow_bash:
        raise SystemExit(
            f"The agent reached the {phase} phase. Re-run with --allow-bash."
        )


def compact_result(result: Any, limit: int = 7000) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


async def create_codemode_client(workspace: Path) -> ClosableCodeModeUtcpClient:
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(PROVIDER))}"
    base_client = await UtcpClient.create(
        root_dir=str(workspace),
        config={
            "manual_call_templates": [
                {
                    "name": "local",
                    "call_template_type": "cli",
                    "commands": [{"command": command}],
                    "working_dir": str(workspace),
                }
            ]
        },
    )
    return ClosableCodeModeUtcpClient(base_client)


async def main() -> None:
    args = build_parser().parse_args()

    if args.max_steps < 1:
        raise SystemExit("--max-steps must be >= 1")

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required")

    workspace = Path.cwd().resolve()
    code_mode = await create_codemode_client(workspace)
    interfaces = await code_mode.get_all_tools_python_interfaces()

    jev_provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
    router = JevToolRouter(
        jev_provider,
        RoutingConfig(
            direct_execution_threshold=0.80,
            fallback_threshold=0.50,
        ),
    )
    planner = OpenRouterFreeAgent(api_key=api_key, model=args.model)

    history: list[ActionSummary] = []
    observation = ""

    try:
        for step in range(1, args.max_steps + 1):
            phases = available_phases(history)
            state = HarnessState(
                goal=args.goal,
                observation=observation or None,
                last_action=history[-1].tool if history else None,
                recent_actions=history[-6:],
                constraints=[
                    "Inspect before editing.",
                    "Reproduce or demonstrate a concrete bug before fixing it.",
                    "Keep fixes minimal.",
                    "Verify after every fix.",
                    "Do not finish before a post-fix verification phase.",
                ],
            )

            decision = await router.route(state, phases)

            if decision.fallback or decision.tool is None:
                phase = phase_name(phases[0].name)
                print(
                    f"\n[step {step}] Jev fallback ({decision.fallback_reason}); "
                    f"using safe phase: {phase}"
                )
            else:
                phase = phase_name(decision.tool)
                print(
                    f"\n[step {step}] Jev selected: {phase} "
                    f"(confidence={decision.confidence:.3f})"
                )

            if phase == "finish":
                plan = await planner.plan(
                    goal=args.goal,
                    phase="finish",
                    observation=observation,
                    interfaces=interfaces,
                    allow_mutation=args.allow_mutation,
                    allow_bash=args.allow_bash,
                )
                print(f"\nFinal agent summary:\n{plan.summary}")
                return

            gate_phase(
                phase,
                allow_mutation=args.allow_mutation,
                allow_bash=args.allow_bash,
            )

            plan = await planner.plan(
                goal=args.goal,
                phase=phase,
                observation=observation,
                interfaces=interfaces,
                allow_mutation=args.allow_mutation,
                allow_bash=args.allow_bash,
            )
            print(f"Agent plan: {plan.summary}")

            if plan.done:
                print("Agent requested completion.")
                return

            if not plan.code.strip():
                raise SystemExit(
                    "OpenRouter agent returned no CodeMode code for this phase."
                )

            if args.show_code:
                print("\nCodeMode program:")
                print(plan.code)

            if not args.execute:
                print(
                    "\nDry run complete. Add --execute --allow-bash "
                    "--allow-mutation for the full workflow."
                )
                return

            result = await code_mode.call_tool_chain(plan.code, timeout=60)
            result_text = compact_result(result)
            print("\nCodeMode result:")
            print(result_text)

            observation = (
                f"Phase: {phase}\n"
                f"Agent summary: {plan.summary}\n"
                f"Execution result: {result_text}"
            )
            history.append(
                ActionSummary(
                    tool=f"phase.{phase}",
                    outcome=plan.summary,
                )
            )

        raise SystemExit(
            f"Reached --max-steps={args.max_steps} before verified completion."
        )
    finally:
        await planner.close()
        await jev_provider.aclose()
        await code_mode.close()


if __name__ == "__main__":
    asyncio.run(main())
