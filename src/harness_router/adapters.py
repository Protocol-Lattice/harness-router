from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from .models import RiskLevel, ToolDescriptor


class ToolAdapter(Protocol):
    def normalize(self, tool: Mapping[str, Any]) -> ToolDescriptor: ...


def infer_category(name: str, description: str = "") -> str:
    haystack = f"{name} {description}".lower()
    rules = (
        ("git", ("git", "commit", "branch", "diff")),
        ("browser", ("browser", "click", "navigate", "webpage", "url", "dom")),
        ("memory", ("memory", "recall", "remember")),
        ("network", ("http", "network", "request", "fetch url", "api call")),
        ("verify", ("test", "verify", "check", "lint", "build", "compile")),
        ("mutate", ("write", "edit", "patch", "create", "delete", "remove", "rename")),
        ("inspect", ("read", "search", "find", "list", "inspect", "grep", "lookup")),
        ("execute", ("shell", "exec", "execute", "command", "run")),
        ("finish", ("finish", "complete", "done", "finalize")),
    )
    for category, tokens in rules:
        if any(token in haystack for token in tokens):
            return category
    return "general"


def infer_risk(name: str, description: str = "") -> RiskLevel:
    haystack = f"{name} {description}".lower()
    critical = ("payment", "transfer funds", "wallet private", "delete repository", "drop database")
    high = ("delete", "remove", "reset --hard", "force push", "revoke", "terminate")
    medium = ("write", "edit", "patch", "create", "shell", "exec", "commit", "send")
    low = ("read", "search", "find", "list", "inspect", "diff", "status", "get")
    if any(token in haystack for token in critical):
        return RiskLevel.CRITICAL
    if any(token in haystack for token in high):
        return RiskLevel.HIGH
    if any(token in haystack for token in medium):
        return RiskLevel.MEDIUM
    if any(token in haystack for token in low):
        return RiskLevel.LOW
    return RiskLevel.MEDIUM


class GenericToolAdapter:
    def normalize(self, tool: Mapping[str, Any]) -> ToolDescriptor:
        name = _required_string(tool, "name")
        description = str(tool.get("description") or name)
        category = tool.get("category")
        if category is not None and not isinstance(category, str):
            raise ValueError("tool category must be a string")
        risk = _parse_risk(tool.get("risk")) or infer_risk(name, description)
        schema = tool.get("schema") or tool.get("input_schema") or tool.get("inputSchema") or {}
        if not isinstance(schema, Mapping):
            raise ValueError("tool schema must be a mapping")
        return ToolDescriptor(
            name=name,
            description=description,
            category=category or infer_category(name, description),
            risk=risk,
            schema=dict(schema),
        )


class MCPToolAdapter(GenericToolAdapter):
    """Normalize standard MCP tool definitions without depending on an MCP SDK."""


class UTCPToolAdapter(GenericToolAdapter):
    """Normalize UTCP-like tool definitions without depending on a UTCP SDK."""

    def normalize(self, tool: Mapping[str, Any]) -> ToolDescriptor:
        normalized = dict(tool)
        if "schema" not in normalized:
            for key in ("input_schema", "inputSchema", "inputs", "args"):
                value = normalized.get(key)
                if isinstance(value, Mapping):
                    normalized["schema"] = value
                    break
        return super().normalize(normalized)


def normalize_tools(
    tools: Iterable[Mapping[str, Any]],
    *,
    adapter: ToolAdapter | None = None,
) -> list[ToolDescriptor]:
    selected = adapter or GenericToolAdapter()
    return [selected.normalize(tool) for tool in tools]


def _required_string(tool: Mapping[str, Any], key: str) -> str:
    value = tool.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"tool {key} must be a non-empty string")
    return value.strip()


def _parse_risk(value: object) -> RiskLevel | None:
    if value is None:
        return None
    if isinstance(value, RiskLevel):
        return value
    if isinstance(value, str):
        try:
            return RiskLevel(value.lower())
        except ValueError as exc:
            raise ValueError(f"unknown risk level: {value}") from exc
    raise ValueError("tool risk must be a string or RiskLevel")
