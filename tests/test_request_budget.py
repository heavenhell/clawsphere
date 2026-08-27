from __future__ import annotations

import json

import pytest

from backend.agent import request_budget


def _payload_with_content(content: str) -> dict:
    return {"messages": [{"role": "user", "content": content}]}


def test_exact_90_kib_body_is_allowed_without_compression():
    empty = _payload_with_content("")
    overhead = len(request_budget.compact_json_bytes(empty))
    payload = _payload_with_content("x" * (request_budget.MAX_REQUEST_BYTES - overhead))

    result = request_budget.enforce_request_budget(payload)

    assert result.final_bytes == request_budget.MAX_REQUEST_BYTES
    assert result.body == request_budget.compact_json_bytes(result.payload)
    assert result.compressed is False


def test_one_byte_over_limit_with_only_current_request_fails_closed():
    empty = _payload_with_content("")
    overhead = len(request_budget.compact_json_bytes(empty))
    payload = _payload_with_content(
        "x" * (request_budget.MAX_REQUEST_BYTES - overhead + 1)
    )

    with pytest.raises(request_budget.LLMPayloadTooLargeError, match="no compressible history"):
        request_budget.enforce_request_budget(payload)


def test_system_and_developer_messages_are_never_compressed():
    payload = {
        "messages": [
            {"role": "system", "content": "s" * request_budget.MAX_REQUEST_BYTES},
            {"role": "developer", "content": "fixed developer instruction"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current"},
        ],
    }
    captured = []

    with pytest.raises(request_budget.LLMPayloadTooLargeError, match="requested size target"):
        request_budget.enforce_request_budget(
            payload,
            compressor=lambda history: captured.extend(history) or "{}",
        )

    assert captured == [{"role": "assistant", "content": "old answer"}]


def test_oversized_history_is_semantically_compressed_below_target():
    current_request = "当前请求必须逐字保留：检查 vm-1001，错误码 E409"
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "固定系统安全规则"},
            {"role": "user", "content": "旧问题 " + "历史数据" * 15_000},
            {"role": "assistant", "content": "旧结论 vm-1001 / E409"},
            {"role": "user", "content": current_request},
        ],
        "tools": [{"type": "function", "function": {"name": "list_vms"}}],
    }
    captured_history = []

    def compressor(history):
        captured_history.extend(history)
        return json.dumps({
            "objective": "检查 vm-1001",
            "constraints": [],
            "decisions": [],
            "files": [],
            "completed_work": [],
            "tool_results": [],
            "errors": ["E409"],
            "pending_work": ["检查 vm-1001"],
            "exact_literals": ["vm-1001", "E409"],
        }, ensure_ascii=False)

    result = request_budget.enforce_request_budget(payload, compressor=compressor)

    assert result.compressed is True
    assert result.original_bytes > request_budget.MAX_REQUEST_BYTES
    assert result.final_bytes <= request_budget.TARGET_REQUEST_BYTES
    assert result.body == request_budget.compact_json_bytes(result.payload)
    assert result.payload["messages"][0]["content"] == "固定系统安全规则"
    assert result.payload["messages"][-1]["content"] == current_request
    assert captured_history[0]["content"].startswith("旧问题")
    assert all(item.get("content") != current_request for item in captured_history)


def test_long_compressor_output_is_trimmed_using_exact_utf8_size():
    payload = {
        "messages": [
            {"role": "system", "content": "fixed"},
            {"role": "assistant", "content": "旧数据" * 20_000},
            {"role": "user", "content": "current"},
        ],
    }

    result = request_budget.enforce_request_budget(
        payload,
        compressor=lambda history: "压缩摘要" * 30_000,
    )

    assert result.compressed is True
    assert result.final_bytes <= request_budget.TARGET_REQUEST_BYTES
    assert len(result.body) == result.final_bytes


def test_immutable_fields_may_exceed_target_but_never_hard_limit():
    payload = {
        "messages": [
            {"role": "system", "content": "s" * (83 * 1024)},
            {"role": "assistant", "content": "old" * 10_000},
            {"role": "user", "content": "current"},
        ],
    }

    result = request_budget.enforce_request_budget(
        payload,
        compressor=lambda history: "small semantic summary",
    )

    assert result.compressed is True
    assert request_budget.TARGET_REQUEST_BYTES < result.final_bytes
    assert result.final_bytes <= request_budget.MAX_REQUEST_BYTES


def test_compressor_failure_never_returns_an_oversized_request():
    payload = {
        "messages": [
            {"role": "system", "content": "fixed"},
            {"role": "assistant", "content": "x" * request_budget.MAX_REQUEST_BYTES},
            {"role": "user", "content": "current"},
        ],
    }

    def unavailable(history):
        raise RuntimeError("offline")

    with pytest.raises(request_budget.LLMPayloadTooLargeError, match="compression failed"):
        request_budget.enforce_request_budget(payload, compressor=unavailable)


def test_empty_compressor_output_fails_closed():
    payload = {
        "messages": [
            {"role": "system", "content": "fixed"},
            {"role": "assistant", "content": "x" * request_budget.MAX_REQUEST_BYTES},
            {"role": "user", "content": "current"},
        ],
    }

    with pytest.raises(request_budget.LLMPayloadTooLargeError, match="no summary"):
        request_budget.enforce_request_budget(payload, compressor=lambda history: None)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:11434/api/chat",
        "http://ollama.example/api/chat",
        "http://127.0.0.1:11434/other",
        "http://user:pass@127.0.0.1:11434/api/chat",
    ],
)
def test_semantic_compressor_rejects_non_loopback_or_nonstandard_urls(url):
    with pytest.raises(ValueError, match="loopback Ollama"):
        request_budget._validate_compressor_url(url)


def test_semantic_compressor_accepts_ipv4_ipv6_and_localhost_loopback():
    for url in (
        "http://127.0.0.1:11434/api/chat",
        "http://localhost:11434/api/chat",
        "http://[::1]:11434/api/chat",
    ):
        request_budget._validate_compressor_url(url)


def test_ollama_compressor_uses_structured_non_thinking_chat(monkeypatch):
    monkeypatch.setenv("DCS_LLM_SEMANTIC_COMPRESSION", "true")
    monkeypatch.setenv("DCS_LLM_COMPRESSOR_URL", request_budget.DEFAULT_COMPRESSOR_URL)
    monkeypatch.setenv("DCS_LLM_COMPRESSOR_MODEL", request_budget.DEFAULT_COMPRESSOR_MODEL)
    captured = {}
    summary = {
        "objective": "inspect vm-1001",
        "constraints": [],
        "decisions": [],
        "files": [],
        "completed_work": [],
        "tool_results": [],
        "errors": ["E409"],
        "pending_work": [],
        "exact_literals": ["vm-1001", "E409"],
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": json.dumps(summary)}}

    class CaptureClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs["json"]
            return FakeResponse()

    monkeypatch.setattr(request_budget.httpx, "Client", CaptureClient)
    result = request_budget._ollama_semantic_compressor([
        {"role": "assistant", "content": "vm-1001 failed with E409"},
    ])

    assert json.loads(result) == summary
    assert captured["url"] == request_budget.DEFAULT_COMPRESSOR_URL
    assert captured["json"]["model"] == "qwen3.5:4b-q4_K_M"
    assert captured["json"]["think"] is False
    assert captured["json"]["format"] == "json"
    assert captured["json"]["options"]["num_ctx"] == 32_768


def test_ollama_compressor_rejects_wrong_summary_schema(monkeypatch):
    monkeypatch.setenv("DCS_LLM_SEMANTIC_COMPRESSION", "true")
    monkeypatch.setenv("DCS_LLM_COMPRESSOR_URL", request_budget.DEFAULT_COMPRESSOR_URL)
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": '{"summary":"too vague"}'}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(request_budget.httpx, "Client", FakeClient)
    with pytest.raises(request_budget.SemanticCompressionError, match="invalid schema"):
        request_budget._ollama_semantic_compressor([
            {"role": "assistant", "content": "old"},
        ])
