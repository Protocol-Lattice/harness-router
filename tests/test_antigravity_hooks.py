import importlib.util
import io
import json
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / ".antigravity/hooks" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = load_module("antigravity_hook", "pre_tool_use.py")
discovery = load_module("antigravity_discovery", "discover_tools.py")


@pytest.fixture
def runtime(monkeypatch):
    state = {
        "metadata": [
            {
                "stepIndices": [3],
                "chatModel": {
                    "tools": [
                        {
                            "name": "view_file",
                            "description": "Read a file",
                            "jsonSchemaString": '{"type":"object","required":["AbsolutePath"]}',
                        },
                        {"name": "grep_search", "description": "Find text in files"},
                        {"name": "write_to_file", "description": "Write a file"},
                    ]
                },
            }
        ],
        "servers": [],
        "calls": [],
        "fail": False,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append((self.path, payload, self.headers.get("X-Codeium-Csrf-Token")))
            if state["fail"]:
                self.send_error(503)
                return
            if self.path.endswith("GetCascadeTrajectoryGeneratorMetadata"):
                offset = payload["generatorMetadataOffset"]
                response = {"generatorMetadata": state["metadata"][offset:]}
            elif self.path.endswith("GetMcpServerStates"):
                response = {"states": state["servers"]}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HARNESS_ROUTER_ANTIGRAVITY_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("HARNESS_ROUTER_ANTIGRAVITY_CSRF_TOKEN", "local-test-token")
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def payload(tmp_path, monkeypatch):
    for setting in (
        "PRETOOL_MCTS_GRAPH_CMD",
        "PRETOOL_MIN_CONFIDENCE",
        "PRETOOL_MAX_CANDIDATES",
        "PRETOOL_MCTS_THRESHOLD",
        "ANTIGRAVITY_TOOLS_FILE",
        "ANTIGRAVITY_TOOLS_JSON",
        "TEST_MODE",
        "TEST_LOG",
        "TEST_DELAY",
    ):
        monkeypatch.delenv(f"HARNESS_ROUTER_{setting}", raising=False)
    shutil.copytree(ROOT / ".antigravity", tmp_path / ".antigravity")
    binary = tmp_path / "fake mcp"
    shutil.copyfile(ROOT / "tests/fixtures/ohmypi_mcp.py", binary)
    binary.chmod(0o755)
    monkeypatch.setenv("HARNESS_ROUTER_MCP_BIN", str(binary))
    monkeypatch.setenv("HARNESS_ROUTER_PRETOOL_TIMEOUT", "2")
    monkeypatch.setenv("HARNESS_ROUTER_DISCOVERY_TIMEOUT", "2")
    monkeypatch.setenv("HARNESS_ROUTER_TEST_RESULT", '{"tool":"grep_search","confidence":0.95}')
    monkeypatch.setattr(hook, "PROJECT_ROOT", tmp_path)
    return {
        "toolCall": {"name": "view_file", "args": {"AbsolutePath": "/src/parser.py"}},
        "stepIdx": 4,
        "conversationId": "conversation",
        "workspacePaths": [str(tmp_path)],
    }


@pytest.mark.parametrize("mode", ["structured", "text"])
def test_live_inventory_to_mcp_denial_and_one_redirect(
    payload, runtime, tmp_path, monkeypatch, mode
):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("HARNESS_ROUTER_TEST_LOG", str(log))
    monkeypatch.setenv("HARNESS_ROUTER_TEST_MODE", mode)
    result = hook.handle(payload)
    assert result["decision"] == "deny"
    assert "grep_search" in result["reason"]
    assert set(result) == {"decision", "reason"}
    request = json.loads(log.read_text().splitlines()[0])["params"]
    assert request["name"] == "route"
    assert {tool["name"] for tool in request["arguments"]["tools"]} == {
        "view_file",
        "grep_search",
        "write_to_file",
    }
    assert "/src/parser.py" in request["arguments"]["observation"]
    snapshot = json.loads((tmp_path / ".antigravity/harness-router-tools.json").read_text())
    assert snapshot["tools"][0]["input_schema"]["required"] == ["AbsolutePath"]
    assert "local-test-token" not in json.dumps(snapshot)
    assert runtime["calls"][0][1]["cascadeId"] == payload["conversationId"]
    assert runtime["calls"][0][2] == "local-test-token"
    assert hook.handle(payload) == {}
    assert len(log.read_text().splitlines()) == 1


def test_refresh_drops_removed_tools_and_failure_does_not_reuse_cache(payload, runtime, tmp_path):
    assert len(hook.load_tools(payload)) == 3
    runtime["metadata"].append({"stepIndices": [4], "chatModel": {"tools": [{"name": "new_tool"}]}})
    assert [tool["name"] for tool in hook.load_tools(payload)] == ["new_tool"]
    runtime["fail"] = True
    assert hook.load_tools(payload) == []
    assert hook.handle(payload) == {}
    assert not hook.redirect_marker(payload).exists()


def test_model_inventory_filters_disabled_mcp_and_preserves_real_names(payload, runtime, tmp_path):
    runtime["metadata"][0]["chatModel"]["tools"] += [
        {
            "name": "mcp7_find",
            "originalName": "find",
            "serverName": "index",
            "jsonSchemaString": '{"type":"object"}',
            "responseJsonSchemaString": '{"type":"array"}',
        },
        {"name": "mcp7_delete", "originalName": "delete", "serverName": "index"},
        {"name": "mcp8_hidden", "serverName": "disabled"},
    ]
    runtime["servers"] = [
        {
            "status": "MCP_SERVER_STATUS_READY",
            "spec": {
                "serverName": "index",
                "disabledTools": ["delete"],
                "env": {"SECRET": "hidden"},
            },
            "tools": [{"name": "not_exposed_to_this_conversation"}],
        },
        {"status": 2, "spec": {"serverName": "disabled", "disabled": True}},
    ]
    snapshot = discovery.discover(payload, tmp_path)
    names = {tool["name"] for tool in snapshot["tools"]}
    assert "mcp7_find" in names
    assert not names & {"mcp7_delete", "mcp8_hidden", "not_exposed_to_this_conversation"}
    assert "SECRET" not in json.dumps(snapshot)
    assert snapshot["tools"][-1]["output_schema"] == {"type": "array"}


def test_latest_empty_inventory_does_not_reuse_previous_tools(payload, runtime):
    runtime["metadata"].append({"stepIndices": [4], "chatModel": {}})
    assert hook.load_tools(payload) == []


def test_stop_only_rearms_the_idle_conversation(payload, runtime):
    assert hook.handle(payload)["decision"] == "deny"
    for stopped in ({}, {"fullyIdle": False}, {"fullyIdle": "true"}):
        assert hook.stop({**payload, **stopped}) == {}
        assert hook.handle(payload) == {}
    assert hook.stop({**payload, "conversationId": "other", "fullyIdle": True}) == {}
    assert hook.handle(payload) == {}
    assert hook.stop({**payload, "fullyIdle": True}) == {}
    assert hook.handle(payload)["decision"] == "deny"


def test_parallel_hooks_cannot_both_redirect(payload, monkeypatch):
    monkeypatch.setenv(
        "HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON", '[{"name":"view_file"},{"name":"grep_search"}]'
    )
    barrier = threading.Barrier(2)

    def route(*args):
        barrier.wait(timeout=5)
        return {"tool": "grep_search", "confidence": 0.95}, "route"

    monkeypatch.setattr(hook, "route", route)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(hook.handle, [payload, payload]))
    assert sum(result.get("decision") == "deny" for result in results) == 1
    assert {} in results


@pytest.mark.parametrize(
    "decision",
    [
        {"tool": "view_file", "confidence": 0.99},
        {"tool": "grep_search", "confidence": 0.79},
        {"tool": "grep_search", "confidence": 0.99, "fallback": True},
        {"tool": "unknown", "confidence": 0.99},
        {"tool": "grep_search", "confidence": True},
        {"tool": "grep_search", "confidence": "0.99"},
        {"tool": "grep_search", "confidence": float("nan")},
        {"tool": "grep_search", "confidence": float("inf")},
        {"tool": "grep_search", "confidence": 1.01},
        {"tool": ["grep_search"], "confidence": 0.99},
        {},
    ],
)
def test_abstention_preserves_permissions_and_budget(payload, monkeypatch, decision):
    monkeypatch.setenv(
        "HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON", '[{"name":"view_file"},{"name":"grep_search"}]'
    )
    monkeypatch.setattr(hook, "route", lambda *args: (decision, "route"))
    assert hook.handle(payload) == {}
    assert not hook.redirect_marker(payload).exists()


@pytest.mark.parametrize("mode", ["error", "malformed", "exit", "partial"])
def test_router_errors_obey_deadline_and_fail_open(payload, tmp_path, monkeypatch, mode):
    monkeypatch.setenv(
        "HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON", '[{"name":"view_file"},{"name":"grep_search"}]'
    )
    monkeypatch.setenv("HARNESS_ROUTER_TEST_MODE", mode)
    monkeypatch.setenv("HARNESS_ROUTER_PRETOOL_TIMEOUT", "0.2")
    started = time.monotonic()
    run = subprocess.run(
        [sys.executable, str(tmp_path / ".antigravity/hooks/pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert run.returncode == 0
    assert json.loads(run.stdout) == {}
    assert time.monotonic() - started < 2


@pytest.mark.parametrize("raw", ["not json", "[]", "null", "{}", '{"toolCall":42}'])
def test_bad_input_fails_open(raw, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    assert hook.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_router_aliases_cannot_recurse(payload, monkeypatch):
    monkeypatch.setenv(
        "HARNESS_ROUTER_ANTIGRAVITY_TOOLS_JSON",
        json.dumps(
            [
                {"name": "view_file"},
                {"name": "mcp3_route", "serverName": "harness-router"},
            ]
        ),
    )
    monkeypatch.setattr(hook, "route", lambda *args: pytest.fail("should not route"))
    assert hook.handle(payload) == {}
    payload["toolCall"]["name"] = "mcp3_route"
    assert hook.handle(payload) == {}


def test_mcts_uses_live_schemas_and_same_mcp_session(payload, runtime, tmp_path, monkeypatch):
    runtime["metadata"][0]["chatModel"]["tools"][1]["name"] = "grep"
    log, graph_input, provider = (
        tmp_path / name for name in ("calls.jsonl", "graph.json", "graph.py")
    )
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
    result = hook.handle(payload)
    assert result["decision"] == "deny" and "route_mcts" in result["reason"]
    assert [json.loads(line)["params"]["name"] for line in log.read_text().splitlines()] == [
        "route",
        "route_mcts",
    ]
    assert json.loads(graph_input.read_text())["candidates"][0]["input_schema"]["required"] == [
        "AbsolutePath"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:443",
        "http://127.0.0.1:80@evil.com:80",
        "http://127.0.0.1:80/path",
        "file:///tmp/data",
        "http://localhost",
        "http://localhost:80?token=secret",
    ],
)
def test_discovery_rejects_non_local_or_ambiguous_endpoints(url):
    with pytest.raises(ValueError):
        discovery.local_url(url)


def test_runtime_discovery_uses_parent_process_not_global_scan(monkeypatch):
    monkeypatch.delenv("HARNESS_ROUTER_ANTIGRAVITY_URL", raising=False)
    monkeypatch.setattr(discovery.os, "getppid", lambda: 42)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        args = (
            "41 /bin/sh -c python3 hook.py"
            if command[2] == "42"
            else (
                "1 /Applications/Antigravity.app/Contents/Resources/bin/language_server "
                "--https_server_port 12345 --csrf_token=test-token"
            )
        )
        return subprocess.CompletedProcess(command, 0, args, "")

    monkeypatch.setattr(discovery.subprocess, "run", run)
    assert discovery.runtime_endpoint(time.monotonic() + 2) == (
        "https://127.0.0.1:12345",
        "test-token",
    )
    assert [command[2] for command in calls] == ["42", "41"]
