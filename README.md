<p align="center">
  <img src="./assets/harness-router-logo.png" width="520" alt="Harness Router">
</p>

<h1 align="center">Harness Router</h1>

<p align="center">
  <strong>The decision layer between your AI agent and its tools.</strong>
</p>

<p align="center">
  Route every tool call with a hook — or invoke routing only when you need it with a skill.
  <br>
  Fast Jev decisions for ordinary ambiguity. Bounded MCTS when the next move has consequences.
</p>

<p align="center">
  <a href="https://harness-router.vercel.app/">Website</a>
  ·
  <a href="#codex-pretooluse-hook">Codex hook</a>
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

### 1. Hook — route every tool call

Use the Codex `PreToolUse` hook to intercept pending tool calls before execution.

Harness Router compares the tool Codex chose against the session's discovered tool catalog.

```text
Codex chooses a tool
        |
        v
    PreToolUse
        |
        v
 Harness Router
        |
   same choice? -------- yes ------> allow
        |
        no
        |
        v
 ask Codex to re-plan
```

The hook is **fail-open**: if routing fails, Codex keeps its original choice.

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
- 🪝 **Codex `PreToolUse` hook** for interception before execution
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
mkdir -p .codex/hooks

curl -fsSL   https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/.codex/hooks.json   -o .codex/hooks.json

curl -fsSL   https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/.codex/hooks/discover_tools.py   -o .codex/hooks/discover_tools.py

curl -fsSL   https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/.codex/hooks/pre_tool_use.py   -o .codex/hooks/pre_tool_use.py

chmod +x .codex/hooks/discover_tools.py .codex/hooks/pre_tool_use.py
```

Add generated state to `.gitignore`:

```gitignore
.codex/harness-router-tools.json
.codex/harness-router/sessions/
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
