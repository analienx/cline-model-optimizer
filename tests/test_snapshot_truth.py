"""Snapshot truth gates: freshness contract, unknown reasons, goal liveness."""
from __future__ import annotations

from pathlib import Path

import pytest

from cmo.events import EventStore
from cmo.snapshot import build_snapshot


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CMO_TEST_MODE", "isolated")
    return tmp_path / "test-snapshot-truth.sqlite3"


def _snap(path: Path) -> dict:
    store = EventStore(path)
    try:
        return build_snapshot(store)
    finally:
        store.close()


def test_freshness_contract_has_both_keys(db: Path):
    snap = _snap(db)
    fresh = snap["freshness"]
    assert "browser_freshness_ms" in fresh
    assert "browser_clock_ms" in fresh
    assert isinstance(fresh["browser_freshness_ms"], int)


def test_cells_carry_unknown_reasons(db: Path):
    snap = _snap(db)
    unknowns = [c for c in snap["cells"] if c["state"] in ("UNKNOWN", "STALE")]
    assert unknowns, "a fresh policy DB must have unobserved routes"
    for cell in unknowns:
        assert cell["unknown_reason"], f"{cell['route_key']} lacks an unknown reason"
        assert "never observed" in cell["unknown_reason"]


def test_data_quality_flags_fixtures(db: Path):
    store = EventStore(db)
    try:
        store.ingest({
            "event_id": "fix-1", "event_type": "route.probe.succeeded",
            "occurred_at": 1_700_000_000_000, "source_component": "browser-acceptance",
            "goal_id": "goal-live-9", "account_alias": "account-1",
            "provider": "cline", "model": "m9", "tier": "free",
        })
        snap = build_snapshot(store)
    finally:
        store.close()
    quality = snap["data_quality"]
    assert quality["fixture_events"] == 1
    assert quality["banner"], "fixture presence must raise a banner"


def test_stale_active_goal_not_selected(db: Path):
    store = EventStore(db)
    try:
        old = 1_700_000_000_000
        store.ingest({"event_id": "g-old", "event_type": "goal.started",
                      "occurred_at": old, "source_component": "pi",
                      "goal_id": "goal-stale-1", "strategy": "free-first"})
        store.ingest({"event_id": "a-new", "event_type": "attempt.started",
                      "occurred_at": old + 10 * 86_400_000,
                      "source_component": "pi", "goal_id": "goal-fresh-1",
                      "attempt_id": "attempt-fresh-1", "account_alias": "account-1",
                      "provider": "cline", "model": "m1", "tier": "free"})
        snap = build_snapshot(store, now=old + 10 * 86_400_000)
    finally:
        store.close()
    by_id = {g["goal_id"]: g for g in snap["goals"]}
    assert by_id["goal-stale-1"]["display_status"] == "disconnected"
    assert by_id["goal-stale-1"]["liveness"] == "disconnected"
    assert snap["selected_goal_id"] == "goal-fresh-1"
