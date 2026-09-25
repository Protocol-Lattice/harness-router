from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

import httpx

from .config import OpenRouterConfig
from .errors import InvalidProviderResponse, ProviderError, ProviderTimeoutError
from .models import ChoiceDecision


class DecisionProvider(Protocol):
    async def choose(
        self,
        *,
        state: str,
        instructions: str,
        criteria: Mapping[str, str],
    ) -> ChoiceDecision: ...


class OpenRouterJevProvider:
    """Jev provider backed by OpenRouter's Decisions API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "typesafe/jev-1.13",
        base_url: str = "https://openrouter.ai/api/alpha/decisions",
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=httpx.Limits(
                max_connections=16,
                max_keepalive_connections=8,
                keepalive_expiry=30.0,
            ),
        )

    @classmethod
    def from_config(
        cls,
        config: OpenRouterConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> OpenRouterJevProvider:
        return cls(
            api_key=config.api_key(),
            model=config.model,
            base_url=config.url,
            timeout_seconds=config.timeout_seconds,
            client=client,
        )

    async def choose(
        self,
        *,
        state: str,
        instructions: str,
        criteria: Mapping[str, str],
    ) -> ChoiceDecision:
        if len(criteria) < 2:
            raise ValueError("Jev choice requires at least two criteria")

        state_payload: object = state
        if state.lstrip().startswith("{"):
            try:
                parsed_state = json.loads(state)
            except ValueError:
                pass
            else:
                if isinstance(parsed_state, dict):
                    state_payload = parsed_state

        payload = {
            "model": self._model,
            "state": state_payload,
            "questions": {
                "route": {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": dict(criteria),
                }
            },
        }

        try:
            response = await self._client.post(
                self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("OpenRouter Jev request timed out") from exc
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise ProviderError(
                f"OpenRouter Jev returned HTTP {exc.response.status_code}: {body}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenRouter Jev request failed: {exc}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise InvalidProviderResponse("OpenRouter response was not valid JSON") from exc

        answer = data.get("answers", {}).get("route")
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise InvalidProviderResponse("missing choice answer at answers.route")

        choice = answer.get("choice")
        probabilities_raw = answer.get("probabilities", {})
        confidence_raw = answer.get("confidence", 0.0)

        if not isinstance(choice, str):
            raise InvalidProviderResponse("choice answer did not contain a string choice")
        if choice not in criteria:
            raise InvalidProviderResponse(f"provider selected unknown criterion: {choice}")
        if not isinstance(probabilities_raw, dict):
            raise InvalidProviderResponse("choice probabilities must be an object")
        if not isinstance(confidence_raw, (int, float)):
            raise InvalidProviderResponse("choice confidence must be numeric")

        probabilities: dict[str, float] = {}
        for key, value in probabilities_raw.items():
            if not isinstance(key, str) or not isinstance(value, (int, float)):
                raise InvalidProviderResponse("choice probabilities must map strings to numbers")
            probabilities[key] = float(value)

        confidence = float(confidence_raw)
        if not 0.0 <= confidence <= 1.0:
            raise InvalidProviderResponse("choice confidence must be between 0 and 1")

        return ChoiceDecision(
            choice=choice,
            probabilities=probabilities,
            confidence=confidence,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> OpenRouterJevProvider:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
