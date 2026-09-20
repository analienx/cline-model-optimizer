"""Fixture quarantine + recheck lifecycle (first-class dashboard truth gates).

Runs against isolated temp databases with CMO_TEST_MODE=isolated.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cmo import events as events_mod
from cmo.events import EventStore, EventValidationError, is_test_db
from cmo.migration import quarantine_fixtures
from cmo.policy import TIER_FREE


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CMO_TEST_MODE", "isolated")
    return tmp_path / "test-cmo-isolated.sqlite3"


@pytest.fixture()
def live_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("CMO_TEST_MODE", raising=False)
    return tmp_path / "state" / "cmo-live.sqlite3"


def _route_event(account: str = "account-1", model: str = "m1",
                 source: str = "pi", goal: str = "goal-live-1",
                 event_type: str = "route.probe.succeeded") -> dict:
    return {
        "event_id": f"evt-{account}-{model}-{source}-{event_type}",
        "event_type": event_type,
        "occurred_at": 1_700_000_000_000,
        "source_component": source,
        "goal_id": goal,
        "account_alias": account,
        "provider": "cline",
        "model": model,
        "tier": TIER_FREE,
    }


def test_is_test_db_requires_both_conditions(tmp_path, monkeypatch):
    live = Path(r"C:\Workspace\state\cmo.sqlite3")
    monkeypatch.delenv("CMO_TEST_MODE", raising=False)
    assert not is_test_db(tmp_path / "test-x.sqlite3")
    assert not is_test_db(live)
    monkeypatch.setenv("CMO_TEST_MODE", "isolated")
    assert is_test_db(tmp_path / "test-x.sqlite3")
    assert is_test_db(tmp_path / "sub" / "isolated.sqlite3")
    assert is_test_db(tmp_path / "anything.sqlite3")  # tmp_path is under temp dir
    assert not is_test_db(live)


def test_live_db_rejects_fixture_source(live_db: Path):
    store = EventStore(live_db)
    try:
        with pytest.raises(EventValidationError):
            store.ingest(_route_event(source="browser-acceptance"))
        with pytest.raises(EventValidationError):
            store.ingest(_route_event(goal="goal-42"))
        with pytest.raises(EventValidationError):
            store.ingest(_route_event(account="a", model="m",
                                      event_type="route.probe.failed"))
    finally:
        store.close()


def test_isolated_db_accepts_fixture_source(isolated_db: Path):
    store = EventStore(isolated_db)
    try:
        result = store.ingest(_route_event(source="browser-acceptance"))
        assert result["inserted"]
    finally:
        store.close()


def test_recheck_request_dedup(isolated_db: Path):
    store = EventStore(isolated_db)
    try:
        first = store.ingest({
            "event_id": "recheck-1",
            "event_type": "route.recheck.requested",
            "occurred_at": 1_700_000_000_000,
            "source_component": "ui",
            "account_alias": "account-1",
            "provider": "cline",
            "model": "m1",
            "tier": TIER_FREE,
        })
        assert first["projection"] == "applied"
        second = store.ingest({
            "event_id": "recheck-2",
            "event_type": "route.recheck.requested",
            "occurred_at": 1_700_000_000_100,
            "source_component": "ui",
            "account_alias": "account-1",
            "provider": "cline",
            "model": "m1",
            "tier": TIER_FREE,
        })
        assert second["projection"] == "duplicate"
        pending = store.recheck_requests(status="pending")
        assert len(pending) == 1
        assert pending[0]["request_id"] == "recheck-1"
    finally:
        store.close()


def test_recheck_lifecycle_moves_route_state(isolated_db: Path):
    store = EventStore(isolated_db)
    route = {"account_alias": "account-1", "provider": "cline",
             "model": "m1", "tier": TIER_FREE}
    try:
        store.ingest({"event_id": "rq-1", "event_type": "route.recheck.requested",
                      "occurred_at": 1_700_000_000_000,
                      "source_component": "ui", **route})
        store.ingest({"event_id": "rs-1", "event_type": "route.recheck.started",
                      "occurred_at": 1_700_000_000_100,
                      "source_component": "cmo-worker", **route})
        running = store.recheck_requests(status="running")
        assert len(running) == 1
        assert running[0]["started_at"] == 1_700_000_000_100
        store.ingest({"event_id": "rf-1", "event_type": "route.recheck.succeeded",
                      "occurred_at": 1_700_000_000_200,
                      "source_component": "cmo-worker", **route})
        finished = store._conn.execute(
            "SELECT * FROM recheck_requests WHERE request_id='rq-1'").fetchone()
        assert finished["status"] == "finished:succeeded"
        assert finished["finished_at"] == 1_700_000_000_200
        rows = store.route_state()
        assert rows and rows[0]["state"] == "AVAILABLE"
    finally:
        store.close()


def test_quarantine_removes_fixtures_and_is_idempotent(isolated_db: Path):
    store = EventStore(isolated_db)
    try:
        store.ingest(_route_event(source="browser-acceptance"))
        store.ingest(_route_event(account="account-2", model="m2", source="pi",
                                  goal="goal-live-1"))
        first = quarantine_fixtures(store)
        assert first["ok"] and first["events_removed"] == 1
        remaining = store.events(limit=100)
        assert all(e["source_component"] != "browser-acceptance" for e in remaining)
        second = quarantine_fixtures(store)
        assert second["ok"] and second["events_removed"] == 0
        live_rows = store.route_state()
        assert all(r["model"] != "m1" or r["evidence_source"] != "browser-acceptance"
                   for r in live_rows)
    finally:
        store.close()


def test_live_db_rejects_new_recheck_lifecycle_event_types(live_db: Path):
    # New lifecycle types still require full route identity on live DBs.
    store = EventStore(live_db)
    try:
        with pytest.raises(EventValidationError):
            store.ingest({"event_id": "bad-1",
                          "event_type": "route.recheck.started",
                          "occurred_at": 1_700_000_000_000,
                          "source_component": "cmo-worker"})
    finally:
        store.close()
