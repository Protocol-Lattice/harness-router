from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from collections.abc import Sequence

from .adapters import infer_category
from .config import RoutingConfig
from .errors import ProviderError, RouterError, UnknownToolError
from .models import HarnessState, RouteDecision, RoutingMode, ToolDescriptor
from .provider import DecisionProvider

_FALLBACK = "__fallback__"
logger = logging.getLogger(__name__)


class JevToolRouter:
    """Fast, provider-agnostic tool router using Jev as a System-1 decision layer."""

    def __init__(self, provider: DecisionProvider | None, config: RoutingConfig | None = None) -> None:
        self._provider = provider
        self._config = config or RoutingConfig()
        if self._config.mode is not RoutingMode.PLANNER_ONLY and provider is None:
            raise ValueError("provider is required unless routing mode is planner_only")

    async def route(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
    ) -> RouteDecision:
        if self._config.mode is RoutingMode.PLANNER_ONLY:
            return RouteDecision.fallback_to_planner("planner_only")
        if not tools:
            return RouteDecision.fallback_to_planner("no_tools")

        self._validate_tools(tools)
        started = time.perf_counter()
        try:
            if len(tools) > self._config.hierarchical_threshold:
                decision = await self._route_hierarchical(state, tools)
            else:
                decision = await self._choose_tool(state, tools)
            decision = self._apply_confidence_policy(decision)
        except ProviderError as exc:
            if self._config.mode is RoutingMode.JEV_ONLY:
                raise
            logger.warning("Jev provider error; falling back to planner", exc_info=exc)
            decision = RouteDecision.fallback_to_planner("provider_error")
        except RouterError:
            if self._config.mode is RoutingMode.JEV_ONLY:
                raise
            decision = RouteDecision.fallback_to_planner("router_error")

        logger.debug(
            "jev_route tool=%s category=%s confidence=%.4f fallback=%s reason=%s latency_ms=%.2f",
            decision.tool,
            decision.category,
            decision.confidence,
            decision.fallback,
            decision.fallback_reason,
            (time.perf_counter() - started) * 1000,
        )
        return decision

    async def _route_hierarchical(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
    ) -> RouteDecision:
        groups: dict[str, list[ToolDescriptor]] = defaultdict(list)
        for tool in tools:
            category = (tool.category or infer_category(tool.name, tool.description)).strip().lower()
            groups[category].append(tool)

        if len(groups) <= 1:
            return await self._choose_tool(state, tools)

        assert self._provider is not None
        category_criteria = {
            category: self._category_description(category, members)
            for category, members in sorted(groups.items())
        }
        category_criteria[_FALLBACK] = "No available category is appropriate; use the reasoning planner."

        category_choice = await self._provider.choose(
            state=self._state_text(state),
            instructions=(
                "Choose the single tool category that best advances the user's goal from the current "
                "observation. Prefer the smallest useful next action. Choose __fallback__ when none fit."
            ),
            criteria=category_criteria,
        )
        if category_choice.choice == _FALLBACK:
            return RouteDecision.fallback_to_planner(
                "no_matching_category",
                confidence=category_choice.confidence,
                probabilities=category_choice.probabilities,
            )

        category = category_choice.choice
        members = groups.get(category)
        if not members:
            raise UnknownToolError(f"Jev selected unknown category: {category}")

        if category_choice.confidence < self._config.fallback_threshold:
            return RouteDecision.fallback_to_planner(
                "low_category_confidence",
                confidence=category_choice.confidence,
                category=category,
                probabilities=category_choice.probabilities,
            )

        tool_decision = await self._choose_tool(state, members, category=category)
        if tool_decision.fallback:
            return tool_decision
        return RouteDecision(
            tool=tool_decision.tool,
            category=category,
            confidence=min(category_choice.confidence, tool_decision.confidence),
            probabilities=tool_decision.probabilities,
        )

    async def _choose_tool(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
        *,
        category: str | None = None,
    ) -> RouteDecision:
        assert self._provider is not None
        criteria = {tool.name: self._tool_description(tool) for tool in tools}
        criteria[_FALLBACK] = "No available tool is appropriate; use the reasoning planner instead."

        choice = await self._provider.choose(
            state=self._state_text(state),
            instructions=(
                "Choose the single best next tool for the current goal and observation. Choose only a tool "
                "that can make concrete progress. Do not infer permission for risky actions. Choose "
                "__fallback__ when reasoning or a different capability is required."
            ),
            criteria=criteria,
        )
        if choice.choice == _FALLBACK:
            return RouteDecision.fallback_to_planner(
                "no_matching_tool",
                confidence=choice.confidence,
                category=category,
                probabilities=choice.probabilities,
            )

        selected = next((tool for tool in tools if tool.name == choice.choice), None)
        if selected is None:
            raise UnknownToolError(f"Jev selected unknown tool: {choice.choice}")

        return RouteDecision(
            tool=selected.name,
            category=category or selected.category,
            confidence=choice.confidence,
            probabilities=choice.probabilities,
        )

    def _apply_confidence_policy(self, decision: RouteDecision) -> RouteDecision:
        if decision.fallback:
            return decision
        if decision.confidence < self._config.fallback_threshold:
            return RouteDecision.fallback_to_planner(
                "low_confidence",
                confidence=decision.confidence,
                category=decision.category,
                probabilities=decision.probabilities,
            )
        if (
            self._config.mode is RoutingMode.HYBRID
            and decision.confidence < self._config.direct_execution_threshold
        ):
            return RouteDecision.fallback_to_planner(
                "planner_confirmation",
                confidence=decision.confidence,
                category=decision.category,
                probabilities=decision.probabilities,
            )
        return decision

    def _state_text(self, state: HarnessState) -> str:
        return json.dumps(state.compact(), ensure_ascii=False, separators=(",", ":"), default=str)

    def _tool_description(self, tool: ToolDescriptor) -> str:
        description = " ".join(tool.description.split())[: self._config.description_limit]
        category = tool.category or infer_category(tool.name, tool.description)
        return f"Category: {category}. Risk: {tool.risk.value}. Capability: {description}"

    def _category_description(self, category: str, tools: Sequence[ToolDescriptor]) -> str:
        names = ", ".join(tool.name for tool in tools[:8])
        suffix = "..." if len(tools) > 8 else ""
        return f"Use this category for {category} actions. Available tools: {names}{suffix}"

    @staticmethod
    def _validate_tools(tools: Sequence[ToolDescriptor]) -> None:
        names = [tool.name for tool in tools]
        if any(not name.strip() for name in names):
            raise ValueError("tool names must be non-empty")
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        if _FALLBACK in names:
            raise ValueError(f"tool name {_FALLBACK!r} is reserved")
