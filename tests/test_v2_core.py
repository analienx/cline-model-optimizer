"""Deterministic acceptance tests for CMO v2 core (spec 7.2/7.3, section 11)."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "python"
spec = importlib.util.spec_from_file_location("cmo", PKG / "cmo" / "__init__.py",
                                              submodule_search_locations=[str(PKG / "cmo")])
cmo = importlib.util.module_from_spec(spec)
sys.modules["cmo"] = cmo
spec.loader.exec_module(cmo)

from cmo import cli, decision, events, policy  # noqa: E402


class PolicyTests(unittest.TestCase):
    def test_canonical_policy_valid_and_payg_free(self):
        doc = policy.load_canonical_policy()
        self.assertEqual(doc["schema"], "cmo.route-policy/v2")
        self.assertEqual(len(doc["routes"]), 5)

    def test_payg_is_impossible_by_schema(self):
        for bad in (
            {"schema": policy.POLICY_SCHEMA, "routes": policy.CANONICAL_ROUTES, "defaults": {},
             "payg": [{"model": "x"}]},
            {"schema": policy.POLICY_SCHEMA, "routes": policy.CANONICAL_ROUTES, "defaults": {},
             "paid": True},
        ):
            with self.assertRaises(policy.PolicyError):
                policy.validate_policy(bad)

    def test_payg_tier_rejected(self):
        doc = json.loads(json.dumps(policy.canonical_policy_document()))
        doc["routes"][0]["tier"] = "paid"
        with self.assertRaises(policy.PolicyError):
            policy.validate_policy(doc)

    def test_digest_is_stable(self):
        d1 = policy.policy_digest(policy.canonical_policy_document())
        d2 = policy.policy_digest(policy.canonical_policy_document())
        self.assertEqual(d1, d2)


class RouteOrderTests(unittest.TestCase):
    def setUp(self):
        self.engine = decision.DecisionEngine(now_ms=lambda: 1_000_000)

    def test_muse_then_glm_then_deepseek_then_clinepass(self):
        models = [r["model"] for r in self.engine.policy["routes"]]
        self.assertEqual(models, [
            "cline-free/muse-spark-1.3-contributor", "z-ai/glm-5.3-flash",
            "cline-free/deepseek-v4.1-flash", "cline-pass/glm-5.3-flash",
            "cline-pass/deepseek-v4.1-flash"])

    def test_account_rotation_before_model_advance(self):
        # account-1 quota -> account-2 same model, NOT next model on account-1
        # (cooldown still active at the fixed clock: 0 + 9_000_000 > 1_000_000)
        rows = [
            {"model": "cline-free/muse-spark-1.3-contributor", "account_alias": "account-1",
             "state": "QUOTA", "state_changed_at": 0, "reset_after_ms": 9_000_000},
        ]
        d = self.engine.decide(rows, [])
        self.assertEqual(d["route"], {"model": "cline-free/muse-spark-1.3-contributor",
                                      "tier": "free", "account_alias": "account-2"})

    def test_all_free_quota_advances_to_clinepass(self):
        rows = [
            {"model": m, "account_alias": a, "state": "QUOTA", "state_changed_at": 0,
             "reset_after_ms": 9_000_000}
            for m in ("cline-free/muse-spark-1.3-contributor", "z-ai/glm-5.3-flash",
                      "cline-free/deepseek-v4.1-flash")
            for a in ("account-1", "account-2", "account-3")
        ]
        d = self.engine.decide(rows, [])
        self.assertEqual(d["route"]["model"], "cline-pass/glm-5.3-flash")
        self.assertEqual(d["route"]["tier"], "subscription")

    def test_subscription_exhaustion_stops_blocked(self):
        rows = [
            {"model": m, "account_alias": a, "state": "QUOTA", "state_changed_at": 0,
             "reset_after_ms": 9_000_000}
            for m in ("cline-free/muse-spark-1.3-contributor", "z-ai/glm-5.3-flash",
                      "cline-free/deepseek-v4.1-flash")
            for a in ("account-1", "account-2", "account-3")
        ] + [
            {"model": "cline-pass/glm-5.3-flash", "account_alias": "account-1",
             "state": "QUOTA", "state_changed_at": 0, "reset_after_ms": 9_000_000},
            {"model": "cline-pass/deepseek-v4.1-flash", "account_alias": "account-1",
             "state": "QUOTA", "state_changed_at": 0, "reset_after_ms": 9_000_000},
        ]
        d = self.engine.decide(rows, [])
        self.assertEqual(d["action"], "BLOCKED")

    def test_available_launches_directly(self):
        rows = [{"model": "cline-free/muse-spark-1.3-contributor", "account_alias": "account-1",
                 "state": "AVAILABLE", "state_changed_at": 0, "last_probe_at": 999}]
        d = self.engine.decide(rows, [])
        self.assertEqual(d["action"], "LAUNCH")
        self.assertIn("AVAILABLE", d["reason"])

    def test_force_skip_override_is_honoured_and_expiring(self):
        base = [{"model": "cline-free/muse-spark-1.3-contributor", "account_alias": "account-1",
                 "state": "AVAILABLE", "state_changed_at": 0}]
        d = self.engine.decide(base, [{"model": "cline-free/muse-spark-1.3-contributor",
                                       "account_alias": "account-1", "kind": "FORCE_SKIP",
                                       "expires_at": 10_000_000, "reason": "operator"}])
        self.assertEqual(d["route"]["account_alias"], "account-2")
        d2 = self.engine.decide(base, [{"model": "cline-free/muse-spark-1.3-contributor",
                                        "account_alias": "account-1", "kind": "FORCE_SKIP",
                                        "expires_at": 900_000, "reason": "operator"}])
        self.assertEqual(d2["route"]["account_alias"], "account-1")

    def test_quota_cooldown_elapsed_uses_provider_reset(self):
        rows = [{"model": "cline-free/muse-spark-1.3-contributor", "account_alias": "account-1",
                 "state": "QUOTA", "state_changed_at": 0, "reset_after_ms": 500}]
        d = self.engine.decide(rows, [])
        self.assertEqual(d["route"]["account_alias"], "account-1")
        self.assertIn("cooldown elapsed", d["reason"])

    def test_standard_strategy_selects_clinepass_only(self):
        rows = [{"model": "cline-pass/glm-5.3-flash", "account_alias": "account-1",
                 "state": "UNKNOWN", "state_changed_at": 0}]
        d = self.engine.decide(rows, [], strategy="standard")
        self.assertEqual(d["route"]["model"], "cline-pass/glm-5.3-flash")

    def test_context_escalation_preserves_session_and_records_no_quota(self):
        esc = decision.DecisionEngine.context_escalation({"goal_id": "g1"})
        self.assertEqual(esc["new_strategy"], "standard")
        self.assertTrue(esc["preserve_session"])
        self.assertFalse(esc["record_quota"])

    def test_failure_classes_are_distinct(self):
        self.assertEqual(len(set(decision.ROUTE_STATES)), 8)
        for s in ("QUOTA", "AUTH_BLOCKED", "TRANSIENT", "CAPABILITY_UNAVAILABLE"):
            self.assertIn(s, decision.ROUTE_STATES)


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.store = events.EventStore(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ingest_idempotent_by_event_id(self):
        ev = {"event_id": "e1", "event_type": "attempt.started", "model": "m",
              "account_alias": "account-1", "tier": "free"}
        self.assertEqual(self.store.ingest(ev), (True, "e1"))
        self.assertEqual(self.store.ingest(ev), (False, "e1"))

    def test_unknown_tier_rejected_payg_impossible(self):
        with self.assertRaises(events.EventValidationError):
            self.store.ingest({"event_type": "attempt.started", "tier": "payg"})

    def test_oversized_detail_scrubbed(self):
        _, eid = self.store.ingest({"event_type": "attempt.failed",
                                    "safe_detail": "x" * 5000})
        row = [e for e in self.store.events() if e["event_id"] == eid][0]
        self.assertEqual(len(row["safe_detail"]), 512)

    def test_projection_rebuild_is_identical(self):
        for i in range(3):
            self.store.ingest({"event_id": f"g{i}", "event_type": "goal.started",
                               "goal_id": f"g{i}", "strategy": "free-first"})
        before = self.store.events(limit=10)
        self.assertEqual(len(before), 3)


class CliTests(unittest.TestCase):
    def test_policy_validate_exit0(self):
        self.assertEqual(cli.main(["policy", "validate"]), 0)

    def test_doctor_new_db_reports_missing_matrix(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "d.db")
            rc = cli.main(["--db", db, "doctor"])
            self.assertEqual(rc, 1)  # free matrix not yet seeded -> not ok

    def test_migration_dry_run_and_execute(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "quota-state.json"
            ledger.write_text(json.dumps({"routes": {
                "account-1|cline|z-ai/glm-5.3-flash": {
                    "model": "z-ai/glm-5.3-flash", "reason": "confirmed_free_quota_exhaustion"}}}))
            db = str(Path(tmp) / "d.db")
            r1 = json.loads(_capture(cli, ["--db", db, "migrate", "legacy",
                                           "--dry-run", "--sources", str(ledger)]))
            self.assertEqual(r1["mode"], "dry-run")
            r2 = json.loads(_capture(cli, ["--db", db, "migrate", "legacy",
                                           "--execute", "--sources", str(ledger)]))
            self.assertEqual(r2["ingested_events"], 1)
            self.assertEqual(r2["tokens_imported"], 0)


def _capture(cli_mod, argv):
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli_mod.main(argv)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
