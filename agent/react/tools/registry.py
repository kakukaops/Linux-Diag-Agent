"""Build the global ToolRegistry for the M22 ReAct Investigation phase.

Call build_registry() once at agent startup; pass the returned registry
to run_react_loop(). All tools are imported lazily (inside each fn) so
startup does not require DB / CodeGraph connectivity.
"""

from __future__ import annotations

from agent.react.tool_registry import ToolRegistry
from agent.react.tools.retrieval_tools import ALL_RETRIEVAL_TOOLS
from agent.react.tools.log_tools import ALL_LOG_TOOLS
from agent.react.tools.code_tools import ALL_CODE_TOOLS
from agent.react.tools.hardware_tools import ALL_HARDWARE_TOOLS
from agent.react.tools.vmcore_tools import ALL_VMCORE_TOOLS


def build_registry() -> ToolRegistry:
    """Return a ToolRegistry populated with all Category A / B / D / F / H tools."""
    reg = ToolRegistry()
    for tool in (ALL_RETRIEVAL_TOOLS + ALL_LOG_TOOLS + ALL_CODE_TOOLS
                 + ALL_HARDWARE_TOOLS + ALL_VMCORE_TOOLS):
        reg.register(tool)
    return reg
