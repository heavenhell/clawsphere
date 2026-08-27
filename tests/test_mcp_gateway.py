from __future__ import annotations

import asyncio
from uuid import uuid4

import mcp.types as mcp_types
import pytest

from backend.agent import copilot
from backend.guardrails.approvals import approval_store
from backend.mcp.auth import AuthContext, decode_mcp_caller_token, issue_demo_token
from backend.mcp.gateway import (
    CALLER_TOKEN_META_KEY,
    TOOL_META_KEY,
    McpToolGateway,
    McpUnavailableError,
    GatewayTool,
    ToolCatalogSnapshot,
    ToolCatalogChangedError,
)
from backend.mcp.mcp_server import mcp
from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import TOOL_REGISTRY, call_tool
from backend.providers import repo


def _mcp_tool(name: str, description: str = "List alarms") -> mcp_types.Tool:
    spec = TOOL_REGISTRY[name]
    return mcp_types.Tool(
        name=name,
        description=description,
        inputSchema=spec.input_model.model_json_schema(),
        _meta={
            TOOL_META_KEY: {
                "risk": spec.risk,
                "auth_roles": spec.auth_roles,
                "category": spec.category,
                "tags": spec.tags,
            }
        },
    )


class FakeSession:
    def __init__(self) -> None:
        self.tools = [_mcp_tool("list_alarms")]
        self.list_calls = 0
        self.last_list_meta = None
        self.last_call_meta = None

    async def list_tools(self, *, params=None):
        self.list_calls += 1
        self.last_list_meta = params.meta.model_dump(by_alias=True) if params and params.meta else {}
        return mcp_types.ListToolsResult(tools=self.tools)

    async def call_tool(self, name, arguments, *, meta=None, **_kwargs):
        self.last_call_meta = meta
        return mcp_types.CallToolResult(
            content=[],
            structuredContent={
                "tool_name": name,
                "success": True,
                "data": {"arguments": arguments},
                "error_code": None,
                "error_msg": None,
                "execution_time_ms": 1,
                "audit_id": "audit-test",
            },
            isError=False,
        )


def _gateway_with_fake_session() -> tuple[McpToolGateway, FakeSession]:
    gateway = McpToolGateway("http://mcp.test/mcp")
    session = FakeSession()
    gateway._session = session
    return gateway, session


def test_list_changed_notification_eagerly_refreshes_known_catalogs():
    async def scenario():
        gateway, session = _gateway_with_fake_session()
        auth = AuthContext("user-1", ["readonly"], "tenant-1")
        before = await gateway._catalog(auth)
        assert session.list_calls == 1

        session.tools = [_mcp_tool("list_alarms", "Changed description")]
        notification = mcp_types.ServerNotification(mcp_types.ToolListChangedNotification())
        await gateway._handle_message(notification)
        assert gateway._refresh_task is not None
        await gateway._refresh_task

        after = await gateway._catalog(auth)
        assert session.list_calls == 2
        assert after.version != before.version

    asyncio.run(scenario())


def test_execution_rejects_plan_built_from_catalog_before_notification():
    async def scenario():
        gateway, session = _gateway_with_fake_session()
        auth = AuthContext("user-2", ["readonly"], "tenant-2")
        before = await gateway._catalog(auth)
        session.tools = [_mcp_tool("list_alarms", "Changed schema version")]
        await gateway._handle_message(
            mcp_types.ServerNotification(mcp_types.ToolListChangedNotification())
        )
        assert gateway._refresh_task is not None
        await gateway._refresh_task

        request = ToolRequest(
            tool_name="list_alarms",
            params={},
            caller_user_id=auth.user_id,
            caller_roles=auth.roles,
            tenant_id=auth.tenant_id,
            task_id="task-version-barrier",
        )
        with pytest.raises(ToolCatalogChangedError, match="重新规划"):
            await gateway._call(request, before.version)

    asyncio.run(scenario())


def test_gateway_injects_signed_identity_for_list_and_call():
    async def scenario():
        gateway, session = _gateway_with_fake_session()
        auth = AuthContext("caller-7", ["ops"], "tenant-7")
        catalog = await gateway._catalog(auth)
        list_auth, list_task = decode_mcp_caller_token(
            session.last_list_meta[CALLER_TOKEN_META_KEY]
        )
        assert list_auth == auth
        assert list_task.startswith("catalog-")

        request = ToolRequest(
            tool_name="list_alarms",
            params={"severity": "critical"},
            caller_user_id=auth.user_id,
            caller_roles=auth.roles,
            tenant_id=auth.tenant_id,
            task_id="task-signed-identity",
        )
        response = await gateway._call(request, catalog.version)
        call_auth, call_task = decode_mcp_caller_token(
            session.last_call_meta[CALLER_TOKEN_META_KEY]
        )
        assert response.success
        assert call_auth == auth
        assert call_task == request.task_id

    asyncio.run(scenario())


def test_normal_api_token_cannot_be_reused_as_internal_mcp_token():
    with pytest.raises(PermissionError, match="MCP 调用者令牌"):
        decode_mcp_caller_token(issue_demo_token())


def test_mcp_server_advertises_list_changed_and_hides_control_fields():
    capabilities = mcp._mcp_server.create_initialization_options().capabilities
    assert capabilities.tools and capabilities.tools.listChanged is True
    assert set(mcp._tool_manager._tools) == set(TOOL_REGISTRY)
    for name, tool in mcp._tool_manager._tools.items():
        assert "ctx" not in tool.parameters.get("properties", {})
        assert "task_id" not in tool.parameters.get("properties", {})
        assert tool.parameters == TOOL_REGISTRY[name].input_model.model_json_schema()
        assert tool.meta[TOOL_META_KEY]["risk"] == TOOL_REGISTRY[name].risk


def test_agent_catalog_barrier_discards_old_plan_and_replans_once(monkeypatch):
    discovered = GatewayTool(
        name="list_alarms",
        description="Changed catalog",
        input_schema={"type": "object", "properties": {}},
        risk="none",
        auth_roles=("readonly",),
        category="alert",
        tags=("alarm",),
    )
    gateway = SimpleGateway(ToolCatalogSnapshot("mcp:new", (discovered,), "mcp"))
    monkeypatch.setattr(copilot, "get_tool_gateway", lambda: gateway)
    state = {
        "user_id": "user-replan",
        "user_roles": ["readonly"],
        "tenant_id": "tenant-replan",
        "tool_catalog_version": "mcp:old",
        "tool_catalog_refresh_count": 0,
        "tool_calls_proposed": [{"tool_name": "list_alarms", "params": {}}],
        "route_decisions": [],
    }
    result = copilot.tool_catalog_barrier(state)
    assert result["tool_catalog_barrier"] == "replan"
    assert result["tool_calls_proposed"] == []
    assert result["tool_catalog_version"] == "mcp:new"
    assert copilot.route_after_tool_catalog_barrier(result) == "skill_router"

    second = copilot.tool_catalog_barrier({**state, "tool_catalog_refresh_count": 1})
    assert second["tool_catalog_barrier"] == "error"
    assert "连续变化" in second["error"]


def test_agent_surfaces_mcp_unavailable_without_local_fallback(monkeypatch):
    class FailingGateway(SimpleGateway):
        def catalog(self, _auth):
            raise McpUnavailableError("MCP Server 不可用：http://mcp.test/mcp")

    monkeypatch.setattr(copilot, "get_tool_gateway", lambda: FailingGateway(None))
    result = copilot.tool_catalog_loader({
        "user_id": "user-failure",
        "user_roles": ["readonly"],
        "tenant_id": "tenant-failure",
    })
    assert result["fallback_reason"] == "mcp_unavailable"
    assert result["response_source"] == "mcp_error"
    assert "MCP Server 不可用" in result["final_response"]
    assert "tool_catalog" not in result


class SimpleGateway:
    mode = "mcp"

    def __init__(self, snapshot):
        self.snapshot = snapshot

    def catalog(self, _auth):
        return self.snapshot

    def call(self, _request, _expected_catalog_version):  # pragma: no cover
        raise AssertionError("not used")

    def close(self):
        return None


def test_server_rechecks_blast_radius_even_with_an_approved_task():
    cluster = repo.clusters()[0]
    params = {
        "cluster_id": cluster["id"],
        "target_hosts": cluster["host_count"] + 6,
        "reason": "verify final server blast-radius guard",
    }
    task_id = f"server-guard-{uuid4()}"
    tenant_id = f"server-guard-tenant-{uuid4()}"
    approval = approval_store.create_or_get(
        task_id,
        task_id,
        "server-guard-user",
        tenant_id,
        "approved payload must still pass final server guard",
        [{"tool_name": "scale_cluster", "params": params}],
        "high",
    )
    approval_store.decide(approval["id"], True, "server-guard-admin", "test")
    response = call_tool(ToolRequest(
        tool_name="scale_cluster",
        params=params,
        caller_user_id="server-guard-user",
        caller_roles=["admin"],
        tenant_id=tenant_id,
        task_id=task_id,
    ))
    assert not response.success
    assert response.error_code == "BUSINESS_VALIDATION_FAILED"
    assert "爆炸半径" in response.error_msg
