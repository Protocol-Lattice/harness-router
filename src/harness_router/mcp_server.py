from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from pydantic import BaseModel, ConfigDict, Field

from .config import OpenRouterConfig, RoutingConfig
from .errors import RouterConfigurationError
from .mcts import MCTSConfig, MCTSToolRouter, SimulatedStep
from .models import HarnessState, RiskLevel, RoutingMode, ToolDescriptor
from .provider import OpenRouterJevProvider
from .router import JevToolRouter


class RouteCandidate(BaseModel):
    """Compact candidate descriptor accepted by the MCP routing tools."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    category: str | None = None
    risk: Literal["low", "medium", "high", "critical"] = "medium"


class RouteResult(BaseModel):
    """Token-light structured response returned by the fast route tool."""

    tool: str | None
    confidence: float
    fallback: bool
    reason: str | None = None


class MCTSState(BaseModel):
    """One side-effect-free simulated state supplied by the MCP host."""

    model_config = ConfigDict(extra="forbid")

    id: str
    goal: str
    observation: str | None = None
    last_action: str | None = None
    value: float = 0.0
    tools: list[RouteCandidate] = Field(default_factory=list)


class MCTSTransition(BaseModel):
    """A predicted transition; it never executes a real tool."""

    model_config = ConfigDict(extra="forbid")

    from_state: str
    tool: str
    to_state: str
    reward: float = 0.0
    terminal: bool = False


class MCTSRouteResult(BaseModel):
    """Compact MCTS result. Confidence is root visit share, not Jev confidence."""

    tool: str | None
    confidence: float
    fallback: bool
    reason: str | None = None
    principal_variation: list[str] = Field(default_factory=list)
    simulations: int = 0
    policy_evaluations: int = 0


@dataclass(slots=True)
class ServerContext:
    router: JevToolRouter | None = None
    provider: OpenRouterJevProvider | None = None
    init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _descriptor(item: RouteCandidate) -> ToolDescriptor:
    return ToolDescriptor(
        name=item.name,
        description=item.description,
        category=item.category,
        risk=RiskLevel(item.risk),
    )


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


class _GraphEnvironment:
    """In-memory, side-effect-free search environment for MCP MCTS."""

    def __init__(
        self,
        states: Sequence[MCTSState],
        transitions: Sequence[MCTSTransition],
    ) -> None:
        if not states:
            raise ValueError("states must not be empty")
        if len(states) > 256:
            raise ValueError("states must contain at most 256 entries")
        if len(transitions) > 1024:
            raise ValueError("transitions must contain at most 1024 entries")

        self._specs: dict[str, MCTSState] = {}
        self._states: dict[str, HarnessState] = {}
        self._state_ids: dict[int, str] = {}
        for spec in states:
            if not spec.id.strip():
                raise ValueError("state ids must be non-empty")
            if spec.id in self._specs:
                raise ValueError(f"duplicate state id: {spec.id}")
            state = HarnessState(
                goal=spec.goal,
                observation=spec.observation,
                last_action=spec.last_action,
            )
            self._specs[spec.id] = spec
            self._states[spec.id] = state
            self._state_ids[id(state)] = spec.id

        self._transitions: dict[tuple[str, str], MCTSTransition] = {}
        for edge in transitions:
            if edge.from_state not in self._states:
                raise ValueError(f"unknown from_state: {edge.from_state}")
            if edge.to_state not in self._states:
                raise ValueError(f"unknown to_state: {edge.to_state}")
            key = (edge.from_state, edge.tool)
            if key in self._transitions:
                raise ValueError(
                    f"duplicate transition for state/tool: {edge.from_state}/{edge.tool}"
                )
            self._transitions[key] = edge

    def state(self, state_id: str) -> HarnessState:
        try:
            return self._states[state_id]
        except KeyError:
            raise ValueError(f"unknown root_state: {state_id}") from None

    def _id_for(self, state: HarnessState) -> str:
        try:
            return self._state_ids[id(state)]
        except KeyError:
            raise ValueError("MCTS received a state outside the supplied graph") from None

    async def tools(self, state: HarnessState) -> Sequence[ToolDescriptor]:
        state_id = self._id_for(state)
        return [_descriptor(item) for item in self._specs[state_id].tools]

    async def transition(
        self,
        state: HarnessState,
        tool: ToolDescriptor,
    ) -> SimulatedStep:
        state_id = self._id_for(state)
        edge = self._transitions.get((state_id, tool.name))
        if edge is None:
            return SimulatedStep(state, reward=-1.0, terminal=True)
        return SimulatedStep(
            self._states[edge.to_state],
            reward=edge.reward,
            terminal=edge.terminal,
        )

    async def evaluate(self, state: HarnessState) -> float:
        state_id = self._id_for(state)
        return self._specs[state_id].value


def create_mcp_server(*, router: JevToolRouter | None = None) -> MCPServer[ServerContext]:
    """Create the native harness-router MCP server.

    Provider initialization is lazy so MCP initialization and tools/list do not
    depend on OPENROUTER_API_KEY being present in the host environment. The key
    is required only when the fast route tool or an MCTS Jev prior is requested.
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
        description="Low-token Jev routing plus bounded side-effect-free MCTS lookahead.",
        instructions=(
            "Use route for ordinary ambiguous closed choices. Use route_mcts only when "
            "multi-step consequences matter and you can provide a side-effect-free simulated graph. "
            "Never simulate real writes, shell commands, browser mutations, or network mutations."
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

        decision = await active_router.route(
            HarnessState(
                goal=goal,
                observation=observation,
                last_action=last_action,
            ),
            [_descriptor(item) for item in tools],
        )

        return RouteResult(
            tool=decision.tool,
            confidence=round(decision.confidence, 4),
            fallback=decision.fallback,
            reason=decision.fallback_reason,
        )

    @server.tool()
    async def route_mcts(
        ctx: Context[ServerContext],
        root_state: str,
        states: list[MCTSState],
        transitions: list[MCTSTransition],
        simulations: int = 32,
        max_depth: int = 3,
        use_jev_prior: bool = True,
    ) -> MCTSRouteResult:
        """Search a supplied side-effect-free state graph and select only the first tool."""

        if simulations < 1 or simulations > 256:
            raise ValueError("simulations must be between 1 and 256")
        if max_depth < 1 or max_depth > 8:
            raise ValueError("max_depth must be between 1 and 8")

        environment = _GraphEnvironment(states, transitions)
        root = environment.state(root_state)

        policy_router: JevToolRouter | None = None
        if use_jev_prior:
            try:
                policy_router = await _get_router(ctx.request_context.lifespan_context)
            except RouterConfigurationError:
                # MCTS remains useful with a uniform prior when no API key is configured.
                policy_router = None

        mcts = MCTSToolRouter(
            environment,
            policy_router=policy_router,
            config=MCTSConfig(
                simulations=simulations,
                max_depth=max_depth,
                max_policy_evaluations=1 if policy_router is not None else 0,
            ),
        )
        result = await mcts.search(root)

        return MCTSRouteResult(
            tool=result.decision.tool,
            confidence=round(result.decision.confidence, 4),
            fallback=result.decision.fallback,
            reason=result.decision.fallback_reason,
            principal_variation=list(result.principal_variation),
            simulations=result.simulations,
            policy_evaluations=result.policy_evaluations,
        )

    return server


def main() -> None:
    """Run the MCP server over stdio."""

    create_mcp_server().run()
