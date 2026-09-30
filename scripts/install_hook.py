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
COMMON_ASSETS = ("hooks/pre_decision.py", "hooks/post_tool_use.py")
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
    ".antigravity/hooks/post_tool_use.py",
    ".antigravity/hooks/pre_invocation.py",
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
        ],
        "PostToolUse": [
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
# DeepSeek maps the bridge onto pre/post tool execution points.
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
        scripts = [f".{provider}/hooks/{name}.py" for name in ("discover_tools", "pre_decision", "pre_tool_use", "post_tool_use", "pre_invocation", "stop")]
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
    """Merge Antigravity named hook entries while preserving unrelated hooks."""
    incoming = copy.deepcopy(template.get("harness-router"))
    required = {"PreToolUse", "PostToolUse", "PreInvocation", "Stop"}
    if not isinstance(incoming, dict) or not required <= incoming.keys():
        raise ValueError("Incomplete antigravity hook template")

    merged = copy.deepcopy(existing)
    current = merged.setdefault("harness-router", {})
    if not isinstance(current, dict):
        raise ValueError("Existing harness-router hook must be an object; leaving config unchanged")

    commands = {
        "PreToolUse": shlex.join(["python3", str(root / ".antigravity/hooks/pre_tool_use.py")]),
        "PostToolUse": shlex.join(["python3", str(root / ".antigravity/hooks/post_tool_use.py")]),
        "PreInvocation": shlex.join(["python3", str(root / ".antigravity/hooks/pre_invocation.py")]),
        "Stop": shlex.join(["python3", str(root / ".antigravity/hooks/pre_tool_use.py"), "--stop"]),
    }

    for event in ("PreToolUse", "PostToolUse"):
        incoming_groups = incoming[event]
        if not isinstance(incoming_groups, list) or not incoming_groups:
            raise ValueError(f"Invalid antigravity hook template for {event}")
        existing_groups = current.get(event, [])
        if not isinstance(existing_groups, list):
            raise ValueError(f"Existing {event} hooks must be an array; leaving config unchanged")
        cleaned = merge_config(
            {"hooks": {event: existing_groups}},
            {"hooks": {event: incoming_groups}},
            "antigravity",
        )["hooks"][event]
        for group in cleaned:
            for handler in group.get("hooks", []):
                if isinstance(handler, dict) and owned_handler(handler, "antigravity"):
                    handler["command"] = commands[event]
        current[event] = cleaned

    for event in ("PreInvocation", "Stop"):
        incoming_handlers = incoming[event]
        if not isinstance(incoming_handlers, list) or not incoming_handlers:
            raise ValueError(f"Invalid antigravity hook template for {event}")
        existing_handlers = current.get(event, [])
        if not isinstance(existing_handlers, list) or not all(isinstance(h, dict) for h in existing_handlers):
            raise ValueError(f"Existing {event} hooks must be an array; leaving config unchanged")
        preserved = [h for h in existing_handlers if not owned_handler(h, "antigravity")]
        fresh = []
        for handler in incoming_handlers:
            if not isinstance(handler, dict) or not owned_handler(handler, "antigravity"):
                raise ValueError(f"Invalid antigravity hook handler for {event}")
            handler["command"] = commands[event]
            fresh.append(handler)
        current[event] = preserved + fresh

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



def uninstall_deepseek(project: Path) -> list[Path]:
    root = project.resolve()
    config = root / ".dsh/harness-router-hooks.json"
    hook = root / ".dsh/hooks/hook.py"
    patch = root / ".dsh/harness-router.patch.yml"
    removed: list[Path] = []

    if config.exists():
        check_target(root, config)
        existing = object_from_json(config.read_bytes(), str(config))
        hooks = existing.get("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError(f"{config} must contain a hooks object")
        cleaned = copy.deepcopy(existing)
        cleaned_hooks = cleaned["hooks"]
        for event, groups in list(cleaned_hooks.items()):
            if not isinstance(groups, list):
                continue
            kept_groups = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    kept_groups.append(group)
                    continue
                remaining = [h for h in group["hooks"] if not owned_handler(h, "deepseek")]
                if remaining:
                    kept_groups.append({**group, "hooks": remaining})
            if kept_groups:
                cleaned_hooks[event] = kept_groups
            else:
                cleaned_hooks.pop(event, None)

        if cleaned != existing:
            if cleaned.get("hooks"):
                atomic_write(
                    config,
                    (json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n").encode(),
                    stat.S_IMODE(config.stat().st_mode),
                )
                removed.append(config)
            else:
                config.unlink()
                removed.append(config)

    for path in (hook, patch):
        if path.exists():
            check_target(root, path)
            path.unlink()
            removed.append(path)
    return removed


def uninstall(project: Path, providers: list[str]) -> list[Path]:
    if providers != ["deepseek"]:
        raise ValueError("uninstall currently supports only the deepseek provider")
    return uninstall_deepseek(project)


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
            for relative in COMMON_ASSETS:
                data = asset(relative, source, ref)
                ast.parse(data, filename=relative, feature_version=(3, 11))
                target = root / relative
                check_target(root, target)
                planned[target] = data
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
            for relative in COMMON_ASSETS:
                data = asset(relative, source, ref)
                ast.parse(data, filename=relative, feature_version=(3, 11))
                target = root / relative
                check_target(root, target)
                planned[target] = data
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
            f".{provider}/hooks/{name}.py" for name in ("discover_tools", "pre_tool_use", "post_tool_use")
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
        for relative in COMMON_ASSETS:
            planned[root / relative] = asset(relative, source, ref)
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


    # Install the shared pre-decision primitive for every selected provider.
    for relative in COMMON_ASSETS:
        data = asset(relative, source, ref)
        ast.parse(data, filename=relative, feature_version=(3, 11))
        target = root / relative
        check_target(root, target)
        planned[target] = data

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
        "--action", choices=["install", "uninstall"], default="install",
        help="install or uninstall the selected provider",
    )
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
        changed = (
            install(args.project, providers, args.source, args.ref)
            if args.action == "install"
            else uninstall(args.project, providers)
        )
    except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
        print(f"Hook installation failed: {exc}", file=sys.stderr)
        return 1
    if args.action == "uninstall":
        print(f"Removed {len(changed)} DeepSeek Harness files/integration entries.")
        return 0
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
    print("Routing uses the harness-router CLI on PATH and OPENROUTER_API_KEY.")
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
