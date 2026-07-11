from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from backend.mcp.tools import TOOL_REGISTRY
from backend.memory.database import memory_db
from backend.mock.repository import repo


WRITE_HINTS = ["重启", "停止", "删除", "迁移", "扩容", "修改", "启用", "禁用", "执行", "处理掉"]
READ_ONLY_HINTS = ["时间", "记录", "历史", "状态", "是否", "谁", "什么", "查看", "查询"]
COMMAND_HINTS = ["立即", "直接执行", "现在执行", "执行变更", "处理掉", "改为", "设置为"]
ADVISORY_HINTS = ["是否需要", "需不需要", "要不要", "该不该", "建议", "评估", "需要扩容吗", "扩容吗"]


def detect_write_intent(message: str) -> bool:
    """Early intent hint only; authorization is always decided from ToolSpec."""
    if not any(word in message for word in WRITE_HINTS):
        return False
    if any(word in message for word in ADVISORY_HINTS) and not any(word in message for word in COMMAND_HINTS):
        return False
    if any(word in message for word in READ_ONLY_HINTS) and not any(word in message for word in COMMAND_HINTS):
        return False
    return True


def risk_for_tool(tool_name: str) -> str:
    spec = TOOL_REGISTRY.get(tool_name)
    return spec.risk if spec else "unknown"


def _rate_policy(tool_name: str) -> tuple[timedelta, int]:
    if tool_name == "scale_cluster":
        return timedelta(days=1), 1
    if tool_name == "restart_vm":
        return timedelta(hours=1), 2
    return timedelta(hours=1), 20


def validate_tool_calls(
    tool_calls: list[dict[str, Any]],
    roles: list[str],
    message: str = "",
    task_id: str | None = None,
) -> dict[str, Any]:
    violations = []
    approved_calls = []
    hitl_required = False

    for proposed in tool_calls:
        tool_name = proposed["tool_name"]
        spec = TOOL_REGISTRY.get(tool_name)
        if not spec:
            violations.append(f"工具不存在：{tool_name}")
            continue
        if not any(role in spec.auth_roles for role in roles):
            violations.append(f"{'/'.join(roles)} 角色无权调用工具：{tool_name}")
            continue

        try:
            normalized = spec.input_model.model_validate(proposed.get("params", {})).model_dump(exclude_none=True)
            call = {**proposed, "params": normalized}
        except ValidationError as exc:
            violations.append(f"参数校验失败：{tool_name} / {exc.errors()[0]['msg']}")
            continue

        risk = spec.risk
        if risk in {"medium", "high"}:
            hitl_required = True

        params = call["params"]
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

        if risk in {"medium", "high"} and task_id:
            window, limit = _rate_policy(tool_name)
            since = (datetime.now(timezone.utc) - window).isoformat()
            if not memory_db.reserve_tool_rate_slot(task_id, tool_name, resource_id, since, limit):
                violations.append(f"工具调用频率过高：{tool_name} / {resource_id}")
                continue
        approved_calls.append(call)

    return {
        "allowed": not violations,
        "hitl_required": hitl_required,
        "tool_calls": approved_calls,
        "violations": violations,
    }
