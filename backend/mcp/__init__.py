# Lazy imports to avoid circular dependency with guardrails

import importlib
from typing import Any

__all__ = [
    "DEMO_MODE",
    "AuthContext",
    "GatewayToolRequest",
    "TOOL_REGISTRY",
    "ToolRequest",
    "ToolSpec",
    "call_tool",
    "get_auth_context",
    "issue_demo_token",
    "main",
    "mcp",
]


def __getattr__(name: str) -> Any:
    if name == "DEMO_MODE":
        from backend.mcp.auth import DEMO_MODE; return DEMO_MODE
    if name == "AuthContext":
        from backend.mcp.auth import AuthContext; return AuthContext
    if name == "get_auth_context":
        from backend.mcp.auth import get_auth_context; return get_auth_context
    if name == "issue_demo_token":
        from backend.mcp.auth import issue_demo_token; return issue_demo_token
    if name == "main":
        from backend.mcp.mcp_server import main; return main
    if name == "mcp":
        from backend.mcp.mcp_server import mcp; return mcp
    if name == "GatewayToolRequest":
        from backend.mcp.schemas import GatewayToolRequest; return GatewayToolRequest
    if name == "ToolRequest":
        from backend.mcp.schemas import ToolRequest; return ToolRequest
    if name == "ToolSpec":
        from backend.mcp.schemas import ToolSpec; return ToolSpec
    if name == "TOOL_REGISTRY":
        from backend.mcp.tools import TOOL_REGISTRY; return TOOL_REGISTRY
    if name == "call_tool":
        from backend.mcp.tools import call_tool; return call_tool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
