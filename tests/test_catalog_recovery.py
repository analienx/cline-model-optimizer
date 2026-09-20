"""Catalog regression: real provider-sized responses must survive ingestion."""
from __future__ import annotations

import unittest
from unittest.mock import patch
from support import StoreCase
from cmo.catalog import refresh_catalog
from cmo.snapshot import build_snapshot


class CatalogRecoveryTests(StoreCase):
    def test_many_models_are_published_and_replayable(self):
        ids = [f"cline-free/fixture-model-{i:03d}" for i in range(40)]
        with patch("cmo.catalog.fetch_catalog", return_value={
            "models": {model: "available" for model in ids},
            "source_url": "https://catalog.example.test/",
        }):
            result = refresh_catalog(self.store)
        self.assertTrue(result["ok"])
        snapshot = build_snapshot(self.store)
        self.assertEqual(len(snapshot["catalog"]), 40)
        self.assertEqual(snapshot["freshness"]["catalog_status"], "fresh")
        self.assertEqual({r["model"] for r in snapshot["catalog"]}, set(ids))
        from cmo.events import rebuild
        rebuild(self.store._conn)
        self.assertEqual({r["model"] for r in self.store.catalog()}, set(ids))

    def test_truncated_legacy_catalog_must_not_be_fresh(self):
        from cmo.events import EventValidationError
        with self.assertRaises(EventValidationError):
            self.store.ingest({"event_type": "catalog.refreshed", "source_component": "cmo",
                               "safe_detail": '{"models": [{"model": "broken"}]' + 'x' * 600})
        self.assertEqual(len(self.store.catalog()), 0)

    def test_partial_catalog_batch_rolls_back_on_conflict(self):
        from cmo.events import EventConflictError
        first = {"event_id": "catalog-conflict", "event_type": "catalog.refreshed",
                 "model": "cline-free/example-one", "capability": "available",
                 "source_component": "cmo"}
        second = {**first, "capability": "unavailable"}
        with self.assertRaises(EventConflictError):
            self.store.ingest_many([first, second])
        self.assertEqual(self.store.catalog(), [])
        self.assertEqual(self.store.events(), [])
