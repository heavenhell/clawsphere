"""Audit logging for agent sessions.

Two independent, daily-rotating (gzip-compressed) JSONL logs live under the
project root's ``logs/`` directory, each keeping only the last ``RETENTION_DAYS``
days. Records are recursively redacted before writing and oversized records
are compacted to a bounded metadata summary:

* ``grounding_rejections.log`` — every answer the responder produced but which
  ``verify_resource_claims`` rejected, with the rejection reason after
  sanitization (the rejected ``answer`` strings would otherwise be ephemeral).

* ``session_turns.log`` — one record per completed turn of a conversation
  (normal answers included), supporting a sanitized replay from disk.
"""

from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.agent.llm import _is_sensitive_key, _redact_text

# Project root: backend/agent/audit_log.py -> parents[2] == clawsphere.
ROOT_DIR = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT_DIR / "logs"
RETENTION_DAYS = 7
MAX_AUDIT_RECORD_BYTES = 64 * 1024
_TRUNCATED_TEXT_BYTES = 2 * 1024

_COMPACT_FIELDS = (
    "ts",
    "conversation_id",
    "task_id",
    "user_id",
    "tenant_id",
    "attempt",
    "reason",
    "message",
    "answer",
    "intent",
    "response_source",
    "fallback_reason",
    "agent_step_count",
)

GROUNDING_LOG_FILE = LOG_DIR / "grounding_rejections.log"
SESSION_LOG_FILE = LOG_DIR / "session_turns.log"

_loggers: dict[str, logging.Logger] = {}
_logger_lock = threading.Lock()


class _GzipDailyRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Daily-rotating handler whose rolled-over files are gzip-compressed."""

    def rotation_filename(self, default_name: str) -> str:
        return default_name + ".gz"

    def rotate(self, source: str, dest: str) -> None:
        with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.remove(source)
        _prune_expired(Path(dest).parent, Path(source).name, RETENTION_DAYS)


def _prune_expired(log_dir: Path, prefix: str, retention_days: int) -> None:
    """Delete gzip archives older than ``retention_days`` from ``log_dir``.

    ``prefix`` is the live log's basename (e.g. ``session_turns.log``); archives
    are matched as ``<prefix>.<date>.gz``.
    """
    cutoff = time.time() - retention_days * 24 * 3600
    if not log_dir.exists():
        return
    try:
        entries = list(log_dir.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.is_file():
            continue
        name = path.name
        if not (name.startswith(prefix + ".") and name.endswith(".gz")):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _get_rolling_logger(name: str, filename: Path) -> logging.Logger:
    logger = _loggers.get(name)
    if logger is not None:
        return logger
    with _logger_lock:
        logger = _loggers.get(name)
        if logger is not None:
            return logger
        filename.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        handler = _GzipDailyRotatingFileHandler(
            str(filename), when="midnight", interval=1, backupCount=0, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        _loggers[name] = logger
        return logger


def _sanitize_audit_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _is_sensitive_key(str(key))
                else _sanitize_audit_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_audit_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip() + "…"


def _serialize_bounded_record(record: dict[str, Any]) -> str:
    sanitized = _sanitize_audit_value(record)
    serialized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
    encoded_size = len(serialized.encode("utf-8"))
    if encoded_size <= MAX_AUDIT_RECORD_BYTES:
        return serialized

    compact: dict[str, Any] = {}
    for key in _COMPACT_FIELDS:
        if key not in sanitized:
            continue
        value = sanitized[key]
        if isinstance(value, str):
            compact[key] = _truncate_utf8(value, _TRUNCATED_TEXT_BYTES)
        elif value is None or isinstance(value, (bool, int, float)):
            compact[key] = value
    omitted_fields = sorted(set(sanitized) - set(compact))
    compact["_audit_truncated"] = {
        "original_bytes": encoded_size,
        "max_bytes": MAX_AUDIT_RECORD_BYTES,
        "omitted_field_count": len(omitted_fields),
        "omitted_fields": [
            _truncate_utf8(key, 128) for key in omitted_fields[:100]
        ],
    }
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _write_jsonl(name: str, filename: Path, record: dict[str, Any]) -> None:
    try:
        _get_rolling_logger(name, filename).info(_serialize_bounded_record(record))
    except Exception:
        # Auditing must never break the main agent pipeline.
        return


def log_grounding_rejection(
    answer: str,
    reason: str,
    attempt: int,
    conversation_id: str = "",
    task_id: str = "",
    claims: list[dict[str, Any]] | None = None,
) -> None:
    """Append one grounding-rejection record to the rotating audit log."""
    _write_jsonl(
        "clawsphere.grounding_rejections",
        GROUNDING_LOG_FILE,
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "conversation_id": conversation_id,
            "task_id": task_id,
            "attempt": attempt,
            "reason": reason,
            "answer": answer,
            "claims": claims or [],
        },
    )


def log_session_turn(record: dict[str, Any]) -> None:
    """Append one full-turn record to the session audit log.

    ``record`` is a dict of the turn's observability fields (message, answer,
    route_decisions, llm_stage_metrics, tool results, ...); a ``ts`` timestamp is
    prepended automatically. Sensitive values are redacted, and oversized
    records retain only bounded identifying and summary fields.
    """
    _write_jsonl(
        "clawsphere.session_turns",
        SESSION_LOG_FILE,
        {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record},
    )


# --- Session export ----------------------------------------------------------


def _read_log_file(path: Path) -> list[dict[str, Any]]:
    """Parse every JSON record from a log file (gzip or plain), skipping blank
    and malformed lines so a single bad write can't break an export."""
    records: list[dict[str, Any]] = []
    opener = gzip.open if path.suffix == ".gz" else open
    mode = "rt" if path.suffix == ".gz" else "r"
    try:
        with opener(path, mode, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return records
    return records


def export_session(
    conversation_id: str,
    log_file: Path = SESSION_LOG_FILE,
) -> list[dict[str, Any]]:
    """Return every turn of ``conversation_id`` in chronological order.

    Reads the live log plus all of its gzip archives in the same directory, so
    a session can be reconstructed even after daily rotation has archived some
    of its turns. Records are already sanitized by ``log_session_turn``.
    """
    if not conversation_id:
        raise ValueError("conversation_id must be non-empty")

    records: list[dict[str, Any]] = []
    if log_file.exists():
        records.extend(_read_log_file(log_file))

    log_dir = log_file.parent
    if log_dir.exists():
        prefix = log_file.name + "."
        for archive in sorted(log_dir.glob(prefix + "*.gz")):
            records.extend(_read_log_file(archive))

    matched = [r for r in records if r.get("conversation_id") == conversation_id]
    matched.sort(key=lambda r: r.get("ts", ""))
    return matched


def _format_session_text(records: list[dict[str, Any]], verbose: bool = False) -> str:
    if not records:
        return "(no records found)"

    lines: list[str] = []
    for i, r in enumerate(records, 1):
        lines.append(f"===== Turn {i} · {r.get('ts', '')} =====")
        lines.append(f"task_id: {r.get('task_id', '')}")
        meta = " | ".join(
            f"{k}: {r.get(k)}" for k in ("intent", "response_source", "fallback_reason") if r.get(k)
        )
        lines.append(meta or "(no stage metadata)")
        lines.append("")
        lines.append(f"[user] {r.get('message', '')}")
        lines.append("")
        lines.append(f"[assistant] {r.get('answer', '')}")
        if verbose:
            for section, value in (
                ("route_decisions", r.get("route_decisions", [])),
                ("tool_calls", r.get("tool_calls", [])),
                ("tool_results", r.get("tool_results", [])),
                ("resource_claims", r.get("resource_claims", [])),
                ("llm_stage_metrics", r.get("llm_stage_metrics", [])),
                ("plan", r.get("plan", [])),
            ):
                if value:
                    lines.append("")
                    lines.append(f"-- {section} --")
                    lines.append(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Export a full conversation from the session audit log "
        "(reads the live log plus its gzip archives)."
    )
    parser.add_argument("conversation_id", help="conversation_id to reconstruct")
    parser.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="output format (default: text)",
    )
    parser.add_argument(
        "--log", default=None,
        help="path to session_turns.log (default: logs/session_turns.log)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="include route_decisions/tool_calls/metrics in text output",
    )
    args = parser.parse_args(argv)

    log_file = Path(args.log) if args.log else SESSION_LOG_FILE
    records = export_session(args.conversation_id, log_file)

    if args.format == "json":
        print(json.dumps(records, ensure_ascii=False, indent=2, default=str))
    else:
        print(_format_session_text(records, verbose=args.verbose))
    return 0
