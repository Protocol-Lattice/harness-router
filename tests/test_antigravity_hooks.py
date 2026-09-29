import importlib.util
import io
import json
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path); mod=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(mod); return mod
hook=load("antigravity_hook",".antigravity/hooks/pre_tool_use.py")
discovery=load("antigravity_discovery",".antigravity/hooks/discover_tools.py")
@pytest.fixture
def payload(tmp_path,monkeypatch):
    monkeypatch.setattr(hook,"PROJECT_ROOT",tmp_path)
    monkeypatch.setenv("HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON",json.dumps([
        {"name":"view_file","description":"Read a file"},{"name":"grep_search","description":"Find text in files"},{"name":"write_to_file","description":"Write a file"}]))
    return {"toolCall":{"name":"view_file","args":{"AbsolutePath":"/src/parser.py"}},"conversationId":"conversation","workspacePaths":[str(tmp_path)],"stepIdx":4}
def test_cli_route_denies_confident_alternative(payload,monkeypatch):
    monkeypatch.setattr(hook,"route",lambda *args: ({"tool":"grep_search","confidence":.95},"route"))
    assert hook.handle(payload)["decision"]=="deny"; assert hook.handle(payload)=={}
def test_redirect_marker_rearms_after_stop(payload,monkeypatch):
    monkeypatch.setattr(hook,"route",lambda *args: ({"tool":"grep_search","confidence":.95},"route"))
    assert hook.handle(payload)["decision"]=="deny"; assert hook.handle(payload)=={}
    assert hook.stop({**payload,"fullyIdle":False})=={}; assert hook.handle(payload)=={}
    assert hook.stop({**payload,"fullyIdle":True})=={}; assert hook.handle(payload)["decision"]=="deny"
@pytest.mark.parametrize("decision",[{"tool":"view_file","confidence":.99},{"tool":"grep_search","confidence":.79},{"tool":"grep_search","confidence":.99,"fallback":True},{"tool":"unknown","confidence":.99},{"tool":"grep_search","confidence":True},{"tool":"grep_search","confidence":"0.99"},{}])
def test_invalid_decisions_fail_open(payload,monkeypatch,decision):
    monkeypatch.setattr(hook,"route",lambda *args:(decision,"route")); assert hook.handle(payload)=={}
def test_router_calls_are_ignored(payload,monkeypatch):
    payload["toolCall"]["name"]="mcp__harness_router__route"; monkeypatch.setattr(hook,"route",lambda *args:pytest.fail("recursion")); assert hook.handle(payload)=={}
def test_missing_binary_fails_open(payload,monkeypatch):
    monkeypatch.delenv("HARNESS_ROUTER_BIN",raising=False); monkeypatch.setattr(hook.shutil,"which",lambda _:None); assert hook.handle(payload)=={}
@pytest.mark.parametrize("raw",["not json","[]","null","{}",'{"toolCall":42}'])
def test_bad_input_fails_open(raw,monkeypatch,capsys):
    monkeypatch.setattr(sys,"stdin",io.StringIO(raw)); assert hook.main([])==0; assert json.loads(capsys.readouterr().out)=={}
def test_discovery_rejects_non_local_endpoints():
    for url in ("https://example.com:443","http://127.0.0.1:80@evil.com:80","http://127.0.0.1:80/path","file:///tmp/data","http://localhost"):
        with pytest.raises(ValueError): discovery.local_url(url)
def test_discovery_filters_disabled_mcp_tools():
    result=discovery.active_mcp_tools([{"name":"mcp_find","originalName":"find","serverName":"index"},{"name":"mcp_delete","originalName":"delete","serverName":"index"},{"name":"native"}],{"states":[{"status":"MCP_SERVER_STATUS_READY","spec":{"serverName":"index","disabledTools":["delete"]}}]})
    assert [x["name"] for x in result]==["mcp_find","native"]
