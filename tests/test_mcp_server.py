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
