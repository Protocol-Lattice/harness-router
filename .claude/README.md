# Harness Router for Claude Code

This folder is a portable project hook package. It needs Python 3.11+ and
`harness-router-mcp` on `PATH`; the hook scripts themselves use only Python's
standard library.

```text
.claude/
  settings.json             SessionStart and PreToolUse registration
  hooks/discover_tools.py   Publish/query the live tool registry
  hooks/pre_tool_use.py     Shortlist tools, route, and request a re-plan
```

## Install

Install the router and set the provider key before starting Claude Code:

```bash
uv tool install --force --with 'mcp>=2,<3' \
  'git+https://github.com/Protocol-Lattice/harness-router.git@main'
export OPENROUTER_API_KEY="your-key"
```

For this repository, the hooks are already registered. Start a new Claude Code
session in the project.

To install in another project, run the universal installer from its root:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider claude
```

It merges existing settings and hooks, backs up changed files as
`*.harness-router.bak`, and updates `.gitignore`. Re-running it does not duplicate
the hooks. Use `--provider codex` or `--provider both` for the other integrations;
Codex installation requires the Git repository root. `--project PATH` selects a
different destination. To install from a local checkout without downloading:

```bash
python3 /path/to/harness-router/scripts/install_hook.py \
  --provider claude --project /path/to/project --source /path/to/harness-router
```

Manual installation also works: copy `hooks/` and merge the hook entries from
`settings.json` into the project's `.claude/settings.json`.
The quoted `$CLAUDE_PROJECT_DIR` paths work when the project path contains spaces
and when a tool changes the working directory. No `.codex` files are needed.

Add generated state to the target project's `.gitignore`:

```gitignore
.claude/harness-router-tools.json
.claude/harness-router/
.claude/settings.local.json
*.harness-router.bak
```

## Behavior

`SessionStart` builds a separate catalog for each session, including starts,
resumes, clears, and compactions. It discovers both tool inventories from Claude:

- `claude mcp serve` → every page of `tools/list`, including native tool
  descriptions, schemas, and other metadata returned by Claude.
- Claude's SDK control interface → `mcp_status` for configured MCP servers'
  tools, including descriptions and annotations when available.

There is no hard-coded built-in tool list. Short-lived metadata processes have
hooks disabled and a recursion guard; the SDK process also disables session
persistence. They send no user prompt and make no model turn or tool call. Claude
handles the server connections and its configured approvals.

The startup hook publishes the combined live registry to
`.claude/harness-router/sessions/<session_id>.json`, with a convenience copy at
`.claude/harness-router-tools.json`. Each snapshot includes its session ID,
provider, discovery timestamp, count, and tool descriptors. The hook exposes the
session path in Claude's startup context and exports `HARNESS_ROUTER_TOOL_REGISTRY`
through `CLAUDE_ENV_FILE` for subsequent Bash commands:

```bash
cat "$HARNESS_ROUTER_TOOL_REGISTRY"
```

To query a fresh live registry directly, using the same discovery path:

```bash
python3 .claude/hooks/discover_tools.py --list-tools
```

This prints the registry as JSON without replacing an active session's snapshot.
Use `--project /path/to/project` to query another project's configuration. The
startup hook and `PreToolUse` share the session file, matching the Codex flow.

`PreToolUse` loads that catalog, keeps the
pending tool plus up to seven similar candidates, and calls the router's `route`
MCP tool. It reads a bounded tail of the Claude transcript for the latest user
text and includes a compact version of the pending tool input in the routing
request. The provider receives this context, so use the same data-handling
expectations as for other calls to your configured routing provider.

A different catalog tool selected at confidence 0.80 or higher causes a `deny`
response asking Claude to re-plan. There is at most one redirect per user turn
and per agent, so retries and the replacement tool can proceed through normal
permission checks. On older Claude versions without `prompt_id`, the hook uses
the latest user message UUID, falling back to its text. If no user context is
available, it permits only one redirect for that session/agent.

The hook abstains on the original choice, low confidence, fallback, unknown
tools, missing dependencies/catalogs, malformed input, or router failures. It
never emits `permissionDecision: "allow"`, changes tool arguments, or executes
the selected tool. Calls to Harness Router itself are skipped.

## Tool discovery and optional overrides

The hook exposes the live registry by querying
[`claude mcp serve`](https://code.claude.com/docs/en/mcp#use-claude-code-as-an-mcp-server)
and following all `tools/list` pages for native tools. Configured MCP tool discovery
uses the CLI control interface used by
[`ClaudeSDKClient.get_mcp_status()`](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/client.py).
It imports connected servers' tool names, descriptions when available, and
annotations. The SDK status interface does not currently promise tool schemas;
older Claude versions may omit descriptions too. Server configurations and
credentials are not stored in the catalog.

Native tools come from the installed Claude version, including newly added tools,
rather than a maintained list. Their actual descriptions, schemas, aliases, and
other returned metadata are preserved. The catalog covers everything exposed by
these discovery interfaces; controls available only in the interactive UI or a
parent host session may not be exposed by the standalone CLI.

Discovery uses the project, user, and local configuration visible to a new CLI
process. Servers injected only into a parent SDK/IDE session or through transient
CLI flags may not be visible. This is a fresh startup snapshot; the hook does not
change Claude's upstream event payload or monitor later tool changes. If Claude is
unavailable, discovery fails, or a server does not connect within the deadline,
the hook keeps successfully
discovered tools and any supplied descriptors. It does not invent a replacement
catalog. Unknown tools pass through.

To supplement or override discovery, provide actual descriptors before starting Claude:

```bash
export HARNESS_ROUTER_CLAUDE_TOOLS_FILE="/absolute/path/to/claude-tools.json"
```

The file accepts an array or an object containing `tools`:

```json
{
  "tools": [
    {
      "name": "mcp__my_server__search_code",
      "description": "Search indexed source code in the current repository.",
      "inputSchema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"]
      },
      "annotations": {"readOnlyHint": true}
    }
  ]
}
```

Use the exact tool names exposed in your Claude session. Schemas, annotations,
and other supplied metadata are preserved in the catalog. Relative catalog paths
resolve from the project root. `HARNESS_ROUTER_CLAUDE_TOOLS_JSON` accepts the same
JSON inline and takes precedence for duplicate tool names. Imported descriptors
override discovered descriptors with the same name. Unknown tools pass through;
the hook does not guess MCP definitions from their first invocation.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_ROUTER_MCP_BIN` | `harness-router-mcp` on `PATH` | Router executable path |
| `HARNESS_ROUTER_CLAUDE_BIN` | `claude` on `PATH` | Claude executable for metadata discovery |
| `HARNESS_ROUTER_CLAUDE_DISCOVERY` | `1` | Set to `0` to use only imported descriptors |
| `HARNESS_ROUTER_DISCOVERY_TIMEOUT` | `8` | Startup discovery deadline, seconds |
| `HARNESS_ROUTER_PRETOOL_TIMEOUT` | `4` | Total routing deadline, seconds |
| `HARNESS_ROUTER_PRETOOL_MAX_CANDIDATES` | `8` | Shortlist size, bounded to 2–32 |
| `HARNESS_ROUTER_PRETOOL_MIN_CONFIDENCE` | `0.80` | Minimum confidence for a redirect |
| `HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD` | `0.80` | Escalate below this confidence or on fallback |
| `HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD` | unset | Optional side-effect-free graph provider |

If you increase a deadline, also increase the corresponding `SessionStart` or
`PreToolUse` timeout in `settings.json`, leaving time for process cleanup.

Optional MCTS uses the same graph-provider contract as the Codex hook: the
command receives `goal`, `observation`, `current_tool`, `candidates`, and
`route_result` as JSON on stdin. It returns `root_state`, `states`, and
`transitions`, optionally `simulations`, `max_depth`, and `use_jev_prior`.
With at least three candidates, the hook can call `route_mcts` using that graph
within the same routing deadline. The simulator must not perform real tool
actions. Without a provider, routing stays on the fast path.

Hook configuration and decision format follow the
[Claude Code hooks reference](https://code.claude.com/docs/en/hooks).
