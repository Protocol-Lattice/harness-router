#!/usr/bin/env python3
"""Antigravity PostToolUse -> Harness Router next-decision precompute bridge."""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def main():
    try: p=json.load(sys.stdin)
    except (OSError,ValueError): p={}
    if not isinstance(p,dict): print("{}"); return 0
    conversation=str(p.get("conversationId") or "default")
    call=p.get("toolCall") if isinstance(p.get("toolCall"),dict) else {}
    tool_name=str(call.get("name") or p.get("tool_name") or "").strip()
    workspace=p.get("workspacePaths")
    cwd=str(workspace[0]) if isinstance(workspace,list) and workspace else str(p.get("cwd") or ROOT)
    tools=[]
    try:
        raw=json.loads((ROOT/".antigravity/harness-router-tools.json").read_text(encoding="utf-8"))
        tools=raw.get("tools",raw) if isinstance(raw,dict) else raw
    except (OSError,ValueError): pass
    decision=ROOT/".harness-router"/"sessions"/f"{conversation}.decision.json"
    goal=""
    try:
        raw=json.loads(decision.read_text(encoding="utf-8"))
        goal=str(raw.get("goal") or "")[:1600]
    except (OSError,ValueError): pass
    error=str(p.get("error") or "")
    observation=error or f"Antigravity completed {tool_name!r}."
    request={"harness":"antigravity","hook":"PostToolUse","cwd":cwd,"session_id":conversation,
             "decision_id":str(p.get("stepIdx") or ""),"goal":goal,
             "observation":observation[:4000],"last_action":tool_name or None,"tools":tools}
    if goal and isinstance(tools,list) and len(tools)>=2:
        try:
            r=subprocess.run([sys.executable,str(ROOT/"hooks"/"post_tool_use.py")],
                input=json.dumps(request,ensure_ascii=False),text=True,capture_output=True,
                timeout=float(os.environ.get("HARNESS_ROUTER_POSTTOOL_TIMEOUT","4")),
                cwd=cwd,env=os.environ.copy(),check=False)
            if r.stderr: print(r.stderr.rstrip(),file=sys.stderr)
        except (OSError,subprocess.SubprocessError,ValueError): pass
    print("{}")
    return 0
if __name__=="__main__": raise SystemExit(main())
