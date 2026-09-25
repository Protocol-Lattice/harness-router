from __future__ import annotations

import argparse
import ast
import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
    RoutingMode,
    ToolDescriptor,
)

PLANNER_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_ROUTER_CALLS = 2


TOOLS: tuple[ToolDescriptor, ...] = (
    ToolDescriptor(
        name="list_files",
        description="List repository files to understand project structure.",
        category="inspect",
        risk=RiskLevel.LOW,
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory, default '.'"},
            },
            "additionalProperties": False,
        },
    ),
    ToolDescriptor(
        name="read_file",
        description="Read a UTF-8 text file from the repository.",
        category="inspect",
        risk=RiskLevel.LOW,
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    ),
    ToolDescriptor(
        name="search_code",
        description="Search repository text for a literal query.",
        category="inspect",
        risk=RiskLevel.LOW,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "Relative search root, default '.'"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolDescriptor(
        name="replace_text",
        description="Replace one exact text fragment in one existing file.",
        category="mutate",
        risk=RiskLevel.MEDIUM,
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    ),
    ToolDescriptor(
        name="write_file",
        description="Create or completely replace one UTF-8 text file.",
        category="mutate",
        risk=RiskLevel.MEDIUM,
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    ),
    ToolDescriptor(
        name="run_tests",
        description="Run the repository test suite with 'python -m pytest -q'.",
        category="verify",
        risk=RiskLevel.LOW,
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolDescriptor(
        name="finish",
        description="Finish the task only after verification passes.",
        category="finish",
        risk=RiskLevel.LOW,
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
)


@dataclass(slots=True)
class AgentStats:
    planner_calls: int = 0
    planner_input_tokens: int = 0
    planner_output_tokens: int = 0
    router_calls: int = 0
    router_fallbacks: int = 0
    router_ms: float = 0.0
    tool_calls: int = 0

    @property
    def planner_tokens(self) -> int:
        return self.planner_input_tokens + self.planner_output_tokens


@dataclass(slots=True)
class AgentState:
    goal: str
    observation: str = "Start the task."
    history: list[ActionSummary] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    tests_passed: bool = False
    router_enabled: bool = True
    router_calls: int = 0
    finished: bool = False
    final_summary: str = ""


class Planner:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        stats: AgentStats,
    ) -> None:
        self._model = _normalize_model(model)
        self._stats = stats
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def choose_tool(
        self,
        *,
        state: AgentState,
        tools: Sequence[ToolDescriptor],
    ) -> tuple[str, dict[str, Any]]:
        registry = [_tool_payload(tool) for tool in tools]
        prompt = (
            "Choose exactly one next coding-agent tool and generate its arguments. "
            "Inspect evidence before editing. Prefer a minimal edit. Run tests after mutation. "
            "Do not finish before tests pass. Return JSON only as "
            '{"tool":"name","arguments":{...}}.\n\n'
            f"GOAL:\n{state.goal}\n\n"
            f"LATEST OBSERVATION:\n{state.observation}\n\n"
            f"RECENT ACTIONS:\n{_transcript(state.transcript)}\n\n"
            f"AVAILABLE TOOLS:\n{json.dumps(registry, separators=(',', ':'))}"
        )
        value = await self._call_json(prompt)
        action = _normalize_planner_action(value)
        if action is None:
            rendered = json.dumps(value, ensure_ascii=False, default=str)[:1000]
            raise RuntimeError(f"planner did not return a tool action: {rendered}")
        return action

    async def arguments_for(
        self,
        *,
        state: AgentState,
        tool: ToolDescriptor,
    ) -> dict[str, Any]:
        prompt = (
            "A fast runtime router already selected the next coding-agent tool. "
            "Generate arguments only for that selected tool. Do not reconsider tool choice. "
            "For source edits, make the smallest correct change. Return JSON only as "
            '{"arguments":{...}}.\n\n'
            f"GOAL:\n{state.goal}\n\n"
            f"LATEST OBSERVATION:\n{state.observation}\n\n"
            f"RECENT ACTIONS:\n{_transcript(state.transcript)}\n\n"
            f"SELECTED TOOL:\n{json.dumps(_tool_payload(tool), separators=(',', ':'))}"
        )
        value = await self._call_json(prompt)
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict):
            raise RuntimeError("planner arguments must be an object")
        return arguments

    async def _call_json(self, prompt: str) -> dict[str, Any]:
        self._stats.planner_calls += 1
        response = await self._client.post(
            PLANNER_URL,
            json={
                "model": self._model,
                "temperature": 0,
                "max_tokens": 4000,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a careful coding agent. Keep reasoning concise. "
                            "Never invent file contents you have not inspected. "
                            "Return machine-readable output only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = response.text[:2000]
            raise RuntimeError(
                f"OpenRouter planner request failed with HTTP "
                f"{response.status_code} for model {self._model!r}: {body}"
            ) from exc

        data = response.json()
        usage = data.get("usage") or {}
        self._stats.planner_input_tokens += int(
            usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        )
        self._stats.planner_output_tokens += int(
            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        )

        message = data["choices"][0]["message"]
        native_action = _parse_native_tool_calls(message.get("tool_calls"))
        if native_action is not None:
            return native_action

        content = message.get("content")
        if not isinstance(content, str):
            raise RuntimeError(
                f"planner returned neither text nor tool_calls: "
                f"{json.dumps(message, ensure_ascii=False, default=str)[:1000]}"
            )
        return _parse_model_object(content)


class Workspace:
    def __init__(self, root: Path, *, apply: bool, test_timeout: float) -> None:
        self.original_root = root.resolve()
        self.apply = apply
        self.test_timeout = test_timeout
        self._temp: tempfile.TemporaryDirectory[str] | None = None
        self.changed_files: set[str] = set()

        if apply:
            self.root = self.original_root
        else:
            self._temp = tempfile.TemporaryDirectory(prefix="harness-router-agent-")
            target = Path(self._temp.name) / "workspace"
            shutil.copytree(
                self.original_root,
                target,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    "venv",
                    "__pycache__",
                    ".mypy_cache",
                    ".pytest_cache",
                    ".ruff_cache",
                    "node_modules",
                ),
            )
            self.root = target.resolve()

    def close(self) -> None:
        if self._temp is not None:
            self._temp.cleanup()

    def execute(self, tool: str, arguments: Mapping[str, Any], state: AgentState) -> str:
        if tool == "list_files":
            path = self._safe_path(_optional_string(arguments, "path", "."))
            if not path.exists() or not path.is_dir():
                return f"error: directory not found: {path.relative_to(self.root)}"
            files = [
                str(item.relative_to(self.root))
                for item in sorted(path.rglob("*"))
                if item.is_file() and not _ignored(item.relative_to(self.root))
            ]
            return "\n".join(files[:500]) or "(no files)"

        if tool == "read_file":
            path = self._safe_path(_required_string(arguments, "path"))
            if not path.exists() or not path.is_file():
                return f"error: file not found: {path.relative_to(self.root)}"
            if path.stat().st_size > 300_000:
                return "error: file too large to read in this example agent"
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return "error: file is not UTF-8 text"

        if tool == "search_code":
            query = _required_string(arguments, "query")
            search_root = self._safe_path(_optional_string(arguments, "path", "."))
            if not search_root.exists():
                return "error: search root not found"
            matches: list[str] = []
            candidates = [search_root] if search_root.is_file() else search_root.rglob("*")
            for path in candidates:
                if not path.is_file() or _ignored(path.relative_to(self.root)):
                    continue
                if path.stat().st_size > 300_000:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if query in line:
                        matches.append(
                            f"{path.relative_to(self.root)}:{line_number}:{line[:300]}"
                        )
                        if len(matches) >= 100:
                            return "\n".join(matches)
            return "\n".join(matches) if matches else "no matches"

        if tool == "replace_text":
            path = self._safe_path(_required_string(arguments, "path"))
            old = _required_string(arguments, "old")
            new = _required_string(arguments, "new")
            if not path.exists() or not path.is_file():
                return "error: target file does not exist"
            text = path.read_text(encoding="utf-8")
            count = text.count(old)
            if count == 0:
                return "error: old text not found; inspect the exact file contents"
            if count > 1:
                return f"error: old text matched {count} times; make the edit more specific"
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            self.changed_files.add(str(path.relative_to(self.root)))
            state.tests_passed = False
            return f"updated {path.relative_to(self.root)}"

        if tool == "write_file":
            path = self._safe_path(_required_string(arguments, "path"))
            content = _required_string(arguments, "content", allow_empty=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.changed_files.add(str(path.relative_to(self.root)))
            state.tests_passed = False
            return f"wrote {path.relative_to(self.root)}"

        if tool == "run_tests":
            completed = subprocess.run(
                [os.sys.executable, "-m", "pytest", "-q"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.test_timeout,
                check=False,
            )
            output = (completed.stdout + "\n" + completed.stderr).strip()
            state.tests_passed = completed.returncode == 0
            status = "tests passed" if state.tests_passed else f"tests failed ({completed.returncode})"
            return f"{status}\n{output[-8000:]}"

        if tool == "finish":
            summary = arguments.get("summary", "")
            if not state.tests_passed:
                return "error: cannot finish before tests pass"
            state.finished = True
            state.final_summary = summary if isinstance(summary, str) else ""
            return "finished"

        return f"error: unknown tool {tool}"

    def _safe_path(self, value: str) -> Path:
        relative = Path(value)
        if relative.is_absolute():
            raise ValueError("absolute paths are not allowed")
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("path escapes the workspace")
        if ".git" in candidate.relative_to(self.root).parts:
            raise ValueError(".git access is not allowed")
        return candidate


class CodingAgent:
    """Skill-driven coding agent using Jev only for ambiguous discrete routing.

    This implements the cost-aware behavior documented in
    skills/harness-router/SKILL.md:
    - at most two router calls per task
    - stop routing after the first fallback
    - keep argument/code generation in the planner
    - keep execution authorization separate from Jev confidence
    """

    def __init__(
        self,
        *,
        planner: Planner,
        router: JevToolRouter,
        workspace: Workspace,
        stats: AgentStats,
        max_steps: int,
    ) -> None:
        self.planner = planner
        self.router = router
        self.workspace = workspace
        self.stats = stats
        self.max_steps = max_steps

    async def run(self, goal: str) -> AgentState:
        state = AgentState(goal=goal)

        for step in range(1, self.max_steps + 1):
            if state.finished:
                break

            tool_name, arguments = await self._next_action(state)
            descriptor = _tool_by_name(tool_name)
            if descriptor is None:
                state.observation = f"error: unknown tool selected: {tool_name}"
                state.transcript.append(state.observation)
                continue

            self.stats.tool_calls += 1
            try:
                result = self.workspace.execute(tool_name, arguments, state)
            except subprocess.TimeoutExpired:
                result = "error: tests timed out"
            except (OSError, ValueError) as exc:
                result = f"error: {exc}"

            state.observation = result
            state.history.append(ActionSummary(tool=tool_name, outcome=result[:500]))
            state.transcript.append(
                f"{tool_name}({json.dumps(arguments, ensure_ascii=False)[:1200]}) -> "
                f"{result[:2000]}"
            )

            print(f"[step {step}] {tool_name}")
            print(_one_line(result))

        return state

    async def _next_action(self, state: AgentState) -> tuple[str, dict[str, Any]]:
        if self._should_route(state):
            state.router_calls += 1
            self.stats.router_calls += 1
            started = time.perf_counter()
            decision = await self.router.route(_router_state(state), TOOLS)
            self.stats.router_ms += (time.perf_counter() - started) * 1000

            if decision.fallback or decision.tool is None:
                self.stats.router_fallbacks += 1
                state.router_enabled = False
                print(f"[router] fallback: {decision.fallback_reason}")
            else:
                descriptor = _tool_by_name(decision.tool)
                if descriptor is not None:
                    print(
                        f"[router] {decision.tool} "
                        f"(confidence={decision.confidence:.3f})"
                    )
                    if descriptor.schema.get("required") or descriptor.name == "finish":
                        arguments = await self.planner.arguments_for(
                            state=state,
                            tool=descriptor,
                        )
                    else:
                        arguments = {}
                    return descriptor.name, arguments

        return await self.planner.choose_tool(state=state, tools=TOOLS)

    @staticmethod
    def _should_route(state: AgentState) -> bool:
        if not state.router_enabled or state.router_calls >= MAX_ROUTER_CALLS:
            return False

        # The skill explicitly says to skip obvious linear coding steps.
        if state.history:
            last = state.history[-1].tool
            observation = state.observation.lower()

            if last in {"replace_text", "write_file"}:
                return False  # obvious next step is verification
            if "tests passed" in observation:
                return False  # obvious next step is finish
            if "file not found" in observation or "old text not found" in observation:
                return False  # planner must reason about recovery

        # Initial repository exploration is often a real closed choice:
        # list files vs search vs read a known path vs run tests.
        if not state.history:
            return True

        # A failing test can make inspect/search/edit/verify genuinely ambiguous.
        return "tests failed" in state.observation.lower()


def _normalize_model(model: str) -> str:
    model = model.strip()
    if model.startswith("openrouter:"):
        return "openrouter/" + model.removeprefix("openrouter:")
    return model


def _router_state(state: AgentState) -> HarnessState:
    return HarnessState(
        goal=state.goal,
        observation=state.observation[:800],
        last_action=state.history[-1].tool if state.history else None,
        recent_actions=state.history[-3:],
        constraints=[
            "Inspect before mutation",
            "Prefer minimal edits",
            "Run tests after mutation",
            "Do not finish before tests pass",
        ],
    )


def _tool_payload(tool: ToolDescriptor) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "category": tool.category,
        "risk": tool.risk.value,
        "schema": tool.schema,
    }


def _tool_by_name(name: str) -> ToolDescriptor | None:
    return next((tool for tool in TOOLS if tool.name == name), None)


def _transcript(items: Sequence[str]) -> str:
    return "\n".join(items[-6:]) if items else "(empty)"


def _one_line(value: str) -> str:
    line = " ".join(value.split())
    return line[:300] + ("..." if len(line) > 300 else "")


def _required_string(
    arguments: Mapping[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"missing string argument: {key}")
    return value


def _optional_string(arguments: Mapping[str, Any], key: str, default: str) -> str:
    value = arguments.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"argument {key} must be a string")
    return value


def _ignored(path: Path) -> bool:
    ignored_parts = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
    }
    return any(part in ignored_parts for part in path.parts)


def _parse_arguments(value: Any) -> dict[str, Any] | None:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _normalize_planner_action(value: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    for wrapper in ("tool_call", "function", "action", "call"):
        nested = value.get(wrapper)
        if isinstance(nested, dict):
            normalized = _normalize_planner_action(nested)
            if normalized is not None:
                return normalized

    tool_calls = value.get("tool_calls")
    if isinstance(tool_calls, list):
        for item in tool_calls:
            if isinstance(item, dict):
                normalized = _normalize_planner_action(item)
                if normalized is not None:
                    return normalized

    name = value.get("tool")
    if not isinstance(name, str):
        name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    raw_arguments: Any = {}
    for key in ("arguments", "args", "input", "parameters"):
        if key in value:
            raw_arguments = value[key]
            break

    arguments = _parse_arguments(raw_arguments)
    if arguments is None:
        return None
    return name.strip(), arguments


def _parse_native_tool_calls(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_planner_action(item)
        if normalized is None:
            continue
        tool, arguments = normalized
        return {"tool": tool, "arguments": arguments}
    return None


def _parse_model_object(raw: str) -> dict[str, Any]:
    text = raw.strip()

    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                value = _parse_non_json_tool_call(text)
        else:
            value = _parse_non_json_tool_call(text)

    if value is None or not isinstance(value, dict):
        raise RuntimeError(f"planner returned malformed JSON/tool call: {text[:300]!r}")
    return value


def _parse_non_json_tool_call(raw: str) -> dict[str, Any] | None:
    return _parse_text_tool_call(raw) or _parse_mcp_tool_call(raw)


def _parse_text_tool_call(raw: str) -> dict[str, Any] | None:
    text = raw.replace("<|tool_call_start|>", "").replace("<|tool_call_end|>", "").strip()
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        return None

    if isinstance(node, ast.List):
        if len(node.elts) != 1:
            return None
        node = node.elts[0]

    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.args:
        return None

    arguments: dict[str, Any] = {}
    for keyword in node.keywords:
        if keyword.arg is None:
            return None
        try:
            arguments[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError):
            return None

    return {"tool": node.func.id, "arguments": arguments}


def _parse_mcp_tool_call(raw: str) -> dict[str, Any] | None:
    """Parse XML-like MCP tool calls emitted as plain text by some chat models."""
    invoke = re.search(
        r'<invoke\s+name=["\']([^"\']+)["\']\s*>(.*?)</invoke>',
        raw,
        flags=re.DOTALL,
    )
    if invoke is None:
        return None

    tool = html.unescape(invoke.group(1)).strip()
    if not tool:
        return None

    arguments: dict[str, Any] = {}
    for name, value in re.findall(
        r'<parameter\s+name=["\']([^"\']+)["\']\s*>(.*?)</parameter>',
        invoke.group(2),
        flags=re.DOTALL,
    ):
        key = html.unescape(name).strip()
        if not key:
            return None
        arguments[key] = html.unescape(value).strip()

    return {"tool": tool, "arguments": arguments}


async def async_main(args: argparse.Namespace) -> int:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required")

    workspace_path = Path(args.workspace).expanduser().resolve()
    if not workspace_path.exists() or not workspace_path.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace_path}")

    task = args.task_option or args.task
    if not task:
        raise SystemExit("task is required: pass it positionally or with --task")
    if args.task_option and args.task:
        raise SystemExit("pass the task either positionally or with --task, not both")

    model = _normalize_model(args.model)

    stats = AgentStats()
    workspace = Workspace(
        workspace_path,
        apply=args.apply,
        test_timeout=args.test_timeout,
    )
    planner = Planner(
        api_key=api_key,
        model=model,
        timeout_seconds=args.timeout,
        stats=stats,
    )
    provider = OpenRouterJevProvider.from_config(
        OpenRouterConfig(timeout_seconds=min(args.timeout, 10.0))
    )
    router = JevToolRouter(
        provider,
        RoutingConfig(
            mode=RoutingMode.HYBRID,
            direct_execution_threshold=0.85,
            fallback_threshold=0.60,
            hierarchical_threshold=24,
        ),
    )

    agent = CodingAgent(
        planner=planner,
        router=router,
        workspace=workspace,
        stats=stats,
        max_steps=args.max_steps,
    )

    try:
        state = await agent.run(task)
    finally:
        await planner.aclose()
        await provider.aclose()
        workspace.close()

    print("\n=== result ===")
    print(f"verified: {state.tests_passed}")
    print(f"finished: {state.finished}")
    print(f"mode: {'apply' if args.apply else 'temporary copy (original unchanged)'}")
    print(f"changed files: {', '.join(sorted(workspace.changed_files)) or '(none)'}")
    if state.final_summary:
        print(f"summary: {state.final_summary}")

    print("\n=== metrics ===")
    print(f"planner calls: {stats.planner_calls}")
    print(f"planner tokens: {stats.planner_tokens}")
    print(f"router calls: {stats.router_calls}")
    print(f"router fallbacks: {stats.router_fallbacks}")
    print(f"router time: {stats.router_ms:.1f} ms")
    print(f"tool calls: {stats.tool_calls}")

    return 0 if state.finished and state.tests_passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Small coding agent using harness-router as a selective Jev tool router."
    )
    parser.add_argument("task", nargs="?", help="coding task for the agent")
    parser.add_argument(
        "--task",
        dest="task_option",
        help="coding task for the agent (alternative to positional task)",
    )
    parser.add_argument("--workspace", default=".", help="repository root")
    parser.add_argument(
        "--model",
        default=os.getenv("OPENROUTER_MODEL", "openrouter/free"),
        help="OpenRouter planner model",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="modify the real workspace; default runs in a temporary copy",
    )
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--test-timeout", type=float, default=120.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_steps < 1:
        raise SystemExit("--max-steps must be >= 1")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
