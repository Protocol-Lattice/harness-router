from __future__ import annotations

import pytest

mcp = pytest.importorskip("mcp")

from mcp import Client

from harness_router import ChoiceDecision, JevToolRouter, RoutingConfig
from harness_router.mcp_server import create_mcp_server


class FakeProvider:
    def __init__(self, choice: str = "read_file", confidence: float = 0.93) -> None:
        self.choice = choice
        self.confidence = confidence

    async def choose(self, *, state, instructions, criteria):
        del state, instructions
        probabilities = {name: 0.0 for name in criteria}
        probabilities[self.choice] = self.confidence
        return ChoiceDecision(
            choice=self.choice,
            probabilities=probabilities,
            confidence=self.confidence,
        )


@pytest.mark.asyncio
async def test_mcp_route_returns_compact_structured_result() -> None:
    router = JevToolRouter(
        FakeProvider(),
        RoutingConfig(
            direct_execution_threshold=0.72,
            fallback_threshold=0.72,
        ),
    )
    server = create_mcp_server(router=router)

    async with Client(server) as client:
        result = await client.call_tool(
            "route",
            {
                "goal": "Fix the parser test",
                "observation": "Failure points to src/parser.py",
                "tools": [
                    {
                        "name": "read_file",
                        "description": "Read source",
                        "category": "inspect",
                        "risk": "low",
                    },
                    {
                        "name": "search_code",
                        "description": "Search repository",
                        "category": "inspect",
                        "risk": "low",
                    },
                ],
            },
        )

    assert result.is_error is False
    assert result.structured_content == {
        "tool": "read_file",
        "confidence": 0.93,
        "fallback": False,
        "reason": None,
    }


@pytest.mark.asyncio
async def test_mcp_route_preserves_fallback() -> None:
    router = JevToolRouter(
        FakeProvider(confidence=0.4),
        RoutingConfig(
            direct_execution_threshold=0.72,
            fallback_threshold=0.72,
        ),
    )
    server = create_mcp_server(router=router)

    async with Client(server) as client:
        result = await client.call_tool(
            "route",
            {
                "goal": "Choose the next action",
                "tools": [
                    {"name": "read_file", "risk": "low"},
                    {"name": "search_code", "risk": "low"},
                ],
            },
        )

    assert result.is_error is False
    assert result.structured_content["tool"] is None
    assert result.structured_content["fallback"] is True
    assert result.structured_content["reason"] == "low_confidence"


@pytest.mark.asyncio
async def test_mcp_lists_route_without_openrouter_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    server = create_mcp_server()

    async with Client(server) as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools.tools] == ["route", "route_mcts"]


@pytest.mark.asyncio
async def test_mcp_missing_key_falls_back_on_route(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    server = create_mcp_server()

    async with Client(server) as client:
        result = await client.call_tool(
            "route",
            {
                "goal": "Choose the next action",
                "tools": [
                    {"name": "read_file", "risk": "low"},
                    {"name": "search_code", "risk": "low"},
                ],
            },
        )

    assert result.is_error is False
    assert result.structured_content == {
        "tool": None,
        "confidence": 0.0,
        "fallback": True,
        "reason": "missing_openrouter_api_key",
    }


@pytest.mark.asyncio
async def test_mcp_mcts_prefers_long_horizon_action(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    server = create_mcp_server()

    async with Client(server) as client:
        result = await client.call_tool(
            "route_mcts",
            {
                "root_state": "root",
                "simulations": 64,
                "max_depth": 2,
                "use_jev_prior": False,
                "states": [
                    {
                        "id": "root",
                        "goal": "Choose",
                        "tools": [
                            {"name": "quick", "risk": "low"},
                            {"name": "invest", "risk": "low"},
                        ],
                    },
                    {
                        "id": "quick_done",
                        "goal": "Quick done",
                        "tools": [],
                    },
                    {
                        "id": "invested",
                        "goal": "Invested",
                        "tools": [{"name": "finish", "risk": "low"}],
                    },
                    {
                        "id": "finished",
                        "goal": "Finished",
                        "tools": [],
                    },
                ],
                "transitions": [
                    {
                        "from_state": "root",
                        "tool": "quick",
                        "to_state": "quick_done",
                        "reward": 0.6,
                        "terminal": True,
                    },
                    {
                        "from_state": "root",
                        "tool": "invest",
                        "to_state": "invested",
                        "reward": 0.05,
                    },
                    {
                        "from_state": "invested",
                        "tool": "finish",
                        "to_state": "finished",
                        "reward": 1.0,
                        "terminal": True,
                    },
                ],
            },
        )

    assert result.is_error is False
    assert result.structured_content["tool"] == "invest"
    assert result.structured_content["fallback"] is False
    assert result.structured_content["principal_variation"][:2] == ["invest", "finish"]
    assert result.structured_content["simulations"] == 64
    assert result.structured_content["policy_evaluations"] == 0


@pytest.mark.asyncio
async def test_mcp_mcts_uses_single_jev_prior() -> None:
    router = JevToolRouter(
        FakeProvider(choice="b", confidence=0.9),
        RoutingConfig(
            direct_execution_threshold=0.72,
            fallback_threshold=0.72,
        ),
    )
    server = create_mcp_server(router=router)

    async with Client(server) as client:
        result = await client.call_tool(
            "route_mcts",
            {
                "root_state": "root",
                "simulations": 24,
                "max_depth": 1,
                "states": [
                    {
                        "id": "root",
                        "goal": "Choose",
                        "tools": [
                            {"name": "a", "risk": "low"},
                            {"name": "b", "risk": "low"},
                        ],
                    },
                    {"id": "done_a", "goal": "Done A", "tools": []},
                    {"id": "done_b", "goal": "Done B", "tools": []},
                ],
                "transitions": [
                    {
                        "from_state": "root",
                        "tool": "a",
                        "to_state": "done_a",
                        "terminal": True,
                    },
                    {
                        "from_state": "root",
                        "tool": "b",
                        "to_state": "done_b",
                        "terminal": True,
                    },
                ],
            },
        )

    assert result.is_error is False
    assert result.structured_content["tool"] == "b"
    assert result.structured_content["policy_evaluations"] == 1
