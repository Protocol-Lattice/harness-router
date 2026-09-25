#!/usr/bin/env python3
"""Local UTCP CLI provider exposing filesystem and bash tools.

This provider is intentionally workspace-scoped. Filesystem operations cannot
escape the directory from which the provider is launched.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


WORKSPACE = Path.cwd().resolve()
SELF = Path(__file__).resolve()


def _command_prefix() -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(SELF))}"


def _resolve_workspace_path(raw: str) -> Path:
    candidate = (WORKSPACE / raw).resolve(strict=False)
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise ValueError(f"path escapes workspace: {raw}")
    return candidate


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(WORKSPACE))
    except ValueError:
        return str(path)


def manual() -> dict[str, Any]:
    prefix = _command_prefix()
    return {
        "manual_version": "1.0.0",
        "name": "Local harness tools",
        "description": "Workspace-scoped filesystem and bash tools for harness-router examples.",
        "tools": [
            {
                "name": "fs_read",
                "description": "Read a UTF-8 text file inside the current workspace.",
                "inputs": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                "outputs": {"type": "object"},
                "tags": ["filesystem", "read", "inspect"],
                "tool_call_template": {
                    "call_template_type": "cli",
                    "commands": [
                        {"command": f"{prefix} read --path UTCP_ARG_path_UTCP_END"}
                    ],
                    "working_dir": str(WORKSPACE),
                },
            },
            {
                "name": "fs_list",
                "description": "List files and directories inside the current workspace.",
                "inputs": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                "outputs": {"type": "object"},
                "tags": ["filesystem", "list", "inspect"],
                "tool_call_template": {
                    "call_template_type": "cli",
                    "commands": [
                        {"command": f"{prefix} list --path UTCP_ARG_path_UTCP_END"}
                    ],
                    "working_dir": str(WORKSPACE),
                },
            },
            {
                "name": "fs_write",
                "description": "Write UTF-8 text to a file inside the current workspace.",
                "inputs": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                "outputs": {"type": "object"},
                "tags": ["filesystem", "write", "mutate"],
                "tool_call_template": {
                    "call_template_type": "cli",
                    "commands": [
                        {
                            "command": (
                                f"{prefix} write "
                                "--path UTCP_ARG_path_UTCP_END "
                                "--content UTCP_ARG_content_UTCP_END"
                            )
                        }
                    ],
                    "working_dir": str(WORKSPACE),
                },
            },
            {
                "name": "fs_patch",
                "description": (
                    "Patch a UTF-8 text file by replacing one unique exact text block "
                    "inside the current workspace."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {"type": "string"},
                        "new_text": {"type": "string"},
                    },
                    "required": ["path", "old_text", "new_text"],
                },
                "outputs": {"type": "object"},
                "tags": ["filesystem", "patch", "mutate"],
                "tool_call_template": {
                    "call_template_type": "cli",
                    "commands": [
                        {
                            "command": (
                                f"{prefix} patch "
                                "--path UTCP_ARG_path_UTCP_END "
                                "--old-text UTCP_ARG_old_text_UTCP_END "
                                "--new-text UTCP_ARG_new_text_UTCP_END"
                            )
                        }
                    ],
                    "working_dir": str(WORKSPACE),
                },
            },
            {
                "name": "bash_run",
                "description": (
                    "Run an arbitrary Bash command in the current workspace. "
                    "This is a high-risk tool and should require explicit approval."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                "outputs": {"type": "object"},
                "tags": ["bash", "shell", "execute"],
                "tool_call_template": {
                    "call_template_type": "cli",
                    "commands": [
                        {
                            "command": (
                                f"{prefix} bash "
                                "--command UTCP_ARG_command_UTCP_END"
                            )
                        }
                    ],
                    "working_dir": str(WORKSPACE),
                },
            },
        ],
    }


def do_read(path_raw: str) -> dict[str, Any]:
    path = _resolve_workspace_path(path_raw)
    if not path.is_file():
        raise ValueError(f"not a file: {path_raw}")
    return {"path": _relative(path), "content": path.read_text(encoding="utf-8")}


def do_list(path_raw: str) -> dict[str, Any]:
    path = _resolve_workspace_path(path_raw)
    if not path.is_dir():
        raise ValueError(f"not a directory: {path_raw}")
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
        resolved = child.resolve(strict=False)
        if resolved != WORKSPACE and WORKSPACE not in resolved.parents:
            continue
        entries.append(
            {
                "name": child.name,
                "path": _relative(child),
                "type": "directory" if child.is_dir() else "file",
            }
        )
    return {"path": _relative(path), "entries": entries}


def do_write(path_raw: str, content: str) -> dict[str, Any]:
    path = _resolve_workspace_path(path_raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"path": _relative(path), "bytes_written": len(content.encode("utf-8"))}


def do_patch(path_raw: str, old_text: str, new_text: str) -> dict[str, Any]:
    path = _resolve_workspace_path(path_raw)
    if not path.is_file():
        raise ValueError(f"not a file: {path_raw}")
    content = path.read_text(encoding="utf-8")
    matches = content.count(old_text)
    if matches != 1:
        raise ValueError(
            f"patch requires exactly one match for old_text; found {matches}"
        )
    updated = content.replace(old_text, new_text, 1)
    path.write_text(updated, encoding="utf-8")
    return {
        "path": _relative(path),
        "replacements": 1,
        "bytes_written": len(updated.encode("utf-8")),
    }


def do_bash(command: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["/bin/bash", "-lc", command],
        cwd=WORKSPACE,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action")

    read = sub.add_parser("read")
    read.add_argument("--path", required=True)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--path", required=True)

    write = sub.add_parser("write")
    write.add_argument("--path", required=True)
    write.add_argument("--content", required=True)

    patch = sub.add_parser("patch")
    patch.add_argument("--path", required=True)
    patch.add_argument("--old-text", required=True)
    patch.add_argument("--new-text", required=True)

    bash = sub.add_parser("bash")
    bash.add_argument("--command", required=True)

    return parser


def main() -> None:
    if len(sys.argv) == 1:
        print(json.dumps(manual()))
        return

    args = build_parser().parse_args()

    try:
        if args.action == "read":
            result = do_read(args.path)
        elif args.action == "list":
            result = do_list(args.path)
        elif args.action == "write":
            result = do_write(args.path, args.content)
        elif args.action == "patch":
            result = do_patch(args.path, args.old_text, args.new_text)
        elif args.action == "bash":
            result = do_bash(args.command)
        else:
            raise ValueError(f"unknown action: {args.action}")
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        raise SystemExit(1) from exc

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
