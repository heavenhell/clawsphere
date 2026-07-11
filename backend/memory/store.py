from __future__ import annotations

from typing import Any

from backend.memory.database import memory_db


def write_conversation_summary(task_id: str, summary: str, execution_log: list[dict[str, Any]]) -> dict[str, Any]:
    return memory_db.write_memory(task_id, summary, execution_log)


def list_memory_writes(limit: int = 50) -> list[dict[str, Any]]:
    return memory_db.list_memory_writes(limit)
