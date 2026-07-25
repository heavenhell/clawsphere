from __future__ import annotations

import json
from typing import Any, Callable
import re


MAX_FULL_TURNS = 6
MAX_FULL_MESSAGES = MAX_FULL_TURNS * 2
SUMMARY_MAX_CHARS = 1200
TOKEN_THRESHOLD = 3000
RECENT_TOKEN_BUDGET = 1400
SUMMARY_TOKEN_BUDGET = 800
RELEVANT_TOKEN_BUDGET = 500
WORKING_TOKEN_BUDGET = 300
CONTEXT_SCHEMA_VERSION = 2
WORKING_REQUEST_MAX_CHARS = 160
RESOURCE_ID_PATTERN = re.compile(
    r"(?:alarm|cluster|vm|ds|host)-\d+|(?:dcs|fc|edme)-[a-z0-9-]+|\b[a-f0-9]{32}\b",
    re.I,
)


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    total = 0
    for item in messages:
        content = item.get("content", "")
        cjk = len(re.findall(r"[\u4e00-\u9fff]", content))
        non_cjk_tokens = sum(
            max(1, (len(term) + 3) // 4)
            for term in re.findall(r"[A-Za-z0-9_.-]+", content)
        )
        total += max(1, cjk + non_cjk_tokens)
    return total


def deterministic_summary(messages: list[dict[str, str]]) -> str:
    lines = []
    entity_ids: list[str] = []
    for item in messages[-18:]:
        role = "用户" if item.get("role") == "user" else "助手"
        content = item.get("content", "").replace("\n", " ").strip()
        if content:
            lines.append(f"{role}: {content[:180]}")
        for match in RESOURCE_ID_PATTERN.findall(item.get("content", "")):
            normalized = match.lower()
            if normalized not in entity_ids:
                entity_ids.append(normalized)
    if entity_ids:
        lines.append(f"涉及对象: {', '.join(entity_ids[-8:])}")
    return "\n".join(lines)


def _truncate_message(message: dict[str, str], token_budget: int) -> dict[str, str]:
    content = message.get("content", "")
    current_tokens = estimate_tokens([message])
    if current_tokens <= token_budget:
        return dict(message)
    low = 0
    high = len(content)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = {**message, "content": content[:middle].rstrip() + "…"}
        if estimate_tokens([candidate]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return {**message, "content": content[:low].rstrip() + "…"}


def _select_recent_messages(
    history: list[dict[str, str]],
    token_budget: int = RECENT_TOKEN_BUDGET,
) -> tuple[list[dict[str, str]], int]:
    candidates = history[-MAX_FULL_MESSAGES:]
    selected: list[dict[str, str]] = []
    selected_tokens = 0
    consumed = 0
    for message in reversed(candidates):
        message_tokens = estimate_tokens([message])
        if not selected and message_tokens > token_budget:
            selected.append(_truncate_message(message, token_budget))
            consumed = 1
            break
        if selected_tokens + message_tokens > token_budget:
            break
        selected.append(dict(message))
        selected_tokens += message_tokens
        consumed += 1
    return list(reversed(selected)), consumed


def _limit_summary(summary: str) -> str:
    if len(summary) > SUMMARY_MAX_CHARS:
        boundary = summary.rfind("\n", 0, SUMMARY_MAX_CHARS)
        summary = summary[:boundary if boundary > 0 else SUMMARY_MAX_CHARS].rstrip()
    summary_tokens = estimate_tokens([{"role": "system", "content": summary}])
    if summary_tokens > SUMMARY_TOKEN_BUDGET:
        summary = _truncate_message(
            {"role": "system", "content": summary},
            SUMMARY_TOKEN_BUDGET,
        )["content"]
    return summary


def _query_terms(message: str) -> set[str]:
    terms: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]{2,}", message):
        token = token.lower()
        if len(token) < 2:
            continue
        terms.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            terms.update(token[index:index + 2] for index in range(len(token) - 1))
    return terms


def select_relevant_messages(
    messages: list[dict[str, str]],
    current_message: str,
    token_budget: int = RELEVANT_TOKEN_BUDGET,
    limit: int = 4,
) -> list[dict[str, str]]:
    terms = _query_terms(current_message)
    if not terms:
        return []
    turns: list[tuple[int, list[dict[str, str]]]] = []
    index = 0
    while index < len(messages):
        items = [messages[index]]
        if (
            messages[index].get("role") == "user"
            and index + 1 < len(messages)
            and messages[index + 1].get("role") == "assistant"
        ):
            items.append(messages[index + 1])
            index += 1
        turns.append((index - len(items) + 1, items))
        index += 1

    scored: list[tuple[int, int, list[dict[str, str]]]] = []
    for turn_index, items in turns:
        content = " ".join(item.get("content", "") for item in items).lower()
        score = sum(
            8 if RESOURCE_ID_PATTERN.fullmatch(term) else 1
            for term in terms
            if term in content
        )
        if score:
            scored.append((score, turn_index, items))
    selected = sorted(scored, key=lambda row: (-row[0], -row[1]))[:max(1, limit // 2)]
    selected.sort(key=lambda row: row[1])
    results: list[dict[str, str]] = []
    used_tokens = 0
    for _, _, items in selected:
        item_tokens = estimate_tokens(items)
        remaining = token_budget - used_tokens
        if remaining <= 0:
            break
        if item_tokens <= remaining:
            results.extend(dict(item) for item in items)
            used_tokens += item_tokens
            continue
        if results:
            break
        per_message_budget = max(1, remaining // len(items))
        truncated = [_truncate_message(item, per_message_budget) for item in items]
        results.extend(truncated)
        used_tokens += estimate_tokens(truncated)
    return results


def extract_working_context(
    history: list[dict[str, str]],
    current_message: str,
) -> dict[str, Any]:
    resources: list[str] = []
    for item in [*history, {"role": "user", "content": current_message}]:
        for match in RESOURCE_ID_PATTERN.findall(item.get("content", "")):
            normalized = match.lower()[:80]
            if normalized not in resources:
                resources.append(normalized)
    latest_user_request = current_message.strip()
    if not latest_user_request:
        latest_user_request = next(
            (
                item.get("content", "").strip()
                for item in reversed(history)
                if item.get("role") == "user" and item.get("content", "").strip()
            ),
            "",
        )
    working_context = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "active_resource_ids": resources[-8:],
        "latest_user_request": latest_user_request[:WORKING_REQUEST_MAX_CHARS],
        "history_message_count": len(history),
    }
    while estimate_tokens([{
        "role": "system",
        "content": json.dumps(working_context, ensure_ascii=False),
    }]) > WORKING_TOKEN_BUDGET:
        latest = working_context["latest_user_request"]
        if len(latest) > 40:
            working_context["latest_user_request"] = latest[:-20]
        elif working_context["active_resource_ids"]:
            working_context["active_resource_ids"].pop(0)
        else:
            break
    return working_context


def manage_context_window(
    history: list[dict[str, str]],
    existing_summary: str = "",
    summarizer: Callable[[list[dict[str, str]]], str] | None = None,
    current_message: str = "",
) -> dict[str, Any]:
    recent, consumed = _select_recent_messages(history)
    older = history[:len(history) - consumed] if consumed else list(history)
    summary = existing_summary.strip()
    if older:
        # Stored history is authoritative, so recompute its older segment instead of
        # repeatedly appending the same turns to the persisted summary.
        summary = (summarizer or deterministic_summary)(older).strip()
        if len(summary) > SUMMARY_MAX_CHARS:
            recursive_input = [{"role": "system", "content": summary}]
            summary = (summarizer or deterministic_summary)(recursive_input).strip()
        summary = _limit_summary(summary)
    relevant = select_relevant_messages(older, current_message)
    working_context = extract_working_context(history, current_message)
    summary_tokens = (
        estimate_tokens([{"role": "system", "content": summary}])
        if summary else 0
    )
    relevant_tokens = estimate_tokens(relevant)
    working_tokens = estimate_tokens([{
        "role": "system",
        "content": json.dumps(working_context, ensure_ascii=False),
    }])
    return {
        "recent_messages": recent,
        "relevant_messages": relevant,
        "working_context": working_context,
        "conversation_summary": summary,
        "older_message_count": len(older),
        "estimated_tokens": (
            estimate_tokens(recent)
            + summary_tokens
            + relevant_tokens
            + working_tokens
        ),
        "schema_version": CONTEXT_SCHEMA_VERSION,
    }
