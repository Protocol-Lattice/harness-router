import pytest

from harness_router import (
    DefaultExecutionPolicy,
    LoopGuard,
    RiskLevel,
    RouteDecision,
    ToolDescriptor,
)


@pytest.mark.asyncio
async def test_policy_allows_low_risk_and_denies_high_risk() -> None:
    policy = DefaultExecutionPolicy()
    decision = RouteDecision(tool="x", confidence=0.99)
    low = ToolDescriptor("read", "read", risk=RiskLevel.LOW)
    high = ToolDescriptor("delete", "delete", risk=RiskLevel.HIGH)
    assert await policy.allow(decision, low) is True
    assert await policy.allow(decision, high) is False


def test_loop_guard_detects_repeated_action() -> None:
    guard = LoopGuard(max_same_action_repeats=2)
    assert guard.record("read", arguments={"path": "a"}) is False
    assert guard.record("read", arguments={"path": "a"}) is False
    assert guard.record("read", arguments={"path": "a"}) is True


def test_loop_guard_detects_abab() -> None:
    guard = LoopGuard(max_same_action_repeats=10)
    assert guard.record("a") is False
    assert guard.record("b") is False
    assert guard.record("a") is False
    assert guard.record("b") is True
