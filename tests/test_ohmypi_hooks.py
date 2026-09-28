import importlib.util
import io
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / ".omp/hooks/pre_tool_use.py"
spec = importlib.util.spec_from_file_location("ohmypi_bridge", BRIDGE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


@pytest.fixture
def payload(tmp_path, monkeypatch):
    binary = tmp_path / "fake mcp"
    shutil.copyfile(ROOT / "tests/fixtures/ohmypi_mcp.py", binary)
    binary.chmod(0o755)
    for name in ("MCTS_GRAPH_CMD", "MIN_CONFIDENCE", "MAX_CANDIDATES", "MCTS_THRESHOLD"):
        monkeypatch.delenv(f"HARNESS_ROUTER_PRETOOL_{name}", raising=False)
    monkeypatch.setenv("HARNESS_ROUTER_MCP_BIN", str(binary))
    monkeypatch.setenv("HARNESS_ROUTER_PRETOOL_TIMEOUT", "2")
    return {
        "hook_event_name": "tool_call",
        "cwd": str(tmp_path),
        "tool_name": "read",
        "tool_input": {"path": "src/parser.py"},
        "goal": "Find parser references",
        "tools": [
            {"name": "read", "description": "Read a source file", "parameters": {"type": "object"}},
            {"name": "grep", "description": "Search source code"},
            {"name": "write", "description": "Write a source file"},
            {"name": "mcp__harness_router__route", "description": "Route"},
        ],
    }


@pytest.mark.parametrize("mode", ["structured", "text"])
def test_bridge_routes_actual_catalog_through_mcp(payload, tmp_path, monkeypatch, mode):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("HARNESS_ROUTER_TEST_LOG", str(log))
    monkeypatch.setenv("HARNESS_ROUTER_TEST_MODE", mode)
    result = bridge.handle(payload)
    assert result["block"] is True
    assert result["tool"] == "grep"
    request = json.loads(log.read_text().splitlines()[0])["params"]
    assert request["name"] == "route"
    assert request["arguments"]["goal"] == payload["goal"]
    assert {tool["name"] for tool in request["arguments"]["tools"]} == {"read", "grep", "write"}
    assert request["arguments"]["tools"][0]["description"] == "Read a source file"
    assert "src/parser.py" in request["arguments"]["observation"]
    assert bridge.normalize_tools(payload["tools"])[0]["input_schema"] == {"type": "object"}


@pytest.mark.parametrize(
    "decision",
    [
        {"tool": "read", "confidence": 0.99},
        {"tool": "grep", "confidence": 0.79},
        {"tool": "grep", "confidence": 0.99, "fallback": True},
        {"tool": "disabled_tool", "confidence": 0.99},
        {"tool": "mcp__harness_router__route", "confidence": 0.99},
        {"tool": "grep", "confidence": True},
        {"tool": "grep", "confidence": "0.99"},
        {"tool": "grep", "confidence": float("nan")},
        {"tool": "grep", "confidence": float("inf")},
        {"tool": "grep", "confidence": 1.01},
        {"tool": ["grep"], "confidence": 0.99},
        {},
    ],
)
def test_only_confident_known_alternatives_can_block(payload, monkeypatch, decision):
    monkeypatch.setattr(bridge, "route", lambda *args: (decision, "route"))
    assert bridge.handle(payload) == {}


@pytest.mark.parametrize("mode", ["error", "malformed", "exit", "partial"])
def test_mcp_failure_and_partial_lines_fail_open_with_deadline(payload, monkeypatch, mode):
    monkeypatch.setenv("HARNESS_ROUTER_TEST_MODE", mode)
    monkeypatch.setenv("HARNESS_ROUTER_PRETOOL_TIMEOUT", "0.2")
    started = time.monotonic()
    run = subprocess.run(
        [sys.executable, str(BRIDGE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert run.returncode == 0
    assert json.loads(run.stdout) == {}
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("raw", ["not json", "[]", "null", '{"tools":42}', "{}"])
def test_bad_input_fails_open(raw, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert bridge.main() == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_unknown_and_router_calls_skip_mcp(payload, monkeypatch):
    monkeypatch.setattr(bridge, "route", lambda *args: pytest.fail("should not route"))
    for current in ("unknown", "mcp__harness_router__route", "harness-router.route"):
        payload["tool_name"] = current
        assert bridge.handle(payload) == {}


def test_missing_binary_fails_open(payload, monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_ROUTER_MCP_BIN", "/does/not/exist")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert bridge.main() == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_mcts_receives_real_schemas_and_uses_same_mcp_session(payload, tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    graph_input = tmp_path / "graph-input.json"
    provider = tmp_path / "graph provider.py"
    provider.write_text(
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(graph_input)!r}).write_text(sys.stdin.read())\n"
        'print(json.dumps({"root_state":"start","states":[],"transitions":[]}))\n'
    )
    monkeypatch.setenv("HARNESS_ROUTER_TEST_LOG", str(log))
    monkeypatch.setenv("HARNESS_ROUTER_TEST_RESULT", '{"fallback":true}')
    monkeypatch.setenv(
        "HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD", shlex.join([sys.executable, str(provider)])
    )
    result = bridge.handle(payload)
    assert result["block"] is True
    assert "route_mcts" in result["reason"]
    assert [json.loads(line)["params"]["name"] for line in log.read_text().splitlines()] == [
        "route",
        "route_mcts",
    ]
    assert json.loads(graph_input.read_text())["candidates"][0]["input_schema"] == {
        "type": "object"
    }


def test_no_graph_provider_means_no_mcts(payload, tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("HARNESS_ROUTER_TEST_LOG", str(log))
    monkeypatch.setenv("HARNESS_ROUTER_TEST_RESULT", '{"fallback":true}')
    assert bridge.handle(payload) == {}
    assert len(log.read_text().splitlines()) == 1
