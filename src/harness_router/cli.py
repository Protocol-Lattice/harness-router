from __future__ import annotations
import argparse, asyncio, json, os, sys
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Mapping, Sequence
from .adapters import GenericToolAdapter
from .config import OpenRouterConfig, RoutingConfig
from .models import HarnessState, RoutingMode
from .provider import OpenRouterJevProvider
from .router import JevToolRouter

def package_version() -> str:
    try: return version("harness-router")
    except PackageNotFoundError: return "0.1.1"

def build_parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(prog="harness-router",description="Route harness tools through OpenRouter Jev.")
    parser.add_argument("--version",action="version",version=f"%(prog)s {package_version()}")
    subparsers=parser.add_subparsers(dest="command")
    route=subparsers.add_parser("route",help="Choose the next tool using OpenRouter Jev.")
    route.add_argument("--goal",required=True); route.add_argument("--observation",default=None)
    route.add_argument("--last-action",default=None); route.add_argument("--tools-json",required=True)
    route.add_argument("--mode",choices=[m.value for m in RoutingMode],default=RoutingMode.HYBRID.value)
    route.add_argument("--direct-threshold",type=float,default=0.85); route.add_argument("--fallback-threshold",type=float,default=0.60)
    route.add_argument("--hierarchical-threshold",type=int,default=24); route.add_argument("--verbose",action="store_true")
    route.add_argument("--no-daemon",action="store_true"); route.add_argument("--no-cache",action="store_true")
    mcts=subparsers.add_parser("route-mcts",help="Search a supplied side-effect-free state graph with MCTS.")
    mcts.add_argument("--graph-json",default=None)
    return parser

def _load_tools(raw:str)->list[Mapping[str,Any]]:
    try: value=json.loads(raw)
    except json.JSONDecodeError as exc: raise SystemExit(f"invalid --tools-json: {exc}") from exc
    if not isinstance(value,list) or not value: raise SystemExit("--tools-json must be a non-empty JSON array")
    if not all(isinstance(x,dict) for x in value): raise SystemExit("--tools-json entries must be objects")
    return value

def _request_payload(args:argparse.Namespace)->dict[str,Any]:
    return {"goal":args.goal,"observation":args.observation,"last_action":args.last_action,"tools":_load_tools(args.tools_json),
            "mode":args.mode,"direct_threshold":args.direct_threshold,"fallback_threshold":args.fallback_threshold,
            "hierarchical_threshold":args.hierarchical_threshold,"route_cache_size":0 if args.no_cache else 128,
            "obvious_cache_size":0 if args.no_cache else 256}

async def _run_direct(request:Mapping[str,Any])->tuple[dict[str,object],int]:
    adapter=GenericToolAdapter(); tools=[adapter.normalize(t) for t in request["tools"]]
    provider=OpenRouterJevProvider.from_config(OpenRouterConfig())
    router=JevToolRouter(provider,RoutingConfig(mode=RoutingMode(str(request["mode"])),
        direct_execution_threshold=float(request["direct_threshold"]),fallback_threshold=float(request["fallback_threshold"]),
        hierarchical_threshold=int(request["hierarchical_threshold"]),route_cache_size=int(request.get("route_cache_size",128)),
        obvious_cache_size=int(request.get("obvious_cache_size",256))))
    try:
        decision=await router.route(HarnessState(goal=str(request["goal"]),observation=request["observation"],last_action=request["last_action"]),tools)
    finally: await provider.aclose()
    return {"tool":decision.tool,"category":decision.category,"confidence":round(decision.confidence,4),
            "fallback":decision.fallback,"fallback_reason":decision.fallback_reason,"provider":"openrouter",
            "provider_requests":provider.requests_made},(0 if provider.requests_made>0 else 2)

async def _run_route(args:argparse.Namespace)->int:
    request=_request_payload(args); response=None
    if not args.no_daemon and os.environ.get("HARNESS_ROUTER_NO_DAEMON")!="1":
        try:
            from .daemon import ensure_daemon,request_daemon
            if await ensure_daemon(): response=await request_daemon(request)
        except (OSError,asyncio.TimeoutError,ValueError): response=None
    if response is None: response,status=await _run_direct(request)
    else: status=0 if int(response.get("provider_requests",0))>0 else 2
    print(json.dumps(response,ensure_ascii=False,indent=2 if args.verbose else None,sort_keys=bool(args.verbose))); return status

async def _run_mcts(raw:str)->dict[str,object]:
    try: graph=json.loads(raw)
    except json.JSONDecodeError as exc: raise SystemExit(f"invalid MCTS graph JSON: {exc}") from exc
    if not isinstance(graph,dict): raise SystemExit("MCTS graph must be a JSON object")
    from .mcp_server import MCTSTransition,MCTSState,_GraphEnvironment
    from .mcts import MCTSConfig,MCTSToolRouter
    states=[MCTSState.model_validate(x) for x in graph.get("states",[])]
    transitions=[MCTSTransition.model_validate(x) for x in graph.get("transitions",[])]
    root_id=str(graph.get("root_state",""))
    if not root_id: raise SystemExit("root_state is required")
    env=_GraphEnvironment(states,transitions); root=env.state(root_id)
    provider=None; policy=None
    if bool(graph.get("use_jev_prior",True)):
        try:
            provider=OpenRouterJevProvider.from_config(OpenRouterConfig(timeout_seconds=2.0))
            policy=JevToolRouter(provider,RoutingConfig(mode=RoutingMode.HYBRID,direct_execution_threshold=0.72,
                fallback_threshold=0.72,hierarchical_threshold=48,description_limit=96,history_limit=1,state_field_limit=400,constraint_limit=2))
        except Exception: policy=None
    try:
        router=MCTSToolRouter(env,policy_router=policy,config=MCTSConfig(
            simulations=max(1,min(4096,int(graph.get("simulations",64)))),max_depth=max(1,min(8,int(graph.get("max_depth",3)))),
            max_policy_evaluations=1 if policy is not None else 0))
        result=await router.search(root)
        return {"tool":result.decision.tool,"confidence":round(result.decision.confidence,4),"fallback":result.decision.fallback,
                "fallback_reason":result.decision.fallback_reason,"principal_variation":list(result.principal_variation),
                "simulations":result.simulations,"policy_evaluations":result.policy_evaluations}
    finally:
        if provider is not None: await provider.aclose()

async def _amain(args:argparse.Namespace,parser:argparse.ArgumentParser)->int:
    if args.command=="route": return await _run_route(args)
    if args.command=="route-mcts":
        raw=args.graph_json if args.graph_json is not None else sys.stdin.read()
        print(json.dumps(await _run_mcts(raw),ensure_ascii=False,separators=(",",":"))); return 0
    parser.print_help(); return 0

def main(argv:Sequence[str]|None=None)->None:
    parser=build_parser(); args=parser.parse_args(argv)
    try: raise SystemExit(asyncio.run(_amain(args,parser)))
    except KeyboardInterrupt: raise SystemExit(130)

if __name__=="__main__": main()
