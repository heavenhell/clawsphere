from __future__ import annotations

from typing import Any, Callable
import re


MAX_FULL_TURNS = 6
MAX_FULL_MESSAGES = MAX_FULL_TURNS * 2
SUMMARY_MAX_CHARS = 1200
TOKEN_THRESHOLD = 3000


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    total = 0
    for item in messages:
        content = item.get("content", "")
        cjk = len(re.findall(r"[\u4e00-\u9fff]", content))
        non_cjk_words = len(re.findall(r"[A-Za-z0-9_.-]+", content))
        total += max(1, cjk + int(non_cjk_words * 1.3))
    return total


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
        # Stored history is authoritative, so recompute its older segment instead of
        # repeatedly appending the same turns to the persisted summary.
        summary = (summarizer or deterministic_summary)(older).strip()
        if len(summary) > SUMMARY_MAX_CHARS:
            recursive_input = [{"role": "system", "content": summary}]
            summary = (summarizer or deterministic_summary)(recursive_input).strip()
        if len(summary) > SUMMARY_MAX_CHARS:
            boundary = summary.rfind("\n", 0, SUMMARY_MAX_CHARS)
            summary = summary[:boundary if boundary > 0 else SUMMARY_MAX_CHARS].rstrip()
    return {
        "recent_messages": recent,
        "conversation_summary": summary,
        "older_message_count": len(older),
        "estimated_tokens": estimate_tokens(recent) + max(0, len(summary) // 3),
    }
