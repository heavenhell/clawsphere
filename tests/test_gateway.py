from fastapi.testclient import TestClient

from backend.app import app
from backend.mcp.schemas import ToolRequest
from backend.mcp.tools import TOOL_REGISTRY, call_tool
from backend.memory.database import memory_db


client = TestClient(app)


def test_mock_scenario_has_normal_and_abnormal_resources():
    assert len(client.get("/mock/fusioncompute/vms").json()) == 5
    assert len(client.get("/mock/fusioncompute/alarms").json()) == 5
    assert len(client.get("/mock/dorado/storage-pools").json()) == 3


def test_tool_schema_rejects_invalid_parameters():
    response = call_tool(ToolRequest(
        tool_name="run_capacity_forecast",
        params={"cluster_id": "not-a-cluster", "forecast_days": 999},
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
    response = call_tool(ToolRequest(tool_name="list_alarms", params={}, task_id="audit-test"))
    records = memory_db.list_tool_audit(20)
    assert any(item["audit_id"] == response.audit_id and item["task_id"] == "audit-test" for item in records)


def test_prometheus_metrics_are_exposed():
    response = client.get("/metrics/")
    assert response.status_code == 200
    assert (
        "clawsphere_http_requests_total" in response.text
        or "Prometheus metrics are disabled" in response.text
    )
