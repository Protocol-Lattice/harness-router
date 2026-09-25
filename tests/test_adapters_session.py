import pytest

from harness_router import (
    ChoiceDecision,
    GenericToolAdapter,
    MCPToolAdapter,
    HarnessState,
    JevToolRouter,
    RiskLevel,
    RoutingConfig,
    RoutingSession,
    ToolDescriptor,
)


class Provider:
    async def choose(self, **kwargs):
        return ChoiceDecision("read_file", {"read_file": 0.99, "__fallback__": 0.01}, 0.99)


def test_generic_adapter_infers_category_and_risk() -> None:
    tool = GenericToolAdapter().normalize(
        {
            "name": "read_file",
            "description": "Read a local source file",
            "inputSchema": {"type": "object"},
        }
    )
    assert tool.category == "inspect"
    assert tool.risk is RiskLevel.LOW
    assert tool.schema["type"] == "object"


def test_mcp_adapter_normalizes_standard_input_schema() -> None:
    tool = MCPToolAdapter().normalize(
        {
            "name": "search_code",
            "description": "Search repository source code",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    )
    assert tool.name == "search_code"
    assert tool.category == "inspect"
    assert tool.risk is RiskLevel.LOW
    assert tool.schema["required"] == ["query"]


@pytest.mark.asyncio
async def test_session_enforces_max_steps() -> None:
    config = RoutingConfig(max_route_steps=1)
    router = JevToolRouter(Provider(), config)
    session = RoutingSession(router, config)
    tools = [ToolDescriptor("read_file", "Read", risk=RiskLevel.LOW)]
    first = await session.route(HarnessState(goal="inspect"), tools)
    second = await session.route(HarnessState(goal="inspect"), tools)
    assert first.tool == "read_file"
    assert second.fallback_reason == "max_route_steps"
