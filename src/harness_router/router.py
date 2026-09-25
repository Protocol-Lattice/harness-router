from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict, defaultdict
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
        self._route_cache: OrderedDict[tuple[object, ...], RouteDecision] = OrderedDict()
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
        state_text = self._state_text(state)
        cache_key = self._cache_key(state_text, tools)
        cached = self._cache_get(cache_key)
        if cached is not None:
            logger.debug(
                "jev_route cache_hit tool=%s fallback=%s reason=%s",
                cached.tool,
                cached.fallback,
                cached.fallback_reason,
            )
            return cached

        started = time.perf_counter()
        try:
            if (
                len(tools) > self._config.hierarchical_threshold
                and self._should_route_hierarchical(state_text, tools)
            ):
                decision = await self._route_hierarchical(state_text, tools)
            else:
                decision = await self._choose_tool(state_text, tools)
            decision = self._apply_confidence_policy(decision)
            self._cache_put(cache_key, decision)
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
        state_text: str,
        tools: Sequence[ToolDescriptor],
    ) -> RouteDecision:
        groups: dict[str, list[ToolDescriptor]] = defaultdict(list)
        for tool in tools:
            category = (tool.category or infer_category(tool.name, tool.description)).strip().lower()
            groups[category].append(tool)

        if len(groups) <= 1:
            return await self._choose_tool(state_text, tools)

        assert self._provider is not None
        category_criteria = {
            category: self._category_description(category, members)
            for category, members in sorted(groups.items())
        }
        category_criteria[_FALLBACK] = "None fit; use planner."

        category_choice = await self._provider.choose(
            state=state_text,
            instructions="Choose the next tool category; use __fallback__ if none fit.",
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

        tool_decision = await self._choose_tool(state_text, members, category=category)
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
        state_text: str,
        tools: Sequence[ToolDescriptor],
        *,
        category: str | None = None,
    ) -> RouteDecision:
        assert self._provider is not None
        criteria = {tool.name: self._tool_description(tool) for tool in tools}
        criteria[_FALLBACK] = "None fit; use planner."

        choice = await self._provider.choose(
            state=state_text,
            instructions=(
                "Choose the next tool; use __fallback__ if none fit or reasoning is required."
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
        payload = state.compact(history_limit=self._config.history_limit)
        payload["goal"] = self._clip_text(payload["goal"], self._config.state_field_limit)
        payload["observation"] = self._clip_text(
            payload["observation"],
            self._config.state_field_limit,
        )
        payload["constraints"] = [
            self._clip_text(value, self._config.state_field_limit)
            for value in payload["constraints"][: self._config.constraint_limit]
        ]
        outcome_limit = max(64, self._config.state_field_limit // 2)
        for action in payload["recent_actions"]:
            action["outcome"] = self._clip_text(action["outcome"], outcome_limit)

        if payload["observation"] is None:
            payload.pop("observation")
        if payload["last_action"] is None:
            payload.pop("last_action")
        if not payload["recent_actions"]:
            payload.pop("recent_actions")
        if not payload["constraints"]:
            payload.pop("constraints")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    def _tool_description(self, tool: ToolDescriptor) -> str:
        description = " ".join(tool.description.split())[: self._config.description_limit]
        category = tool.category or infer_category(tool.name, tool.description)
        return f"{category}|r:{tool.risk.value[0]}|{description}"

    def _category_description(self, category: str, tools: Sequence[ToolDescriptor]) -> str:
        names = ",".join(tool.name for tool in tools[:6])
        suffix = ",..." if len(tools) > 6 else ""
        return f"{category}:{names}{suffix}"

    def _should_route_hierarchical(
        self,
        state_text: str,
        tools: Sequence[ToolDescriptor],
    ) -> bool:
        if not self._config.adaptive_hierarchy:
            return True

        groups: dict[str, list[ToolDescriptor]] = defaultdict(list)
        for tool in tools:
            category = (tool.category or infer_category(tool.name, tool.description)).strip().lower()
            groups[category].append(tool)
        if len(groups) <= 1:
            return False

        flat_chars = self._choice_payload_chars(state_text, tools)
        category_criteria = {
            category: self._category_description(category, members)
            for category, members in groups.items()
        }
        category_chars = (
            len(state_text)
            + sum(len(name) + len(description) for name, description in category_criteria.items())
            + len(_FALLBACK)
            + 64
        )
        second_stage_chars = max(
            self._choice_payload_chars(state_text, members) for members in groups.values()
        )
        hierarchical_chars = category_chars + second_stage_chars
        required_savings = self._config.hierarchical_min_savings_ratio
        return hierarchical_chars <= flat_chars * (1.0 - required_savings)

    def _choice_payload_chars(
        self,
        state_text: str,
        tools: Sequence[ToolDescriptor],
    ) -> int:
        criteria_chars = sum(
            len(tool.name) + len(self._tool_description(tool)) for tool in tools
        )
        return len(state_text) + criteria_chars + len(_FALLBACK) + 96

    def _cache_key(
        self,
        state_text: str,
        tools: Sequence[ToolDescriptor],
    ) -> tuple[object, ...]:
        return (
            state_text,
            tuple(
                (tool.name, tool.description, tool.category, tool.risk.value)
                for tool in tools
            ),
        )

    def _cache_get(self, key: tuple[object, ...]) -> RouteDecision | None:
        if self._config.route_cache_size == 0:
            return None
        decision = self._route_cache.get(key)
        if decision is not None:
            self._route_cache.move_to_end(key)
        return decision

    def _cache_put(self, key: tuple[object, ...], decision: RouteDecision) -> None:
        if self._config.route_cache_size == 0:
            return
        self._route_cache[key] = decision
        self._route_cache.move_to_end(key)
        while len(self._route_cache) > self._config.route_cache_size:
            self._route_cache.popitem(last=False)

    @staticmethod
    def _clip_text(value: str | None, limit: int) -> str | None:
        if value is None or len(value) <= limit:
            return value
        return value[: limit - 1] + "…"

    @staticmethod
    def _validate_tools(tools: Sequence[ToolDescriptor]) -> None:
        names = [tool.name for tool in tools]
        if any(not name.strip() for name in names):
            raise ValueError("tool names must be non-empty")
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        if _FALLBACK in names:
            raise ValueError(f"tool name {_FALLBACK!r} is reserved")
