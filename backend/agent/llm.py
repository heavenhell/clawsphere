from __future__ import annotations

import json
import os
import re
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env")


DEEPSEEK_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
MAX_LLM_PAYLOAD_BYTES = 64 * 1024
MAX_LLM_MESSAGE_CHARS = 16_000


class LLMPayloadTooLargeError(ValueError):
    pass

_status_lock = threading.Lock()
_request_status: ContextVar[dict[str, Any] | None] = ContextVar(
    "dcs_llm_request_status",
    default=None,
)
_last_usage: ContextVar[dict[str, Any] | None] = ContextVar(
    "dcs_llm_last_usage",
    default=None,
)


def get_last_llm_usage() -> dict[str, Any] | None:
    """Token usage from the most recent successful _invoke() call on this
    request, or None — DeepSeek's response `usage` field isn't guaranteed by
    every backend/mock, so callers must degrade gracefully rather than assert
    it's present."""
    return _last_usage.get()


def _initial_status() -> dict[str, Any]:
    configured = bool(os.getenv("DEEPSEEK_API_KEY"))
    return {
        "configured": configured,
        "model": DEEPSEEK_MODEL,
        "status": "unknown" if configured else "not_configured",
        "last_attempt_at": None,
        "last_error_class": None,
        "last_status_code": None,
    }


_runtime_status: dict[str, Any] = _initial_status()


def _record_status(
    status: str,
    *,
    error_class: str | None = None,
    status_code: int | None = None,
) -> None:
    recorded = {
        "configured": bool(os.getenv("DEEPSEEK_API_KEY")),
        "model": DEEPSEEK_MODEL,
        "status": status,
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        "last_error_class": error_class,
        "last_status_code": status_code,
    }
    _request_status.set(recorded)
    with _status_lock:
        _runtime_status.update(recorded)


def reset_llm_request_status() -> None:
    _request_status.set(_initial_status())


def get_llm_status(*, aggregate: bool = False) -> dict[str, Any]:
    request_status = _request_status.get()
    if request_status is not None and not aggregate:
        status = dict(request_status)
    else:
        with _status_lock:
            status = dict(_runtime_status)
    status["configured"] = bool(os.getenv("DEEPSEEK_API_KEY"))
    status["model"] = DEEPSEEK_MODEL
    return status


def get_public_llm_status(*, aggregate: bool = False) -> dict[str, Any]:
    status = get_llm_status(aggregate=aggregate)
    return {
        "configured": status["configured"],
        "model": status["model"],
        "status": status["status"],
    }


def _validate_deepseek_url() -> None:
    parsed = urlparse(DEEPSEEK_URL)
    valid = (
        parsed.scheme == "https"
        and parsed.hostname == "api.deepseek.com"
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"/chat/completions", "/v1/chat/completions"}
    )
    if not valid:
        raise ValueError("DeepSeek API URL must be an approved api.deepseek.com HTTPS endpoint")


_SENSITIVE_NAME = (
    r"(?:authorization|accesssession|apikey|clientsecret|refreshtoken|"
    r"accesstoken|authtoken|sessionid|privatekey|x-auth-token|"
    r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|secret|token|session(?:[_-]?id)?|"
    r"password|passwd|private[_-]?key|cookie|credential))"
)
_SENSITIVE_KEY = re.compile(_SENSITIVE_NAME, re.I)
_TEXT_ASSIGNMENT = re.compile(
    r"(?i)\b(?P<key>[a-z][a-z0-9_.-]{0,127})"
    r"(?P<separator>\s*=\s*|\s*:\s*(?!//))(?:bearer\s+)?"
    r"""(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)"""
)
_JSON_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<prefix>["'](?P<key>[a-z][a-z0-9_.-]{0,127})["']\s*:\s*)
    (?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_URL_USERINFO = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)"
    r"(?P<username>[^/\s:@]+):(?P<password>[^@/\s]+)@"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.I | re.S,
)
_SENSITIVE_MARKERS = (
    "authorization",
    "accesssession",
    "api_key",
    "api-key",
    "apikey",
    "clientsecret",
    "refreshtoken",
    "accesstoken",
    "authtoken",
    "sessionid",
    "privatekey",
    "secret",
    "token",
    "session",
    "password",
    "passwd",
    "private_key",
    "private-key",
    "cookie",
    "credential",
    "bearer ",
    "private key",
)


def _redact_text(value: str) -> str:
    value = _URL_USERINFO.sub(
        lambda match: f'{match.group("scheme")}{match.group("username")}:[REDACTED]@',
        value,
    )
    lowered = value.lower()
    if not any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return value
    value = _PRIVATE_KEY_BLOCK.sub("[REDACTED PRIVATE KEY]", value)
    value = _JSON_ASSIGNMENT.sub(
        lambda match: (
            f'{match.group("prefix")}"[REDACTED]"'
            if _is_sensitive_key(match.group("key"))
            else match.group(0)
        ),
        value,
    )
    value = _TEXT_ASSIGNMENT.sub(
        lambda match: (
            f'{match.group("key")}{match.group("separator")}[REDACTED]'
            if _is_sensitive_key(match.group("key"))
            else match.group(0)
        ),
        value,
    )
    return _BEARER_TOKEN.sub("Bearer [REDACTED]", value)


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    for wrapper_suffix in ("value", "field"):
        if normalized.endswith(wrapper_suffix):
            normalized = normalized[:-len(wrapper_suffix)]
    return normalized.endswith((
        "authorization",
        "apikey",
        "secret",
        "secretkey",
        "secretaccesskey",
        "token",
        "session",
        "sessionid",
        "password",
        "passwd",
        "privatekey",
        "cookie",
        "credential",
    ))


def _sanitize_for_llm(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(str(key)) else _sanitize_for_llm(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_for_llm(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _prepare_payload(payload: dict[str, Any]) -> dict[str, Any]:
    prepared = _sanitize_for_llm(payload)
    for message in prepared.get("messages", []):
        content = message.get("content")
        if isinstance(content, str) and len(content) > MAX_LLM_MESSAGE_CHARS:
            message["content"] = content[:MAX_LLM_MESSAGE_CHARS].rstrip() + "…"
    encoded = json.dumps(prepared, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_LLM_PAYLOAD_BYTES:
        raise LLMPayloadTooLargeError("LLM payload exceeds the configured outbound size limit")
    return prepared


def _validate_response_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("response body is not an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("response choices are missing")
    if not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
        raise ValueError("response message is missing")
    message = choices[0]["message"]
    content = message.get("content")
    tool_calls = message.get("tool_calls")
    valid_tool_calls = False
    if tool_calls is not None:
        if not isinstance(tool_calls, list) or not tool_calls:
            raise ValueError("response tool calls are invalid")
        for item in tool_calls:
            function = item.get("function") if isinstance(item, dict) else None
            if (
                not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not function["name"].strip()
                or not isinstance(function.get("arguments"), str)
            ):
                raise ValueError("response tool call is invalid")
        valid_tool_calls = True
    if content is not None and not isinstance(content, str):
        raise ValueError("response content is not text")
    if (content is None or not content.strip()) and not valid_tool_calls:
        raise ValueError("response has no usable content")
    return body


def _invoke(payload: dict[str, Any]) -> dict[str, Any] | None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        _record_status("not_configured")
        return None
    try:
        _validate_deepseek_url()
    except ValueError as exc:
        _record_status("misconfigured", error_class=type(exc).__name__)
        raise
    try:
        prepared_payload = _prepare_payload(payload)
    except LLMPayloadTooLargeError as exc:
        _record_status("degraded", error_class=type(exc).__name__)
        raise
    # Allow enough read time for a full structured answer without a premature
    # timeout, but bounded so a rare failure doesn't stall the turn for minutes.
    timeout = httpx.Timeout(connect=5, read=45, write=10, pool=5)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    DEEPSEEK_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=prepared_payload,
                )
                response.raise_for_status()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            last_error = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                break
            if attempt < 2:
                time.sleep(0.4 * (2 ** attempt))
            continue
        try:
            body = _validate_response_body(response.json())
        except (TypeError, ValueError, KeyError) as exc:
            _record_status(
                "degraded",
                error_class=type(exc).__name__,
                status_code=response.status_code,
            )
            raise RuntimeError("DeepSeek returned an invalid response") from exc
        _record_status("healthy", status_code=response.status_code)
        usage = body.get("usage")
        _last_usage.set(usage if isinstance(usage, dict) else None)
        return body
    response = getattr(last_error, "response", None)
    _record_status(
        "degraded",
        error_class=type(last_error).__name__ if last_error else "UnknownError",
        status_code=getattr(response, "status_code", None),
    )
    raise RuntimeError("DeepSeek request failed after retries") from last_error


def call_deepseek(system_prompt: str, user_prompt: str) -> str | None:
    body = _invoke({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    })
    if not body:
        return None
    return body["choices"][0]["message"].get("content")


def call_deepseek_json(system_prompt: str, user_prompt: str, max_tokens: int = 1200) -> dict[str, Any] | None:
    """Structured-output call. Returns the parsed JSON object the model emitted,
    or None when the LLM is not configured. Raises on transport failure so the
    caller can decide how to degrade. The schema is described in the prompt and
    enforced with DeepSeek JSON mode; the caller still validates the shape."""
    body = _invoke({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    })
    if not body:
        return None
    content = body["choices"][0]["message"].get("content") or ""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def call_deepseek_agent_plan(
    system_prompt: str,
    history: list[dict[str, str]],
    tools: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """LLM-native router: the model decides which tools (if any) to call from the
    full conversation. No heuristic intent is supplied — routing is the model's
    job. Returns {"tool_calls": [...], "reason": str} or None when unconfigured."""
    messages = [{"role": "system", "content": system_prompt}, *history]
    body = _invoke({
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0,
        "max_tokens": 1000,
    })
    if not body:
        return None
    choice = body["choices"][0]["message"]
    calls = []
    for item in choice.get("tool_calls") or []:
        function = item.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            continue
        calls.append({"tool_name": function.get("name"), "params": arguments})
    return {"tool_calls": calls, "reason": choice.get("content") or ""}


def summarize_messages(messages: list[dict[str, str]]) -> str | None:
    return call_deepseek(
        "你负责压缩运维对话。保留资源标识、告警编号、执行结论、用户偏好和待处理事项，不超过300 token。",
        json.dumps(messages, ensure_ascii=False),
    )


def classify_intent_with_llm(message: str, history: list[dict[str, str]]) -> str | None:
    response = call_deepseek(
        """只输出一个意图标识，不要解释。允许值：smalltalk, alert_explain, resource_query,
 capacity_forecast, vm_diagnosis, ops_concept_explain, change_execute, config_modify, general。
 用户追问运维技术术语的含义、原理或“刚才说的某个词是什么意思”时，
 输出 ops_concept_explain。
只有用户明确要求执行重启、删除、迁移、扩容或修改配置时才输出写意图。""",
        json.dumps({"history": history[-6:], "message": message}, ensure_ascii=False),
    )
    if not response:
        return None
    intent = response.strip().strip("`").splitlines()[-1].strip()
    allowed = {
        "smalltalk", "alert_explain", "resource_query", "capacity_forecast",
        "vm_diagnosis", "ops_concept_explain", "change_execute", "config_modify", "general",
    }
    return intent if intent in allowed else None
