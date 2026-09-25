# Jev vs normal tool calling benchmark

This note summarizes the controlled benchmark used on the harness-router project site.

## Result

In this experiment, the skill-driven harness-router integration **did not reduce token usage**.

| Mean per run | Normal Codex | Harness-router + Jev |
|---|---:|---:|
| Main-model requests | 13.6 | 24.2 |
| Main-model input tokens | 339,585.6 | 699,540.6 |
| Main-model output tokens | 2,169.8 | 4,235.2 |
| Main-model total tokens | 341,755.4 | 703,775.8 |
| Router total tokens | 0 | 13,360.6 |
| **Full-system total tokens** | **341,755.4** | **717,136.4** |
| Tool calls | 12.6 | 23.2 |
| Routing helper calls | 0 | 11.6 |
| Fallbacks | 0 | 10.8 |
| **Elapsed seconds** | **115.8** | **228.9** |

Mean main-model tokens increased by **105.9%**. Mean full-system tokens increased by **109.8%**.

All ten runs passed independent verification.

## Setup

- Ten fresh Codex sessions: five normal and five using the harness-router skill.
- Repository: `Protocol-Lattice/harness-router`
- Starting commit: `2f71a0b1f429c3ea32a16c4f47151e2c09ae4742`
- Model: `gpt-6-astra`
- Reasoning: `max`
- The initial task, environment context, tool definitions, source tree, execution policy, and limits matched.
- Every session started without previous findings, tool output, patches, or reasoning.

The active mode label was the only initial developer-instruction difference.

## Router latency

Across 58 routing decisions, Jev decisions averaged **454 ms** (median 386 ms, range 319–2756 ms).

Total Jev routing wall time across all routed runs was 26.34 seconds. This includes router/provider client overhead; it is not model-only compute time.

Normal Codex does not expose tool-selection time separately from reasoning and argument generation, so this experiment does **not** make an isolated selector-to-selector latency claim.

## Why did the integrated result get worse?

This benchmark tests the **skill-driven integration with unmodified Codex**.

Codex issues a routing-helper tool call, then another main-model request generates the selected tool arguments. Both requests count toward measured usage.

Across 58 routing decisions, **54 ultimately fell back to Codex**: 47 automatic fallbacks and 7 explicit planner overrides.

Most main-model tokens were repeated input context. Cached input still counts under the requested metric.

This experiment does **not** test a custom Codex runtime that invokes Jev outside a main-model turn.

## Limits

This result should not be generalized beyond the tested integration.

- Five repetitions per arm on one small repository are not enough for a universal performance claim.
- The task permitted independently chosen bugs, so search paths and difficulty could vary.
- Run order was fixed and alternating, not randomized.
- Agents selected different focused-test commands and regression cases.
- The shared pass-through capture transport added some overhead to both arms.
- Setup, smoke tests, dependency work, supervisor verification, and report-generation tokens were excluded equally from both arms.
- No pricing assumptions or cost-savings claims are made.

## Changes made in response

The repository now targets the overhead exposed by this benchmark:

- Codex skill routing is selective rather than per-step, with a maximum of two helper calls per task.
- The skill stops using the helper after the first fallback or explicit planner override.
- Helper output is compact by default and omits probability maps unless `--verbose` is requested.
- Jev state/tool descriptions are truncated more aggressively.
- Flat routing is used through 24 tools to avoid a second Jev request for medium-size registries.
- `RoutingSession` opens a fallback circuit after repeated fallbacks so custom runtimes stop paying for a route that is not helping.

These are benchmark-driven design changes, **not new benchmark results**. Re-run the controlled benchmark before claiming a measured latency or token reduction.

## Takeaway

The benchmark is a reminder to measure the **complete harness loop**, not just the selector.

A low-latency discrete decision model can still increase total latency or token usage when the integration adds main-model turns, repeats context, or falls back frequently. Runtime-native routing may behave differently and requires its own benchmark.
