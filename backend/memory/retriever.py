from __future__ import annotations

import json
import re
import threading
from typing import Any

from rank_bm25 import BM25Okapi

from backend.memory.knowledge_store import knowledge_store
from backend.skills.loader import load_all_skills


_seed_lock = threading.Lock()
_seeded = False


def tokenize(text: str) -> list[str]:
    ascii_tokens = re.findall(r"[A-Za-z0-9_.-]+", text.lower())
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]+", text)
    chinese_tokens: list[str] = []
    for chunk in chinese_chunks:
        chinese_tokens.extend(chunk[index:index + 2] for index in range(max(1, len(chunk) - 1)))
    return ascii_tokens + chinese_tokens


def _empty_embedding() -> str:
    # Schema compatibility only. No vector ranking is performed until a real
    # embedding provider is configured.
    return json.dumps([0.0] * 128)


def _skill_records() -> list[dict[str, Any]]:
    records = []
    for skill in load_all_skills():
        for tier, content in [(1, skill.one_liner), (2, skill.summary), (3, skill.detail)]:
            records.append({
                "id": f"skill:{skill.id}:tier{tier}",
                "tenant_id": "global",
                "doc_type": "skill",
                "tier": tier,
                "title": skill.title,
                "content": content,
                "embedding": _empty_embedding(),
                "tags": json.dumps(skill.tags, ensure_ascii=False),
                "permission": skill.permission,
                "version": skill.version,
            })
    cases = [
        ("case:storage-low-001", "SSD 数据存储容量不足历史案例", "数据存储 ds-002 剩余低于 10%，通过清理过期快照释放 620GB，并安排下一维护窗扩容。", ["告警", "容量", "历史案例"]),
        ("case:cpu-ready-001", "VM CPU Ready 过高历史案例", "应用 VM 变慢且 CPU Ready 为 7%，迁移同宿主机批处理 VM 后恢复至 2%。", ["VM", "性能", "CPU Ready"]),
    ]
    for case_id, title, content, tags in cases:
        records.append({
            "id": case_id, "tenant_id": "global", "doc_type": "alert_case", "tier": 2,
            "title": title, "content": content, "embedding": _empty_embedding(),
            "tags": json.dumps(tags, ensure_ascii=False), "permission": "public", "version": 1,
        })
    return records


def ensure_knowledge_seeded() -> None:
    global _seeded
    if _seeded:
        return
    with _seed_lock:
        if not _seeded:
            knowledge_store.upsert_knowledge(_skill_records())
            _seeded = True


def _bm25_search(query: str, roles: list[str], tenant_id: str, tiers: tuple[int, ...], doc_type: str, top_k: int) -> list[dict[str, Any]]:
    candidates = [row for row in knowledge_store.list_knowledge(roles, tenant_id, tiers) if row["doc_type"] == doc_type]
    if not candidates:
        return []
    bm25 = BM25Okapi([tokenize(row["title"] + " " + row["content"] + " " + row["tags"]) for row in candidates])
    scores = bm25.get_scores(tokenize(query))
    ranking = sorted(range(len(candidates)), key=lambda index: scores[index], reverse=True)[:top_k]
    return [
        {
            "id": candidates[index]["id"], "doc_type": candidates[index]["doc_type"],
            "tier": candidates[index]["tier"], "title": candidates[index]["title"],
            "content": candidates[index]["content"], "tags": json.loads(candidates[index]["tags"]),
            "score": round(float(scores[index]), 5), "retrieval_mode": "bm25",
        }
        for index in ranking
    ]


def retrieve(query: str, roles: list[str], top_k: int = 5, tenant_id: str = "global", tier: int = 2) -> list[dict[str, Any]]:
    ensure_knowledge_seeded()
    return _bm25_search(query, roles, tenant_id, (tier,), "skill", top_k)


def retrieve_history(query: str, roles: list[str], top_k: int = 3, tenant_id: str = "global") -> list[dict[str, Any]]:
    ensure_knowledge_seeded()
    return _bm25_search(query, roles, tenant_id, (2,), "alert_case", top_k)


def retrieve_skill_detail(skill_id: str, roles: list[str], tenant_id: str = "global") -> dict[str, Any] | None:
    ensure_knowledge_seeded()
    expected = skill_id.replace(":tier2", ":tier3")
    row = knowledge_store.get_knowledge(expected, roles, tenant_id)
    if not row:
        return None
    return {
        "id": row["id"], "doc_type": row["doc_type"], "tier": row["tier"],
        "title": row["title"], "content": row["content"], "tags": json.loads(row["tags"]),
        "score": 1.0, "retrieval_mode": "direct_id",
    }
