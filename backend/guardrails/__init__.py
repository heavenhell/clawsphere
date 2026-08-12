# Lazy imports to avoid circular dependency with memory and mcp

import importlib
from typing import Any

__all__ = [
    "ApprovalStore",
    "ChatLimiter",
    "approval_store",
    "chat_limiter",
    "detect_write_intent",
    "has_allowed_role",
    "permissions_for_roles",
    "risk_for_tool",
    "validate_tool_calls",
]


def __getattr__(name: str) -> Any:
    if name == "ApprovalStore":
        from backend.guardrails.approvals import ApprovalStore; return ApprovalStore
    if name == "approval_store":
        from backend.guardrails.approvals import approval_store; return approval_store
    if name == "ChatLimiter":
        from backend.guardrails.chat_limits import ChatLimiter; return ChatLimiter
    if name == "chat_limiter":
        from backend.guardrails.chat_limits import chat_limiter; return chat_limiter
    if name == "has_allowed_role":
        from backend.guardrails.permission import has_allowed_role; return has_allowed_role
    if name == "permissions_for_roles":
        from backend.guardrails.permission import permissions_for_roles; return permissions_for_roles
    if name == "detect_write_intent":
        from backend.guardrails.policy import detect_write_intent; return detect_write_intent
    if name == "risk_for_tool":
        from backend.guardrails.policy import risk_for_tool; return risk_for_tool
    if name == "validate_tool_calls":
        from backend.guardrails.policy import validate_tool_calls; return validate_tool_calls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
