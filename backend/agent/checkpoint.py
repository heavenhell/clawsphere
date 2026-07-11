from __future__ import annotations

import os
import sqlite3

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from backend.memory.database import DATA_DIR


_postgres_context = None
_sqlite_connection = None


def create_checkpointer():
    global _postgres_context, _sqlite_connection
    if os.getenv("DCS_CHECKPOINT_BACKEND", "sqlite").lower() == "postgres":
        dsn = os.getenv("POSTGRES_DSN")
        if not dsn:
            raise RuntimeError("DCS_CHECKPOINT_BACKEND=postgres requires POSTGRES_DSN")
        _postgres_context = PostgresSaver.from_conn_string(dsn)
        saver = _postgres_context.__enter__()
        saver.setup()
        return saver
    _sqlite_connection = sqlite3.connect(DATA_DIR / "checkpoints.db", check_same_thread=False)
    return SqliteSaver(_sqlite_connection)


CHECKPOINTER = create_checkpointer()
