from __future__ import annotations

from typing import Any
from uuid import uuid4

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.lowlevel.server import NotificationOptions

from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import TOOL_METADATA_KEY, TOOL_REGISTRY, call_tool
from backend.mcp.auth import MCP_CALLER_TOKEN_META_KEY, decode_mcp_caller_token, get_mcp_auth_context
from backend.providers import runtime_config


mcp = FastMCP(
    "ClawSphere DCS Operations",
    instructions="FusionCompute, Dorado and eDME operations tools with RBAC and audit logging.",
    host=runtime_config.mcp.host,
    port=runtime_config.mcp.port,
    streamable_http_path="/mcp",
)

CALLER_TOKEN_META_KEY = MCP_CALLER_TOKEN_META_KEY
TOOL_META_KEY = TOOL_METADATA_KEY


# FastMCP 1.x exposes list_changed notifications but does not currently offer a
# public high-level switch for advertising the capability. Keep the override in
# this adapter so clients can correctly negotiate tools.listChanged=true.
_default_initialization_options = mcp._mcp_server.create_initialization_options


def _initialization_options(
    notification_options: NotificationOptions | None = None,
    experimental_capabilities: dict[str, dict[str, Any]] | None = None,
):
    options = notification_options or NotificationOptions()
    options.tools_changed = True
    return _default_initialization_options(options, experimental_capabilities)


mcp._mcp_server.create_initialization_options = _initialization_options


def _auth_from_context(ctx: Context) -> tuple[Any, str]:
    meta = ctx.request_context.meta
    payload = meta.model_dump(by_alias=True) if meta else {}
    token = payload.get(CALLER_TOKEN_META_KEY)
    if not isinstance(token, str) or not token:
        raise PermissionError("MCP tools/call 请求缺少调用者令牌")
    return decode_mcp_caller_token(token)


def _call(
    name: str,
    arguments: dict[str, Any],
    task_id: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    if ctx is None:
        # Backward-compatible direct Python entry point used by local tests and
        # administration scripts. Every network-exposed wrapper passes Context
        # and therefore requires the per-request signed caller token.
        auth = get_mcp_auth_context()
        effective_task_id = task_id or str(uuid4())
    else:
        auth, effective_task_id = _auth_from_context(ctx)
        if task_id is not None and task_id != effective_task_id:
            raise PermissionError("MCP task_id 与调用者令牌不匹配")
    response = call_tool(ToolRequest(
        tool_name=name,
        params=arguments,
        caller_user_id=auth.user_id,
        caller_roles=auth.roles,
        tenant_id=auth.tenant_id,
        task_id=effective_task_id,
    ))
    return response.model_dump(mode="json")


@mcp.tool()
def list_alarms(severity: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """List active FusionCompute alarms, optionally filtered by severity."""
    return _call("list_alarms", {"severity": severity}, ctx=ctx)


@mcp.tool()
def get_alarm_detail(alarm_id: str, ctx: Context) -> dict[str, Any]:
    """Get an alarm and its related resource context."""
    return _call("get_alarm_detail", {"alarm_id": alarm_id}, ctx=ctx)


@mcp.tool()
def get_resource_overview(ctx: Context) -> dict[str, Any]:
    """Get sites, clusters, hosts, VMs, datastores and alarm counts."""
    return _call("get_resource_overview", {}, ctx=ctx)


@mcp.tool()
def list_clusters(ctx: Context) -> dict[str, Any]:
    """List FusionCompute clusters and their capacity overview."""
    return _call("list_clusters", {}, ctx=ctx)


@mcp.tool()
def list_vms(status: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """List FusionCompute virtual machines."""
    return _call("list_vms", {"status": status}, ctx=ctx)


@mcp.tool()
def get_vm_detail(vm_id: str, ctx: Context) -> dict[str, Any]:
    """Get a VM's detail, host and related alarms."""
    return _call("get_vm_detail", {"vm_id": vm_id}, ctx=ctx)


@mcp.tool()
def get_vm_metrics(
    vm_id: str,
    metric_names: list[str] | None = None,
    time_range: str = "1h",
    ctx: Context = None,
) -> dict[str, Any]:
    """Read CPU, memory, disk and network metrics for a VM."""
    return _call("get_vm_metrics", {"vm_id": vm_id, "metric_names": metric_names, "time_range": time_range}, ctx=ctx)


@mcp.tool()
def search_session_history(
    resource_id: str | None = None,
    fact_type: str | None = None,
    keywords: str | None = None,
    since_days: int = 90,
    limit: int = 3,
    ctx: Context = None,
) -> dict[str, Any]:
    """Recall past diagnoses, applied fixes and executed changes for a resource.

    Returns historical observations, never current state. Scoping to the caller
    happens server-side from the signed token, so no identity argument is
    accepted here.
    """
    return _call(
        "search_session_history",
        {
            "resource_id": resource_id,
            "fact_type": fact_type,
            "keywords": keywords,
            "since_days": since_days,
            "limit": limit,
        },
        ctx=ctx,
    )


@mcp.tool()
def get_cluster_capacity(cluster_id: str, ctx: Context) -> dict[str, Any]:
    """Read current capacity and risk for a cluster."""
    return _call("get_cluster_capacity", {"cluster_id": cluster_id}, ctx=ctx)


@mcp.tool()
def run_capacity_forecast(cluster_id: str, forecast_days: int = 30, ctx: Context = None) -> dict[str, Any]:
    """Forecast capacity exhaustion from mock historical growth."""
    return _call("run_capacity_forecast", {"cluster_id": cluster_id, "forecast_days": forecast_days}, ctx=ctx)


@mcp.tool()
def get_storage_pool_usage(pool_id: str | None = None, ctx: Context = None) -> dict[str, Any]:
    """Read Dorado storage pool capacity and latency."""
    return _call("get_storage_pool_usage", {"pool_id": pool_id}, ctx=ctx)


@mcp.tool()
def query_edme_current_alarms(
    severity: int | None = None,
    iterator: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Query current alarms from the eDME operations-plane model."""
    return _call("query_edme_current_alarms", {"severity": severity, "iterator": iterator}, ctx=ctx)


@mcp.tool()
def query_edme_resources(
    class_name: str = "SYS_StorageDevice",
    page_no: int = 1,
    page_size: int = 20,
    ctx: Context = None,
) -> dict[str, Any]:
    """Query eDME resource instances by system resource class."""
    return _call("query_edme_resources", {
        "class_name": class_name,
        "page_no": page_no,
        "page_size": page_size,
    }, ctx=ctx)


@mcp.tool()
def get_edme_metric_catalog(object_type_id: int | None = None, ctx: Context = None) -> dict[str, Any]:
    """List eDME monitoring object types and supported indicators."""
    return _call("get_edme_metric_catalog", {"object_type_id": object_type_id}, ctx=ctx)


@mcp.tool()
def query_edme_performance_history(
    object_ids: list[str] | None = None,
    indicator_ids: list[int] | None = None,
    time_range: str = "LAST_1_HOUR",
    ctx: Context = None,
) -> dict[str, Any]:
    """Query eDME historical performance data for resources and indicators."""
    return _call("query_edme_performance_history", {
        "object_ids": object_ids,
        "indicator_ids": indicator_ids,
        "time_range": time_range,
    }, ctx=ctx)


@mcp.tool()
def create_approval_request(
    title: str,
    description: str,
    tool_calls: list[dict[str, Any]],
    ctx: Context,
) -> dict[str, Any]:
    """Create an external approval ticket bound to exact write tools and parameters."""
    return _call(
        "create_approval_request",
        {"title": title, "description": description, "tool_calls": tool_calls},
        ctx=ctx,
    )


@mcp.tool()
def restart_vm(vm_id: str, reason: str, change_ticket_id: str, ctx: Context) -> dict[str, Any]:
    """Restart a VM using the task_id of a previously approved matching request."""
    return _call("restart_vm", {"vm_id": vm_id, "reason": reason, "change_ticket_id": change_ticket_id}, ctx=ctx)


@mcp.tool()
def scale_cluster(cluster_id: str, target_hosts: int, reason: str, ctx: Context) -> dict[str, Any]:
    """Change cluster host count using the task_id of an approved matching request."""
    return _call("scale_cluster", {"cluster_id": cluster_id, "target_hosts": target_hosts, "reason": reason}, ctx=ctx)


@mcp.tool()
def modify_ha_policy(cluster_id: str, policy: dict[str, Any], reason: str, ctx: Context) -> dict[str, Any]:
    """Modify HA policy using the task_id of an approved matching request."""
    return _call("modify_ha_policy", {"cluster_id": cluster_id, "policy": policy, "reason": reason}, ctx=ctx)


def _attach_tool_metadata() -> None:
    for name, exposed in mcp._tool_manager._tools.items():
        spec = TOOL_REGISTRY[name]
        # The MCP list response is the Agent's source of truth, so publish the
        # exact Pydantic schema enforced by call_tool instead of the looser
        # hand-written wrapper signature schema.
        exposed.parameters = spec.input_model.model_json_schema()
        exposed.meta = {
            **(exposed.meta or {}),
            TOOL_META_KEY: {
                "risk": spec.risk,
                "auth_roles": spec.auth_roles,
                "category": spec.category,
                "tags": spec.tags,
            },
        }


_attach_tool_metadata()


async def notify_current_client_tool_list_changed(ctx: Context) -> None:
    """Notify the connected client after an in-request runtime catalog mutation."""
    await ctx.session.send_tool_list_changed()


def main() -> None:
    mcp.run(transport=runtime_config.mcp.transport)


if __name__ == "__main__":
    main()
