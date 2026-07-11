from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.mcp.tools import TOOL_REGISTRY


HIGH_RISK_TOOLS = {"restart_vm", "scale_cluster", "modify_ha_policy"}
MEDIUM_RISK_TOOLS = {"create_approval_request"}
WRITE_HINTS = ["重启", "停止", "删除", "迁移", "扩容", "修改", "启用", "禁用", "执行", "处理掉"]
CALL_HISTORY: dict[tuple[str, str], list[datetime]] = defaultdict(list)


def detect_write_intent(message: str) -> bool:
    return any(word in message for word in WRITE_HINTS)


def risk_for_tool(tool_name: str) -> str:
    if tool_name in HIGH_RISK_TOOLS:
        return "high"
    if tool_name in MEDIUM_RISK_TOOLS:
        return "medium"
    spec = TOOL_REGISTRY.get(tool_name)
    return spec.risk if spec else "unknown"


def validate_tool_calls(tool_calls: list[dict[str, Any]], roles: list[str], message: str) -> dict[str, Any]:
    violations = []
    approved_calls = []
    hitl_required = False

    if detect_write_intent(message) and not any(role in roles for role in ["ops", "admin"]):
        return {
            "allowed": False,
            "hitl_required": False,
            "tool_calls": [],
            "violations": ["readonly 角色不能执行或发起写操作"],
        }

    now = datetime.now(timezone.utc)
    for call in tool_calls:
        tool_name = call["tool_name"]
        spec = TOOL_REGISTRY.get(tool_name)
        if not spec:
            violations.append(f"工具不存在：{tool_name}")
            continue
        if not any(role in spec.auth_roles for role in roles):
            violations.append(f"角色无权调用工具：{tool_name}")
            continue

        risk = risk_for_tool(tool_name)
        if risk in {"medium", "high"}:
            hitl_required = True

        key = ("demo-user", tool_name)
        CALL_HISTORY[key] = [t for t in CALL_HISTORY[key] if now - t < timedelta(minutes=60)]
        if len(CALL_HISTORY[key]) >= 20:
            violations.append(f"工具调用频率过高：{tool_name}")
            continue
        CALL_HISTORY[key].append(now)
        approved_calls.append(call)

    return {
        "allowed": not violations,
        "hitl_required": hitl_required,
        "tool_calls": approved_calls,
        "violations": violations,
    }
