from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
