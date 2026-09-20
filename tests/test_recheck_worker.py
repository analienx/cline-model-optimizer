"""Recheck worker: one leg per pass, honest settlement, no silent pending."""
from __future__ import annotations

from pathlib import Path

import pytest

from cmo.events import EventStore
from cmo.policy import TIER_FREE
from cmo.recheck import split_route_key, step


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CMO_TEST_MODE", "isolated")
    return tmp_path / "test-recheck-worker.sqlite3"


ROUTE = {"account_alias": "account-1", "provider": "cline",
         "model": "m1", "tier": TIER_FREE}


def _request(store: EventStore, event_id: str = "rq-1",
             at: int = 1_700_000_000_000) -> dict:
    return store.ingest({"event_id": event_id, "event_type": "route.recheck.requested",
                         "occurred_at": at, "source_component": "ui", **ROUTE})


def test_step_settles_with_injected_probe(db: Path):
    store = EventStore(db)
    try:
        _request(store)
        calls = []

        def fake_probe(route_key_value: str) -> dict:
            calls.append(route_key_value)
            return {"outcome": "succeeded", "reason": "recheck.probe_succeeded"}

        report = step(store, probe=fake_probe)
        assert report["claimed"] == 1 and report["settled"] == 1
        assert calls == ["account-1|cline|m1|free"]
        rows = store.route_state()
        assert rows and rows[0]["state"] == "AVAILABLE"
        assert not store.recheck_requests(status="pending")
        assert not store.recheck_requests(status="running")
    finally:
        store.close()


def test_step_without_executor_blocks_honestly(db: Path, monkeypatch):
    monkeypatch.delenv("CMO_PROBE_COMMAND", raising=False)
    monkeypatch.delenv("PI_MODEL_PROBE", raising=False)
    store = EventStore(db)
    try:
        _request(store)
        report = step(store)
        assert report["outcome"] == "blocked"
        assert "no_probe_executor" in report["reason"]
        rows = store.route_state()
        # Blocked without a probe must not fabricate route evidence.
        assert rows == [] or all(r["state"] == "UNKNOWN" for r in rows)
    finally:
        store.close()


def test_step_times_out_stale_running(db: Path):
    store = EventStore(db)
    try:
        _request(store, at=1_700_000_000_000)
        store.ingest({"event_id": "rs-stale", "event_type": "route.recheck.started",
                      "occurred_at": 1_700_000_000_000,
                      "source_component": "cmo-worker", **ROUTE})
        report = step(store, probe=lambda rk: (_ for _ in ()).throw(
            AssertionError("probe must not run on a timeout-only pass")))
        assert report["timed_out"] == 1
        finished = store._conn.execute(
            "SELECT status FROM recheck_requests WHERE request_id='rq-1'").fetchone()
        assert finished["status"] == "finished:failed"
    finally:
        store.close()


def test_split_route_key_rejects_malformed():
    with pytest.raises(ValueError):
        split_route_key("not-a-route-key")
    with pytest.raises(ValueError):
        split_route_key("a|b|c")
    assert split_route_key("a|b|c|d") == ("a", "b", "c", "d")
