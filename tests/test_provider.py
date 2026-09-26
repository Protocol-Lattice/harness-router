import json

import httpx
import pytest

from harness_router import InvalidProviderResponse, OpenRouterJevProvider, ProviderError


@pytest.mark.asyncio
async def test_openrouter_jev_payload_and_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.ai/api/alpha/decisions"
        assert request.headers["Authorization"] == "Bearer secret"
        payload = json.loads(request.content)
        assert payload["model"] == "typesafe/jev-1.13"
        assert payload["state"] == {"goal": "inspect"}
        assert payload["questions"]["route"]["type"] == "choice"
        assert "read_file" in payload["questions"]["route"]["criteria"]
        return httpx.Response(
            200,
            json={
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "read_file",
                        "probabilities": {"read_file": 0.97, "__fallback__": 0.03},
                        "confidence": 0.97,
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterJevProvider(api_key="secret", client=client)
    decision = await provider.choose(
        state='{"goal":"inspect"}',
        instructions="Choose",
        criteria={"read_file": "Read a file", "__fallback__": "Fallback"},
    )
    assert decision.choice == "read_file"
    assert decision.confidence == 0.97
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_rejects_unknown_choice() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "not_registered",
                        "probabilities": {"not_registered": 1.0},
                        "confidence": 1.0,
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterJevProvider(api_key="secret", client=client)
    with pytest.raises(InvalidProviderResponse):
        await provider.choose(
            state="x",
            instructions="Choose",
            criteria={"a": "A", "b": "B"},
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_preserves_plain_text_state() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["state"] == "plain state"
        return httpx.Response(
            200,
            json={
                "answers": {
                    "route": {
                        "type": "choice",
                        "choice": "a",
                        "probabilities": {"a": 1.0, "b": 0.0},
                        "confidence": 1.0,
                    }
                }
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterJevProvider(api_key="secret", client=client)
    await provider.choose(
        state="plain state",
        instructions="Choose",
        criteria={"a": "A", "b": "B"},
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_refuses_api_key_in_routing_payload() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent when payload contains the API key")

    api_key = "sk-or-test-secret-value"
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterJevProvider(api_key=api_key, client=client)

    with pytest.raises(ProviderError, match="refusing to send"):
        await provider.choose(
            state=f"user accidentally pasted {api_key}",
            instructions="Choose",
            criteria={"a": "A", "b": "B"},
        )

    await client.aclose()


@pytest.mark.asyncio
async def test_provider_redacts_api_key_from_http_error() -> None:
    api_key = "sk-or-test-secret-value"

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad credential: {api_key}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenRouterJevProvider(api_key=api_key, client=client)

    with pytest.raises(ProviderError) as exc_info:
        await provider.choose(
            state="inspect",
            instructions="Choose",
            criteria={"a": "A", "b": "B"},
        )

    rendered = str(exc_info.value)
    assert api_key not in rendered
    assert "[REDACTED]" in rendered
    assert exc_info.value.__cause__ is None

    await client.aclose()
