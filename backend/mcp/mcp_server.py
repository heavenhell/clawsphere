from __future__ import annotations

from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import call_tool
from backend.mcp.auth import get_mcp_auth_context
from backend.providers import runtime_config


mcp = FastMCP(
    "ClawSphere DCS Operations",
    instructions="FusionCompute, Dorado and eDME operations tools with RBAC and audit logging.",
    host=runtime_config.mcp.host,
    port=runtime_config.mcp.port,
    streamable_http_path="/mcp",
)


def _call(name: str, arguments: dict[str, Any], task_id: str | None = None) -> dict[str, Any]:
    auth = get_mcp_auth_context()
    response = call_tool(ToolRequest(
        tool_name=name,
        params=arguments,
        caller_user_id=auth.user_id,
        caller_roles=auth.roles,
        tenant_id=auth.tenant_id,
        task_id=task_id or str(uuid4()),
    ))
    return response.model_dump(mode="json")


@mcp.tool()
def list_alarms(severity: str | None = None) -> dict[str, Any]:
    """List active FusionCompute alarms, optionally filtered by severity."""
    return _call("list_alarms", {"severity": severity})


@mcp.tool()
def get_alarm_detail(alarm_id: str) -> dict[str, Any]:
    """Get an alarm and its related resource context."""
    return _call("get_alarm_detail", {"alarm_id": alarm_id})


@mcp.tool()
def get_resource_overview() -> dict[str, Any]:
    """Get sites, clusters, hosts, VMs, datastores and alarm counts."""
    return _call("get_resource_overview", {})


@mcp.tool()
def list_vms(status: str | None = None) -> dict[str, Any]:
    """List FusionCompute virtual machines."""
    return _call("list_vms", {"status": status})


@mcp.tool()
def get_vm_metrics(vm_id: str, metric_names: list[str] | None = None, time_range: str = "1h") -> dict[str, Any]:
    """Read CPU, memory, disk and network metrics for a VM."""
    return _call("get_vm_metrics", {"vm_id": vm_id, "metric_names": metric_names, "time_range": time_range})


@mcp.tool()
def get_cluster_capacity(cluster_id: str) -> dict[str, Any]:
    """Read current capacity and risk for a cluster."""
    return _call("get_cluster_capacity", {"cluster_id": cluster_id})


@mcp.tool()
def run_capacity_forecast(cluster_id: str, forecast_days: int = 30) -> dict[str, Any]:
    """Forecast capacity exhaustion from mock historical growth."""
    return _call("run_capacity_forecast", {"cluster_id": cluster_id, "forecast_days": forecast_days})


@mcp.tool()
def get_storage_pool_usage(pool_id: str | None = None) -> dict[str, Any]:
    """Read Dorado storage pool capacity and latency."""
    return _call("get_storage_pool_usage", {"pool_id": pool_id})


@mcp.tool()
def query_edme_current_alarms(severity: int | None = None, iterator: str | None = None) -> dict[str, Any]:
    """Query current alarms from the eDME operations-plane model."""
    return _call("query_edme_current_alarms", {"severity": severity, "iterator": iterator})


@mcp.tool()
def query_edme_resources(
    class_name: str = "SYS_StorageDevice",
    page_no: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """Query eDME resource instances by system resource class."""
    return _call("query_edme_resources", {
        "class_name": class_name,
        "page_no": page_no,
        "page_size": page_size,
    })


@mcp.tool()
def get_edme_metric_catalog(object_type_id: int | None = None) -> dict[str, Any]:
    """List eDME monitoring object types and supported indicators."""
    return _call("get_edme_metric_catalog", {"object_type_id": object_type_id})


@mcp.tool()
def query_edme_performance_history(
    object_ids: list[str] | None = None,
    indicator_ids: list[int] | None = None,
    time_range: str = "LAST_1_HOUR",
) -> dict[str, Any]:
    """Query eDME historical performance data for resources and indicators."""
    return _call("query_edme_performance_history", {
        "object_ids": object_ids,
        "indicator_ids": indicator_ids,
        "time_range": time_range,
    })


@mcp.tool()
def create_approval_request(
    task_id: str,
    title: str,
    description: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create an external approval ticket bound to exact write tools and parameters."""
    return _call(
        "create_approval_request",
        {"title": title, "description": description, "tool_calls": tool_calls},
        task_id,
    )


@mcp.tool()
def restart_vm(task_id: str, vm_id: str, reason: str, change_ticket_id: str) -> dict[str, Any]:
    """Restart a VM using the task_id of a previously approved matching request."""
    return _call("restart_vm", {"vm_id": vm_id, "reason": reason, "change_ticket_id": change_ticket_id}, task_id)


@mcp.tool()
def scale_cluster(task_id: str, cluster_id: str, target_hosts: int, reason: str) -> dict[str, Any]:
    """Change cluster host count using the task_id of an approved matching request."""
    return _call("scale_cluster", {"cluster_id": cluster_id, "target_hosts": target_hosts, "reason": reason}, task_id)


@mcp.tool()
def modify_ha_policy(task_id: str, cluster_id: str, policy: dict[str, Any], reason: str) -> dict[str, Any]:
    """Modify HA policy using the task_id of an approved matching request."""
    return _call("modify_ha_policy", {"cluster_id": cluster_id, "policy": policy, "reason": reason}, task_id)


def main() -> None:
    mcp.run(transport=runtime_config.mcp.transport)


if __name__ == "__main__":
    main()
