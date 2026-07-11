from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from backend.guardrails.permission import has_allowed_role
from backend.mcp.schemas import (
    AlarmDetailParams,
    AlarmListParams,
    ApprovalRequestParams,
    ClusterCapacityParams,
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
from backend.mock.repository import repo
from backend.memory.database import memory_db


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk: str
    auth_roles: list[str]
    input_model: type[ToolParams]
    fn: Callable[..., Any]


TOOL_REGISTRY: dict[str, ToolSpec] = {}
AUDIT_LOG: list[dict[str, Any]] = []
APPROVAL_QUEUE: list[dict[str, Any]] = []
MOCK_CHANGE_LOG: list[dict[str, Any]] = []


def mcp_tool(
    name: str,
    description: str,
    input_model: type[ToolParams] = EmptyParams,
    risk: str = "none",
    auth_roles: list[str] | None = None,
):
    def decorator(fn: Callable[..., Any]):
        TOOL_REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            risk=risk,
            auth_roles=auth_roles or ["readonly", "ops", "admin"],
            input_model=input_model,
            fn=fn,
        )
        return fn
    return decorator


@mcp_tool("list_alarms", "查询当前活动告警", AlarmListParams)
def list_alarms(severity: str | None = None):
    alarms = [alarm for alarm in repo.alarms() if alarm.get("status") == "active"]
    if severity:
        alarms = [a for a in alarms if a.get("severity") == severity]
    return alarms


@mcp_tool("get_resource_overview", "查询 DCS/FusionCompute 资源总览")
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


@mcp_tool("get_alarm_detail", "查询告警详情和关联资源", AlarmDetailParams)
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


@mcp_tool("list_clusters", "查询集群列表和容量概览")
def list_clusters():
    return repo.clusters()


@mcp_tool("get_cluster_capacity", "查询指定集群容量和风险", ClusterCapacityParams)
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


@mcp_tool("list_vms", "查询虚拟机列表", VmListParams)
def list_vms(status: str | None = None):
    vms = repo.vms()
    if status:
        vms = [vm for vm in vms if vm.get("status") == status]
    return vms


@mcp_tool("get_vm_detail", "查询虚拟机详情、主机和关联告警", VmDetailParams)
def get_vm_detail(vm_id: str):
    vm = next((item for item in repo.vms() if item["id"] == vm_id or item["name"] == vm_id), None)
    if not vm:
        return None
    host = next((h for h in repo.hosts() if h["id"] == vm.get("host_id")), None)
    return {"vm": vm, "host": host, "alarms": repo.alarms()}


@mcp_tool("get_vm_metrics", "查询虚拟机性能指标", VmMetricsParams)
def get_vm_metrics(vm_id: str, metric_names: list[str] | None = None, time_range: str = "1h"):
    detail = get_vm_detail(vm_id)
    vm = detail["vm"] if detail else {"id": vm_id, "name": vm_id}
    cpu_usage = 86 if vm.get("name") == "dcs-app-01" else 42
    cpu_ready = 6.8 if vm.get("name") == "dcs-app-01" else 1.1
    balloon = 640 if vm.get("name") == "dcs-cache-01" else 0
    disk_latency = 24 if vm.get("host_id") == "host-005" else 9
    base = [
        {"metric": "cpu.usage", "value": cpu_usage, "unit": "%", "status": "warning" if cpu_usage > 80 else "normal"},
        {"metric": "cpu.ready", "value": cpu_ready, "unit": "%", "status": "warning" if cpu_ready > 5 else "normal"},
        {"metric": "mem.usage", "value": 72, "unit": "%", "status": "normal"},
        {"metric": "mem.balloon", "value": balloon, "unit": "MB", "status": "warning" if balloon else "normal"},
        {"metric": "disk.latency", "value": disk_latency, "unit": "ms", "status": "warning" if disk_latency > 20 else "normal"},
        {"metric": "net.drop", "value": 0.2, "unit": "%", "status": "normal"},
    ]
    if metric_names:
        base = [m for m in base if m["metric"] in metric_names]
    return {"vm": vm, "time_range": time_range, "series": base}


@mcp_tool("run_capacity_forecast", "基于 mock 历史指标预测容量风险", ForecastParams)
def run_capacity_forecast(cluster_id: str, forecast_days: int = 30):
    capacity = get_cluster_capacity(cluster_id)
    if not capacity:
        return None
    free = capacity["datastore_free_gb"]
    daily_growth_gb = 95 if cluster_id == "cluster-002" else 42
    days_to_exhaustion = max(0, int(free / daily_growth_gb)) if daily_growth_gb else 999
    return {
        "cluster_id": cluster_id,
        "forecast_days": forecast_days,
        "daily_growth_gb": daily_growth_gb,
        "days_to_exhaustion": days_to_exhaustion,
        "risk_level": "critical" if days_to_exhaustion <= 14 else "high" if days_to_exhaustion <= 30 else "medium",
        "recommendation": "建议优先扩容数据存储或清理低价值快照，并评估 VM 迁移窗口。",
    }


@mcp_tool("get_storage_pool_usage", "查询 Dorado 存储池容量和时延", StoragePoolParams)
def get_storage_pool_usage(pool_id: str | None = None):
    return repo.storage_pool_usage(pool_id)


@mcp_tool(
    "create_approval_request",
    "创建高风险操作审批项",
    ApprovalRequestParams,
    risk="medium",
    auth_roles=["ops", "admin"],
)
def create_approval_request(title: str, description: str, risk: str = "high"):
    item = {
        "id": f"approval-{len(APPROVAL_QUEUE) + 1:04d}",
        "title": title,
        "description": description,
        "risk": risk,
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    APPROVAL_QUEUE.append(item)
    return item


@mcp_tool("restart_vm", "重启指定虚拟机", RestartVmParams, risk="high", auth_roles=["ops", "admin"])
def restart_vm(vm_id: str, reason: str, change_ticket_id: str):
    vm = next((item for item in repo.vms() if item["id"] == vm_id or item["name"] == vm_id), None)
    if not vm:
        raise ValueError("虚拟机不存在")
    result = {
        "task_id": f"mock-restart-{len(MOCK_CHANGE_LOG) + 1:04d}",
        "action": "restart_vm",
        "resource_id": vm["id"],
        "resource_name": vm["name"],
        "change_ticket_id": change_ticket_id,
        "reason": reason,
        "status": "completed",
    }
    MOCK_CHANGE_LOG.append(result)
    return result


@mcp_tool("scale_cluster", "调整集群目标主机数", ScaleClusterParams, risk="high", auth_roles=["admin"])
def scale_cluster(cluster_id: str, target_hosts: int, reason: str):
    cluster = next((item for item in repo.clusters() if item["id"] == cluster_id), None)
    if not cluster:
        raise ValueError("集群不存在")
    result = {
        "task_id": f"mock-scale-{len(MOCK_CHANGE_LOG) + 1:04d}",
        "action": "scale_cluster",
        "resource_id": cluster_id,
        "previous_hosts": cluster["host_count"],
        "target_hosts": target_hosts,
        "reason": reason,
        "status": "completed",
    }
    MOCK_CHANGE_LOG.append(result)
    return result


@mcp_tool("modify_ha_policy", "修改集群 HA 策略", ModifyHaPolicyParams, risk="high", auth_roles=["admin"])
def modify_ha_policy(cluster_id: str, policy: dict[str, Any], reason: str):
    if not any(item["id"] == cluster_id for item in repo.clusters()):
        raise ValueError("集群不存在")
    result = {
        "task_id": f"mock-ha-{len(MOCK_CHANGE_LOG) + 1:04d}",
        "action": "modify_ha_policy",
        "resource_id": cluster_id,
        "policy": policy,
        "reason": reason,
        "status": "completed",
    }
    MOCK_CHANGE_LOG.append(result)
    return result


def call_tool(request: ToolRequest) -> ToolResponse:
    started = perf_counter()
    audit_id = str(uuid4())
    spec = TOOL_REGISTRY.get(request.tool_name)
    if not spec:
        return ToolResponse(
            tool_name=request.tool_name,
            success=False,
            error_code="TOOL_NOT_FOUND",
            error_msg="工具不存在",
            execution_time_ms=0,
            audit_id=audit_id,
        )
    if not has_allowed_role(request.caller_roles, spec.auth_roles):
        response = ToolResponse(
            tool_name=request.tool_name,
            success=False,
            error_code="PERMISSION_DENIED",
            error_msg="当前角色无权调用该工具",
            execution_time_ms=int((perf_counter() - started) * 1000),
            audit_id=audit_id,
        )
    else:
        try:
            params = spec.input_model.model_validate(request.params).model_dump(exclude_none=True)
            data = spec.fn(**params)
            response = ToolResponse(
                tool_name=request.tool_name,
                success=True,
                data=data,
                execution_time_ms=int((perf_counter() - started) * 1000),
                audit_id=audit_id,
            )
        except (TypeError, ValidationError) as exc:
            response = ToolResponse(
                tool_name=request.tool_name,
                success=False,
                error_code="SCHEMA_VALIDATION_FAILED",
                error_msg=str(exc),
                execution_time_ms=int((perf_counter() - started) * 1000),
                audit_id=audit_id,
            )
        except ValueError as exc:
            response = ToolResponse(
                tool_name=request.tool_name,
                success=False,
                error_code="BUSINESS_VALIDATION_FAILED",
                error_msg=str(exc),
                execution_time_ms=int((perf_counter() - started) * 1000),
                audit_id=audit_id,
            )
    audit_record = {
            "audit_id": audit_id,
            "task_id": request.task_id,
            "user_id": request.caller_user_id,
            "tenant_id": request.tenant_id,
            "tool_name": request.tool_name,
            "params": request.params,
            "roles": request.caller_roles,
            "success": response.success,
            "error_code": response.error_code,
            "risk_level": spec.risk,
            "duration_ms": response.execution_time_ms,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    AUDIT_LOG.append(audit_record)
    memory_db.append_tool_audit(audit_record)
    return response
