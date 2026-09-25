from __future__ import annotations

from collections.abc import Sequence

import pytest

from harness_router import (
    HarnessState,
    MCTSConfig,
    MCTSToolRouter,
    RouteDecision,
    SimulatedStep,
    ToolDescriptor,
)


def tool(name: str) -> ToolDescriptor:
    return ToolDescriptor(name=name, description=name, category="test")


class LongHorizonEnvironment:
    async def tools(self, state: HarnessState) -> Sequence[ToolDescriptor]:
        if state.goal == "root":
            return [tool("quick"), tool("invest")]
        if state.goal == "invested":
            return [tool("finish")]
        return []

    async def transition(
        self,
        state: HarnessState,
        selected: ToolDescriptor,
    ) -> SimulatedStep:
        if state.goal == "root" and selected.name == "quick":
            return SimulatedStep(HarnessState(goal="quick_done"), reward=0.60, terminal=True)
        if state.goal == "root" and selected.name == "invest":
            return SimulatedStep(HarnessState(goal="invested"), reward=0.05)
        if state.goal == "invested" and selected.name == "finish":
            return SimulatedStep(HarnessState(goal="finished"), reward=1.0, terminal=True)
        raise AssertionError((state.goal, selected.name))

    async def evaluate(self, state: HarnessState) -> float:
        return 0.0


class FlatEnvironment:
    async def tools(self, state: HarnessState) -> Sequence[ToolDescriptor]:
        return [tool("a"), tool("b")]

    async def transition(
        self,
        state: HarnessState,
        selected: ToolDescriptor,
    ) -> SimulatedStep:
        return SimulatedStep(
            HarnessState(goal=f"done:{selected.name}"),
            reward=0.0,
            terminal=True,
        )

    async def evaluate(self, state: HarnessState) -> float:
        return 0.0


class EmptyEnvironment:
    async def tools(self, state: HarnessState) -> Sequence[ToolDescriptor]:
        return []

    async def transition(
        self,
        state: HarnessState,
        selected: ToolDescriptor,
    ) -> SimulatedStep:
        raise AssertionError("transition must not be called")

    async def evaluate(self, state: HarnessState) -> float:
        raise AssertionError("evaluate must not be called")


class FakePolicyRouter:
    def __init__(self) -> None:
        self.calls = 0

    async def route(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
    ) -> RouteDecision:
        self.calls += 1
        return RouteDecision(
            tool="b",
            confidence=0.9,
            probabilities={"a": 0.1, "b": 0.9},
        )


class FallbackPolicyRouter(FakePolicyRouter):
    async def route(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
    ) -> RouteDecision:
        self.calls += 1
        return RouteDecision.fallback_to_planner(
            "planner_confirmation",
            confidence=0.7,
            probabilities={"a": 0.01, "b": 0.29, "__fallback__": 0.70},
        )


@pytest.mark.asyncio
async def test_mcts_prefers_better_long_horizon_sequence() -> None:
    router = MCTSToolRouter(
        LongHorizonEnvironment(),
        config=MCTSConfig(
            simulations=64,
            max_depth=2,
            max_policy_evaluations=0,
        ),
    )

    result = await router.search(HarnessState(goal="root"))

    assert result.decision.tool == "invest"
    assert result.principal_variation[:2] == ("invest", "finish")
    assert result.root_values["invest"] > result.root_values["quick"]
    assert result.simulations == 64
    assert result.policy_evaluations == 0


@pytest.mark.asyncio
async def test_mcts_uses_jev_as_bounded_root_prior() -> None:
    policy = FakePolicyRouter()
    router = MCTSToolRouter(
        FlatEnvironment(),
        policy_router=policy,  # type: ignore[arg-type]
        config=MCTSConfig(
            simulations=24,
            max_depth=1,
            max_policy_evaluations=1,
        ),
    )

    result = await router.search(HarnessState(goal="root"))

    assert result.decision.tool == "b"
    assert result.root_visits["b"] > result.root_visits["a"]
    assert result.policy_evaluations == 1
    assert policy.calls == 1


@pytest.mark.asyncio
async def test_mcts_uses_neutral_prior_when_policy_falls_back() -> None:
    policy = FallbackPolicyRouter()
    router = MCTSToolRouter(
        FlatEnvironment(),
        policy_router=policy,  # type: ignore[arg-type]
        config=MCTSConfig(
            simulations=24,
            max_depth=1,
            max_policy_evaluations=1,
        ),
    )

    result = await router.search(HarnessState(goal="root"))

    assert abs(result.root_visits["a"] - result.root_visits["b"]) <= 1
    assert result.policy_evaluations == 1
    assert policy.calls == 1


@pytest.mark.asyncio
async def test_mcts_route_returns_plain_route_decision() -> None:
    router = MCTSToolRouter(
        LongHorizonEnvironment(),
        config=MCTSConfig(simulations=32, max_depth=2, max_policy_evaluations=0),
    )
    tools = [tool("quick"), tool("invest")]

    decision = await router.route(HarnessState(goal="root"), tools)

    assert decision.tool == "invest"
    assert decision.fallback is False


@pytest.mark.asyncio
async def test_mcts_falls_back_when_no_tools_exist() -> None:
    router = MCTSToolRouter(EmptyEnvironment())

    result = await router.search(HarnessState(goal="root"))

    assert result.decision.fallback is True
    assert result.decision.fallback_reason == "mcts_no_tools"
    assert result.simulations == 0


def test_mcts_config_rejects_unbounded_values() -> None:
    with pytest.raises(ValueError):
        MCTSConfig(simulations=0)
    with pytest.raises(ValueError):
        MCTSConfig(max_depth=0)
    with pytest.raises(ValueError):
        MCTSConfig(max_policy_evaluations=-1)
