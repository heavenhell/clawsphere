from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from backend.memory.database import memory_db


class ApprovalStore:
    def create_or_get(
        self,
        task_id: str,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        description: str,
        tool_calls: list[dict[str, Any]],
        risk: str,
        resume_required: bool = True,
    ) -> dict[str, Any]:
        existing = self.get_by_task(task_id)
        if existing:
            if existing["user_id"] != user_id or existing["tenant_id"] != tenant_id:
                raise PermissionError("task id belongs to another user or tenant")
            if existing["tool_calls"] != tool_calls or existing["resume_required"] != resume_required:
                raise ValueError("task id already exists with a different approval payload")
            return existing
        now = datetime.now(timezone.utc).isoformat()
        approval_id = f"approval-{task_id}"
        with memory_db.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO approvals
                    (id, task_id, conversation_id, user_id, tenant_id, description, tool_calls, risk,
                     resume_required, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    approval_id, task_id, conversation_id, user_id, tenant_id, description,
                    json.dumps(tool_calls, ensure_ascii=False), risk, int(resume_required), now,
                ),
            )
        return self.get_by_task(task_id) or {}

    def get_by_task(self, task_id: str) -> dict[str, Any] | None:
        with memory_db.connect() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE task_id = ?", (task_id,)).fetchone()
        return self._decode(row) if row else None

    def get_pending_for_conversation(
        self,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        with memory_db.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM approvals
                WHERE conversation_id = ? AND user_id = ? AND tenant_id = ?
                  AND status = 'pending' AND resume_required = 1
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (conversation_id, user_id, tenant_id),
            ).fetchone()
        return self._decode(row) if row else None

    def get(self, approval_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM approvals WHERE id = ?"
        params: list[Any] = [approval_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        with memory_db.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._decode(row) if row else None

    def decide(self, approval_id: str, approved: bool, approver: str, reason: str = "") -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        task_id = ""
        with memory_db.connect() as connection:
            current = connection.execute("SELECT status, user_id, task_id FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if not current:
                raise KeyError(approval_id)
            if current["status"] != "pending":
                raise ValueError(f"approval already decided: {current['status']}")
            if current["user_id"] == approver:
                raise PermissionError("requester cannot approve own change")
            cursor = connection.execute(
                """
                UPDATE approvals SET status = ?, approver = ?, decision_reason = ?, decided_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                ("approved" if approved else "rejected", approver, reason, now, approval_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("approval decision race detected")
            task_id = current["task_id"]
        if not approved:
            memory_db.release_tool_rate_slots(task_id)
        return self.get(approval_id) or {}

    def list(self, status: str | None = None, tenant_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        query = "SELECT * FROM approvals"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with memory_db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._decode(row) for row in rows]

    def mark_executed(self, task_id: str) -> None:
        with memory_db.connect() as connection:
            cursor = connection.execute(
                "UPDATE approvals SET status = 'executed' WHERE task_id = ? AND status = 'approved'",
                (task_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("approval is not in an executable state")

    @staticmethod
    def _decode(row) -> dict[str, Any]:
        item = dict(row)
        item["tool_calls"] = json.loads(item["tool_calls"])
        item["resume_required"] = bool(item["resume_required"])
        return item


approval_store = ApprovalStore()
