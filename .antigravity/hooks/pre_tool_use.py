#!/usr/bin/env python3
"""Antigravity PreToolUse: validate the precomputed next-tool decision locally."""
from __future__ import annotations
import argparse, hashlib, json, os, subprocess, sys
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def main(argv=None)->int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--stop",action="store_true")
    args=parser.parse_args(argv)
    try:p=json.load(sys.stdin)
    except (OSError,ValueError): p={}
    if not isinstance(p,dict):
        print("{}"); return 0
    if args.stop:
        conversation=str(p.get("conversationId") or "default")
        try:
            result=subprocess.run(
                [sys.executable, str(ROOT/"hooks"/"stop.py")],
                input=json.dumps({
                    "cwd": str(ROOT),
                    "session_id": conversation,
                    "stop_hook_active": bool(p.get("stopHookActive", False)),
                }),
                text=True, capture_output=True, timeout=2, check=False,
            )
            output=result.stdout.strip()
            if output:
                print(output.splitlines()[0])
                return 0
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        print("{}"); return 0
    call=p.get("toolCall") if isinstance(p.get("toolCall"),dict) else {}
    current=str(call.get("name") or "").strip()
    if not current or "harness_router" in current.lower().replace("-","_"):
        print("{}"); return 0
    conversation=str(p.get("conversationId") or "default")
    path=ROOT/".harness-router"/"sessions"/f"{conversation}.decision.json"
    try:
        decision=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError):
        print("{}"); return 0
    if not isinstance(decision,dict): print("{}"); return 0
    try:
        generated=datetime.fromisoformat(str(decision.get("generated_at","")).replace("Z","+00:00"))
        ttl=float(os.environ.get("HARNESS_ROUTER_PREDECISION_TTL","30"))
        if (datetime.now(UTC)-generated).total_seconds()>ttl: print("{}"); return 0
        confidence=float(decision.get("confidence"))
        threshold=float(os.environ.get("HARNESS_ROUTER_PREDECISION_THRESHOLD","0.85"))
    except (TypeError,ValueError):
        print("{}"); return 0
    selected=decision.get("tool")
    if not isinstance(selected,str) or not selected or selected==current or confidence<threshold:
        print("{}"); return 0
    key=f"{conversation}:{decision.get('state_key','')}:{current}"
    marker=ROOT/".harness-router"/"sessions"/f"{hashlib.sha256(key.encode()).hexdigest()}.rerouted"
    try:
        marker.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        os.close(fd)
    except FileExistsError:
        print("{}"); return 0
    except OSError:
        print("{}"); return 0
    print(json.dumps({"decision":"deny","reason":
        f"Harness Router precomputed {selected!r} at confidence {confidence:.3f}, "
        f"but Antigravity selected {current!r}. Re-plan once."}))
    return 0

if __name__=="__main__": raise SystemExit(main())
