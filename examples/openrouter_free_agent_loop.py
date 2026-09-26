from __future__ import annotations

import argparse
import asyncio
import difflib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
DEFAULT_MODEL = "openrouter/free"
MAX_ROUTER_CALLS = 2
MAX_PLANNER_ATTEMPTS = 2
MAX_INSPECTIONS_BEFORE_MUTATION = 3


TOOLS: tuple[ToolDescriptor, ...] = (
    ToolDescriptor(
        name="list_files",
        description="List repository files.",
        category="inspect",
        risk=RiskLevel.LOW,
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    ToolDescriptor(
        name="read_file",
        description="Read one UTF-8 repository file.",
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
                "path": {"type": "string"},
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
        description="Create or replace one UTF-8 text file.",
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
        description="Run the repository tests with python -m pytest -q.",
        category="verify",
        risk=RiskLevel.LOW,
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolDescriptor(
        name="finish",
        description="Finish after the task is complete and tests pass.",
        category="finish",
        risk=RiskLevel.LOW,
        schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    ),
)


@dataclass(slots=True)
class AgentState:
    goal: str
    observation: str = "Start by inspecting the repository."
    history: list[ActionSummary] = field(default_factory=list)
    tests_passed: bool = False
    finished: bool = False
    final_summary: str = ""
    router_enabled: bool = True
    router_calls: int = 0
    inspection_calls: set[str] = field(default_factory=set)
    inspected_paths: set[str] = field(default_factory=set)


class OpenRouterFreePlanner:
    """Small planner that defaults to OpenRouter's free-model router."""

    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        self.model = model
        self.calls = 0
        self.client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def choose(
        self,
        state: AgentState,
        tools: Sequence[ToolDescriptor],
        *,
        forced_tool: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        prompt = (
            "Choose exactly one next coding-agent tool. Inspect before editing, prefer the "
            "smallest correct edit, run tests after mutations, and only finish after tests pass. "
            "Return exactly one tool call, never a list or batch. Do not repeat the same "
            "read/list/search with identical arguments unless a mutation changed the workspace. "
            "Use native tool calling. If native tool calling is unavailable, return only JSON "
            'in the form {"tool":"name","arguments":{...}}.\n\n'
            f"GOAL:\n{state.goal}\n\n"
            f"LATEST OBSERVATION:\n{state.observation}\n\n"
            f"RECENT ACTIONS:\n{_history_text(state.history)}"
        )

        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 4000,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the planning model inside a coding-agent loop. "
                        "Never invent file contents that have not been inspected."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "tools": [_native_tool(tool) for tool in tools],
        }
        if forced_tool is None:
            payload["tool_choice"] = "required"
        else:
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": forced_tool},
            }

        message: Mapping[str, Any] | None = None
        action: tuple[str, dict[str, Any]] | None = None
        for attempt in range(MAX_PLANNER_ATTEMPTS):
            request_payload = dict(payload)
            if attempt:
                request_payload["messages"] = [
                    *payload["messages"],
                    {
                        "role": "user",
                        "content": (
                            "RETRY: return exactly one valid tool call. "
                            "Do not return a list of calls or prose."
                        ),
                    },
                ]

            self.calls += 1
            response = await self.client.post(PLANNER_URL, json=request_payload)
            if response.is_error:
                raise RuntimeError(
                    f"OpenRouter planner failed ({response.status_code}): "
                    f"{response.text[:1200]}"
                )

            message = response.json()["choices"][0]["message"]
            action = _parse_planner_message(message)
            if action is not None:
                break

        if action is None:
            raise RuntimeError(
                "OpenRouter planner did not return a usable tool call after retry: "
                + json.dumps(message, ensure_ascii=False, default=str)[:1200]
            )

        tool_name, arguments = action
        if forced_tool is not None and tool_name != forced_tool:
            raise RuntimeError(
                f"planner returned {tool_name!r}, expected forced tool {forced_tool!r}"
            )
        return tool_name, arguments


class Workspace:
    """Tiny sandbox: default to a temporary copy, mutate the real repo only with --apply."""

    def __init__(self, root: Path, *, apply: bool, test_timeout: float) -> None:
        self.original_root = root.resolve()
        self.test_timeout = test_timeout
        self.changed_files: set[str] = set()
        self._temp: tempfile.TemporaryDirectory[str] | None = None

        if apply:
            self.root = self.original_root
        else:
            self._temp = tempfile.TemporaryDirectory(prefix="harness-router-free-agent-")
            self.root = Path(self._temp.name) / "workspace"
            shutil.copytree(
                self.original_root,
                self.root,
                ignore=shutil.ignore_patterns(
                    ".git",
                    ".venv",
                    "venv",
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    ".ruff_cache",
                    "node_modules",
                ),
            )
            self.root = self.root.resolve()

    def close(self) -> None:
        if self._temp is not None:
            self._temp.cleanup()

    def execute(
        self,
        tool_name: str,
        arguments: Mapping[str, Any],
        state: AgentState,
    ) -> str:
        if tool_name == "list_files":
            root = self._safe_path(_string(arguments, "path", default="."))
            if not root.is_dir():
                return "error: directory not found"
            files = [
                str(path.relative_to(self.root))
                for path in sorted(root.rglob("*"))
                if path.is_file() and not _ignored(path.relative_to(self.root))
            ]
            return "\n".join(files[:400]) or "(no files)"

        if tool_name == "read_file":
            path = self._safe_path(_string(arguments, "path"))
            if not path.is_file():
                return "error: file not found"
            try:
                return path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return "error: file is not UTF-8 text"

        if tool_name == "search_code":
            query = _string(arguments, "query")
            root = self._safe_path(_string(arguments, "path", default="."))
            if not root.exists():
                return "error: search path not found"
            matches: list[str] = []
            candidates = [root] if root.is_file() else root.rglob("*")
            for path in candidates:
                if not path.is_file() or _ignored(path.relative_to(self.root)):
                    continue
                if path.stat().st_size > 200_000:
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if query in line:
                        matches.append(
                            f"{path.relative_to(self.root)}:{line_number}:{line[:240]}"
                        )
                        if len(matches) >= 80:
                            return "\n".join(matches)
            return "\n".join(matches) if matches else "no matches"

        if tool_name == "replace_text":
            path = self._safe_path(_string(arguments, "path"))
            old = _string(arguments, "old")
            new = _string(arguments, "new", allow_empty=True)
            if not path.is_file():
                return "error: target file not found"
            if old == new:
                return "error: no-op mutation: old and new text are identical"
            content = path.read_text(encoding="utf-8")
            count = content.count(old)
            if count != 1:
                return f"error: expected exactly one match, found {count}"
            updated = content.replace(old, new, 1)
            if updated == content:
                return "error: no-op mutation: file content would not change"
            path.write_text(updated, encoding="utf-8")
            relative = str(path.relative_to(self.root))
            self.changed_files.add(relative)
            state.tests_passed = False
            return _mutation_result("updated", relative, content, updated)

        if tool_name == "write_file":
            path = self._safe_path(_string(arguments, "path"))
            content = _string(arguments, "content", allow_empty=True)
            previous = ""
            existed = path.is_file()
            if existed:
                previous = path.read_text(encoding="utf-8")
                if previous == content:
                    return "error: no-op mutation: generated content matches the existing file"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            relative = str(path.relative_to(self.root))
            self.changed_files.add(relative)
            state.tests_passed = False
            return _mutation_result(
                "updated" if existed else "created",
                relative,
                previous,
                content,
            )

        if tool_name == "run_tests":
            command = [sys.executable, "-m", "pytest", "-q"]
            if importlib.util.find_spec("pytest") is None:
                uv = shutil.which("uv")
                if uv is None:
                    state.tests_passed = False
                    return (
                        "tests unavailable: pytest is not installed. "
                        "Install dev dependencies with: uv sync --extra dev"
                    )
                command = [uv, "run", "--extra", "dev", "python", "-m", "pytest", "-q"]

            try:
                result = subprocess.run(
                    command,
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=self.test_timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                state.tests_passed = False
                return "tests failed: timeout"
            output = (result.stdout + "\n" + result.stderr).strip()
            state.tests_passed = result.returncode == 0
            status = "tests passed" if state.tests_passed else f"tests failed ({result.returncode})"
            return f"{status}\n{output[-8000:]}"

        if tool_name == "finish":
            if not state.tests_passed:
                return "error: cannot finish before tests pass"
            state.finished = True
            state.final_summary = _string(arguments, "summary", default="Task complete.")
            return "finished"

        return f"error: unknown tool {tool_name}"

    def _safe_path(self, value: str) -> Path:
        raw = Path(value)
        candidate = raw.resolve() if raw.is_absolute() else (self.root / raw).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError("path escapes the workspace")
        if ".git" in candidate.relative_to(self.root).parts:
            raise ValueError(".git access is not allowed")
        return candidate


class CodingAgent:
    def __init__(
        self,
        planner: OpenRouterFreePlanner,
        router: JevToolRouter,
        workspace: Workspace,
        *,
        max_steps: int,
    ) -> None:
        self.planner = planner
        self.router = router
        self.workspace = workspace
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
                continue

            fingerprint = _call_fingerprint(tool_name, arguments)
            if descriptor.category == "inspect" and fingerprint in state.inspection_calls:
                result = (
                    f"duplicate inspection blocked: {tool_name} with identical arguments. "
                    "Choose a different inspection or make progress with an edit/test."
                )
                state.observation = result
                state.history.append(ActionSummary(tool=tool_name, outcome=result))
                print(f"[step {step}] {tool_name} (blocked duplicate)")
                print(_one_line(result))
                continue

            try:
                result = self.workspace.execute(tool_name, arguments, state)
            except (OSError, ValueError) as exc:
                result = f"error: {exc}"

            if descriptor.category == "inspect":
                state.inspection_calls.add(fingerprint)
                if tool_name == "read_file" and not result.startswith("error:"):
                    path = arguments.get("path")
                    if isinstance(path, str):
                        state.inspected_paths.add(path)
            elif descriptor.category == "mutate" and not result.startswith("error:"):
                state.inspection_calls.clear()

            state.observation = result
            state.history.append(ActionSummary(tool=tool_name, outcome=result))

            print(f"[step {step}] {tool_name}")
            if tool_name == "read_file":
                print(result)
            else:
                print(_one_line(result))

        return state

    async def _next_action(self, state: AgentState) -> tuple[str, dict[str, Any]]:
        target_path = _target_path_from_goal(state.goal)

        if not state.history and target_path is not None:
            candidate = (self.workspace.root / target_path).resolve()
            if candidate.is_relative_to(self.workspace.root) and candidate.is_file():
                print(f"[progress] direct target from goal: {target_path}")
                return "read_file", {"path": target_path}

        if state.history:
            last_tool = state.history[-1].tool

            # Keep obvious linear steps out of both Jev and the planner.
            if last_tool in {"replace_text", "write_file"} and not state.observation.startswith(
                "error:"
            ):
                return "run_tests", {}

            if state.tests_passed and self.workspace.changed_files:
                changed = ", ".join(sorted(self.workspace.changed_files))
                return "finish", {"summary": f"Updated {changed}; tests passed."}

        if _goal_requires_mutation(state.goal) and not self.workspace.changed_files:
            inspections = sum(
                1
                for item in state.history
                if item.tool in {"list_files", "read_file", "search_code"}
                and not item.outcome.startswith("error:")
                and not item.outcome.startswith("duplicate inspection blocked:")
            )
            target_is_inspected = (
                target_path is not None and target_path in state.inspected_paths
            )
            if target_is_inspected or inspections >= MAX_INSPECTIONS_BEFORE_MUTATION:
                print("[progress] inspection budget reached -> mutation phase")
                mutation_tools = [
                    tool
                    for tool in TOOLS
                    if tool.name in {"replace_text", "write_file"}
                ]
                tool_name, arguments = await self.planner.choose(state, mutation_tools)
                if target_path is not None:
                    planned_path = arguments.get("path")
                    if planned_path != target_path:
                        print(
                            f"[progress] scoped mutation path: "
                            f"{planned_path!r} -> {target_path!r}"
                        )
                    arguments["path"] = target_path
                return tool_name, arguments

        if state.router_enabled and state.router_calls < MAX_ROUTER_CALLS:
            state.router_calls += 1
            try:
                decision = await self.router.route(_router_state(state), TOOLS)
            except Exception as exc:
                state.router_enabled = False
                print(f"[router] error -> planner: {_one_line(str(exc))}")
            else:
                if decision.fallback or decision.tool is None:
                    state.router_enabled = False
                    print(f"[router] fallback -> planner: {decision.fallback_reason}")
                else:
                    descriptor = _tool_by_name(decision.tool)
                    if descriptor is not None:
                        print(
                            f"[router] {descriptor.name} "
                            f"(confidence={decision.confidence:.3f})"
                        )
                        if descriptor.schema.get("required"):
                            arguments = await self.planner.choose(
                                state,
                                [descriptor],
                                forced_tool=descriptor.name,
                            )
                            return arguments
                        return descriptor.name, {}

        return await self.planner.choose(state, TOOLS)


def _router_state(state: AgentState) -> HarnessState:
    return HarnessState(
        goal=state.goal,
        observation=state.observation[:800],
        last_action=state.history[-1].tool if state.history else None,
        recent_actions=state.history[-2:],
        constraints=[
            "Inspect before mutation",
            "Run tests after mutation",
        ],
    )


def _native_tool(tool: ToolDescriptor) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.schema),
        },
    }


def _parse_planner_message(
    message: Mapping[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for item in tool_calls:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function")
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = _parse_arguments(function.get("arguments"))
            if arguments is not None:
                return name, arguments

    function_call = message.get("function_call")
    if isinstance(function_call, Mapping):
        name = function_call.get("name")
        if isinstance(name, str) and name:
            arguments = _parse_arguments(function_call.get("arguments"))
            if arguments is not None:
                return name, arguments

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None

    text = content.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None

    action = _normalize_action_value(value)
    if action is not None:
        return action

    # Free models occasionally wrap one or more tool calls in malformed text,
    # e.g. [[{"name":"read_file","parameters":{...}}, ...].
    # Scan for the first independently decodable JSON object/list instead of
    # crashing the whole agent loop.
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        action = _normalize_action_value(candidate)
        if action is not None:
            return action

    return None


def _normalize_action_value(value: Any) -> tuple[str, dict[str, Any]] | None:
    if isinstance(value, list):
        for item in value:
            action = _normalize_action_value(item)
            if action is not None:
                return action
        return None

    if not isinstance(value, Mapping):
        return None

    for wrapper in ("tool_call", "function", "action", "call"):
        nested = value.get(wrapper)
        if isinstance(nested, Mapping):
            action = _normalize_action_value(nested)
            if action is not None:
                return action

    name = value.get("tool", value.get("name"))
    if not isinstance(name, str) or not name:
        return None

    raw_arguments: Any = {}
    for key in ("arguments", "args", "input", "parameters"):
        if key in value:
            raw_arguments = value[key]
            break

    arguments = _parse_arguments(raw_arguments)
    if arguments is None:
        return None
    return name, arguments


def _parse_arguments(value: Any) -> dict[str, Any] | None:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _mutation_result(
    action: str,
    path: str,
    before: str,
    after: str,
) -> str:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=2,
        )
    )
    if not diff:
        return "error: no-op mutation: no diff produced"
    clipped = diff[:4000]
    suffix = "\n... diff clipped ..." if len(diff) > len(clipped) else ""
    return f"{action} {path}\n{clipped}{suffix}"


def _goal_requires_mutation(goal: str) -> bool:
    words = {
        "add",
        "change",
        "create",
        "delete",
        "fix",
        "implement",
        "modify",
        "refactor",
        "remove",
        "rename",
        "replace",
        "rewrite",
        "update",
    }
    lowered = goal.lower()
    return any(word in lowered.split() for word in words)


def _target_path_from_goal(goal: str) -> str | None:
    candidates: list[str] = []
    cleaned = goal.replace("`", " ").replace('"', " ").replace("\'", " ")
    for raw in cleaned.split():
        token = raw.strip(".,:;()[]{}")
        if "/" not in token:
            continue
        if token.startswith(("/", "../")):
            continue
        if token.endswith(
            (
                ".py",
                ".go",
                ".rs",
                ".js",
                ".ts",
                ".tsx",
                ".jsx",
                ".md",
                ".toml",
                ".yaml",
                ".yml",
                ".json",
            )
        ):
            candidates.append(token)
    return candidates[0] if candidates else None


def _call_fingerprint(tool_name: str, arguments: Mapping[str, Any]) -> str:
    return json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _tool_by_name(name: str) -> ToolDescriptor | None:
    return next((tool for tool in TOOLS if tool.name == name), None)


def _history_text(history: Sequence[ActionSummary]) -> str:
    if not history:
        return "(empty)"
    return "\n".join(
        f"{item.tool}: {item.outcome}"
        for item in history[-5:]
    )


def _string(
    arguments: Mapping[str, Any],
    key: str,
    *,
    default: str | None = None,
    allow_empty: bool = False,
) -> str:
    value = arguments.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"argument {key!r} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"argument {key!r} cannot be empty")
    return value


def _ignored(path: Path) -> bool:
    ignored = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
    return any(part in ignored for part in path.parts)


def _one_line(value: str) -> str:
    compact = " ".join(value.split())
    return compact[:300] + ("..." if len(compact) > 300 else "")


async def async_main(args: argparse.Namespace) -> int:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required")

    workspace_path = Path(args.workspace).expanduser().resolve()
    if not workspace_path.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace_path}")

    planner = OpenRouterFreePlanner(
        api_key=api_key,
        model=args.model,
        timeout=args.timeout,
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
        ),
    )
    workspace = Workspace(
        workspace_path,
        apply=args.apply,
        test_timeout=args.test_timeout,
    )
    agent = CodingAgent(
        planner,
        router,
        workspace,
        max_steps=args.max_steps,
    )

    try:
        state = await agent.run(args.task)
    finally:
        await planner.aclose()
        await provider.aclose()
        workspace.close()

    print("\n=== result ===")
    print(f"planner model: {args.model}")
    print(f"planner calls: {planner.calls}")
    print(f"router calls: {state.router_calls}")
    print(f"tests passed: {state.tests_passed}")
    print(f"finished: {state.finished}")
    print(f"changed files: {', '.join(sorted(workspace.changed_files)) or '(none)'}")
    print(f"mode: {'apply' if args.apply else 'temporary copy'}")
    if state.final_summary:
        print(f"summary: {state.final_summary}")

    return 0 if state.finished else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Minimal harness-router coding-agent loop with OpenRouter free models as planner."
        )
    )
    parser.add_argument("task", help="coding task")
    parser.add_argument("--workspace", default=".", help="repository root")
    parser.add_argument(
        "--model",
        default=os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL),
        help="OpenRouter planner model; default: openrouter/free",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="modify the real workspace; default uses a temporary copy",
    )
    parser.add_argument("--max-steps", type=int, default=12)
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
