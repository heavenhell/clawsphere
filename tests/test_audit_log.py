"""Tests for backend/agent/audit_log.py and its call sites in copilot.py.

Coverage is behaviour-focused, exercising only the public helpers and the two
call sites (llm_responder rejection logging + memory_writer full-turn logging):

  * grounding rejection / full session-turn records land as valid JSONL,
  * logs append (never overwrite) and never break the agent pipeline,
  * daily rotation gzip-compresses and removes the source,
  * expired archives are pruned while recent ones and the live file survive,
  * copilot writes the expected records on rejection and on a normal turn.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

import backend.agent.copilot as copilot
from backend.agent import audit_log
from backend.agent.audit_log import (
    MAX_AUDIT_RECORD_BYTES,
    _GzipDailyRotatingFileHandler,
    _format_session_text,
    _prune_expired,
    export_session,
    log_grounding_rejection,
    log_session_turn,
)
from backend.agent.copilot import llm_responder, memory_writer


@pytest.fixture
def isolated_logs(tmp_path, monkeypatch):
    """Point the audit-log paths at tmp_path and reset the logger cache so each
    test writes to a private directory and closes its handlers on teardown."""
    for logger in list(audit_log._loggers.values()):
        for handler in list(logger.handlers):
            handler.close()
        logger.handlers.clear()
    audit_log._loggers.clear()

    ground = tmp_path / "grounding_rejections.log"
    session = tmp_path / "session_turns.log"
    monkeypatch.setattr(audit_log, "LOG_DIR", tmp_path)
    monkeypatch.setattr(audit_log, "GROUNDING_LOG_FILE", ground)
    monkeypatch.setattr(audit_log, "SESSION_LOG_FILE", session)

    yield ground, session

    for logger in list(audit_log._loggers.values()):
        for handler in list(logger.handlers):
            handler.flush()
            handler.close()
        logger.handlers.clear()
    audit_log._loggers.clear()


def _read_jsonl(path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --- audit_log unit tests --------------------------------------------------


def test_log_grounding_rejection_writes_jsonl(isolated_logs):
    ground, _ = isolated_logs
    log_grounding_rejection(
        answer="被拒答案",
        reason="资源标识无法核实",
        attempt=2,
        conversation_id="c-1",
        task_id="t-1",
        claims=[{"resource_id": "dcs-app-01"}],
    )

    rows = _read_jsonl(ground)
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "资源标识无法核实"
    assert row["answer"] == "被拒答案"
    assert row["attempt"] == 2
    assert row["conversation_id"] == "c-1"
    assert row["task_id"] == "t-1"
    assert row["claims"] == [{"resource_id": "dcs-app-01"}]
    assert "ts" in row


def test_log_session_turn_writes_jsonl_with_ts(isolated_logs):
    _, session = isolated_logs
    log_session_turn({"conversation_id": "c-1", "message": "查询 dcs-app-01", "answer": "正常回答"})

    rows = _read_jsonl(session)
    assert len(rows) == 1
    row = rows[0]
    assert row["conversation_id"] == "c-1"
    assert row["message"] == "查询 dcs-app-01"
    assert row["answer"] == "正常回答"
    assert "ts" in row


def test_audit_log_redacts_sensitive_values(isolated_logs):
    _, session = isolated_logs
    secret = "audit-secret-sentinel"
    log_session_turn({
        "conversation_id": "c-secret",
        "message": f"authorization=Bearer {secret}; api_key={secret}",
        "answer": f"https://operator:{secret}@example.invalid/",
        "tool_results": [
            {
                "password": secret,
                "nested": {"accessSession": secret},
            }
        ],
    })

    raw = session.read_text(encoding="utf-8")
    row = _read_jsonl(session)[0]
    assert secret not in raw
    assert "[REDACTED]" in raw
    assert row["tool_results"][0]["password"] == "[REDACTED]"
    assert row["tool_results"][0]["nested"]["accessSession"] == "[REDACTED]"


def test_oversized_audit_record_is_compacted(isolated_logs):
    _, session = isolated_logs
    log_session_turn({
        "conversation_id": "c-large",
        "message": "查询" * MAX_AUDIT_RECORD_BYTES,
        "answer": "完成",
        "tool_results": [{"payload": "x" * MAX_AUDIT_RECORD_BYTES}],
    })

    raw = session.read_bytes()
    row = _read_jsonl(session)[0]
    assert len(raw) <= MAX_AUDIT_RECORD_BYTES + 1
    assert row["conversation_id"] == "c-large"
    assert row["answer"] == "完成"
    assert row["message"].endswith("…")
    assert row["_audit_truncated"]["original_bytes"] > MAX_AUDIT_RECORD_BYTES
    assert row["_audit_truncated"]["max_bytes"] == MAX_AUDIT_RECORD_BYTES
    assert "tool_results" in row["_audit_truncated"]["omitted_fields"]


def test_logs_append_instead_of_overwrite(isolated_logs):
    ground, _ = isolated_logs
    log_grounding_rejection(answer="a1", reason="r1", attempt=1)
    log_grounding_rejection(answer="a2", reason="r2", attempt=2)

    rows = _read_jsonl(ground)
    assert [r["answer"] for r in rows] == ["a1", "a2"]


def test_concurrent_first_writes_share_one_handler(isolated_logs):
    _, session = isolated_logs
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: log_session_turn({"message": f"m-{index}"}), range(32)))

    rows = _read_jsonl(session)
    assert len(rows) == 32
    assert {row["message"] for row in rows} == {f"m-{index}" for index in range(32)}
    assert len(audit_log._loggers["clawsphere.session_turns"].handlers) == 1


def test_logging_never_breaks_the_pipeline(monkeypatch):
    def boom(name, filename):
        raise OSError("disk full")

    monkeypatch.setattr(audit_log, "_get_rolling_logger", boom)
    audit_log._loggers.clear()

    # Neither helper may propagate the failure to the agent pipeline.
    log_grounding_rejection(answer="a", reason="r", attempt=1)
    log_session_turn({"message": "hi"})


def test_prune_expired_removes_old_keeps_recent_and_live(tmp_path):
    old = time.time() - 8 * 24 * 3600
    recent = time.time() - 2 * 24 * 3600
    for name, mtime in [
        ("session_turns.log.2026-08-01.gz", old),
        ("session_turns.log.2026-08-05.gz", old),
        ("session_turns.log.2026-08-15.gz", recent),
    ]:
        p = tmp_path / name
        p.write_bytes(b"x")
        os.utime(p, (mtime, mtime))

    live = tmp_path / "session_turns.log"
    live.write_text("live", encoding="utf-8")
    other = tmp_path / "other.log.2020-01-01.gz"
    other.write_bytes(b"x")
    os.utime(other, (old, old))

    _prune_expired(tmp_path, "session_turns.log", 7)

    assert not (tmp_path / "session_turns.log.2026-08-01.gz").exists()
    assert not (tmp_path / "session_turns.log.2026-08-05.gz").exists()
    assert (tmp_path / "session_turns.log.2026-08-15.gz").exists()
    assert live.exists()
    assert other.exists()  # non-matching prefix is untouched


def test_rotate_gzips_and_removes_source(tmp_path):
    src = tmp_path / "session_turns.log"
    src.write_text("line1\nline2\n", encoding="utf-8")
    dest = tmp_path / "session_turns.log.2026-08-10.gz"

    handler = _GzipDailyRotatingFileHandler(
        str(src), when="midnight", interval=1, backupCount=0, encoding="utf-8", delay=True
    )
    handler.rotate(str(src), str(dest))

    assert not src.exists()
    with gzip.open(dest, "rt", encoding="utf-8") as f:
        assert f.read() == "line1\nline2\n"


# --- copilot call-site tests -----------------------------------------------


def test_llm_responder_records_rejected_answers(monkeypatch):
    rejected = []
    monkeypatch.setattr(
        copilot, "call_deepseek_json",
        lambda *a, **k: {"answer": "被拒答案", "resource_claims": [{"resource_id": "dcs-1", "claim": "x"}]},
    )
    monkeypatch.setattr(
        copilot, "verify_resource_claims",
        lambda answer, claims, tool_results: (False, "资源标识无法核实"),
    )
    monkeypatch.setattr(copilot, "_responder_payload", lambda state, feedback: "{}")
    monkeypatch.setattr(copilot, "get_public_llm_status", lambda: {"configured": True})
    monkeypatch.setattr(copilot, "get_last_llm_usage", lambda: None)
    monkeypatch.setattr(copilot, "log_grounding_rejection", lambda **kw: rejected.append(kw))

    result = cast(Any, llm_responder({}))

    assert result["response_source"] == "grounding_guard"
    assert result["fallback_reason"] == "grounding_rejected"

    decisions = result["route_decisions"]
    assert len(decisions) == 3
    for i, d in enumerate(decisions, 1):
        assert d["stage"] == "llm_responder"
        assert d["decision"] == "grounding_rejected"
        assert d["reason"] == "资源标识无法核实"
        assert d["detail"]["answer"] == "被拒答案"
        assert d["detail"]["attempt"] == i

    assert len(rejected) == 3
    assert rejected[0]["answer"] == "被拒答案"


def test_memory_writer_logs_full_turn(monkeypatch):
    captured = {}
    monkeypatch.setattr(copilot, "write_conversation_summary", lambda *a, **k: None)
    monkeypatch.setattr(copilot, "log_session_turn", lambda record: captured.update(record))

    class _MemDB:
        def append_turn(self, *a, **k):
            pass

    monkeypatch.setattr(copilot, "memory_db", _MemDB())

    state = {
        "task_id": "task-1",
        "user_id": "u-1",
        "tenant_id": "t-1",
        "conversation_id": "c-1",
        "message": "查询 dcs-app-01",
        "final_response": "正常回答",
        "intent": "tool_execution",
        "response_source": "deepseek",
        "fallback_reason": None,
        "route_decisions": [{"stage": "skill_router", "decision": "use_skill"}],
        "llm_stage_metrics": [{"stage": "skill_router"}],
        "tool_calls_proposed": [{"tool_name": "get_vm_metrics"}],
        "tool_results": [{"tool_name": "get_vm_metrics"}],
        "resource_claims": [{"resource_id": "dcs-app-01"}],
        "skill_decision": {"decision": "use_skill"},
        "plan": ["诊断"],
        "agent_step_count": 2,
        "execution_log": [],
        "conversation_summary": "",
    }

    result = memory_writer(cast(Any, state))

    assert result == {"summary": "message=查询 dcs-app-01; tools=['get_vm_metrics']"}
    assert captured["conversation_id"] == "c-1"
    assert captured["message"] == "查询 dcs-app-01"
    assert captured["answer"] == "正常回答"
    assert captured["response_source"] == "deepseek"
    assert captured["route_decisions"] == [{"stage": "skill_router", "decision": "use_skill"}]
    assert captured["tool_calls"] == [{"tool_name": "get_vm_metrics"}]
    assert captured["tool_results"] == [{"tool_name": "get_vm_metrics"}]
    assert captured["agent_step_count"] == 2


# --- session export tests ---------------------------------------------------


def _dump_jsonl(path, records):
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def test_export_session_filters_and_sorts(tmp_path):
    log_file = tmp_path / "session_turns.log"
    _dump_jsonl(log_file, [
        {"ts": "2026-08-18T10:00:00+00:00", "conversation_id": "c-1", "message": "第二轮"},
        {"ts": "2026-08-18T09:00:00+00:00", "conversation_id": "c-2", "message": "别人的"},
        {"ts": "2026-08-18T08:00:00+00:00", "conversation_id": "c-1", "message": "第一轮"},
    ])

    result = export_session("c-1", log_file)

    assert [r["message"] for r in result] == ["第一轮", "第二轮"]
    assert all(r["conversation_id"] == "c-1" for r in result)


def test_export_session_reads_gzip_archives(tmp_path):
    log_file = tmp_path / "session_turns.log"
    _dump_jsonl(log_file, [
        {"ts": "2026-08-19T10:00:00+00:00", "conversation_id": "c-1", "message": "今天"},
    ])
    archive = tmp_path / "session_turns.log.2026-08-18.gz"
    with gzip.open(archive, "wt", encoding="utf-8") as f:
        f.write(json.dumps(
            {"ts": "2026-08-18T09:00:00+00:00", "conversation_id": "c-1", "message": "昨天"},
            ensure_ascii=False,
        ) + "\n")

    result = export_session("c-1", log_file)

    assert [r["message"] for r in result] == ["昨天", "今天"]


def test_export_session_skips_corrupt_lines(tmp_path):
    log_file = tmp_path / "session_turns.log"
    log_file.write_text(
        '{"ts":"2026-08-18T09:00:00+00:00","conversation_id":"c-1","message":"ok"}\n'
        "not-json\n"
        '{"broken": \n'
        "[]\n"
        '{"ts":"2026-08-18T10:00:00+00:00","conversation_id":"c-1","message":"also ok"}\n',
        encoding="utf-8",
    )

    result = export_session("c-1", log_file)

    assert [r["message"] for r in result] == ["ok", "also ok"]


def test_export_session_requires_non_empty_id(tmp_path):
    with pytest.raises(ValueError):
        export_session("", tmp_path / "session_turns.log")


def test_export_session_missing_file_returns_empty(tmp_path):
    assert export_session("c-1", tmp_path / "nope.log") == []


def test_format_session_text_includes_dialogue():
    records = [
        {
            "ts": "2026-08-18T09:00:00+00:00",
            "task_id": "t-1",
            "intent": "tool_execution",
            "response_source": "deepseek",
            "message": "查询 dcs-app-01",
            "answer": "正常回答",
        },
    ]
    text = _format_session_text(records)
    assert "[user]" in text
    assert "[assistant]" in text
    assert "查询 dcs-app-01" in text
    assert "正常回答" in text
    assert "tool_execution" in text


def test_format_session_text_verbose_includes_route_decisions():
    records = [
        {
            "ts": "2026-08-18T09:00:00+00:00",
            "conversation_id": "c-1",
            "message": "hi",
            "answer": "a",
            "route_decisions": [{"stage": "llm_responder", "decision": "grounding_rejected"}],
        },
    ]
    text = _format_session_text(records, verbose=True)
    assert "-- route_decisions --" in text
    assert "grounding_rejected" in text


def test_format_session_text_empty():
    assert _format_session_text([]) == "(no records found)"


def test_main_json_output(tmp_path, capsys):
    log_file = tmp_path / "session_turns.log"
    _dump_jsonl(log_file, [
        {"ts": "2026-08-18T09:00:00+00:00", "conversation_id": "c-1", "message": "hi"},
    ])

    rc = audit_log.main(["c-1", "--log", str(log_file), "--format", "json"])
    out = capsys.readouterr().out

    assert rc == 0
    assert json.loads(out)[0]["message"] == "hi"
