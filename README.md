# Harness Router

<p align="center">
  <img src="./assets/harness-router-logo.png" width="520" alt="Harness Router">
</p>

<p align="center">
  <strong>A decision layer embedded into the harness tool-selection loop.</strong>
</p>

<p align="center">
  Harness Router intercepts the host harness at its native control point,
  evaluates the available tools, and routes the next tool with cache, Jev, and optional MCTS.
</p>

<p align="center">
  <a href="https://harness-router.vercel.app/">Website</a>
  ·
  <a href="#architecture">Architecture</a>
  ·
  <a href="#integrations">Integrations</a>
  ·
  <a href="#mcp">MCP</a>
  ·
  <a href="#python-api">Python API</a>
</p>

---

## The idea

Agent harnesses already have a tool-selection loop:

```text
goal
  │
  ▼
model reasoning
  │
  ▼
tool selection
  │
  ▼
tool execution
  │
  ▼
result
  │
  └──────────────► next iteration
```

Harness Router inserts a decision layer **inside that loop**:

```text
model proposes / harness reaches tool-selection point
                         │
                         ▼
                 ┌─────────────────┐
                 │ Harness Router  │
                 │                 │
                 │ cache → Jev     │
                 │        → MCTS   │
                 └────────┬────────┘
                          │
                    routing decision
                          │
                          ▼
                    next tool call
```

The host harness still owns the agent.

**Harness Router owns the tool-selection decision.**

It does not replace the harness's:

- reasoning,
- user intent handling,
- argument generation,
- permissions,
- approvals,
- sandboxing,
- credentials,
- execution,
- result handling.

This separation is intentional.

---

# Architecture

## Decision path

Every routing request follows the cheapest applicable path first:

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
                 ambiguous /
              downstream-dependent
                       │
                       ▼
                ┌─────────────┐
                │    MCTS     │
                │   bounded   │
                │ local search│
                └──────┬──────┘
                       │
                       ▼
                 routing decision
```

### Cache

Repeated compact decisions can be served without invoking a decision provider.

### Jev

Jev is the normal decision path for tool-selection choices.

### MCTS

MCTS is an optional escalation path for decisions where the best immediate action depends on possible downstream state.

MCTS operates against a side-effect-free simulator. Real tools are not executed during search.

---

# Integrations

Harness Router is framework-agnostic, but integrations use the **native control point available in each host**.

The control point determines how directly Router can influence the next tool.

## Codex

Codex uses:

- `SessionStart` for tool discovery,
- `PreToolUse` for routing,
- deny + re-plan when Router identifies a different confident candidate.

```text
Codex
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
  ├── cache
  ├── Jev
  └── optional MCTS
  │
  ├── same / fallback / error ──► allow
  │
  └── different confident tool ─► deny
                                      │
                                      ▼
                                  re-plan
                                      │
                                      ▼
                                  next call
```

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider codex
```

---

## Claude Code

Claude Code uses:

- `SessionStart` for tool discovery,
- `PreToolUse` as the routing interception point,
- deny + re-plan for a confident alternative.

```text
Claude Code
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
    ├── same / fallback ──────► normal flow
    │
    └── different tool ───────► deny + re-plan
                                  │
                                  ▼
                              next call
```

The hook does not execute tools or rewrite their arguments.

See [the Claude integration guide](.claude/README.md).

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider claude
```

---

## ohmypi

ohmypi exposes a stronger control point through its active tool surface.

The integration can call `setActiveTools()` before the next provider request:

```text
agent turn
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
tool execution
    │
    ▼
next routing decision
```

The active tool set is restored on fallback, timeout, low confidence, malformed output, or router failure.

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
Antigravity
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
    ├── same / fallback ──────► allow
    │
    └── different tool ───────► deny + re-plan
```

The adapter uses the live conversation tool inventory and does not invent a fallback catalog when discovery fails.

See [the Antigravity integration guide](.antigravity/README.md).

Install:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider antigravity
```

---

## DeepSeek Harness

DeepSeek Harness reaches Router through its supported Codex hook bridge.

```text
DeepSeek Harness
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
```

The command-hook bridge does not provide a faithful live tool registry, so this integration requires a supplied catalog and fails open when a valid routing context is unavailable.

Install:

```bash
python3 hooks/deepseek/install.py
dsh --patch .dsh/harness-router.patch.yml
```

See [the DeepSeek integration guide](hooks/deepseek/README.md).

---

# MCP

Harness Router is also available as a native MCP server.

Start it with:

```bash
harness-router-mcp
```

Available tools:

| Tool | Purpose |
| --- | --- |
| `route` | Fast next-tool routing |
| `route_mcts` | Bounded multi-step routing |

Example:

```toml
[mcp_servers.harness-router]
command = "harness-router-mcp"
```

MCP provides an explicit routing interface for hosts that do not use automatic hook integration.

---

# Installation

Requires **Python 3.11+**.

```bash
uv tool install --force --with 'mcp>=2,<3' \
  'git+https://github.com/Protocol-Lattice/harness-router.git@main'
```

Set the OpenRouter key:

```bash
export OPENROUTER_API_KEY="your-key"
```

Default decision model:

```text
typesafe/jev-1.13
```

---

# CLI

Route a tool-selection decision directly:

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

Use `route_mcts` when the immediate choice depends on possible downstream outcomes.

```text
current state
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
     │
     ▼
best first action
```

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

The simulator should model possible transitions rather than execute real tools.

---

# What Router decides

Harness Router is designed for **known alternatives at a tool-selection point**.

Typical decisions:

- read vs search,
- one MCP tool vs another,
- inspect vs mutate,
- test vs inspect,
- one browser action vs another,
- which available action best advances the current state.

Keep these in the main planner:

- user-intent interpretation,
- code generation,
- patch generation,
- complex command construction,
- long-form argument generation,
- open-ended planning,
- authorization.

The intended division is:

```text
                 harness / planner
                       │
                 goal + state
                       │
                       ▼
                Harness Router
                       │
                    next tool
                       │
                       ▼
                 argument generation
                       │
                       ▼
                   permissions
                       │
                       ▼
                    execution
                       │
                       ▼
                     result
                       │
                       └────────► next iteration
```

---

# Large tool registries

Routing is particularly useful when many tools overlap semantically.

Hierarchical routing can reduce the decision surface:

```text
                 current state
                      │
                      ▼
                 category route
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
     inspect        mutate        verify
        │             │             │
   read/search    write/patch    test/lint
```

Common categories include:

`inspect` · `mutate` · `execute` · `verify` · `git` · `browser` · `memory` · `network` · `finish`

---

# Safety and failure behavior

Routing is not authorization.

A high-confidence routing decision means:

> This is the strongest candidate from the supplied tool set.

It does not mean:

> This action is authorized.

The host harness remains responsible for:

- permissions,
- approvals,
- sandboxing,
- credentials,
- execution,
- user confirmation.

Harness Router does not bypass those controls.

Where the host integration supports it, routing failures fail open: if discovery, the router, or the decision provider is unavailable, the original host behavior continues.

---

# Skill mode

For agents without automatic hook integration:

```text
skills/harness-router/SKILL.md
```

Skill mode is explicit:

```text
agent
  │
  ▼
invoke routing skill
  │
  ▼
Harness Router
  │
  ▼
next tool
```

Use hooks when routing should be embedded in the host lifecycle. Use the skill when routing should be invoked selectively.

---

# When to use Harness Router

Harness Router is useful when:

- several tools can plausibly satisfy the same step,
- the tool registry is large,
- wrong-tool selection causes retries,
- tool calls are expensive,
- the host exposes a useful control point,
- a dedicated decision layer can reduce work done by the main model.

If there are only a few obvious tools, routing can add unnecessary overhead.

Evaluate the **whole agent loop**, not router latency in isolation:

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

The project focuses on one boundary in an agentic system:

```text
current harness state
        │
        ▼
  tool-selection point
        │
        ▼
  Harness Router
        │
        ▼
     next tool
```

The host harness remains in control of the rest of the agent lifecycle.

---

<p align="center">
  <strong>A decision layer embedded into the harness tool-selection loop.</strong>
  <br>
  Cache → Jev → MCTS
</p>

<p align="center">
  <a href="https://harness-router.vercel.app/">harness-router.vercel.app</a>
</p>

## License

MIT.
