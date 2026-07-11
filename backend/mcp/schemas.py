from __future__ import annotations

from typing import Any

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


class ApprovalRequestParams(ToolParams):
    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=5, max_length=2000)
    risk: str = Field(default="high", pattern=r"^(medium|high)$")


class ToolRequest(BaseModel):
    tool_name: str
    params: dict[str, Any] = Field(default_factory=dict)
    caller_user_id: str = "demo-user"
    caller_roles: list[str] = Field(default_factory=lambda: ["readonly"])
    task_id: str = "demo-task"
    tenant_id: str = "demo-tenant"


class ToolResponse(BaseModel):
    tool_name: str
    success: bool
    data: Any | None = None
    error_code: str | None = None
    error_msg: str | None = None
    execution_time_ms: int
    audit_id: str
