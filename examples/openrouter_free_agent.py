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
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "openrouter/free",
        timeout_seconds: float = 90.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._model = model
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

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

Return only a JSON object with keys summary, code, and done.

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
- finish: set done=true and code to an empty string.

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
            "If permission is missing, explain it in summary, use an empty code "
            "string, and keep done=false."
        )

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
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

        value = _parse_json_object(raw)
        summary = value.get("summary")
        code = value.get("code")
        done = value.get("done", False)

        if not isinstance(summary, str):
            raise RuntimeError("agent response is missing string field 'summary'")
        if not isinstance(code, str):
            raise RuntimeError("agent response is missing string field 'code'")
        if not isinstance(done, bool):
            raise RuntimeError("agent response field 'done' must be boolean")

        return AgentPlan(summary=summary, code=code, done=done)

    async def close(self) -> None:
        await self._client.aclose()


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
            raise RuntimeError("agent did not return a JSON object") from exc
        try:
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError as nested:
            raise RuntimeError("agent returned malformed JSON") from nested

    if not isinstance(value, dict):
        raise RuntimeError("agent response must be a JSON object")
    return value
