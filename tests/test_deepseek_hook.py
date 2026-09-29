import importlib.util
import io
import json
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("deepseek_hook",ROOT/"hooks/deepseek/hook.py"); hook=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(hook)
def test_deepseek_allows_without_binary(monkeypatch,tmp_path,capsys):
    monkeypatch.setenv("HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON",json.dumps([{"name":"read_file"},{"name":"search_code"}])); monkeypatch.setattr(hook.shutil,"which",lambda _:None)
    monkeypatch.setattr(sys,"stdin",io.StringIO(json.dumps({"hook_event_name":"PreToolUse","tool_name":"read_file","cwd":str(tmp_path)})))
    assert hook.main()==0; assert "permissionDecision" not in json.loads(capsys.readouterr().out)["hookSpecificOutput"]
def test_deepseek_denies_via_cli(monkeypatch,tmp_path,capsys):
    monkeypatch.setenv("HARNESS_ROUTER_DEEPSEEK_TOOLS_JSON",json.dumps([{"name":"read_file","description":"Read"},{"name":"search_code","description":"Search"}])); monkeypatch.setattr(hook.shutil,"which",lambda _:"harness-router")
    seen={}
    def fake_route(**kwargs): seen.update(kwargs); return {"tool":"search_code","confidence":.94},"route"
    monkeypatch.setattr(hook,"route",fake_route)
    monkeypatch.setattr(sys,"stdin",io.StringIO(json.dumps({"hook_event_name":"PreToolUse","tool_name":"read_file","tool_input":{"path":"src/parser.py"},"cwd":str(tmp_path)})))
    assert hook.main()==0
    out=json.loads(capsys.readouterr().out)["hookSpecificOutput"]; assert out["permissionDecision"]=="deny"; assert "search_code" in out["permissionDecisionReason"]; assert seen["goal"]
def test_deepseek_hook_uses_current_cli_route():
    assert "binary, cwd, goal, observation, current, candidates" in (ROOT/"hooks/deepseek/hook.py").read_text()
