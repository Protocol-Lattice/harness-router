import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "hook_installer", ROOT / "scripts/install_hook.py"
)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


def test_deepseek_install_is_idempotent_and_preserves_custom_hooks(tmp_path):
    config = tmp_path / ".dsh/harness-router-hooks.json"
    config.parent.mkdir()
    config.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": "echo custom"},
                        {"type": "command", "command": "python3 .dsh/hooks/hook.py"},
                    ]
                }
            ]
        },
        "custom": True,
    }))
    installer.install(tmp_path, ["deepseek"], ROOT, "main")
    installed = json.loads(config.read_text())
    assert installed["custom"] is True
    handlers = installed["hooks"]["PreToolUse"][0]["hooks"]
    assert {"type": "command", "command": "echo custom"} in handlers
    assert len(installed["hooks"]["PreToolUse"]) == 2

    first = config.read_text()
    assert installer.install(tmp_path, ["deepseek"], ROOT, "main") == []
    assert config.read_text() == first
    assert (tmp_path / ".dsh/hooks/hook.py").exists()
    assert (tmp_path / ".dsh/harness-router.patch.yml").exists()


def test_deepseek_install_creates_real_bridge_config(tmp_path):
    installer.install(tmp_path, ["deepseek"], ROOT, "main")
    config = json.loads((tmp_path / ".dsh/harness-router-hooks.json").read_text())
    command = config["hooks"]["PreToolUse"][-1]["hooks"][0]["command"]
    assert command == "python3 .dsh/hooks/hook.py"
    patch = (tmp_path / ".dsh/harness-router.patch.yml").read_text()
    assert "@deepseek-ai/dsh-hooks-codex" in patch
    assert "tools/pre-execute" in patch


def test_deepseek_uninstall_removes_only_router_integration(tmp_path):
    installer.install(tmp_path, ["deepseek"], ROOT, "main")
    config = tmp_path / ".dsh/harness-router-hooks.json"
    config.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": "echo keep"},
                        {"type": "command", "command": "python3 .dsh/hooks/hook.py"},
                    ]
                }
            ]
        },
        "other": {"keep": True},
    }))
    removed = installer.uninstall(tmp_path, ["deepseek"])
    assert config.exists()
    remaining = json.loads(config.read_text())
    assert remaining["other"]["keep"] is True
    assert remaining["hooks"]["PreToolUse"][0]["hooks"] == [
        {"type": "command", "command": "echo keep"}
    ]
    assert (tmp_path / ".dsh/hooks/hook.py").exists() is False
    assert (tmp_path / ".dsh/harness-router.patch.yml").exists() is False
    assert config in removed or not config.exists()


def test_deepseek_hook_allow_on_missing_router(monkeypatch, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("deepseek_hook", ROOT / "hooks/deepseek/hook.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.setenv("HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON", json.dumps([
        {"name": "read_file"},
        {"name": "search_code"},
    ]))
    monkeypatch.setattr(module.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "read_file",
            "cwd": str(tmp_path),
        })),
    )
    assert module.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert "permissionDecision" not in output["hookSpecificOutput"]


def test_deepseek_hook_denies_confident_alternative(monkeypatch, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("deepseek_hook", ROOT / "hooks/deepseek/hook.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.setenv("HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON", json.dumps([
        {"name": "read_file"},
        {"name": "search_code"},
    ]))
    monkeypatch.setattr(module, "route", lambda **_: {"tool": "search_code", "confidence": 0.94})
    monkeypatch.setattr(module.shutil, "which", lambda _: "harness-router-mcp")
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "read_file",
            "cwd": str(tmp_path),
        })),
    )
    assert module.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "search_code" in output["hookSpecificOutput"]["permissionDecisionReason"]
