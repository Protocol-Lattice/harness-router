# MCTS decision latency with 4,096 simulations — 2026-09-26

With `simulations=4096`, `max_depth=3`, and the Jev prior enabled, **route_mcts selected the optimal first action in 24/24 cases**. Codex also selected the optimum in **24/24**. The timed rerun measured **388 ms** per MCTS choice and **4,772.4 ms** per Codex name-only choice: **12.3× by the ratio of observed means**.

This is a **retuned result on the same 24 fixtures**, not a held-out accuracy result. The user requested a budget reaching 24/24. A separate 24-case budget check at 4,096 simulations reached 24/24 before this new paired timing run. Both sets of responses are retained. The run does not establish that 4,096 is the minimum sufficient budget or guarantee optimal choices on other graphs.

| Time per first-action choice | Codex in-session selection | Harness-router route_mcts |
|---|---:|---:|
| Mean | 4,772.4 ms | 388 ms |
| Median | 4,328.5 ms | 372.5 ms |
| p95, nearest rank | 6,958 ms | 545 ms |
| Minimum | 3,325 ms | 294 ms |
| Maximum | 7,981 ms | 700 ms |
| Sum of 24 intervals | 114.538 s | 9.313 s |
| Optimal first actions | 24/24 | 24/24 |

There were **zero final MCTS fallbacks and zero tool errors**. Every response reported **4,096 simulations and one policy evaluation**. Every returned principal variation was a valid three-transition terminal path whose first action matched the selected tool. All measured samples are included; none were removed or retried.

## Configuration and historical comparison

| Run | Simulations | Depth | MCTS optimal | MCTS mean | Codex mean |
|---|---:|---:|---:|---:|---:|
| [Original run](mcts-decision-latency-2026-09-26.md) | 32 | 3 | 11/24 | 356.7 ms | 5,950.5 ms |
| This timed rerun | 4,096 | 3 | 24/24 | 388 ms | 4,772.4 ms |

The search budget increased 128-fold. The reward fixtures and search algorithm were preserved. The native MCP server was reconnected with support for budgets up to 4,096; a separate no-provider diagnostic confirmed that the active server accepted and returned that budget.

These historical and current means come from separate runs with different context, warmup and connection conditions. Their difference does not isolate the runtime cost of increasing the simulation budget.

## Method and exclusions

- Reused the original 24 fixtures: eight families with 4, 8 or 16 root choices, each followed by a deterministic three-edge chain. All heuristic state values were zero. The reward objective was `r1 + 0.95*r2 + 0.9025*r3`.
- Preserved rewards, candidate names and order, case order, and pair order. Twelve pairs were Codex-first and twelve MCTS-first.
- Namespaced case IDs, state IDs and observation IDs separately for budget selection, the pre-run check, and measured calls. This prevents identical routing-state cache reuse between these phases; internal provider cache telemetry is unavailable.
- Both arms received equivalent reward information. Codex received named reward paths, and MCTS received the expanded graph with those same paths in root tool descriptions.
- For MCTS-first pairs, the response was retained inside the recorder and not shown before the Codex selection. Codex submitted a name without using another tool to calculate it.
- A separate **412 ms pre-run check** was excluded. It followed budget selection and is not a cold-start measurement. All 24 budget-selection calls, process diagnostics, setup, input construction, oracle calculations, progress comments and report generation are outside the paired timing summary.
- No candidate action was executed. No Python script, OpenAI API call or subagent was used. JavaScript called the installed native MCP tool.
- The default Jev provider is OpenRouter, as documented by the project. Credentials were not inspected or printed. One policy evaluation does not prove that an internal Jev call succeeded or exclude an internal fallback.

## Timing boundaries and limits

**Codex:** `Date.now()` immediately before the recorder returned a scenario, to `Date.now()` as the first statement of the next `functions.exec` submitting the chosen name. This includes delivery, reasoning, response generation, host scheduling and dispatch.

**MCTS:** `Date.now()` immediately before invoking `route_mcts`, until its MCP promise resolved. This includes MCP transport, parsing, any provider round trip and local search. Caller-side graph preparation and generation of the invocation are excluded.

Both are millisecond-resolution wall-clock intervals. All recorded intervals were nonnegative and internally consistent. The unequal boundaries do not measure pure reasoning time, identical inference envelopes, or complete agent-task latency. Codex's arm remains a name-only selector proxy rather than a full tool invocation with substantive arguments.

The cases were authored and previously inspected in the same growing Codex conversation. Budget selection and evaluation reused them. This is an exploratory demonstration on known fixtures, not a blind, held-out or general accuracy estimate. Exact active Codex model and reasoning settings were not independently captured. The synthetic graphs branch only at the root.

The checkout at measurement was `65a1e82501b9f211c4429f66481c5208f705e7d3`. Installed `mcts.py`, `mcp_server.py`, `router.py`, `provider.py` and `config.py` matched the checkout byte-for-byte; their SHA-256 hashes are in the raw JSON. Source-fixture SHA-256: `98acd0e3e44c30b1019bda769475740d43fa76542559d3a6fe4e0467927f8fc2`.

## Candidate-count breakdown

| Root choices | Pairs | Codex mean | MCTS mean | Codex optimal | MCTS optimal |
|---|---:|---:|---:|---:|---:|
| 4 | 8 | 4,505.4 ms | 362.8 ms | 8/8 | 8/8 |
| 8 | 8 | 3,968.5 ms | 402 ms | 8/8 | 8/8 |
| 16 | 8 | 5,843.4 ms | 399.4 ms | 8/8 | 8/8 |

## Individual measurements

| Trial | Candidates | Order | Codex ms | MCTS ms | Codex choice | MCTS choice | Optimal |
|---|---:|---|---:|---:|---|---|---|
| 1 | 8 | native_first | 4218 | 545 | choice_02 | choice_02 | choice_02 |
| 2 | 8 | mcts_first | 3325 | 430 | choice_03 | choice_03 | choice_03 |
| 3 | 16 | native_first | 6073 | 363 | choice_06 | choice_06 | choice_06 |
| 4 | 4 | mcts_first | 3803 | 307 | choice_04 | choice_04 | choice_04 |
| 5 | 16 | mcts_first | 6958 | 358 | choice_10 | choice_10 | choice_10 |
| 6 | 4 | native_first | 4142 | 405 | choice_01 | choice_01 | choice_01 |
| 7 | 8 | mcts_first | 4254 | 387 | choice_06 | choice_06 | choice_06 |
| 8 | 8 | native_first | 3738 | 314 | choice_02 | choice_02 | choice_02 |
| 9 | 4 | mcts_first | 3351 | 401 | choice_01 | choice_01 | choice_01 |
| 10 | 8 | native_first | 5132 | 356 | choice_06 | choice_06 | choice_06 |
| 11 | 4 | native_first | 5055 | 325 | choice_03 | choice_03 | choice_03 |
| 12 | 8 | native_first | 4121 | 328 | choice_05 | choice_05 | choice_05 |
| 13 | 4 | mcts_first | 4793 | 402 | choice_02 | choice_02 | choice_02 |
| 14 | 16 | mcts_first | 5946 | 323 | choice_13 | choice_13 | choice_13 |
| 15 | 16 | native_first | 5077 | 336 | choice_02 | choice_02 | choice_02 |
| 16 | 16 | native_first | 7981 | 406 | choice_02 | choice_02 | choice_02 |
| 17 | 16 | native_first | 4194 | 366 | choice_14 | choice_14 | choice_14 |
| 18 | 16 | mcts_first | 5274 | 343 | choice_04 | choice_04 | choice_04 |
| 19 | 8 | mcts_first | 3517 | 455 | choice_03 | choice_03 | choice_03 |
| 20 | 4 | mcts_first | 4259 | 389 | choice_04 | choice_04 | choice_04 |
| 21 | 16 | mcts_first | 5244 | 700 | choice_15 | choice_15 | choice_15 |
| 22 | 4 | native_first | 6242 | 294 | choice_01 | choice_01 | choice_01 |
| 23 | 4 | native_first | 4398 | 379 | choice_02 | choice_02 | choice_02 |
| 24 | 8 | mcts_first | 3443 | 401 | choice_04 | choice_04 | choice_04 |

The [raw JSON](mcts-4096-decision-latency-2026-09-26.json) contains all measured inputs and responses, timestamps, exact oracle values, the separate budget check, the excluded pre-run check, source hashes and JavaScript recording helpers.
