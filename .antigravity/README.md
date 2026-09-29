# Harness Router for Antigravity

The project integration registers `harness-router-mcp` and intercepts tool calls
with Antigravity's native `PreToolUse` hook. The router selects a tool; Antigravity
supplies its arguments and executes it under its normal permissions.

Requires Python 3.11+, `harness-router-mcp` on `PATH`, and `OPENROUTER_API_KEY` in
Antigravity's environment for Jev routing.

## Install

From a local Harness Router checkout:

```bash
python3 /path/to/harness-router/scripts/install_hook.py \
  --provider antigravity --project /path/to/project --source /path/to/harness-router
```

Or download the installer from the project root:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider antigravity
```

The installer creates or merges these native registrations:

```text
.agents/hooks.json                    Named PreToolUse and Stop hooks
.agents/mcp_config.json               harness-router MCP server
.antigravity/hooks/discover_tools.py  Live conversation inventory
.antigravity/hooks/pre_tool_use.py    Routing and redirect guard
```

Existing hooks, MCP servers and explicit router settings are preserved. Changed
files receive `*.harness-router.bak` backups. Reinstalling is idempotent. Commands
use quoted absolute paths, so projects can contain spaces and need not use Git.
The JSON files in this source directory are installer templates; Antigravity
loads the installed registrations from `.agents/`.

Restart the Antigravity session, or refresh its hooks and MCP servers. Both
`route` and `route_mcts` are exposed through the registered server:

```json
{
  "mcpServers": {
    "harness-router": {"command": "harness-router-mcp", "args": []}
  }
}
```

The installer resolves an installed executable to its absolute path when
possible. It does not copy API keys into project files.

## Live inventory

Before routing a pending call, the hook queries the owning Antigravity runtime's
`GetCascadeTrajectoryGeneratorMetadata` endpoint for that conversation. It reads
the latest model invocation's actual `chatModel.tools`: exact names, descriptions,
input/output schemas, MCP server attribution and original names. It also checks
`GetMcpServerStates` to remove disconnected or disabled MCP tools.

There is no built-in fallback list and no agent/model turn for discovery. Only
tools exposed to this conversation are candidates; global MCP entries are never
added to a restricted subagent's catalog. Deferred tools become candidates when
the runtime exposes their definitions to the conversation. Newly removed tools
disappear on the next routing check. Failure to refresh causes the hook to
abstain, rather than use an old inventory.

Successful snapshots are written atomically to:

```text
.antigravity/harness-router/sessions/<hashed-conversation-id>.json
.antigravity/harness-router-tools.json
```

Snapshots contain tool metadata and discovery timestamps, not runtime tokens,
server credentials or conversation messages. The session snapshot also tracks a
metadata offset to avoid repeatedly fetching the entire invocation history.

These are internal runtime APIs. Their request/response schemas were inspected
in Antigravity 2.11.0; future runtime changes may require adapter updates. Hook
and installer tests exercise their JSON contract with a local runtime fixture.
The public hook payload itself does not supply a tool catalog.

On macOS/Linux, hooks locate the owning runtime through their parent process
chain. Outside that chain, on Windows, or when the runtime starts with a dynamic
port (`0`), configure `HARNESS_ROUTER_ANTIGRAVITY_URL` and, if required,
`HARNESS_ROUTER_ANTIGRAVITY_CSRF_TOKEN`. Only loopback URLs are accepted. Proxy
settings and redirects are disabled for these authenticated local requests.

To inspect a conversation through an explicitly configured runtime connection:

```bash
python3 .antigravity/hooks/discover_tools.py --list-tools --conversation-id CONVERSATION_ID
```

For hosts with a separate inventory bridge, `HARNESS_ROUTER_ANTIGRAVITY_TOOLS_FILE`
or `HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON` can supply an authoritative array of
descriptors or an object containing `tools`. Inline JSON takes precedence;
relative file paths resolve from the project root. These explicit overrides
replace runtime discovery, including when the supplied catalog is empty.

## Routing

`PreToolUse` shortlists the pending tool and up to seven relevant alternatives,
then calls `route` over MCP. The request includes compact pending-tool arguments
and intent. A different known tool at confidence 0.80 or higher produces a
native `deny` with a re-plan reason. The hook never substitutes arguments,
executes the recommendation, or emits a permission `allow` override.

An atomic guard permits one redirect per conversation until `Stop` reports
`fullyIdle: true`. Retries and the replacement tool then proceed normally. A
missed idle event conservatively leaves routing disabled for that conversation.
Router calls are excluded by name and server attribution to prevent recursion.
Missing inventory, dependencies, low confidence, fallbacks, malformed input and
timeouts all abstain with `{}`. Use a current Antigravity release that handles
empty hook decisions (CLI support was fixed in 1.0.16).

The hook starts with **`route`**. With at least three candidates, a low-confidence
or fallback result can escalate to **`route_mcts`** if this is configured:

```bash
export HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD="/path/to/build_mcts_graph.py"
```

The side-effect-free graph provider receives `goal`, `observation`,
`current_tool`, `candidates` (with schemas) and `route_result` on stdin. It returns
`root_state`, `states` and `transitions`, optionally `simulations`, `max_depth`
and `use_jev_prior`. Both decisions use the same MCP connection and routing
deadline. Without a simulator, the hook uses `route` only. Antigravity can also
invoke either registered MCP tool directly with the required inputs.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_ROUTER_MCP_URL` | `http://127.0.0.1:8765/mcp` | Persistent MCP HTTP endpoint |
| `HARNESS_ROUTER_DISCOVERY_TIMEOUT` | `2` | Live inventory deadline in seconds |
| `HARNESS_ROUTER_PRETOOL_TIMEOUT` | `4` | Routing deadline in seconds |
| `HARNESS_ROUTER_PRETOOL_MAX_CANDIDATES` | `8` | Shortlist size, bounded to 2–32 |
| `HARNESS_ROUTER_PRETOOL_MIN_CONFIDENCE` | `0.80` | Minimum redirect confidence |
| `HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD` | `0.80` | MCTS escalation threshold |

If you increase deadlines, increase the installed `PreToolUse` timeout too
(default 8 seconds). The installer ignores generated inventory, guard files and
backups in Git.

Native registration formats follow the official [hooks](https://antigravity.google/docs/hooks)
and [MCP configuration](https://antigravity.google/docs/mcp) references. Empty
decision support is recorded in the [CLI changelog](https://www.antigravity.google/changelog?tab=cli).
