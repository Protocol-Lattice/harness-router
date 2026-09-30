<p align="center">
  <img src="./assets/harness-router-logo.png" width="520" alt="Harness Router">
</p>

<h1 align="center">Harness Router</h1>

<p align="center">
  <strong>A decision layer for AI tool routing.</strong>
</p>

<p align="center">
  Intercept the host harness at its native control point, route the next tool with cache + Jev,
  and escalate ambiguous multi-step choices to bounded MCTS.
</p>

<p align="center">
  <a href="https://harness-router.vercel.app/">Website</a>
  ·
  <a href="#architecture">Architecture</a>
  ·
  <a href="#codex">Codex</a>
  ·
  <a href="#claude-code">Claude Code</a>
  ·
  <a href="#ohmypi">ohmypi</a>
  ·
  <a href="#antigravity">Antigravity</a>
  ·
  <a href="#deepseek-harness">DeepSeek Harness</a>
  ·
  <a href="#mcp">MCP</a>
</p>

---

## What is Harness Router?

AI agents repeatedly answer one operational question:

> **Which tool should happen next?**

Harness Router is a framework-agnostic decision layer for that question.

It receives compact state plus candidate tools and returns a routing decision:

```text
state + candidate tools
        │
        ▼
   Harness Router
        │
   ┌────┴────┐
   │         │
 cache      Jev
   │         │
   │      ambiguous
   │         │
   │        MCTS
   └────┬────┘
        ▼
   next tool
```

The router is deliberately narrower than an agent planner.

It does **not** own:

- user intent,
- model reasoning,
- tool argument generation,
- permissions,
- approvals,
- sandboxing,
- tool execution.

It owns the **tool-selection decision**.

---

# Architecture

## The routing pipeline

The decision engine uses the cheapest applicable path first:

```text
                 routing request
                       │
                       ▼
                ┌─────────────┐
                │ Route cache │
                └──────┬──────┘
                       │ miss
                       ▼
                ┌─────────────┐
                │     Jev     │
                │ fast route  │
                └──────┬──────┘
                       │
              low confidence /
             deeper decision
                       │
                       ▼
                ┌─────────────┐
                │    MCTS     │
                │ bounded     │
                │ local search│
                └──────┬──────┘
                       │
                       ▼
                 route decision
```

### Cache

Repeated compact decisions can be served without invoking the decision provider.

### Jev

Jev is the normal routing path for ambiguous tool choices.

### MCTS

MCTS is an optional escalation path when the best first action depends on downstream state. Search runs against a side-effect-free simulator; real tools are never executed during simulation.

---

# Harness integration

Harness Router does **not** pretend that every harness exposes the same control flow.

Each adapter uses the strongest native control point that the host actually provides.

There are two integration patterns:

1. **Control-point interception** — the harness has a pre-tool hook. Router evaluates the pending call and can allow it or request a re-plan.
2. **Tool-surface control** — the harness lets the extension change the tools visible to the next model request. Router can directly constrain the next decision.

```text
                         HOST HARNESS
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  user/task → model reasoning → tool selection → execution  │
│                                  │                          │
│                                  │ native hook              │
│                                  ▼                          │
│                         ┌─────────────────┐                 │
│                         │ Harness Router  │                 │
│                         │                 │                 │
│                         │ cache → Jev     │                 │
│                         │        → MCTS   │                 │
│                         └────────┬────────┘                 │
│                                  │                          │
│                    allow / constrain / re-plan              │
│                                  │                          │
│                                  ▼                          │
│                            tool execution                   │
│                                  │                          │
│                                  ▼                          │
│                            tool result                      │
│                                  │                          │
│                                  └──► next model iteration  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

The exact loop is host-specific.

---

## Codex

Codex integration uses:

- `SessionStart` for tool discovery,
- `PreToolUse` for routing,
- deny + re-plan when Router identifies a different high-confidence candidate.

```text
Codex model
    │
    ▼
proposes tool call
    │
    ▼
PreToolUse
    │
    ▼
Harness Router
    │
    ├── cache
    ├── Jev
    └── optional MCTS
    │
    ├── same / fallback / error ──► allow original call
    │
    └── different confident tool ─► deny once
                                      │
                                      ▼
                                Codex re-plans
                                      │
                                      ▼
                                  new call
                                      │
                                      ▼
                                  execute
                                      │
                                      ▼
                                tool result
                                      │
                                      └────► Codex
```

`SessionStart` builds the tool catalog once per session. The routing hook uses a relevant subset instead of sending the complete registry on every decision.

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider codex
```

The installer preserves existing hooks and settings and creates backups when modifying files.

---

## Claude Code

Claude Code uses:

- `SessionStart` for live tool discovery,
- `PreToolUse` as the routing interception point,
- deny + re-plan for a confident alternative.

```text
Claude model
    │
    ▼
tool proposal
    │
    ▼
PreToolUse
    │
    ▼
Harness Router
    │
    ├── cache → Jev → optional MCTS
    │
    ├── same / fallback ──────► normal permission flow
    │
    └── different tool ───────► deny + re-plan
                                  │
                                  ▼
                              Claude model
                                  │
                                  ▼
                              next call
```

The hook never executes the selected tool and never rewrites its arguments.

See [the Claude integration guide](.claude/README.md).

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider claude
```

---

## ohmypi

ohmypi exposes a stronger control point than a simple pre-execution hook.

The extension can call `setActiveTools()` before the next provider request, so the router can constrain the tool surface the model sees.

```text
user / agent turn
       │
       ▼
before_agent_start
       │
       ▼
Harness Router
       │
       ├── cache
       ├── Jev
       └── optional MCTS
       │
       ▼
setActiveTools([selected])
       │
       ▼
next provider request
       │
       ▼
model selects from controlled tools
       │
       ▼
tool_call
       │
       ├── selected tool ──► execute
       │                       │
       │                       ▼
       │                   tool_result
       │                       │
       │                       ▼
       │                 next router decision
       │                       │
       │                       └──► next provider request
       │
       └── unexpected tool ──► defensive block
```

This is the closest integration to directly controlling the next-tool decision because the selected tool can be installed into the active tool surface before inference.

The original active-tool set is restored on fallback, timeout, low confidence, malformed output, or router failure.

See [the ohmypi integration guide](.omp/README.md).

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider ohmypi
```

---

## Antigravity

Antigravity uses its native `PreToolUse` hook.

```text
Antigravity model
    │
    ▼
tool proposal
    │
    ▼
PreToolUse
    │
    ▼
live tool inventory
    │
    ▼
Harness Router
    │
    ├── cache → Jev → optional MCTS
    │
    ├── same / fallback ──────► normal execution
    │
    └── different tool ───────► deny + re-plan
                                  │
                                  ▼
                            Antigravity
                                  │
                                  ▼
                              next call
```

The adapter reads the live conversation tool inventory. It does not invent a fallback catalog when discovery fails.

`Stop` is used to reset the redirect guard.

See [the Antigravity integration guide](.antigravity/README.md).

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider antigravity
```

---

## DeepSeek Harness

DeepSeek Harness reaches the router through its supported Codex hook bridge:

```text
DeepSeek model
    │
    ▼
tool proposal
    │
    ▼
tools/pre-execute
    │
    ▼
dsh-hooks-codex
    │
    ▼
Harness Router
    │
    ├── same / fallback / error ──► allow
    │
    └── different tool ───────────► deny + re-plan
                                      │
                                      ▼
                               DeepSeek Harness
                                      │
                                      ▼
                                  next call
```

The command-hook bridge does not provide a faithful live tool registry, so DeepSeek integration requires a supplied catalog and fails open when a valid routing context is unavailable.

Install:

```bash
python3 hooks/deepseek/install.py
dsh --patch .dsh/harness-router.patch.yml
```

See [the DeepSeek integration guide](hooks/deepseek/README.md).

---

# MCP

Harness Router also exposes routing as a native MCP server.

Start it with:

```bash
harness-router-mcp
```

Available tools:

| Tool | Purpose |
| --- | --- |
| `route` | Fast next-tool routing |
| `route_mcts` | Bounded multi-step routing |

Example Codex configuration:

```toml
[mcp_servers.harness-router]
command = "harness-router-mcp"
```

MCP is useful when the host does not need an automatic hook integration or when an agent should invoke routing explicitly.

---

# Installation

Requires **Python 3.11+**.

```bash
uv tool install --force --with 'mcp>=2,<3' \
  'git+https://github.com/Protocol-Lattice/harness-router.git@main'
```

Set the provider key:

```bash
export OPENROUTER_API_KEY="your-key"
```

Default decision model:

```text
typesafe/jev-1.13
```

---

# CLI

A minimal routing request:

```bash
har route \
  --goal "Fix the failing parser test" \
  --observation "Failure points to src/parser.py" \
  --tools-json '[
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
      "name": "run_tests",
      "description": "Run tests",
      "category": "verify",
      "risk": "low"
    }
  ]'
```

Example result:

```json
{
  "tool": "read_file",
  "confidence": 0.93,
  "fallback": false
}
```

---

# Python API

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

# MCTS

Use `route_mcts` when the first tool depends on what may happen later.

The simulations are local:

```text
router
  │
  ▼
policy prior
  │
  ▼
local simulator
  │
  ├── state
  ├── candidate tools
  ├── transition
  ├── reward
  └── terminal condition
```

There is no real shell execution, file mutation, browser action, or network side effect during search.

Example:

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

A simulator should model possible transitions rather than execute real tools.

---

# Routing policy

Harness Router is designed for **known alternatives**.

Good routing decisions:

- read vs search,
- one MCP tool vs another,
- inspect vs mutate,
- test vs inspect,
- one browser action vs another,
- which small action advances the current state.

Keep these in the main planner:

- code generation,
- patch generation,
- complex command construction,
- long-form argument generation,
- open-ended planning,
- interpretation of user intent,
- authorization.

The intended division is:

```text
planner
   │
   │ goal + compact state
   ▼
Harness Router
   │
   │ next tool
   ▼
planner
   │
   │ arguments
   ▼
execution policy
   │
   ▼
tool
   │
   ▼
result
   │
   └──────────────► next iteration
```

---

# Large tool registries

Routing becomes more useful when many tools overlap semantically.

Harness Router can use hierarchical routing:

```text
                  current state
                       │
                       ▼
                  category route
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
     inspect         mutate         verify
        │              │              │
    read/search     write/patch     test/lint
```

Common categories include:

`inspect` · `mutate` · `execute` · `verify` · `git` · `browser` · `memory` · `network` · `finish`

The route cache can then reuse repeated compact decisions.

---

# Safety and execution policy

Routing and authorization are separate.

A high-confidence decision means:

> This is the best candidate from the supplied tool set.

It does **not** mean:

> This action is authorized.

The host harness remains responsible for:

- permissions,
- approval prompts,
- sandboxing,
- credentials,
- execution,
- user confirmation.

Harness Router does not bypass those controls.

Routing failures are designed to fail open where the host integration supports it: if discovery, routing, or the decision provider is unavailable, the original host behavior continues unless the specific adapter contract says otherwise.

---

# Skill mode

For agents without automatic hook integration, the repository also provides:

```text
skills/harness-router/SKILL.md
```

Skill mode is explicit rather than automatic:

```text
agent
  │
  ▼
decides to route
  │
  ▼
Harness Router
  │
  ▼
next tool
```

Use hooks when you want routing integrated into the host's tool lifecycle. Use the skill when the agent should invoke routing selectively.

---

# When to use it

Harness Router is most useful when:

- several tools perform similar operations,
- the registry is large,
- wrong-tool selection causes retries,
- tool calls are expensive,
- the harness exposes a native hook,
- a small decision layer can be cheaper than repeatedly asking the main model to choose.

If an agent has only a few obvious tools, routing may add unnecessary overhead.

Benchmark the **whole agent loop**, not just router latency:

- task success,
- tool-selection accuracy,
- wrong-tool rate,
- retries,
- latency,
- token usage,
- provider cost.

---

# Development

```bash
git clone https://github.com/Protocol-Lattice/harness-router.git
cd harness-router
python -m pip install -e ".[dev]"
```

Run checks:

```bash
pytest
ruff check .
mypy
```

---

# Project status

Harness Router is **alpha**.

The project is focused on the decision layer between an agent's current state and its next tool call. Host adapters may evolve as their hook and extension APIs change.

The architecture intentionally avoids claiming that one adapter can replace an entire agent harness. The router owns routing; the host owns the rest of the agent lifecycle.

---

<p align="center">
  <strong>One router. Multiple harnesses.</strong>
  <br>
  Cache → Jev → MCTS
</p>

<p align="center">
  <a href="https://harness-router.vercel.app/">harness-router.vercel.app</a>
</p>

## License

MIT.
