#!/usr/bin/env python3
"""Install the Harness Router hook bridge for DeepSeek Harness."""

from __future__ import annotations

import json
import os
from pathlib import Path

HOOK_CONFIG = {
    "description": "Route DeepSeek Harness tool calls through Harness Router.",
    "hooks": {
        "PreToolUse": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "python3 .dsh/hooks/hook.py",
                        "timeout": 5,
                    }
                ]
            }
        ]
    },
}

PATCH = """# Harness Router integration for DeepSeek Harness.
# DeepSeek maps the Codex PreToolUse bridge onto tools/pre-execute.
# Start dsh with: dsh --patch .dsh/harness-router.patch.yml

- id: harness-router-hooks-codex
  name: '@deepseek-ai/dsh-hooks-codex'
  config:
    configPath: ./.dsh/harness-router-hooks.json
    model: !!js process.env.DSH_MODEL ?? ''
"""


def install(project: Path) -> list[Path]:
    project = project.resolve()
    if not project.is_dir():
        raise ValueError(f"Project directory does not exist: {project}")

    source = Path(__file__).with_name("hook.py")
    hook = project / ".dsh/hooks/hook.py"
    config = project / ".dsh/harness-router-hooks.json"
    patch = project / ".dsh/harness-router.patch.yml"

    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    config.write_text(json.dumps(HOOK_CONFIG, indent=2) + "\n", encoding="utf-8")
    patch.write_text(PATCH, encoding="utf-8")
    return [hook, config, patch]


def main() -> int:
    project = Path(os.environ.get("HARNESS_ROUTER_PROJECT", os.getcwd()))
    try:
        changed = install(project)
    except (OSError, ValueError) as exc:
        print(f"DeepSeek Harness installation failed: {exc}", file=__import__("sys").stderr)
        return 1

    print(f"Installed DeepSeek Harness hook in {project.resolve()}")
    print("Start DeepSeek Harness with:")
    print("  dsh --patch .dsh/harness-router.patch.yml")
    print("Tool metadata is read from HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON,")
    print("HARNESS_ROUTER_DEEPSEEK_TOOLS_FILE, or .dsh/harness-router-tools.json.")
    print(f"Updated {len(changed)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
