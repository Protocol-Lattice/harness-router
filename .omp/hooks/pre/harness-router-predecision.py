#!/usr/bin/env python3
"""OhMyPi turn_start bridge.

OhMyPi's native turn_start hook fires before each model turn. We intentionally
use the session transcript as the source of truth instead of inventing a
prompt field in the hook event.
"""
import json,os,subprocess,sys
from pathlib import Path
ROOT=Path.cwd()
# OhMyPi exposes its transcript/session through its runtime; this bridge accepts
# an optional JSON payload for hosts that pipe it into the hook process.
try:p=json.load(sys.stdin)
except Exception:p={}
goal=str(p.get("prompt") or p.get("goal") or os.environ.get("HARNESS_ROUTER_GOAL","")).strip()
if goal:
  try:
    catalog=json.loads((ROOT/".omp"/"harness-router-tools.json").read_text())
    tools=catalog.get("tools",[]) if isinstance(catalog,dict) else catalog
    req={"harness":"ohmypi","hook":"turn_start","cwd":str(ROOT),"session_id":str(p.get("session_id","default")),"goal":goal[:1600],"tools":tools}
    subprocess.run([sys.executable,str(ROOT/"hooks"/"pre_decision.py")],input=json.dumps(req),text=True,timeout=4)
  except Exception: pass
