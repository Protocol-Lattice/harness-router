from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .models import HarnessState, RouteDecision, ToolDescriptor
from .router import JevToolRouter


@dataclass(frozen=True, slots=True)
class MCTSConfig:
    """Bounded Monte Carlo Tree Search settings."""

    simulations: int = 64
    max_depth: int = 4
    exploration_constant: float = 1.5
    discount: float = 0.95
    max_policy_evaluations: int = 1
    min_prior: float = 1e-6

    def __post_init__(self) -> None:
        if self.simulations < 1:
            raise ValueError("simulations must be >= 1")
        if self.max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        if self.exploration_constant < 0.0:
            raise ValueError("exploration_constant must be >= 0")
        if not 0.0 <= self.discount <= 1.0:
            raise ValueError("discount must be between 0 and 1")
        if self.max_policy_evaluations < 0:
            raise ValueError("max_policy_evaluations must be >= 0")
        if self.min_prior <= 0.0:
            raise ValueError("min_prior must be > 0")


@dataclass(frozen=True, slots=True)
class SimulatedStep:
    """A side-effect-free predicted transition used only during search."""

    state: HarnessState
    reward: float = 0.0
    terminal: bool = False


class SearchEnvironment(Protocol):
    """Side-effect-free model of the harness used by MCTS."""

    async def tools(self, state: HarnessState) -> Sequence[ToolDescriptor]: ...

    async def transition(
        self,
        state: HarnessState,
        tool: ToolDescriptor,
    ) -> SimulatedStep: ...

    async def evaluate(self, state: HarnessState) -> float: ...


@dataclass(frozen=True, slots=True)
class MCTSResult:
    """Search diagnostics plus the selected first tool.

    The embedded RouteDecision confidence is the selected root action's visit share,
    and probabilities are normalized root visit counts. They are not calibrated Jev
    confidence values.
    """

    decision: RouteDecision
    principal_variation: tuple[str, ...]
    root_visits: Mapping[str, int]
    root_values: Mapping[str, float]
    simulations: int
    policy_evaluations: int


@dataclass(slots=True)
class _Node:
    state: HarnessState
    parent: _Node | None = None
    action: ToolDescriptor | None = None
    prior: float = 1.0
    reward: float = 0.0
    depth: int = 0
    terminal: bool = False
    visits: int = 0
    value_sum: float = 0.0
    prepared: bool = False
    unexpanded: list[tuple[ToolDescriptor, float]] = field(default_factory=list)
    children: dict[str, _Node] = field(default_factory=dict)

    @property
    def mean_value(self) -> float:
        if self.visits == 0:
            return 0.0
        return self.value_sum / self.visits


class MCTSToolRouter:
    """Look ahead across tool sequences with bounded Monte Carlo Tree Search.

    The environment must simulate transitions without executing real tools. Jev is
    optional and is used as a policy prior only. By default the policy router is
    evaluated at most once, keeping the remaining simulations local and token-free.
    """

    def __init__(
        self,
        environment: SearchEnvironment,
        *,
        policy_router: JevToolRouter | None = None,
        config: MCTSConfig | None = None,
    ) -> None:
        self._environment = environment
        self._policy_router = policy_router
        self._config = config or MCTSConfig()
        self._policy_evaluations = 0

    async def route(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
    ) -> RouteDecision:
        return (await self.search(state, tools)).decision

    async def search(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor] | None = None,
    ) -> MCTSResult:
        self._policy_evaluations = 0
        root_tools = list(tools) if tools is not None else list(await self._environment.tools(state))
        self._validate_tools(root_tools)

        if not root_tools:
            return MCTSResult(
                decision=RouteDecision.fallback_to_planner("mcts_no_tools"),
                principal_variation=(),
                root_visits={},
                root_values={},
                simulations=0,
                policy_evaluations=0,
            )

        root = _Node(state=state)
        await self._prepare(root, root_tools)

        for _ in range(self._config.simulations):
            node = root
            path = [root]

            while True:
                if node.terminal or node.depth >= self._config.max_depth:
                    leaf_value = await self._evaluate(node.state)
                    break

                if not node.prepared:
                    node_tools = list(await self._environment.tools(node.state))
                    self._validate_tools(node_tools)
                    await self._prepare(node, node_tools)

                if node.unexpanded:
                    tool, prior = node.unexpanded.pop(0)
                    step = await self._environment.transition(node.state, tool)
                    self._validate_number(step.reward, "transition reward")
                    child = _Node(
                        state=step.state,
                        parent=node,
                        action=tool,
                        prior=prior,
                        reward=float(step.reward),
                        depth=node.depth + 1,
                        terminal=step.terminal,
                    )
                    node.children[tool.name] = child
                    node = child
                    path.append(node)
                    leaf_value = await self._evaluate(node.state)
                    break

                if not node.children:
                    leaf_value = await self._evaluate(node.state)
                    break

                node = self._select_child(node)
                path.append(node)

            self._backpropagate(path, leaf_value)

        if not root.children:
            return MCTSResult(
                decision=RouteDecision.fallback_to_planner("mcts_no_simulated_action"),
                principal_variation=(),
                root_visits={},
                root_values={},
                simulations=self._config.simulations,
                policy_evaluations=self._policy_evaluations,
            )

        best = max(
            root.children.values(),
            key=lambda child: (
                child.visits,
                self._action_value(child),
                child.prior,
                child.action.name if child.action is not None else "",
            ),
        )
        assert best.action is not None

        total_root_visits = sum(child.visits for child in root.children.values())
        visit_probabilities = {
            name: child.visits / total_root_visits
            for name, child in root.children.items()
            if total_root_visits > 0
        }
        confidence = visit_probabilities.get(best.action.name, 0.0)

        return MCTSResult(
            decision=RouteDecision(
                tool=best.action.name,
                category=best.action.category,
                confidence=confidence,
                probabilities=visit_probabilities,
            ),
            principal_variation=self._principal_variation(root),
            root_visits={name: child.visits for name, child in root.children.items()},
            root_values={
                name: self._action_value(child)
                for name, child in root.children.items()
            },
            simulations=self._config.simulations,
            policy_evaluations=self._policy_evaluations,
        )

    async def _prepare(
        self,
        node: _Node,
        tools: Sequence[ToolDescriptor],
    ) -> None:
        if node.prepared:
            return

        priors = await self._priors(node.state, tools)
        node.unexpanded = sorted(
            ((tool, priors[tool.name]) for tool in tools),
            key=lambda item: (-item[1], item[0].name),
        )
        node.prepared = True

    async def _priors(
        self,
        state: HarnessState,
        tools: Sequence[ToolDescriptor],
    ) -> dict[str, float]:
        if not tools:
            return {}
        if len(tools) == 1:
            return {tools[0].name: 1.0}

        raw: Mapping[str, float] = {}
        if (
            self._policy_router is not None
            and self._policy_evaluations < self._config.max_policy_evaluations
        ):
            self._policy_evaluations += 1
            decision = await self._policy_router.route(state, tools)
            if not decision.fallback:
                raw = decision.probabilities
                if decision.tool is not None and decision.tool not in raw:
                    raw = {
                        **raw,
                        decision.tool: max(decision.confidence, self._config.min_prior),
                    }

        weights: dict[str, float] = {}
        for tool in tools:
            value = float(raw.get(tool.name, 0.0))
            if not math.isfinite(value) or value <= 0.0:
                value = self._config.min_prior
            weights[tool.name] = max(value, self._config.min_prior)
        total = sum(weights.values())
        if total <= 0.0:
            uniform = 1.0 / len(tools)
            return {tool.name: uniform for tool in tools}
        return {name: value / total for name, value in weights.items()}

    def _select_child(self, node: _Node) -> _Node:
        parent_visits = max(1, node.visits)

        def score(child: _Node) -> tuple[float, float, str]:
            exploitation = self._action_value(child)
            exploration = (
                self._config.exploration_constant
                * child.prior
                * math.sqrt(parent_visits)
                / (1 + child.visits)
            )
            name = child.action.name if child.action is not None else ""
            return exploitation + exploration, child.prior, name

        return max(node.children.values(), key=score)

    def _action_value(self, child: _Node) -> float:
        return child.reward + self._config.discount * child.mean_value

    async def _evaluate(self, state: HarnessState) -> float:
        value = float(await self._environment.evaluate(state))
        self._validate_number(value, "state value")
        return value

    def _backpropagate(self, path: Sequence[_Node], leaf_value: float) -> None:
        value = leaf_value
        for node in reversed(path):
            node.visits += 1
            node.value_sum += value
            if node.parent is not None:
                value = node.reward + self._config.discount * value

    def _principal_variation(self, root: _Node) -> tuple[str, ...]:
        variation: list[str] = []
        node = root
        while node.children:
            child = max(
                node.children.values(),
                key=lambda item: (
                    item.visits,
                    self._action_value(item),
                    item.prior,
                    item.action.name if item.action is not None else "",
                ),
            )
            if child.action is None or child.visits == 0:
                break
            variation.append(child.action.name)
            node = child
        return tuple(variation)

    @staticmethod
    def _validate_tools(tools: Sequence[ToolDescriptor]) -> None:
        names = [tool.name for tool in tools]
        if any(not name.strip() for name in names):
            raise ValueError("tool names must be non-empty")
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")

    @staticmethod
    def _validate_number(value: float, label: str) -> None:
        if not math.isfinite(value):
            raise ValueError(f"{label} must be finite")
