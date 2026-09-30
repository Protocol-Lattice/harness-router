<p align="center">
  <img src="./assets/harness-router-logo.png" width="520" alt="Harness Router">
</p>

<h1 align="center">Harness Router</h1>

<p align="center">
  <strong>The decision layer between your AI agent and its tools.</strong>
</p>

<p align="center">
  Precompute the next decision between tool steps with a hook — or invoke routing only when you need it with a skill.
  <br>
  Fast Jev decisions for ordinary ambiguity. Bounded MCTS when the next move has consequences.
</p>

<p align="center">
  <a href="https://harness-router.vercel.app/">Website</a>
  ·
  <a href="#codex-pretooluse-hook">Codex hook</a>
  ·
  <a href="#claude-code-pretooluse-hook">Claude Code hook</a>
  ·
  <a href="#ohmypi-tool-call-hook">ohmypi hook</a>
  ·
  <a href="#antigravity-pretooluse-hook">Antigravity hook</a>
  ·
  <a href="#codex-skill">Codex skill</a>
  ·
  <a href="#native-mcp-server">MCP</a>
</p>

---

## Your agent already has tools.

The hard part is choosing the right one.

When an agent sees several plausible actions, the main model often spends another expensive reasoning step deciding whether to:

- read or search,
- inspect or mutate,
- test or keep editing,
- navigate or extract,
- call tool A, B, or C.

**Harness Router moves that decision into a dedicated routing layer.**

```text
Agent wants to call a tool
          |
          v
   Harness Router
          |
    +-----+------+
    |            |
   route      route_mcts
    |            |
  fast        deeper search
    |            |
    +-----+------+
          |
          v
    selected tool
```

The planner still does the hard work: reasoning, coding, arguments, interpretation, and execution.

Harness Router decides **which tool should run next**.

---

## One router. Two ways in.

### 1. Hook — own the next tool decision

Hook integrations keep a **state → next-tool** decision ready before the next
model step. The hand-off is host-specific:

```text
                         Harness Router
                              |
                         next tool
                              |
        +---------------------+----------------------+
        |                     |                      |
      Codex                 Claude               Antigravity
        |                     |                      |
  PostToolUse           PostToolUse           PreInvocation
  context injection     context injection     context injection
        |                     |                      |
        +---------------------+----------------------+
                              |
                         model step
                              |
                         tool execution
                              |
                         PostToolUse
                              |
                              +----> Harness Router
```

There are two enforcement levels:

**Direct tool-surface control.** ohmypi exposes a runtime setActiveTools() API,
so the extension narrows the active tool set to the Router-selected tool before
the next provider request. The model can therefore generate arguments for the
Router-selected tool without choosing among the full registry. The extension
restores the original active set, observes the result, and routes the next state.

**Hook enforcement/context.** Codex and Claude Code do not expose an equivalent
project hook that renames the model's already-generated tool choice into another
tool. Their adapters therefore precompute after each tool result, inject the next
Router decision into the next model context, and keep PreToolUse as a local
safety/consistency gate. Antigravity uses its native PreInvocation checkpoint
for the same purpose.

This means Harness Router owns the **tool-selection policy**, while the adapter
uses the strongest control point the host exposes. It does not claim to replace
the host's internal model sampler through hooks alone.

```text
PostToolUse  →  Router(cache/Jev/MCTS)  →  next decision
                                      |
                    +-----------------+------------------+
                    |                                    |
             set active tool                     inject / gate
                    |                                    |
                 ohmypi                       Codex / Claude / Antigravity
```

The hooks are **fail-open**: routing failures never bypass the harness's normal
permissions or execution policy.
### Hook decision latency

All five hook integrations use the same `harness-router-mcp` routing core, so the decision itself is not a different model per harness. The latest recorded ordinary-routing benchmark measured the shared `route` decision at **404.5 ms mean / 332 ms median / 718 ms p95** over 24 choices. The 4,096-simulation `route_mcts` benchmark measured **388 ms mean / 372.5 ms median / 545 ms p95**.

| Hook | Routing decision | Measured router latency |
| --- | --- | ---: |
| Codex | `route` → optional `route_mcts` | **332 ms median** (`route`) |
| Claude Code | `route` → optional `route_mcts` | **332 ms median** (`route`) |
| ohmypi | `route` → optional `route_mcts` | **332 ms median** (`route`) |
| DeepSeek Harness | `route` → optional `route_mcts` | **332 ms median** (`route`) |
| Antigravity | `route` → optional `route_mcts` | **332 ms median** (`route`) |

These are **shared router-core measurements**, not five separate end-to-end hook benchmarks. Hook startup, catalog discovery, process spawning, harness scheduling, and provider/network conditions can add overhead that varies by harness. The benchmark timing includes MCP/provider overhead for the router call but not the surrounding hook wrapper.

At runtime, every hook adapter writes one JSON timing record per invocation to `stderr` (`event: harness_router.hook_timing`, with `hook` and `duration_ms`). This measures the full adapter call, including input parsing, local preparation, routing, and writing the hook response. The hook protocol on `stdout` is unchanged. Capture each harness's `stderr` as JSON Lines to calculate observed per-hook mean, median, and p95 latency; these runtime measurements include the wrapper overhead omitted by the shared router-core benchmark above.

See [`benchmark-results/decision-latency-2026-09-26.md`](benchmark-results/decision-latency-2026-09-26.md) and [`benchmark-results/mcts-4096-decision-latency-2026-09-26.md`](benchmark-results/mcts-4096-decision-latency-2026-09-26.md).

### Persistent router daemon

The Codex, Claude Code, ohmypi, Antigravity, and DeepSeek Harness hooks share one local daemon on macOS and Linux. You normally do not need to start it yourself: launch the harness as usual, and the first routed tool call starts the daemon through the `harness-router route` CLI fallback. Later hook calls connect directly to the daemon's Unix socket, avoiding a new router CLI process for each decision. The daemon inherits the environment of the process that starts it, so make sure `OPENROUTER_API_KEY` is available before the first routed call.

To start it manually from a Harness Router checkout, run this from the repository root, then launch the harness in another terminal:

```bash
export OPENROUTER_API_KEY="your-key"
uv run python -m harness_router.daemon
```

`python -m harness_router.daemon` works when Harness Router is installed in that exact Python environment. A system Python that only has the repository checkout on disk cannot import the `src/harness_router` package by itself.

The command stays in the foreground while the daemon runs. The default socket is `/tmp/harness-router-<uid>.sock`; set `HARNESS_ROUTER_SOCKET` in both the daemon and harness environments to use a different path. Set `HARNESS_ROUTER_NO_DAEMON=1` to make hooks and the CLI bypass the daemon. Windows uses the direct CLI path because Unix domain sockets are unavailable there.

### 2. Skill — route only when useful

Use the included Harness Router skill when you want routing to stay explicit and selective.

Good for:

- ambiguous tool choices,
- overlapping MCP tools,
- expensive tool registries,
- experiments and benchmarks,
- agents where you do not want global interception.

**Hook for always-on routing. Skill for opt-in routing.**

---

## Why Harness Router?

Because tool selection is usually a **decision problem**, not a generation problem.

Harness Router gives you:

- ⚡ **Fast `route` path** for ordinary tool ambiguity
- 🌳 **Bounded `route_mcts`** for multi-step decisions
- 🪝 **Codex, Claude Code, ohmypi, Antigravity, and DeepSeek Harness hooks** for interception before execution
- 🧠 **TypeSafeAI Jev** through OpenRouter Decisions
- 🔌 **Native MCP server**
- 🧩 **Framework-agnostic Python API**
- 🗂️ **Automatic tool discovery** for Codex sessions
- 🏗️ **Hierarchical routing** for larger registries
- 💾 **Bounded route cache**
- 🛡️ **Risk metadata and execution-policy separation**
- 🔁 **Loop detection** for long-running agents
- 🧯 **Planner fallback** when confidence is low

> Routing is not authorization. Your sandbox, approval gates, permissions, and execution policy still decide whether a tool is allowed to run.

---

## Install

Requires **Python 3.11+**.

```bash
uv tool install --force --with 'mcp>=2,<3'   'git+https://github.com/Protocol-Lattice/harness-router.git@main'
```

Set your OpenRouter key:

```bash
export OPENROUTER_API_KEY="your-key"
```

Default decision model:

```text
typesafe/jev-1.13
```

The default provider uses the OpenRouter Decisions API.

---

## 30-second demo

Start the MCP server:

```bash
harness-router-mcp
```

Or route directly from the CLI:

```bash
har route   --goal "Fix the failing parser test"   --observation "The failure points to src/parser.py"   --tools-json '[
    {
      "name": "read_file",
      "description": "Read a repository file",
      "category": "inspect",
      "risk": "low"
    },
    {
      "name": "search_code",
      "description": "Search repository source",
      "category": "inspect",
      "risk": "low"
    },
    {
      "name": "write_file",
      "description": "Replace a repository file",
      "category": "mutate",
      "risk": "medium"
    }
  ]'
```

Example:

```json
{
  "tool": "read_file",
  "confidence": 0.93,
  "fallback": false
}
```

That is the core idea:

**give the router a compact state + candidate tools → get the next tool.**

---

## Native MCP server

Harness Router exposes two MCP tools over stdio:

| Tool | Use it when |
| --- | --- |
| `route` | The next tool is ambiguous but mostly local |
| `route_mcts` | The first action depends on downstream consequences |

Add it to Codex:

```toml
[mcp_servers.harness-router]
command = "harness-router-mcp"
```

Then keep the routing instruction simple:

> Use `route` when several tools are genuinely plausible. Use `route_mcts` only when multi-step consequences matter and a side-effect-free simulation graph is available.

### Fast route payload

```json
{
  "goal": "Fix the failing parser test",
  "observation": "Failure points to src/parser.py",
  "tools": [
    {
      "name": "read_file",
      "description": "Read source",
      "category": "inspect",
      "risk": "low"
    },
    {
      "name": "search_code",
      "description": "Search repo",
      "category": "inspect",
      "risk": "low"
    },
    {
      "name": "run_tests",
      "description": "Run tests",
      "category": "verify",
      "risk": "low"
    }
  ]
}
```

Response:

```json
{
  "tool": "read_file",
  "confidence": 0.93,
  "fallback": false,
  "reason": null
}
```

---

## Codex PreToolUse hook

This repository ships a project-local Codex hook.

It turns Harness Router from "another tool the model may call" into a layer that can review **every pending tool call**.

### How it works

```text
SessionStart
    |
    v
discover_tools.py
    |
    +--> Codex app-server
    |      |
    |      +--> mcpServerStatus/list
    |
    v
.codex/harness-router-tools.json
    |
    v
PreToolUse
    |
    +--> pending tool
    +--> similar candidate tools
    +--> compact session context
    |
    v
harness-router-mcp
    |
    +--> same choice / fallback / error -> allow
    |
    +--> different confident choice -> deny once
                                      and ask Codex to re-plan
```

The catalog is discovered **once at session startup**.

For MCP tools, Harness Router can retain real metadata such as:

- tool name,
- description,
- input schema,
- output schema,
- annotations,
- source MCP server.

The PreToolUse hook uses a **relevant subset** of the discovered catalog rather than blindly sending the entire registry on every call.

### Install the hook in another repo

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider codex
```

Run from the Git repository root, or supply `--project /path/to/repo`. The same
installer supports `--provider claude`, `--provider ohmypi`, `--provider antigravity`,
and `--provider all`.
`--provider both` continues to mean Codex + Claude Code. It preserves other
hooks and settings, backs up changed files as `*.harness-router.bak`, and avoids
duplicate registrations on repeat runs. Use `--ref TAG_OR_COMMIT` to select a
version, or `--source /path/to/harness-router` to copy from a local checkout.

The installer adds generated state and backups to `.gitignore`:

```gitignore
.codex/harness-router-tools.json
.codex/harness-router/sessions/
*.harness-router.bak
```

Start a **new Codex session**.

The startup hook discovers tools and writes the session catalog automatically.

### Optional MCTS escalation

The hook starts with fast `route`.

It can escalate to `route_mcts` when:

- the fast route falls back or is below threshold,
- enough plausible candidates remain,
- a side-effect-free graph provider is configured.

```bash
export HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD="./scripts/build_mcts_graph.py"
export HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD="0.80"
```

No graph provider? No fake tree.

Harness Router stays on the fast path.

---

## Claude Code PreToolUse hook

The portable Claude Code integration lives in [`.claude/`](.claude/README.md):

```text
.claude/settings.json
.claude/hooks/discover_tools.py
.claude/hooks/pre_tool_use.py
```

It follows the same flow: build a session catalog, shortlist tools before a call,
run `route`, and optionally escalate to `route_mcts`. A confident alternative asks
Claude to re-plan, with at most one redirect per user turn to prevent loops.
Errors and fallbacks preserve the original call's normal permission checks.

Install `harness-router-mcp`, export `OPENROUTER_API_KEY`, and start a new Claude
Code session. For another project, run the universal installer above with
`--provider claude` to merge the hooks into its `.claude/settings.json`.

At `SessionStart`, the hook publishes a fresh live tool registry by querying the
Claude native server's complete `tools/list` inventory and its SDK control
interface for configured MCP tools without a model turn. The session registry
is available to `PreToolUse` and through `HARNESS_ROUTER_TOOL_REGISTRY` in Claude's
Bash commands. Run `python3 .claude/hooks/discover_tools.py --list-tools` to query
it directly as JSON. It uses no hard-coded built-in catalog, and accepts
supplemental descriptors through
`HARNESS_ROUTER_CLAUDE_TOOLS_FILE` or `HARNESS_ROUTER_CLAUDE_TOOLS_JSON`.
See the [Claude hook guide](.claude/README.md) for catalog format, configuration,
and installation details, using the [official hook format](https://code.claude.com/docs/en/hooks).

---

## ohmypi tool-call hook

The [ohmypi integration](.omp/README.md) runs inside the harness as a native extension:

```text
.omp/extensions/harness-router.ts
.omp/hooks/pre_tool_use.py
```

It reads the actual runtime catalog through `pi.getAllTools()`, including names,
descriptions, schemas, and source metadata, and filters it using
`pi.getActiveTools()`. Discovery needs no hard-coded built-ins or separate agent
process. The catalog is read again before each `tool_call`, so newly loaded and
disabled tools are reflected immediately.

The extension shortlists candidates, calls `route`, and optionally escalates to
`route_mcts` using the same graph-provider contract as the other hooks. A confident
alternative returns `{ block: true, reason }` to request one re-plan per user turn.
Errors, fallbacks, and unknown tools pass through with normal permissions intact.

Install in another project:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider ohmypi
```

Install `harness-router-mcp`, export `OPENROUTER_API_KEY`, and start `omp` from the
project root. ohmypi discovers `.omp/extensions` automatically; no settings edits
are needed. The installer requires Bun to install the pinned TypeScript dependencies
and run the extension's type check in `.omp`. Use `--skip-ohmypi-deps` for a
files-only installation. See the [ohmypi hook guide](.omp/README.md) for local installation,
catalog snapshots, settings, and limitations.

---

## Antigravity PreToolUse hook

The [Antigravity integration](.antigravity/README.md) installs native hooks into
`.agents/hooks.json` and registers `harness-router-mcp` in `.agents/mcp_config.json`.
Both **`route`** and **`route_mcts`** are available to Antigravity.

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider antigravity
```

Before routing, the hook reads the current conversation's live tool definitions
from Antigravity's local runtime, including names, descriptions and schemas.
It filters disabled MCP tools and publishes a session inventory. There is no
hard-coded tool list; unavailable discovery causes the hook to abstain.

The hook starts with `route`, optionally escalates to `route_mcts` through a
configured simulator, and asks for one re-plan on a confident alternative.
Antigravity retains argument generation, permissions and execution. The live
inventory adapter uses internal runtime APIs; see the guide for compatibility,
connection settings and explicit catalog overrides.

---

## DeepSeek Harness hook

DeepSeek Harness exposes a native `tools/pre-execute` interception point. Its
supported `@deepseek-ai/dsh-hooks-codex` bridge can run synchronous Codex
command hooks at that point, so Harness Router reuses the same interception
semantics instead of inventing a private DeepSeek hook protocol.

Install it in a project with:

```bash
python3 hooks/deepseek/install.py
```

Then launch DeepSeek Harness with:

```bash
dsh --patch .dsh/harness-router.patch.yml
```

The command hook reads a supplied DeepSeek tool catalog from
`HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON`, `HARNESS_ROUTER_DEEPSEEK_TOOLS_FILE`,
or `.dsh/harness-router-tools.json`. If the catalog or router is unavailable,
the hook fails open.

See [the DeepSeek hook guide](hooks/deepseek/README.md) for configuration and
limitations.

## Codex skill

The repository also ships:

```text
skills/harness-router/SKILL.md
```

Use the skill when you want the agent to call Harness Router selectively instead of intercepting every tool use.

The helper is intentionally conservative:

- skips obvious linear steps,
- routes genuine ambiguity,
- keeps argument generation in the planner,
- stops routing after planner fallback or override.

Direct helper:

```bash
python skills/harness-router/scripts/route.py   --goal "Fix the failing parser test"   --observation "The failing assertion points to src/parser.py"   --tools-json '[
    {
      "name": "read_file",
      "description": "Read a source file",
      "category": "inspect",
      "risk": "low"
    },
    {
      "name": "search_code",
      "description": "Search repository text",
      "category": "inspect",
      "risk": "low"
    }
  ]'
```

---

## MCTS without token explosion

`route_mcts` is for decisions where the best first tool depends on what may happen later.

The important detail:

**the simulations are local.**

Increasing the simulation count does **not** mean making one LLM/Jev request per simulation.

By default, Jev can be used as a policy prior at the first ambiguous node, while the bounded Monte Carlo search runs locally over a side-effect-free simulator.

```python
mcts = MCTSToolRouter(
    simulator,
    policy_router=router,
    config=MCTSConfig(
        simulations=64,
        max_depth=4,
        max_policy_evaluations=1,
    ),
)
```

Your simulator predicts:

- available tools,
- next state,
- immediate reward,
- terminal state,
- heuristic value.

It must **not** execute real writes, shell commands, browser mutations, or network side effects during search.

---

## Python API

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
    provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
    router = JevToolRouter(provider, RoutingConfig())

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

    try:
        decision = await router.route(
            HarnessState(
                goal="Fix the failing parser test",
                observation="Failure points to src/parser.py",
            ),
            tools,
        )

        print(decision.tool)
        print(decision.confidence)
    finally:
        await provider.aclose()


asyncio.run(main())
```

---

## Routing philosophy

Harness Router should choose among **known alternatives**.

Good routing questions:

- read vs search,
- inspect vs mutate,
- which browser action,
- which MCP tool,
- run tests vs inspect again,
- which small fixed action advances the state.

Keep these in the main planner:

- generating code,
- writing patches,
- constructing non-trivial commands,
- long-form argument generation,
- open-ended planning,
- interpreting ambiguous user intent,
- authorization decisions.

A good agent loop looks like:

```text
planner defines goal
      |
      v
build compact state
      |
      v
tool choice obvious? ---- yes ----> use it
      |
      no
      |
      v
 Harness Router
      |
      +--> route
      |
      +--> route_mcts when deeper search is justified
      |
      v
planner generates arguments
      |
      v
execution policy
      |
      v
run tool
```

---

## Routing modes

### Hybrid

Recommended default.

```python
RoutingConfig(
    mode=RoutingMode.HYBRID,
    direct_execution_threshold=0.85,
    fallback_threshold=0.60,
)
```

| Confidence | Result |
| --- | --- |
| < 0.60 | planner fallback |
| 0.60–0.85 | planner confirmation |
| >= 0.85 | route directly, subject to execution policy |

### Jev only

```python
RoutingConfig(mode=RoutingMode.JEV_ONLY)
```

Provider/router errors propagate.

### Planner only

```python
RoutingConfig(mode=RoutingMode.PLANNER_ONLY)
```

No Jev provider required.

---

## Large tool registries

Large flat registries become noisy.

Harness Router can switch to category-first routing when that is estimated to save enough input.

```text
                     +--> inspect --> read / search / list
                     |
Harness state ------>+--> mutate  --> write / patch
                     |
                     +--> verify  --> test / lint
                     |
                     +--> git     --> diff / commit
```

Common inferred categories include:

`inspect` · `mutate` · `execute` · `verify` · `git` · `browser` · `memory` · `network` · `finish` · `general`

Repeated identical compact routes can also be served from a bounded in-process LRU cache.

---

## Safety

A high-confidence route means:

> "This is probably the best candidate tool."

It does **not** mean:

> "This action is authorized."

Keep execution policy separate.

The package includes `DefaultExecutionPolicy`, but production harnesses should wrap routing with their own permissions, approval model, and sandbox rules.

The Codex hook never bypasses Codex sandboxing or approval prompts.

---

## Development

```bash
git clone https://github.com/Protocol-Lattice/harness-router.git
cd harness-router
python -m pip install -e ".[dev]"
```

Run the checks:

```bash
pytest
ruff check .
mypy
```

---

## Project status

Harness Router is **alpha**.

The API will evolve as it is tested against real coding agents, MCP hosts, browser agents, computer-use systems, and larger tool registries.

If you benchmark it, benchmark the **whole agent loop** — latency, cost, tool accuracy, retries, and task success — not routing in isolation.

---

## Built for tool-heavy agents

If your agent has three tools, you may not need Harness Router.

If it has thirty, three hundred, or several tools that all look almost identical to a language model, routing starts to become infrastructure.

**That is the layer Harness Router is trying to own.**

<p align="center">
  <strong>One router. Two ways in.</strong>
  <br>
  Skill or hook.
</p>

<p align="center">
  <a href="https://harness-router.vercel.app/">harness-router.vercel.app</a>
</p>

## License

MIT.
