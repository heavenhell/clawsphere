from fastapi.testclient import TestClient

from backend.adapters.edme import PlatformPermissionDeniedError
from backend.app import app
from backend.mcp.schemas import ToolRequest
from backend.agent.copilot import verify_resource_claims
from backend.mcp.tools import TOOL_REGISTRY, call_tool
from backend.memory.context_manager import TOKEN_THRESHOLD
from backend.memory.database import memory_db
import backend.app as app_module
import backend.mcp.tools as tools_module


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


# --- Northbound mocks --------------------------------------------------------

def test_mock_scenario_has_normal_and_abnormal_resources():
    assert len(client.get("/mock/fusioncompute/vms").json()) == 5
    assert len(client.get("/mock/fusioncompute/alarms").json()) == 5
    assert len(client.get("/mock/dorado/storage-pools").json()) == 3
    status = client.get("/api/platform-status").json()
    assert status["fusioncompute"] == "mock"
    assert status["edme"] == "mock"


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

    invalid_page = client.get(
        "/mock/edme/rest/resourcedb/v1/instances/SYS_StorageDevice?pageNo=0&pageSize=0",
        headers=headers,
    )
    assert invalid_page.status_code == 422

    history = client.post(
        "/mock/edme/rest/metrics/v1/data-svc/history-data/action/query",
        headers=headers,
        json={"obj_ids": ["BAD43F19D4424E8BB9982F44AE783210"], "range": "LAST_1_HOUR"},
    ).json()
    assert len(history["data"]) == 4


# --- Structured-output grounding (verify_resource_claims) --------------------
# Replaces the old regex grounding. Philosophy: the model self-declares every
# resource it mentions; code deterministically checks that declaration.

def test_state_assertion_backed_by_tool_result_is_grounded():
    grounded, _ = verify_resource_claims(
        "vm-1001 当前 CPU ready 为 6.8%。",
        [{"id": "vm-1001", "kind": "state_assertion", "from_tool": "get_vm_metrics"}],
        [{"tool_name": "get_vm_metrics", "success": True, "data": {"vm": {"id": "vm-1001"}}}],
    )
    assert grounded


def test_state_assertion_without_tool_backing_is_rejected():
    grounded, reason = verify_resource_claims(
        "vm-8888 当前运行正常。",
        [{"id": "vm-8888", "kind": "state_assertion", "from_tool": "get_vm_metrics"}],
        [],
    )
    assert not grounded
    assert "vm-8888" in reason


def test_example_reference_is_allowed_without_any_tool_call():
    # The key new behavior: a term explanation may cite a history object as an
    # example even though this turn ran no tools.
    grounded, _ = verify_resource_claims(
        "热迁移就是把运行中的虚拟机迁走；上一轮提到的 host-005 就是一个例子。",
        [{"id": "host-005", "kind": "example"}],
        [],
    )
    assert grounded


def test_undeclared_resource_in_answer_is_rejected():
    grounded, reason = verify_resource_claims(
        "host-005 当前 CPU 95%。",
        [],
        [],
    )
    assert not grounded
    assert "host-005" in reason


def test_an_id_buried_in_a_tool_text_field_still_grounds():
    """Real platforms return ids inside compound text, not only as own fields.

    eDME's alarm MOI is one string carrying the object type, name and URN, and
    alarm names mention the resource inline. Matching whole field values only
    would reject an answer whose id genuinely came from this turn's tool — the
    model would be told to retract a true statement.
    """
    grounded, _ = verify_resource_claims(
        "vm-1001 当前内存不足，由 alarm-9003 上报。",
        [
            {"id": "vm-1001", "kind": "state_assertion", "from_tool": "list_alarms"},
            {"id": "alarm-9003", "kind": "state_assertion", "from_tool": "list_alarms"},
        ],
        [{"tool_name": "list_alarms", "success": True, "data": [{
            "id": "alarm-9003",
            "name": "虚拟机内存不足",
            "moi": "对象类型=虚拟机, 虚拟机ID=vm-1001, 主机URN=urn:sites:DEMO:hosts:178",
        }]}],
    )
    assert grounded


def test_burying_an_id_in_text_does_not_ground_one_the_tools_never_returned():
    grounded, reason = verify_resource_claims(
        "vm-7777 也受影响。",
        [{"id": "vm-7777", "kind": "state_assertion", "from_tool": "list_alarms"}],
        [{"tool_name": "list_alarms", "success": True, "data": [{
            "id": "alarm-9003",
            "moi": "对象类型=虚拟机, 虚拟机ID=vm-1001",
        }]}],
    )
    assert not grounded
    assert "vm-7777" in reason


def test_numeric_ids_returned_unquoted_are_groundable():
    # eDME returns host_id as a bare number; str-only collection would miss it.
    grounded, _ = verify_resource_claims(
        "该主机负载偏高。",
        [{"id": "178", "kind": "state_assertion", "from_tool": "list_hosts"}],
        [{"tool_name": "list_hosts", "success": True, "data": [{"host_id": 178}]}],
    )
    assert grounded


# --- Tool gateway: RBAC / schema / audit -------------------------------------

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


def test_edme_403_returns_permission_denied_and_is_audited(monkeypatch):
    def deny(*_args, **_kwargs):
        raise PlatformPermissionDeniedError("vendor payload must not escape")

    monkeypatch.setattr(tools_module.repo, "edme_resource_instances", deny)
    response = call_tool(_tool_request(
        "query_edme_resources",
        {},
        "audit-edme-permission-denied",
    ))

    assert response.success is False
    assert response.error_code == "PERMISSION_DENIED"
    assert response.error_msg == "当前 eDME 业务账号权限不足"
    records = memory_db.list_tool_audit(50, "gateway-test-tenant")
    assert any(
        item["audit_id"] == response.audit_id
        and item["error_code"] == "PERMISSION_DENIED"
        for item in records
    )


def test_prometheus_metrics_are_exposed():
    response = client.get("/metrics/")
    assert response.status_code == 200
    assert (
        "clawsphere_http_requests_total" in response.text
        or "Prometheus metrics are disabled" in response.text
    )


# --- LLM-native offline behavior + status redaction --------------------------

def test_chat_offline_prompts_to_connect_llm():
    response = client.post(
        "/api/chat",
        json={"message": "你好", "conversation_id": "offline-prompt-test"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["response_source"] == "unavailable"
    assert "大模型" in payload["answer"]
    assert payload["llm_status"]["configured"] is False
    assert "api_key" not in payload["llm_status"]


def test_llm_status_endpoint_is_redacted():
    response = client.get("/api/llm-status")
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"configured", "model", "status"}
    assert "api_key" not in payload


def test_chat_trace_exposes_structured_context_metrics():
    response = client.post(
        "/api/chat",
        json={
            "message": "诊断 vm-1001 的性能",
            "conversation_id": "structured-context-test",
        },
    )
    assert response.status_code == 200
    context = response.json()["context"]
    assert context["schema_version"] == 3
    assert context["working_context"]["active_resource_ids"] == ["vm-1001"]
    assert context["estimated_tokens"] <= TOKEN_THRESHOLD
    # A single-turn conversation is nowhere near the threshold, so the trace
    # must show that no compaction was spent on it.
    assert context["compacted"] is False


def test_chat_rejects_oversized_message_before_agent_execution():
    response = client.post(
        "/api/chat",
        json={"message": "告警" * 4001},
    )
    assert response.status_code == 422


def test_chat_rate_limit_returns_429_before_agent_execution(monkeypatch):
    class DeniedLimiter:
        def try_acquire(self, user_id, tenant_id):
            return None

    monkeypatch.setattr(app_module, "chat_limiter", DeniedLimiter(), raising=False)
    response = client.post(
        "/api/chat",
        json={"message": "查询资源"},
    )
    assert response.status_code == 429


def test_a_metric_value_cannot_ground_a_claim_about_a_resource():
    """Numeric ids must be groundable; numeric measurements must not be.

    eDME returns `host_id: 178` unquoted, so numbers have to count as evidence —
    but only from a field that names an identifier. Otherwise any metric value
    in the payload (memory_mb: 8192) would satisfy a state assertion about a
    resource called "8192".
    """
    tool_results = [{
        "tool_name": "list_hosts", "success": True,
        "data": [{"host_id": 178, "memory_mb": 8192, "cpu_usage": 0.58}],
    }]

    grounded, _ = verify_resource_claims(
        "该主机负载偏高。", [{"id": "178", "kind": "state_assertion"}], tool_results
    )
    assert grounded

    grounded, reason = verify_resource_claims(
        "该主机负载偏高。", [{"id": "8192", "kind": "state_assertion"}], tool_results
    )
    assert not grounded
    assert "8192" in reason
