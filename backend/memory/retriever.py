from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class KnowledgeItem:
    id: str
    doc_type: str
    title: str
    content: str
    tags: list[str]
    permission: str = "internal"


KNOWLEDGE: list[KnowledgeItem] = [
    KnowledgeItem(
        id="skill-alert-storage-capacity",
        doc_type="skill",
        title="数据存储容量告警解释",
        tags=["告警", "容量", "数据存储"],
        content="当数据存储剩余容量不足时，优先确认剩余容量比例、增长速度、快照和镜像占用。建议先清理低价值快照，再规划扩容或迁移。",
    ),
    KnowledgeItem(
        id="skill-host-cpu-high",
        doc_type="skill",
        title="主机 CPU 使用率过高诊断",
        tags=["告警", "主机", "CPU"],
        content="主机 CPU 高时，先查看同主机 VM CPU 使用、CPU ready、近期任务和集群负载。避免直接重启主机，应先评估业务窗口和 HA 策略。",
    ),
    KnowledgeItem(
        id="skill-vm-performance",
        doc_type="skill",
        title="VM 性能诊断流程",
        tags=["VM", "性能", "CPU", "内存", "存储"],
        content="VM 变慢按 CPU ready、CPU usage、内存 balloon/swap、磁盘 latency、网络丢包顺序排查。CPU ready > 5% 表示存在 CPU 争用风险。",
    ),
    KnowledgeItem(
        id="skill-capacity-forecast",
        doc_type="skill",
        title="容量预测流程",
        tags=["容量", "预测", "集群"],
        content="容量预测需要当前剩余容量、日增长率和风险阈值。小于 14 天为 critical，14-30 天为 high，超过 30 天为 medium 或 low。",
    ),
]


def _tokens(text: str) -> set[str]:
    parts = re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]{2,}", text)
    return {p.lower() for p in parts}


def retrieve(query: str, roles: list[str], top_k: int = 5) -> list[dict]:
    q_tokens = _tokens(query)
    role = "admin" if "admin" in roles else "ops" if "ops" in roles else "readonly"
    results = []
    for item in KNOWLEDGE:
        if item.permission == "confidential" and role != "admin":
            continue
        haystack = _tokens(" ".join([item.title, item.content, " ".join(item.tags)]))
        score = len(q_tokens & haystack)
        if score:
            results.append((score, item))
    results.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {
            "id": item.id,
            "doc_type": item.doc_type,
            "title": item.title,
            "content": item.content,
            "tags": item.tags,
            "score": score,
        }
        for score, item in results[:top_k]
    ]
