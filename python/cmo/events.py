"""CMO v2 event store: cmo.event/v1 schema, SQLite WAL, idempotent ingest.

Only the whitelisted event fields are ever persisted. Tokens, raw
prompts/responses and complete provider errors are rejected at ingest.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

EVENT_SCHEMA = "cmo.event/v1"

EVENT_FIELDS = (
    "event_id", "event_type", "occurred_at", "source_component", "source_instance",
    "goal_id", "attempt_id", "strategy", "account_alias", "provider", "model",
    "tier", "reason_code", "reset_after_ms", "safe_detail",
)

EVENT_TYPES = {
    "goal.started", "goal.resumed", "goal.completed", "goal.failed", "goal.context_escalated",
    "route.probe.started", "route.probe.succeeded", "route.probe.failed",
    "attempt.started", "attempt.heartbeat", "attempt.completed", "attempt.failed",
    "catalog.refreshed", "catalog.failed",
    "override.created", "override.cleared", "override.expired",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source_component TEXT,
    source_instance TEXT,
    goal_id TEXT,
    attempt_id TEXT,
    strategy TEXT,
    account_alias TEXT,
    provider TEXT,
    model TEXT,
    tier TEXT CHECK (tier IN ('free','subscription') OR tier IS NULL),
    reason_code TEXT,
    reset_after_ms INTEGER,
    safe_detail TEXT,
    ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class EventValidationError(ValueError):
    pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate/normalize one inbound event; scrub anything outside the schema."""
    if not isinstance(raw, dict):
        raise EventValidationError("event must be a JSON object")
    event_type = raw.get("event_type")
    if event_type not in EVENT_TYPES:
        raise EventValidationError(f"unknown event_type: {event_type!r}")
    event = {field: raw.get(field) for field in EVENT_FIELDS}
    event["event_id"] = event["event_id"] or uuid.uuid4().hex
    event["occurred_at"] = event["occurred_at"] or _now_iso()
    if event["tier"] is not None and event["tier"] not in ("free", "subscription"):
        raise EventValidationError(f"tier must be free|subscription (PAYG impossible), got {event['tier']!r}")
    # safe_detail must be a short scrubbed summary; refuse oversized payloads.
    detail = event.get("safe_detail")
    if detail is not None:
        if not isinstance(detail, str):
            detail = json.dumps(detail, sort_keys=True, ensure_ascii=False)
        if len(detail) > 512:
            detail = detail[:509] + "..."
        event["safe_detail"] = detail
    else:
        event["safe_detail"] = None
    if event.get("reset_after_ms") is not None and not isinstance(event["reset_after_ms"], int):
        raise EventValidationError("reset_after_ms must be an integer or null")
    return event


class EventStore:
    """SQLite WAL event store with idempotent (event_id-keyed) ingestion."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.execute(
            "INSERT INTO metadata(key, value) VALUES('schema','cmo.event/v1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

    def close(self) -> None:
        self._conn.close()

    def ingest(self, raw: dict[str, Any]) -> tuple[bool, str]:
        """Idempotent ingest. Returns (inserted, event_id)."""
        event = normalize_event(raw)
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO events (" + ",".join(EVENT_FIELDS) + ", ingested_at) "
            "VALUES (" + ",".join("?" for _ in EVENT_FIELDS) + ", ?)",
            tuple(event[f] for f in EVENT_FIELDS) + (_now_iso(),),
        )
        return cur.rowcount > 0, event["event_id"]

    def events(self, limit: int = 200, event_type: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        args: tuple = ()
        if event_type:
            sql += " WHERE event_type = ?"
            args = (event_type,)
        sql += " ORDER BY occurred_at DESC, event_id LIMIT ?"
        rows = self._conn.execute(sql, args + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
