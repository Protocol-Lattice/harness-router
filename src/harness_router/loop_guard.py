from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ActionFingerprint:
    tool: str
    arguments_hash: str | None = None
    result_class: str | None = None


class LoopGuard:
    def __init__(self, *, max_same_action_repeats: int = 2, history_size: int = 8) -> None:
        if max_same_action_repeats < 1:
            raise ValueError("max_same_action_repeats must be >= 1")
        if history_size < 4:
            raise ValueError("history_size must be >= 4")
        self._max_same_action_repeats = max_same_action_repeats
        self._history: deque[ActionFingerprint] = deque(maxlen=history_size)

    def record(
        self,
        tool: str,
        *,
        arguments: Mapping[str, Any] | None = None,
        result_class: str | None = None,
    ) -> bool:
        fingerprint = ActionFingerprint(
            tool=tool,
            arguments_hash=hash_arguments(arguments) if arguments is not None else None,
            result_class=result_class,
        )
        self._history.append(fingerprint)
        return self.loop_detected()

    def loop_detected(self) -> bool:
        items = list(self._history)
        if not items:
            return False

        current = items[-1]
        same = 0
        for item in reversed(items):
            if item == current:
                same += 1
            else:
                break
        if same > self._max_same_action_repeats:
            return True

        return len(items) >= 4 and items[-4] == items[-2] and items[-3] == items[-1]


def hash_arguments(arguments: Mapping[str, Any]) -> str:
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
