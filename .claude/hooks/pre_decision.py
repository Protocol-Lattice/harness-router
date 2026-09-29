#!/usr/bin/env python3
"""Claude Code UserPromptSubmit -> Harness Router pre-decision bridge."""
import json, os, subprocess, sys
from pathlib import Path

ROOT=Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
CATALOG=ROOT/".claude"/"harness-router-tools.json"

def main():
    try: p=json.load(sys.stdin)
    except Exception: p={}
    goal=str(p.get("prompt") or "").strip()
    if not goal: return 0
    try: data=json.loads(CATALOG.read_text())
    except Exception: return 0
    tools=data.get("tools",[]) if isinstance(data,dict) else data
    if not isinstance(tools,list) or len(tools)<2: return 0
    req={"harness":"claude","hook":"UserPromptSubmit","cwd":str(ROOT),
         "session_id":str(p.get("session_id") or "default"),"goal":goal[:1600],"tools":tools}
    proc=subprocess.run([sys.executable,str(ROOT/"hooks"/"pre_decision.py")],
                        input=json.dumps(req),text=True,capture_output=True,
                        timeout=float(os.environ.get("HARNESS_ROUTER_PREDECISION_TIMEOUT","4")))
    if proc.returncode==0 and proc.stdout.strip():
        try:
            d=json.loads(proc.stdout)
            if d.get("tool"):
                print(json.dumps({"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
                    "additionalContext":f"Harness Router precomputed next tool {d['tool']!r} (confidence {float(d.get('confidence',0)):.3f}). Prefer it when it matches the task."}}))
        except Exception: pass
    return 0
if __name__=="__main__": raise SystemExit(main())
