"""M22 ReAct Loop Engine (v2).

Spike prototype — proves a client-side ReAct loop on the project's own LLM
provider stack. See docs/v2/M22_spike_findings.md for the spike verdict.
"""

from agent.react.loop import ReactResult, run_react_loop
from agent.react.tool_registry import Tool, ToolRegistry
from agent.react.tools import build_registry

__all__ = ["run_react_loop", "ReactResult", "ToolRegistry", "Tool", "build_registry"]
