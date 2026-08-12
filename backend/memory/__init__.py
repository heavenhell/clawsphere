# Lazy imports to avoid circular dependency with guardrails.mcp

import importlib
from typing import Any

__all__ = [
    "KnowledgeStore",
    "MemoryDatabase",
    "PostgresKnowledgeStore",
    "create_knowledge_store",
    "deterministic_summary",
    "ensure_knowledge_seeded",
    "estimate_tokens",
    "extract_working_context",
    "knowledge_store",
    "list_memory_writes",
    "manage_context_window",
    "memory_db",
    "retrieve",
    "retrieve_history",
    "retrieve_tools",
    "select_relevant_messages",
    "tokenize",
    "write_conversation_summary",
]


def __getattr__(name: str) -> Any:
    if name == "MemoryDatabase":
        from backend.memory.database import MemoryDatabase; return MemoryDatabase
    if name == "memory_db":
        from backend.memory.database import memory_db; return memory_db
    if name == "deterministic_summary":
        from backend.memory.context_manager import deterministic_summary; return deterministic_summary
    if name == "estimate_tokens":
        from backend.memory.context_manager import estimate_tokens; return estimate_tokens
    if name == "extract_working_context":
        from backend.memory.context_manager import extract_working_context; return extract_working_context
    if name == "manage_context_window":
        from backend.memory.context_manager import manage_context_window; return manage_context_window
    if name == "select_relevant_messages":
        from backend.memory.context_manager import select_relevant_messages; return select_relevant_messages
    if name == "KnowledgeStore":
        from backend.memory.knowledge_store import KnowledgeStore; return KnowledgeStore
    if name == "PostgresKnowledgeStore":
        from backend.memory.knowledge_store import PostgresKnowledgeStore; return PostgresKnowledgeStore
    if name == "create_knowledge_store":
        from backend.memory.knowledge_store import create_knowledge_store; return create_knowledge_store
    if name == "knowledge_store":
        from backend.memory.knowledge_store import knowledge_store; return knowledge_store
    if name == "retrieve":
        from backend.memory.retriever import retrieve; return retrieve
    if name == "retrieve_history":
        from backend.memory.retriever import retrieve_history; return retrieve_history
    if name == "retrieve_tools":
        from backend.memory.retriever import retrieve_tools; return retrieve_tools
    if name == "ensure_knowledge_seeded":
        from backend.memory.retriever import ensure_knowledge_seeded; return ensure_knowledge_seeded
    if name == "tokenize":
        from backend.memory.retriever import tokenize; return tokenize
    if name == "list_memory_writes":
        from backend.memory.store import list_memory_writes; return list_memory_writes
    if name == "write_conversation_summary":
        from backend.memory.store import write_conversation_summary; return write_conversation_summary
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
