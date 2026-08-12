from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from backend.guardrails.permission import has_allowed_role
from backend.guardrails.approvals import approval_store
from backend.mcp.schemas import (
    AlarmDetailParams,
    AlarmListParams,
    ApprovalRequestParams,
    ClusterCapacityParams,
    EdmeAlarmParams,
    EdmeHistoryParams,
    EdmeMetricCatalogParams,
    EdmeResourceParams,
    EmptyParams,
    ForecastParams,
    ModifyHaPolicyParams,
    RestartVmParams,
    ScaleClusterParams,
    StoragePoolParams,
    ToolParams,
    ToolRequest,
    ToolResponse,
    VmDetailParams,
    VmListParams,
    VmMetricsParams,
)
from backend.providers import repo
from backend.memory.database import memory_db
from backend.observability import TOOL_CALLS, TOOL_LATENCY


# Bumped manually whenever a tool's presence/definition changes materially.
# Consumed by authorization_epoch() to detect a stale in-flight write across a
# HITL pause.
TOOL_CATALOG_VERSION = 1


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk: str
    auth_roles: list[str]
    input_model: type[ToolParams]
    fn: Callable[..., Any]
    category: str
    tags: list[str]


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def mcp_tool(
    name: str,
    description: str,
    input_model: type[ToolParams] = EmptyParams,
    risk: str = "none",
    auth_roles: list[str] | None = None,
    category: str = "general",
    tags: list[str] | None = None,
):
    def decorator(fn: Callable[..., Any]):
        TOOL_REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            risk=risk,
            auth_roles=auth_roles or ["readonly", "ops", "admin"],
            input_model=input_model,
            fn=fn,
            category=category,
            tags=tags or [],
        )
        return fn
    return decorator


@mcp_tool("list_alarms", "查询当前活动告警", AlarmListParams, category="alert", tags=["alarm", "fusioncompute"])
def list_alarms(severity: str | None = None):
    alarms = [alarm for alarm in repo.alarms() if alarm.get("status") == "active"]
    if severity:
        alarms = [a for a in alarms if a.get("severity") == severity]
    return alarms


@mcp_tool(
    "get_resource_overview",
    "查询 DCS/FusionCompute 资源总览",
    category="resource",
    tags=["overview", "aggregate", "fusioncompute"],
)
def get_resource_overview():
    return {
        "overview": repo.overview(),
        "sites": repo.sites(),
        "clusters": repo.clusters(),
        "hosts": repo.hosts(),
        "vms": repo.vms(),
        "datastores": repo.datastores(),
        "alarms": repo.alarms(),
    }


@mcp_tool(
    "get_alarm_detail",
    "查询告警详情和关联资源",
    AlarmDetailParams,
    category="alert",
    tags=["alarm", "detail", "fusioncompute"],
)
def get_alarm_detail(alarm_id: str):
    alarm = next((a for a in repo.alarms() if a["id"] == alarm_id), None)
    if not alarm:
        return None
    related = {}
    object_id = alarm.get("object_id")
    if alarm.get("object_type") == "datastore":
        related["datastore"] = next((d for d in repo.datastores() if d["id"] == object_id), None)
    if alarm.get("object_type") == "host":
        related["host"] = next((h for h in repo.hosts() if h["id"] == object_id), None)
    if alarm.get("object_type") == "vm":
        related["vm"] = next((vm for vm in repo.vms() if vm["id"] == object_id), None)
    related["clusters"] = repo.clusters()
    related["vms"] = repo.vms()
    return {"alarm": alarm, "related": related}


@mcp_tool("list_clusters", "查询集群列表和容量概览", category="resource", tags=["cluster", "fusioncompute"])
def list_clusters():
    return repo.clusters()


@mcp_tool(
    "get_cluster_capacity",
    "查询指定集群容量和风险",
    ClusterCapacityParams,
    category="capacity",
    tags=["cluster", "capacity", "fusioncompute"],
)
def get_cluster_capacity(cluster_id: str):
    cluster = next((c for c in repo.clusters() if c["id"] == cluster_id), None)
    if not cluster:
        return None
    datastores = [ds for ds in repo.datastores() if ds.get("cluster_id") == cluster_id]
    total = sum(ds.get("capacity_gb", 0) for ds in datastores)
    free = sum(ds.get("free_gb", 0) for ds in datastores)
    return {
        "cluster": cluster,
        "datastore_capacity_gb": total,
        "datastore_free_gb": free,
        "datastore_used_ratio": round(1 - free / total, 3) if total else 0,
        "risk_level": "high" if free / total < 0.15 else "medium" if free / total < 0.3 else "low",
    }


@mcp_tool("list_vms", "查询虚拟机列表", VmListParams, category="resource", tags=["vm", "fusioncompute"])
def list_vms(status: str | None = None):
    vms = repo.vms()
    if status:
        vms = [vm for vm in vms if vm.get("status") == status]
    return vms


@mcp_tool(
    "get_vm_detail",
    "查询虚拟机详情、主机和关联告警",
    VmDetailParams,
    category="resource",
    tags=["vm", "detail", "fusioncompute"],
)
def get_vm_detail(vm_id: str):
    vm = next((item for item in repo.vms() if item["id"] == vm_id or item["name"] == vm_id), None)
    if not vm:
        return None
    host = next((h for h in repo.hosts() if h["id"] == vm.get("host_id")), None)
    return {"vm": vm, "host": host, "alarms": repo.alarms()}


@mcp_tool(
    "get_vm_metrics",
    "查询虚拟机性能指标",
    VmMetricsParams,
    category="performance",
    tags=["vm", "metrics", "performance", "fusioncompute"],
)
def get_vm_metrics(vm_id: str, metric_names: list[str] | None = None, time_range: str = "1h"):
    detail = get_vm_detail(vm_id)
    if not detail:
        raise ValueError(f"虚拟机不存在：{vm_id}")
    vm = detail["vm"]
    base = repo.vm_metrics(vm["id"])
    if metric_names:
        base = [m for m in base if m["metric"] in metric_names]
    return {"vm": vm, "time_range": time_range, "series": base}


@mcp_tool(
    "run_capacity_forecast",
    "基于 mock 历史指标预测容量风险",
    ForecastParams,
    category="capacity",
    tags=["forecast", "capacity", "cluster"],
)
def run_capacity_forecast(cluster_id: str, forecast_days: int = 30):
    capacity = get_cluster_capacity(cluster_id)
    if not capacity:
        return None
    free = capacity["datastore_free_gb"]
    daily_growth_gb = repo.cluster_daily_growth_gb(cluster_id)
    days_to_exhaustion = max(0, int(free / daily_growth_gb)) if daily_growth_gb else 999
    return {
        "cluster_id": cluster_id,
        "forecast_days": forecast_days,
        "daily_growth_gb": daily_growth_gb,
        "days_to_exhaustion": days_to_exhaustion,
        "risk_level": "critical" if days_to_exhaustion <= 14 else "high" if days_to_exhaustion <= 30 else "medium",
        "recommendation": "建议优先扩容数据存储或清理低价值快照，并评估 VM 迁移窗口。",
    }


@mcp_tool(
    "get_storage_pool_usage",
    "查询 Dorado 存储池容量和时延",
    StoragePoolParams,
    category="resource",
    tags=["storage", "dorado"],
)
def get_storage_pool_usage(pool_id: str | None = None):
    return repo.storage_pool_usage(pool_id)


@mcp_tool(
    "query_edme_current_alarms",
    "查询 eDME 运维面当前告警",
    EdmeAlarmParams,
    category="alert",
    tags=["alarm", "edme"],
)
def query_edme_current_alarms(severity: int | None = None, iterator: str | None = None):
    return repo.edme_current_alarms(severity, iterator)


@mcp_tool(
    "query_edme_resources",
    "查询 eDME 系统资源实例",
    EdmeResourceParams,
    category="resource",
    tags=["edme", "resource"],
)
def query_edme_resources(class_name: str = "SYS_StorageDevice", page_no: int = 1, page_size: int = 20):
    return repo.edme_resource_instances(class_name, page_no, page_size)


@mcp_tool(
    "get_edme_metric_catalog",
    "查询 eDME 监控对象及性能指标目录",
    EdmeMetricCatalogParams,
    category="performance",
    tags=["edme", "metric", "catalog"],
)
def get_edme_metric_catalog(object_type_id: int | None = None):
    return {
        "object_types": repo.edme_object_types(),
        "indicators": repo.edme_indicators(object_type_id),
    }


@mcp_tool(
    "query_edme_performance_history",
    "查询 eDME 历史性能数据",
    EdmeHistoryParams,
    category="performance",
    tags=["edme", "metric", "history"],
)
def query_edme_performance_history(
    object_ids: list[str] | None = None,
    indicator_ids: list[int] | None = None,
    time_range: str = "LAST_1_HOUR",
):
    return {
        "time_range": time_range,
        "series": repo.edme_history(object_ids, indicator_ids, time_range),
        "indicators": repo.edme_indicators(),
    }


@mcp_tool(
    "create_approval_request",
    "创建高风险操作审批项",
    ApprovalRequestParams,
    risk="none",
    auth_roles=["ops", "admin"],
    category="admin",
    tags=["approval", "write"],
)
def create_approval_request(title: str, description: str, tool_calls: list[dict[str, Any]]):
    return {"title": title, "description": description, "tool_calls": tool_calls}


@mcp_tool(
    "restart_vm",
    "重启指定虚拟机",
    RestartVmParams,
    risk="high",
    auth_roles=["ops", "admin"],
    category="admin",
    tags=["vm", "write", "restart"],
)
def restart_vm(vm_id: str, reason: str, change_ticket_id: str):
    vm = next((item for item in repo.vms() if item["id"] == vm_id or item["name"] == vm_id), None)
    if not vm:
        raise ValueError("虚拟机不存在")
    result = {
        "task_id": f"mock-restart-{uuid4().hex[:8]}",
        "action": "restart_vm",
        "resource_id": vm["id"],
        "resource_name": vm["name"],
        "change_ticket_id": change_ticket_id,
        "reason": reason,
        "status": "completed",
    }
    return result


@mcp_tool(
    "scale_cluster",
    "调整集群目标主机数",
    ScaleClusterParams,
    risk="high",
    auth_roles=["admin"],
    category="admin",
    tags=["cluster", "write", "scale"],
)
def scale_cluster(cluster_id: str, target_hosts: int, reason: str):
    cluster = next((item for item in repo.clusters() if item["id"] == cluster_id), None)
    if not cluster:
        raise ValueError("集群不存在")
    result = {
        "task_id": f"mock-scale-{uuid4().hex[:8]}",
        "action": "scale_cluster",
        "resource_id": cluster_id,
        "previous_hosts": cluster["host_count"],
        "target_hosts": target_hosts,
        "reason": reason,
        "status": "completed",
    }
    return result


@mcp_tool(
    "modify_ha_policy",
    "修改集群 HA 策略",
    ModifyHaPolicyParams,
    risk="high",
    auth_roles=["admin"],
    category="admin",
    tags=["cluster", "write", "ha"],
)
def modify_ha_policy(cluster_id: str, policy: dict[str, Any], reason: str):
    if not any(item["id"] == cluster_id for item in repo.clusters()):
        raise ValueError("集群不存在")
    result = {
        "task_id": f"mock-ha-{uuid4().hex[:8]}",
        "action": "modify_ha_policy",
        "resource_id": cluster_id,
        "policy": policy,
        "reason": reason,
        "status": "completed",
    }
    return result


def call_tool(request: ToolRequest) -> ToolResponse:
    started = perf_counter()
    audit_id = str(uuid4())
    spec = TOOL_REGISTRY.get(request.tool_name)

    def response(
        success: bool,
        *,
        data: Any | None = None,
        error_code: str | None = None,
        error_msg: str | None = None,
    ) -> ToolResponse:
        return ToolResponse(
            tool_name=request.tool_name,
            success=success,
            data=data,
            error_code=error_code,
            error_msg=error_msg,
            execution_time_ms=int((perf_counter() - started) * 1000),
            audit_id=audit_id,
        )

    if not spec:
        tool_response = response(False, error_code="TOOL_NOT_FOUND", error_msg="工具不存在")
        risk_level = "unknown"
    elif not has_allowed_role(request.caller_roles, spec.auth_roles):
        tool_response = response(False, error_code="PERMISSION_DENIED", error_msg="当前角色无权调用该工具")
        risk_level = spec.risk
    else:
        risk_level = spec.risk
        try:
            params = spec.input_model.model_validate(request.params).model_dump(exclude_none=True)
            if spec.risk in {"medium", "high"}:
                approval = approval_store.get_by_task(request.task_id)
                matching_call = approval and any(
                    call.get("tool_name") == request.tool_name and call.get("params") == params
                    for call in approval.get("tool_calls", [])
                )
                if not (
                    approval
                    and approval["status"] == "approved"
                    and approval["tenant_id"] == request.tenant_id
                    and matching_call
                ):
                    tool_response = response(
                        False,
                        error_code="APPROVAL_REQUIRED",
                        error_msg="高风险工具必须匹配已批准的工具和参数",
                    )
                else:
                    tool_response = response(True, data=spec.fn(**params))
            elif request.tool_name == "create_approval_request":
                from backend.guardrails.policy import validate_tool_calls

                validation = validate_tool_calls(
                    params["tool_calls"],
                    request.caller_roles,
                    params["description"],
                    request.task_id,
                )
                if not validation["allowed"]:
                    raise ValueError("；".join(validation["violations"]))
                if not validation["hitl_required"]:
                    raise ValueError("审批单必须至少包含一个 medium 或 high 风险工具")
                risk_order = {"none": 0, "low": 1, "medium": 2, "high": 3}
                approval_risk = max(
                    (TOOL_REGISTRY[call["tool_name"]].risk for call in validation["tool_calls"]),
                    key=lambda risk: risk_order.get(risk, 0),
                )
                data = approval_store.create_or_get(
                    request.task_id,
                    request.task_id,
                    request.caller_user_id,
                    request.tenant_id,
                    params["description"],
                    validation["tool_calls"],
                    approval_risk,
                    resume_required=False,
                )
                tool_response = response(True, data=data)
            else:
                tool_response = response(True, data=spec.fn(**params))
        except (TypeError, ValidationError) as exc:
            tool_response = response(False, error_code="SCHEMA_VALIDATION_FAILED", error_msg=str(exc))
        except ValueError as exc:
            tool_response = response(False, error_code="BUSINESS_VALIDATION_FAILED", error_msg=str(exc))

    audit_record = {
        "audit_id": audit_id,
        "task_id": request.task_id,
        "user_id": request.caller_user_id,
        "tenant_id": request.tenant_id,
        "tool_name": request.tool_name,
        "params": request.params,
        "roles": request.caller_roles,
        "success": tool_response.success,
        "error_code": tool_response.error_code,
        "risk_level": risk_level,
        "duration_ms": tool_response.execution_time_ms,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    memory_db.append_tool_audit(audit_record)
    if tool_response.success and request.tool_name in {"restart_vm", "scale_cluster", "modify_ha_policy"}:
        payload = tool_response.data if isinstance(tool_response.data, dict) else {"result": tool_response.data}
        resource_id = request.params.get("vm_id") or request.params.get("cluster_id") or "unknown"
        memory_db.append_mock_change(request.task_id, request.tool_name, resource_id, payload)
        memory_db.mark_tool_rate_executed(request.task_id, request.tool_name, resource_id)
        approval_store.mark_executed(request.task_id)
    TOOL_CALLS.labels(request.tool_name, str(tool_response.success).lower()).inc()
    TOOL_LATENCY.labels(request.tool_name).observe(tool_response.execution_time_ms / 1000)
    return tool_response
