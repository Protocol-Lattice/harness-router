#!/usr/bin/env python3
"""Shared router-first pre-decision hook.

Adapters normalize a harness turn into:
  {"hook":"<harness>","goal":"...","tools":[...],"session_id":"..."}

The hook uses the local router daemon when available, otherwise the CLI. It
writes a short-lived decision record that the harness-specific PreToolUse
adapter can consume. It never executes or mutates tools.
"""
from __future__ import annotations
import json, os, shutil, socket, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def root(cwd: str) -> Path:
    p=Path(cwd).resolve()
    for x in (p,*p.parents):
        if (x/".git").exists(): return x
    return Path(cwd)

def route(goal: str, tools: list[dict[str,Any]], cwd: str, timeout: float) -> dict[str,Any]:
    req={"goal":goal[:1600],"observation":"Pre-tool-selection: choose the best next tool.",
         "last_action":None,"tools":[{k:t.get(k) for k in ("name","description","category","risk")} for t in tools],
         "mode":"jev_only","direct_threshold":0.85,"fallback_threshold":0.60,
         "hierarchical_threshold":24,"route_cache_size":256,
         "obvious_cache_size":512,"obvious_cache_ttl_seconds":300}
    wire=(json.dumps(req,separators=(",",":"),ensure_ascii=False)+"\n").encode()
    uid=str(os.getuid()) if hasattr(os,"getuid") else str(os.getpid())
    sock=os.environ.get("HARNESS_ROUTER_SOCKET") or str(Path(tempfile.gettempdir())/f"harness-router-{uid}.sock")
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
            s.settimeout(timeout); s.connect(sock); s.sendall(wire)
            data=bytearray()
            while len(data)<65536:
                chunk=s.recv(4096)
                if not chunk: break
                data.extend(chunk)
                if b"\n" in chunk: break
        result=json.loads(data.split(b"\n",1)[0])
        if isinstance(result,dict): return result
    except (OSError,ValueError,json.JSONDecodeError):
        pass
    binary=os.environ.get("HARNESS_ROUTER_BIN") or shutil.which("harness-router")
    if not binary: return {}
    try:
        p=subprocess.run([binary,"route","--mode","jev_only","--goal",goal[:1600],
                          "--observation","Pre-tool-selection: choose the best next tool.",
                          "--tools-json",json.dumps(req["tools"],separators=(",",":"))],
                         cwd=cwd,capture_output=True,text=True,timeout=timeout,check=False)
        if p.returncode==0:
            result=json.loads(p.stdout.strip())
            return result if isinstance(result,dict) else {}
    except (OSError,ValueError,subprocess.SubprocessError):
        pass
    return {}

def main() -> int:
    try: payload=json.load(sys.stdin)
    except (OSError,ValueError): payload={}
    if not isinstance(payload,dict): return 0
    goal=str(payload.get("goal") or "").strip()
    tools=payload.get("tools")
    if not goal or not isinstance(tools,list) or len(tools)<2: return 0
    cwd=str(payload.get("cwd") or os.getcwd())
    session=str(payload.get("session_id") or "default")
    result=route(goal,tools,cwd,float(os.environ.get("HARNESS_ROUTER_PREDECISION_TIMEOUT","4")))
    selected=result.get("tool"); confidence=result.get("confidence")
    if result.get("fallback") or not isinstance(selected,str) or not selected: return 0
    try: confidence=float(confidence)
    except (TypeError,ValueError): return 0
    r=root(cwd); path=r/".harness-router"/"sessions"/f"{session}.decision.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({"generated_at":datetime.now(timezone.utc).isoformat(),
                                "harness":payload.get("harness"),"goal":goal,
                                "tool":selected,"confidence":confidence},separators=(",",":")),encoding="utf-8")
    print(json.dumps({"tool":selected,"confidence":confidence,"duration_ms":0},separators=(",",":")))
    return 0

if __name__=="__main__": raise SystemExit(main())
