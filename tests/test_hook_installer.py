from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import install_hook

REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "scripts/install_hook.py"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@pytest.mark.parametrize("provider", ["codex", "claude", "both"])
def test_cli_installs_selected_providers(project, provider):
    result = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "--provider",
            provider,
            "--project",
            str(project),
            "--source",
            str(REPOSITORY),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Installed" in result.stdout
    assert not result.stderr
    chosen = list(install_hook.PROVIDERS) if provider == "both" else [provider]
    for name, (_, config) in install_hook.PROVIDERS.items():
        assert (project / config).exists() == (name in chosen)
        if name in chosen:
            settings = json.loads((project / config).read_text())
            assert set(settings["hooks"]) == {"SessionStart", "PreToolUse"}
            for script in ("discover_tools", "pre_tool_use"):
                relative = f".{name}/hooks/{script}.py"
                assert (project / relative).read_bytes() == (REPOSITORY / relative).read_bytes()
            assert f".{name}/harness-router-tools.json" in (project / ".gitignore").read_text()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_existing_settings_and_other_hooks_survive_reinstall(project, provider):
    config = project / install_hook.PROVIDERS[provider][1]
    config.parent.mkdir()
    unrelated = {"type": "command", "command": "python3 keep_my_hook.py"}
    original = {
        "permissions": {"deny": ["Bash(rm *)"]},
        "description": "Keep my project configuration",
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        unrelated,
                        {
                            "type": "command",
                            "command": f"python3 .{provider}/hooks/pre_tool_use.py",
                            "timeout": 1,
                        },
                    ],
                }
            ],
            "Stop": [{"hooks": [unrelated]}],
        },
    }
    old_bytes = json.dumps(original).encode()
    config.write_bytes(old_bytes)
    config.chmod(0o600)
    install_hook.install(project, [provider], REPOSITORY, "main")
    merged = json.loads(config.read_text())
    assert merged["permissions"] == original["permissions"]
    assert merged["description"] == original["description"]
    assert merged["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert merged["hooks"]["PreToolUse"][0] == {"matcher": "Bash", "hooks": [unrelated]}
    assert len(merged["hooks"]["PreToolUse"]) == 2
    assert config.stat().st_mode & 0o777 == 0o600
    backup = config.with_name(config.name + install_hook.BACKUP_SUFFIX)
    assert backup.read_bytes() == old_bytes
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in project.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert install_hook.install(project, [provider], REPOSITORY, "main") == []
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before}
    assert after == before


def test_provider_switch_keeps_both_integrations(project):
    install_hook.install(project, ["codex"], REPOSITORY, "main")
    config = project / ".codex/hooks.json"
    previous = config.read_bytes()
    install_hook.install(project, ["claude"], REPOSITORY, "main")
    assert config.read_bytes() == previous
    assert (project / ".claude/settings.json").exists()
    ignored = (project / ".gitignore").read_text().splitlines()
    assert ignored.count("*.harness-router.bak") == 1
    assert ".codex/harness-router-tools.json" in ignored
    assert ".claude/harness-router-tools.json" in ignored


@pytest.mark.parametrize(
    "contents", [b"not json", b"[]", b'{"hooks": []}', b'{"hooks": {"PreToolUse": {}}}']
)
def test_invalid_existing_config_leaves_everything_unchanged(project, contents):
    config = project / ".claude/settings.json"
    config.parent.mkdir()
    config.write_bytes(contents)
    with pytest.raises(ValueError):
        install_hook.install(project, ["codex", "claude"], REPOSITORY, "main")
    assert config.read_bytes() == contents
    assert not (project / ".codex").exists()
    assert not (config.parent / "hooks").exists()
    assert not (project / ".gitignore").exists()


def test_failed_download_does_not_leave_partial_installation(project, monkeypatch):
    real_asset = install_hook.asset

    def download(path, source, ref):
        if path.endswith("pre_tool_use.py"):
            raise OSError("network unavailable")
        return real_asset(path, REPOSITORY, ref)

    monkeypatch.setattr(install_hook, "asset", download)
    with pytest.raises(OSError, match="network unavailable"):
        install_hook.install(project, ["codex"], None, "main")
    assert not (project / ".codex").exists()
    assert not (project / ".gitignore").exists()


def test_remote_assets_use_selected_ref_and_install_both_providers(project, monkeypatch):
    urls = []

    def download(request, timeout):
        urls.append(request.full_url)
        assert timeout > 0
        relative = request.full_url.split("/release%2Ftest/", 1)[1]
        return io.BytesIO((REPOSITORY / relative).read_bytes())

    monkeypatch.setattr(install_hook, "urlopen", download)
    install_hook.install(project, ["codex", "claude"], None, "release/test")
    assert len(urls) == 6
    assert (project / ".codex/hooks.json").exists()
    assert (project / ".claude/settings.json").exists()


def test_installer_can_run_from_stdin_like_documented_command(project):
    result = subprocess.run(
        [sys.executable, "-", "--provider", "claude", "--source", str(REPOSITORY)],
        cwd=project,
        input=INSTALLER.read_text(),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Installed Claude Code" in result.stdout
    assert (project / ".claude/settings.json").exists()


def test_local_script_changes_are_backed_up(project):
    script = project / ".claude/hooks/pre_tool_use.py"
    script.parent.mkdir(parents=True)
    script.write_text("# My custom routing hook\n")
    install_hook.install(project, ["claude"], REPOSITORY, "main")
    assert script.with_name(script.name + install_hook.BACKUP_SUFFIX).read_text() == (
        "# My custom routing hook\n"
    )


def test_existing_gitignore_is_preserved(project):
    ignore = project / ".gitignore"
    original = b"# Project ignores\r\n.env\r\nnode_modules/"
    ignore.write_bytes(original)
    install_hook.install(project, ["claude"], REPOSITORY, "main")
    assert ignore.read_bytes().startswith(original + b"\r\n")
    assert ignore.with_name(ignore.name + install_hook.BACKUP_SUFFIX).read_bytes() == original
    assert b"\n" not in ignore.read_bytes().replace(b"\r\n", b"")


def test_refuses_symlinked_global_settings_before_writing(project, tmp_path):
    global_settings = tmp_path / "global-claude"
    global_settings.mkdir()
    (global_settings / "settings.json").write_text('{"theme": "dark"}')
    (project / ".claude").symlink_to(global_settings, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        install_hook.install(project, ["codex", "claude"], REPOSITORY, "main")
    assert not (project / ".codex").exists()
    assert list(global_settings.iterdir()) == [global_settings / "settings.json"]


def test_claude_can_install_without_git_but_codex_requires_repository_root(tmp_path):
    install_hook.install(tmp_path, ["claude"], REPOSITORY, "main")
    assert (tmp_path / ".claude/settings.json").exists()
    with pytest.raises(ValueError, match="Git repository root"):
        install_hook.install(tmp_path, ["codex"], REPOSITORY, "main")


def test_codex_cannot_silently_install_hooks_into_a_git_subdirectory(project):
    nested = project / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="Git repository root"):
        install_hook.install(nested, ["codex"], REPOSITORY, "main")
    assert not (nested / ".codex").exists()
