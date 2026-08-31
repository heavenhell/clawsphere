import json
from inspect import signature

from backend.memory.context_manager import (
    HISTORY_TARGET_TOKENS,
    PRESERVE_RECENT_TOKENS,
    TOKEN_THRESHOLD,
    estimate_tokens,
    manage_context_window,
)
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


def _long_history(turns: int, chars_per_message: int = 300, prefix: str = ""):
    """Build a history whose size is predictable in estimated tokens (CJK is
    counted per character), so tests can sit deliberately above or below
    TOKEN_THRESHOLD."""
    history = []
    for index in range(turns):
        history.extend([
            {"role": "user", "content": f"{prefix}第{index}轮问题：" + "容量告警排查记录。" * (chars_per_message // 9)},
            {"role": "assistant", "content": f"第{index}轮结论：" + "已核实并持续观察。" * (chars_per_message // 9)},
        ])
    return history


def test_short_conversation_is_carried_verbatim_without_calling_the_summarizer():
    history = []
    for index in range(8):
        history.extend([
            {"role": "user", "content": f"第 {index} 轮问题"},
            {"role": "assistant", "content": f"第 {index} 轮回答"},
        ])
    calls = []

    def tracking_summarizer(messages):
        calls.append(messages)
        return "不该被调用"

    context = manage_context_window(history, summarizer=tracking_summarizer)

    # The whole point of triggered compaction: a short conversation costs zero
    # summarizer calls and loses nothing.
    assert calls == []
    assert context["compacted"] is False
    assert len(context["recent_messages"]) == len(history)
    assert context["older_message_count"] == 0
    assert context["conversation_summary"] == ""


def test_compaction_fires_once_the_conversation_exceeds_the_threshold():
    history = _long_history(turns=20)
    assert estimate_tokens(history) > TOKEN_THRESHOLD

    context = manage_context_window(history, summarizer=lambda messages: "压缩后的摘要")

    assert context["compacted"] is True
    assert context["older_message_count"] > 0
    assert context["conversation_summary"] == "压缩后的摘要"
    assert estimate_tokens(context["recent_messages"]) <= PRESERVE_RECENT_TOKENS
    assert context["estimated_tokens"] <= HISTORY_TARGET_TOKENS


def test_compaction_merges_the_previous_summary_instead_of_recomputing_it():
    history = _long_history(turns=20)
    seen = {}

    def summarizer(messages):
        seen["payload"] = messages
        return "合并后的摘要"

    manage_context_window(history, existing_summary="上一轮的摘要", summarizer=summarizer)

    # The prior summary must enter as input, not be silently discarded.
    assert "上一轮的摘要" in seen["payload"][0]["content"]


def test_watermark_advances_only_when_compaction_runs():
    short = [{"id": index, "role": "user", "content": f"短消息{index}"} for index in range(4)]
    context = manage_context_window(short, watermark=7, summarizer=lambda m: "x")
    assert context["new_watermark"] == 7

    history = [
        {"id": index, "role": "user" if index % 2 == 0 else "assistant", "content": "容量告警排查记录。" * 40}
        for index in range(40)
    ]
    context = manage_context_window(history, watermark=7, summarizer=lambda m: "摘要")
    assert context["new_watermark"] > 7
    assert context["new_watermark"] == history[context["older_message_count"] - 1]["id"]


def test_compaction_failure_falls_back_to_full_history_without_advancing_watermark():
    history = _long_history(turns=20)

    def failing_summarizer(messages):
        raise RuntimeError("compressor unavailable")

    context = manage_context_window(history, watermark=3, summarizer=failing_summarizer)

    # Losing the compressor must never lose context or fail the turn.
    assert context["compacted"] is False
    assert context["compaction_skipped_reason"] == "compaction_failed"
    assert len(context["recent_messages"]) == len(history)
    assert context["new_watermark"] == 3


def test_message_ids_never_reach_the_model():
    history = [{"id": 1, "role": "user", "content": "查询 vm-1001"}]
    context = manage_context_window(history)
    assert context["recent_messages"] == [{"role": "user", "content": "查询 vm-1001"}]


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
    history, summary, watermark = second.load_conversation("c-1", "u-1", "t-1")
    assert summary == "摘要"
    assert watermark == 0
    assert history[-1]["role"] == "assistant"
    assert history[-1]["content"] == "回答"


def test_watermark_persists_and_hides_already_summarized_messages(tmp_path):
    database = MemoryDatabase(tmp_path / "watermark.db")
    database.append_turn("c-2", "u-1", "t-1", "第一轮问题", "第一轮回答", "")
    history, _, _ = database.load_conversation("c-2", "u-1", "t-1")
    first_turn_last_id = history[-1]["id"]

    database.append_turn(
        "c-2", "u-1", "t-1", "第二轮问题", "第二轮回答", "已压缩摘要",
        summarized_upto_id=first_turn_last_id,
    )
    history, summary, watermark = database.load_conversation("c-2", "u-1", "t-1")

    assert watermark == first_turn_last_id
    assert summary == "已压缩摘要"
    # Everything at or below the watermark is represented by the summary only.
    assert [item["content"] for item in history] == ["第二轮问题", "第二轮回答"]


def test_non_compacting_turn_never_rewinds_the_watermark(tmp_path):
    database = MemoryDatabase(tmp_path / "no-rewind.db")
    database.append_turn("c-3", "u-1", "t-1", "问题一", "回答一", "")
    history, _, _ = database.load_conversation("c-3", "u-1", "t-1")
    database.append_turn(
        "c-3", "u-1", "t-1", "问题二", "回答二", "摘要",
        summarized_upto_id=history[-1]["id"],
    )
    _, _, advanced = database.load_conversation("c-3", "u-1", "t-1")

    database.append_turn("c-3", "u-1", "t-1", "问题三", "回答三", "摘要")
    _, _, after = database.load_conversation("c-3", "u-1", "t-1")

    assert after == advanced


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


def test_agent_carries_short_conversations_without_producing_a_summary():
    conversation_id = "summary-test"
    for index in range(8):
        run_copilot(f"查询第 {index} 轮资源", conversation_id=conversation_id)
    result = run_copilot("有多少虚拟机", conversation_id=conversation_id)
    # Eight short turns sit far below the threshold, so nothing is compacted
    # and no summarizer call was spent.
    assert result["summary"] == ""
    assert result["context"]["compacted"] is False


def test_chinese_token_estimate_and_summary_boundary():
    messages = [{"role": "user", "content": "这是十个中文字符测试文本"}]
    assert estimate_tokens(messages) >= 10
    long_history = _long_history(turns=24)
    context = manage_context_window(long_history)
    assert context["compacted"] is True
    assert estimate_tokens([
        {"role": "system", "content": context["conversation_summary"]}
    ]) <= HISTORY_TARGET_TOKENS - PRESERVE_RECENT_TOKENS
    assert not context["conversation_summary"].endswith("第")


def test_long_ascii_identifier_token_estimate_scales_with_length():
    message = [{"role": "user", "content": "dcs-" + "x" * 100}]
    assert estimate_tokens(message) >= 25


def test_a_few_very_large_turns_still_trigger_compaction():
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"第{index}条：" + "容量告警诊断证据。" * 180,
        }
        for index in range(8)
    ]

    context = manage_context_window(history, summarizer=lambda messages: "摘要")

    # Only 8 messages, but each is huge — the trigger is tokens, not turn count.
    assert context["compacted"] is True
    assert context["older_message_count"] > 0
    assert context["estimated_tokens"] <= TOKEN_THRESHOLD


def test_context_api_accepts_current_message_for_relevance():
    assert "current_message" in signature(manage_context_window).parameters


def test_working_context_survives_compaction_intact():
    history = [
        {"role": "user", "content": "检查 cluster-001 的容量风险"},
        {"role": "assistant", "content": "cluster-001 当前需要持续观察。"},
        *_long_history(turns=20),
    ]

    context = manage_context_window(
        history,
        current_message="继续分析 cluster-001 的容量趋势",
        summarizer=lambda messages: "摘要",
    )

    # The resource pointer is the one thing compaction must never lose: the
    # originating turn is now inside the summary, but the ID is still exact.
    assert context["compacted"] is True
    assert context["working_context"]["active_resource_ids"] == ["cluster-001"]
    assert context["working_context"]["latest_user_request"] == "继续分析 cluster-001 的容量趋势"
    assert context["working_context"]["history_message_count"] == len(history)
    assert context["estimated_tokens"] <= TOKEN_THRESHOLD


def test_relevant_older_evidence_is_pulled_back_after_compaction():
    history = [
        {"role": "user", "content": "检查 cluster-001 的容量风险"},
        {"role": "assistant", "content": "cluster-001 当前需要持续观察。"},
        *_long_history(turns=20),
    ]

    context = manage_context_window(
        history,
        current_message="继续分析 cluster-001 的容量趋势",
        summarizer=lambda messages: "摘要",
    )

    assert any(
        "cluster-001" in item["content"]
        for item in context["relevant_messages"]
    )


def test_relevant_history_keeps_user_and_assistant_turn_together():
    history = [
        {"role": "user", "content": "检查 cluster-001 的 CPU Ready"},
        {"role": "assistant", "content": "峰值为 26%，建议检查宿主机争抢。"},
        *_long_history(turns=20),
    ]

    context = manage_context_window(
        history,
        current_message="继续分析 cluster-001",
        summarizer=lambda messages: "摘要",
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
        summarizer=lambda messages: "摘要",
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
