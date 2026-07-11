from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("DCS_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "clawsphere.db"


class MemoryDatabase:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS ops_knowledge (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'global',
                    doc_type TEXT NOT NULL,
                    tier INTEGER NOT NULL DEFAULT 2,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    permission TEXT NOT NULL DEFAULT 'public',
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_filter
                    ON ops_knowledge(tenant_id, doc_type, permission, tier);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON conversation_messages(conversation_id, id);
                CREATE TABLE IF NOT EXISTS memory_writes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    execution_log_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    tool_calls TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    approver TEXT,
                    decision_reason TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
                CREATE TABLE IF NOT EXISTS tool_audit (
                    audit_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    params TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    error_code TEXT,
                    risk_level TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_audit_task ON tool_audit(task_id, created_at);
                """
            )

    def upsert_knowledge(self, records: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO ops_knowledge
                    (id, tenant_id, doc_type, tier, title, content, embedding, tags, permission, version, updated_at)
                VALUES
                    (:id, :tenant_id, :doc_type, :tier, :title, :content, :embedding, :tags, :permission, :version, :updated_at)
                ON CONFLICT(id) DO UPDATE SET
                    content=excluded.content, embedding=excluded.embedding, tags=excluded.tags,
                    permission=excluded.permission, version=excluded.version, updated_at=excluded.updated_at
                """,
                [{**record, "updated_at": now} for record in records],
            )

    def list_knowledge(self, roles: list[str], tenant_id: str = "global", tiers: tuple[int, ...] = (2, 3)) -> list[dict[str, Any]]:
        permissions = ["public"]
        if set(roles) & {"ops", "admin"}:
            permissions.append("internal")
        if "admin" in roles:
            permissions.append("confidential")
        permission_marks = ",".join("?" for _ in permissions)
        tier_marks = ",".join("?" for _ in tiers)
        query = f"""
            SELECT * FROM ops_knowledge
            WHERE tenant_id IN ('global', ?) AND permission IN ({permission_marks}) AND tier IN ({tier_marks})
        """
        with self.connect() as connection:
            rows = connection.execute(query, [tenant_id, *permissions, *tiers]).fetchall()
        return [dict(row) for row in rows]

    def append_turn(self, conversation_id: str, user_id: str, tenant_id: str, user_message: str, answer: str, summary: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations(id, user_id, tenant_id, summary, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at
                """,
                (conversation_id, user_id, tenant_id, summary, now),
            )
            connection.executemany(
                "INSERT INTO conversation_messages(conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(conversation_id, "user", user_message, now), (conversation_id, "assistant", answer, now)],
            )

    def load_conversation(self, conversation_id: str, limit: int = 12) -> tuple[list[dict[str, str]], str]:
        with self.connect() as connection:
            session = connection.execute("SELECT summary FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            rows = connection.execute(
                "SELECT role, content FROM conversation_messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        history = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]
        return history, session["summary"] if session else ""

    def write_memory(self, task_id: str, summary: str, execution_log: list[dict[str, Any]]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO memory_writes(task_id, summary, execution_log_count, created_at) VALUES (?, ?, ?, ?)",
                (task_id, summary, len(execution_log), now),
            )
            for item in execution_log:
                connection.execute(
                    "INSERT INTO execution_logs(task_id, payload, created_at) VALUES (?, ?, ?)",
                    (task_id, json.dumps(item, ensure_ascii=False), now),
                )
        return {"id": f"memory-{cursor.lastrowid:04d}", "task_id": task_id, "summary": summary, "created_at": now}

    def list_memory_writes(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM memory_writes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def append_tool_audit(self, record: dict[str, Any]) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO tool_audit
                    (audit_id, task_id, user_id, tenant_id, tool_name, params, success,
                     error_code, risk_level, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["audit_id"], record["task_id"], record["user_id"], record["tenant_id"],
                    record["tool_name"], json.dumps(record["params"], ensure_ascii=False), int(record["success"]),
                    record.get("error_code"), record["risk_level"], record["duration_ms"], record["created_at"],
                ),
            )

    def list_tool_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM tool_audit ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item["params"])
            item["success"] = bool(item["success"])
            items.append(item)
        return items


memory_db = MemoryDatabase()
