from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from harness_router import (
    ActionSummary,
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RiskLevel,
    RoutingConfig,
    RoutingSession,
    ToolDescriptor,
)

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openrouter/free"
IGNORED = {".git", ".venv", "node_modules", "dist", "build", "__pycache__"}

TOOLS = [
    ToolDescriptor("list_files", "List repository files", "inspect", RiskLevel.LOW),
    ToolDescriptor("read_file", "Read a repository file", "inspect", RiskLevel.LOW),
    ToolDescriptor("search_code", "Search repository text", "inspect", RiskLevel.LOW),
    ToolDescriptor("write_file", "Create or replace one file", "mutate", RiskLevel.MEDIUM),
    ToolDescriptor("run_tests", "Run pytest -q", "verify", RiskLevel.LOW),
    ToolDescriptor("finish", "Finish the task with a short summary", "finish", RiskLevel.LOW),
]

SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
    },
    "read_file": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    "search_code": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    "write_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    },
    "run_tests": {"type": "object", "properties": {}},
    "finish": {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    },
}


class Planner:
    """OpenRouter chat model: generates arguments/code, but normally not the tool choice."""

    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self.client = httpx.AsyncClient(
            timeout=60,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def arguments(
        self,
        state: HarnessState,
        tool: ToolDescriptor,
    ) -> dict[str, Any]:
        tool_name, arguments = await self._tool_call(
            "The routing layer already selected the tool. Call exactly that function with "
            "the best arguments for the current coding task.",
            {
                "goal": state.goal,
                "observation": state.observation,
                "selected_tool": tool.name,
            },
            [tool],
            forced_tool=tool.name,
        )
        if tool_name != tool.name:
            raise RuntimeError(
                f"planner called {tool_name!r}, expected selected tool {tool.name!r}"
            )
        return arguments

    async def fallback(self, state: HarnessState) -> tuple[str, dict[str, Any]]:
        return await self._tool_call(
            "The router requested planner fallback. Choose exactly one function that best "
            "advances the coding task and call it with valid arguments.",
            {
                "goal": state.goal,
                "observation": state.observation,
            },
            TOOLS,
        )

    async def _tool_call(
        self,
        system: str,
        payload: dict[str, Any],
        tools: list[ToolDescriptor],
        *,
        forced_tool: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        api_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": SCHEMAS[tool.name],
                },
            }
            for tool in tools
        ]
        tool_choice: str | dict[str, Any]
        if forced_tool is None:
            tool_choice = "required"
        else:
            tool_choice = {
                "type": "function",
                "function": {"name": forced_tool},
            }

        last_error = "planner returned no tool call"
        for attempt in range(2):
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            if attempt:
                messages.append(
                    {
                        "role": "user",
                        "content": "Your previous response did not contain a valid function call. "
                        "Call exactly one of the provided functions now.",
                    }
                )

            response = await self.client.post(
                CHAT_URL,
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": messages,
                    "tools": api_tools,
                    "tool_choice": tool_choice,
                    "provider": {"require_parameters": True},
                },
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = message.get("content")
                last_error = f"planner returned no tool call: {str(content)[:300]}"
                continue

            function = tool_calls[0].get("function") or {}
            name = function.get("name")
            raw_arguments = function.get("arguments", {})
            if not isinstance(name, str):
                last_error = "planner tool call is missing function name"
                continue

            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            elif isinstance(raw_arguments, str):
                try:
                    arguments = parse_json(raw_arguments)
                except RuntimeError as exc:
                    last_error = str(exc)
                    continue
            else:
                last_error = "planner returned invalid function arguments"
                continue

            if not isinstance(arguments, dict):
                last_error = "planner returned non-object function arguments"
                continue
            return name, arguments

        raise RuntimeError(last_error)

    async def aclose(self) -> None:
        await self.client.aclose()


class Workspace:
    def __init__(self, root: Path, apply: bool) -> None:
        self.root = root.resolve()
        self.apply = apply

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"path escapes workspace: {relative}")
        return path

    async def execute(self, tool: str, args: dict[str, Any]) -> str:
        if tool == "list_files":
            base = self.path(str(args.get("path", ".")))
            files = []
            for path in sorted(base.rglob("*")):
                relative = path.relative_to(self.root)
                if any(part in IGNORED for part in relative.parts):
                    continue
                if path.is_file():
                    files.append(str(relative))
                if len(files) == 200:
                    files.append("... truncated ...")
                    break
            return "\n".join(files) or "No files found."

        if tool == "read_file":
            path = self.path(required(args, "path"))
            return clip(path.read_text(encoding="utf-8", errors="replace"), 16_000)

        if tool == "search_code":
            query = required(args, "query").lower()
            hits: list[str] = []
            for path in sorted(self.root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(self.root)
                if any(part in IGNORED for part in relative.parts):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if query in line.lower():
                        hits.append(f"{relative}:{number}: {line.strip()}")
                        if len(hits) == 40:
                            return "\n".join(hits) + "\n... truncated ..."
            return "\n".join(hits) or "No matches."

        if tool == "write_file":
            relative = required(args, "path")
            content = required(args, "content")
            path = self.path(relative)
            old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            diff = "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
            if not self.apply:
                return "DRY RUN (use --apply to write)\n" + clip(diff, 8_000)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return "Applied write.\n" + clip(diff, 8_000)

        if tool == "run_tests":
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pytest",
                "-q",
                cwd=self.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=120)
            except TimeoutError:
                process.kill()
                await process.communicate()
                return "pytest timed out after 120 seconds"
            output = stdout.decode("utf-8", errors="replace")
            return f"pytest exit={process.returncode}\n{clip(output, 8_000)}"

        raise RuntimeError(f"unknown tool: {tool}")


def required(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"missing string argument: {key}")
    return value


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_json(raw: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"model returned malformed JSON: {raw[:300]}")


async def run(args: argparse.Namespace) -> int:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENROUTER_API_KEY first.")

    config = RoutingConfig(max_route_steps=args.max_steps)
    provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
    session = RoutingSession(JevToolRouter(provider, config), config)
    planner = Planner(api_key, args.model)
    workspace = Workspace(Path(args.workspace), args.apply)

    observation = "Repository has not been inspected yet."
    history: list[ActionSummary] = []
    last_action: str | None = None
    by_name = {tool.name: tool for tool in TOOLS}

    try:
        for step in range(1, args.max_steps + 1):
            state = HarnessState(
                goal=args.task,
                observation=observation,
                last_action=last_action,
                recent_actions=history[-3:],
                constraints=[
                    "Stay inside the workspace.",
                    "Prefer the smallest useful next action.",
                    "Writes are dry-run unless --apply is enabled.",
                ],
            )
            decision = await session.route(state, TOOLS)

            if decision.fallback:
                tool_name, tool_args = await planner.fallback(state)
                session.reset_fallbacks()
                source = f"planner fallback={decision.fallback_reason}"
            else:
                if decision.tool is None:
                    raise RuntimeError("router returned no tool")
                tool_name = decision.tool
                tool_args = await planner.arguments(state, by_name[tool_name])
                source = f"Jev confidence={decision.confidence:.3f}"

            if tool_name not in by_name:
                raise RuntimeError(f"unknown selected tool: {tool_name}")

            print(f"[step {step}] {source} -> {tool_name}")
            if tool_name == "finish":
                print(str(tool_args.get("answer", "Task finished.")))
                return 0

            try:
                result = await workspace.execute(tool_name, tool_args)
                result_class = "success"
            except (OSError, RuntimeError, ValueError) as exc:
                result = f"Tool error: {exc}"
                result_class = "error"

            loop = session.record_execution(
                tool_name,
                arguments=tool_args,
                result_class=result_class,
            )
            history.append(ActionSummary(tool_name, clip(result, 500)))
            observation = clip(result, 8_000)
            last_action = tool_name
            print(clip(result, 1_200))
            if loop:
                observation += "\nRouting loop detected; change strategy or finish."

        print("Stopped after max steps without finish.")
        return 2
    finally:
        await planner.aclose()
        await provider.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenRouter coding agent using harness-router")
    parser.add_argument("task")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--model", default=os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--apply", action="store_true", help="Allow write_file to modify files")
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
