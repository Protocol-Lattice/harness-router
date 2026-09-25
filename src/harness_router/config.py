from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import RouterConfigurationError
from .models import RoutingMode


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    mode: RoutingMode = RoutingMode.HYBRID
    direct_execution_threshold: float = 0.85
    fallback_threshold: float = 0.60
    hierarchical_threshold: int = 24
    max_same_action_repeats: int = 2
    max_route_steps: int = 50
    max_consecutive_fallbacks: int = 2
    description_limit: int = 160
    history_limit: int = 3
    state_field_limit: int = 800
    constraint_limit: int = 4

    def __post_init__(self) -> None:
        if not 0.0 <= self.fallback_threshold <= 1.0:
            raise RouterConfigurationError("fallback_threshold must be between 0 and 1")
        if not 0.0 <= self.direct_execution_threshold <= 1.0:
            raise RouterConfigurationError("direct_execution_threshold must be between 0 and 1")
        if self.fallback_threshold > self.direct_execution_threshold:
            raise RouterConfigurationError(
                "fallback_threshold cannot exceed direct_execution_threshold"
            )
        if self.hierarchical_threshold < 1:
            raise RouterConfigurationError("hierarchical_threshold must be >= 1")
        if self.max_same_action_repeats < 1:
            raise RouterConfigurationError("max_same_action_repeats must be >= 1")
        if self.max_route_steps < 1:
            raise RouterConfigurationError("max_route_steps must be >= 1")
        if self.max_consecutive_fallbacks < 1:
            raise RouterConfigurationError("max_consecutive_fallbacks must be >= 1")
        if self.description_limit < 32:
            raise RouterConfigurationError("description_limit must be >= 32")
        if self.history_limit < 1:
            raise RouterConfigurationError("history_limit must be >= 1")
        if self.state_field_limit < 64:
            raise RouterConfigurationError("state_field_limit must be >= 64")
        if self.constraint_limit < 0:
            raise RouterConfigurationError("constraint_limit must be >= 0")


@dataclass(frozen=True, slots=True)
class OpenRouterConfig:
    model: str = "typesafe/jev-1.13"
    url: str = "https://openrouter.ai/api/alpha/decisions"
    api_key_env: str = "OPENROUTER_API_KEY"
    timeout_seconds: float = 5.0

    def api_key(self) -> str:
        value = os.getenv(self.api_key_env)
        if not value:
            raise RouterConfigurationError(
                f"missing OpenRouter API key in environment variable {self.api_key_env}"
            )
        return value
