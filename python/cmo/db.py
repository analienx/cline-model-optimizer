"""SQLite storage layer: schema, connections, transactions and leases.

Every request/process opens its own connection. Ingest and its projection
update run inside one ``BEGIN IMMEDIATE`` transaction with bounded retries.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from . import SCHEMA_NAME, SCHEMA_VERSION

BUSY_TIMEOUT_MS = 5000
MAX_TX_RETRIES = 4

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    received_at INTEGER NOT NULL,
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
    reset_known INTEGER,
    capability TEXT,
    repo TEXT,
    work_state TEXT,
    free_only INTEGER,
    safe_detail TEXT,
    payload_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at, seq);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_goal ON events(goal_id, occurred_at);

CREATE TABLE IF NOT EXISTS goals (
    goal_id TEXT PRIMARY KEY,
    repo TEXT,
    strategy TEXT NOT NULL DEFAULT 'free-first',
    effective_strategy TEXT NOT NULL DEFAULT 'free-first',
    free_only INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    session_ref TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    started_at INTEGER,
    updated_at INTEGER,
    last_activity_at INTEGER,
    reason_code TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    goal_id TEXT,
    route_key TEXT,
    model TEXT,
    provider TEXT,
    account_alias TEXT,
    tier TEXT,
    status TEXT NOT NULL,
    started_at INTEGER,
    ended_at INTEGER,
    last_heartbeat_at INTEGER,
    reason_code TEXT,
    session_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_goal ON attempts(goal_id);
CREATE INDEX IF NOT EXISTS idx_attempts_route ON attempts(route_key);

CREATE TABLE IF NOT EXISTS route_state (
    route_key TEXT PRIMARY KEY,
    account_alias TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    tier TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'UNKNOWN',
    state_changed_at INTEGER NOT NULL DEFAULT 0,
    observed_at INTEGER,
    received_at INTEGER,
    source_event_id TEXT,
    reset_after_ms INTEGER,
    reset_known INTEGER,
    reset_at INTEGER,
    next_check_at INTEGER,
    last_reason_code TEXT,
    capability TEXT,
    transient_retries INTEGER NOT NULL DEFAULT 0,
    evidence_source TEXT
);

CREATE TABLE IF NOT EXISTS catalog_models (
    model TEXT PRIMARY KEY,
    capability TEXT NOT NULL,
    catalog_seen_at INTEGER,
    source TEXT,
    last_error_at INTEGER
);

CREATE TABLE IF NOT EXISTS overrides (
    override_id TEXT PRIMARY KEY,
    route_key TEXT,
    model TEXT NOT NULL,
    provider TEXT,
    account_alias TEXT,
    tier TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('FORCE_SKIP')),
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,
    source_event_id TEXT
);

CREATE TABLE IF NOT EXISTS probe_leases (
    route_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    acquired_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_receipts (
    source_fingerprint TEXT PRIMARY KEY,
    source_path TEXT,
    imported_at INTEGER,
    event_count INTEGER,
    warnings TEXT
);

CREATE TABLE IF NOT EXISTS service_heartbeats (
    instance TEXT PRIMARY KEY,
    started_at INTEGER,
    heartbeat_at INTEGER,
    artifact_digest TEXT,
    pid INTEGER
);

CREATE TABLE IF NOT EXISTS recheck_requests (
    request_id TEXT PRIMARY KEY,
    route_key TEXT,
    requested_at INTEGER,
    requested_by TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    started_at INTEGER,
    finished_at INTEGER,
    lease_owner TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""

_RECHECK_COLUMNS = ("started_at", "finished_at", "lease_owner", "attempt",
                    "last_error")


def _ensure_recheck_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in
                conn.execute("PRAGMA table_info(recheck_requests)").fetchall()}
    for column in _RECHECK_COLUMNS:
        if column not in existing:
            definition = "INTEGER" if column in ("started_at", "finished_at") \
                else "INTEGER NOT NULL DEFAULT 0" if column == "attempt" \
                else "TEXT"
            conn.execute(f"ALTER TABLE recheck_requests ADD COLUMN {column} {definition}")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False,
                           timeout=BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.executescript(SCHEMA_SQL)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    _ensure_recheck_columns(conn)
    row = conn.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("INSERT INTO metadata(key, value) VALUES('schema_name', ?)",
                     (SCHEMA_NAME,))
        conn.execute("INSERT INTO metadata(key, value) VALUES('state_revision', '0')")
    else:
        found = int(row["value"])
        if found > SCHEMA_VERSION:
            raise RuntimeError(
                f"state schema {found} is newer than this build ({SCHEMA_VERSION}); "
                "refusing to downgrade evidence")


def bump_revision(conn: sqlite3.Connection, amount: int = 1) -> int:
    row = conn.execute("SELECT value FROM metadata WHERE key='state_revision'").fetchone()
    current = int(row["value"]) if row else 0
    new_value = current + amount
    conn.execute("INSERT INTO metadata(key, value) VALUES('state_revision', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(new_value),))
    return new_value


def read_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM metadata WHERE key='state_revision'").fetchone()
    return int(row["value"]) if row else 0


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO metadata(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


class TransactionBusy(RuntimeError):
    """Raised when the store stayed locked after the bounded retry budget."""


def run_transaction(conn: sqlite3.Connection, work: Callable[[], Any]) -> Any:
    """Run ``work`` inside BEGIN IMMEDIATE with bounded busy retries."""
    last: Exception | None = None
    for attempt in range(MAX_TX_RETRIES):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:  # pragma: no cover - timing dependent
            last = exc
            if _is_busy(exc):
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
        try:
            result = work()
            conn.execute("COMMIT")
            return result
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            raise
    raise TransactionBusy(f"store stayed locked after {MAX_TX_RETRIES} attempts: {last}")


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


@contextmanager
def lease(conn: sqlite3.Connection, route_key_value: str, owner: str,
          now_ms: int, ttl_ms: int) -> Iterator[bool]:
    """Acquire a short probe lease; yields True when this owner holds it."""
    acquired = False
    try:
        run_transaction(conn, lambda: _acquire_lease(conn, route_key_value, owner, now_ms, ttl_ms))
        acquired = True
    except _LeaseHeld:
        acquired = False
    try:
        yield acquired
    finally:
        if acquired:
            run_transaction(conn, lambda: conn.execute(
                "DELETE FROM probe_leases WHERE route_key=? AND owner=?",
                (route_key_value, owner)))


class _LeaseHeld(Exception):
    pass


def _acquire_lease(conn: sqlite3.Connection, route_key_value: str, owner: str,
                   now_ms: int, ttl_ms: int) -> None:
    row = conn.execute("SELECT owner, expires_at FROM probe_leases WHERE route_key=?",
                       (route_key_value,)).fetchone()
    if row is not None and row["owner"] != owner and int(row["expires_at"]) > now_ms:
        raise _LeaseHeld(route_key_value)
    conn.execute(
        "INSERT INTO probe_leases(route_key, owner, acquired_at, expires_at) VALUES(?,?,?,?) "
        "ON CONFLICT(route_key) DO UPDATE SET owner=excluded.owner, "
        "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
        (route_key_value, owner, now_ms, now_ms + ttl_ms))
