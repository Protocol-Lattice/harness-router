"""MCTS-assisted coding-agent routing.

Search is side-effect free: the simulator predicts likely outcomes. The harness
executes only the first tool selected by MCTS after normal policy/approval checks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from harness_router import (
    HarnessState,
    JevToolRouter,
    MCTSConfig,
    MCTSToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RiskLevel,
    RoutingConfig,
    SimulatedStep,
    ToolDescriptor,
)

TOOLS = [
    ToolDescriptor(
        "read_file",
        "Read a repository file by path",
        category="inspect",
        risk=RiskLevel.LOW,
    ),
    ToolDescriptor(
        "search_code",
        "Search repository text for symbols or patterns",
        category="inspect",
        risk=RiskLevel.LOW,
    ),
    ToolDescriptor(
        "patch_file",
        "Apply a source-code patch",
        category="mutate",
        risk=RiskLevel.MEDIUM,
    ),
    ToolDescriptor(
        "run_tests",
        "Run focused tests for the changed code",
        category="verify",
        risk=RiskLevel.LOW,
    ),
]


class CodingSimulator:
    """Tiny heuristic world model used only for planning."""

    async def tools(self, state: HarnessState) -> Sequence[ToolDescriptor]:
        if state.observation == "candidate patch prepared":
            return [TOOLS[0], TOOLS[3]]
        if state.observation == "relevant code located":
            return [TOOLS[2], TOOLS[3]]
        return TOOLS

    async def transition(
        self,
        state: HarnessState,
        tool: ToolDescriptor,
    ) -> SimulatedStep:
        if tool.name == "search_code":
            observation = "relevant code located"
            reward = 0.15
            terminal = False
        elif tool.name == "read_file":
            observation = "relevant code located"
            reward = 0.10
            terminal = False
        elif tool.name == "patch_file":
            observation = "candidate patch prepared"
            reward = 0.25
            terminal = False
        elif tool.name == "run_tests" and state.observation == "candidate patch prepared":
            observation = "tests passed"
            reward = 1.0
            terminal = True
        else:
            observation = "tests did not yet validate a fix"
            reward = 0.05
            terminal = False

        return SimulatedStep(
            state=HarnessState(
                goal=state.goal,
                observation=observation,
                last_action=tool.name,
                constraints=state.constraints,
            ),
            reward=reward,
            terminal=terminal,
        )

    async def evaluate(self, state: HarnessState) -> float:
        return {
            "tests passed": 1.0,
            "candidate patch prepared": 0.40,
            "relevant code located": 0.20,
        }.get(state.observation or "", 0.0)


async def main() -> None:
    provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
    jev_router = JevToolRouter(provider, RoutingConfig())
    router = MCTSToolRouter(
        CodingSimulator(),
        policy_router=jev_router,
        config=MCTSConfig(
            simulations=64,
            max_depth=3,
            # One Jev request supplies the root prior; the remaining search is local.
            max_policy_evaluations=1,
        ),
    )

    state = HarnessState(
        goal="Fix the failing parser test",
        observation="pytest points at parser behavior",
        constraints=["Do not modify generated files"],
    )

    try:
        result = await router.search(state, TOOLS)
    finally:
        await provider.aclose()

    print("selected:", result.decision.tool)
    print("principal variation:", " -> ".join(result.principal_variation))
    print("root visits:", dict(result.root_visits))

    # The real coding harness would now generate arguments, run normal approval/
    # policy checks, and execute ONLY result.decision.tool.


if __name__ == "__main__":
    asyncio.run(main())
