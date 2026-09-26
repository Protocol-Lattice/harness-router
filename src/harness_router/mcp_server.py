from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict

from .config import OpenRouterConfig, RoutingConfig
from .models import HarnessState, RiskLevel, RoutingMode, ToolDescriptor
from .provider import OpenRouterJevProvider
from .router import JevToolRouter


class RouteCandidate(BaseModel):
    """Compact candidate descriptor accepted by the MCP route tool."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    category: str | None = None
    risk: Literal["low", "medium", "high", "critical"] = "medium"


class RouteResult(BaseModel):
    """Token-light structured response returned to the MCP host."""

    tool: str | None
    confidence: float
    fallback: bool
    reason: str | None = None


@dataclass(slots=True)
class ServerContext:
    router: JevToolRouter
    provider: OpenRouterJevProvider | None = None


def _fast_router(provider: OpenRouterJevProvider) -> JevToolRouter:
    return JevToolRouter(
        provider,
        RoutingConfig(
            mode=RoutingMode.HYBRID,
            direct_execution_threshold=0.72,
            fallback_threshold=0.72,
            hierarchical_threshold=48,
            description_limit=96,
            history_limit=1,
            state_field_limit=400,
            constraint_limit=2,
        ),
    )


def create_mcp_server(*, router: JevToolRouter | None = None) -> MCPServer[ServerContext]:
    """Create the native harness-router MCP server.

    Passing a router is primarily useful for embedding and tests. When omitted,
    one OpenRouter provider is created for the whole server lifetime so HTTP
    connections and the route cache are reused across calls.
    """

    @asynccontextmanager
    async def lifespan(_: MCPServer[ServerContext]) -> AsyncIterator[ServerContext]:
        if router is not None:
            yield ServerContext(router=router)
            return

        provider = OpenRouterJevProvider.from_config(
            OpenRouterConfig(timeout_seconds=2.0)
        )
        try:
            yield ServerContext(router=_fast_router(provider), provider=provider)
        finally:
            await provider.aclose()

    server: MCPServer[ServerContext] = MCPServer(
        "harness-router",
        description="Low-token Jev tool routing for ambiguous closed choices.",
        instructions=(
            "Call route only when several next tools are genuinely plausible. "
            "Skip it for obvious linear tool steps."
        ),
        lifespan=lifespan,
    )

    @server.tool()
    async def route(
        ctx: Context[ServerContext],
        goal: str,
        tools: list[RouteCandidate],
        observation: str | None = None,
        last_action: str | None = None,
    ) -> RouteResult:
        """Choose one next tool from a compact candidate set."""

        descriptors = [
            ToolDescriptor(
                name=item.name,
                description=item.description,
                category=item.category,
                risk=RiskLevel(item.risk),
            )
            for item in tools
        ]

        decision = await ctx.request_context.lifespan_context.router.route(
            HarnessState(
                goal=goal,
                observation=observation,
                last_action=last_action,
            ),
            descriptors,
        )

        return RouteResult(
            tool=decision.tool,
            confidence=round(decision.confidence, 4),
            fallback=decision.fallback,
            reason=decision.fallback_reason,
        )

    return server


def main() -> None:
    """Run the MCP server over stdio."""

    create_mcp_server().run()
