from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[2] / ".env")


DEEPSEEK_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")


def call_deepseek(system_prompt: str, user_prompt: str) -> str | None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    request = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


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
