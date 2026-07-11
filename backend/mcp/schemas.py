from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ToolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyParams(ToolParams):
    pass


class AlarmListParams(ToolParams):
    severity: str | None = None


class AlarmDetailParams(ToolParams):
    alarm_id: str = Field(pattern=r"^alarm-\d+$")


class ClusterCapacityParams(ToolParams):
    cluster_id: str = Field(pattern=r"^cluster-\d+$")


class VmListParams(ToolParams):
    status: str | None = None


class VmDetailParams(ToolParams):
    vm_id: str = Field(min_length=3, max_length=80)


class VmMetricsParams(VmDetailParams):
    metric_names: list[str] | None = None
    time_range: str = Field(default="1h", pattern=r"^\d+[mhd]$")


class ForecastParams(ClusterCapacityParams):
    forecast_days: int = Field(default=30, ge=1, le=365)


class StoragePoolParams(ToolParams):
    pool_id: str | None = Field(default=None, pattern=r"^ds-\d+$")


class ProposedToolCall(ToolParams):
    tool_name: str = Field(min_length=3, max_length=80)
    params: dict[str, Any]


class ApprovalRequestParams(ToolParams):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=5, max_length=2000)
    tool_calls: list[ProposedToolCall] = Field(min_length=1, max_length=10)


class RestartVmParams(VmDetailParams):
    reason: str = Field(min_length=5, max_length=500)
    change_ticket_id: str = Field(pattern=r"^(CHG-|DEMO-)[A-Za-z0-9-]+$")


class ScaleClusterParams(ClusterCapacityParams):
    target_hosts: int = Field(ge=1, le=64)
    reason: str = Field(min_length=5, max_length=500)


class ModifyHaPolicyParams(ClusterCapacityParams):
    policy: dict[str, Any]
    reason: str = Field(min_length=5, max_length=500)


class ToolRequest(BaseModel):
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    caller_user_id: str
    caller_roles: list[str]
    task_id: str = Field(min_length=8, max_length=128)
    tenant_id: str


class GatewayToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    task_id: str = Field(default_factory=lambda: str(uuid4()), min_length=8, max_length=128)


class ToolResponse(BaseModel):
    tool_name: str
    success: bool
    data: Any | None = None
    error_code: str | None = None
    error_msg: str | None = None
    execution_time_ms: int
    audit_id: str
