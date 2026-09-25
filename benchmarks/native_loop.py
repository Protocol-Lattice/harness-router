from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from harness_router import (
    ActionSummary,
    ChoiceDecision,
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


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    goal: str
    files: Mapping[str, str]
    failure: str
    expected_path: str
    accepted_fragments: tuple[str, ...]


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="percentage_discount",
        goal="Fix the failing percentage-discount test without changing the public API.",
        files={
            "src/pricing.py": (
                "def apply_discount(total: float, percent: float) -> float:\n"
                "    return round(total * (1 - percent), 2)\n"
            ),
            "tests/test_pricing.py": (
                "from src.pricing import apply_discount\n\n"
                "def test_percentage_discount():\n"
                "    assert apply_discount(100.0, 25.0) == 75.0\n"
            ),
        },
        failure="test_percentage_discount: expected 75.0, got -2400.0",
        expected_path="src/pricing.py",
        accepted_fragments=("percent / 100", "percent/100", "percent * 0.01", "percent*0.01"),
    ),
    Scenario(
        name="slug_separator",
        goal="Fix the failing slug test while keeping slugify small and deterministic.",
        files={
            "src/slug.py": (
                "def slugify(value: str) -> str:\n"
                "    return value.strip().lower().replace(' ', '_')\n"
            ),
            "tests/test_slug.py": (
                "from src.slug import slugify\n\n"
                "def test_slug_separator():\n"
                "    assert slugify('Hello Router') == 'hello-router'\n"
            ),
        },
        failure="test_slug_separator: expected 'hello-router', got 'hello_router'",
        expected_path="src/slug.py",
        accepted_fragments=("replace(' ', '-')", 'replace(" ", "-")'),
    ),
    Scenario(
        name="retry_limit",
        goal="Fix the retry loop so it performs exactly max_attempts calls.",
        files={
            "src/retry.py": (
                "def run(operation, max_attempts: int):\n"
                "    for attempt in range(max_attempts + 1):\n"
                "        try:\n"
                "            return operation()\n"
                "        except Exception:\n"
                "            if attempt == max_attempts:\n"
                "                raise\n"
            ),
            "tests/test_retry.py": (
                "from src.retry import run\n\n"
                "def test_retry_limit():\n"
                "    # a failing operation must be invoked exactly 3 times\n"
                "    ...\n"
            ),
        },
        failure="test_retry_limit: expected 3 calls, observed 4",
        expected_path="src/retry.py",
        accepted_fragments=("range(max_attempts)",),
    ),
)


TOOLS: tuple[ToolDescriptor, ...] = (
    ToolDescriptor(
        name="list_files",
        description="List repository files.",
        category="inspect",
        risk=RiskLevel.LOW,
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolDescriptor(
        name="read_file",
        description="Read one repository file by exact path.",
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
        description="Search repository files for a literal string.",
        category="inspect",
        risk=RiskLevel.LOW,
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    ToolDescriptor(
        name="replace_text",
        description="Replace one exact text fragment in one file.",
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
        name="run_tests",
        description="Run the focused regression test for the current task.",
        category="verify",
        risk=RiskLevel.LOW,
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    ToolDescriptor(
        name="finish",
        description="Finish only after the focused regression test passes.",
        category="finish",
        risk=RiskLevel.LOW,
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
)


@dataclass(slots=True)
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class RouterUsage:
    requests: int = 0
    elapsed_ms: float = 0.0
    fallbacks: int = 0


@dataclass(slots=True)
class RunResult:
    arm: str
    scenario: str
    success: bool
    elapsed_seconds: float
    planner_requests: int
    planner_input_tokens: int
    planner_output_tokens: int
    planner_total_tokens: int
    router_requests: int
    router_elapsed_ms: float
    router_fallbacks: int
    tool_calls: int
    steps: int


class CountingProvider:
    def __init__(self, inner: OpenRouterJevProvider, usage: RouterUsage) -> None:
        self._inner = inner
        self._usage = usage

    async def choose(
        self,
        *,
        state: str,
        instructions: str,
        criteria: Mapping[str, str],
    ) -> ChoiceDecision:
        self._usage.requests += 1
        started = time.perf_counter()
        try:
            return await self._inner.choose(
                state=state,
                instructions=instructions,
                criteria=criteria,
            )
        finally:
            self._usage.elapsed_ms += (time.perf_counter() - started) * 1000


class PlannerClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        usage: Usage,
    ) -> None:
        self._model = model
        self._usage = usage
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def select_and_argue(
        self,
        *,
        goal: str,
        observation: str,
        transcript: Sequence[str],
        tools: Sequence[ToolDescriptor],
    ) -> tuple[str, dict[str, Any]]:
        registry = [
            {
                "name": tool.name,
                "description": tool.description,
                "schema": tool.schema,
            }
            for tool in tools
        ]
        prompt = (
            "You control a tiny coding-agent benchmark. Pick exactly one next tool and its "
            "arguments. Do not finish before run_tests has passed. Prefer reading evidence "
            "before editing. Return only JSON with shape "
            '{"tool":"tool_name","arguments":{...}}.\n\n'
            f"GOAL:\n{goal}\n\n"
            f"LATEST OBSERVATION:\n{observation}\n\n"
            f"RECENT TRANSCRIPT:\n{_transcript_text(transcript)}\n\n"
            f"TOOLS:\n{json.dumps(registry, separators=(',', ':'))}"
        )
        value = await self._json_call(prompt)
        tool = value.get("tool")
        arguments = value.get("arguments", {})
        if not isinstance(tool, str):
            raise RuntimeError("planner response did not include a string tool")
        if not isinstance(arguments, dict):
            raise RuntimeError("planner response arguments must be an object")
        return tool, arguments

    async def arguments_for(
        self,
        *,
        goal: str,
        observation: str,
        transcript: Sequence[str],
        tool: ToolDescriptor,
    ) -> dict[str, Any]:
        selected_tool = {
            "name": tool.name,
            "description": tool.description,
            "schema": tool.schema,
        }
        prompt = (
            "You are the reasoning/argument-generation part of a coding agent. A native "
            "router has already selected the next tool. Generate arguments only for that "
            "tool. Return only JSON with shape "
            '{"arguments":{...}}.\n\n'
            f"GOAL:\n{goal}\n\n"
            f"LATEST OBSERVATION:\n{observation}\n\n"
            f"RECENT TRANSCRIPT:\n{_transcript_text(transcript)}\n\n"
            f"SELECTED TOOL:\n{json.dumps(selected_tool, separators=(',', ':'))}"
        )
        value = await self._json_call(prompt)
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict):
            raise RuntimeError("planner response arguments must be an object")
        return arguments

    async def _json_call(self, prompt: str) -> dict[str, Any]:
        self._usage.requests += 1
        response = await self._client.post(
            PLANNER_URL,
            json={
                "model": self._model,
                "temperature": 0,
                "max_tokens": 900,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Be concise. Inspect before mutating, verify before finishing, "
                            "and output valid JSON only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            },
        )
        response.raise_for_status()
        data = response.json()
        usage = data.get("usage") or {}
        self._usage.input_tokens += int(
            usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
        )
        self._usage.output_tokens += int(
            usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
        )
        raw = data["choices"][0]["message"]["content"]
        if not isinstance(raw, str):
            raise RuntimeError("planner returned non-text content")
        return _parse_json_object(raw)


class MiniRepo:
    def __init__(self, scenario: Scenario) -> None:
        self.scenario = scenario
        self.files = dict(scenario.files)
        self.tests_passed = False

    def execute(self, tool: str, arguments: Mapping[str, Any]) -> tuple[str, bool]:
        if tool == "list_files":
            return "\n".join(sorted(self.files)), False

        if tool == "read_file":
            path = _required_string(arguments, "path")
            content = self.files.get(path)
            if content is None:
                return f"error: file not found: {path}", False
            return f"{path}:\n{content}", False

        if tool == "search_code":
            query = _required_string(arguments, "query")
            matches = []
            for path, content in sorted(self.files.items()):
                for index, line in enumerate(content.splitlines(), start=1):
                    if query in line:
                        matches.append(f"{path}:{index}:{line}")
            return "\n".join(matches) if matches else "no matches", False

        if tool == "replace_text":
            path = _required_string(arguments, "path")
            old = _required_string(arguments, "old")
            new = _required_string(arguments, "new")
            content = self.files.get(path)
            if content is None:
                return f"error: file not found: {path}", False
            if old not in content:
                return "error: old text not found; inspect the file and retry", False
            self.files[path] = content.replace(old, new, 1)
            self.tests_passed = False
            return f"updated {path}", False

        if tool == "run_tests":
            content = self.files.get(self.scenario.expected_path, "")
            self.tests_passed = any(
                fragment in content for fragment in self.scenario.accepted_fragments
            )
            if self.tests_passed:
                return "1 passed", False
            return self.scenario.failure, False

        if tool == "finish":
            if self.tests_passed:
                return "finished: verified", True
            return "cannot finish: run_tests has not passed", False

        return f"error: unknown tool {tool}", False


def _required_string(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing string argument: {key}")
    return value


def _parse_json_object(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"planner returned malformed JSON: {raw[:200]!r}") from None
        value = json.loads(raw[start : end + 1])
    if not isinstance(value, dict):
        raise RuntimeError("planner JSON response must be an object")
    return value


def _transcript_text(transcript: Sequence[str]) -> str:
    if not transcript:
        return "(empty)"
    return "\n".join(transcript[-6:])


def _tool_by_name(name: str) -> ToolDescriptor | None:
    return next((tool for tool in TOOLS if tool.name == name), None)


def _router_state(goal: str, observation: str, transcript: Sequence[str]) -> HarnessState:
    recent: list[ActionSummary] = []
    for item in transcript[-3:]:
        tool, _, outcome = item.partition(" -> ")
        recent.append(ActionSummary(tool=tool[:80], outcome=outcome[:240]))
    last_action = recent[-1].tool if recent else None
    return HarnessState(
        goal=goal,
        observation=observation[:800],
        last_action=last_action,
        recent_actions=recent,
        constraints=["Inspect before mutation", "Run tests before finish"],
    )


async def run_once(
    *,
    arm: str,
    scenario: Scenario,
    planner_model: str,
    api_key: str,
    max_steps: int,
    timeout_seconds: float,
) -> RunResult:
    planner_usage = Usage()
    router_usage = RouterUsage()
    planner = PlannerClient(
        api_key=api_key,
        model=planner_model,
        timeout_seconds=timeout_seconds,
        usage=planner_usage,
    )
    provider: OpenRouterJevProvider | None = None
    router: JevToolRouter | None = None

    if arm == "native":
        provider = OpenRouterJevProvider.from_config(
            OpenRouterConfig(timeout_seconds=min(timeout_seconds, 10.0))
        )
        counting = CountingProvider(provider, router_usage)
        router = JevToolRouter(
            counting,
            RoutingConfig(
                mode=RoutingMode.HYBRID,
                direct_execution_threshold=0.80,
                fallback_threshold=0.55,
                hierarchical_threshold=24,
            ),
        )

    repo = MiniRepo(scenario)
    transcript: list[str] = []
    observation = "Start the task."
    tool_calls = 0
    success = False
    started = time.perf_counter()

    try:
        for step in range(1, max_steps + 1):
            if arm == "baseline":
                tool_name, arguments = await planner.select_and_argue(
                    goal=scenario.goal,
                    observation=observation,
                    transcript=transcript,
                    tools=TOOLS,
                )
            else:
                assert router is not None
                decision = await router.route(
                    _router_state(scenario.goal, observation, transcript),
                    TOOLS,
                )
                if decision.fallback or decision.tool is None:
                    router_usage.fallbacks += 1
                    tool_name, arguments = await planner.select_and_argue(
                        goal=scenario.goal,
                        observation=observation,
                        transcript=transcript,
                        tools=TOOLS,
                    )
                else:
                    tool_name = decision.tool
                    descriptor = _tool_by_name(tool_name)
                    if descriptor is None:
                        raise RuntimeError(f"router selected unknown tool: {tool_name}")
                    if descriptor.schema.get("required"):
                        arguments = await planner.arguments_for(
                            goal=scenario.goal,
                            observation=observation,
                            transcript=transcript,
                            tool=descriptor,
                        )
                    else:
                        arguments = {}

            descriptor = _tool_by_name(tool_name)
            if descriptor is None:
                observation = f"error: planner selected unknown tool {tool_name!r}"
                transcript.append(f"{tool_name} -> {observation}")
                continue

            tool_calls += 1
            try:
                observation, finished = repo.execute(tool_name, arguments)
            except (TypeError, ValueError) as exc:
                observation = f"tool argument error: {exc}"
                finished = False

            transcript.append(f"{tool_name} -> {observation[:1200]}")
            if finished:
                success = True
                break
        else:
            step = max_steps
    finally:
        await planner.aclose()
        if provider is not None:
            await provider.aclose()

    return RunResult(
        arm=arm,
        scenario=scenario.name,
        success=success,
        elapsed_seconds=time.perf_counter() - started,
        planner_requests=planner_usage.requests,
        planner_input_tokens=planner_usage.input_tokens,
        planner_output_tokens=planner_usage.output_tokens,
        planner_total_tokens=planner_usage.total_tokens,
        router_requests=router_usage.requests,
        router_elapsed_ms=router_usage.elapsed_ms,
        router_fallbacks=router_usage.fallbacks,
        tool_calls=tool_calls,
        steps=step,
    )


def summarize(results: Sequence[RunResult]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for arm in ("baseline", "native"):
        rows = [row for row in results if row.arm == arm]
        if not rows:
            continue
        summary[arm] = {
            "runs": float(len(rows)),
            "success_rate": sum(row.success for row in rows) / len(rows),
            "planner_requests_mean": statistics.mean(row.planner_requests for row in rows),
            "planner_input_tokens_mean": statistics.mean(
                row.planner_input_tokens for row in rows
            ),
            "planner_output_tokens_mean": statistics.mean(
                row.planner_output_tokens for row in rows
            ),
            "planner_total_tokens_mean": statistics.mean(
                row.planner_total_tokens for row in rows
            ),
            "router_requests_mean": statistics.mean(row.router_requests for row in rows),
            "router_fallbacks_mean": statistics.mean(row.router_fallbacks for row in rows),
            "router_elapsed_ms_mean": statistics.mean(
                row.router_elapsed_ms for row in rows
            ),
            "tool_calls_mean": statistics.mean(row.tool_calls for row in rows),
            "elapsed_seconds_mean": statistics.mean(row.elapsed_seconds for row in rows),
        }
    return summary


def markdown_report(
    results: Sequence[RunResult],
    summary: Mapping[str, Mapping[str, float]],
    *,
    model: str,
    runs: int,
    seed: int,
) -> str:
    lines = [
        "# Native agent-loop benchmark",
        "",
        f"- Planner model: `{model}`",
        f"- Repetitions per scenario/arm: `{runs}`",
        f"- Seed: `{seed}`",
        f"- Scenarios: `{len(SCENARIOS)}`",
        "",
        "The baseline planner selects a tool and generates its arguments from the full "
        "registry on every step. The native arm calls Jev directly from the runtime; the "
        "planner receives only the selected tool when arguments are required, and receives "
        "the full registry only after a router fallback.",
        "",
        "| Mean per run | Baseline planner | Native Jev loop |",
        "| --- | ---: | ---: |",
    ]
    baseline = summary.get("baseline", {})
    native = summary.get("native", {})
    metrics = (
        ("Success rate", "success_rate", True),
        ("Planner requests", "planner_requests_mean", False),
        ("Planner input tokens", "planner_input_tokens_mean", False),
        ("Planner output tokens", "planner_output_tokens_mean", False),
        ("Planner total tokens", "planner_total_tokens_mean", False),
        ("Jev requests", "router_requests_mean", False),
        ("Jev routing ms", "router_elapsed_ms_mean", False),
        ("Jev fallbacks", "router_fallbacks_mean", False),
        ("Tool calls", "tool_calls_mean", False),
        ("Elapsed seconds", "elapsed_seconds_mean", False),
    )
    for label, key, percent in metrics:
        left = baseline.get(key, 0.0)
        right = native.get(key, 0.0)
        if percent:
            left_text = f"{left * 100:.1f}%"
            right_text = f"{right * 100:.1f}%"
        else:
            left_text = f"{left:.1f}"
            right_text = f"{right:.1f}"
        lines.append(f"| {label} | {left_text} | {right_text} |")

    if baseline and native and baseline.get("planner_total_tokens_mean", 0.0):
        base_tokens = baseline["planner_total_tokens_mean"]
        native_tokens = native["planner_total_tokens_mean"]
        delta = ((native_tokens / base_tokens) - 1.0) * 100
        lines.extend(
            [
                "",
                f"Planner-token delta for native vs baseline: **{delta:+.1f}%**.",
                "",
                "> This benchmark is intentionally small and synthetic. Treat it as an "
                "integration benchmark for the runtime architecture, not as a universal "
                "claim about coding-agent cost or latency.",
            ]
        )

    lines.extend(["", "## Per-run results", ""])
    lines.append(
        "| Arm | Scenario | Success | Planner tokens | Jev calls | Fallbacks | Tools | Seconds |"
    )
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for row in results:
        lines.append(
            f"| {row.arm} | {row.scenario} | {'yes' if row.success else 'no'} | "
            f"{row.planner_total_tokens} | {row.router_requests} | "
            f"{row.router_fallbacks} | {row.tool_calls} | {row.elapsed_seconds:.2f} |"
        )
    return "\n".join(lines) + "\n"


async def async_main(args: argparse.Namespace) -> int:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required")

    rng = random.Random(args.seed)
    schedule: list[tuple[str, Scenario]] = []
    for _ in range(args.runs):
        batch = list(SCENARIOS)
        rng.shuffle(batch)
        for scenario in batch:
            arms = ["baseline", "native"]
            rng.shuffle(arms)
            schedule.extend((arm, scenario) for arm in arms)

    results: list[RunResult] = []
    for index, (arm, scenario) in enumerate(schedule, start=1):
        print(f"[{index}/{len(schedule)}] {arm}: {scenario.name}", flush=True)
        result = await run_once(
            arm=arm,
            scenario=scenario,
            planner_model=args.model,
            api_key=api_key,
            max_steps=args.max_steps,
            timeout_seconds=args.timeout,
        )
        results.append(result)
        print(
            f"  success={result.success} planner_tokens={result.planner_total_tokens} "
            f"jev_calls={result.router_requests} fallbacks={result.router_fallbacks} "
            f"seconds={result.elapsed_seconds:.2f}",
            flush=True,
        )

    summary = summarize(results)
    payload = {
        "model": args.model,
        "runs_per_scenario_arm": args.runs,
        "seed": args.seed,
        "results": [asdict(row) for row in results],
        "summary": summary,
    }
    report = markdown_report(
        results,
        summary,
        model=args.model,
        runs=args.runs,
        seed=args.seed,
    )
    print("\n" + report)

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if args.markdown_out:
        path = Path(args.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare full-planner tool calling with a runtime-native Jev loop."
    )
    parser.add_argument("--model", default="openrouter/free", help="OpenRouter planner model")
    parser.add_argument("--runs", type=int, default=3, help="repetitions per scenario and arm")
    parser.add_argument("--seed", type=int, default=20260925)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--json-out")
    parser.add_argument("--markdown-out")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.runs < 1:
        raise SystemExit("--runs must be >= 1")
    if args.max_steps < 1:
        raise SystemExit("--max-steps must be >= 1")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
