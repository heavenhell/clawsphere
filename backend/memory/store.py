from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


MEMORY_WRITES: list[dict[str, Any]] = []


def write_conversation_summary(task_id: str, summary: str, execution_log: list[dict[str, Any]]) -> dict[str, Any]:
    record = {
        "id": f"memory-{len(MEMORY_WRITES) + 1:04d}",
        "task_id": task_id,
        "summary": summary,
        "execution_log_count": len(execution_log),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    MEMORY_WRITES.append(record)
    return record
