import pytest

from harness_router import (
    ChoiceDecision,
    HarnessRunner,
    HarnessToolCall,
    HarnessToolResult,
    JevToolRouter,
    RiskLevel,
    RoutingConfig,
    RoutingSession,
    ToolDescriptor,
)
from harness_router.harness import UnexpectedToolSelection


def tool(name: str) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description=f"{name} capability",
        category="inspect",
        risk=RiskLevel.LOW,
    )


class FakeProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def choose(self, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


class FakeAdapter:
    name = "fake"

    def __init__(self, calls):
        self.calls = list(calls)
        self.selected = []
        self.executed = []
        self.closed = False

    async def get_tools(self):
        return [tool("read_file"), tool("search_code")]

    async def generate_tool_call(self, state, selected):
        self.selected.append(selected.name)
        return self.calls.pop(0)

    async def execute_tool(self, call, selected):
        self.executed.append((call.tool, selected.name))
        return HarnessToolResult(
            observation=f"completed {call.tool}",
            raw={"tool": call.tool},
            done=not self.calls,
        )

    async def planner_fallback(self, state, tools):
        return {"fallback": True}

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_runner_routes_before_each_model_step():
    provider = FakeProvider(
        ChoiceDecision("read_file", {"read_file": 0.97, "search_code": 0.02, "__fallback__": 0.01}, 0.97),
        ChoiceDecision("search_code", {"read_file": 0.01, "search_code": 0.97, "__fallback__": 0.02}, 0.97),
    )
    session = RoutingSession(
        JevToolRouter(
            provider,
            RoutingConfig(route_cache_size=0, obvious_cache_size=0),
        )
    )
    adapter = FakeAdapter([
        HarnessToolCall("read_file", {"path": "a.py"}),
        HarnessToolCall("search_code", {"query": "parser"}),
    ])

    result = await HarnessRunner(session, adapter).run("fix parser")

    assert result == {"tool": "search_code"}
    assert adapter.selected == ["read_file", "search_code"]
    assert adapter.executed == [
        ("read_file", "read_file"),
        ("search_code", "search_code"),
    ]
    assert adapter.closed is True


@pytest.mark.asyncio
async def test_runner_rejects_model_tool_different_from_router():
    provider = FakeProvider(
        ChoiceDecision("read_file", {"read_file": 0.97, "search_code": 0.02, "__fallback__": 0.01}, 0.97),
    )
    session = RoutingSession(JevToolRouter(provider))
    adapter = FakeAdapter([HarnessToolCall("search_code")])

    with pytest.raises(UnexpectedToolSelection):
        await HarnessRunner(session, adapter).run("inspect parser")

    assert adapter.closed is True


@pytest.mark.asyncio
async def test_runner_falls_back_without_tool_choice():
    provider = FakeProvider(
        ChoiceDecision("__fallback__", {"__fallback__": 0.80, "read_file": 0.20}, 0.80),
    )
    session = RoutingSession(JevToolRouter(provider))
    adapter = FakeAdapter([])

    result = await HarnessRunner(session, adapter).run("complex task")

    assert result == {"fallback": True}
    assert adapter.closed is True
