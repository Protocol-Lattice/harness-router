# Integration reference

## Purpose

`harness-router` is a semantic tool-selection layer. It is not a replacement for the harness executor or the main reasoning model.

In integrations where invoking the router itself consumes a planner turn, route only at real ambiguity points. Skip the router when the next tool is already obvious, and stop routing after repeated fallbacks.

Recommended control flow:

```text
goal/state
   |
   v
JevToolRouter
   |
   +--> fallback ----------> planner/model
   |
   v
selected tool
   |
   v
argument generation
   |
   v
policy / approval
   |
   v
executor
   |
   v
observation
```

## Codex fast path

In unmodified Codex, invoking a routing helper is itself an extra model/tool turn. Optimize for
the complete loop, not selector latency in isolation.

Use normal Codex tool calling for linear work. Invoke the helper at most once, only at a
high-ambiguity branch point where at least four tools remain plausible after deterministic
pruning and a wrong choice is likely to create multiple exploratory turns.

For the helper call:

- pass 4-12 plausible candidates when possible
- use short descriptions; omit full input schemas
- send only the latest observation and decision-relevant constraints
- use equal direct/fallback thresholds to avoid a planner-confirmation band
- stop routing for the task after a fallback or planner override
- do not use hierarchical routing for medium registries when one flat request fits

The bundled skill helper defaults to a 2 second provider timeout, 0.72/0.72 confidence
thresholds, compact state limits, and flat routing through 48 tools.

## Minimal setup

```python
from harness_router import (
    HarnessState,
    JevToolRouter,
    OpenRouterConfig,
    OpenRouterJevProvider,
    RiskLevel,
    RoutingConfig,
    ToolDescriptor,
)

provider = OpenRouterJevProvider.from_config(OpenRouterConfig())
router = JevToolRouter(provider, RoutingConfig())

state = HarnessState(
    goal="Find the cause of the failing test",
    observation="pytest reports a failure in parser tests",
)

tools = [
    ToolDescriptor(
        name="read_file",
        description="Read a source file",
        category="inspect",
        risk=RiskLevel.LOW,
    ),
    ToolDescriptor(
        name="run_tests",
        description="Run the test suite",
        category="verify",
        risk=RiskLevel.LOW,
    ),
]

decision = await router.route(state, tools)
```

## Fallback

Always check:

```python
if decision.fallback:
    return await planner_step(...)
```

Do not silently treat fallback as a selected tool.

## Tool argument generation

Use Jev for closed choices and the main model for open-ended generation.

For example:

```text
Jev chooses:
write_file

main model generates:
path + full source content

policy checks:
is this write allowed?

executor:
writes the file
```

## Risk

Treat tool risk independently from confidence.

A high-confidence Jev result is an estimate about which action fits the state. It is not permission to execute a destructive action.
