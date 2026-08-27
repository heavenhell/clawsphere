from __future__ import annotations

import json
import re
import threading
from typing import Any

from rank_bm25 import BM25Okapi

from backend.memory.knowledge_store import knowledge_store
from backend.mcp.tools import TOOL_REGISTRY
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




_tool_corpus_lock = threading.Lock()
_TOOL_CORPUS: list[dict[str, Any]] | None = None


def _tool_corpus() -> list[dict[str, Any]]:
    """In-memory, non-persisted index over TOOL_REGISTRY. Deliberately not routed
    through knowledge_store: tool RBAC is an exact auth_roles membership check,
    not knowledge_store's 3-tier permission model, so forcing tools through that
    model would be fragile. TOOL_REGISTRY doesn't change after import, so this
    is lazily built once."""
    global _TOOL_CORPUS
    if _TOOL_CORPUS is None:
        with _tool_corpus_lock:
            if _TOOL_CORPUS is None:
                _TOOL_CORPUS = [
                    {
                        "tool_name": spec.name,
                        "category": spec.category,
                        "tags": spec.tags,
                        "description": spec.description,
                        "auth_roles": spec.auth_roles,
                        "tokens": tokenize(f"{spec.name} {spec.description} {spec.category} {' '.join(spec.tags)}"),
                    }
                    for spec in TOOL_REGISTRY.values()
                ]
    return _TOOL_CORPUS


def retrieve_tools(query: str, roles: list[str], tenant_id: str = "global", top_k: int = 5) -> list[dict[str, Any]]:
    """RBAC-filter TOOL_REGISTRY first, then BM25-rank the allowed subset.
    tenant_id is accepted for interface parity with retrieve()/retrieve_history()
    even though tools aren't tenant-scoped today (TOOL_REGISTRY/call_tool() have
    no tenant concept either)."""
    candidates = [row for row in _tool_corpus() if any(role in row["auth_roles"] for role in roles)]
    if not candidates:
        return []
    bm25 = BM25Okapi([row["tokens"] for row in candidates])
    scores = bm25.get_scores(tokenize(query))
    ranking = sorted(range(len(candidates)), key=lambda index: scores[index], reverse=True)[:top_k]
    return [
        {
            "tool_name": candidates[index]["tool_name"],
            "category": candidates[index]["category"],
            "tags": candidates[index]["tags"],
            "description": candidates[index]["description"],
            "score": round(float(scores[index]), 5),
            "retrieval_mode": "bm25",
        }
        for index in ranking
    ]


def retrieve_discovered_tools(
    query: str,
    tools: list[dict[str, Any]],
    roles: list[str],
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """RBAC-filter and rank an MCP-discovered catalog snapshot.

    Unlike retrieve_tools(), this never consults the process-local registry;
    the supplied snapshot is the complete source of truth for this search.
    """
    candidates = []
    for tool in tools:
        auth_roles = tool.get("auth_roles") or []
        if not any(role in auth_roles for role in roles):
            continue
        category = str(tool.get("category") or "general")
        tags = [str(tag) for tag in (tool.get("tags") or [])]
        description = str(tool.get("description") or "")
        name = str(tool.get("name") or "")
        candidates.append({
            "tool_name": name,
            "category": category,
            "tags": tags,
            "description": description,
            "tokens": tokenize(f"{name} {description} {category} {' '.join(tags)}"),
        })
    if not candidates:
        return []
    bm25 = BM25Okapi([row["tokens"] for row in candidates])
    scores = bm25.get_scores(tokenize(query))
    ranking = sorted(range(len(candidates)), key=lambda index: scores[index], reverse=True)[:top_k]
    return [
        {
            "tool_name": candidates[index]["tool_name"],
            "category": candidates[index]["category"],
            "tags": candidates[index]["tags"],
            "description": candidates[index]["description"],
            "score": round(float(scores[index]), 5),
            "retrieval_mode": "bm25",
        }
        for index in ranking
    ]
