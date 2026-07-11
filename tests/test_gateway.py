from fastapi.testclient import TestClient

from backend.app import app
from backend.mcp.schemas import ToolRequest
from backend.agent.copilot import classify_intent, plan_tools
from backend.mcp.tools import TOOL_REGISTRY, call_tool
from backend.memory.database import memory_db


client = TestClient(app)


def _tool_request(tool_name: str, params: dict, task_id: str = "gateway-test") -> ToolRequest:
    return ToolRequest(
        tool_name=tool_name,
        params=params,
        caller_user_id="gateway-test-user",
        caller_roles=["readonly"],
        task_id=task_id,
        tenant_id="gateway-test-tenant",
    )


def test_mock_scenario_has_normal_and_abnormal_resources():
    assert len(client.get("/mock/fusioncompute/vms").json()) == 5
    assert len(client.get("/mock/fusioncompute/alarms").json()) == 5
    assert len(client.get("/mock/dorado/storage-pools").json()) == 3


def test_edme_mock_requires_session_and_exposes_operations_apis():
    unauthenticated = client.post(
        "/mock/edme/rest/alarmmgmt/v1/alarms/current-alarm/query",
        json={"query": {}},
    )
    assert unauthenticated.status_code == 403

    login = client.put(
        "/mock/edme/rest/plat/smapp/v1/sessions",
        json={"grantType": "password", "userName": "northbound", "value": "demo-secret"},
    )
    token = login.json()["accessSession"]
    headers = {"X-Auth-Token": token}

    alarms = client.post(
        "/mock/edme/rest/alarmmgmt/v1/alarms/current-alarm/query",
        headers=headers,
        json={"query": {"severity": 2}},
    ).json()
    assert len(alarms["hits"]) == 1
    assert alarms["hits"][0]["alarmId"] == "edme-alarm-1001"

    resources = client.get(
        "/mock/edme/rest/resourcedb/v1/instances/SYS_StorageDevice",
        headers=headers,
    ).json()
    assert resources["totalNum"] == 2

    history = client.post(
        "/mock/edme/rest/metrics/v1/data-svc/history-data/action/query",
        headers=headers,
        json={"obj_ids": ["BAD43F19D4424E8BB9982F44AE783210"], "range": "LAST_1_HOUR"},
    ).json()
    assert len(history["data"]) == 4


def test_edme_agent_routing_and_tools_are_available():
    assert classify_intent("eDME 现在有哪些存储资源？") == "edme_operations"
    calls = plan_tools("edme_operations", "查询 eDME 性能指标", [])
    assert [call["tool_name"] for call in calls] == [
        "query_edme_resources",
        "get_edme_metric_catalog",
        "query_edme_performance_history",
    ]
    assert "query_edme_current_alarms" in TOOL_REGISTRY


def test_tool_schema_rejects_invalid_parameters():
    response = call_tool(_tool_request(
        "run_capacity_forecast",
        {"cluster_id": "not-a-cluster", "forecast_days": 999},
    ))
    assert not response.success
    assert response.error_code == "SCHEMA_VALIDATION_FAILED"


def test_jwt_identity_can_call_gateway():
    token_response = client.post("/api/auth/demo-token", json={
        "user_id": "ops-1",
        "roles": ["ops"],
        "tenant_id": "tenant-a",
    })
    token = token_response.json()["access_token"]
    response = client.post(
        "/api/tools/call",
        headers={"Authorization": f"Bearer {token}"},
        json={"tool_name": "list_vms", "params": {}},
    )
    assert response.status_code == 200
    assert response.json()["success"]


def test_tool_catalog_exposes_json_schema():
    schema = TOOL_REGISTRY["get_vm_metrics"].input_model.model_json_schema()
    assert set(schema["required"]) == {"vm_id"}
    assert "time_range" in schema["properties"]


def test_tool_audit_is_persisted():
    response = call_tool(_tool_request("list_alarms", {}, "audit-test"))
    records = memory_db.list_tool_audit(20)
    assert any(item["audit_id"] == response.audit_id and item["task_id"] == "audit-test" for item in records)


def test_prometheus_metrics_are_exposed():
    response = client.get("/metrics/")
    assert response.status_code == 200
    assert (
        "clawsphere_http_requests_total" in response.text
        or "Prometheus metrics are disabled" in response.text
    )
