# Live tool-choice latency benchmark — 2026-09-26

Harness-router returned a choice in **404.5 ms on average** (median **332 ms**). Codex's observed time from receiving a case to submitting a tool name averaged **3842.1 ms** (median **3384 ms**).

These are selection-only wall-clock measurements with **no selected-tool execution**. They are **not isolated model reasoning times** and are not an apples-to-apples inference-speed benchmark: Codex's timer includes its host round trip and submission generation, while the router's timer covers the MCP call and provider round trip. The observed ratio of means is 9.50; it must not be interpreted as a pure reasoning speedup or an end-to-end agent speedup.

| Time per choice | Codex in-session selection | Harness-router MCP |
|---|---:|---:|
| Mean | 3842.1 ms | 404.5 ms |
| Median | 3384.0 ms | 332.0 ms |
| p95, nearest rank | 6435 ms | 718 ms |
| Minimum | 2492 ms | 276 ms |
| Maximum | 8123 ms | 1326 ms |
| Sum of 24 measured intervals | 92.210 s | 9.708 s |

Both selectors chose the same tool in **24/24** cases. The router returned **0 fallbacks** and **0 errors**. No samples or outliers were removed. Agreement is a sanity check, not an independent quality evaluation.

## Method

- Ran directly in the existing Codex task using JavaScript tool orchestration and the installed `mcp__harness_router__route` tool.
- No Python script was written or run, and no OpenAI API client or endpoint was called. The installed router uses its configured provider; repository documentation identifies OpenRouter/Jev. Provider credentials were never accessed or printed.
- 24 matched pairs: eight authored scenario families, each with 4, 8 and 16 candidates. Families cover reading a known file, locating a symbol, applying a known patch, focused tests, full tests, lint, final diff review and missing user intent.
- Both arms received the same goal, observation, last action and candidate descriptors. Candidate order and case order were shuffled with seed 260926. Twelve pairs were native-first and twelve router-first.
- For router-first pairs, the router result was retained internally and never displayed before Codex submitted its choice.
- Every router input was unique. No repeated-input cache-hit benchmark was mixed into the results. Internal cache-hit telemetry is unavailable.
- One router warmup took 695 ms and is excluded from the 24 measurements. Connections could be reused after warmup. No separately timed Codex warmup was performed; its first sample remains included.
- No repository file reads, edits, tests, browser actions or other candidate actions were executed as part of a trial. Only the choice was recorded. Setup, progress comments and report generation were outside active native timing intervals.
- Measurements were recorded at millisecond resolution with `Date.now()`. This is a wall clock, not a monotonic performance clock; no backwards intervals were observed.

### Timer boundaries

**Codex:** Start immediately before returning a scenario from a tool result. Stop at the first `Date.now()` in the next `functions.exec` call submitting the selected name. This measures scenario delivery, model reasoning, generation of a short recording call, host scheduling and dispatch. It does not expose the instant when the model internally decided.

**Harness-router:** Start immediately before calling `route`. Stop when the MCP promise resolves. This includes serialization, MCP transport, provider/network latency and decision processing; it excludes Codex constructing the route invocation or interpreting its result.

The normal arm is therefore a **Codex name-only selector proxy**, not a native full-schema tool call with real arguments. That restriction isolates selection from action execution and substantive argument generation, but limits comparisons to ordinary tool calling.

### Context and reproducibility limits

- One existing, growing Codex conversation was used, with repository/tool context already loaded. Exact active model and reasoning settings were not independently captured. These were not fresh model sessions with matched context size.
- Codex authored the templates and expected labels before the timed selections. This is not a blinded, held-out dataset. The eight templates repeat across candidate sizes and mostly describe clear next steps; results do not establish performance on difficult ambiguous choices.
- The larger candidate sets appear disproportionately early in this shuffled run, so size comparisons also reflect warmup/order effects. Eight samples per size do not establish scaling behavior.
- Local checkout: `66cea2195ba8de06e85752f3c094c517a2dd1611`. Local graph coverage was current at generation `2026-09-26T18:24:20Z`; checked documentation and MCP server source had no recorded coverage issues.
- Local `mcp_server.py` configures a 0.72 direct/fallback threshold and a 2-second provider timeout. The installed MCP server's package revision and actual provider settings were not independently verified, so this report benchmarks the live installed tool rather than asserting it exactly matches the checkout.
- Results are one small exploratory run. No claim is made about pure inference latency, tokens, cost, real task completion time or universal speedup.

## Candidate-count breakdown

| Candidates | Pairs | Codex mean ms | Codex median ms | Router mean ms | Router median ms |
|---|---:|---:|---:|---:|---:|
| 4 | 8 | 3423.4 | 3137.5 | 364.5 | 364.0 |
| 8 | 8 | 3474.1 | 3161.0 | 318.5 | 318.0 |
| 16 | 8 | 4628.8 | 4281.0 | 530.5 | 309.0 |

## Individual choices

| Trial | Case | Candidates | Order | Codex ms | Router ms | Codex choice | Router choice | Fallback |
|---|---|---:|---|---:|---:|---|---|---|
| 1 | c16-7 | 16 | native_first | 8123 | 1326 | inspect_diff | inspect_diff | false |
| 2 | c16-2 | 16 | router_first | 4363 | 718 | search_code | search_code | false |
| 3 | c4-7 | 4 | native_first | 3436 | 311 | inspect_diff | inspect_diff | false |
| 4 | c16-1 | 16 | native_first | 3498 | 715 | read_file | read_file | false |
| 5 | c16-3 | 16 | router_first | 3649 | 287 | apply_patch | apply_patch | false |
| 6 | c16-4 | 16 | router_first | 4729 | 295 | run_focused_tests | run_focused_tests | false |
| 7 | c8-2 | 8 | router_first | 3171 | 312 | search_code | search_code | false |
| 8 | c8-4 | 8 | native_first | 4940 | 344 | run_focused_tests | run_focused_tests | false |
| 9 | c4-6 | 4 | native_first | 2492 | 380 | run_linter | run_linter | false |
| 10 | c4-8 | 4 | native_first | 3312 | 426 | request_clarification | request_clarification | false |
| 11 | c8-8 | 8 | router_first | 3151 | 276 | request_clarification | request_clarification | false |
| 12 | c4-3 | 4 | router_first | 2561 | 438 | apply_patch | apply_patch | false |
| 13 | c16-6 | 16 | native_first | 3251 | 315 | run_linter | run_linter | false |
| 14 | c8-5 | 8 | native_first | 2765 | 298 | run_full_tests | run_full_tests | false |
| 15 | c16-8 | 16 | router_first | 4199 | 285 | request_clarification | request_clarification | false |
| 16 | c4-1 | 4 | native_first | 6435 | 293 | read_file | read_file | false |
| 17 | c4-5 | 4 | router_first | 2963 | 364 | run_full_tests | run_full_tests | false |
| 18 | c8-7 | 8 | native_first | 2923 | 358 | inspect_diff | inspect_diff | false |
| 19 | c4-2 | 4 | router_first | 2579 | 340 | search_code | search_code | false |
| 20 | c16-5 | 16 | router_first | 5218 | 303 | run_full_tests | run_full_tests | false |
| 21 | c4-4 | 4 | native_first | 3609 | 364 | run_focused_tests | run_focused_tests | false |
| 22 | c8-1 | 8 | native_first | 2743 | 324 | read_file | read_file | false |
| 23 | c8-6 | 8 | router_first | 3332 | 282 | run_linter | run_linter | false |
| 24 | c8-3 | 8 | router_first | 4768 | 354 | apply_patch | apply_patch | false |

The [raw JSON](decision-latency-2026-09-26.json) contains all inputs, outputs, timestamps, the excluded warmup, protocol and summary statistics.

