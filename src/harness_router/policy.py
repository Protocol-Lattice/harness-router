from __future__ import annotations

from dataclasses import dataclass

from .models import RiskLevel, RouteDecision, ToolDescriptor


@dataclass(frozen=True, slots=True)
class DefaultExecutionPolicy:
    """Conservative default policy; confidence is never treated as authorization."""

    medium_min_confidence: float | None = None

    async def allow(self, decision: RouteDecision, tool: ToolDescriptor) -> bool:
        if decision.fallback:
            return False
        if tool.risk is RiskLevel.LOW:
            return True
        if tool.risk is RiskLevel.MEDIUM and self.medium_min_confidence is not None:
            return decision.confidence >= self.medium_min_confidence
        return False
