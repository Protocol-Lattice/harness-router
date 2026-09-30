#!/usr/bin/env python3
"""DeepSeek Harness hook: precompute after tools, validate before tools."""
from __future__ import annotations
from datetime import UTC, datetime
import hashlib, json, os, subprocess, sys
from pathlib import Path

def emit(value: dict) -> None:
    print(json.dumps(value, separators=(",", ":"), ensure_ascii=False))

def root() -> Path:
    return Path(os.getcwd()).resolve()

def tools_for(cwd: Path) -> list[dict]:
    path = cwd / ".dsh" / "harness-router-tools.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    tools = value.get("tools", value) if isinstance(value, dict) else value
    return tools if isinstance(tools, list) else []

def load_decision(cwd: Path, session: str) -> dict:
    path = cwd / ".harness-router" / "sessions" / f"{session}.decision.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

def fresh(value: dict) -> bool:
    try:
        generated = datetime.fromisoformat(str(value.get("generated_at","")).replace("Z","+00:00"))
        return (datetime.now(UTC)-generated).total_seconds() <= float(
            os.environ.get("HARNESS_ROUTER_PREDECISION_TTL","30")
        )
    except (TypeError, ValueError):
        return False

def precompute(payload: dict) -> int:
    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    session = str(payload.get("session_id") or "default")
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = tools_for(cwd)
    decision = load_decision(cwd, session)
    goal = str(payload.get("goal") or decision.get("goal") or "").strip()[:1600]
    if not goal or len(tools) < 2:
        emit({}); return 0
    request = {
        "harness":"deepseek", "hook":"PostToolUse", "cwd":str(cwd),
        "session_id":session,
        "decision_id":str(payload.get("step_idx") or payload.get("turn_id") or ""),
        "goal":goal,
        "observation":str(payload.get("tool_response") or payload.get("tool_result") or payload.get("error") or "")[:4000],
        "last_action":str(payload.get("tool_name") or "") or None,
        "tools":tools,
    }
    try:
        p=subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[2]/"hooks"/"post_tool_use.py")],
            input=json.dumps(request,ensure_ascii=False), text=True, capture_output=True,
            timeout=float(os.environ.get("HARNESS_ROUTER_POSTTOOL_TIMEOUT","4")),
            cwd=str(cwd), env=os.environ.copy(), check=False)
        if p.stderr: print(p.stderr.rstrip(),file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    emit({}); return 0

def validate(payload: dict) -> int:
    cwd=Path(str(payload.get("cwd") or os.getcwd()))
    session=str(payload.get("session_id") or "default")
    current=str(payload.get("tool_name") or "").strip()
    if not current or "harness-router" in current.lower().replace("-","_"):
        emit({"hookSpecificOutput":{"hookEventName":"PreToolUse"}}); return 0
    decision=load_decision(cwd,session)
    selected=decision.get("tool")
    try:
        confidence=float(decision.get("confidence"))
        threshold=float(os.environ.get("HARNESS_ROUTER_PREDECISION_THRESHOLD","0.85"))
    except (TypeError,ValueError):
        emit({"hookSpecificOutput":{"hookEventName":"PreToolUse"}}); return 0
    if not fresh(decision) or not isinstance(selected,str) or not selected or selected==current or confidence<threshold:
        emit({"hookSpecificOutput":{"hookEventName":"PreToolUse"}}); return 0
    marker_key = f"{session}:{decision.get('state_key', '')}:{current}"
    marker = cwd / ".harness-router" / "sessions" / (
        f"{hashlib.sha256(marker_key.encode()).hexdigest()}.rerouted"
    )
    try:
        marker.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600); os.close(fd)
    except FileExistsError:
        emit({"hookSpecificOutput":{"hookEventName":"PreToolUse"}}); return 0
    except OSError:
        emit({"hookSpecificOutput":{"hookEventName":"PreToolUse"}}); return 0
    reason=f"Harness Router precomputed {selected!r} at confidence {confidence:.3f}, but DeepSeek selected {current!r}. Re-plan once."
    emit({"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":reason,"additionalContext":reason}})
    return 0

def main()->int:
    try:p=json.load(sys.stdin)
    except (OSError,ValueError): p={}
    if not isinstance(p,dict): emit({}); return 0
    event=str(p.get("hook_event_name") or "")
    if event=="PostToolUse": return precompute(p)
    if event=="PreToolUse": return validate(p)
    if event=="Stop":
        script = Path(__file__).with_name("stop.py")
        try:
            result = subprocess.run(
                [sys.executable, str(script)],
                input=json.dumps(p, ensure_ascii=False), text=True,
                capture_output=True, timeout=float(os.environ.get("HARNESS_ROUTER_STOP_TIMEOUT","2")),
                cwd=str(Path(str(p.get("cwd") or os.getcwd()))), env=os.environ.copy(), check=False,
            )
            if result.stdout.strip():
                emit(json.loads(result.stdout.strip().splitlines()[0]))
                return 0
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
        emit({}); return 0
    emit({}); return 0

if __name__=="__main__": raise SystemExit(main())
