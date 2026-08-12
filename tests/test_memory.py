import json
from inspect import signature

from backend.memory.context_manager import TOKEN_THRESHOLD, estimate_tokens, manage_context_window
from backend.memory.database import MemoryDatabase
from backend.memory.retriever import retrieve, retrieve_history, retrieve_tools
from backend.mcp.tools import TOOL_REGISTRY, tool_catalog_tier1
from backend.skills.loader import (
    SkillDocument,
    list_skills_for_roles,
    load_all_skills,
    load_skill_by_id,
    reload_skills,
    skill_catalog_version,
    startup_skill_summaries,
)
import backend.agent.copilot as copilot
from backend.agent.copilot import run_copilot, skill_loader, tool_catalog_search


def test_context_keeps_six_turns_and_compresses_older_messages():
    history = []
    for index in range(8):
        history.extend([
            {"role": "user", "content": f"第 {index} 轮问题"},
            {"role": "assistant", "content": f"第 {index} 轮回答"},
        ])
    context = manage_context_window(history)
    assert len(context["recent_messages"]) == 12
    assert context["older_message_count"] == 4
    assert "第 0 轮问题" in context["conversation_summary"]


def test_skill_loader_has_three_progressive_tiers():
    skills = load_all_skills()
    assert len(skills) >= 4
    assert all(skill.one_liner and skill.summary and skill.detail for skill in skills)
    assert "diagnose_vm_performance" in startup_skill_summaries()


def test_bm25_retrieval_finds_skill_and_history_case():
    skills = retrieve("dcs-app-01 变慢 CPU Ready", ["readonly"], top_k=2)
    cases = retrieve_history("虚拟机 CPU Ready 过高", ["readonly"], top_k=1)
    assert skills[0]["id"].startswith("skill:diagnose_vm_performance")
    assert cases[0]["doc_type"] == "alert_case"
    assert skills[0]["retrieval_mode"] == "bm25"


def test_tool_spec_has_category_and_tags_for_all_registered_tools():
    for name, spec in TOOL_REGISTRY.items():
        assert spec.category, f"{name} is missing a category"
        assert isinstance(spec.tags, list)


def test_load_skill_by_id_returns_known_id_and_none_for_unknown():
    assert load_skill_by_id("diagnose_vm_performance") is not None
    assert load_skill_by_id("diagnose_vm_performance").id == "diagnose_vm_performance"
    assert load_skill_by_id("does_not_exist") is None


def test_list_skills_for_roles_filters_by_applicable_roles():
    ops_only = SkillDocument(
        id="ops_only_skill", title="ops only", version=1, tags=[], permission="public",
        applicable_roles=["ops", "admin"], one_liner="", summary="", detail="",
    )
    everyone = SkillDocument(
        id="open_skill", title="open", version=1, tags=[], permission="public",
        applicable_roles=["readonly", "ops", "admin"], one_liner="", summary="", detail="",
    )
    filtered = list_skills_for_roles([ops_only, everyone], ["readonly"])
    assert [skill.id for skill in filtered] == ["open_skill"]


def test_reload_skills_bumps_skill_catalog_version():
    before = skill_catalog_version()
    after = reload_skills()
    assert after == before + 1
    assert skill_catalog_version() == after


def test_retrieve_tools_excludes_rbac_unauthorized_tools_before_ranking():
    results = retrieve_tools("scale_cluster 调整集群主机数", ["readonly"], top_k=5)
    assert all(item["tool_name"] != "scale_cluster" for item in results)
    admin_results = retrieve_tools("scale_cluster 调整集群主机数", ["admin"], top_k=5)
    assert any(item["tool_name"] == "scale_cluster" for item in admin_results)


def test_retrieve_tools_finds_exact_name_match_as_top_hit():
    results = retrieve_tools("get_vm_metrics 性能指标", ["readonly"], top_k=3)
    assert results[0]["tool_name"] == "get_vm_metrics"
    assert results[0]["retrieval_mode"] == "bm25"


def test_skill_loader_drops_unknown_and_non_applicable_skill_ids():
    state = {
        "skill_decision": {"skill_ids": ["diagnose_vm_performance", "does_not_exist"]},
        "user_roles": ["readonly"],
    }
    result = skill_loader(state)
    assert result["selected_skill_ids"] == ["diagnose_vm_performance"]
    assert result["loaded_skills"][0]["id"] == "diagnose_vm_performance"
    assert result["loaded_skills"][0]["detail"]
    assert result["skill_catalog_version"] >= 1


def test_tool_catalog_search_returns_full_schemas_for_candidates_only():
    state = {
        "tool_search_request": {"query": "list_alarms 当前告警", "top_k": 2},
        "user_roles": ["readonly"],
        "tenant_id": "global",
    }
    result = tool_catalog_search(state)
    candidate_names = {item["tool_name"] for item in result["tool_search_candidates"]}
    schema_names = {item["function"]["name"] for item in result["selected_tool_schemas"]}
    assert candidate_names == schema_names
    assert "list_alarms" in candidate_names
    assert all("parameters" in item["function"] for item in result["selected_tool_schemas"])


def test_tool_catalog_tier1_excludes_unauthorized_tools():
    readonly_catalog = tool_catalog_tier1(["readonly"])
    admin_catalog = tool_catalog_tier1(["admin"])
    for write_tool in ("restart_vm", "scale_cluster", "modify_ha_policy"):
        assert write_tool not in readonly_catalog
        assert write_tool in admin_catalog
    assert "list_alarms" in readonly_catalog
    # No parameter schema — only name/category/description lines.
    assert "{" not in readonly_catalog and "parameters" not in readonly_catalog
    assert readonly_catalog.count("\n") + 1 < len(TOOL_REGISTRY)  # fewer lines than the full (unfiltered) set
    assert admin_catalog.count("\n") + 1 == len(TOOL_REGISTRY)  # admin sees every tool


def test_conversation_survives_database_reopen(tmp_path):
    path = tmp_path / "memory.db"
    first = MemoryDatabase(path)
    first.append_turn("c-1", "u-1", "t-1", "问题", "回答", "摘要")
    second = MemoryDatabase(path)
    history, summary = second.load_conversation("c-1", "u-1", "t-1")
    assert summary == "摘要"
    assert history[-1] == {"role": "assistant", "content": "回答"}


def test_conversation_isolated_by_user_and_tenant(tmp_path):
    database = MemoryDatabase(tmp_path / "isolated.db")
    database.append_turn("shared-id", "user-a", "tenant-a", "问题A", "回答A", "摘要A")
    try:
        database.load_conversation("shared-id", "user-b", "tenant-a")
        assert False, "cross-user read should fail"
    except PermissionError:
        pass
    try:
        database.load_conversation("shared-id", "user-a", "tenant-b")
        assert False, "cross-tenant read should fail"
    except PermissionError:
        pass


def test_agent_returns_rolling_summary_after_six_turns():
    conversation_id = "summary-test"
    for index in range(8):
        run_copilot(f"查询第 {index} 轮资源", conversation_id=conversation_id)
    result = run_copilot("有多少虚拟机", conversation_id=conversation_id)
    assert "查询第 0 轮资源" in result["summary"]
    assert len(result["summary"]) <= 1200


def test_chinese_token_estimate_and_summary_boundary():
    messages = [{"role": "user", "content": "这是十个中文字符测试文本"}]
    assert estimate_tokens(messages) >= 10
    long_history = []
    for index in range(20):
        long_history.extend([
            {"role": "user", "content": f"第{index}轮：" + "容量告警处理记录。" * 20},
            {"role": "assistant", "content": f"第{index}轮结论：已核实。"},
        ])
    context = manage_context_window(long_history)
    assert len(context["conversation_summary"]) <= 1200
    assert not context["conversation_summary"].endswith("第")


def test_long_ascii_identifier_token_estimate_scales_with_length():
    message = [{"role": "user", "content": "dcs-" + "x" * 100}]
    assert estimate_tokens(message) >= 25


def test_context_budget_compresses_fewer_than_six_very_large_turns():
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"第{index}条：" + "容量告警诊断证据。" * 180,
        }
        for index in range(8)
    ]

    context = manage_context_window(history)

    assert context["older_message_count"] > 0
    assert context["estimated_tokens"] <= TOKEN_THRESHOLD


def test_context_api_accepts_current_message_for_relevance():
    assert "current_message" in signature(manage_context_window).parameters


def test_context_preserves_relevant_older_evidence_and_structured_working_state():
    history = [
        {"role": "user", "content": "检查 cluster-001 的容量风险"},
        {"role": "assistant", "content": "cluster-001 当前需要持续观察。"},
    ]
    for index in range(7):
        history.extend([
            {"role": "user", "content": f"第 {index} 轮查询普通资源"},
            {"role": "assistant", "content": f"第 {index} 轮普通资源结果"},
        ])

    context = manage_context_window(
        history,
        current_message="继续分析 cluster-001 的容量趋势",
    )

    assert any(
        "cluster-001" in item["content"]
        for item in context["relevant_messages"]
    )
    assert context["working_context"]["active_resource_ids"] == ["cluster-001"]
    assert context["working_context"]["latest_user_request"] == "继续分析 cluster-001 的容量趋势"
    assert context["working_context"]["history_message_count"] == len(history)
    assert context["estimated_tokens"] <= TOKEN_THRESHOLD


def test_relevant_history_matches_chinese_topic_without_resource_id():
    history = [
        {"role": "user", "content": "之前讨论过集群容量风险和扩容窗口"},
        {"role": "assistant", "content": "建议持续观察剩余容量。"},
    ]
    for index in range(7):
        history.extend([
            {"role": "user", "content": f"普通资源查询 {index}"},
            {"role": "assistant", "content": f"普通资源结果 {index}"},
        ])

    context = manage_context_window(
        history,
        current_message="继续分析容量趋势",
    )

    assert any(
        "容量风险" in item["content"]
        for item in context["relevant_messages"]
    )


def test_relevant_history_keeps_user_and_assistant_turn_together():
    history = [
        {"role": "user", "content": "检查 cluster-001 的 CPU Ready"},
        {"role": "assistant", "content": "峰值为 26%，建议检查宿主机争抢。"},
    ]
    for index in range(7):
        history.extend([
            {"role": "user", "content": f"普通资源查询 {index}"},
            {"role": "assistant", "content": f"普通资源结果 {index}"},
        ])

    context = manage_context_window(
        history,
        current_message="继续分析 cluster-001",
    )

    assert context["relevant_messages"][:2] == history[:2]


def test_all_context_sections_share_one_hard_token_budget():
    history = []
    for index in range(30):
        resource_id = f"dcs-{'x' * 50}-{index}"
        history.extend([
            {
                "role": "user",
                "content": f"{resource_id} 容量告警：" + "容量趋势证据。" * 120,
            },
            {
                "role": "assistant",
                "content": f"{resource_id} 诊断结论：" + "建议继续观察。" * 120,
            },
        ])

    context = manage_context_window(
        history,
        current_message="继续分析这些资源的容量趋势：" + "下一步计划。" * 80,
    )

    assert context["estimated_tokens"] <= TOKEN_THRESHOLD


def test_responder_payload_includes_skill_and_tool_search_summaries_and_stays_under_cap():
    state = {
        "message": "查询 dcs-app-01 的性能",
        "conversation_summary": "",
        "working_context": {},
        "recent_messages": [],
        "loaded_skills": [{
            "id": "diagnose_vm_performance", "title": "diagnose vm performance", "version": 1,
            "tags": [], "summary": "先确认 VM 身份和宿主机", "detail": "详细步骤",
        }],
        "retrieved_cases": [{"title": "CPU Ready 过高历史案例", "content": "迁移同宿主机批处理 VM 后恢复"}],
        "skill_decision": {
            "decision": "use_skill", "skill_ids": ["diagnose_vm_performance"],
            "confidence": 0.9, "reason_summary": "用户要求诊断VM性能",
        },
        "tool_search_request": {"query": "get_vm_metrics", "top_k": 5, "required_capabilities": []},
        "tool_search_candidates": [{"tool_name": "get_vm_metrics", "score": 1.2}],
        "route_decisions": [],
        "plan": ["用户要求诊断VM性能"],
        "tool_results": [],
        "error": None,
    }
    payload = copilot._responder_payload(state)
    assert len(payload.encode("utf-8")) <= copilot.RESPONDER_PAYLOAD_CAP
    parsed = json.loads(payload)
    assert parsed["skill_decision"]["skill_ids"] == ["diagnose_vm_performance"]
    assert parsed["tool_search_request"]["query"] == "get_vm_metrics"
    assert parsed["tool_search_candidates"][0]["tool_name"] == "get_vm_metrics"
    assert parsed["retrieved_cases"][0]["title"] == "CPU Ready 过高历史案例"
