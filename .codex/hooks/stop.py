#!/usr/bin/env python3
"""Codex Stop hook bridge for Harness Router."""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
p = subprocess.run(
    [sys.executable, str(ROOT / "hooks" / "stop.py")],
    input=sys.stdin.read(),
    text=True,
    capture_output=True,
    check=False,
)
try:
    result = json.loads(p.stdout or "{}")
except json.JSONDecodeError:
    result = {}
if result.get("continue") and result.get("reason"):
    print(json.dumps({"decision": "block", "reason": result["reason"]}))
else:
    print("{}")
