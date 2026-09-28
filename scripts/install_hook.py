#!/usr/bin/env python3
"""Install project hooks. Requires Python 3.11+ and Bun for ohmypi's TypeScript setup."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import os
import shlex
import shutil
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
    "ohmypi": ("ohmypi", None),
    "antigravity": ("Antigravity", ".agents/hooks.json"),
    "deepseek": ("DeepSeek Harness", ".dsh/harness-router-hooks.json"),
}
OHMYPI_ASSETS = (
    ".omp/extensions/harness-router.ts",
    ".omp/hooks/pre_tool_use.py",
    ".omp/tsconfig.json",
    ".omp/package.json",
    ".omp/bun.lock",
)
ANTIGRAVITY_ASSETS = (
    ".antigravity/hooks/pre_tool_use.py",
    ".antigravity/hooks/discover_tools.py",
)
DEEPSEEK_HOOK_CONFIG = {
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
DEEPSEEK_PATCH = """# Harness Router integration for DeepSeek Harness.
# DeepSeek maps the Codex PreToolUse bridge onto tools/pre-execute.
# Start dsh with: dsh --patch .dsh/harness-router.patch.yml

- id: harness-router-hooks-codex
  name: '@deepseek-ai/dsh-hooks-codex'
  config:
    configPath: ./.dsh/harness-router-hooks.json
    model: !!js process.env.DSH_MODEL ?? ''
"""


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
    default_type = "command" if provider == "antigravity" else None
    if handler.get("type", default_type) != "command" or not isinstance(
        handler.get("command"), str
    ):
        return False
    try:
        words = shlex.split(handler["command"])
    except ValueError:
        return False
    arguments = handler.get("args", [])
    if isinstance(arguments, list):
        words.extend(argument for argument in arguments if isinstance(argument, str))
    if provider == "deepseek":
        scripts = [".dsh/hooks/hook.py"]
    else:
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


def merge_antigravity_config(
    existing: dict[str, Any], template: dict[str, Any], root: Path
) -> dict[str, Any]:
    """Antigravity maps names to events, with ungrouped handlers for Stop."""
    incoming = copy.deepcopy(template.get("harness-router"))
    if not isinstance(incoming, dict) or not {"PreToolUse", "Stop"} <= incoming.keys():
        raise ValueError("Incomplete antigravity hook template")
    # Anchor commands to the installation, including non-Git projects and changed cwd.
    command = shlex.join(["python3", str(root / ".antigravity/hooks/pre_tool_use.py")])
    for event, suffix in (("PreToolUse", ""), ("Stop", " --stop")):
        handlers = incoming[event]
        if not isinstance(handlers, list) or not handlers:
            raise ValueError(f"Invalid antigravity hook template for {event}")
        for item in handlers:
            if not isinstance(item, dict):
                raise ValueError(f"Invalid antigravity hook template for {event}")
            nested = item.get("hooks") if event == "PreToolUse" else [item]
            if not isinstance(nested, list) or not nested:
                raise ValueError(f"Invalid antigravity hook template for {event}")
            for handler in nested:
                if not isinstance(handler, dict) or not owned_handler(handler, "antigravity"):
                    raise ValueError(f"Invalid antigravity hook template for {event}")
                handler["command"] = command + suffix

    merged = copy.deepcopy(existing)
    current = merged.setdefault("harness-router", {})
    if not isinstance(current, dict):
        raise ValueError("Existing harness-router hook must be an object; leaving config unchanged")
    pretool = merge_config(
        {"hooks": {"PreToolUse": current.get("PreToolUse", [])}},
        {"hooks": {"PreToolUse": incoming["PreToolUse"]}},
        "antigravity",
    )
    stops = current.get("Stop", [])
    if not isinstance(stops, list) or not all(isinstance(handler, dict) for handler in stops):
        raise ValueError("Existing Stop hooks must be an array of handlers; config unchanged")
    current["PreToolUse"] = pretool["hooks"]["PreToolUse"]
    current["Stop"] = [
        handler for handler in stops if not owned_handler(handler, "antigravity")
    ] + incoming["Stop"]
    # Keep enabled:false and any other events or metadata already set by the user.
    return merged


def merge_antigravity_mcp(existing: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(existing)
    servers = merged.setdefault("mcpServers", {})
    definitions = template.get("mcpServers")
    incoming = definitions.get("harness-router") if isinstance(definitions, dict) else None
    if not isinstance(servers, dict) or not isinstance(incoming, dict):
        raise ValueError("MCP configuration must contain a mcpServers object")
    if incoming.get("command") != "harness-router-mcp":
        raise ValueError("Invalid Antigravity Harness Router MCP template")
    if "harness-router" in servers:
        if not isinstance(servers["harness-router"], dict):
            raise ValueError("Existing harness-router MCP entry must be an object")
    else:
        servers["harness-router"] = {
            **incoming, "command": shutil.which("harness-router-mcp") or "harness-router-mcp"
        }
    return merged


def check_target(root: Path, path: Path) -> None:
    # Project hooks must not follow a symlink into global configuration.
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
        if provider == "deepseek":
            hook_source = asset("hooks/deepseek/hook.py", source, ref)
            ast.parse(hook_source, filename="hooks/deepseek/hook.py", feature_version=(3, 11))
            hook_target = root / ".dsh/hooks/hook.py"
            config_target = root / ".dsh/harness-router-hooks.json"
            patch_target = root / ".dsh/harness-router.patch.yml"
            check_target(root, hook_target)
            check_target(root, config_target)
            check_target(root, patch_target)
            planned[hook_target] = hook_source
            existing = (
                object_from_json(config_target.read_bytes(), str(config_target))
                if config_target.exists()
                else {}
            )
            merged = merge_config(
                existing,
                DEEPSEEK_HOOK_CONFIG,
                "deepseek",
            )
            planned[config_target] = (
                config_target.read_bytes()
                if config_target.exists() and existing == merged
                else (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode()
            )
            planned[patch_target] = DEEPSEEK_PATCH.encode()
            ignore_entries.extend([
                ".dsh/harness-router-tools.json",
                ".dsh/harness-router.patch.yml.harness-router.bak",
                ".dsh/harness-router-hooks.json.harness-router.bak",
            ])
            continue
        if provider == "ohmypi":
            # Native extension discovery needs no settings or shell-hook registration.
            for relative in OHMYPI_ASSETS:
                data = asset(relative, source, ref)
                if relative.endswith(".py"):
                    ast.parse(data, filename=relative, feature_version=(3, 11))
                elif relative.endswith(".json"):
                    object_from_json(data, relative)
                else:
                    data.decode("utf-8")
                planned[root / relative] = data
            ignore_entries.extend(
                [".omp/harness-router-tools.json", ".omp/harness-router/", ".omp/node_modules/"]
            )
            continue
        _, config_path = PROVIDERS[provider]
        assert config_path is not None
        template_path = ".antigravity/hooks.json" if provider == "antigravity" else config_path
        template = object_from_json(asset(template_path, source, ref), template_path)
        if provider != "antigravity" and not {"SessionStart", "PreToolUse"} <= template.get(
            "hooks", {}
        ).keys():
            raise ValueError(f"Incomplete {provider} hook template")
        # Fetch and validate every asset before modifying any project files.
        assets = ANTIGRAVITY_ASSETS if provider == "antigravity" else (
            f".{provider}/hooks/{name}.py" for name in ("discover_tools", "pre_tool_use")
        )
        for relative in assets:
            data = asset(relative, source, ref)
            if relative.endswith(".py"):
                ast.parse(data, filename=relative, feature_version=(3, 11))
            else:
                catalog = object_from_json(data, relative)
                if not isinstance(catalog.get("tools"), list):
                    raise ValueError(f"Invalid tool catalog: {relative}")
            planned[root / relative] = data
        config = root / config_path
        check_target(root, config)
        existing = object_from_json(config.read_bytes(), str(config)) if config.exists() else {}
        merged = (
            merge_antigravity_config(existing, template, root)
            if provider == "antigravity"
            else merge_config(existing, template, provider)
        )
        # Preserve formatting too when the existing configuration already matches.
        planned[config] = (
            config.read_bytes()
            if config.exists() and existing == merged
            else (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode()
        )
        ignore_entries.append(f".{provider}/harness-router-tools.json")
        ignore_entries.append(
            f".{provider}/harness-router/"
            if provider in {"claude", "antigravity"}
            else ".codex/harness-router/sessions/"
        )
        if provider == "claude":
            ignore_entries.append(".claude/settings.local.json")
        if provider == "antigravity":
            relative = ".antigravity/mcp_config.json"
            mcp_template = object_from_json(asset(relative, source, ref), relative)
            mcp_config = root / ".agents/mcp_config.json"
            check_target(root, mcp_config)
            existing_mcp = (
                object_from_json(mcp_config.read_bytes(), str(mcp_config))
                if mcp_config.exists() else {}
            )
            merged_mcp = merge_antigravity_mcp(existing_mcp, mcp_template)
            planned[mcp_config] = (
                mcp_config.read_bytes()
                if mcp_config.exists() and existing_mcp == merged_mcp
                else (json.dumps(merged_mcp, indent=2, ensure_ascii=False) + "\n").encode()
            )

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


def install_ohmypi_dependencies(project: Path, bun: str) -> None:
    directory = project.resolve() / ".omp"
    env = os.environ.copy()
    # The API declarations, Node/Bun types, and compiler are devDependencies.
    env["NODE_ENV"] = "development"
    print(f"Installing ohmypi TypeScript dependencies in {directory}", flush=True)
    subprocess.run(
        [bun, "install", "--frozen-lockfile", "--ignore-scripts"],
        cwd=directory,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=300,
        check=True,
    )
    print("Checking ohmypi extension types", flush=True)
    subprocess.run(
        [bun, "run", "typecheck"],
        cwd=directory,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        required=True,
        choices=[*PROVIDERS, "both", "all"],
        help="both = Codex + Claude Code; all = every supported harness",
    )
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="target project root")
    parser.add_argument(
        "--ref", default="main", help="GitHub branch, tag, or commit (default: main)"
    )
    parser.add_argument("--source", type=Path, help="copy from a local harness-router checkout")
    parser.add_argument(
        "--skip-ohmypi-deps",
        action="store_true",
        help="copy ohmypi files without installing or checking TypeScript dependencies",
    )
    args = parser.parse_args(argv)
    if args.provider == "all":
        providers = list(PROVIDERS)
    elif args.provider == "both":
        providers = ["codex", "claude"]
    else:
        providers = [args.provider]
    bun = None
    if "ohmypi" in providers and not args.skip_ohmypi_deps:
        bun = shutil.which("bun")
        if bun is None:
            print(
                "Hook installation failed: Bun is required for ohmypi's TypeScript dependencies. "
                "Install Bun, or use --skip-ohmypi-deps to copy only the files.",
                file=sys.stderr,
            )
            return 1
    try:
        changed = install(args.project, providers, args.source, args.ref)
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
        print(f"Hook installation failed: {exc}", file=sys.stderr)
        return 1
    if bun is not None:
        try:
            install_ohmypi_dependencies(args.project, bun)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"ohmypi dependency setup failed: {exc}", file=sys.stderr)
            print(
                f"Hook files are installed in {args.project.resolve()}. "
                "Resolve the error and rerun the installer to finish setup.",
                file=sys.stderr,
            )
            return 1
    names = " and ".join(PROVIDERS[provider][0] for provider in providers)
    if changed:
        print(f"Installed {names} hooks in {args.project.resolve()}")
        print(f"Updated {len(changed)} files; previous contents are saved in *{BACKUP_SUFFIX}.")
    else:
        print(f"{names} hooks are already up to date in {args.project.resolve()}")
    print("Routing requires harness-router-mcp on PATH and OPENROUTER_API_KEY.")
    if bun is not None:
        print("ohmypi TypeScript dependencies are installed and typecheck passed.")
    elif "ohmypi" in providers:
        print("TypeScript dependency setup was skipped. To enable editor types:")
        print(f"  cd {shlex.quote(str(args.project.resolve() / '.omp'))}")
        print("  bun install --frozen-lockfile --ignore-scripts")
        print("  bun run typecheck")
    print(f"Start a new {names} session to load the hooks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
