from backend.memory.context_manager import estimate_tokens, manage_context_window
from backend.memory.database import MemoryDatabase
from backend.memory.retriever import retrieve, retrieve_history
from backend.skills.loader import load_all_skills, startup_skill_summaries
from backend.agent.copilot import run_copilot


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
    history = []
    for index in range(7):
        history.extend([
            {"role": "user", "content": f"查询第 {index} 轮资源"},
            {"role": "assistant", "content": f"第 {index} 轮资源结果"},
        ])
    result = run_copilot("有多少虚拟机", history=history, conversation_id="summary-test")
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
