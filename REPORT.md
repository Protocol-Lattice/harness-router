# Harness-router token benchmark

Ten fresh Codex sessions: five normal and five using the harness-router skill. Repository `Protocol-Lattice/harness-router`, commit `2f71a0b1f429c3ea32a16c4f47151e2c09ae4742`; model `gpt-6-astra`, reasoning `max`.

**Harness-router did not reduce token usage in this experiment.** Mean main-model tokens increased by **105.9%**, and mean full-system tokens increased by **109.8%**. All ten runs passed independent verification.

## Mean per run

| Metric | Normal Codex | Harness-router |
|---|---:|---:|
| Main-model requests | 13.6 | 24.2 |
| Main-model input tokens | 339,585.6 | 699,540.6 |
| Main-model output tokens | 2,169.8 | 4,235.2 |
| Cached input tokens (included above) | 305,331.2 | 659,430.4 |
| Reasoning tokens (included in output) | 1,106.2 | 1,680.2 |
| Main-model total tokens | 341,755.4 | 703,775.8 |
| Router requests | 0.0 | 11.6 |
| Router input tokens | 0 | 12,305.8 |
| Router output tokens | 0 | 1,054.8 |
| Router total tokens | 0 | 13,360.6 |
| Full-system total tokens | 341,755.4 | 717,136.4 |
| Tool calls (includes router helpers) | 12.6 | 23.2 |
| Read tool calls | 5.6 | 5.6 |
| Search tool calls | 1.0 | 1.0 |
| Mutation tool calls | 2.0 | 2.0 |
| Shell tool calls (overlaps categories) | 10.6 | 21.2 |
| Test tool calls | 3.4 | 3.0 |
| Routing helper calls | 0.0 | 11.6 |
| Failed tool calls (includes expected red tests) | 1.0 | 1.0 |
| Repeated identical tool calls | 0.0 | 0.2 |
| Fallbacks (including explicit planner overrides) | 0.0 | 10.8 |
| Elapsed seconds | 115.8 | 228.9 |

Router reasoning-token counts are not exposed separately by this provider. They remain `null`; input/output usage is complete. No pricing assumptions or cost savings are claimed.

Total measured usage across all ten runs: **5,294,459 tokens** (5,227,656 main-model + 66,803 router). The main-model total includes 4,823,808 cached input tokens.

## All runs

| Run | Success | Main tokens | Jev tokens | Full-system tokens | Main requests | Tool calls | Fallbacks | Seconds | Final tests |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| A1 | Pass | 360,544 | 0 | 360,544 | 15 | 14 | 0 | 109.9 | 19 passed in 0.06s |
| B1 | Pass | 634,103 | 12,984 | 647,087 | 23 | 22 | 10 | 197.0 | 19 passed in 0.06s |
| A2 | Pass | 371,239 | 0 | 371,239 | 14 | 13 | 0 | 127.6 | 22 passed in 0.06s |
| B2 | Pass | 738,279 | 14,094 | 752,373 | 25 | 24 | 10 | 213.1 | 26 passed in 0.06s |
| A3 | Pass | 308,762 | 0 | 308,762 | 13 | 12 | 0 | 103.4 | 19 passed in 0.06s |
| B3 | Pass | 632,550 | 12,436 | 644,986 | 23 | 22 | 11 | 221.6 | 19 passed in 0.06s |
| A4 | Pass | 358,746 | 0 | 358,746 | 13 | 12 | 0 | 126.0 | 22 passed in 0.06s |
| B4 | Pass | 736,121 | 13,555 | 749,676 | 25 | 24 | 11 | 237.9 | 26 passed in 0.06s |
| A5 | Pass | 309,486 | 0 | 309,486 | 13 | 12 | 0 | 112.0 | 19 passed in 0.06s |
| B5 | Pass | 777,826 | 13,734 | 791,560 | 25 | 24 | 12 | 274.7 | 23 passed in 0.06s |

For exact values for every requested metric, use [metrics.csv](metrics.csv) or [results.json](results.json).

## Savings calculations

Savings are baseline minus harness-router; **negative values mean an increase**.

| Pair | Main tokens saved | Main saving % | Full-system tokens saved | Full-system saving % | Tool-call reduction % | Main-request reduction % |
|---|---:|---:|---:|---:|---:|---:|
| 1 | -273,559 | -75.9% | -286,543 | -79.5% | -57.1% | -53.3% |
| 2 | -367,040 | -98.9% | -381,134 | -102.7% | -84.6% | -78.6% |
| 3 | -323,788 | -104.9% | -336,224 | -108.9% | -83.3% | -76.9% |
| 4 | -377,375 | -105.2% | -390,930 | -109.0% | -100.0% | -92.3% |
| 5 | -468,340 | -151.3% | -482,074 | -155.8% | -100.0% | -92.3% |
| Mean comparison | -362,020.4 | -105.9% | -375,381 | -109.8% | -84.1% | -77.9% |

The mean comparison is the percentage difference between the two group means, not the mean of the five pair percentages.

## Variation across repetitions

| Metric | Mode | Median | Minimum | Maximum |
|---|---|---:|---:|---:|
| Main tokens | A | 358,746 | 308,762 | 371,239 |
| Main tokens | B | 736,121 | 632,550 | 777,826 |
| Full-system tokens | A | 358,746 | 308,762 | 371,239 |
| Full-system tokens | B | 749,676 | 644,986 | 791,560 |
| Seconds | A | 112.0 | 103.4 | 127.6 |
| Seconds | B | 221.6 | 197.0 | 274.7 |

## Measured latency

Jev routing decisions averaged **454 ms** (median 386 ms, range 319–2756 ms). Total Jev routing wall time across all B runs was 26.34 seconds. This includes router/provider client overhead; it is not model-only compute time.

Mean end-to-end time was **115.8 seconds** for normal Codex and **228.9 seconds** with harness-router. Normal Codex does not expose tool-selection time separately from reasoning and argument generation, so there is no valid isolated selector-to-selector latency comparison.

## What the patches fixed

- **A1** — AAAA was mistaken for an alternating ABAB loop, bypassing the configured identical-action repeat limit. [Patch](runs/A1/patch.diff). 13 lines added, 1 removed across 2 files.
- **B1** — AAAA was mistaken for an alternating ABAB loop, bypassing the configured identical-action repeat limit. [Patch](runs/B1/patch.diff). 13 lines added, 1 removed across 2 files.
- **A2** — Non-object JSON response containers raised AttributeError instead of InvalidProviderResponse, bypassing the documented router fallback. [Patch](runs/A2/patch.diff). 18 lines added, 1 removed across 2 files.
- **B2** — Non-object JSON response containers raised AttributeError instead of InvalidProviderResponse, bypassing the documented router fallback. [Patch](runs/B2/patch.diff). 44 lines added, 2 removed across 2 files.
- **A3** — AAAA was mistaken for an alternating ABAB loop, bypassing the configured identical-action repeat limit. [Patch](runs/A3/patch.diff). 13 lines added, 1 removed across 2 files.
- **B3** — AAAA was mistaken for an alternating ABAB loop, bypassing the configured identical-action repeat limit. [Patch](runs/B3/patch.diff). 13 lines added, 1 removed across 2 files.
- **A4** — Non-object JSON response containers raised AttributeError instead of InvalidProviderResponse, bypassing the documented router fallback. [Patch](runs/A4/patch.diff). 18 lines added, 1 removed across 2 files.
- **B4** — Non-object JSON response containers raised AttributeError instead of InvalidProviderResponse, bypassing the documented router fallback. [Patch](runs/B4/patch.diff). 32 lines added, 1 removed across 2 files.
- **A5** — AAAA was mistaken for an alternating ABAB loop, bypassing the configured identical-action repeat limit. [Patch](runs/A5/patch.diff). 13 lines added, 1 removed across 2 files.
- **B5** — Non-object JSON response containers raised AttributeError instead of InvalidProviderResponse, bypassing the documented router fallback. [Patch](runs/B5/patch.diff). 45 lines added, 2 removed across 2 files.

Each final test suite also ran against the original production code and failed on the new regression. Existing tests were preserved. Supervisor verification logs are under each run directory.

## Control audit and limitations

- all_tool_sets_equal: **True**.
- all_common_developer_instructions_equal: **True**.
- all_initial_user_contexts_equal: **True**.
- all_sessions_distinct: **True**.
- all_prompt_cache_keys_distinct: **True**.
- all_initial_histories_empty: **True**.
- all_usage_reconciles: **True**.
- all_routing_compliant: **True**.

The active mode label is the only initial developer-instruction difference. The initial user task, environment context, tool definitions, source tree, model, reasoning setting, execution policy, and limits match. Each session starts without previous findings, tool output, patches, or reasoning. The raw provider usage reconciles to Codex turn totals in every completed run.

This tests the **skill-driven integration with unmodified Codex**. Codex issues a routing-helper tool call, then another main-model request generates the selected tool arguments. Both requests count. It does not test a custom Codex runtime that invokes Jev outside a main-model turn. Most main tokens are repeated input context; cached input still counts under the requested metric.

Across 58 router decisions, 54 ultimately fell back to Codex: 47 automatic fallbacks and 7 explicitly recorded planner overrides. These are included, not removed from the statistics.

The task permits independently chosen bugs. Difficulty and search paths can vary, even with identical task text and starting commit. Five repetitions on this single small repository do not establish a universal performance result. Run order was fixed and alternating, not randomized. The shared pass-through capture transport adds some overhead to both modes. The user checkout remains unchanged; fixes are retained as run-specific patches.

The required before/after full-suite command was identical. Agents chose different intermediate focused-test selectors and different regression cases. This limits strict equality of every test invocation; the exact commands are retained in the logs rather than normalized away.

Setup, smoke tests, dependency work, supervisor verification, and report-generation tokens are excluded equally from both arms. Failed calls, expected failing tests, routing helper calls, and explicit planner fallbacks remain counted.

## Reproduce and inspect

See [PROTOCOL.md](PROTOCOL.md), [manifest.json](manifest.json), [benchmark.py](benchmark.py), [analyze.py](analyze.py), [audit.json](audit.json), and [reviews.json](reviews.json). The runner refuses to overwrite existing run evidence. It requires the recorded Codex/Python environment, Codex authentication, and an environment-provided OpenRouter key. Adjust installation paths for another machine without changing the task or treatment. No credential values are stored in these artifacts.
