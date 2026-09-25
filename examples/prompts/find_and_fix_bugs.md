# Find and Fix Bugs with harness-router + UTCP

Use this prompt with Codex or another coding agent after installing the UTCP example tools.

## Prompt

You are working inside the current repository.

Your task is to find real bugs in the codebase, fix them, and verify the fixes.

Use `harness-router` with OpenRouter Jev as the fast tool-selection layer.

Available UTCP tools include:

- `local.fs_list` — list files and directories
- `local.fs_read` — read source files
- `local.fs_write` — write complete file contents
- `local.fs_patch` — replace one exact text block in a file
- `local.bash_run` — run Bash commands such as tests, linters, builds, grep, git diff, and git status

Follow this control loop:

1. Understand the repository structure.
2. Use Jev through `harness-router` to choose the next tool from the available UTCP tools.
3. Inspect code before modifying it.
4. Look for concrete defects such as:
   - failing tests
   - incorrect edge-case handling
   - broken error handling
   - invalid assumptions
   - off-by-one errors
   - unsafe path handling
   - resource leaks
   - stale or inconsistent state
   - incorrect async behavior
   - malformed parsing/serialization
   - unreachable or contradictory logic
5. Reproduce or demonstrate a bug before fixing it whenever practical.
6. Use the main reasoning model, not Jev, to:
   - analyze root cause
   - design the fix
   - generate code
   - generate patches
   - generate shell commands
7. Prefer the smallest correct fix.
8. Use `fs_patch` when a precise local edit is sufficient.
9. Use `fs_write` only when replacing a whole file is clearer.
10. Use `bash_run` to execute tests, linters, builds, and targeted reproduction commands.
11. After every modification, verify the affected behavior.
12. Run the relevant test suite before finishing.
13. Inspect `git diff` before completing the task.
14. Do not change unrelated behavior.
15. Do not claim a bug is fixed unless verification passes.

Important routing rules:

- Jev selects the next tool/action.
- Jev does not generate source code or patches.
- Jev confidence is not permission to mutate files or execute Bash.
- Mutating filesystem actions require explicit execution approval.
- Bash execution requires explicit execution approval.
- If Jev returns a fallback or low-confidence decision, use normal Codex/planner reasoning.

Suggested initial sequence:

```text
fs_list
  ↓
inspect likely source/test files
  ↓
bash_run targeted tests
  ↓
fs_read failing implementation
  ↓
reason about root cause
  ↓
fs_patch or fs_write
  ↓
bash_run targeted tests
  ↓
bash_run broader test suite
  ↓
bash_run git diff
```

Success criteria:

- at least one concrete bug is identified
- the root cause is explained
- the implementation is fixed
- regression coverage is added or existing tests demonstrate the fix
- relevant tests pass
- the final diff contains only justified changes

At the end, report:

1. Bug found
2. Root cause
3. Files changed
4. Fix applied
5. Verification commands
6. Test results
7. Any remaining risks or follow-up work
