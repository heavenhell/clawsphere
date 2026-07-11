from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import call_tool


mcp = FastMCP(
    "ClawSphere DCS Operations",
    instructions="FusionCompute and Dorado operations tools with RBAC and audit logging.",
)


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = call_tool(ToolRequest(tool_name=name, params=arguments))
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


def main() -> None:
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
