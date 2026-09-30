from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .models import ActionSummary, HarnessState, RouteDecision, ToolDescriptor
from .policy import DefaultExecutionPolicy
from .session import RoutingSession


@dataclass(frozen=True, slots=True)
class HarnessToolCall:
    """A model-produced call that must match the Router decision."""

    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HarnessToolResult:
    """Normalized tool execution result returned by a harness adapter."""

    observation: str
    raw: Any = None
    done: bool = False


class HarnessAdapter(Protocol):
    """Host-specific bridge for a Router-owned agent loop.

    Implementations must expose only the Router-selected tool to the model for
    the next argument-generation step, then execute that exact call under the
    host's existing permission and sandbox rules.
    """

    name: str

    async def get_tools(self) -> Sequence[ToolDescriptor]: ...

    async def generate_tool_call(
        self,
        state: HarnessState,
        tool: ToolDescriptor,
    ) -> HarnessToolCall: ...

    async def execute_tool(
        self,
        call: HarnessToolCall,
        tool: ToolDescriptor,
    ) -> HarnessToolResult: ...

    async def planner_fallback(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
    ) -> Any: ...

    async def close(self) -> None: ...


class HarnessAdapterError(RuntimeError):
    """Base error for the Router-owned harness loop."""


class UnexpectedToolSelection(HarnessAdapterError):
    """The host asked to execute a tool different from Router output."""


class HarnessRunner:
    """Own the state -> route -> model -> execute loop."""

    def __init__(
        self,
        routing: RoutingSession,
        adapter: HarnessAdapter,
        *,
        execution_policy: Any | None = None,
    ) -> None:
        self._routing = routing
        self._adapter = adapter
        self._execution_policy = execution_policy or DefaultExecutionPolicy()

    async def run(self, goal: str, *, constraints: Sequence[str] = ()) -> Any:
        state = HarnessState(
            goal=goal,
            constraints=[str(value) for value in constraints],
        )
        try:
            while True:
                tools = await self._adapter.get_tools()
                if not tools:
                    return await self._adapter.planner_fallback(state, tools)

                decision = await self._routing.route(state, tools)
                if decision.fallback:
                    return await self._adapter.planner_fallback(state, tools)

                tool = self._tool_for_decision(tools, decision)
                if not await self._execution_policy.allow(decision, tool):
                    return await self._adapter.planner_fallback(state, tools)

                call = await self._adapter.generate_tool_call(state, tool)
                if call.tool != decision.tool:
                    raise UnexpectedToolSelection(
                        f"{self._adapter.name} selected {call.tool!r}; "
                        f"Router selected {decision.tool!r}"
                    )

                result = await self._adapter.execute_tool(call, tool)
                state.last_action = call.tool
                state.observation = result.observation
                state.recent_actions.append(
                    ActionSummary(tool=call.tool, outcome=result.observation[:512])
                )
                if result.done:
                    return result.raw
        finally:
            await self._adapter.close()

    @staticmethod
    def _tool_for_decision(
        tools: Sequence[ToolDescriptor],
        decision: RouteDecision,
    ) -> ToolDescriptor:
        selected = decision.tool
        if not selected:
            raise UnexpectedToolSelection("Router returned an empty tool selection")
        for tool in tools:
            if tool.name == selected:
                return tool
        raise UnexpectedToolSelection(f"Router selected unavailable tool {selected!r}")
