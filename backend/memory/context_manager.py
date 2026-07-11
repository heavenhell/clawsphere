from __future__ import annotations

from typing import Any


MAX_FULL_TURNS = 6
MAX_FULL_MESSAGES = MAX_FULL_TURNS * 2


def split_context(history: list[dict[str, str]], existing_summary: str = "") -> dict[str, Any]:
    recent = history[-MAX_FULL_MESSAGES:]
    older = history[:-MAX_FULL_MESSAGES]
    summary = existing_summary.strip()
    if older:
        additions = []
        for item in older[-12:]:
            role = item.get("role", "unknown")
            content = item.get("content", "").replace("\n", " ")
            additions.append(f"{role}: {content[:180]}")
        summary = (summary + "\n" if summary else "") + "\n".join(additions)
        summary = summary[-1200:]
    return {
        "recent_messages": recent,
        "conversation_summary": summary,
        "older_message_count": len(older),
    }
