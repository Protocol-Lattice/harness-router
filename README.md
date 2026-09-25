# harness-router

**Framework-agnostic Jev tool routing for agentic harnesses.**

<code>harness-router</code> uses [TypeSafeAI Jev](https://www.typesafe.ai/) through the [OpenRouter Decisions API](https://openrouter.ai/) as a fast **System-1 tool-selection layer**.

Instead of asking a large reasoning model to decide which tool to call on every step, give Jev a compact harness state and a fixed set of candidate tools. Jev chooses the next action; your main planner remains responsible for deep reasoning, free-form argument generation, code generation, and ambiguous tasks.

~~~text
User goal + observation
        |
        v
  harness-router
        |
        +---- high confidence ----> selected tool
        |
        +---- low/uncertain ------> planner fallback
~~~

## Why harness-router?

Agentic systems often spend an expensive model call on a decision that is fundamentally discrete:

- read a file or search the repository?
- inspect or mutate?
- run tests or continue editing?
- click, navigate, or extract?
- which MCP/UTCP tool should run next?

<code>harness-router</code> separates **tool selection** from **reasoning and execution**.

The router is intentionally small:

- Jev-backed discrete routing through OpenRouter
- hybrid Jev + planner fallback
- hierarchical routing for larger tool registries
- confidence-aware decisions
- MCP, UTCP, and generic tool adapters
- conservative risk metadata
- optional loop detection for long-running harnesses
- no dependency on an MCP or UTCP SDK
- async Python API
- CLI aliases: <code>harness-router</code> and <code>har</code>

> Jev confidence is never authorization. Keep your normal execution policy, permissions, approval gates, and sandbox boundaries around tool execution.

## Installation

Requires **Python 3.11+**.

~~~bash
pip install harness-router
~~~

For development:

~~~bash
git clone https://github.com/Protocol-Lattice/harness-router.git
cd harness-router
python -m pip install -e ".[dev]"
~~~

## OpenRouter setup

The default provider uses:

~~~text
model:    typesafe/jev-1.13
endpoint: https://openrouter.ai/api/alpha/decisions
env:      OPENROUTER_API_KEY
~~~

Set your API key:

~~~bash
export OPENROUTER_API_KEY="your-key"
~~~

Do not commit or log the key.

## CLI

After installation, both commands are available:

~~~bash
harness-router --version
har --version
~~~

Route a tool directly from the terminal:

~~~bash
har route \
  --goal "Fix the failing parser test" \
  --observation "The failing assertion references src/parser.py" \
  --tools-json '[
    {
      "name": "read_file",
      "description": "Read a repository file by path",
      "category": "inspect",
      "risk": "low"
    },
    {
      "name": "search_code",
      "description": "Search source code for a symbol or text",
      "category": "inspect",
      "risk": "low"
    },
    {
      "name": "write_file",
      "description": "Replace a repository file with new content",
      "category": "mutate",
      "risk": "medium"
    }
  ]'
~~~

Example response:

~~~json
{
  "category": "inspect",
  "confidence": 0.93,
  "fallback": false,
  "fallback_reason": null,
  "probabilities": {
    "__fallback__": 0.01,
    "read_file": 0.93,
    "search_code": 0.05,
    "write_file": 0.01
  },
  "tool": "read_file"
}
~~~

The exact probabilities depend on the current state and Jev response.

## Python quick start

~~~python
import asyncio

from harness_router import (
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RiskLevel,
    RoutingConfig,
    ToolDescriptor,
)


async def main() -> None:
    provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
    router = JevToolRouter(provider, RoutingConfig())

    tools = [
        ToolDescriptor(
            name="read_file",
            description="Read a repository file by path",
            category="inspect",
            risk=RiskLevel.LOW,
        ),
        ToolDescriptor(
            name="search_code",
            description="Search repository source code",
            category="inspect",
            risk=RiskLevel.LOW,
        ),
        ToolDescriptor(
            name="write_file",
            description="Replace a repository file",
            category="mutate",
            risk=RiskLevel.MEDIUM,
        ),
    ]

    try:
        decision = await router.route(
            HarnessState(
                goal="Fix the failing parser test",
                observation="The failure points to src/parser.py",
            ),
            tools,
        )

        if decision.fallback:
            print("Planner fallback:", decision.fallback_reason)
        else:
            print("Selected:", decision.tool)
            print("Confidence:", decision.confidence)
    finally:
        await provider.aclose()


asyncio.run(main())
~~~

## The routing model

<code>HarnessState</code> keeps the decision context intentionally compact:

~~~python
HarnessState(
    goal="Fix the failing parser test",
    observation="The assertion references src/parser.py",
    last_action="search_code",
    constraints=["Do not modify generated files"],
)
~~~

A candidate action is represented by <code>ToolDescriptor</code>:

~~~python
ToolDescriptor(
    name="read_file",
    description="Read a repository file by path",
    category="inspect",
    risk=RiskLevel.LOW,
)
~~~

The result is a <code>RouteDecision</code> containing:

- selected tool, when one is appropriate
- inferred or explicit category
- confidence
- per-choice probabilities
- fallback flag
- fallback reason

## Routing modes

Three routing modes are available.

### Hybrid

Recommended default.

~~~python
from harness_router import RoutingConfig, RoutingMode

config = RoutingConfig(
    mode=RoutingMode.HYBRID,
    direct_execution_threshold=0.85,
    fallback_threshold=0.60,
)
~~~

Default behavior:

| Confidence | Result |
| --- | --- |
| < 0.60 | planner fallback |
| 0.60 - 0.85 | planner confirmation |
| >= 0.85 | tool can be routed directly, subject to your execution policy |

### Jev only

~~~python
RoutingConfig(mode=RoutingMode.JEV_ONLY)
~~~

Provider/router errors propagate instead of silently falling back to the planner.

### Planner only

~~~python
RoutingConfig(mode=RoutingMode.PLANNER_ONLY)
~~~

The router immediately returns a planner fallback decision. No Jev provider is required.

## Hierarchical routing

Large flat tool lists are harder to route efficiently.

When the number of available tools exceeds <code>hierarchical_threshold</code> (default: <code>8</code>), <code>JevToolRouter</code> automatically performs category-first routing:

~~~text
                 +--> inspect --> read_file / search_code / list_files
Harness state -->+--> mutate  --> write_file / patch_file
                 +--> verify  --> run_tests / lint
                 +--> git     --> diff / commit
~~~

First Jev selects a category, then it selects a tool inside that category.

You can provide categories explicitly or let the built-in adapter infer common categories such as:

- <code>inspect</code>
- <code>mutate</code>
- <code>execute</code>
- <code>verify</code>
- <code>git</code>
- <code>browser</code>
- <code>memory</code>
- <code>network</code>
- <code>finish</code>
- <code>general</code>

## Tool adapters

You do not need to convert every tool registry manually.

### Generic tools

~~~python
from harness_router import normalize_tools

tools = normalize_tools([
    {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            }
        }
    }
])
~~~

### MCP

~~~python
from harness_router import MCPToolAdapter, normalize_tools

tools = normalize_tools(mcp_tools, adapter=MCPToolAdapter())
~~~

### UTCP

~~~python
from harness_router import UTCPToolAdapter, normalize_tools

tools = normalize_tools(utcp_tools, adapter=UTCPToolAdapter())
~~~

The adapters normalize tool names, descriptions, schemas, categories, and risk metadata without requiring an MCP or UTCP SDK dependency.

## Safety and execution policy

Routing and execution are deliberately separate.

A high-confidence Jev decision means:

> "This is probably the best candidate tool."

It does **not** mean:

> "This action is authorized."

The package includes a conservative <code>DefaultExecutionPolicy</code>:

~~~python
from harness_router import DefaultExecutionPolicy

policy = DefaultExecutionPolicy()
allowed = await policy.allow(decision, tool)
~~~

By default:

- low-risk tools may be allowed
- medium-risk tools require an explicit configured confidence threshold
- high- and critical-risk tools are denied

For production harnesses, implement your own <code>ExecutionPolicy</code> around the permissions and approval model of your application.

## Keep argument generation in the planner

Jev should choose among known alternatives.

Good routing questions:

- read vs search
- inspect vs mutate
- which browser action to take
- which MCP/UTCP tool to call
- run tests vs inspect another file
- which small fixed action advances the current state

Keep these tasks in the main reasoning model:

- generating source code
- writing patches
- constructing non-trivial shell commands
- generating long free-form arguments
- open-ended planning
- interpreting ambiguous intent
- deciding user authorization

A typical agent loop looks like this:

~~~text
1. Main planner creates/updates the goal
2. Harness builds compact state
3. harness-router chooses the next tool
4. Main planner generates required arguments
5. Execution policy checks permission
6. Harness executes the tool
7. Result becomes the next observation
8. Repeat
~~~

## Stateful routing and loop detection

The core router is stateless.

For long-running agent loops, <code>RoutingSession</code> adds:

- route-step counting
- configurable maximum route steps
- repeated-action detection
- A/B loop detection

~~~python
from harness_router import RoutingSession

session = RoutingSession(router)

decision = await session.route(state, tools)

loop_detected = session.record_execution(
    "read_file",
    arguments={"path": "src/parser.py"},
    result_class="success",
)

if loop_detected:
    # Escalate to the planner, change strategy, or stop.
    ...
~~~

The session reports loops; your harness decides what to do next.

## Custom provider

<code>JevToolRouter</code> depends on the small <code>DecisionProvider</code> protocol rather than directly on OpenRouter.

A custom provider only needs to implement:

~~~python
async def choose(
    *,
    state: str,
    instructions: str,
    criteria: Mapping[str, str],
) -> ChoiceDecision:
    ...
~~~

This keeps the routing layer provider-agnostic while <code>OpenRouterJevProvider</code> provides the default Jev integration.

## Codex skill

This repository includes a ready-to-use skill at:

~~~text
skills/harness-router/SKILL.md
~~~

For Codex, copy the <code>skills/harness-router</code> directory into a location Codex scans for skills, or expose that directory through your existing Codex skill configuration.

The skill tells Codex to use Jev for **tool selection**, while retaining Codex for reasoning and free-form argument generation.

It also contains a direct routing helper:

~~~bash
python skills/harness-router/scripts/route.py \
  --goal "Fix the failing parser test" \
  --observation "The failing assertion points to src/parser.py" \
  --tools-json '[
    {"name":"read_file","description":"Read a source file","category":"inspect","risk":"low"},
    {"name":"search_code","description":"Search repository text","category":"inspect","risk":"low"},
    {"name":"write_file","description":"Write a source file","category":"mutate","risk":"medium"}
  ]'
~~~

## Configuration

### RoutingConfig

~~~python
RoutingConfig(
    mode=RoutingMode.HYBRID,
    direct_execution_threshold=0.85,
    fallback_threshold=0.60,
    hierarchical_threshold=8,
    max_same_action_repeats=2,
    max_route_steps=50,
    description_limit=320,
)
~~~

### OpenRouterConfig

~~~python
OpenRouterConfig(
    model="typesafe/jev-1.13",
    url="https://openrouter.ai/api/alpha/decisions",
    api_key_env="OPENROUTER_API_KEY",
    timeout_seconds=5.0,
)
~~~

## Error handling

In hybrid mode, provider failures are converted into planner fallback decisions.

Common fallback reasons include:

- <code>planner_only</code>
- <code>no_tools</code>
- <code>no_matching_tool</code>
- <code>no_matching_category</code>
- <code>low_confidence</code>
- <code>low_category_confidence</code>
- <code>planner_confirmation</code>
- <code>provider_error</code>
- <code>router_error</code>
- <code>max_route_steps</code>

In <code>jev_only</code> mode, provider/router failures propagate so the harness can handle them explicitly.

## Architecture

~~~mermaid
flowchart LR
    A[Harness state] --> B[JevToolRouter]
    T[Tool registry] --> D[Adapters]
    D --> B

    B -->|small registry| J[Jev decision]
    B -->|large registry| C[Category decision]
    C --> J

    J --> P{Confidence policy}
    P -->|high| X[Selected tool]
    P -->|uncertain| F[Planner fallback]

    X --> E[Execution policy]
    E -->|allowed| R[Harness executor]
    E -->|denied| F
~~~

## Development

Install development dependencies:

~~~bash
python -m pip install -e ".[dev]"
~~~

Run tests:

~~~bash
pytest
~~~

Lint:

~~~bash
ruff check .
~~~

Type-check:

~~~bash
mypy
~~~

## Project status

<code>harness-router</code> is currently **alpha** software. APIs may still evolve as integrations with real coding, browser, computer-use, MCP, and UTCP harnesses are exercised.

If you build an integration, benchmark the complete harness loop rather than assuming routing automatically improves latency or cost.

## License

MIT.
