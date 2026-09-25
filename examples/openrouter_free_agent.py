from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass(frozen=True, slots=True)
class AgentPlan:
    summary: str
    code: str
    done: bool = False


class OpenRouterFreeAgent:
    """Coding planner backed by OpenRouter's free model router.

    Free-router models can vary between requests, so this example deliberately
    uses a simple tagged text envelope instead of embedding multiline Python
    source in a JSON string.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "openrouter/free",
        timeout_seconds: float = 90.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._model = model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def plan(
        self,
        *,
        goal: str,
        phase: str,
        observation: str,
        interfaces: str,
        allow_mutation: bool,
        allow_bash: bool,
    ) -> AgentPlan:
        system = """You are a repository bug-fix coding agent.

You do not directly access the filesystem or shell. Generate a short Python
program that runs inside UTCP CodeMode with registered tools.

Return EXACTLY this tagged envelope, without Markdown fences:

<summary>short explanation of the next step</summary>
<done>false</done>
<code>
Python CodeMode program here
</code>

For the finish phase use:

<summary>final concise result</summary>
<done>true</done>
<code></code>

CodeMode rules:
- Tool calls are synchronous. Do not use await.
- Use full names such as local.fs_read(path="README.md").
- End executable programs with a return value.
- Do not import os, sys, subprocess, pathlib, or use open, exec, or eval.
- Use only registered UTCP tools and safe Python operations.
- Inspect before modifying.
- Never fabricate test results or file contents.

Phase rules:
- inspect: only local.fs_list and local.fs_read.
- reproduce: use local.bash_run for targeted tests/checks.
- fix: use local.fs_read plus local.fs_patch or local.fs_write; prefer fs_patch.
- verify: use local.bash_run and optionally local.fs_read.
- finish: done=true and empty code.

Do not finish until a concrete bug was identified, fixed, and verified.
"""

        permissions = {
            "filesystem_mutation_allowed": allow_mutation,
            "bash_allowed": allow_bash,
        }
        user = (
            f"Goal:\n{goal}\n\n"
            f"Current phase:\n{phase}\n\n"
            f"Permissions:\n{json.dumps(permissions)}\n\n"
            f"Latest observation:\n{observation or '(none yet)'}\n\n"
            f"Available UTCP CodeMode interfaces:\n{interfaces}\n\n"
            "Generate the smallest useful next CodeMode program for this phase. "
            "If permission is missing, explain it in summary, use empty code, "
            "and keep done=false."
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        last_error: RuntimeError | None = None
        for attempt in range(2):
            raw = await self._complete(messages)
            try:
                return _parse_plan_response(raw)
            except RuntimeError as exc:
                last_error = exc
                if attempt == 0:
                    messages.extend(
                        [
                            {"role": "assistant", "content": raw},
                            {
                                "role": "user",
                                "content": (
                                    "Your previous response did not match the required "
                                    "tagged envelope. Retry now. Return only "
                                    "<summary>...</summary><done>true|false</done>"
                                    "<code>...</code>. Do not use Markdown fences and "
                                    "do not return JSON."
                                ),
                            },
                        ]
                    )

        assert last_error is not None
        raise RuntimeError(
            "OpenRouter agent returned an invalid plan after one retry"
        ) from last_error

    async def _complete(self, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 2200,
        }

        try:
            response = await self._client.post(
                OPENROUTER_CHAT_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/Protocol-Lattice/harness-router",
                    "X-Title": "harness-router bugfix agent",
                },
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"OpenRouter free agent request failed: {exc}"
            ) from exc

        try:
            data = response.json()
            raw = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenRouter returned an invalid chat response") from exc

        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError("OpenRouter returned an empty agent response")
        return raw

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_plan_response(raw: str) -> AgentPlan:
    text = raw.strip()

    try:
        summary = _extract_tag(text, "summary").strip()
        done_text = _extract_tag(text, "done").strip().lower()
        code = _extract_tag(text, "code").strip()
    except RuntimeError:
        # Backward-compatible fallback for models that still return valid JSON.
        value = _parse_json_object(text)
        summary = value.get("summary")
        code = value.get("code")
        done = value.get("done", False)

        if not isinstance(summary, str):
            raise RuntimeError("agent response is missing a summary")
        if not isinstance(code, str):
            raise RuntimeError("agent response is missing code")
        if not isinstance(done, bool):
            raise RuntimeError("agent response has an invalid done value")
        return AgentPlan(summary=summary.strip(), code=code.strip(), done=done)

    if not summary:
        raise RuntimeError("agent response contains an empty summary")
    if done_text not in {"true", "false"}:
        raise RuntimeError("agent response done tag must be true or false")

    return AgentPlan(
        summary=summary,
        code=code,
        done=done_text == "true",
    )


def _extract_tag(text: str, tag: str) -> str:
    open_tag = f"<{tag}>"
    close_tag = f"</{tag}>"
    start = text.find(open_tag)
    if start < 0:
        raise RuntimeError(f"agent response is missing <{tag}>")
    start += len(open_tag)
    end = text.find(close_tag, start)
    if end < 0:
        raise RuntimeError(f"agent response is missing </{tag}>")
    return text[start:end]


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = chr(96) * 3

    if text.startswith(fence):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith(fence):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        if text.startswith("json"):
            text = text[4:].lstrip()

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("agent did not return a parseable plan") from exc
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested:
            raise RuntimeError("agent returned malformed JSON") from nested

    if not isinstance(value, dict):
        raise RuntimeError("agent response must be an object")
    return value
