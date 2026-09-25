from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "examples" / "openrouter_free_agent.py"
SPEC = importlib.util.spec_from_file_location("openrouter_free_agent_example", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

AgentPlan = MODULE.AgentPlan
OpenRouterFreeAgent = MODULE.OpenRouterFreeAgent
_parse_plan_response = MODULE._parse_plan_response


def test_parse_tagged_plan_with_multiline_python() -> None:
    raw = """<summary>Inspect the repository.</summary>
<done>false</done>
<code>
listing = local.fs_list(path=".")
target = local.fs_read(path="README.md")
return {"listing": listing, "target": target}
</code>"""

    plan = _parse_plan_response(raw)

    assert plan.summary == "Inspect the repository."
    assert plan.done is False
    assert 'local.fs_list(path=".")' in plan.code
    assert 'return {"listing": listing, "target": target}' in plan.code


def test_parse_valid_json_for_backward_compatibility() -> None:
    raw = json.dumps(
        {
            "summary": "Inspect README",
            "code": 'return local.fs_read(path="README.md")',
            "done": False,
        }
    )

    plan = _parse_plan_response(raw)

    assert plan == AgentPlan(
        summary="Inspect README",
        code='return local.fs_read(path="README.md")',
        done=False,
    )


@pytest.mark.asyncio
async def test_agent_retries_invalid_format_once() -> None:
    responses = [
        "I will inspect the repository first.",
        (
            "<summary>Inspect repository files.</summary>"
            "<done>false</done>"
            "<code>return local.fs_list(path=\".\")</code>"
        ),
    ]
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        body = responses[calls]
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": body}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    agent = OpenRouterFreeAgent(api_key="test", client=client)

    plan = await agent.plan(
        goal="Find a bug",
        phase="inspect",
        observation="",
        interfaces="local.fs_list(path: str)",
        allow_mutation=True,
        allow_bash=True,
    )

    assert calls == 2
    assert plan.summary == "Inspect repository files."
    assert plan.done is False
    assert plan.code == 'return local.fs_list(path=".")'

    await client.aclose()
