from __future__ import annotations

from typing import Any, Mapping, Sequence

from .config import RoutingConfig
from .loop_guard import LoopGuard
from .models import HarnessState, RouteDecision, ToolDescriptor
from .router import JevToolRouter


class RoutingSession:
    """Optional stateful guard around a stateless router for long-running harness loops."""

    def __init__(self, router: JevToolRouter, config: RoutingConfig | None = None) -> None:
        self._router = router
        self._config = config or RoutingConfig()
        self._steps = 0
        self._consecutive_fallbacks = 0
        self._loop_guard = LoopGuard(
            max_same_action_repeats=self._config.max_same_action_repeats
        )

    async def route(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
    ) -> RouteDecision:
        if self._steps >= self._config.max_route_steps:
            return RouteDecision.fallback_to_planner("max_route_steps")
        if self._consecutive_fallbacks >= self._config.max_consecutive_fallbacks:
            return RouteDecision.fallback_to_planner("fallback_circuit_open")

        self._steps += 1
        decision = await self._router.route(state, tools)
        if decision.fallback:
            self._consecutive_fallbacks += 1
        else:
            self._consecutive_fallbacks = 0
        return decision

    def reset_fallbacks(self) -> None:
        """Re-enable Jev after the planner materially changes the routing state."""
        self._consecutive_fallbacks = 0

    def record_execution(
        self,
        tool: str,
        *,
        arguments: Mapping[str, Any] | None = None,
        result_class: str | None = None,
    ) -> bool:
        """Return True when the recorded action closes a repeated or A/B loop."""
        return self._loop_guard.record(
            tool,
            arguments=arguments,
            result_class=result_class,
        )

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def consecutive_fallbacks(self) -> int:
        return self._consecutive_fallbacks
