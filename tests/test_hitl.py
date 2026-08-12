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
from backend.skills.loader import reload_skills


def _clear_rate_events(resource_id: str):
    with memory_db.connect() as connection:
        connection.execute("DELETE FROM tool_rate_events WHERE resource_id = ?", (resource_id,))


def _propose(
    monkeypatch,
    tool_name: str,
    params: dict,
    reason: str = "model proposal",
    *,
    bypass_rbac_search: bool = False,
):
    """Pin all three planning stages (skill router, tool search, tool call) to
    a deterministic path so the HITL/RBAC safety spine can be exercised
    without a live model.

    bypass_rbac_search=True also stubs the tool_catalog_search host step to
    offer tool_name regardless of RBAC, for tests whose entire point is
    proving guardrail's RBAC re-check independently catches an unauthorized
    tool even though something upstream apparently offered it. For tools the
    test's role can actually call, leave this False so the real, RBAC-filtered
    tool_catalog_search runs for extra integration coverage."""
    monkeypatch.setattr(
        copilot,
        "_call_skill_router",
        lambda *args, **kwargs: {
            "decision": "use_skill",
            "skill_ids": ["resource_query"],
            "arguments": {},
            "confidence": 0.9,
            "missing_context": [],
            "reason_summary": "test stub",
        },
    )
    monkeypatch.setattr(
        copilot,
        "_call_tool_search_planner",
        lambda *args, **kwargs: {
            "tool_calls": [{
                "tool_name": "ToolSearch",
                "params": {"query": tool_name, "top_k": 5, "required_capabilities": []},
            }],
            "reason": "test stub",
        },
    )
    if bypass_rbac_search:
        # Patch what the real tool_catalog_search node calls internally
        # (retrieve_tools, _rbac_tool_catalog), not the node function itself.
        # The compiled graph is a process-wide singleton built once (see
        # get_graph()) — node functions are bound into it by reference at
        # build time, so patching copilot.tool_catalog_search as a whole is
        # only reliable before the graph's first build and silently no-ops
        # (or worse, permanently poisons the singleton for every later test)
        # depending on test order. retrieve_tools/_rbac_tool_catalog are
        # plain module-level names the node looks up fresh on every call, so
        # patching those is safe regardless of when the graph was built.
        spec = TOOL_REGISTRY[tool_name]
        schema = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": spec.description,
                "parameters": spec.input_model.model_json_schema(),
            },
        }
        monkeypatch.setattr(
            copilot,
            "retrieve_tools",
            lambda *args, **kwargs: [{
                "tool_name": tool_name,
                "category": spec.category,
                "tags": spec.tags,
                "description": spec.description,
                "score": 1.0,
                "retrieval_mode": "stub",
            }],
        )
        monkeypatch.setattr(copilot, "_rbac_tool_catalog", lambda roles, names=None: [schema])
    monkeypatch.setattr(
        copilot,
        "_call_tool_call_planner",
        lambda *args, **kwargs: {"tool_calls": [{"tool_name": tool_name, "params": params}], "reason": reason},
    )


_RESTART = {"vm_id": "dcs-app-01", "reason": "维护窗口验证", "change_ticket_id": "DEMO-AUTO"}


def test_readonly_write_request_is_blocked_before_tool_execution(monkeypatch):
    _propose(monkeypatch, "restart_vm", _RESTART, bypass_rbac_search=True)
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
    # Skill router declining to use a Skill (direct_answer) short-circuits
    # before the tool-search/tool-call stages ever run — same observable
    # outcome as the old "router proposed zero tool_calls" case, with less
    # mocking since it exercises the intended fast path.
    monkeypatch.setattr(
        copilot,
        "_call_skill_router",
        lambda *args, **kwargs: {
            "decision": "direct_answer",
            "skill_ids": [],
            "arguments": {},
            "confidence": 0.9,
            "missing_context": [],
            "reason_summary": "需要更多信息",
        },
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


def test_tool_call_outside_offered_candidates_is_rejected_by_guardrail(monkeypatch):
    """restart_vm is RBAC-authorized for ops, but this turn's tool search only
    looked for list_alarms — restart_vm was never offered, so guardrail must
    reject it even though a plain RBAC check alone would have allowed it."""
    monkeypatch.setattr(copilot, "_call_skill_router", lambda *args, **kwargs: {
        "decision": "use_skill",
        "skill_ids": ["resource_query"],
        "arguments": {},
        "confidence": 0.9,
        "missing_context": [],
        "reason_summary": "test stub",
    })
    monkeypatch.setattr(copilot, "_call_tool_search_planner", lambda *args, **kwargs: {
        "tool_calls": [{
            "tool_name": "ToolSearch",
            "params": {"query": "list_alarms", "top_k": 5, "required_capabilities": []},
        }],
        "reason": "test stub",
    })
    monkeypatch.setattr(copilot, "_call_tool_call_planner", lambda *args, **kwargs: {
        "tool_calls": [{"tool_name": "restart_vm", "params": _RESTART}],
        "reason": "model proposed a tool outside the offered set",
    })
    result = run_copilot("查询告警", ["ops"])
    assert "未在本轮检索候选中提供" in result["answer"]
    assert not result["tool_results"]


def test_authorization_epoch_invalidates_stale_write_after_catalog_change(monkeypatch):
    _propose(monkeypatch, "restart_vm", {**_RESTART, "reason": "epoch 失效验证"})
    conversation_id = f"epoch-test-{uuid4()}"
    _clear_rate_events("dcs-app-01")
    before = memory_db.count_mock_changes()
    paused = run_copilot(
        "帮我重启 dcs-app-01",
        ["ops"],
        conversation_id=conversation_id,
        user_id="epoch-test-user",
    )
    assert paused["approval"]
    reload_skills()
    resumed = resume_copilot(paused["approval"]["task_id"], True, "admin-test", "批准 epoch 测试")
    assert memory_db.count_mock_changes() == before
    assert "目录版本已变化" in resumed["answer"]


def test_llm_stage_metrics_records_one_entry_per_stage_on_the_tool_execution_path(monkeypatch):
    _propose(monkeypatch, "list_alarms", {})
    result = run_copilot("现在有哪些告警", ["readonly"])
    stages = [item["stage"] for item in result["llm_stage_metrics"]]
    assert stages[:3] == ["skill_router", "tool_search_planner", "tool_call_planner"]
    assert all(item["latency_ms"] >= 0 for item in result["llm_stage_metrics"])
    assert result["agent_step_count"] == len(result["llm_stage_metrics"])


def test_llm_call_budget_is_enforced_before_invoking_the_llm(monkeypatch):
    called = []
    monkeypatch.setattr(copilot, "_call_skill_router", lambda *args, **kwargs: called.append(True))
    state = {
        "agent_step_count": copilot.MAX_LLM_CALLS_PER_TURN,
        "message": "帮我看看告警",
        "user_roles": ["readonly"],
    }
    result = copilot.skill_router(state)
    assert not called
    assert result["fallback_reason"] == "llm_call_budget_exhausted"
    assert result["intent"] == "direct_answer"


def test_direct_answer_short_circuit_never_calls_tool_search_or_tool_call_planner(monkeypatch):
    monkeypatch.setattr(copilot, "_call_skill_router", lambda *args, **kwargs: {
        "decision": "direct_answer",
        "skill_ids": [],
        "arguments": {},
        "confidence": 0.95,
        "missing_context": [],
        "reason_summary": "寒暄，无需工具",
    })

    def fail_if_called(*args, **kwargs):
        pytest.fail("tool_search_planner/tool_call_planner should not run after a direct_answer decision")

    monkeypatch.setattr(copilot, "_call_tool_search_planner", fail_if_called)
    monkeypatch.setattr(copilot, "_call_tool_call_planner", fail_if_called)
    result = run_copilot("你好", ["readonly"])
    assert not result["tool_calls"]


def test_llm1_then_llm2_then_llm3_call_order_on_the_full_tool_path(monkeypatch):
    order: list[str] = []

    def spy_skill_router(*args, **kwargs):
        order.append("skill_router")
        return {
            "decision": "use_skill",
            "skill_ids": ["resource_query"],
            "arguments": {},
            "confidence": 0.9,
            "missing_context": [],
            "reason_summary": "test stub",
        }

    def spy_tool_search(*args, **kwargs):
        order.append("tool_search_planner")
        return {
            "tool_calls": [{
                "tool_name": "ToolSearch",
                "params": {"query": "list_alarms", "top_k": 5, "required_capabilities": []},
            }],
            "reason": "test stub",
        }

    def spy_tool_call(*args, **kwargs):
        order.append("tool_call_planner")
        return {"tool_calls": [{"tool_name": "list_alarms", "params": {}}], "reason": "test stub"}

    monkeypatch.setattr(copilot, "_call_skill_router", spy_skill_router)
    monkeypatch.setattr(copilot, "_call_tool_search_planner", spy_tool_search)
    monkeypatch.setattr(copilot, "_call_tool_call_planner", spy_tool_call)
    # tool_catalog_search itself is real here (RBAC + BM25), unlike _propose's
    # default — this covers PR2's host function end-to-end too.
    run_copilot("现在有哪些告警", ["readonly"])
    assert order == ["skill_router", "tool_search_planner", "tool_call_planner"]


def test_llm_responder_preserves_upstream_fallback_reason_instead_of_overwriting_it():
    """llm_available=False can come from a genuinely unconfigured LLM OR from
    the call budget being exhausted — these must stay distinguishable in
    fallback_reason rather than both collapsing to "llm_not_configured"."""
    state = {
        "message": "test",
        "llm_available": False,
        "fallback_reason": "llm_call_budget_exhausted",
    }
    result = copilot.llm_responder(state)
    assert result["fallback_reason"] == "llm_call_budget_exhausted"

    # No upstream reason recorded (e.g. the very first call fails): falls back
    # to the generic "not configured" message, matching prior behavior.
    bare_state = {"message": "test", "llm_available": False}
    bare_result = copilot.llm_responder(bare_state)
    assert bare_result["fallback_reason"] == "llm_not_configured"
