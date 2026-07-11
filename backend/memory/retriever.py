from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from typing import Any

from rank_bm25 import BM25Okapi

from backend.memory.knowledge_store import knowledge_store
from backend.skills.loader import load_all_skills


EMBEDDING_DIM = 128


def tokenize(text: str) -> list[str]:
    ascii_tokens = re.findall(r"[A-Za-z0-9_.-]+", text.lower())
    chinese_chunks = re.findall(r"[\u4e00-\u9fff]+", text)
    chinese_tokens: list[str] = []
    for chunk in chinese_chunks:
        chinese_tokens.extend(chunk[index:index + 2] for index in range(max(1, len(chunk) - 1)))
    return ascii_tokens + chinese_tokens


def embed(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


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
                "embedding": json.dumps(embed(" ".join([skill.title, *skill.tags, content]))),
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
            "id": case_id,
            "tenant_id": "global",
            "doc_type": "alert_case",
            "tier": 2,
            "title": title,
            "content": content,
            "embedding": json.dumps(embed(" ".join([title, content, *tags]))),
            "tags": json.dumps(tags, ensure_ascii=False),
            "permission": "public",
            "version": 1,
        })
    return records


def ensure_knowledge_seeded() -> None:
    knowledge_store.upsert_knowledge(_skill_records())


def _hybrid_search(query: str, roles: list[str], tenant_id: str, tiers: tuple[int, ...], doc_type: str, top_k: int) -> list[dict[str, Any]]:
    candidates = [row for row in knowledge_store.list_knowledge(roles, tenant_id, tiers) if row["doc_type"] == doc_type]
    if not candidates:
        return []
    query_tokens = tokenize(query)
    bm25 = BM25Okapi([tokenize(row["title"] + " " + row["content"]) for row in candidates])
    bm25_scores = bm25.get_scores(query_tokens)
    query_vector = embed(query)
    vector_scores = [cosine(query_vector, json.loads(row["embedding"])) for row in candidates]

    bm25_rank = sorted(range(len(candidates)), key=lambda index: bm25_scores[index], reverse=True)[:20]
    vector_rank = sorted(range(len(candidates)), key=lambda index: vector_scores[index], reverse=True)[:20]
    rrf: dict[int, float] = defaultdict(float)
    for ranking in (bm25_rank, vector_rank):
        for position, index in enumerate(ranking, start=1):
            rrf[index] += 1.0 / (60 + position)

    results = []
    for index in sorted(rrf, key=rrf.get, reverse=True)[:top_k]:
        row = candidates[index]
        results.append({
            "id": row["id"],
            "doc_type": row["doc_type"],
            "tier": row["tier"],
            "title": row["title"],
            "content": row["content"],
            "tags": json.loads(row["tags"]),
            "score": round(rrf[index], 5),
            "bm25_score": round(float(bm25_scores[index]), 4),
            "vector_score": round(float(vector_scores[index]), 4),
        })
    return results


def retrieve(query: str, roles: list[str], top_k: int = 5, tenant_id: str = "global", tier: int = 2) -> list[dict[str, Any]]:
    ensure_knowledge_seeded()
    return _hybrid_search(query, roles, tenant_id, (tier,), "skill", top_k)


def retrieve_history(query: str, roles: list[str], top_k: int = 3, tenant_id: str = "global") -> list[dict[str, Any]]:
    ensure_knowledge_seeded()
    return _hybrid_search(query, roles, tenant_id, (2,), "alert_case", top_k)


def retrieve_skill_detail(skill_id: str, roles: list[str], tenant_id: str = "global") -> dict[str, Any] | None:
    ensure_knowledge_seeded()
    expected = skill_id.replace(":tier2", ":tier3")
    return next((row for row in _hybrid_search(skill_id, roles, tenant_id, (3,), "skill", 10) if row["id"] == expected), None)
