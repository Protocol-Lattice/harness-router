# Integration reference

## Purpose

`harness-router` is a semantic tool-selection layer. It is not a replacement for the harness executor or the main reasoning model.

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
