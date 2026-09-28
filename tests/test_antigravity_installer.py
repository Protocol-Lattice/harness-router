import importlib.util
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "antigravity_installer", ROOT / "scripts/install_hook.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_named_hooks_merge_preserves_customizations_and_is_idempotent(tmp_path):
    config = tmp_path / ".agents/hooks.json"
    config.parent.mkdir()
    other = {"PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "lint"}]}]}
    custom = {"type": "command", "command": "echo custom"}
    existing = {
        "my-linter": other,
        "harness-router": {
            "enabled": False,
            "PreToolUse": [
                {
                    "matcher": "view_file",
                    "hooks": [
                        custom,
                        {"command": "python3 .antigravity/hooks/pre_tool_use.py"},
                    ],
                }
            ],
            "Stop": [custom, {"command": "python3 .antigravity/hooks/pre_tool_use.py --stop"}],
            "PreInvocation": [custom],
        },
    }
    original = json.dumps(existing)
    config.write_text(original)
    catalog = tmp_path / ".antigravity/harness-router-tools.json"
    catalog.parent.mkdir()
    catalog.write_text("[]")
    assert installer.install(tmp_path, ["antigravity"], ROOT, "main")
    installed = json.loads(config.read_text())
    assert installed["my-linter"] == other
    group = installed["harness-router"]
    assert group["enabled"] is False
    assert group["PreInvocation"] == [custom]
    assert group["PreToolUse"][0] == {"matcher": "view_file", "hooks": [custom]}
    assert len(group["PreToolUse"]) == 2
    assert group["Stop"][0] == custom
    assert len(group["Stop"]) == 2
    assert config.with_name("hooks.json.harness-router.bak").read_text() == original
    assert catalog.read_text() == "[]"
    assert ".antigravity/harness-router/" in (tmp_path / ".gitignore").read_text()
    server = json.loads((tmp_path / ".agents/mcp_config.json").read_text())["mcpServers"][
        "harness-router"
    ]
    assert Path(server["command"]).name == "harness-router-mcp"
    assert server["args"] == []
    assert "disabledTools" not in server
    assert installer.install(tmp_path, ["antigravity"], ROOT, "main") == []
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".claude").exists()


def test_installed_commands_work_from_other_cwd_in_non_git_project(tmp_path, monkeypatch):
    project = tmp_path / "project with 'quotes' and spaces"
    project.mkdir()
    installer.install(project, ["antigravity"], ROOT, "main")
    config = json.loads((project / ".agents/hooks.json").read_text())["harness-router"]
    command = config["PreToolUse"][0]["hooks"][0]["command"]
    binary = tmp_path / "fake mcp"
    shutil.copyfile(ROOT / "tests/fixtures/ohmypi_mcp.py", binary)
    binary.chmod(0o755)
    monkeypatch.setenv("HARNESS_ROUTER_MCP_BIN", str(binary))
    monkeypatch.setenv("HARNESS_ROUTER_TEST_RESULT", '{"tool":"grep_search","confidence":0.95}')
    for name in ("ANTIGRAVITY_TOOLS_FILE", "ANTIGRAVITY_TOOLS_JSON", "PRETOOL_MCTS_GRAPH_CMD"):
        monkeypatch.delenv(f"HARNESS_ROUTER_{name}", raising=False)
    monkeypatch.setenv(
        "HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON",
        '[{"name":"view_file"},{"name":"grep_search"}]',
    )
    payload = {
        "toolCall": {"name": "view_file", "args": {"AbsolutePath": "src/parser.py"}},
        "conversationId": "session",
        "workspacePaths": [str(project)],
        "stepIdx": 2,
    }
    run = subprocess.run(
        command,
        shell=True,
        cwd=tmp_path,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert json.loads(run.stdout)["decision"] == "deny"
    assert shlex.split(command) == ["python3", str(project / ".antigravity/hooks/pre_tool_use.py")]
    run = subprocess.run(
        config["Stop"][0]["command"],
        shell=True,
        cwd=tmp_path,
        input=json.dumps({"conversationId": "session", "fullyIdle": True}),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert json.loads(run.stdout) == {}
    assert not list((project / ".antigravity/harness-router/sessions").iterdir())


@pytest.mark.parametrize(
    "blocked",
    [
        ".agents",
        ".agents/hooks.json",
        ".agents/mcp_config.json",
        ".antigravity",
        ".antigravity/hooks",
    ],
)
def test_install_rejects_symlinks_before_any_write(tmp_path, blocked):
    project, outside = tmp_path / "project", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    target = project / blocked
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        installer.install(project, ["antigravity"], ROOT, "main")
    assert not list(outside.iterdir())
    assert not (project / ".gitignore").exists()


@pytest.mark.parametrize(
    "config", ["invalid", "[]", '{"harness-router":false}', '{"harness-router":{"Stop":{}}}']
)
def test_invalid_existing_config_is_preserved_without_partial_install(tmp_path, config):
    path = tmp_path / ".agents/hooks.json"
    path.parent.mkdir()
    path.write_text(config)
    with pytest.raises(ValueError):
        installer.install(tmp_path, ["antigravity"], ROOT, "main")
    assert path.read_text() == config
    assert not (tmp_path / ".antigravity").exists()
    assert not (tmp_path / ".gitignore").exists()


@pytest.mark.parametrize(
    "broken",
    [
        ".antigravity/hooks.json",
        ".antigravity/mcp_config.json",
        *installer.ANTIGRAVITY_ASSETS,
    ],
)
def test_bad_download_leaves_project_unchanged(tmp_path, monkeypatch, broken):
    monkeypatch.setattr(
        installer,
        "asset",
        lambda path, source, ref: (
            b"invalid content" if path == broken else (ROOT / path).read_bytes()
        ),
    )
    with pytest.raises((ValueError, SyntaxError)):
        installer.install(tmp_path, ["antigravity"], None, "main")
    assert not list(tmp_path.iterdir())


def test_mcp_registration_preserves_existing_servers_and_router_settings(tmp_path):
    path = tmp_path / ".agents/mcp_config.json"
    path.parent.mkdir()
    existing = {
        "mcpServers": {
            "other": {"command": "custom-mcp", "env": {"TOKEN": "keep"}},
            "harness-router": {"command": "/custom/harness-router-mcp", "env": {"MODEL": "custom"}},
        }
    }
    original = json.dumps(existing)
    path.write_text(original)
    installer.install(tmp_path, ["antigravity"], ROOT, "main")
    assert path.read_text() == original


@pytest.mark.parametrize(
    "existing", ["[]", '{"mcpServers":[]}', '{"mcpServers":{"harness-router":false}}']
)
def test_invalid_mcp_config_leaves_hooks_and_files_unchanged(tmp_path, existing):
    path = tmp_path / ".agents/mcp_config.json"
    path.parent.mkdir()
    path.write_text(existing)
    with pytest.raises(ValueError):
        installer.install(tmp_path, ["antigravity"], ROOT, "main")
    assert path.read_text() == existing
    assert not (tmp_path / ".antigravity").exists()
    assert not (tmp_path / ".agents/hooks.json").exists()
