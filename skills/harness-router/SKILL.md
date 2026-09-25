---
name: harness-router
description: Use Protocol Lattice harness-router with OpenRouter Jev to choose the next tool or action in an agentic harness. Use when working on coding agents, browser/computer-use agents, MCP harnesses, or generic tool registries, or when the user asks to route tool selection through Jev instead of the main planner.
---

# Harness Router

Use this skill when the task involves integrating or using `harness-router` as a fast tool-selection layer.

The router uses **TypeSafeAI Jev through OpenRouter Decisions API** as a System-1 decision layer. It should choose *which tool/action to use next*. Keep deep reasoning, source-code generation, patches, shell-command generation, and other free-form content in the main planner/model.

## Preconditions

1. Prefer an existing local checkout of `harness-router` when working inside this repository:
   ```bash
   python -m pip install -e .
   ```
2. In another project, install the package when available:
   ```bash
   python -m pip install harness-router
   ```
3. Require `OPENROUTER_API_KEY` for live Jev routing.
4. Never print, log, echo, or commit the API key.

## Default model and endpoint

Use:

```text
model: typesafe/jev-1.13
endpoint: https://openrouter.ai/api/alpha/decisions
api key env: OPENROUTER_API_KEY
```

Do not replace the Decisions API with `/chat/completions` for tool routing.

## Preferred routing mode

Default to hybrid routing:

```python
RoutingConfig(
    mode=RoutingMode.HYBRID,
    direct_execution_threshold=0.85,
    fallback_threshold=0.60,
)
```

Interpret decisions as follows:

- confidence >= 0.85: candidate for direct routing
- 0.60 <= confidence < 0.85: fall back to the main planner for confirmation
- confidence < 0.60: fall back to the main planner

Confidence is not authorization. Always preserve the harness's execution policy and approval rules.

## Workflow

When routing an actual harness step:

1. Build a compact `HarnessState` from:
   - user goal
   - latest observation
   - last action
   - short recent-action history
   - relevant constraints
2. Convert currently available tools to `ToolDescriptor`.
3. Prefer existing adapters:
   - `MCPToolAdapter` for MCP tool definitions
   - `GenericToolAdapter` for generic dictionaries
4. Assign conservative risk levels. If unsure, do not mark a mutating tool as low risk.
5. Call `JevToolRouter.route(...)`.
6. If `decision.fallback` is true, resume normal Codex/planner reasoning.
7. If a tool is selected, validate that:
   - the tool still exists
   - required arguments can be produced
   - execution policy allows it
8. Use the main model to generate free-form tool arguments such as code, patches, shell commands, or long text.
9. Execute the tool only through the harness's normal executor/approval boundary.
10. Feed the result back as the next compact observation.

## Fast CLI routing

For a quick routing decision from Codex, run the bundled script:

```bash
python skills/harness-router/scripts/route.py \
  --goal "Fix the failing parser test" \
  --observation "The failing assertion points to src/parser.py" \
  --tools-json '[
    {"name":"read_file","description":"Read a source file","category":"inspect","risk":"low"},
    {"name":"search_code","description":"Search repository text","category":"inspect","risk":"low"},
    {"name":"write_file","description":"Write a source file","category":"mutate","risk":"medium"}
  ]'
```

The script prints a JSON object containing the selected tool, confidence, probabilities, category, and fallback status.

Do not follow a selected mutating/high-risk action merely because the script returned high confidence. Apply normal Codex/harness permissions and user approval requirements.

## Python integration

Use this pattern:

```python
from harness_router import (
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RoutingConfig,
    RoutingMode,
)

provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
router = JevToolRouter(
    provider,
    RoutingConfig(mode=RoutingMode.HYBRID),
)

decision = await router.route(state, tools)

if decision.fallback:
    # Continue with the normal planner/model.
    ...
else:
    # Resolve arguments, check policy, then execute through the harness.
    ...
```

Close the provider when finished:

```python
await provider.aclose()
```

## What Jev should decide

Good Jev decisions:

- read vs search vs run tests
- browser click vs navigation vs extraction
- inspect vs mutate vs verify
- which MCP or generic tool should run next
- which small fixed action should be selected from a known set

Keep these in the planner instead:

- writing source code
- generating a patch
- composing a shell command with nontrivial arguments
- long-form reasoning
- open-ended planning
- interpreting ambiguous user intent that cannot be represented by the candidate tools

## Hierarchical routing

Do not manually flatten very large tool registries. `JevToolRouter` automatically uses category-first routing when the number of tools exceeds `hierarchical_threshold`.

Give tools useful categories where possible:

```text
inspect
mutate
execute
verify
git
browser
computer
memory
network
finish
```

For unknown tools, allow the library's adapter/category inference to normalize them.

## Failure behavior

In hybrid mode, provider failures should fall back to the normal planner rather than breaking the harness.

Treat these as fallback conditions:

- OpenRouter timeout/error
- malformed Jev response
- low confidence
- no matching tool
- no matching category
- repeated/no-progress tool loop
- action requiring unsupported reasoning

Do not hide fallback. Preserve the reason in logs or diagnostics.

## Final checks

Before finishing an integration:

1. Confirm `OPENROUTER_API_KEY` is read only from the environment.
2. Confirm Jev uses OpenRouter Decisions API.
3. Confirm tool choice and tool argument generation are separate.
4. Confirm policy/approval remains separate from Jev confidence.
5. Confirm planner fallback still works.
6. Run the project's tests.
7. Do not claim latency or cost improvements without measurements.
