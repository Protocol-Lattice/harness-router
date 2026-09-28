#!/usr/bin/env python3
"""Local MCP peer for hook tests. Never calls a model or executes a tool."""

import json
import os
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {}}
    else:
        log = os.environ.get("HARNESS_ROUTER_TEST_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(request) + "\n")
        delay = float(os.environ.get("HARNESS_ROUTER_TEST_DELAY", "0"))
        time.sleep(delay)
        mode = os.environ.get("HARNESS_ROUTER_TEST_MODE", "structured")
        if mode == "partial":
            sys.stdout.write('{"id":')
            sys.stdout.flush()
            time.sleep(30)
            continue
        if mode == "exit":
            raise SystemExit(1)
        decision = json.loads(
            os.environ.get(
                "HARNESS_ROUTER_TEST_RESULT", '{"tool":"grep","confidence":0.95,"fallback":false}'
            )
        )
        if request["params"]["name"] == "route_mcts":
            decision = {"tool": "grep", "confidence": 0.96, "fallback": False}
        if mode == "error":
            result = {"isError": True, "structuredContent": decision}
        elif mode == "text":
            result = {"content": [{"type": "text", "text": json.dumps(decision)}]}
        elif mode == "malformed":
            result = {"content": [{"type": "text", "text": "not json"}]}
        else:
            result = {"structuredContent": decision}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
