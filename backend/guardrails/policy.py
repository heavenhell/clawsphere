from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from backend.mcp.tools import TOOL_REGISTRY
from backend.mock.repository import repo


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

        try:
            normalized = spec.input_model.model_validate(call.get("params", {})).model_dump(exclude_none=True)
            call = {**call, "params": normalized}
        except ValidationError as exc:
            violations.append(f"参数校验失败：{tool_name} / {exc.errors()[0]['msg']}")
            continue

        risk = risk_for_tool(tool_name)
        if risk in {"medium", "high"}:
            hitl_required = True

        params = call.get("params", {})
        resource_id = params.get("vm_id") or params.get("cluster_id") or "global"
        if params.get("vm_id") and not any(
            vm["id"] == params["vm_id"] or vm["name"] == params["vm_id"] for vm in repo.vms()
        ):
            violations.append(f"虚拟机不存在：{params['vm_id']}")
            continue
        if params.get("cluster_id") and not any(cluster["id"] == params["cluster_id"] for cluster in repo.clusters()):
            violations.append(f"集群不存在：{params['cluster_id']}")
            continue
        if tool_name == "scale_cluster":
            cluster = next(cluster for cluster in repo.clusters() if cluster["id"] == params["cluster_id"])
            if abs(params["target_hosts"] - cluster["host_count"]) > 5:
                violations.append("单次扩缩容影响超过 5 台主机的爆炸半径限制")
                continue

        key = (tool_name, resource_id)
        window = timedelta(days=1) if tool_name == "scale_cluster" else timedelta(hours=1)
        limit = 1 if tool_name == "scale_cluster" else 2 if tool_name == "restart_vm" else 20
        CALL_HISTORY[key] = [timestamp for timestamp in CALL_HISTORY[key] if now - timestamp < window]
        if len(CALL_HISTORY[key]) >= limit:
            violations.append(f"工具调用频率过高：{tool_name} / {resource_id}")
            continue
        approved_calls.append(call)

    return {
        "allowed": not violations,
        "hitl_required": hitl_required,
        "tool_calls": approved_calls,
        "violations": violations,
    }


def record_tool_execution(tool_name: str, params: dict[str, Any]) -> None:
    resource_id = params.get("vm_id") or params.get("cluster_id") or "global"
    CALL_HISTORY[(tool_name, resource_id)].append(datetime.now(timezone.utc))
