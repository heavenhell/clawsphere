from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env")


DEEPSEEK_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def _invoke(payload: dict[str, Any]) -> dict[str, Any] | None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    timeout = httpx.Timeout(connect=5, read=30, write=10, pool=5)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(
                    DEEPSEEK_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                return response.json()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            last_error = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                break
            if attempt < 2:
                time.sleep(0.4 * (2 ** attempt))
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


def call_deepseek_tool_plan(
    message: str,
    intent: str,
    history: list[dict[str, str]],
    retrieved_docs: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any] | None:
    body = _invoke({
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是运维任务规划器。只根据用户明确表达的对象和动作选择工具；"
                    "缺少 VM、集群或告警标识时不要猜测资源，也不要调用工具。"
                    "查询可组合多个只读工具；写操作只提出工具调用，执行由护栏和审批控制。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "message": message,
                    "intent": intent,
                    "recent_history": history[-8:],
                    "skills": retrieved_docs[:3],
                }, ensure_ascii=False),
            },
        ],
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
    return {"tool_calls": calls, "reason": choice.get("content") or "LLM structured tool plan"}


def summarize_messages(messages: list[dict[str, str]]) -> str | None:
    return call_deepseek(
        "你负责压缩运维对话。保留资源标识、告警编号、执行结论、用户偏好和待处理事项，不超过300 token。",
        json.dumps(messages, ensure_ascii=False),
    )


def classify_intent_with_llm(message: str, history: list[dict[str, str]]) -> str | None:
    response = call_deepseek(
        """只输出一个意图标识，不要解释。允许值：smalltalk, alert_explain, resource_query,
capacity_forecast, vm_diagnosis, change_execute, config_modify, general。
只有用户明确要求执行重启、删除、迁移、扩容或修改配置时才输出写意图。""",
        json.dumps({"history": history[-6:], "message": message}, ensure_ascii=False),
    )
    if not response:
        return None
    intent = response.strip().strip("`").splitlines()[-1].strip()
    allowed = {
        "smalltalk", "alert_explain", "resource_query", "capacity_forecast",
        "vm_diagnosis", "change_execute", "config_modify", "general",
    }
    return intent if intent in allowed else None
