#!/usr/bin/env python3
"""Shared router-first pre-decision hook.

Adapters normalize a harness turn into:
  {"hook":"<harness>","goal":"...","tools":[...],"session_id":"..."}

The hook uses the local router daemon when available, otherwise the CLI. It
writes a short-lived decision record that the harness-specific PreToolUse
adapter can consume. It never executes or mutates tools.
"""
from __future__ import annotations
import json, os, shlex, shutil, socket, subprocess, sys, tempfile, time
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
    if not isinstance(payload,dict): payload={}
    goal=str(payload.get("goal") or os.environ.get("HARNESS_ROUTER_GOAL") or "").strip()
    tools=payload.get("tools")
    if not isinstance(tools,list):
        catalog=os.environ.get("HARNESS_ROUTER_TOOLS_FILE")
        if catalog:
            try:
                raw=json.loads(Path(catalog).read_text(encoding="utf-8")); tools=raw.get("tools",raw) if isinstance(raw,dict) else raw
            except (OSError,ValueError): tools=[]
    if not isinstance(tools,list):
        harness=str(payload.get("harness") or os.environ.get("HARNESS_ROUTER_HARNESS") or "").lower()
        if harness:
            candidate=Path(str(payload.get("cwd") or os.getcwd()))/f".{harness}"/"harness-router-tools.json"
            try:
                raw=json.loads(candidate.read_text(encoding="utf-8")); tools=raw.get("tools",raw) if isinstance(raw,dict) else raw
            except (OSError,ValueError): tools=[]
    if not goal or not isinstance(tools,list) or len(tools)<2: return 0
    cwd=str(payload.get("cwd") or os.getcwd()); session=str(payload.get("session_id") or "default")
    started=time.monotonic(); deadline=started+float(os.environ.get("HARNESS_ROUTER_PREDECISION_TIMEOUT","4"))
    result=route(goal,tools,cwd,max(0.05,deadline-time.monotonic()))
    selected=result.get("tool"); confidence=result.get("confidence"); routing_mode="route"; mcts_result={}
    try: threshold=float(os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_THRESHOLD","0.80"))
    except ValueError: threshold=0.80
    try: confidence_value=float(confidence)
    except (TypeError,ValueError): confidence_value=0.0
    graph_cmd=os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_GRAPH_CMD","").strip()
    if isinstance(selected,str) and selected and len(tools)>=3 and graph_cmd and (result.get("fallback") or confidence_value<threshold):
        graph_input={"goal":goal[:1600],"observation":"Pre-tool-selection: choose the best next tool.","current_tool":selected,
                     "candidates":tools[:32],"route_result":result}
        try:
            p=subprocess.run(shlex.split(graph_cmd),cwd=cwd,input=json.dumps(graph_input,separators=(",",":")),
                             capture_output=True,text=True,timeout=max(0.05,deadline-time.monotonic()),check=False)
            graph=json.loads(p.stdout) if p.returncode==0 and p.stdout.strip() else {}
            if isinstance(graph,dict) and graph.get("root_state") and isinstance(graph.get("states"),list) and isinstance(graph.get("transitions"),list):
                graph.setdefault("simulations",max(1,min(4096,int(os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_SIMULATIONS","64")))))
                graph.setdefault("max_depth",max(1,min(8,int(os.environ.get("HARNESS_ROUTER_PRETOOL_MCTS_MAX_DEPTH","3")))))
                graph.setdefault("use_jev_prior",True)
                binary=os.environ.get("HARNESS_ROUTER_BIN") or shutil.which("harness-router")
                command=[binary,"route-mcts"] if binary else [sys.executable,"-m","harness_router.cli","route-mcts"]
                p=subprocess.run(command,cwd=cwd,input=json.dumps(graph,separators=(",",":")),capture_output=True,text=True,
                                 timeout=max(0.05,deadline-time.monotonic()),check=False)
                if p.returncode==0:
                    candidate=json.loads(p.stdout.strip())
                    if isinstance(candidate,dict) and isinstance(candidate.get("tool"),str) and candidate.get("tool") and not candidate.get("fallback"):
                        mcts_result=candidate; result={**result,**candidate}; routing_mode="route_mcts"
        except (OSError,ValueError,TypeError,subprocess.SubprocessError): pass
    duration_ms=round((time.monotonic()-started)*1000,3)
    selected=result.get("tool"); confidence=result.get("confidence")
    if not isinstance(selected,str) or not selected: return 0
    try: confidence=float(confidence)
    except (TypeError,ValueError): return 0
    r=root(cwd); path=r/".harness-router"/"sessions"/f"{session}.decision.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps({"generated_at":datetime.now(timezone.utc).isoformat(),"harness":payload.get("harness"),
                                "goal":goal,"tool":selected,"confidence":confidence,"routing_mode":routing_mode,
                                "duration_ms":duration_ms,"principal_variation":mcts_result.get("principal_variation",[])},
                               separators=(",",":")),encoding="utf-8")
    print(json.dumps({"tool":selected,"confidence":confidence,"routing_mode":routing_mode,"duration_ms":duration_ms},separators=(",",":")))
    return 0

if __name__=="__main__": raise SystemExit(main())
