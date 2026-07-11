from backend.memory.context_manager import manage_context_window
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


def test_hybrid_retrieval_finds_skill_and_history_case():
    skills = retrieve("dcs-app-01 变慢 CPU Ready", ["readonly"], top_k=2)
    cases = retrieve_history("虚拟机 CPU Ready 过高", ["readonly"], top_k=1)
    assert skills[0]["id"].startswith("skill:diagnose_vm_performance")
    assert cases[0]["doc_type"] == "alert_case"
    assert "bm25_score" in skills[0] and "vector_score" in skills[0]


def test_conversation_survives_database_reopen(tmp_path):
    path = tmp_path / "memory.db"
    first = MemoryDatabase(path)
    first.append_turn("c-1", "u-1", "t-1", "问题", "回答", "摘要")
    second = MemoryDatabase(path)
    history, summary = second.load_conversation("c-1")
    assert summary == "摘要"
    assert history[-1] == {"role": "assistant", "content": "回答"}


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
