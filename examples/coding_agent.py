from __future__ import annotations

import argparse
import ast
import asyncio
import html
import importlib.util
import json
import math
import os
import random
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
MAX_SUBAGENTS = 8
MAX_PLANNER_ATTEMPTS = 2


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


REPORT_FINDINGS_TOOL = ToolDescriptor(
    name="report_findings",
    description="Return concise read-only findings to the parent coding agent.",
    category="inspect",
    risk=RiskLevel.LOW,
    schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Concrete findings with relevant file paths and symbols.",
            },
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
)

SUBAGENT_TOOLS: tuple[ToolDescriptor, ...] = tuple(
    tool for tool in TOOLS if tool.category == "inspect"
) + (REPORT_FINDINGS_TOOL,)

DELEGATE_SUBTASKS_TOOL = ToolDescriptor(
    name="delegate_subtasks",
    description="Split a coding task into independent read-only investigations.",
    category="plan",
    risk=RiskLevel.LOW,
    schema={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "maxItems": MAX_SUBAGENTS,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "goal": {"type": "string"},
                    },
                    "required": ["id", "goal"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["tasks"],
        "additionalProperties": False,
    },
)


@dataclass(slots=True, frozen=True)
class SubagentTask:
    id: str
    goal: str


@dataclass(slots=True, frozen=True)
class SubagentResult:
    id: str
    goal: str
    summary: str
    steps: int
    success: bool


@dataclass(slots=True)
class AgentStats:
    planner_calls: int = 0
    planner_input_tokens: int = 0
    planner_output_tokens: int = 0
    router_calls: int = 0
    router_fallbacks: int = 0
    router_ms: float = 0.0
    tool_calls: int = 0
    subagent_plans: int = 0
    subagents_spawned: int = 0
    subagent_tool_calls: int = 0
    mcts_decisions: int = 0
    mcts_simulations: int = 0
    mcts_ms: float = 0.0

    @property
    def planner_tokens(self) -> int:
        return self.planner_input_tokens + self.planner_output_tokens


@dataclass(slots=True)
class AgentState:
    goal: str
    observation: str = "Start the task."
    history: list[ActionSummary] = field(default_factory=list)
    transcript: list[str] = field(default_factory=list)
    inspection_fingerprints: set[str] = field(default_factory=set)
    tests_passed: bool = False
    router_enabled: bool = True
    router_calls: int = 0
    finished: bool = False
    final_summary: str = ""


@dataclass(slots=True, frozen=True)
class MCTSState:
    last_tool: str | None
    inspected: bool
    mutated: bool
    tests_passed: bool
    tests_failed: bool


@dataclass(slots=True)
class MCTSNode:
    state: MCTSState
    parent: MCTSNode | None = None
    action: str | None = None
    visits: int = 0
    value: float = 0.0
    children: dict[str, MCTSNode] = field(default_factory=dict)
    untried_actions: list[str] = field(default_factory=list)


class MCTSActionSelector:
    """Small Monte Carlo tree search over tool sequences.

    The search is intentionally model-free: it explores only tool categories and
    lifecycle constraints. The planner still generates concrete arguments/code
    for the selected tool, keeping MCTS cheap enough for a coding-agent example.
    """

    def __init__(
        self,
        *,
        tools: Sequence[ToolDescriptor],
        simulations: int,
        max_depth: int,
        exploration: float,
        seed: int,
        stats: AgentStats,
    ) -> None:
        self.tools = tuple(tools)
        self.simulations = simulations
        self.max_depth = max_depth
        self.exploration = exploration
        self.random = random.Random(seed)
        self.stats = stats

    def select(self, state: AgentState) -> tuple[str, float]:
        started = time.perf_counter()
        root_state = _mcts_state_from_agent(state)
        root = MCTSNode(
            state=root_state,
            untried_actions=self._legal_actions(root_state),
        )
        if not root.untried_actions:
            raise RuntimeError("MCTS has no legal actions")

        for _ in range(self.simulations):
            node = root
            depth = 0

            while (
                depth < self.max_depth
                and not node.untried_actions
                and node.children
            ):
                node = self._select_child(node)
                depth += 1

            if depth < self.max_depth and node.untried_actions:
                index = self.random.randrange(len(node.untried_actions))
                action = node.untried_actions.pop(index)
                next_state = _mcts_transition(node.state, action)
                child = MCTSNode(
                    state=next_state,
                    parent=node,
                    action=action,
                    untried_actions=self._legal_actions(next_state),
                )
                node.children[action] = child
                node = child
                depth += 1

            reward = self._path_reward(node) + self._rollout(node.state, depth)
            self._backpropagate(node, reward)

        if not root.children:
            action = root.untried_actions[0]
            score = 0.0
        else:
            action, best = max(
                root.children.items(),
                key=lambda item: (
                    item[1].visits,
                    item[1].value / max(1, item[1].visits),
                    item[0],
                ),
            )
            score = best.value / max(1, best.visits)

        self.stats.mcts_decisions += 1
        self.stats.mcts_simulations += self.simulations
        self.stats.mcts_ms += (time.perf_counter() - started) * 1000
        return action, score

    def _select_child(self, node: MCTSNode) -> MCTSNode:
        log_parent = math.log(max(1, node.visits))

        def uct(child: MCTSNode) -> float:
            if child.visits == 0:
                return math.inf
            exploit = child.value / child.visits
            explore = self.exploration * math.sqrt(log_parent / child.visits)
            return exploit + explore

        return max(node.children.values(), key=uct)

    @staticmethod
    def _path_reward(node: MCTSNode) -> float:
        reward = 0.0
        current: MCTSNode | None = node
        while current is not None and current.parent is not None:
            if current.action is not None:
                reward += _mcts_action_reward(current.parent.state, current.action)
            current = current.parent
        return reward

    def _rollout(self, state: MCTSState, depth: int) -> float:
        total = 0.0
        current = state
        remaining = max(0, self.max_depth - depth)

        for _ in range(remaining):
            actions = self._legal_actions(current)
            if not actions:
                break
            action = max(
                actions,
                key=lambda candidate: (
                    _mcts_action_reward(current, candidate),
                    self.random.random(),
                ),
            )
            total += _mcts_action_reward(current, action)
            current = _mcts_transition(current, action)
            if action == "finish":
                break

        return total

    @staticmethod
    def _backpropagate(node: MCTSNode, reward: float) -> None:
        current: MCTSNode | None = node
        while current is not None:
            current.visits += 1
            current.value += reward
            current = current.parent

    def _legal_actions(self, state: MCTSState) -> list[str]:
        available = {tool.name for tool in self.tools}
        inspect = [
            name
            for name in ("read_file", "search_code", "list_files")
            if name in available
        ]
        mutate = [
            name
            for name in ("replace_text", "write_file")
            if name in available
        ]

        if state.tests_passed and "finish" in available:
            return ["finish"]

        if not state.inspected:
            actions = list(inspect)
            if "run_tests" in available:
                actions.append("run_tests")
            return actions

        if state.mutated and not state.tests_passed:
            actions: list[str] = []
            if "run_tests" in available:
                actions.append("run_tests")
            actions.extend(inspect)
            actions.extend(mutate)
            return actions

        actions = list(mutate)
        actions.extend(inspect)
        if state.tests_failed and "run_tests" in available:
            actions.append("run_tests")
        return actions


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
        prompt = (
            "Choose exactly one next coding-agent tool. "
            "Inspect evidence before editing, prefer a minimal edit, run tests after mutation, "
            "and do not finish before tests pass. Never repeat the same read/list/search call "
            "unless a mutation changed the workspace since that inspection. "
            "Use one of the provided tools.\n\n"
            f"GOAL:\n{state.goal}\n\n"
            f"LATEST OBSERVATION:\n{state.observation}\n\n"
            f"RECENT ACTIONS:\n{_transcript(state.transcript)}"
        )
        return await self._call_tool(prompt, tools=tools)

    async def arguments_for(
        self,
        *,
        state: AgentState,
        tool: ToolDescriptor,
    ) -> dict[str, Any]:
        prompt = (
            "A fast runtime router already selected the next coding-agent tool. "
            "Call exactly that tool with the best arguments. Do not reconsider tool choice. "
            "For source edits, make the smallest correct change.\n\n"
            f"GOAL:\n{state.goal}\n\n"
            f"LATEST OBSERVATION:\n{state.observation}\n\n"
            f"RECENT ACTIONS:\n{_transcript(state.transcript)}"
        )
        selected, arguments = await self._call_tool(
            prompt,
            tools=[tool],
            forced_tool=tool.name,
        )
        if selected != tool.name:
            raise RuntimeError(
                f"planner called {selected!r} after router selected {tool.name!r}"
            )
        return arguments

    async def plan_subtasks(
        self,
        *,
        goal: str,
        max_subagents: int,
    ) -> list[SubagentTask]:
        if max_subagents <= 0:
            return []

        limit = min(max_subagents, MAX_SUBAGENTS)
        self._stats.subagent_plans += 1
        prompt = (
            "Decide whether parallel read-only subagents would materially help this coding task. "
            "Subagents may only inspect the repository; they cannot edit files or run tests. "
            "Return an empty tasks list for a narrow task where delegation would add overhead. "
            f"Otherwise return at most {limit} independent investigations, preferably 2-4. "
            "Keep scopes distinct and make each goal self-contained.\n\n"
            f"CODING TASK:\n{goal}"
        )
        selected, arguments = await self._call_tool(
            prompt,
            tools=[DELEGATE_SUBTASKS_TOOL],
            forced_tool=DELEGATE_SUBTASKS_TOOL.name,
        )
        if selected != DELEGATE_SUBTASKS_TOOL.name:
            return []

        raw_tasks = arguments.get("tasks")
        if not isinstance(raw_tasks, list):
            return []

        tasks: list[SubagentTask] = []
        seen_ids: set[str] = set()
        for index, item in enumerate(raw_tasks[:limit], start=1):
            if not isinstance(item, dict):
                continue
            raw_goal = item.get("goal")
            if not isinstance(raw_goal, str) or not raw_goal.strip():
                continue
            raw_id = item.get("id")
            task_id = raw_id.strip() if isinstance(raw_id, str) else ""
            if not task_id:
                task_id = f"subagent-{index}"
            if task_id in seen_ids:
                task_id = f"{task_id}-{index}"
            seen_ids.add(task_id)
            tasks.append(SubagentTask(id=task_id, goal=raw_goal.strip()))
        return tasks

    async def choose_subagent_tool(
        self,
        *,
        state: AgentState,
    ) -> tuple[str, dict[str, Any]]:
        prompt = (
            "You are a read-only coding subagent working for a parent coding agent. "
            "Investigate only your scoped goal. Never propose or perform edits. "
            "Use list_files, read_file, and search_code to gather concrete evidence. "
            "As soon as you have enough evidence, call report_findings with a concise summary "
            "including relevant file paths, symbols, constraints, and risks.\n\n"
            f"SCOPED GOAL:\n{state.goal}\n\n"
            f"LATEST OBSERVATION:\n{state.observation[:12000]}\n\n"
            f"RECENT ACTIONS:\n{_transcript(state.transcript)}"
        )
        return await self._call_tool(prompt, tools=SUBAGENT_TOOLS)

    async def _call_tool(
        self,
        prompt: str,
        *,
        tools: Sequence[ToolDescriptor],
        forced_tool: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(MAX_PLANNER_ATTEMPTS):
            try:
                return await self._call_tool_once(
                    prompt=prompt,
                    tools=tools,
                    forced_tool=forced_tool,
                    retry=attempt > 0,
                )
            except (RuntimeError, httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                if attempt + 1 >= MAX_PLANNER_ATTEMPTS:
                    raise
        raise RuntimeError(f"planner failed after retries: {last_error}")

    async def _call_tool_once(
        self,
        *,
        prompt: str,
        tools: Sequence[ToolDescriptor],
        forced_tool: str | None,
        retry: bool,
    ) -> tuple[str, dict[str, Any]]:
        self._stats.planner_calls += 1
        if retry:
            prompt += (
                "\n\nRETRY: The previous response was unusable. Return exactly one valid "
                "native tool call and no prose."
            )

        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": 0,
            "max_tokens": 4000,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a careful coding agent. Keep reasoning concise. "
                        "Never invent file contents you have not inspected. "
                        "Use the provided tools instead of describing a tool call in prose."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "tools": [_native_tool_payload(tool) for tool in tools],
        }
        if forced_tool is None:
            payload["tool_choice"] = "required"
        else:
            payload["tool_choice"] = {
                "type": "function",
                "function": {"name": forced_tool},
            }

        response = await self._client.post(PLANNER_URL, json=payload)
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
            action = _normalize_planner_action(native_action)
            if action is not None:
                return action

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            value = _parse_model_object(content)
            action = _normalize_planner_action(value)
            if action is not None:
                return action

        rendered = json.dumps(message, ensure_ascii=False, default=str)[:2000]
        raise RuntimeError(f"planner did not return a usable tool call: {rendered}")


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
            if importlib.util.find_spec("pytest") is None:
                state.tests_passed = False
                return (
                    "tests failed (pytest unavailable)\n"
                    "Install development dependencies with: uv sync --extra dev"
                )
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


class InspectionSubagent:
    """Read-only worker used to gather repository context in parallel."""

    def __init__(
        self,
        *,
        planner: Planner,
        workspace: Workspace,
        stats: AgentStats,
        max_steps: int,
    ) -> None:
        self.planner = planner
        self.workspace = workspace
        self.stats = stats
        self.max_steps = max_steps

    async def run(self, task: SubagentTask) -> SubagentResult:
        state = AgentState(
            goal=task.goal,
            observation="Start the scoped read-only investigation.",
            router_enabled=False,
        )

        try:
            for step in range(1, self.max_steps + 1):
                tool_name, arguments = await self.planner.choose_subagent_tool(state=state)
                self.stats.tool_calls += 1
                self.stats.subagent_tool_calls += 1

                if tool_name == REPORT_FINDINGS_TOOL.name:
                    summary = _required_string(arguments, "summary").strip()
                    return SubagentResult(
                        id=task.id,
                        goal=task.goal,
                        summary=summary[:4000],
                        steps=step,
                        success=True,
                    )

                descriptor = _tool_by_name_in(tool_name, SUBAGENT_TOOLS)
                if descriptor is None or descriptor.name == REPORT_FINDINGS_TOOL.name:
                    result = f"error: read-only subagent cannot use tool {tool_name!r}"
                else:
                    try:
                        result = self.workspace.execute(tool_name, arguments, state)
                    except (OSError, ValueError) as exc:
                        result = f"error: {exc}"

                state.observation = result
                state.history.append(ActionSummary(tool=tool_name, outcome=result[:500]))
                state.transcript.append(
                    f"{tool_name}({json.dumps(arguments, ensure_ascii=False)[:800]}) -> "
                    f"{result[:1800]}"
                )
        except Exception as exc:
            return SubagentResult(
                id=task.id,
                goal=task.goal,
                summary=f"subagent failed: {exc}",
                steps=len(state.history),
                success=False,
            )

        fallback = state.observation.strip()
        if not fallback:
            fallback = "subagent reached its step limit without useful findings"
        return SubagentResult(
            id=task.id,
            goal=task.goal,
            summary=fallback[:4000],
            steps=self.max_steps,
            success=False,
        )


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
        max_subagents: int,
        subagent_steps: int,
        mcts: MCTSActionSelector | None,
    ) -> None:
        self.planner = planner
        self.router = router
        self.workspace = workspace
        self.stats = stats
        self.max_steps = max_steps
        self.max_subagents = max_subagents
        self.subagent_steps = subagent_steps
        self.mcts = mcts

    async def run(self, goal: str) -> AgentState:
        state = AgentState(goal=goal)

        subagent_results = await self._run_subagents(goal)
        if subagent_results:
            context = _render_subagent_results(subagent_results)
            state.observation = (
                "Parallel read-only subagents inspected the repository. "
                "Use their findings as evidence, verify anything uncertain, and remain the only writer.\n\n"
                f"{context}"
            )
            state.transcript.append(f"parallel_subagents -> {context[:6000]}")

        for step in range(1, self.max_steps + 1):
            if state.finished:
                break

            try:
                tool_name, arguments = await self._next_action(state)
            except Exception as exc:
                state.router_enabled = False
                state.observation = (
                    f"error: action selection failed: {type(exc).__name__}: {exc}"
                )
                state.transcript.append(state.observation)
                print(f"[step {step}] recovery")
                print(_one_line(state.observation))
                continue

            descriptor = _tool_by_name(tool_name)
            if descriptor is None:
                state.observation = f"error: unknown tool selected: {tool_name}"
                state.transcript.append(state.observation)
                continue

            if descriptor.category == "inspect":
                fingerprint = _call_fingerprint(tool_name, arguments)
                if fingerprint in state.inspection_fingerprints:
                    print(f"[progress] blocked duplicate inspection: {tool_name}")
                    state.observation = (
                        f"Duplicate inspection blocked: {tool_name} with identical arguments "
                        "already ran without an intervening workspace mutation. Choose a "
                        "different inspection or make progress with an edit/test."
                    )
                    state.transcript.append(state.observation)
                    alternatives = tuple(tool for tool in TOOLS if tool.name != tool_name)
                    tool_name, arguments = await self.planner.choose_tool(
                        state=state,
                        tools=alternatives,
                    )
                    descriptor = _tool_by_name(tool_name)
                    if descriptor is None:
                        state.observation = f"error: unknown tool selected: {tool_name}"
                        state.transcript.append(state.observation)
                        continue
                    if descriptor.category == "inspect":
                        fingerprint = _call_fingerprint(tool_name, arguments)
                        if fingerprint in state.inspection_fingerprints:
                            state.observation = (
                                f"error: duplicate inspection blocked again: {tool_name}"
                            )
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
            if descriptor.category == "inspect":
                state.inspection_fingerprints.add(_call_fingerprint(tool_name, arguments))
            elif descriptor.category == "mutate" and not result.startswith("error:"):
                state.inspection_fingerprints.clear()

            state.history.append(ActionSummary(tool=tool_name, outcome=result[:500]))
            state.transcript.append(
                f"{tool_name}({json.dumps(arguments, ensure_ascii=False)[:1200]}) -> "
                f"{result[:2000]}"
            )

            print(f"[step {step}] {tool_name}")
            print(_one_line(result))

        return state

    async def _run_subagents(self, goal: str) -> list[SubagentResult]:
        if self.max_subagents <= 0:
            return []

        try:
            tasks = await self.planner.plan_subtasks(
                goal=goal,
                max_subagents=self.max_subagents,
            )
        except Exception as exc:
            print(f"[subagents] planning skipped: {exc}")
            return []

        if not tasks:
            print("[subagents] no useful parallel investigations")
            return []

        self.stats.subagents_spawned += len(tasks)
        print(f"[subagents] running {len(tasks)} read-only investigations in parallel")

        worker = InspectionSubagent(
            planner=self.planner,
            workspace=self.workspace,
            stats=self.stats,
            max_steps=self.subagent_steps,
        )
        pending: list[tuple[SubagentTask, asyncio.Task[SubagentResult]]] = []
        async with asyncio.TaskGroup() as group:
            for task in tasks:
                pending.append((task, group.create_task(worker.run(task))))

        results = [future.result() for _, future in pending]
        for result in results:
            status = "ok" if result.success else "partial"
            print(f"[subagent {result.id}] {status}: {_one_line(result.summary)}")
        return results

    async def _next_action(self, state: AgentState) -> tuple[str, dict[str, Any]]:
        if state.history:
            last = state.history[-1].tool
            if (
                last in {"replace_text", "write_file"}
                and not state.observation.startswith("error:")
            ):
                return "run_tests", {}
            if state.tests_passed and self.workspace.changed_files:
                changed = ", ".join(sorted(self.workspace.changed_files))
                return "finish", {"summary": f"Updated {changed} and tests passed."}

        if self._should_route(state):
            state.router_calls += 1
            self.stats.router_calls += 1
            started = time.perf_counter()
            decision = await self.router.route(_router_state(state), TOOLS)
            self.stats.router_ms += (time.perf_counter() - started) * 1000

            if decision.fallback or decision.tool is None:
            tool_name, score = self.mcts.select(state)
            descriptor = _tool_by_name(tool_name)
            if descriptor is not None:
                print(f"[mcts] {tool_name} (mean_reward={score:.3f})")
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


def _mcts_state_from_agent(state: AgentState) -> MCTSState:
    inspected = any(
        summary.tool in {"list_files", "read_file", "search_code"}
        for summary in state.history
    ) or any(item.startswith("parallel_subagents ->") for item in state.transcript)
    mutated = any(
        summary.tool in {"replace_text", "write_file"}
        for summary in state.history
    )
    observation = state.observation.lower()
    return MCTSState(
        last_tool=state.history[-1].tool if state.history else None,
        inspected=inspected,
        mutated=mutated,
        tests_passed=state.tests_passed,
        tests_failed="tests failed" in observation,
    )


def _mcts_transition(state: MCTSState, action: str) -> MCTSState:
    inspected = state.inspected or action in {"list_files", "read_file", "search_code"}
    mutated = state.mutated or action in {"replace_text", "write_file"}
    tests_passed = state.tests_passed

    # Rollouts are optimistic about verification after a mutation. Runtime truth
    # still comes only from Workspace.execute("run_tests").
    if action == "run_tests" and mutated:
        tests_passed = True
    elif action in {"replace_text", "write_file"}:
        tests_passed = False

    return MCTSState(
        last_tool=action,
        inspected=inspected,
        mutated=mutated,
        tests_passed=tests_passed,
        tests_failed=False if action == "run_tests" else state.tests_failed,
    )


def _mcts_action_reward(state: MCTSState, action: str) -> float:
    # Small per-step cost makes shorter successful plans preferable.
    reward = -0.25

    if action in {"list_files", "read_file", "search_code"}:
        reward += 2.0 if not state.inspected else -0.5
        if state.tests_failed:
            reward += 1.0

    if action in {"replace_text", "write_file"}:
        if state.mutated and not state.tests_failed:
            reward -= 1.0
        else:
            reward += 3.0 if state.inspected else -5.0
        if state.tests_failed:
            reward += 1.5

    if action == "run_tests":
        reward += 4.0 if state.mutated and not state.tests_passed else -0.5

    if action == "finish":
        reward += 8.0 if state.tests_passed else -10.0

    if action == state.last_tool:
        reward -= 1.5

    return reward


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


def _native_tool_payload(tool: ToolDescriptor) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.schema),
        },
    }


def _call_fingerprint(tool: str, arguments: Mapping[str, Any]) -> str:
    return json.dumps(
        {"tool": tool, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _tool_by_name(name: str) -> ToolDescriptor | None:
    return _tool_by_name_in(name, TOOLS)


def _tool_by_name_in(
    name: str,
    tools: Sequence[ToolDescriptor],
) -> ToolDescriptor | None:
    return next((tool for tool in tools if tool.name == name), None)


def _transcript(items: Sequence[str]) -> str:
    return "\n".join(items[-6:]) if items else "(empty)"


def _one_line(value: str) -> str:
    line = " ".join(value.split())
    return line[:300] + ("..." if len(line) > 300 else "")


def _render_subagent_results(results: Sequence[SubagentResult]) -> str:
    rendered: list[str] = []
    for result in results:
        status = "verified findings" if result.success else "partial findings"
        rendered.append(
            f"[{result.id} | {status} | {result.steps} steps]\n"
            f"Goal: {result.goal}\n"
            f"{result.summary}"
        )
    return "\n\n".join(rendered)


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

    mcts = None
    if args.mcts_simulations > 0:
        mcts = MCTSActionSelector(
            tools=TOOLS,
            simulations=args.mcts_simulations,
            max_depth=args.mcts_depth,
            exploration=args.mcts_exploration,
            seed=args.mcts_seed,
            stats=stats,
        )

    agent = CodingAgent(
        planner=planner,
        router=router,
        workspace=workspace,
        stats=stats,
        max_steps=args.max_steps,
        max_subagents=args.subagents,
        subagent_steps=args.subagent_steps,
        mcts=mcts,
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
    print(f"subagent plans: {stats.subagent_plans}")
    print(f"subagents spawned: {stats.subagents_spawned}")
    print(f"subagent tool calls: {stats.subagent_tool_calls}")
    print(f"mcts decisions: {stats.mcts_decisions}")
    print(f"mcts simulations: {stats.mcts_simulations}")
    print(f"mcts time: {stats.mcts_ms:.1f} ms")

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
    parser.add_argument(
        "--subagents",
        type=int,
        default=0,
        help=f"maximum parallel read-only subagents, 0 disables (max {MAX_SUBAGENTS})",
    )
    parser.add_argument(
        "--subagent-steps",
        type=int,
        default=5,
        help="maximum inspect/report steps per subagent",
    )
    parser.add_argument(
        "--mcts-simulations",
        type=int,
        default=0,
        help="MCTS simulations per tool decision; 0 disables MCTS",
    )
    parser.add_argument(
        "--mcts-depth",
        type=int,
        default=4,
        help="maximum MCTS tool-sequence depth",
    )
    parser.add_argument(
        "--mcts-exploration",
        type=float,
        default=math.sqrt(2.0),
        help="UCT exploration constant",
    )
    parser.add_argument(
        "--mcts-seed",
        type=int,
        default=0,
        help="random seed for reproducible MCTS rollouts",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--test-timeout", type=float, default=120.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.max_steps < 1:
        raise SystemExit("--max-steps must be >= 1")
    if not 0 <= args.subagents <= MAX_SUBAGENTS:
        raise SystemExit(f"--subagents must be between 0 and {MAX_SUBAGENTS}")
    if args.subagent_steps < 1:
        raise SystemExit("--subagent-steps must be >= 1")
    if args.mcts_simulations < 0:
        raise SystemExit("--mcts-simulations must be >= 0")
    if args.mcts_depth < 1:
        raise SystemExit("--mcts-depth must be >= 1")
    if args.mcts_exploration < 0:
        raise SystemExit("--mcts-exploration must be >= 0")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
