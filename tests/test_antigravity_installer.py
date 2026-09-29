import json
import shlex
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
from scripts import install_hook as installer
def test_antigravity_install_uses_cli_only(tmp_path):
    changed=installer.install(tmp_path,["antigravity"],ROOT,"main"); assert changed
    config=json.loads((tmp_path/".agents/hooks.json").read_text())
    command=config["harness-router"]["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(command)==["python3",str(tmp_path/".antigravity/hooks/pre_tool_use.py")]
    assert not (tmp_path/".agents/mcp_config.json").exists()
def test_antigravity_install_is_idempotent(tmp_path):
    installer.install(tmp_path,["antigravity"],ROOT,"main"); assert installer.install(tmp_path,["antigravity"],ROOT,"main")==[]
@pytest.mark.parametrize("blocked",[".agents",".agents/hooks.json",".antigravity",".antigravity/hooks"])
def test_install_rejects_symlinks(tmp_path,blocked):
    project,outside=tmp_path/"project",tmp_path/"outside"; project.mkdir(); outside.mkdir()
    target=project/blocked; target.parent.mkdir(parents=True,exist_ok=True); target.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match="symlink"): installer.install(project,["antigravity"],ROOT,"main")
    assert not list(outside.iterdir())
