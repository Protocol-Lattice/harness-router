#!/usr/bin/env python3
"""UTCP CodeMode example with filesystem and Bash tools.

Flow:
    goal -> OpenRouter Jev workflow selection -> CodeMode program
    -> multiple UTCP tool calls in one execution

Examples:

Inspect a file:
    python examples/utcp_codemode.py \
      --goal "Inspect README.md and show the repository layout" \
      --path README.md \
      --execute

Write and verify:
    python examples/utcp_codemode.py \
      --goal "Write hello.txt with hello and verify it" \
      --path hello.txt \
      --content "hello" \
      --execute \
      --allow-mutation

Patch and verify:
    python examples/utcp_codemode.py \
      --goal "Patch hello.txt from hello to hello world and verify it" \
      --path hello.txt \
      --old-text "hello" \
      --new-text "hello world" \
      --execute \
      --allow-mutation

Run Bash:
    python examples/utcp_codemode.py \
      --goal "Show git status" \
      --command "git status --short" \
      --execute \
      --allow-bash
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from harness_router import (  # noqa: E402
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RiskLevel,
    RoutingConfig,
    ToolDescriptor,
)
from utcp_code_mode import CodeModeUtcpClient  # noqa: E402


PROVIDER = Path(__file__).with_name("utcp_local_tools_provider.py").resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Use Jev to choose a CodeMode workflow, then execute multiple UTCP "
            "filesystem/Bash tools in one sandboxed program."
        )
    )
    parser.add_argument("--goal", required=True)
    parser.add_argument("--observation", default=None)

    parser.add_argument("--path", default="README.md")
    parser.add_argument("--content", default=None)
    parser.add_argument("--old-text", default=None)
    parser.add_argument("--new-text", default=None)
    parser.add_argument("--command", default=None)

    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the selected CodeMode workflow after routing.",
    )
    parser.add_argument(
        "--allow-mutation",
        action="store_true",
        help="Allow write/patch workflows.",
    )
    parser.add_argument(
        "--allow-bash",
        action="store_true",
        help="Allow arbitrary Bash workflow execution.",
    )
    parser.add_argument(
        "--show-code",
        action="store_true",
        help="Print the generated CodeMode program before execution.",
    )
    return parser


def workflow_tools() -> list[ToolDescriptor]:
    return [
        ToolDescriptor(
            name="codemode.inspect",
            description=(
                "Use CodeMode to list the repository and read a target file "
                "in one workflow."
            ),
            category="inspect",
            risk=RiskLevel.LOW,
        ),
        ToolDescriptor(
            name="codemode.write_verify",
            description=(
                "Use CodeMode to write a target file and immediately read it back "
                "to verify the result."
            ),
            category="mutate",
            risk=RiskLevel.MEDIUM,
        ),
        ToolDescriptor(
            name="codemode.patch_verify",
            description=(
                "Use CodeMode to patch one exact text block in a file and read the "
                "file back to verify the change."
            ),
            category="mutate",
            risk=RiskLevel.MEDIUM,
        ),
        ToolDescriptor(
            name="codemode.bash",
            description=(
                "Use CodeMode to execute a Bash command in the current workspace."
            ),
            category="execute",
            risk=RiskLevel.HIGH,
        ),
    ]


def py_string(value: str) -> str:
    """Return a deterministic Python string literal."""
    return repr(value)


def build_workflow_code(args: argparse.Namespace, workflow: str) -> str:
    if workflow == "codemode.inspect":
        return f"""
listing = local.fs_list(path=".")
target = local.fs_read(path={py_string(args.path)})
print("Inspected repository and target file")
return {{"listing": listing, "file": target}}
""".strip()

    if workflow == "codemode.write_verify":
        if args.content is None:
            raise SystemExit("--content is required for the write workflow")
        return f"""
written = local.fs_write(
    path={py_string(args.path)},
    content={py_string(args.content)},
)
verified = local.fs_read(path={py_string(args.path)})
print("File written and verified")
return {{"write": written, "read": verified}}
""".strip()

    if workflow == "codemode.patch_verify":
        if args.old_text is None or args.new_text is None:
            raise SystemExit(
                "--old-text and --new-text are required for the patch workflow"
            )
        return f"""
patched = local.fs_patch(
    path={py_string(args.path)},
    old_text={py_string(args.old_text)},
    new_text={py_string(args.new_text)},
)
verified = local.fs_read(path={py_string(args.path)})
print("File patched and verified")
return {{"patch": patched, "read": verified}}
""".strip()

    if workflow == "codemode.bash":
        if args.command is None:
            raise SystemExit("--command is required for the Bash workflow")
        return f"""
result = local.bash_run(command={py_string(args.command)})
print("Bash workflow completed")
return result
""".strip()

    raise SystemExit(f"unsupported workflow: {workflow}")


def check_gate(
    workflow: str,
    *,
    allow_mutation: bool,
    allow_bash: bool,
) -> None:
    if workflow in {"codemode.write_verify", "codemode.patch_verify"}:
        if not allow_mutation:
            raise SystemExit(
                "selected CodeMode workflow mutates files; "
                "re-run with --allow-mutation"
            )

    if workflow == "codemode.bash" and not allow_bash:
        raise SystemExit(
            "selected CodeMode workflow executes arbitrary Bash; "
            "re-run with --allow-bash"
        )


async def main() -> None:
    args = build_parser().parse_args()
    workspace = Path.cwd().resolve()

    provider_command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(PROVIDER))}"
    )

    client = await CodeModeUtcpClient.create(
        root_dir=str(workspace),
        config={
            "manual_call_templates": [
                {
                    "name": "local",
                    "call_template_type": "cli",
                    "commands": [{"command": provider_command}],
                    "working_dir": str(workspace),
                }
            ]
        },
    )

    jev = OpenRouterJevProvider.from_config(OpenRouterConfig())
    router = JevToolRouter(jev, RoutingConfig())

    try:
        tools = workflow_tools()
        decision = await router.route(
            HarnessState(
                goal=args.goal,
                observation=args.observation,
            ),
            tools,
        )

        print("Jev workflow decision:")
        print(
            json.dumps(
                {
                    **asdict(decision),
                    "probabilities": dict(decision.probabilities),
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )

        if decision.fallback or decision.tool is None:
            return

        code = build_workflow_code(args, decision.tool)

        if args.show_code:
            print("\nCodeMode program:")
            print(code)

        if not args.execute:
            print("\nDry run only. Add --execute to run the CodeMode workflow.")
            return

        check_gate(
            decision.tool,
            allow_mutation=args.allow_mutation,
            allow_bash=args.allow_bash,
        )

        result = await client.call_tool_chain(code, timeout=30)

        print("\nCodeMode result:")
        print(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
    finally:
        await jev.aclose()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
