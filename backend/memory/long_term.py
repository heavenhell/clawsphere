"""Long-term (cross-session) memory: one markdown file per fact.

Design notes:

- **Files are the source of truth.** `long_term_facts` in SQLite is only an
  index so lookup is indexed instead of a directory scan; `rebuild_index()`
  regenerates it from disk. This keeps memory auditable — an operator can read,
  correct, delete, or `git` the record, which a vector store cannot offer.
- **Recall is structured, not semantic.** In an ops domain the question is
  almost always "what happened to *this* resource before", which is an index
  lookup. Keyword ranking exists only as a fallback for questions that name no
  resource.
- **Store conclusions, not state.** Current CPU/memory/alarm values are one
  tool call away and go stale immediately; what tools cannot replay is *why*
  something happened, what fixed it, and what was tried and failed.
- Frontmatter is parsed with a strict `key: json-value` grammar rather than
  YAML, matching `skills/loader.py`'s hand-rolled parsing and avoiding a new
  production dependency (see docs/request-budget-dependency-decision.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.memory.database import memory_db


from backend.memory.context_manager import CROSS_SESSION_TOKEN_BUDGET

FACT_TYPES = ("incident", "change", "preference", "resource")
# Turns inside one diagnostic arc keep updating a single fact instead of
# creating a fragment each: the useful memory is the closed loop
# ("cause -> action -> verified outcome"), which only exists once the arc ends.
MERGE_WINDOW_HOURS = 24
MAX_BODY_CHARS = 1500
MAX_DESCRIPTION_CHARS = 160
MAX_RESOURCE_IDS = 8
DEFAULT_SINCE_DAYS = 90
_SAFE_COMPONENT = re.compile(r"[^a-z0-9._-]+")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


def memory_root() -> Path:
    return Path(os.getenv("DCS_MEMORY_DIR", Path(os.getenv("DCS_DATA_DIR", "data")) / "memory"))


def _safe_component(value: str, fallback: str) -> str:
    """Path components come from request-scoped identity, so traversal and
    absolute-path escapes must be impossible by construction, not by review."""
    cleaned = _SAFE_COMPONENT.sub("-", (value or "").strip().lower()).strip("-.")
    return cleaned[:64] or fallback


@dataclass(frozen=True)
class Fact:
    name: str
    description: str
    fact_type: str
    tenant_id: str
    user_id: str
    conversation_id: str
    observed_at: str
    body: str
    resource_ids: tuple[str, ...] = ()
    tools_used: tuple[str, ...] = ()
    outcome: str = ""

    def to_frontmatter(self) -> str:
        fields: list[tuple[str, Any]] = [
            ("name", self.name),
            ("description", self.description),
            ("type", self.fact_type),
            ("observed_at", self.observed_at),
            ("resource_ids", list(self.resource_ids)),
            ("tools_used", list(self.tools_used)),
            ("outcome", self.outcome),
            ("conversation_id", self.conversation_id),
        ]
        lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields]
        return "---\n" + "\n".join(lines) + "\n---\n" + self.body.strip() + "\n"

    def recall_payload(self, now: datetime | None = None, body_chars: int | None = None) -> dict[str, Any]:
        """Shape handed to the model. `observed_at` and `age_days` are
        mandatory: without them a three-month-old observation reads as current
        state, which is the single biggest hazard of long-term memory."""
        age_days = None
        try:
            observed = datetime.fromisoformat(self.observed_at)
            if observed.tzinfo is None:
                # A hand-edited file may carry a naive timestamp; subtracting it
                # from an aware `now` would raise and take the whole recall down.
                observed = observed.replace(tzinfo=timezone.utc)
            age_days = max(0, ((now or datetime.now(timezone.utc)) - observed).days)
        except (TypeError, ValueError):
            pass
        return {
            "description": self.description,
            "type": self.fact_type,
            "observed_at": self.observed_at,
            "age_days": age_days,
            "resource_ids": list(self.resource_ids),
            "outcome": self.outcome,
            "content": self.body if body_chars is None else self.body[:body_chars],
        }


def fit_recall_budget(
    facts: list[Fact], token_budget: int = CROSS_SESSION_TOKEN_BUDGET
) -> list[dict[str, Any]]:
    """Emit as many recalled facts as the cross-session budget allows.

    The budget is separate from the conversation's own, so a long conversation
    can never squeeze out recalled facts — but recall must not overrun it
    either. Facts are recency-ordered, so truncating the tail drops the least
    relevant first; a fact whose body does not fit is trimmed rather than
    dropped, because its metadata (resource, age, outcome) is the part that
    carries the warning about staleness.
    """
    from backend.memory.context_manager import estimate_tokens

    payloads: list[dict[str, Any]] = []
    spent = 0
    for fact in facts:
        payload = fact.recall_payload()
        cost = estimate_tokens([{"role": "system", "content": json.dumps(payload, ensure_ascii=False)}])
        remaining = token_budget - spent
        if remaining <= 0:
            break
        if cost > remaining:
            overflow = cost - remaining
            trimmed = max(0, len(fact.body) - overflow * 2)
            payload = fact.recall_payload(body_chars=trimmed)
            cost = estimate_tokens([
                {"role": "system", "content": json.dumps(payload, ensure_ascii=False)}
            ])
            if cost > remaining:
                break
        payloads.append(payload)
        spent += cost
    return payloads


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse `key: json-value` frontmatter, tolerating hand edits.

    Values that are not valid JSON fall back to the raw string, so an operator
    correcting a file by hand (`type: incident` instead of `type: "incident"`)
    does not corrupt the record.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError("missing frontmatter block")
    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        key, separator, raw = line.partition(":")
        if not separator:
            continue
        try:
            meta[key.strip()] = json.loads(raw.strip())
        except json.JSONDecodeError:
            meta[key.strip()] = raw.strip()
    return meta, match.group(2).strip()


def _as_list(value: Any) -> list[str]:
    """Coerce a frontmatter field to a list of strings.

    A hand-written YAML-style `resource_ids: [vm-1001]` is not valid JSON and
    falls back to the raw string; iterating that string would yield one entry
    per character, so the index would fill with single-letter resource ids.
    """
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.strip("[]").split(",") if item.strip()]
    return []


@dataclass
class LongTermMemory:
    root: Path = field(default_factory=memory_root)
    database: Any = memory_db

    # --- paths ---------------------------------------------------------------

    def owner_dir(self, tenant_id: str, user_id: str) -> Path:
        return (
            self.root
            / _safe_component(tenant_id, "unknown-tenant")
            / _safe_component(user_id, "unknown-user")
        )

    def fact_path(self, tenant_id: str, user_id: str, name: str) -> Path:
        return self.owner_dir(tenant_id, user_id) / "facts" / f"{_safe_component(name, 'fact')}.md"

    # --- write ---------------------------------------------------------------

    def remember(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        fact_type: str,
        description: str,
        body: str,
        resource_ids: list[str] | None = None,
        tools_used: list[str] | None = None,
        outcome: str = "",
        observed_at: str | None = None,
    ) -> Fact | None:
        if fact_type not in FACT_TYPES or not (description or "").strip():
            return None
        resources = tuple(sorted({
            str(item).strip().lower() for item in (resource_ids or []) if str(item).strip()
        }))[:MAX_RESOURCE_IDS]
        now = datetime.now(timezone.utc)
        stamp = observed_at or now.isoformat()
        merge_key = f"{fact_type}|{conversation_id}|{','.join(resources)}"
        existing = self.database.find_long_term_fact_by_merge_key(
            tenant_id, user_id, merge_key,
            (now - timedelta(hours=MERGE_WINDOW_HOURS)).isoformat(),
        )
        if existing:
            name = existing["name"]
        else:
            primary = _safe_component(resources[0], "general") if resources else fact_type
            digest = hashlib.sha256(merge_key.encode("utf-8")).hexdigest()[:8]
            name = f"{stamp[:10]}-{primary}-{digest}"

        fact = Fact(
            name=name,
            description=description.strip()[:MAX_DESCRIPTION_CHARS],
            fact_type=fact_type,
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            observed_at=stamp,
            body=body.strip()[:MAX_BODY_CHARS],
            resource_ids=resources,
            tools_used=tuple(tools_used or []),
            outcome=outcome,
        )
        path = self.fact_path(tenant_id, user_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fact.to_frontmatter(), encoding="utf-8")
        self.database.upsert_long_term_fact({
            "name": fact.name,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "merge_key": merge_key,
            "fact_type": fact.fact_type,
            "description": fact.description,
            "outcome": fact.outcome,
            "tools_used": json.dumps(list(fact.tools_used), ensure_ascii=False),
            "resource_ids": json.dumps(list(fact.resource_ids), ensure_ascii=False),
            "observed_at": fact.observed_at,
        })
        self._write_index(tenant_id, user_id)
        return fact

    def _write_index(self, tenant_id: str, user_id: str) -> None:
        """Human-readable index. Deliberately not loaded into any prompt — at ops
        scale this grows to thousands of lines, so recall goes through the
        indexed query instead of a resident table of contents."""
        rows = self.database.query_long_term_facts(tenant_id, user_id, limit=1000)
        lines = ["# 长期记忆索引", ""]
        for row in rows:
            resources = ", ".join(json.loads(row["resource_ids"])) or "—"
            lines.append(
                f"- [{row['name']}](facts/{row['name']}.md) "
                f"`{row['fact_type']}` `{row['observed_at'][:10]}` [{resources}] — {row['description']}"
            )
        directory = self.owner_dir(tenant_id, user_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # --- read ----------------------------------------------------------------

    def load(self, tenant_id: str, user_id: str, name: str) -> Fact | None:
        path = self.fact_path(tenant_id, user_id, name)
        if not path.is_file():
            return None
        try:
            meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return Fact(
            name=str(meta.get("name") or name),
            description=str(meta.get("description") or ""),
            fact_type=str(meta.get("type") or "incident"),
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=str(meta.get("conversation_id") or ""),
            observed_at=str(meta.get("observed_at") or ""),
            body=body,
            resource_ids=tuple(_as_list(meta.get("resource_ids"))),
            tools_used=tuple(_as_list(meta.get("tools_used"))),
            outcome=str(meta.get("outcome") or ""),
        )

    def search(
        self,
        *,
        tenant_id: str,
        user_id: str,
        resource_id: str | None = None,
        fact_type: str | None = None,
        keywords: str | None = None,
        since_days: int = DEFAULT_SINCE_DAYS,
        limit: int = 3,
    ) -> list[Fact]:
        observed_after = (
            datetime.now(timezone.utc) - timedelta(days=max(1, since_days))
        ).isoformat()
        # Over-fetch only when ranking is needed; the resource path is exact and
        # ordered by recency already.
        rows = self.database.query_long_term_facts(
            tenant_id, user_id, resource_id, fact_type, observed_after,
            limit=limit if not keywords else max(limit * 10, 30),
        )
        facts = [
            fact for fact in (self.load(tenant_id, user_id, row["name"]) for row in rows)
            if fact is not None
        ]
        if keywords and facts:
            facts = _rank_by_keywords(facts, keywords)
        return facts[:limit]

    def rebuild_index(self, tenant_id: str, user_id: str) -> int:
        """Recreate the SQLite index from the markdown files, which are
        authoritative. Makes hand-edited or restored files usable again."""
        self.database.clear_long_term_facts(tenant_id, user_id)
        directory = self.owner_dir(tenant_id, user_id) / "facts"
        count = 0
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            fact = self.load(tenant_id, user_id, path.stem)
            if fact is None:
                continue
            merge_key = f"{fact.fact_type}|{fact.conversation_id}|{','.join(fact.resource_ids)}"
            self.database.upsert_long_term_fact({
                "name": fact.name,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "conversation_id": fact.conversation_id,
                "merge_key": merge_key,
                "fact_type": fact.fact_type,
                "description": fact.description,
                "outcome": fact.outcome,
                "tools_used": json.dumps(list(fact.tools_used), ensure_ascii=False),
                "resource_ids": json.dumps(list(fact.resource_ids), ensure_ascii=False),
                "observed_at": fact.observed_at,
            })
            count += 1
        if count:
            self._write_index(tenant_id, user_id)
        return count


def _rank_by_keywords(facts: list[Fact], keywords: str) -> list[Fact]:
    from rank_bm25 import BM25Okapi

    from backend.memory.retriever import tokenize

    query = set(tokenize(keywords))
    corpus = [tokenize(f"{fact.description} {fact.body}") for fact in facts]
    if not query or not any(corpus):
        return facts
    # Token overlap decides relevance; BM25 only orders the survivors. BM25's
    # IDF degenerates to exactly zero on a tiny corpus (a term present in one of
    # two documents scores 0), so scoring alone would discard every result
    # precisely when a user's memory is new. Facts sharing no term are dropped
    # rather than padding the result to `limit` — an unrelated record presented
    # as history is worse than returning nothing.
    matched = [
        (index, len(query & set(tokens)))
        for index, tokens in enumerate(corpus)
        if query & set(tokens)
    ]
    if not matched:
        return []
    scores = BM25Okapi(corpus).get_scores(tokenize(keywords))
    matched.sort(key=lambda row: (-scores[row[0]], -row[1], row[0]))
    return [facts[index] for index, _ in matched]


long_term_memory = LongTermMemory()
