from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict

from .config import OpenRouterConfig, RoutingConfig
from .errors import RouterConfigurationError
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
    router: JevToolRouter | None = None
    provider: OpenRouterJevProvider | None = None
    init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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


async def _get_router(context: ServerContext) -> JevToolRouter:
    if context.router is not None:
        return context.router

    async with context.init_lock:
        if context.router is not None:
            return context.router

        provider = OpenRouterJevProvider.from_config(
            OpenRouterConfig(timeout_seconds=2.0)
        )
        context.provider = provider
        context.router = _fast_router(provider)
        return context.router


def create_mcp_server(*, router: JevToolRouter | None = None) -> MCPServer[ServerContext]:
    """Create the native harness-router MCP server.

    Provider initialization is lazy so MCP initialization and tools/list do not
    depend on OPENROUTER_API_KEY being present in the host environment. The key
    is required only when route is actually called.
    """

    @asynccontextmanager
    async def lifespan(_: MCPServer[ServerContext]) -> AsyncIterator[ServerContext]:
        context = ServerContext(router=router)
        try:
            yield context
        finally:
            if context.provider is not None:
                await context.provider.aclose()

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

        try:
            active_router = await _get_router(ctx.request_context.lifespan_context)
        except RouterConfigurationError:
            return RouteResult(
                tool=None,
                confidence=0.0,
                fallback=True,
                reason="missing_openrouter_api_key",
            )

        descriptors = [
            ToolDescriptor(
                name=item.name,
                description=item.description,
                category=item.category,
                risk=RiskLevel(item.risk),
            )
            for item in tools
        ]

        decision = await active_router.route(
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
