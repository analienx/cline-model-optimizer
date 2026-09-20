"""Versioned policy + account registry: validation, ordering, persistence."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from cmo.events import EventStore
from cmo.policy import (PolicyError, account_order, load_canonical_policy, route_cells,
                        routes_for_strategy, validate_policy)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CMO_TEST_MODE", "isolated")
    return tmp_path / "test-policy-versions.sqlite3"


def _doc():
    return copy.deepcopy(load_canonical_policy())


def test_accounts_reject_unknown_route_reference():
    doc = _doc()
    doc["routes"][0]["accounts"] = ["no-such-account"]
    with pytest.raises(PolicyError, match="unknown account"):
        validate_policy(doc)


def test_accounts_reject_duplicates_and_bad_ids():
    doc = _doc()
    doc["accounts"].append({"id": "account-1"})
    with pytest.raises(PolicyError, match="duplicate account"):
        validate_policy(doc)
    doc = _doc()
    doc["accounts"][0]["id"] = "../evil"
    with pytest.raises(PolicyError, match="must match"):
        validate_policy(doc)


def test_accounts_reject_bad_status():
    doc = _doc()
    doc["accounts"][0]["status"] = "superuser"
    with pytest.raises(PolicyError, match="status"):
        validate_policy(doc)


def test_policy_requires_enabled_account():
    doc = _doc()
    for entry in doc["accounts"]:
        entry["enabled"] = False
    with pytest.raises(PolicyError, match="at least one account"):
        validate_policy(doc)


def test_priority_order_drives_cells():
    doc = _doc()
    doc["accounts"][0]["priority"] = 5
    doc["accounts"][2]["priority"] = 0
    doc["accounts"][1]["priority"] = 1
    validate_policy(doc)
    assert account_order(doc) == ["account-3", "account-2", "account-1"]
    first_route = doc["routes"][0]["model"]
    cells = [c for c in route_cells(doc) if c["model"] == first_route]
    assert [c["account_alias"] for c in cells] == ["account-3", "account-2", "account-1"]


def test_disabled_route_and_account_excluded():
    doc = _doc()
    doc["routes"][0]["enabled"] = False
    doc["accounts"][0]["enabled"] = False
    validate_policy(doc)
    cells = route_cells(doc)
    assert all(c["model"] != doc["routes"][0]["model"] for c in cells)
    assert all(c["account_alias"] != "account-1" for c in cells)
    assert doc["routes"][0] not in routes_for_strategy("free-first", doc)


def test_free_model_cap():
    doc = _doc()
    doc["defaults"]["max_enabled_free_models"] = 1
    validate_policy(doc)
    cells = route_cells(doc)
    free_models = {c["model"] for c in cells if c["tier"] == "free"}
    assert free_models == {doc["routes"][0]["model"]}
    # The subscription tail is unaffected by the free-model cap.
    assert any(c["tier"] == "subscription" for c in cells)
    with pytest.raises(PolicyError, match="max_enabled_free_models"):
        bad = _doc()
        bad["defaults"]["max_enabled_free_models"] = 0
        validate_policy(bad)


def test_save_and_rollback_roundtrip(db: Path):
    store = EventStore(db)
    try:
        doc, version, saved = store.active_policy_doc()
        assert (version, saved) == (0, False)
        doc["accounts"][0]["priority"] = 9
        first = store.save_policy_version(doc, created_by="test", note="reorder")
        assert first["version"] == 1
        active, version, is_saved = store.active_policy_doc()
        assert (version, is_saved) == (1, True)
        assert active["accounts"][0]["priority"] == 9
        doc2 = _doc()
        second = store.save_policy_version(doc2, created_by="test", note="restore")
        assert second["version"] == 2
        rolled = store.rollback_policy(1, created_by="test")
        assert rolled["rolled_back_to"] == 1
        active, version, _ = store.active_policy_doc()
        assert version == 3 and active["accounts"][0]["priority"] == 9
        versions = store.policy_versions()
        assert [v["version"] for v in versions] == [3, 2, 1]
        # Audit trail survived in the event log.
        kinds = {e["event_type"] for e in store.events(limit=20)}
        assert {"policy.saved", "policy.rolled_back"} <= kinds
    finally:
        store.close()


def test_save_rejects_invalid_without_touching_active(db: Path):
    store = EventStore(db)
    try:
        good = _doc()
        store.save_policy_version(good, created_by="test")
        bad = _doc()
        bad["routes"] = []
        with pytest.raises(PolicyError):
            store.save_policy_version(bad, created_by="test")
        _, version, _ = store.active_policy_doc()
        assert version == 1
    finally:
        store.close()


def test_expected_digest_conflict(db: Path):
    store = EventStore(db)
    try:
        first = store.save_policy_version(_doc(), created_by="test")
        with pytest.raises(Exception, match="[Cc]onflict"):
            store.save_policy_version(_doc(), created_by="test",
                                      expected_digest="deadbeef")
        assert first["digest"]
    finally:
        store.close()


def test_recheck_history_includes_settled(db: Path):
    store = EventStore(db)
    try:
        store.ingest({"event_id": "rq-h", "event_type": "route.recheck.requested",
                      "occurred_at": 1_700_000_000_000, "source_component": "ui",
                      "account_alias": "account-1", "provider": "cline",
                      "model": "m1", "tier": "free"})
        history = store.recheck_history(limit=10)
        assert history and history[0]["status"] == "pending"
        store.ingest({"event_type": "route.recheck.started",
                      "occurred_at": 1_700_000_000_001, "source_component": "cmo-worker",
                      "reason_code": "route.recheck.started",
                      "account_alias": "account-1", "provider": "cline",
                      "model": "m1", "tier": "free"})
        store.ingest({"event_type": "route.recheck.blocked",
                      "occurred_at": 1_700_000_000_002, "source_component": "cmo-worker",
                      "reason_code": "recheck.no_probe_executor",
                      "account_alias": "account-1", "provider": "cline",
                      "model": "m1", "tier": "free"})
        history = store.recheck_history(limit=10)
        assert history[0]["status"] == "finished:blocked"
        assert history[0]["last_error"] == "recheck.no_probe_executor"
    finally:
        store.close()
