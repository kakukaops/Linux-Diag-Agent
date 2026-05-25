"""M22 ReAct — tool registry with per-route subsetting (spike skeleton).

Spike scope: just enough registry to prove the loop. Full module-import
auto-registration and the MCP_HTTP transport are M22 proper — see the M22
design doc §3 and V2_v1_modules_supplements.md §3.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from llm.provider.base import ToolFunction, ToolSchema


@dataclass
class Tool:
    """A single callable tool exposed to the ReAct LLM."""

    name: str
    description: str
    parameters: dict[str, Any]          # JSON Schema for the arguments object
    fn: Callable[..., Any]
    routes: frozenset[str] = field(default_factory=frozenset)  # empty → all routes


class ToolRegistry:
    """In-process tool registry. Subsets the toolset by triage route (ADR-023)."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def for_route(self, route: str) -> list[Tool]:
        """Tools exposed on `route` — a tool with no `routes` is exposed on all."""
        return [t for t in self._tools.values()
                if not t.routes or route in t.routes]

    def schemas(self, route: str) -> list[ToolSchema]:
        """OpenAI-format tool schemas for the route's toolset (sent to the LLM)."""
        return [
            ToolSchema(function=ToolFunction(
                name=t.name, description=t.description, parameters=t.parameters))
            for t in self.for_route(route)
        ]

    def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        """Invoke a tool by name. Raises KeyError if unknown (caller feeds back)."""
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name].fn(**args)
