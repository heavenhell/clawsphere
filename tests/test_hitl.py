from uuid import uuid4

from backend.agent.copilot import resume_copilot, run_copilot
from backend.guardrails.approvals import approval_store
from backend.guardrails.policy import validate_tool_calls
from backend.mcp.tools import MOCK_CHANGE_LOG


def test_readonly_write_request_is_blocked_before_tool_execution():
    before = len(MOCK_CHANGE_LOG)
    result = run_copilot("帮我重启 dcs-app-01，原因是维护验证", ["readonly"])
    assert result["intent"] == "change_execute"
    assert "readonly" in result["answer"]
    assert not result["approval"]
    assert len(MOCK_CHANGE_LOG) == before


def test_high_risk_change_pauses_then_resumes_once():
    conversation_id = f"hitl-test-{uuid4()}"
    before = len(MOCK_CHANGE_LOG)
    paused = run_copilot(
        "帮我重启 dcs-app-01，原因是维护窗口验证",
        ["ops"],
        conversation_id=conversation_id,
        user_id="ops-test",
    )
    assert paused["approval"]
    assert len(MOCK_CHANGE_LOG) == before
    approval_id = paused["approval"]["approval_id"]

    resumed = resume_copilot(conversation_id, True, "admin-test", "批准自动测试")
    assert len(MOCK_CHANGE_LOG) == before + 1
    assert resumed["tool_results"][-1]["tool_name"] == "restart_vm"
    assert resumed["tool_results"][-1]["success"]
    assert approval_store.get(approval_id)["status"] == "approved"


def test_rejected_change_never_executes():
    conversation_id = f"reject-test-{uuid4()}"
    before = len(MOCK_CHANGE_LOG)
    paused = run_copilot(
        "修改 cluster-002 的 HA 策略，原因是演练验证",
        ["admin"],
        conversation_id=conversation_id,
        user_id="admin-test",
    )
    assert paused["approval"]
    result = resume_copilot(conversation_id, False, "admin-reviewer", "风险窗口不合适")
    assert "已拒绝" in result["answer"]
    assert len(MOCK_CHANGE_LOG) == before


def test_guardrail_validates_resource_and_blast_radius():
    missing = validate_tool_calls(
        [{"tool_name": "restart_vm", "params": {"vm_id": "vm-9999", "reason": "自动测试原因", "change_ticket_id": "DEMO-1"}}],
        ["ops"],
        "重启 vm-9999",
    )
    oversized = validate_tool_calls(
        [{"tool_name": "scale_cluster", "params": {"cluster_id": "cluster-002", "target_hosts": 20, "reason": "自动测试扩容"}}],
        ["admin"],
        "扩容 cluster-002 到 20 台",
    )
    assert not missing["allowed"] and "不存在" in missing["violations"][0]
    assert not oversized["allowed"] and "爆炸半径" in oversized["violations"][0]
