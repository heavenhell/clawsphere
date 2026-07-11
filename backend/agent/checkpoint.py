from __future__ import annotations

import os
import sqlite3
import threading

from langgraph.checkpoint.sqlite import SqliteSaver

from backend.memory.database import DATA_DIR


_lock = threading.Lock()
_checkpointer = None
_postgres_context = None
_sqlite_connection: sqlite3.Connection | None = None


def get_checkpointer():
    global _checkpointer, _postgres_context, _sqlite_connection
    if _checkpointer is not None:
        return _checkpointer
    with _lock:
        if _checkpointer is not None:
            return _checkpointer
        if os.getenv("DCS_CHECKPOINT_BACKEND", "sqlite").lower() == "postgres":
            from langgraph.checkpoint.postgres import PostgresSaver

            dsn = os.getenv("POSTGRES_DSN")
            if not dsn:
                raise RuntimeError("DCS_CHECKPOINT_BACKEND=postgres requires POSTGRES_DSN")
            _postgres_context = PostgresSaver.from_conn_string(dsn)
            _checkpointer = _postgres_context.__enter__()
            _checkpointer.setup()
        else:
            _sqlite_connection = sqlite3.connect(DATA_DIR / "checkpoints.db", check_same_thread=False)
            _checkpointer = SqliteSaver(_sqlite_connection)
        return _checkpointer


def close_checkpointer() -> None:
    global _checkpointer, _postgres_context, _sqlite_connection
    with _lock:
        if _postgres_context is not None:
            _postgres_context.__exit__(None, None, None)
        elif _sqlite_connection is not None:
            _sqlite_connection.close()
        _checkpointer = None
        _postgres_context = None
        _sqlite_connection = None
