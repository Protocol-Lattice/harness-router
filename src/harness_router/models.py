from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RoutingMode(str, Enum):
    PLANNER_ONLY = "planner_only"
    JEV_ONLY = "jev_only"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    description: str
    category: str | None = None
    risk: RiskLevel = RiskLevel.MEDIUM
    schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionSummary:
    tool: str
    outcome: str
    arguments_hash: str | None = None


@dataclass(slots=True)
class HarnessState:
    goal: str
    observation: str | None = None
    last_action: str | None = None
    recent_actions: list[ActionSummary] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)

    def compact(self, *, history_limit: int = 6) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "observation": self.observation,
            "last_action": self.last_action,
            "recent_actions": [
                {"tool": action.tool, "outcome": action.outcome}
                for action in self.recent_actions[-history_limit:]
            ],
            "constraints": self.constraints,
        }


@dataclass(frozen=True, slots=True)
class ChoiceDecision:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True, slots=True)
class RouteDecision:
    tool: str | None = None
    category: str | None = None
    confidence: float = 0.0
    probabilities: Mapping[str, float] = field(default_factory=dict)
    fallback: bool = False
    fallback_reason: str | None = None

    @classmethod
    def fallback_to_planner(
        cls,
        reason: str,
        *,
        confidence: float = 0.0,
        category: str | None = None,
        probabilities: Mapping[str, float] | None = None,
    ) -> RouteDecision:
        return cls(
            fallback=True,
            fallback_reason=reason,
            confidence=confidence,
            category=category,
            probabilities=probabilities or {},
        )
