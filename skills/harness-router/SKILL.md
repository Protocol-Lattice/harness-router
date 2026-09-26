---
name: harness-router
description: Use Protocol Lattice harness-router/Jev only when the user explicitly asks for it or when a tool choice is genuinely ambiguous after local pruning. Do not use it for obvious linear coding steps.
---

# Harness Router

Use this skill as a **rare ambiguity resolver**, not as a per-step router.

If the native MCP tools from the `harness-router` server are available, prefer them directly. Use `route` for ordinary ambiguous closed choices. Use `route_mcts` only when multi-step consequences matter and a side-effect-free predicted state graph is already available. Do not invoke the Python helper script when MCP is available.

## Fast path

Default: use normal harness/Codex tool calling.

Do **not** invoke Jev when the next action is obvious, including:

- known path -> read
- known symbol -> search/read
- edit determined -> patch/write
- edit done -> test
- precise failing line -> inspect it

Invoke the helper only when:

1. at least 4 plausible tools remain after deterministic pruning,
2. the choice is closed/discrete,
3. choosing wrong would likely cost multiple extra turns.

Budget: **max 1 helper call per user task**. After fallback/error/override, never call it again for that task. Never route the same state twice.

## Minimal call

Pass only 4-12 plausible candidates with short descriptions. Never send schemas, full conversation history, source files, or secrets.

Preferred path: call the native MCP `route` tool. Use the script below only when the MCP server is unavailable.

```bash
python skills/harness-router/scripts/route.py \
  --goal "Fix failing parser test" \
  --observation "Failure points to src/parser.py" \
  --tools-json '[{"name":"read_file","description":"Read source","category":"inspect","risk":"low"},{"name":"search_code","description":"Search repo","category":"inspect","risk":"low"},{"name":"run_tests","description":"Run tests","category":"verify","risk":"low"},{"name":"write_file","description":"Write source","category":"mutate","risk":"medium"}]'
```

Use compact output; do not add `--verbose` unless debugging.

If `fallback=true`, resume normal planner/tool calling and stop routing.

## Safety

Read `OPENROUTER_API_KEY` only from the environment. Never print, inspect, echo, log, copy, or place secrets in routing state or tool metadata. Jev confidence is not authorization; normal execution policy still applies.

## MCTS

Do not invoke MCTS during normal linear Codex work.

The native MCP server exposes `route_mcts`. Use it only when:
- the best first action depends on likely downstream consequences
- a side-effect-free predicted state graph is available
- real tools are not executed during search
- bounded search is sufficient

Prefer `simulations=32`, `max_depth=3`, and at most one Jev prior evaluation. Execute only the first selected real action after search. The principal variation is a prediction, not authorization to execute the whole sequence.

## Details

Only when implementing, configuring, benchmarking, or debugging harness-router, read:
- `references/integration.md`
- `references/full-guide.md`

For ordinary use, do not load those references.
