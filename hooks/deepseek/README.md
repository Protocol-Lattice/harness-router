# DeepSeek Harness hook

Harness Router integrates with DeepSeek Harness through its supported Codex hook bridge.

DeepSeek Harness exposes the native tools/pre-execute interception point.
The dsh-hooks-codex package maps a Codex PreToolUse command hook onto that
point, so Harness Router does not invent a second DeepSeek hook protocol.

## Install

From a project root:

    python3 hooks/deepseek/install.py

Or through the universal installer:

    harness-router install deepseek

Then start DeepSeek Harness with:

    dsh --patch .dsh/harness-router.patch.yml

The patch mounts dsh-hooks-codex and points it at
.dsh/harness-router-hooks.json.

## Tool catalog

The DeepSeek command-hook bridge does not expose a faithful live tool registry
to command hooks. Harness Router therefore never invents one.

Provide the registry with:

    export HARNESS_ROUTER_DEEPSEEK_TOOLS_FILE="$PWD/.dsh/harness-router-tools.json"

or:

    export HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON='[{"name":"read_file","description":"Read a file"},{"name":"search_code","description":"Search code"}]'

If the catalog is missing or the current tool is absent, the hook fails open.

## Runtime flow

    DeepSeek Harness
          |
          v
    tools/pre-execute
          |
          v
    dsh-hooks-codex
          |
          v
    .dsh/hooks/hook.py
          |
          v
    harness-router-mcp
          |
          v
    Jev route
          |
       +--+--+
       |     |
      same  different
       |     |
       v     v
     allow  deny + re-plan

The hook never bypasses DeepSeek Harness permissions or sandboxing. A routing
decision only identifies a better candidate tool; DeepSeek remains responsible
for authorization and execution.

## Uninstall

The universal installer removes only Harness Router-owned DeepSeek files and
its patch entry. It does not remove unrelated .dsh settings or user profile
configuration.
