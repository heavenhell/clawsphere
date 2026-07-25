from uuid import uuid4
from dataclasses import replace

import pytest

import backend.agent.copilot as copilot
from backend.agent.copilot import PendingApprovalError, resume_copilot, run_copilot
from backend.guardrails.approvals import approval_store
from backend.guardrails.policy import validate_tool_calls
from backend.memory.database import memory_db
from backend.mcp.tools import TOOL_REGISTRY
from backend.providers import repo


def _clear_rate_events(resource_id: str):
    with memory_db.connect() as connection:
        connection.execute("DELETE FROM tool_rate_events WHERE resource_id = ?", (resource_id,))


def _propose(monkeypatch, tool_name: str, params: dict, reason: str = "model proposal"):
    """Pin the LLM router to a deterministic tool proposal so the HITL/RBAC
    safety spine can be exercised without a live model."""
    monkeypatch.setattr(
        copilot,
        "call_deepseek_agent_plan",
        lambda *args, **kwargs: {"tool_calls": [{"tool_name": tool_name, "params": params}], "reason": reason},
    )


_RESTART = {"vm_id": "dcs-app-01", "reason": "维护窗口验证", "change_ticket_id": "DEMO-AUTO"}


def test_readonly_write_request_is_blocked_before_tool_execution(monkeypatch):
    _propose(monkeypatch, "restart_vm", _RESTART)
    before = memory_db.count_mock_changes()
    result = run_copilot("帮我重启 dcs-app-01", ["readonly"])
    assert "无权调用工具" in result["answer"]
    assert not result["approval"]
    assert memory_db.count_mock_changes() == before


def test_high_risk_change_pauses_then_resumes_once(monkeypatch):
    _propose(monkeypatch, "restart_vm", _RESTART)
    conversation_id = f"hitl-test-{uuid4()}"
    _clear_rate_events("dcs-app-01")
    before = memory_db.count_mock_changes()
    paused = run_copilot(
        "帮我重启 dcs-app-01",
        ["ops"],
        conversation_id=conversation_id,
        user_id="ops-test",
    )
    assert paused["approval"]
    assert memory_db.count_mock_changes() == before
    approval_id = paused["approval"]["approval_id"]

    resumed = resume_copilot(paused["approval"]["task_id"], True, "admin-test", "批准自动测试")
    assert memory_db.count_mock_changes() == before + 1
    assert resumed["tool_results"][-1]["tool_name"] == "restart_vm"
    assert resumed["tool_results"][-1]["success"]
    assert approval_store.get(approval_id)["status"] == "executed"


def test_rejected_change_never_executes(monkeypatch):
    _propose(
        monkeypatch,
        "modify_ha_policy",
        {"cluster_id": "cluster-002", "policy": {"enabled": True}, "reason": "HA 演练验证流程"},
    )
    conversation_id = f"reject-test-{uuid4()}"
    _clear_rate_events("cluster-002")
    before = memory_db.count_mock_changes()
    paused = run_copilot(
        "启用 cluster-002 的 HA 策略",
        ["admin"],
        conversation_id=conversation_id,
        user_id="admin-test",
    )
    assert paused["approval"]
    result = resume_copilot(paused["approval"]["task_id"], False, "admin-reviewer", "风险窗口不合适")
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


def test_no_proposed_tool_means_no_execution(monkeypatch):
    monkeypatch.setattr(
        copilot,
        "call_deepseek_agent_plan",
        lambda *args, **kwargs: {"tool_calls": [], "reason": "需要更多信息"},
    )
    result = run_copilot("帮我重启虚拟机", ["ops"])
    assert not result["tool_calls"]
    assert not result["approval"]


def test_approval_uses_highest_proposed_tool_risk(monkeypatch):
    original = TOOL_REGISTRY["restart_vm"]
    monkeypatch.setitem(TOOL_REGISTRY, "restart_vm", replace(original, risk="medium"))
    _propose(monkeypatch, "restart_vm", {**_RESTART, "reason": "中风险审批验证"})
    _clear_rate_events("dcs-app-01")
    conversation_id = f"medium-risk-{uuid4()}"
    paused = run_copilot(
        "帮我重启 dcs-app-01",
        ["ops"],
        conversation_id=conversation_id,
        user_id="medium-risk-maker",
    )
    item = approval_store.get(paused["approval"]["approval_id"])
    assert item["risk"] == "medium"
    resume_copilot(paused["approval"]["task_id"], False, "medium-risk-checker", "测试结束")


def test_write_is_revalidated_after_approval_wait(monkeypatch):
    _propose(
        monkeypatch,
        "restart_vm",
        {"vm_id": "dcs-monitor-01", "reason": "审批后复检", "change_ticket_id": "DEMO-AUTO"},
    )
    _clear_rate_events("dcs-monitor-01")
    conversation_id = f"recheck-{uuid4()}"
    before = memory_db.count_mock_changes()
    paused = run_copilot(
        "帮我重启 dcs-monitor-01",
        ["ops"], conversation_id=conversation_id, user_id="ops-recheck",
    )
    assert paused["approval"]
    original = repo.vms
    monkeypatch.setattr(repo, "vms", lambda: [vm for vm in original() if vm["name"] != "dcs-monitor-01"])
    resumed = resume_copilot(paused["approval"]["task_id"], True, "admin-recheck", "批准但资源已变化")
    assert "复检失败" in resumed["answer"]
    assert memory_db.count_mock_changes() == before


def test_pending_approval_blocks_later_message_in_same_conversation(monkeypatch):
    _propose(monkeypatch, "restart_vm", {**_RESTART, "reason": "独立 checkpoint 验证"})
    conversation_id = f"independent-checkpoint-{uuid4()}"
    _clear_rate_events("dcs-app-01")
    before = memory_db.count_mock_changes()
    paused = run_copilot(
        "帮我重启 dcs-app-01",
        ["ops"],
        conversation_id=conversation_id,
        user_id="checkpoint-maker",
    )
    assert paused["approval"]
    checkpoint_thread_id = paused["approval"]["task_id"]

    with pytest.raises(PendingApprovalError, match=paused["approval"]["approval_id"]):
        run_copilot(
            "我现在有哪些资源？",
            ["ops"],
            conversation_id=conversation_id,
            user_id="checkpoint-maker",
        )

    resumed = resume_copilot(
        checkpoint_thread_id,
        True,
        "checkpoint-reviewer",
        "批准独立 checkpoint 测试",
    )
    assert resumed["conversation_id"] == conversation_id
    assert memory_db.count_mock_changes() == before + 1


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
