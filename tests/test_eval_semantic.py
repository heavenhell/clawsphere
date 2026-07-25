"""Offline, deterministic tests for the eval scorer and the safety invariant.
These do not need a live LLM."""
from eval.semantic import backend_name, similarity
from eval.evaluator import _executed_writes


def test_similarity_ranks_relevant_answer_above_unrelated():
    reference = "critical 是最高的告警严重级别，需要立即处置。"
    good = "critical 表示最高级别的严重告警，必须立即处理。"
    bad = "今天天气不错，适合出去散步。"
    assert similarity(good, reference) > similarity(bad, reference)
    assert similarity(reference, reference) >= 0.99


def test_backend_name_is_reported():
    assert backend_name() in {"sentence-transformers", "lexical-fallback"}


def test_safety_invariant_detects_executed_writes():
    blocked = {"tool_results": [{"tool_name": "restart_vm", "success": False}]}
    executed = {"tool_results": [{"tool_name": "restart_vm", "success": True}]}
    assert _executed_writes(blocked) == []
    assert _executed_writes(executed) == ["restart_vm"]
