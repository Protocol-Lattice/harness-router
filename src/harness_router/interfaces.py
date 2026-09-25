from __future__ import annotations

from typing import Any, Protocol, Sequence

from .models import HarnessState, RouteDecision, ToolDescriptor


class ToolRegistry(Protocol):
    async def get_tools(self) -> Sequence[ToolDescriptor]: ...


class ToolExecutor(Protocol):
    async def execute(self, tool: ToolDescriptor, arguments: dict[str, Any]) -> Any: ...


class ExecutionPolicy(Protocol):
    async def allow(self, decision: RouteDecision, tool: ToolDescriptor) -> bool: ...


class PlannerFallback(Protocol):
    async def plan(self, state: HarnessState, tools: Sequence[ToolDescriptor]) -> Any: ...
