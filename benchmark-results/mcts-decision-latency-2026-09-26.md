# Live MCTS tool-choice latency benchmark — 2026-09-26

The installed harness-router `route_mcts` tool returned a first-action choice in **356.7 ms on average**, versus **5950.5 ms** for Codex's observed name-only selection time.

The ratio of the observed means is **16.7×**, but the choices were not equally successful: **MCTS chose the optimal simulated branch in 11/24 cases (45.8%); Codex did so in 24/24 (100%)**. The MCTS results include all 13 suboptimal selections.

This is a selection-only wall-clock comparison. **No selected action was executed.** Different timer boundaries prevent a claim about pure reasoning speed or full agent-task speed. The accuracy gap also prevents treating the time ratio as an equal-quality speedup.

| Time per first-action choice | Codex in-session selection | Harness-router route_mcts |
|---|---:|---:|
| Mean | 5950.5 ms | 356.7 ms |
| Median | 5199.5 ms | 333.5 ms |
| p95, nearest rank | 8848 ms | 560 ms |
| Minimum | 3860 ms | 272 ms |
| Maximum | 10997 ms | 578 ms |
| Sum of 24 measured intervals | 142.813 s | 8.560 s |
| Optimal simulated first action | 24/24 | 11/24 |

The selectors agreed in **11/24** cases. MCTS returned **0 final fallbacks** and **0 tool errors**. A successful return without fallback does not guarantee an optimal choice.

## Search configuration and returned diagnostics

- Native MCP tool: `mcp__harness_router__route_mcts`.
- `simulations=32`, `max_depth=3`, `use_jev_prior=true`.
- All 24 responses reported exactly **32 simulations** and **1 policy evaluation**.
- All 24 predicted paths were valid in their supplied graph, matched the returned first action and reached a terminal state within three transitions.
- Every state heuristic value was zero. The simulated discount was **0.95**, matching the local source's MCTS default.
- No real tools, shell commands, file mutations or browser/network mutations were used as simulated transitions. The states and edges represented abstract numeric rewards only.
- MCTS confidence is the selected root action's visit share, not calibrated correctness probability.
- The response counts policy-router evaluations; it does not expose whether the underlying Jev call succeeded, fell back, used a cache, or issued a network request. Zero final MCTS fallbacks must not be read as zero internal Jev fallbacks.

## Same timing protocol, new multi-step inputs

The earlier [ordinary-route benchmark](decision-latency-2026-09-26.md) measured 24 matched pairs with 4, 8 and 16 candidates. This run preserves that layout and recording method while replacing the ordinary scenarios with trees where downstream rewards matter. It does **not** isolate the incremental cost of MCTS relative to ordinary `route`: workloads, native context and timing conditions differ.

- Eight authored scenario families, each instantiated with **4, 8 or 16 root choices**: delayed gain, early gain followed by a penalty, upfront cost, steady rewards, a close discounted choice, early reward winning, late cost and recovery.
- Each root choice leads to a deterministic chain of **three rewarded transitions**. There are 13, 25 or 49 states and 12, 24 or 48 edges per case. Branching occurs only at the root.
- Small seeded reward perturbations and extra candidate paths vary the instances. Candidate order, case order and arm order were shuffled with **seed 260926**.
- Twelve pairs were Codex-first and twelve MCTS-first. For MCTS-first pairs the router answer was stored privately and never shown to Codex before it selected.
- Codex received compact named reward paths. MCTS received the equivalent expanded state graph, and the same paths in root candidate descriptions for the optional Jev prior. The logical reward information matches; the serialization and surrounding context do not.
- All 24 routing inputs were unique. No intentional repeated-input cache hits were measured.
- One separate MCTS warmup took **8370 ms** and is excluded, exactly as the ordinary-route benchmark excluded its router warmup. Its full input and response are retained in the raw JSON. No separate Codex warmup was measured.
- No measured samples were removed or retried. The cold warmup is reported separately rather than silently omitted.
- The explicit user request for repeated benchmarking governs this run; the skill's normal per-task helper limit was not used to suppress benchmark samples. The 16-candidate group was retained to match the earlier protocol.
- No Python scripts, OpenAI API calls or subagents were used. JavaScript orchestrated the installed MCP tool and recorded timestamps. The skill describes the optional Jev provider as OpenRouter; no credential values were inspected.

## Timing boundaries

**Codex:** `Date.now()` immediately before returning the scenario in a tool result, until the first `Date.now()` in its next `functions.exec` call submitting a selected name. This includes case delivery, model reasoning, generation of the short recording call, host scheduling and dispatch. It does not expose the instant of an internal decision.

**MCTS:** `Date.now()` immediately before invoking `route_mcts`, until the MCP promise resolves. This includes MCP transport, input parsing, graph construction inside the tool, the optional provider round trip and local search. It excludes the outer caller constructing the graph, Codex generating the invocation, and interpretation or execution of the result.

Both clocks use millisecond-resolution wall time rather than a monotonic clock. All recorded intervals were nonnegative. Benchmark setup, input generation, oracle calculations, commentary and report generation were outside active timing windows.

The Codex arm is a **name-only selector proxy**, not a normal full-schema tool call with real arguments. MCTS includes an API-sized invocation that is generated before its timer starts, while Codex's short recording-call generation is inside its timer. These measurements therefore compare observed response intervals, not identical inference envelopes.

## Quality check

For every root path, an independent JavaScript oracle computed the exact complete discounted return:

`return = r1 + 0.95 × r2 + 0.9025 × r3`

The oracle's values and winning labels were stored before timing but were not shown during Codex choice submission. Codex chose its answer without invoking a calculator or another tool. Every case had a unique optimal root action. Optimality here means maximizing rewards in the supplied synthetic graph, not success on an actual software task.

| Root candidates | Pairs | Codex mean ms | MCTS mean ms | Codex optimal | MCTS optimal |
|---|---:|---:|---:|---:|---:|
| 4 | 8 | 5701.6 | 346.8 | 8/8 | 6/8 |
| 8 | 8 | 5530.1 | 360.8 | 8/8 | 4/8 |
| 16 | 8 | 6619.9 | 362.5 | 8/8 | 1/8 |

Mean MCTS reward shortfall relative to the optimal path was **0.137437** in the arbitrary reward units used by these fixtures. The larger candidate groups had fewer optimal selections in this run; eight cases per group do not establish a general scaling law.

### Suboptimal MCTS choices

| Trial | Family | MCTS choice | Chosen return | Optimal choice | Best return | Shortfall |
|---|---|---|---:|---|---:|---:|
| 2 | upfront_cost | choice_07 | 0.410025 | choice_03 | 0.912475 | 0.502450 |
| 3 | steady_reward | choice_15 | 0.663225 | choice_06 | 0.784575 | 0.121350 |
| 4 | upfront_cost | choice_02 | 0.420025 | choice_04 | 0.932475 | 0.512450 |
| 5 | late_cost | choice_12 | 0.436200 | choice_10 | 0.670025 | 0.233825 |
| 8 | delayed_gain | choice_06 | 0.506450 | choice_02 | 0.795875 | 0.289425 |
| 14 | recovery_path | choice_14 | 0.508825 | choice_13 | 0.718475 | 0.209650 |
| 15 | delayed_gain | choice_12 | 0.360250 | choice_02 | 0.803925 | 0.443675 |
| 16 | early_gain_later_penalty | choice_13 | 0.519800 | choice_02 | 0.714475 | 0.194675 |
| 18 | upfront_cost | choice_14 | 0.447550 | choice_04 | 0.921500 | 0.473950 |
| 19 | recovery_path | choice_02 | 0.605750 | choice_03 | 0.719000 | 0.113250 |
| 20 | discount_close_choice | choice_03 | 0.550975 | choice_04 | 0.593225 | 0.042250 |
| 21 | discount_close_choice | choice_11 | 0.598025 | choice_15 | 0.649775 | 0.051750 |
| 24 | discount_close_choice | choice_01 | 0.541475 | choice_04 | 0.651275 | 0.109800 |

The benchmark does not establish the cause of these misses. A bounded search can favor immediate rewards before sufficiently exploring delayed gains, but this run does not separate simulation-budget, prior-distribution or implementation effects. Increasing the budget or changing the prior would require a separate benchmark.

## Individual measurements

| Trial | Candidates | Order | Codex ms | MCTS ms | Codex choice | MCTS choice | Optimal | MCTS optimal |
|---|---:|---|---:|---:|---|---|---|---|
| 1 | 8 | native_first | 4084 | 560 | choice_02 | choice_02 | choice_02 | Yes |
| 2 | 8 | mcts_first | 4148 | 358 | choice_03 | choice_07 | choice_03 | No |
| 3 | 16 | native_first | 4244 | 315 | choice_06 | choice_15 | choice_06 | No |
| 4 | 4 | mcts_first | 3860 | 328 | choice_04 | choice_02 | choice_04 | No |
| 5 | 16 | mcts_first | 5725 | 437 | choice_10 | choice_12 | choice_10 | No |
| 6 | 4 | native_first | 5513 | 316 | choice_01 | choice_01 | choice_01 | Yes |
| 7 | 8 | mcts_first | 7588 | 393 | choice_06 | choice_06 | choice_06 | Yes |
| 8 | 8 | native_first | 4793 | 311 | choice_02 | choice_06 | choice_02 | No |
| 9 | 4 | mcts_first | 4248 | 304 | choice_01 | choice_01 | choice_01 | Yes |
| 10 | 8 | native_first | 4567 | 334 | choice_06 | choice_06 | choice_06 | Yes |
| 11 | 4 | native_first | 5091 | 292 | choice_03 | choice_03 | choice_03 | Yes |
| 12 | 8 | native_first | 7675 | 333 | choice_05 | choice_05 | choice_05 | Yes |
| 13 | 4 | mcts_first | 8303 | 314 | choice_02 | choice_02 | choice_02 | Yes |
| 14 | 16 | mcts_first | 7062 | 318 | choice_13 | choice_14 | choice_13 | No |
| 15 | 16 | native_first | 10997 | 380 | choice_02 | choice_12 | choice_02 | No |
| 16 | 16 | native_first | 7388 | 380 | choice_02 | choice_13 | choice_02 | No |
| 17 | 16 | native_first | 4968 | 363 | choice_14 | choice_14 | choice_14 | Yes |
| 18 | 16 | mcts_first | 7267 | 336 | choice_04 | choice_14 | choice_04 | No |
| 19 | 8 | mcts_first | 4826 | 306 | choice_03 | choice_02 | choice_03 | No |
| 20 | 4 | mcts_first | 4826 | 578 | choice_04 | choice_03 | choice_04 | No |
| 21 | 16 | mcts_first | 5308 | 371 | choice_15 | choice_11 | choice_15 | No |
| 22 | 4 | native_first | 8848 | 370 | choice_01 | choice_01 | choice_01 | Yes |
| 23 | 4 | native_first | 4924 | 272 | choice_02 | choice_02 | choice_02 | Yes |
| 24 | 8 | mcts_first | 6560 | 291 | choice_04 | choice_01 | choice_04 | No |

| Pair order | Pairs | Codex mean ms | MCTS mean ms |
|---|---:|---:|---:|
| native_first | 12 | 6091.0 | 352.2 |
| mcts_first | 12 | 5810.1 | 361.2 |

## Context and limits

- One existing, growing Codex conversation was used. Exact active model and reasoning settings were not independently captured; these were not fresh sessions with matched context sizes.
- Codex authored the scenario families. This is not a blind or held-out evaluation. The same families recur across candidate counts.
- The trees are deterministic and branch only at the root. They do not test deeper branching, uncertain transitions or a production simulator.
- Graph preparation and simulator-development costs are excluded. A real harness would need to obtain these predictions.
- The installed server's revision and effective runtime configuration were not independently verified against the local checkout `1b1c370e293542ee269dc2b27b357385f604c5a8`. Local source inspection found discount 0.95, exploration constant 1.5 and a one-evaluation policy budget. Graph coverage was current at generation `2026-09-26T18:44:42Z` with no recorded gap for the inspected files.
- Only one 32-simulation, depth-3 configuration was measured. There was no tuning, budget sweep, cold-start series or repeated run per exact graph.
- Latency, agreement, optimality and final fallbacks are reported separately. No claim is made about cost, tokens, universal speedup or successful real task completion.
- The previous docs chart and ordinary-route results remain unchanged; this is a separate benchmark artifact.

The [raw JSON](mcts-decision-latency-2026-09-26.json) contains every case's compact input and expanded graph, oracle values, response, timestamps, diagnostics, excluded warmup, summary and recording-helper source.

