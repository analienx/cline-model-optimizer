"""CMO v2 projections: goals, attempts, route_state, overrides, catalog.

Rebuildable entirely from the event store (restart reconstructs identical
projections) plus explicit mutable tables for route_state/overrides.
"""

from __future__ import annotations

PROJECTION_SQL = """
CREATE TABLE IF NOT EXISTS goals (
    goal_id TEXT PRIMARY KEY,
    repo TEXT,
    strategy TEXT NOT NULL DEFAULT 'free-first',
    effective_strategy TEXT NOT NULL DEFAULT 'free-first',
    status TEXT NOT NULL DEFAULT 'active',
    session_ref TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL REFERENCES goals(goal_id),
    model TEXT,
    account_alias TEXT,
    tier TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    reason_code TEXT
);
CREATE TABLE IF NOT EXISTS route_state (
    model TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'UNKNOWN',
    state_changed_at INTEGER NOT NULL DEFAULT 0,
    reset_after_ms INTEGER,
    last_probe_at INTEGER,
    last_reason_code TEXT,
    capability TEXT,
    transient_retries INTEGER NOT NULL DEFAULT 0,
    evidence_source TEXT,
    PRIMARY KEY (model, account_alias)
);
CREATE TABLE IF NOT EXISTS catalog_models (
    model TEXT PRIMARY KEY,
    capability TEXT NOT NULL,
    catalog_seen_at INTEGER,
    source TEXT
);
CREATE TABLE IF NOT EXISTS account_aliases (
    alias TEXT PRIMARY KEY,
    masked_identity TEXT NOT NULL,
    auth_freshness TEXT,
    has_dpapi_bridge INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS overrides (
    override_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    account_alias TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('FORCE_SKIP')),
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_attempts_goal ON attempts(goal_id);
CREATE INDEX IF NOT EXISTS idx_events_goal ON events(goal_id);
"""

REPLAY_HANDLERS = {
    "goal.started": "upsert_goal",
    "goal.resumed": "upsert_goal",
    "goal.completed": "close_goal",
    "goal.failed": "close_goal",
    "goal.context_escalated": "escalate_goal",
    "attempt.started": "upsert_attempt",
    "attempt.completed": "close_attempt",
    "attempt.failed": "close_attempt",
}
