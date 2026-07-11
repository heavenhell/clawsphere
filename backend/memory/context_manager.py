from __future__ import annotations

from typing import Any, Callable


MAX_FULL_TURNS = 6
MAX_FULL_MESSAGES = MAX_FULL_TURNS * 2
SUMMARY_MAX_CHARS = 1200
TOKEN_THRESHOLD = 3000


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    return sum(max(1, len(item.get("content", "")) // 3) for item in messages)


def deterministic_summary(messages: list[dict[str, str]]) -> str:
    lines = []
    for item in messages[-18:]:
        role = "用户" if item.get("role") == "user" else "助手"
        content = item.get("content", "").replace("\n", " ").strip()
        if content:
            lines.append(f"{role}: {content[:180]}")
    return "\n".join(lines)


def manage_context_window(
    history: list[dict[str, str]],
    existing_summary: str = "",
    summarizer: Callable[[list[dict[str, str]]], str] | None = None,
) -> dict[str, Any]:
    recent = history[-MAX_FULL_MESSAGES:]
    older = history[:-MAX_FULL_MESSAGES]
    summary = existing_summary.strip()
    should_compress = bool(older) or estimate_tokens(history) > TOKEN_THRESHOLD
    if should_compress and older:
        addition = (summarizer or deterministic_summary)(older)
        summary = "\n".join(part for part in [summary, addition] if part).strip()
        summary = summary[-SUMMARY_MAX_CHARS:]
    return {
        "recent_messages": recent,
        "conversation_summary": summary,
        "older_message_count": len(older),
        "estimated_tokens": estimate_tokens(recent) + max(0, len(summary) // 3),
    }


def split_context(history: list[dict[str, str]], existing_summary: str = "") -> dict[str, Any]:
    return manage_context_window(history, existing_summary)
