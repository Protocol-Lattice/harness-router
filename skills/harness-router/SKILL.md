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

## Cost-aware routing in Codex

Unmodified Codex pays a full extra model turn when it calls the routing helper, then usually
needs another turn to execute the selected tool or generate its arguments. Therefore **normal
Codex tool calling is the default fast path**. Treat the helper as an exception for expensive
ambiguity, not as a per-step router.

### Hard routing budget

- Default to **0 helper calls** for linear coding work.
- Use **at most 1 helper call per user task**.
- After any router fallback, provider error, or explicit planner override, do not call the
  helper again for that task.
- Never route the same state twice.
- Never call the helper merely to confirm a tool Codex has already selected.

### Call the helper only when all of these are true

- at least **4 next tools are genuinely plausible** after deterministic pruning, or a large
  registry still has no obvious shortlist
- the choice is closed and discrete rather than open-ended reasoning
- the ambiguity is high enough that a wrong choice would likely cause **multiple exploratory
  planner/tool turns**
- the latest observation does not already imply an exact next tool
- the candidate set can be represented compactly

When possible, pass only **4-12 plausible candidates**, not the entire registry. Omit input
schemas and long prose from the helper payload; use only name, one-clause description,
category, and risk. The main planner already has the schemas needed for execution.

### Skip the helper for the common coding fast path

Do not route obvious sequences such as:

- exact file/path known -> read it
- exact symbol known -> search/read it
- edit is already determined -> patch/write it
- edit completed -> run the focused test
- focused test passed -> run the broader test suite
- test failed with a precise file/line -> inspect that location

Also skip routing when the selected tool would only introduce an extra planner turn without
resolving real ambiguity, for example choosing `write_file` when there is already one obvious
write target and Codex must immediately generate the patch anyway.

### Fast Codex helper profile

The bundled `scripts/route.py` intentionally uses a more aggressive profile than the library
defaults:

```text
direct threshold:       0.72
fallback threshold:     0.72
hierarchical threshold: 48
provider timeout:       2.0s
description limit:      96 chars
state field limit:      400 chars
history limit:          1 action
constraint limit:       2
```

Equal direct/fallback thresholds remove the `planner_confirmation` confidence band for the
Codex helper. A useful Jev choice is returned directly; genuinely low-confidence/no-match
decisions fall back. This avoids paying for Jev merely to ask Codex to confirm Jev.

The higher hierarchical threshold keeps medium-sized registries in a single flat Jev request
instead of category-first + tool-selection requests. The shorter timeout bounds tail latency.

Use `--verbose` only for diagnostics. The compact output is the normal path.

## Workflow

When routing an actual harness step:

1. First apply the deterministic fast path. If the next tool is obvious, call it normally and
   do not invoke Jev.
2. At a real ambiguity point, prune the candidate list locally before invoking the helper.
3. Build the smallest useful `HarnessState` from the user goal, latest observation, last
   action, and only constraints that can change the choice.
4. Convert only plausible candidates to `ToolDescriptor`; omit long schemas and irrelevant
   tools.
5. Assign conservative risk levels. If unsure, do not mark a mutating tool as low risk.
6. Call `JevToolRouter.route(...)` only if the one-call budget is still unused.
7. If `decision.fallback` is true, resume normal Codex/planner reasoning for the rest of the
   task.
8. If a tool is selected, validate that it still exists, its arguments can be produced, and
   execution policy allows it.
9. Use the main model only for free-form arguments that actually require generation, then
   execute through the normal harness approval boundary.
10. Continue the rest of the task with normal Codex tool calling; do not route again.

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

The script prints compact JSON containing the selected tool, confidence, category, and fallback status.
Pass `--verbose` only when you need the full probability map.

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

## MCTS lookahead

Use `MCTSToolRouter` only when the best immediate tool depends on likely downstream
outcomes and the harness has a **side-effect-free simulator**.

The simulator may predict state, reward, terminal status, and future tool availability. It
must not execute real writes, shell commands, browser actions, network mutations, or other
external side effects during search.

Prefer the default bounded setup:

```python
MCTSConfig(
    simulations=64,
    max_depth=4,
    max_policy_evaluations=1,
)
```

When a `JevToolRouter` is supplied as `policy_router`, the default budget performs at
most one policy-router evaluation at the first ambiguous node, normally the root. The rest
of the tree search is local. Large registries may still trigger hierarchical routing inside
that single router evaluation. Raising `max_policy_evaluations` can increase API/token
cost and should be benchmarked.

Do not invoke MCTS from the Codex routing helper merely to add more reasoning steps. In
unmodified Codex, use it only through a runtime integration that can perform simulations
without extra main-model turns.

After search, execute only the selected first tool through the normal policy/approval
boundary. `principal_variation` is a predicted route, not authorization to execute the
whole sequence.

## Hierarchical routing

Do not manually flatten very large tool registries. `JevToolRouter` automatically uses category-first routing when the number of tools exceeds `hierarchical_threshold` (default: 24). Smaller registries stay flat to avoid a second Jev request.

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
6. If MCTS is enabled, confirm simulation is side-effect free and policy evaluations are bounded.
7. Run the project's tests.
8. Do not claim latency or cost improvements without measurements.
