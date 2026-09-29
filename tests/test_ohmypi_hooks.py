import importlib.util
import json
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("ohmypi_bridge",ROOT/".omp/hooks/pre_tool_use.py"); bridge=importlib.util.module_from_spec(spec); assert spec.loader; spec.loader.exec_module(bridge)
@pytest.fixture
def payload(tmp_path):
    return {"hook_event_name":"tool_call","cwd":str(tmp_path),"tool_name":"read","tool_input":{"path":"src/parser.py"},"goal":"Find parser references","tools":[{"name":"read","description":"Read a source file","parameters":{"type":"object"}},{"name":"grep","description":"Search source code"},{"name":"write","description":"Write a source file"},{"name":"mcp__harness_router__route","description":"Route"}]}
def test_bridge_routes_through_cli(payload,monkeypatch):
    monkeypatch.setattr(bridge.shutil,"which",lambda _:"harness-router"); monkeypatch.setattr(bridge,"route",lambda *args:({"tool":"grep","confidence":.95},"route"))
    result=bridge.handle(payload); assert result["block"] is True; assert result["tool"]=="grep"
@pytest.mark.parametrize("decision",[{"tool":"read","confidence":.99},{"tool":"grep","confidence":.79},{"tool":"grep","confidence":.99,"fallback":True},{"tool":"disabled","confidence":.99},{"tool":"grep","confidence":True},{}])
def test_bridge_invalid_decisions_fail_open(payload,monkeypatch,decision):
    monkeypatch.setattr(bridge.shutil,"which",lambda _:"harness-router"); monkeypatch.setattr(bridge,"route",lambda *args:(decision,"route")); assert bridge.handle(payload)=={}
def test_bridge_skips_router_tools(payload,monkeypatch):
    payload["tool_name"]="mcp__harness_router__route"; monkeypatch.setattr(bridge,"route",lambda *args:pytest.fail("should not route")); assert bridge.handle(payload)=={}
