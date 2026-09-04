from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from backend.guardrails.permission import permissions_for_roles


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("DCS_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "clawsphere.db"


class MemoryDatabase:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    execution_log_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
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
                    resume_required INTEGER NOT NULL DEFAULT 1,
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
                CREATE TABLE IF NOT EXISTS mock_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_rate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, tool_name, resource_id)
                );
                CREATE INDEX IF NOT EXISTS idx_rate_window
                    ON tool_rate_events(tool_name, resource_id, created_at);
                -- Long-term (cross-session) memory index. The markdown files on
                -- disk are the source of truth; these two tables exist only to
                -- make lookup indexed and can be rebuilt from the files.
                CREATE TABLE IF NOT EXISTS long_term_facts (
                    name TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    merge_key TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT '',
                    tools_used TEXT NOT NULL DEFAULT '[]',
                    resource_ids TEXT NOT NULL DEFAULT '[]',
                    observed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_long_term_scope
                    ON long_term_facts(tenant_id, user_id, observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_long_term_merge
                    ON long_term_facts(tenant_id, user_id, merge_key);
                CREATE TABLE IF NOT EXISTS long_term_fact_resources (
                    name TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    PRIMARY KEY (name, resource_id)
                );
                CREATE INDEX IF NOT EXISTS idx_long_term_resource
                    ON long_term_fact_resources(resource_id);
                """
            )
            self._add_column_if_missing(connection, "memory_writes", "user_id", "TEXT NOT NULL DEFAULT 'legacy-user'")
            self._add_column_if_missing(connection, "memory_writes", "tenant_id", "TEXT NOT NULL DEFAULT 'legacy-tenant'")
            self._add_column_if_missing(connection, "execution_logs", "user_id", "TEXT NOT NULL DEFAULT 'legacy-user'")
            self._add_column_if_missing(connection, "execution_logs", "tenant_id", "TEXT NOT NULL DEFAULT 'legacy-tenant'")
            self._add_column_if_missing(connection, "approvals", "resume_required", "INTEGER NOT NULL DEFAULT 1")
            # Highest conversation_messages.id already folded into
            # conversations.summary. Messages at or below it are represented by
            # the summary and are no longer loaded verbatim.
            self._add_column_if_missing(
                connection, "conversations", "summarized_upto_id", "INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                """
                UPDATE approvals SET resume_required = 0
                WHERE tool_calls = '[]' AND conversation_id = task_id
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_writes_tenant ON memory_writes(tenant_id, created_at)"
            )

    @staticmethod
    def _add_column_if_missing(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        # Identifiers are internal migration constants only; never pass request data here.
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

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
        permissions = permissions_for_roles(roles)
        permission_marks = ",".join("?" for _ in permissions)
        tier_marks = ",".join("?" for _ in tiers)
        query = f"""
            SELECT * FROM ops_knowledge
            WHERE tenant_id IN ('global', ?) AND permission IN ({permission_marks}) AND tier IN ({tier_marks})
        """
        with self.connect() as connection:
            rows = connection.execute(query, [tenant_id, *permissions, *tiers]).fetchall()
        return [dict(row) for row in rows]

    def get_knowledge(self, knowledge_id: str, roles: list[str], tenant_id: str = "global") -> dict[str, Any] | None:
        permissions = permissions_for_roles(roles)
        marks = ",".join("?" for _ in permissions)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM ops_knowledge
                WHERE id = ? AND tenant_id IN ('global', ?) AND permission IN ({marks})
                """,
                [knowledge_id, tenant_id, *permissions],
            ).fetchone()
        return dict(row) if row else None

    def append_turn(
        self,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        user_message: str,
        answer: str,
        summary: str,
        summarized_upto_id: int | None = None,
    ) -> None:
        """Persist one turn. `summarized_upto_id` is only written when compaction
        ran this turn; passing None leaves the stored watermark untouched so a
        non-compacting turn can never rewind it."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            owner = connection.execute(
                "SELECT user_id, tenant_id FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if owner and (owner["user_id"] != user_id or owner["tenant_id"] != tenant_id):
                raise PermissionError("conversation does not belong to caller")
            connection.execute(
                """
                INSERT INTO conversations(id, user_id, tenant_id, summary, summarized_upto_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    summary=excluded.summary,
                    summarized_upto_id=max(
                        conversations.summarized_upto_id, excluded.summarized_upto_id
                    ),
                    updated_at=excluded.updated_at
                """,
                (conversation_id, user_id, tenant_id, summary, int(summarized_upto_id or 0), now),
            )
            connection.executemany(
                "INSERT INTO conversation_messages(conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                [(conversation_id, "user", user_message, now), (conversation_id, "assistant", answer, now)],
            )

    def load_conversation(
        self,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        limit: int = 500,
    ) -> tuple[list[dict[str, Any]], str, int]:
        """Return (messages_after_watermark, rolling_summary, watermark).

        Only messages the summary does not already cover are loaded, so history
        length is bounded by the compaction cycle rather than by a fixed cutoff.
        `limit` is a runaway guard for conversations written before the
        watermark existed, not the normal path.
        """
        with self.connect() as connection:
            session = connection.execute(
                """
                SELECT summary, summarized_upto_id FROM conversations
                WHERE id = ? AND user_id = ? AND tenant_id = ?
                """,
                (conversation_id, user_id, tenant_id),
            ).fetchone()
            existing = connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if existing and not session:
                raise PermissionError("conversation does not belong to caller")
            watermark = int(session["summarized_upto_id"]) if session else 0
            rows = connection.execute(
                """
                SELECT id, role, content FROM conversation_messages
                WHERE conversation_id = ? AND id > ? AND EXISTS (
                    SELECT 1 FROM conversations
                    WHERE id = ? AND user_id = ? AND tenant_id = ?
                )
                ORDER BY id DESC LIMIT ?
                """,
                (conversation_id, watermark, conversation_id, user_id, tenant_id, limit),
            ).fetchall()
        history = [
            {"id": row["id"], "role": row["role"], "content": row["content"]}
            for row in reversed(rows)
        ]
        return history, session["summary"] if session else "", watermark

    def write_memory(
        self,
        task_id: str,
        user_id: str,
        tenant_id: str,
        summary: str,
        execution_log: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_writes(task_id, user_id, tenant_id, summary, execution_log_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, user_id, tenant_id, summary, len(execution_log), now),
            )
            for item in execution_log:
                connection.execute(
                    """
                    INSERT INTO execution_logs(task_id, user_id, tenant_id, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (task_id, user_id, tenant_id, json.dumps(item, ensure_ascii=False), now),
                )
        return {
            "id": f"memory-{cursor.lastrowid:04d}",
            "task_id": task_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "summary": summary,
            "created_at": now,
        }

    def list_memory_writes(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_writes WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
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

    def list_tool_audit(self, limit: int = 50, tenant_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM tool_audit"
        params: list[Any] = []
        if tenant_id:
            query += " WHERE tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item["params"])
            item["success"] = bool(item["success"])
            items.append(item)
        return items

    def append_mock_change(self, task_id: str, action: str, resource_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO mock_changes(task_id, action, resource_id, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, action, resource_id, json.dumps(payload, ensure_ascii=False), now),
            )
        return {**payload, "record_id": cursor.lastrowid, "created_at": now}

    def count_mock_changes(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM mock_changes").fetchone()
        return int(row["count"])

    def reserve_tool_rate_slot(
        self,
        task_id: str,
        tool_name: str,
        resource_id: str,
        since: str,
        limit: int,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM tool_rate_events WHERE task_id = ? AND tool_name = ? AND resource_id = ?",
                (task_id, tool_name, resource_id),
            ).fetchone()
            if existing:
                return True
            count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM tool_rate_events
                WHERE tool_name = ? AND resource_id = ? AND created_at >= ?
                """,
                (tool_name, resource_id, since),
            ).fetchone()["count"]
            if count >= limit:
                return False
            connection.execute(
                "INSERT INTO tool_rate_events(task_id, tool_name, resource_id, status, created_at) VALUES (?, ?, ?, 'reserved', ?)",
                (task_id, tool_name, resource_id, now),
            )
            return True

    def mark_tool_rate_executed(self, task_id: str, tool_name: str, resource_id: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                UPDATE tool_rate_events SET status = 'executed'
                WHERE task_id = ? AND tool_name = ? AND resource_id = ?
                """,
                (task_id, tool_name, resource_id),
            )

    # --- Long-term memory index ------------------------------------------
    # Every read is scoped by (tenant_id, user_id): cross-user visibility of
    # operational history is a privilege escalation, not a convenience.

    def upsert_long_term_fact(self, record: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                INSERT INTO long_term_facts
                    (name, tenant_id, user_id, conversation_id, merge_key, fact_type,
                     description, outcome, tools_used, resource_ids, observed_at, updated_at)
                VALUES
                    (:name, :tenant_id, :user_id, :conversation_id, :merge_key, :fact_type,
                     :description, :outcome, :tools_used, :resource_ids, :observed_at, :updated_at)
                ON CONFLICT(name) DO UPDATE SET
                    fact_type=excluded.fact_type, description=excluded.description,
                    outcome=excluded.outcome, tools_used=excluded.tools_used,
                    resource_ids=excluded.resource_ids, observed_at=excluded.observed_at,
                    updated_at=excluded.updated_at
                """,
                {**record, "updated_at": now},
            )
            connection.execute(
                "DELETE FROM long_term_fact_resources WHERE name = ?", (record["name"],)
            )
            connection.executemany(
                "INSERT OR IGNORE INTO long_term_fact_resources(name, resource_id) VALUES (?, ?)",
                [(record["name"], item) for item in json.loads(record["resource_ids"])],
            )

    def find_long_term_fact_by_merge_key(
        self, tenant_id: str, user_id: str, merge_key: str, updated_after: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM long_term_facts
                WHERE tenant_id = ? AND user_id = ? AND merge_key = ? AND updated_at >= ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (tenant_id, user_id, merge_key, updated_after),
            ).fetchone()
        return dict(row) if row else None

    def query_long_term_facts(
        self,
        tenant_id: str,
        user_id: str,
        resource_id: str | None = None,
        fact_type: str | None = None,
        observed_after: str | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        query = [
            "SELECT f.* FROM long_term_facts f",
            "WHERE f.tenant_id = ? AND f.user_id = ?",
        ]
        params: list[Any] = [tenant_id, user_id]
        if resource_id:
            query.insert(1, "JOIN long_term_fact_resources r ON r.name = f.name")
            query.append("AND r.resource_id = ?")
            params.append(resource_id.lower())
        if fact_type:
            query.append("AND f.fact_type = ?")
            params.append(fact_type)
        if observed_after:
            query.append("AND f.observed_at >= ?")
            params.append(observed_after)
        query.append("ORDER BY f.observed_at DESC LIMIT ?")
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(" ".join(query), params).fetchall()
        return [dict(row) for row in rows]

    def clear_long_term_facts(self, tenant_id: str, user_id: str) -> None:
        """Drop the index for one owner so it can be rebuilt from the files."""
        with self._lock, self.connect() as connection:
            connection.execute(
                """
                DELETE FROM long_term_fact_resources WHERE name IN (
                    SELECT name FROM long_term_facts WHERE tenant_id = ? AND user_id = ?
                )
                """,
                (tenant_id, user_id),
            )
            connection.execute(
                "DELETE FROM long_term_facts WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            )

    def release_tool_rate_slots(self, task_id: str) -> None:
        with self._lock, self.connect() as connection:
            connection.execute(
                "DELETE FROM tool_rate_events WHERE task_id = ? AND status = 'reserved'",
                (task_id,),
            )


memory_db = MemoryDatabase()
