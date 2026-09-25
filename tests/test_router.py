import json
from collections import deque

import pytest

from harness_router import (
    ActionSummary,
    ChoiceDecision,
    HarnessState,
    JevToolRouter,
    ProviderError,
    RiskLevel,
    RoutingConfig,
    RoutingMode,
    ToolDescriptor,
)


class FakeProvider:
    def __init__(self, *responses: ChoiceDecision | Exception) -> None:
        self.responses = deque(responses)
        self.calls = []

    async def choose(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def tool(name: str, category: str = "inspect") -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description=f"{name} capability",
        category=category,
        risk=RiskLevel.LOW,
    )


@pytest.mark.asyncio
async def test_high_confidence_routes_directly() -> None:
    provider = FakeProvider(
        ChoiceDecision("read_file", {"read_file": 0.96, "__fallback__": 0.04}, 0.96)
    )
    router = JevToolRouter(provider)
    decision = await router.route(HarnessState(goal="inspect parser"), [tool("read_file")])
    assert decision.tool == "read_file"
    assert decision.fallback is False


@pytest.mark.asyncio
async def test_medium_confidence_requests_planner_confirmation_in_hybrid() -> None:
    provider = FakeProvider(
        ChoiceDecision("read_file", {"read_file": 0.75, "__fallback__": 0.25}, 0.75)
    )
    router = JevToolRouter(provider)
    decision = await router.route(HarnessState(goal="inspect parser"), [tool("read_file")])
    assert decision.fallback is True
    assert decision.fallback_reason == "planner_confirmation"


@pytest.mark.asyncio
async def test_low_confidence_falls_back() -> None:
    provider = FakeProvider(
        ChoiceDecision("read_file", {"read_file": 0.55, "__fallback__": 0.45}, 0.55)
    )
    router = JevToolRouter(provider)
    decision = await router.route(HarnessState(goal="inspect parser"), [tool("read_file")])
    assert decision.fallback is True
    assert decision.fallback_reason == "low_confidence"


@pytest.mark.asyncio
async def test_provider_error_falls_back_in_hybrid() -> None:
    router = JevToolRouter(FakeProvider(ProviderError("boom")))
    decision = await router.route(HarnessState(goal="inspect"), [tool("read_file")])
    assert decision.fallback is True
    assert decision.fallback_reason == "provider_error"


@pytest.mark.asyncio
async def test_provider_error_propagates_in_jev_only() -> None:
    router = JevToolRouter(
        FakeProvider(ProviderError("boom")),
        RoutingConfig(mode=RoutingMode.JEV_ONLY),
    )
    with pytest.raises(ProviderError):
        await router.route(HarnessState(goal="inspect"), [tool("read_file")])


@pytest.mark.asyncio
async def test_planner_only_does_not_call_provider() -> None:
    router = JevToolRouter(None, RoutingConfig(mode=RoutingMode.PLANNER_ONLY))
    decision = await router.route(HarnessState(goal="inspect"), [tool("read_file")])
    assert decision.fallback_reason == "planner_only"


@pytest.mark.asyncio
async def test_hierarchical_routing() -> None:
    tools = [
        tool("read_1", "inspect"),
        tool("read_2", "inspect"),
        tool("write_1", "mutate"),
    ]
    provider = FakeProvider(
        ChoiceDecision("inspect", {"inspect": 0.94, "mutate": 0.05, "__fallback__": 0.01}, 0.94),
        ChoiceDecision("read_2", {"read_1": 0.1, "read_2": 0.88, "__fallback__": 0.02}, 0.91),
    )
    router = JevToolRouter(provider, RoutingConfig(hierarchical_threshold=2))
    decision = await router.route(HarnessState(goal="inspect"), tools)
    assert decision.tool == "read_2"
    assert decision.category == "inspect"
    assert decision.confidence == 0.91
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_router_compacts_provider_payload() -> None:
    provider = FakeProvider(
        ChoiceDecision("read_file", {"read_file": 0.96, "__fallback__": 0.04}, 0.96)
    )
    router = JevToolRouter(
        provider,
        RoutingConfig(
            description_limit=32,
            history_limit=1,
            state_field_limit=64,
            constraint_limit=1,
        ),
    )
    state = HarnessState(
        goal="g" * 100,
        observation="o" * 100,
        recent_actions=[
            ActionSummary("search_code", "x" * 100),
            ActionSummary("read_file", "y" * 100),
        ],
        constraints=["c" * 100, "ignored"],
    )
    verbose_tool = ToolDescriptor(
        name="read_file",
        description="very long capability " * 20,
        category="inspect",
        risk=RiskLevel.LOW,
    )

    await router.route(state, [verbose_tool])

    call = provider.calls[0]
    payload = json.loads(call["state"])
    assert len(payload["goal"]) == 64
    assert len(payload["observation"]) == 64
    assert len(payload["recent_actions"]) == 1
    assert len(payload["recent_actions"][0]["outcome"]) == 64
    assert len(payload["constraints"]) == 1
    assert len(payload["constraints"][0]) == 64
    assert "very long capability" in call["criteria"]["read_file"]
    assert len(call["criteria"]["read_file"]) < 80


@pytest.mark.asyncio
async def test_router_accepts_jev_fallback_choice() -> None:
    provider = FakeProvider(
        ChoiceDecision("__fallback__", {"read_file": 0.2, "__fallback__": 0.8}, 0.8)
    )
    router = JevToolRouter(provider)
    decision = await router.route(HarnessState(goal="compose complex patch"), [tool("read_file")])
    assert decision.fallback is True
    assert decision.fallback_reason == "no_matching_tool"
