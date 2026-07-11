from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from backend.memory.database import memory_db


class KnowledgeStore(Protocol):
    def upsert_knowledge(self, records: list[dict[str, Any]]) -> None: ...
    def list_knowledge(self, roles: list[str], tenant_id: str, tiers: tuple[int, ...]) -> list[dict[str, Any]]: ...
    def get_knowledge(self, knowledge_id: str, roles: list[str], tenant_id: str) -> dict[str, Any] | None: ...


class PostgresKnowledgeStore:
    def __init__(self, dsn: str):
        import psycopg

        self.dsn = dsn
        schema = (Path(__file__).resolve().parent / "postgres_schema.sql").read_text(encoding="utf-8")
        with psycopg.connect(self.dsn) as connection:
            connection.execute(schema)

    def connect(self):
        import psycopg
        from pgvector.psycopg import register_vector

        connection = psycopg.connect(self.dsn)
        register_vector(connection)
        return connection

    def upsert_knowledge(self, records: list[dict[str, Any]]) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                for record in records:
                    cursor.execute(
                        """
                        INSERT INTO ops_knowledge
                            (id, tenant_id, doc_type, tier, title, content, embedding, tags, permission, version)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT(id) DO UPDATE SET
                            content=excluded.content, embedding=excluded.embedding, tags=excluded.tags,
                            permission=excluded.permission, version=excluded.version, updated_at=now()
                        """,
                        (
                            record["id"], record["tenant_id"], record["doc_type"], record["tier"],
                            record["title"], record["content"], json.loads(record["embedding"]),
                            json.loads(record["tags"]), record["permission"], record["version"],
                        ),
                    )

    def list_knowledge(self, roles: list[str], tenant_id: str = "global", tiers: tuple[int, ...] = (2, 3)) -> list[dict[str, Any]]:
        permissions = ["public"]
        if set(roles) & {"ops", "admin"}:
            permissions.append("internal")
        if "admin" in roles:
            permissions.append("confidential")
        with self.connect() as connection:
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            rows = connection.execute(
                """
                SELECT id, tenant_id, doc_type, tier, title, content, embedding, tags, permission, version
                FROM ops_knowledge
                WHERE tenant_id IN ('global', %s) AND permission = ANY(%s) AND tier = ANY(%s)
                """,
                (tenant_id, permissions, list(tiers)),
            ).fetchall()
        columns = ["id", "tenant_id", "doc_type", "tier", "title", "content", "embedding", "tags", "permission", "version"]
        result = []
        for row in rows:
            item = dict(zip(columns, row))
            item["embedding"] = json.dumps(item["embedding"].to_list())
            item["tags"] = json.dumps(item["tags"], ensure_ascii=False)
            result.append(item)
        return result

    def get_knowledge(self, knowledge_id: str, roles: list[str], tenant_id: str = "global") -> dict[str, Any] | None:
        permissions = ["public"]
        if set(roles) & {"ops", "admin"}:
            permissions.append("internal")
        if "admin" in roles:
            permissions.append("confidential")
        with self.connect() as connection:
            connection.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            row = connection.execute(
                """
                SELECT id, tenant_id, doc_type, tier, title, content, embedding, tags, permission, version
                FROM ops_knowledge
                WHERE id = %s AND tenant_id IN ('global', %s) AND permission = ANY(%s)
                """,
                (knowledge_id, tenant_id, permissions),
            ).fetchone()
        if not row:
            return None
        columns = ["id", "tenant_id", "doc_type", "tier", "title", "content", "embedding", "tags", "permission", "version"]
        item = dict(zip(columns, row))
        item["embedding"] = json.dumps(item["embedding"].to_list())
        item["tags"] = json.dumps(item["tags"], ensure_ascii=False)
        return item


def create_knowledge_store() -> KnowledgeStore:
    if os.getenv("DCS_MEMORY_BACKEND", "sqlite").lower() == "postgres":
        dsn = os.getenv("POSTGRES_DSN")
        if not dsn:
            raise RuntimeError("DCS_MEMORY_BACKEND=postgres requires POSTGRES_DSN")
        return PostgresKnowledgeStore(dsn)
    return memory_db


knowledge_store = create_knowledge_store()
