#!/usr/bin/env python3
"""Install project hooks for Codex, Claude Code, or both. Python 3.11+, no dependencies."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY = "Protocol-Lattice/harness-router"
BACKUP_SUFFIX = ".harness-router.bak"
PROVIDERS = {
    "codex": ("Codex", ".codex/hooks.json"),
    "claude": ("Claude Code", ".claude/settings.json"),
}


def asset(path: str, source: Path | None, ref: str) -> bytes:
    if source is not None:
        return (source / path).read_bytes()
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{quote(ref, safe='')}/{path}"
    request = Request(url, headers={"User-Agent": "harness-router-hook-installer"})
    with urlopen(request, timeout=20) as response:
        data = response.read(1_000_001)
    if len(data) > 1_000_000:
        raise ValueError(f"Hook asset is unexpectedly large: {path}")
    return data


def object_from_json(data: bytes, label: str) -> dict[str, Any]:
    try:
        result = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid JSON; leaving it unchanged") from exc
    if not isinstance(result, dict) or not isinstance(result.get("hooks", {}), dict):
        raise ValueError(f"{label} must contain a JSON object with a hooks object")
    return result


def owned_handler(handler: dict[str, Any], provider: str) -> bool:
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        return False
    try:
        words = shlex.split(handler["command"])
    except ValueError:
        return False
    arguments = handler.get("args", [])
    if isinstance(arguments, list):
        words.extend(argument for argument in arguments if isinstance(argument, str))
    scripts = [f".{provider}/hooks/{name}.py" for name in ("discover_tools", "pre_tool_use")]
    return any(
        word == script or word.endswith("/" + script) for word in words for script in scripts
    )


def merge_config(
    existing: dict[str, Any], template: dict[str, Any], provider: str
) -> dict[str, Any]:
    merged = copy.deepcopy(existing)
    hooks = merged.setdefault("hooks", {})
    for event, incoming in template.get("hooks", {}).items():
        if not isinstance(incoming, list) or not incoming:
            raise ValueError(f"Invalid {provider} hook template for {event}")
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Existing {event} hooks must be an array; leaving config unchanged")
        preserved = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"Invalid existing {event} hook group; leaving config unchanged")
            if not all(isinstance(handler, dict) for handler in group["hooks"]):
                raise ValueError(f"Invalid existing {event} hook handler; leaving config unchanged")
            remaining = [
                handler for handler in group["hooks"] if not owned_handler(handler, provider)
            ]
            if remaining or not group["hooks"]:
                preserved.append({**group, "hooks": remaining})
        hooks[event] = preserved + copy.deepcopy(incoming)
    for key, value in template.items():
        if key != "hooks":
            merged.setdefault(key, value)
    return merged


def check_target(root: Path, path: Path) -> None:
    # Project hooks must not follow a .claude/.codex symlink into global settings.
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError(f"Refusing to replace or follow a symlink: {candidate}")
        if candidate.exists() and candidate != path and not candidate.is_dir():
            raise ValueError(f"Expected a directory: {candidate}")
    if path.exists() and not path.is_file():
        raise ValueError(f"Expected a file: {path}")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def install(project: Path, providers: list[str], source: Path | None, ref: str) -> list[Path]:
    root = project.resolve()
    if not root.is_dir():
        raise ValueError(f"Project directory does not exist: {root}")
    if "codex" in providers:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode or Path(result.stdout.strip()).resolve() != root:
            raise ValueError("Install Codex hooks at the Git repository root; use --project PATH")

    planned: dict[Path, bytes] = {}
    ignore_entries = ["*.harness-router.bak"]
    for provider in providers:
        _, config_path = PROVIDERS[provider]
        template = object_from_json(asset(config_path, source, ref), config_path)
        if not {"SessionStart", "PreToolUse"} <= template.get("hooks", {}).keys():
            raise ValueError(f"Incomplete {provider} hook template")
        # Fetch and validate every asset before modifying any project files.
        for name in ("discover_tools", "pre_tool_use"):
            relative = f".{provider}/hooks/{name}.py"
            data = asset(relative, source, ref)
            ast.parse(data, filename=relative, feature_version=(3, 11))
            planned[root / relative] = data
        config = root / config_path
        check_target(root, config)
        existing = object_from_json(config.read_bytes(), str(config)) if config.exists() else {}
        merged = merge_config(existing, template, provider)
        # Preserve formatting too when the existing configuration already matches.
        planned[config] = (
            config.read_bytes()
            if config.exists() and existing == merged
            else (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode()
        )
        ignore_entries.append(f".{provider}/harness-router-tools.json")
        ignore_entries.append(
            f".{provider}/harness-router/"
            if provider == "claude"
            else ".codex/harness-router/sessions/"
        )
        if provider == "claude":
            ignore_entries.append(".claude/settings.local.json")

    ignore = root / ".gitignore"
    check_target(root, ignore)
    old_ignore = ignore.read_bytes() if ignore.exists() else b""
    ignored_lines = old_ignore.decode("utf-8").splitlines()
    missing = [entry for entry in ignore_entries if entry not in ignored_lines]
    if missing:
        newline = "\r\n" if b"\r\n" in old_ignore else "\n"
        separator = newline if old_ignore and not old_ignore.endswith(b"\n") else ""
        addition = separator + newline + "# Harness Router hooks" + newline
        planned[ignore] = old_ignore + (addition + newline.join(missing) + newline).encode()

    changes = []
    for path, data in planned.items():
        check_target(root, path)
        if path.exists() and path.read_bytes() == data:
            continue
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        check_target(root, backup)
        changes.append((path, data, backup))
    for path, data, backup in changes:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        if path.exists():
            atomic_write(backup, path.read_bytes(), mode)
        atomic_write(path, data, mode)
    return [path for path, _, _ in changes]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=[*PROVIDERS, "both"])
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="target project root")
    parser.add_argument(
        "--ref", default="main", help="GitHub branch, tag, or commit (default: main)"
    )
    parser.add_argument("--source", type=Path, help="copy from a local harness-router checkout")
    args = parser.parse_args(argv)
    providers = list(PROVIDERS) if args.provider == "both" else [args.provider]
    try:
        changed = install(args.project, providers, args.source, args.ref)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
        print(f"Hook installation failed: {exc}", file=sys.stderr)
        return 1
    names = " and ".join(PROVIDERS[provider][0] for provider in providers)
    if changed:
        print(f"Installed {names} hooks in {args.project.resolve()}")
        print(f"Updated {len(changed)} files; previous contents are saved in *{BACKUP_SUFFIX}.")
    else:
        print(f"{names} hooks are already up to date in {args.project.resolve()}")
    print("Routing requires harness-router-mcp on PATH and OPENROUTER_API_KEY.")
    print(f"Start a new {names} session to load the hooks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
