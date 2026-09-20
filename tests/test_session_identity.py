"""Session-identity passthrough: goals/attempts carry their real session_ref.

Guards the D03 follow-up: work_state must never masquerade as a session path,
heartbeats carry the persisted session identity, and legacy DBs gain the
events.session_ref column without a rebuild.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from cmo.db import SCHEMA_SQL, connect, ensure_schema
from cmo.events import EventStore


def _goal_row(store: EventStore, goal_id: str) -> dict:
    row = store._conn.execute(
        "SELECT * FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
    return dict(row) if row else {}


def _attempt_row(store: EventStore, attempt_id: str) -> dict:
    row = store._conn.execute(
        "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    return dict(row) if row else {}


def test_goal_started_carries_session_ref(tmp_path: Path):
    store = EventStore(tmp_path / "s1.sqlite3")
    try:
        store.ingest({
            "event_id": "g-1", "event_type": "goal.started",
            "occurred_at": 1_789_900_000_000, "source_component": "pi",
            "goal_id": "goal-x", "repo": "owner/repo", "strategy": "free-first",
            "free_only": 1, "work_state": "active",
            "session_ref": "20260920-0900-abc123.jsonl",
        })
        row = _goal_row(store, "goal-x")
        assert row["session_ref"] == "20260920-0900-abc123.jsonl"
        assert row["repo"] == "owner/repo"
        assert row["free_only"] == 1
    finally:
        store.close()


def test_work_state_never_masquerades_as_session(tmp_path: Path):
    store = EventStore(tmp_path / "s2.sqlite3")
    try:
        store.ingest({
            "event_id": "g-2", "event_type": "goal.started",
            "occurred_at": 1_789_900_000_000, "source_component": "pi",
            "goal_id": "goal-y", "work_state": "active",
        })
        row = _goal_row(store, "goal-y")
        assert row["session_ref"] in (None, ""), \
            "work_state must not be stored as a session identity"
    finally:
        store.close()


def test_attempt_started_and_heartbeat_carry_session_ref(tmp_path: Path):
    store = EventStore(tmp_path / "s3.sqlite3")
    try:
        base = {"source_component": "pi", "account_alias": "account-1",
                "provider": "cline", "model": "m/x", "tier": "free"}
        store.ingest({**base, "event_id": "a-1", "event_type": "attempt.started",
                      "occurred_at": 1_789_900_000_000, "attempt_id": "att-1",
                      "goal_id": "goal-z", "work_state": "launching",
                      "session_ref": "sess-abc.jsonl"})
        row = _attempt_row(store, "att-1")
        assert row["session_ref"] == "sess-abc.jsonl"
        # Heartbeat refreshes both the beat and (idempotently) the session id.
        store.ingest({**base, "event_id": "a-2", "event_type": "attempt.heartbeat",
                      "occurred_at": 1_789_900_030_000, "attempt_id": "att-1",
                      "goal_id": "goal-z", "session_ref": "sess-abc.jsonl"})
        row = _attempt_row(store, "att-1")
        assert row["last_heartbeat_at"] == 1_789_900_030_000
        assert row["session_ref"] == "sess-abc.jsonl"
    finally:
        store.close()


def test_legacy_events_table_gains_session_ref(tmp_path: Path):
    db = tmp_path / "legacy.sqlite3"
    # Pre-Stage-E schema: identical to current, minus events.session_ref.
    legacy_sql = SCHEMA_SQL.replace("    session_ref TEXT,\n", "")
    assert legacy_sql != SCHEMA_SQL
    conn = sqlite3.connect(str(db))
    conn.executescript(legacy_sql)
    conn.close()
    conn = connect(db)
    try:
        ensure_schema(conn)
        cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(events)").fetchall()}
        assert "session_ref" in cols
    finally:
        conn.close()
