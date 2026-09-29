# Harness Router for ohmypi

This integration uses ohmypi's native extension runtime. It needs Python 3.11+,
`harness-router-mcp` on `PATH`, and `OPENROUTER_API_KEY`. The Python bridge uses
only the standard library; running the TypeScript extension needs no additional
npm dependencies. The installer requires Bun on `PATH` to install editor types
and check the extension automatically.

```text
.omp/
  extensions/harness-router.ts   Runtime catalog, lifecycle, tool_call interception
  hooks/pre_tool_use.py          Shortlisting, MCP routing, optional MCTS
  tsconfig.json                 TypeScript module resolution and Node/Bun globals
  package.json                  Pinned development dependencies and typecheck command
  bun.lock                      Reproducible development dependency installation
```

## Install

Install Harness Router and set the provider key:

```bash
uv tool install --force --with 'mcp>=2,<3' \
  'git+https://github.com/Protocol-Lattice/harness-router.git@main'
export OPENROUTER_API_KEY="your-key"
```

In this repository, start a new `omp` session from the project root. For another
project, run the universal installer from that project's root:

```bash
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/harness-router/main/scripts/install_hook.py \
  | python3 - --provider ohmypi
```

To install from a local checkout:

```bash
python3 /path/to/harness-router/scripts/install_hook.py \
  --provider ohmypi --project /path/to/project --source /path/to/harness-router
```

The installer copies all five files and updates `.gitignore`, preserving ohmypi's
runtime settings and other extensions. Changed files, including existing
TypeScript development files, are backed up as `*.harness-router.bak`;
reinstalling is idempotent. No Git repository is required for ohmypi alone.
`--provider all` installs Codex, Claude Code, and ohmypi; `--provider both` still
means Codex + Claude Code and requires a Git repository root.

For ohmypi, the installer then runs `bun install --frozen-lockfile --ignore-scripts`
and `bun run typecheck` inside the target `.omp` directory. This installs the
ohmypi API declarations, Node/Bun types, and TypeScript compiler, including when
`NODE_ENV=production`. Setup reports success only after the type check passes.
Rerunning the installer repairs missing dependencies even when the hook files
are already up to date. If dependency installation fails, the copied files stay
in place and the installer returns an error so setup can be retried.

Use `--skip-ohmypi-deps` for an explicit files-only/offline installation. Editor
types will remain unavailable until you run the development setup commands below.

ohmypi auto-discovers `<cwd>/.omp/extensions`, without walking parent directories.
Start in the project root. With discovery disabled, explicitly load the extension:

```bash
omp --no-extensions --extension .omp/extensions/harness-router.ts
```

The Python bridge is resolved relative to the extension, so paths with spaces
and a different runtime working directory work. Snapshots belong to the runtime
working directory. To uninstall, remove the two integration files.

## Live tool discovery

From event handlers, the extension reads `pi.getAllTools()` and filters by the
names returned by `pi.getActiveTools()`. This supplies actual runtime tool names,
descriptions, parameter schemas, and source metadata, including tools exposed by
the runtime from MCP servers and other extensions. It never synthesizes a catalog
or starts another ohmypi process. Schemas and metadata are kept in the local
snapshot; the fast route sends compact name/description/category/risk descriptors.
Tools without risk annotations use a conservative `medium` classification.

Session start/switch/branch/tree and new user prompts publish diagnostic snapshots:

```text
.omp/harness-router/sessions/<sha256-session-id>.json
.omp/harness-router-tools.json
```

Inspect the most recently published snapshot with:

```bash
cat .omp/harness-router-tools.json
```

Every `tool_call` reads the registry again, so routing uses live active tools even
when a snapshot predates a tool change or cannot be written. Disabled tools and
Harness Router's own tools are excluded. Before blocking, the extension checks
that the selected alternative is still active.

## Routing behavior

The hook supplies the latest user goal and a compact pending tool input to the
router. Those are sent to your configured routing provider. It includes the
pending tool plus up to seven similar alternatives, then calls the MCP `route`
tool. A different shortlisted tool at confidence 0.80 or higher produces an
ohmypi `{ block: true, reason }` result asking the planner to re-plan.

One redirect is allowed per user turn, including simultaneous pending calls. A
custom session entry records the redirect so it can be restored on resume or
branch navigation. New user work resets the in-memory guard; model/tool turns do
not. Results from a previous prompt or session are ignored.

The extension abstains when the router keeps the original choice, falls back,
returns malformed or low-confidence data, times out, or is unavailable. Unknown
tools and missing runtime APIs also pass through. All errors are contained because
ohmypi normally blocks a tool when a `tool_call` handler throws. The extension
does not replace tool arguments, execute an alternative, or grant permissions.

Interception covers AgentTool calls exposed through `tool_call`. Host bridge
operations inside eval (for example direct browser/computer helpers) do not emit
that event and cannot be intercepted by this extension.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARNESS_ROUTER_PYTHON_BIN` | `python3` | Python executable path, without arguments |
| `HARNESS_ROUTER_MCP_URL` | `http://127.0.0.1:8765/mcp` | Persistent MCP HTTP endpoint |
| `HARNESS_ROUTER_PRETOOL_TIMEOUT` | `4` | Total MCP/graph routing deadline in seconds |
| `HARNESS_ROUTER_PRETOOL_MAX_CANDIDATES` | `8` | Shortlist size, bounded to 2–32 |
| `HARNESS_ROUTER_PRETOOL_MIN_CONFIDENCE` | `0.80` | Minimum confidence for a redirect |
| `HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD` | `0.80` | Escalate below this confidence or on fallback |
| `HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD` | unset | Side-effect-free graph provider command |

The extension allows one extra second for Python startup and MCP cleanup, and
terminates the bridge process group on POSIX when its outer deadline expires.
Invalid timeout settings, including values above 3600 seconds, disable routing.

Optional MCTS follows the Codex/Claude graph contract: the configured command is
split into arguments without a shell and receives `goal`, `observation`,
`current_tool`, `candidates`, and `route_result` as JSON on stdin. It returns
`root_state`, `states`, and `transitions`, optionally `simulations`, `max_depth`,
and `use_jev_prior`. With at least three candidates and a fallback/low-confidence
fast result, `route_mcts` runs within the same deadline. The simulator must not
execute actual tool actions. Without a graph provider, only `route` is called.

## TypeScript development

The `.omp/package.json` and `.omp/tsconfig.json` files provide editor and compiler
support for the extension. The installer includes them with `.omp/bun.lock`.
They are development files, separate from ohmypi's automatic extension
registration. The installer installs and checks them automatically. To repeat
the checks, or finish an installation made with `--skip-ohmypi-deps`, run from
the installed project's root:

```bash
cd .omp
bun install --frozen-lockfile --ignore-scripts
bun run typecheck
```

The TypeScript project uses Node and Bun globals and resolves the real ohmypi API
through its published package exports. These development dependencies are not
needed to run the installed hook. CI runs the same type check.

Runtime contracts: [extension API](https://github.com/can1357/oh-my-pi/blob/main/docs/extensions.md),
[discovery paths](https://github.com/can1357/oh-my-pi/blob/main/docs/extension-loading.md),
and [hook interception limits](https://github.com/can1357/oh-my-pi/blob/main/docs/hooks.md).
