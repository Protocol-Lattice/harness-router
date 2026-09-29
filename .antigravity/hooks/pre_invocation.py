#!/usr/bin/env python3
"""Antigravity PreInvocation -> Harness Router pre-decision bridge."""
import json, os, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
CATALOG=ROOT/".antigravity"/"harness-router-tools.json"
def main():
    try:p=json.load(sys.stdin)
    except Exception:p={}
    goal=str(p.get("prompt") or p.get("userPrompt") or p.get("input") or "").strip()
    if not goal:return 0
    try:data=json.loads(CATALOG.read_text())
    except Exception:return 0
    tools=data.get("tools",[]) if isinstance(data,dict) else data
    req={"harness":"antigravity","hook":"PreInvocation","cwd":str(ROOT),
         "session_id":str(p.get("conversationId") or "default"),"goal":goal[:1600],"tools":tools}
    try:
      r=subprocess.run([sys.executable,str(ROOT/"hooks"/"pre_decision.py")],input=json.dumps(req),
                       text=True,capture_output=True,timeout=4)
      if r.stdout.strip(): print(r.stdout.strip())
    except Exception: pass
    return 0
if __name__=="__main__":raise SystemExit(main())
