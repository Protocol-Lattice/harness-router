<p align="center">
  <img src="./assets/harness-router-logo.png" alt="Harness Router">
</p>

**Choose the next tool inside an existing agent loop.**

Harness Router takes a goal, the current observation, and a set of available tools,
then returns a tool choice, confidence, or a fallback to the host planner. It uses
Jev through OpenRouter for discrete decisions, caches reusable choices, and supports
optional Monte Carlo Tree Search (MCTS) over simulated outcomes.

The host harness generates arguments, checks permissions, executes the tool, and
processes its result. Router supplies the next-tool decision at the lifecycle
boundary the host exposes.

**Status:** alpha · **Python:** 3.11+ · **License:** MIT

[Website](https://harness-router.vercel.app/) · [Quick start](#quick-start) ·
[Integrations](#integrations) · [MCP](#mcp) · [Python API](#python-api) · [MCTS](#mcts)

## Quick start

Install the CLI and optional MCP support with `uv`:

```bash
uv tool install --force --with 'mcp>=2,<3' \
  'git+https://github.com/Protocol-Lattice/harness-router.git@main'

export OPENROUTER_API_KEY="your-key"
```

The package exposes `har`, `harness-router`, and `harness-router-mcp`.
`har` and `harness-router` are aliases for the same CLI. The default provider model
is `typesafe/jev-1.13`.

Route a choice among tools:

```bash
har route \
  --goal "Fix the failing parser test" \
  --observation "Failure points to src/parser.py" \
  --tools-json '[
    {"name":"read_file","description":"Read a repository file","category":"inspect","risk":"low"},
    {"name":"search_code","description":"Search repository source","category":"inspect","risk":"low"},
    {"name":"run_tests","description":"Run tests","category":"verify","risk":"low"}
  ]'
```

An illustrative response:

```json
{
  "tool": "read_file",
  "category": "inspect",
  "confidence": 0.93,
  "fallback": false,
  "fallback_reason": null,
  "provider": "openrouter",
  "provider_requests": 1
}
```

Use the selected tool through your harness's normal execution path. When
`fallback` is `true`, return control to the planner and inspect `fallback_reason`.
The CLI example supplies descriptors only; Router does not implement those tools.

## How it works

```text
goal + observation + last action + available tools
                       │
                       ▼
               cached decision?
                       │ miss
                       ▼
                   Jev choice
                       │
                       ├── confident choice ──► next tool
                       ├── uncertain choice ──► planner fallback
                       └── optional simulator ─► MCTS ─► next tool
                                                      │
                                                      ▼
                                    host arguments, permissions, execution
                                                      │
                                                      ▼
                                             new observation
```

For large registries, Jev can choose a category first and then a tool within that
category. The default library configuration considers this above 24 tools and
uses an input-size estimate to decide whether hierarchy is worthwhile.

`JevToolRouter.route()` performs cached Jev routing. MCTS is a separate search path:
invoke it explicitly, or configure a graph provider for the shared hook bridge.
Search predicts tool outcomes through a simulator and returns the first action.

## Integrations

The adapters connect Router to each host's available lifecycle callbacks. Current
hook integrations precompute a next-tool decision before a model request or after
a tool result. `PreToolUse` validates that stored decision locally, without making
another Jev request.

| Harness | Installer provider | Decision boundary and behavior |
| --- | --- | --- |
| Codex | `codex` | Discovers tools at `SessionStart`; precomputes at `UserPromptSubmit` and `PostToolUse`; a confident mismatch at `PreToolUse` requests a re-plan. |
| [Claude Code](.claude/README.md) | `claude` | Uses the same discovery, prompt, result, and validation callbacks; a confident mismatch requests a re-plan. |
| [ohmypi](.omp/README.md) | `ohmypi` | Reads the runtime tool registry, routes at `before_agent_start` and `tool_result`, and uses `setActiveTools()` to expose the selected tool. |
| [Antigravity](.antigravity/README.md) | `antigravity` | Uses `PreInvocation` to expose or seed a decision, `PostToolUse` to update it, and `PreToolUse` to validate it. |
| [DeepSeek Harness](hooks/deepseek/README.md) | `deepseek` | Uses the `dsh-hooks-codex` pre/post execution bridge with a supplied catalog and goal context. |

The shared `Stop` hook can request another step when a fresh, confident next-tool
decision remains. Its default decision age limit is 15 seconds.

### Install project hooks

Install the package and set `OPENROUTER_API_KEY` first. The executable and key must
be available in the environment of the harness process.

From a local checkout, install into a target project:

```bash
git clone https://github.com/Protocol-Lattice/harness-router.git
cd harness-router

python3 scripts/install_hook.py \
  --provider claude \
  --project /path/to/project \
  --source .

# Complete the current Claude Code adapter installation.
cp .claude/hooks/pre_decision.py .claude/hooks/stop.py \
  /path/to/project/.claude/hooks/
```

Replace `claude` with the provider from the table. Only Codex and Claude Code need
the extra copy step. For Codex, use `.codex` in the copy command and install at the
target Git repository root. The current installer
copies the shared hooks but omits the Codex and Claude Code `pre_decision.py` and
`stop.py` adapter scripts; the copy step supplies those registered callbacks.

The installer merges hook configuration, backs up changed files as
`*.harness-router.bak`, and adds provider-specific generated files to `.gitignore`.
Also ignore `.harness-router/`, which stores shared session state. Start a new
harness session after installation.

`--provider both` selects Codex and Claude Code. `--provider all` selects every
supported harness. Supply the two adapter scripts for each selected Codex or
Claude Code integration.

ohmypi installation requires Bun to install its development dependencies and run
the extension type check. `--skip-ohmypi-deps` copies the files without that setup.
Start ohmypi from the project root so it discovers `.omp/extensions`.

For DeepSeek Harness, supply the actual tool descriptors in
`.dsh/harness-router-tools.json` and start with:

```bash
dsh --patch .dsh/harness-router.patch.yml
```

The bridge needs a goal from the hook payload or an existing session decision to
precompute the next choice. Missing catalog or goal context leaves normal host
behavior in place. Adapter READMEs provide inventory and host setup details;
their older descriptions of routing inside `PreToolUse` predate the shared
precomputation flow described here.

### Hook configuration and session state

The shared bridge uses these environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_ROUTER_BIN` | `harness-router` on `PATH` | CLI executable for routing in installed projects. |
| `HARNESS_ROUTER_PREDECISION_TIMEOUT` | `4` | Precomputation deadline in seconds. |
| `HARNESS_ROUTER_POSTTOOL_TIMEOUT` | `4` | Post-tool bridge deadline in seconds. |
| `HARNESS_ROUTER_PREDECISION_THRESHOLD` | `0.85` | Minimum confidence for enforcing a stored choice. |
| `HARNESS_ROUTER_PREDECISION_TTL` | `30` | Stored-choice age limit in seconds for validation hooks. |
| `HARNESS_ROUTER_PREDECISION_CACHE_TTL` | `300` | Shared state-cache lifetime in seconds. |
| `HARNESS_ROUTER_PREDECISION_CACHE_SIZE` | `512` | Maximum shared state-cache entries per session. |
| `HARNESS_ROUTER_PREDECISION_BYPASS_CACHE` | unset | Set to `1` to bypass the shared state-cache lookup. |
| `HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD` | unset | Command that supplies a simulated graph for optional MCTS. |
| `HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD` | `0.80` | Confidence threshold considered for MCTS escalation. |
| `HARNESS_ROUTER_STOP_DECISION_TTL` | `15` | Stored-choice age limit for stop continuation. |
| `HARNESS_ROUTER_STOP_CONFIDENCE` | `0.85` | Minimum confidence for requesting continuation. |

Hook precomputations use a state key covering the goal, observation, last action,
and compact tool catalog. Decisions and traces are stored under:

```text
.harness-router/sessions/<session_id>.decision.json
.harness-router/sessions/<session_id>.decisions.jsonl
.harness-router/sessions/<session_id>.predecision-cache.json
```

The decision records include goal and observation text. Routing sends compact
state and tool descriptions to the configured provider; keep secrets out of those
inputs.

## MCP

`harness-router-mcp` runs a stdio MCP server with two tools:

| Tool | Inputs | Result |
| --- | --- | --- |
| `route` | Goal, tool descriptors, optional observation and last action. | Next tool, confidence, fallback, and reason. |
| `route_mcts` | Root state, simulated states and transitions, search budget, optional Jev prior. | First tool, confidence, fallback, principal variation, and search diagnostics. |

For a host using TOML MCP configuration:

```toml
[mcp_servers.harness-router]
command = "harness-router-mcp"
```

Pass `OPENROUTER_API_KEY` through the host's environment configuration. Server
initialization and tool discovery work without it; `route` returns a
`missing_openrouter_api_key` fallback when the key is absent. MCTS can run locally
with `use_jev_prior=false`.

Use MCP for explicit routing calls. For agents that load skills, the
[Harness Router skill](skills/harness-router/SKILL.md) describes selective use for
ambiguous choices.

## CLI

```bash
har route --help
har route-mcts --help
```

`route` requires `--goal` and a non-empty `--tools-json` array. Each descriptor
needs a unique tool name; descriptions, categories, and risk levels help the
router distinguish candidates. Risk values are `low`, `medium`, `high`, and
`critical`.

| Option | Default | Effect |
| --- | --- | --- |
| `--mode` | `hybrid` | Select `hybrid`, `jev_only`, or `planner_only`. |
| `--direct-threshold` | `0.85` | Confidence required to accept a choice in hybrid mode. |
| `--fallback-threshold` | `0.60` | Confidence below which a choice falls back to the planner. |
| `--hierarchical-threshold` | `24` | Tool count above which category-first routing is considered. |
| `--last-action` | unset | Include the previous tool name in routing state. |
| `--verbose` | off | Pretty-print the JSON response. |
| `--no-daemon` | off | Route in the current process. |
| `--no-cache` | off | Disable the library router's decision caches for this request. |

On Unix-like systems, repeated CLI requests can reuse a local daemon's HTTP
connections and in-memory caches. Daemon responses also include `daemon` and
`cache_hit`. Set `HARNESS_ROUTER_NO_DAEMON=1` to disable CLI daemon use through the
environment.

The `route` command exits with `0` when a provider request was attempted and `2`
when none was attempted, including cache hits. Read the JSON `fallback` field to
determine whether routing produced a usable selection.

## Python API

Install the library into your application's Python environment from a checkout:

```bash
python -m pip install -e .
```

Basic Jev routing needs only the core dependencies. Add `.[mcp]` if the application
also uses the MCP server or the graph-based CLI command.

```python
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


async def main():
    tools = [
        ToolDescriptor(
            name="read_file",
            description="Read a repository file",
            category="inspect",
            risk=RiskLevel.LOW,
        ),
        ToolDescriptor(
            name="search_code",
            description="Search repository source",
            category="inspect",
            risk=RiskLevel.LOW,
        ),
    ]

    async with OpenRouterJevProvider.from_config(OpenRouterConfig()) as provider:
        router = JevToolRouter(provider, RoutingConfig())
        decision = await router.route(
            HarnessState(
                goal="Fix the failing parser test",
                observation="Failure points to src/parser.py",
            ),
            tools,
        )

        if decision.fallback:
            print("Planner fallback:", decision.fallback_reason)
        else:
            print(decision.tool, decision.confidence)


asyncio.run(main())
```

`RoutingConfig` controls confidence thresholds, hierarchy, context limits, and
caches. Its modes behave as follows:

| Mode | Behavior |
| --- | --- |
| `hybrid` | Accept choices at or above the direct threshold; return planner fallbacks for uncertainty and handled provider/router errors. |
| `jev_only` | Accept choices at or above the fallback threshold; propagate provider/router errors. |
| `planner_only` | Return a planner fallback without asking Jev. |

The library includes a short-lived cache that reuses high-confidence choices for
an unchanged goal and tool set, even when observations change. Set
`RoutingConfig(obvious_cache_size=0)` to disable that reuse; the shared hook bridge
uses a separate cache that includes observations in its key.

## MCTS

Use MCTS when the best first action depends on possible downstream outcomes.
Supply predicted states and transitions; the search does not execute real tools.

This complete CLI example runs locally without an API key. It compares inspecting
source with repeating a failing test in a small illustrative graph:

```bash
har route-mcts <<'JSON'
{
  "root_state": "start",
  "use_jev_prior": false,
  "simulations": 64,
  "max_depth": 3,
  "states": [
    {
      "id": "start",
      "goal": "Locate the parser failure",
      "tools": [
        {"name": "read_file", "description": "Inspect the failing source", "risk": "low"},
        {"name": "run_tests", "description": "Repeat the failing test", "risk": "low"}
      ]
    },
    {"id": "located", "goal": "Locate the parser failure"},
    {"id": "unchanged", "goal": "Locate the parser failure"}
  ],
  "transitions": [
    {"from_state": "start", "tool": "read_file", "to_state": "located", "reward": 1.0, "terminal": true},
    {"from_state": "start", "tool": "run_tests", "to_state": "unchanged", "reward": 0.0, "terminal": true}
  ]
}
JSON
```

The graph-based CLI requires the MCP extra. It accepts JSON on stdin or through
`--graph-json`. The MCP `route_mcts` tool accepts the same graph fields and budgets
from 1 to 4,096 simulations and depths from 1 to 8.

For custom Python simulators, implement the async `SearchEnvironment` methods
`tools`, `transition`, and `evaluate`, then use `MCTSToolRouter`. A transition
returns a `SimulatedStep` containing the predicted state, reward, and terminal flag.
`MCTSConfig` defaults to 64 simulations, depth 4, and at most one Jev policy
evaluation per search.

MCTS confidence is the selected root action's share of visits. The principal
variation is a predicted sequence; execute the first selected action through the
host, then route again using the real result.

## Performance and fallback behavior

Routing helps when several tools plausibly fit the current step or the registry
is large. An obvious tool choice may need no additional routing call. Evaluate
task success, retries, total latency, token usage, and provider cost across the
whole harness loop.

The [controlled benchmark](BENCHMARK.md) found increased token usage and elapsed
time for the tested skill-driven Codex integration. It also describes a separate
native-loop benchmark. Those results depend on the tested tasks and integration;
they do not establish a general performance improvement for hooks or custom hosts.

Hybrid library routing returns planner fallbacks on low confidence and handled
provider errors. MCP reports missing credentials as a fallback. Hook adapters
are designed to leave normal host behavior available when routing context or a
usable decision is missing; ohmypi restores its original active tool set.

Confidence describes a routing choice. The host still applies its permissions,
approvals, credentials, sandbox, and execution policy.

## Development

From a checkout:

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
mypy
```

For ohmypi extension checks:

```bash
cd .omp
bun install --frozen-lockfile --ignore-scripts
bun run typecheck
```

## License

[MIT](LICENSE) © 2026 Protocol Lattice.
