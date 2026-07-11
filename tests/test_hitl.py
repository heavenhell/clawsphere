from uuid import uuid4

from backend.agent.copilot import resume_copilot, run_copilot
from backend.guardrails.approvals import approval_store
from backend.guardrails.policy import validate_tool_calls
from backend.memory.database import memory_db
from backend.mock.repository import repo


def _clear_rate_events(resource_id: str):
    with memory_db.connect() as connection:
        connection.execute("DELETE FROM tool_rate_events WHERE resource_id = ?", (resource_id,))


def test_readonly_write_request_is_blocked_before_tool_execution():
    before = memory_db.count_mock_changes()
    result = run_copilot("帮我重启 dcs-app-01，原因是维护验证", ["readonly"])
    assert result["intent"] == "change_execute"
    assert "readonly" in result["answer"]
    assert not result["approval"]
    assert memory_db.count_mock_changes() == before


def test_high_risk_change_pauses_then_resumes_once():
    conversation_id = f"hitl-test-{uuid4()}"
    _clear_rate_events("dcs-app-01")
    before = memory_db.count_mock_changes()
    paused = run_copilot(
        "帮我重启 dcs-app-01，原因是维护窗口验证",
        ["ops"],
        conversation_id=conversation_id,
        user_id="ops-test",
    )
    assert paused["approval"]
    assert memory_db.count_mock_changes() == before
    approval_id = paused["approval"]["approval_id"]

    resumed = resume_copilot(conversation_id, True, "admin-test", "批准自动测试")
    assert memory_db.count_mock_changes() == before + 1
    assert resumed["tool_results"][-1]["tool_name"] == "restart_vm"
    assert resumed["tool_results"][-1]["success"]
    assert approval_store.get(approval_id)["status"] == "approved"


def test_rejected_change_never_executes():
    conversation_id = f"reject-test-{uuid4()}"
    _clear_rate_events("cluster-002")
    before = memory_db.count_mock_changes()
    paused = run_copilot(
        "修改 cluster-002 的 HA 策略，原因是演练验证",
        ["admin"],
        conversation_id=conversation_id,
        user_id="admin-test",
    )
    assert paused["approval"]
    result = resume_copilot(conversation_id, False, "admin-reviewer", "风险窗口不合适")
    assert "已拒绝" in result["answer"]
    assert memory_db.count_mock_changes() == before


def test_write_tool_is_blocked_by_tool_metadata_without_write_keywords():
    result = validate_tool_calls(
        [{"tool_name": "restart_vm", "params": {"vm_id": "vm-1001", "reason": "perform operation", "change_ticket_id": "DEMO-2"}}],
        ["readonly"],
        "perform operation",
    )
    assert not result["allowed"]
    assert "无权调用工具" in result["violations"][0]


def test_missing_write_resource_requires_clarification():
    result = run_copilot("帮我重启虚拟机", ["ops"])
    assert not result["tool_calls"]
    assert not result["approval"]
    assert "请指定" in result["answer"]


def test_write_is_revalidated_after_approval_wait(monkeypatch):
    _clear_rate_events("dcs-monitor-01")
    conversation_id = f"recheck-{uuid4()}"
    before = memory_db.count_mock_changes()
    paused = run_copilot(
        "帮我重启 dcs-monitor-01，原因是审批后复检",
        ["ops"], conversation_id=conversation_id, user_id="ops-recheck",
    )
    assert paused["approval"]
    original = repo.vms
    monkeypatch.setattr(repo, "vms", lambda: [vm for vm in original() if vm["name"] != "dcs-monitor-01"])
    resumed = resume_copilot(conversation_id, True, "admin-recheck", "批准但资源已变化")
    assert "复检失败" in resumed["answer"]
    assert memory_db.count_mock_changes() == before


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
