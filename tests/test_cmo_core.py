"""Core CMO v2 behaviour: identity, ingestion, projection, decision, freshness.

These tests are the local proof for gates A-F: real evidence must move the exact
route projection, unrelated evidence must not, unknown evidence must stay
honestly unknown, and catalog/auth/quota are separate signals.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import StoreCase, scan_for_secrets  # noqa: E402

from cmo.decision import DecisionEngine, context_escalation  # noqa: E402
from cmo.events import (EventConflictError, EventValidationError,  # noqa: E402
                        classify_reason, normalize_event, rebuild, scrub_detail)
from cmo.policy import (TIER_FREE, TIER_SUBSCRIPTION, load_canonical_policy,  # noqa: E402
                        parse_route_key, policy_digest, route_key)
from cmo.snapshot import build_snapshot  # noqa: E402

MUSE = "cline-free/muse-spark-1.3-contributor"
FLASH = "cline-free/deepseek-v4.1-flash"
SUBSCRIPTION = "cline-pass/glm-5.3-flash"


class PolicyInvariantTests(unittest.TestCase):
    def test_never_payg_and_tier_allowlist(self) -> None:
        policy = load_canonical_policy()
        self.assertIs(policy["neverPayg"], True)
        # Internal canonical document; the exported compatibility schema is
        # foundry-route-policy/v1 (asserted in the compat export tests).
        self.assertEqual(policy["schema"], "cmo.route-policy/v2")
        self.assertEqual(policy["policyVersion"], "2.0.0")
        self.assertEqual(policy["owner"], "cline-model-optimizer")
        tiers = {str(route["tier"]).lower() for route in policy["routes"]}
        self.assertEqual(tiers, {TIER_FREE, TIER_SUBSCRIPTION})
        self.assertNotIn("paid", tiers)
        for strategy in policy["strategies"].values():
            self.assertTrue(set(strategy.get("tiers", [])) <= {TIER_FREE, TIER_SUBSCRIPTION})

    def test_policy_digest_is_stable(self) -> None:
        policy = load_canonical_policy()
        self.assertEqual(policy_digest(policy), policy_digest(load_canonical_policy()))
        self.assertEqual(len(policy_digest(policy)), 64)

    def test_route_key_roundtrip_and_rejects_payg(self) -> None:
        key = route_key("Account-1", "cline", "Cline-Free/Muse-Spark-1.3-Contributor",
                        "Free")
        self.assertEqual(key, "account-1|cline|cline-free/muse-spark-1.3-contributor|free")
        self.assertEqual(parse_route_key(key)["tier"], TIER_FREE)
        with self.assertRaises(EventValidationError):
            normalize_event({"event_type": "route.probe.succeeded", "occurred_at": 1,
                             "account_alias": "account-1", "provider": "cline",
                             "model": MUSE, "tier": "paid"})


class IngestionTests(StoreCase):
    def test_unknown_event_type_and_shapes_are_rejected(self) -> None:
        with self.assertRaises(EventValidationError):
            self.store.ingest({"event_type": "route.explode"})
        with self.assertRaises(EventValidationError):
            self.store.ingest("not-an-object")  # type: ignore[arg-type]
        with self.assertRaises(EventValidationError):
            self.store.ingest(self.event("route.probe.failed", account_alias="account-1",
                                         provider="cline", tier="free"))
        with self.assertRaises(EventValidationError):
            self.store.ingest(self.probe_failed("account-1", MUSE, reset_after_ms=-5))

    def test_ingest_is_idempotent_and_conflicts_are_refused(self) -> None:
        event = self.probe_ok("account-1", MUSE, event_id="fixed-1")
        first = self.store.ingest(event)
        revision = self.store.revision()
        second = self.store.ingest(dict(event))
        self.assertTrue(first["inserted"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["inserted"])
        self.assertEqual(self.store.revision(), revision)
        conflicting = dict(event, reason_code="capability.not_offered")
        with self.assertRaises(EventConflictError):
            self.store.ingest(conflicting)
        self.assertEqual(self.store.revision(), revision)

    def test_generated_event_ids_are_unique_within_one_process(self) -> None:
        ids = {self.store.ingest(self.probe_ok("account-1", MUSE))["event_id"]
               for _ in range(40)}
        self.assertEqual(len(ids), 40)

    def test_secrets_are_scrubbed_from_events_and_reads(self) -> None:
        detail = " ".join(scan_for_secrets.__doc__ or "" for _ in range(0))
        payload = ("auth failed " + " ".join([
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
            "token=gho_abcdefghijklmnopqrstuvwxyz0123456789",
            "api_key=sk-live-abcdefghijklmnopqrstuvwxyz",
            "authorization=Bearer abcdefghijklmnopqrstuvwxyz0123",
            "user@example.com"]))
        self.store.ingest(self.probe_failed("account-1", MUSE, reason="auth.required",
                                            safe_detail=payload))
        stored = self.store.events(limit=10)[0]
        snapshot = build_snapshot(self.store)
        surfaces = [stored.get("safe_detail") or "", self.json_text(snapshot)]
        events = self.store.__class__.__name__
        for name in (self.db_path,):
            surfaces.append(Path(name).read_bytes().decode("utf-8", "ignore"))
        for text in surfaces:
            detail_text = scrub_detail(payload) or ""
            for sample in ("gho_abcdefghijklmnopqrstuvwxyz", "sk-live-abcdefghijklmnopqrstuvwxyz",
                           "Bearer abcdefghijklmnopqrstuvwxyz", "user@example.com"):
                self.assertNotIn(sample, text, f"{sample!r} leaked into {events}")
        self.assertIn("[redacted", detail_text)
        self.assertIn("***", detail_text)


class ProjectionTests(StoreCase):
    def test_probe_success_moves_only_the_exact_route(self) -> None:
        routes = [("account-1", MUSE), ("account-1", FLASH), ("account-2", MUSE)]
        for account, model in routes:
            self.store.ingest(self.probe_ok(account, model))
        self.store.ingest(self.probe_failed("account-3", MUSE, reset_after_ms=900000,
                                            reset_known=1))
        snapshot = build_snapshot(self.store)
        self.assertEqual(self.cell(snapshot, route_key("account-1", "cline", MUSE, "free"))["state"],
                         "AVAILABLE")
        self.assertEqual(self.cell(snapshot, route_key("account-1", "cline", FLASH, "free"))["state"],
                         "AVAILABLE")
        self.assertEqual(self.cell(snapshot, route_key("account-2", "cline", MUSE, "free"))["state"],
                         "AVAILABLE")
        quota = self.cell(snapshot, route_key("account-3", "cline", MUSE, "free"))
        self.assertEqual(quota["state"], "QUOTA")
        self.assertEqual(quota["reset_after_ms"], 900000)
        self.assertEqual(quota["last_reason_code"], "quota.confirmed")

    def test_late_evidence_does_not_overwrite_newer_state(self) -> None:
        old = self.probe_failed("account-1", MUSE, occurred_at=self.now - 600_000,
                                reset_after_ms=900000, reset_known=1)
        self.store.ingest(self.probe_ok("account-1", MUSE, occurred_at=self.now))
        self.store.ingest(old)
        snapshot = build_snapshot(self.store)
        cell = self.cell(snapshot, route_key("account-1", "cline", MUSE, "free"))
        self.assertEqual(cell["state"], "AVAILABLE")
        self.assertLessEqual(cell["age_ms"], 50)

    def test_available_evidence_expires_into_honest_stale(self) -> None:
        self.store.ingest(self.probe_ok("account-1", MUSE))
        ttl = load_canonical_policy()["defaults"]["evidence_ttl_ms"]
        later = build_snapshot(self.store)["generated_at"] + int(ttl) + 1
        snapshot = build_snapshot(self.store, now=later)
        cell = self.cell(snapshot, route_key("account-1", "cline", MUSE, "free"))
        self.assertEqual(cell["stored_state"], "AVAILABLE")
        self.assertEqual(cell["state"], "STALE")
        self.assertEqual(cell["freshness"], "stale")
        self.assertEqual(cell["validity"], "expired")

    def test_quota_reset_expiry_becomes_checking_not_available(self) -> None:
        reset_after = 60_000
        self.store.ingest(self.probe_failed("account-1", MUSE, reset_after_ms=reset_after,
                                            reset_known=1))
        fresh = build_snapshot(self.store)
        self.assertEqual(self.cell(fresh, route_key("account-1", "cline", MUSE, "free"))["state"],
                         "QUOTA")
        later = fresh["generated_at"] + reset_after + 1000
        expired = build_snapshot(self.store, now=later)
        cell = self.cell(expired, route_key("account-1", "cline", MUSE, "free"))
        self.assertEqual(cell["state"], "QUOTA_EXPIRED")
        self.assertEqual(cell["state_label"], "Checking")

    def test_unknown_reset_keeps_bounded_next_check(self) -> None:
        self.store.ingest(self.probe_failed("account-1", MUSE, reason="quota.reset_unknown",
                                            reset_known=0))
        snapshot = build_snapshot(self.store)
        cell = self.cell(snapshot, route_key("account-1", "cline", MUSE, "free"))
        self.assertEqual(cell["state"], "QUOTA")
        self.assertEqual(cell["reset_known"], 0)
        self.assertIsNotNone(cell["next_check_at"])
        self.assertGreater(cell["next_check_at"], snapshot["generated_at"])

    def test_attempt_liveness_marks_in_use_and_disconnect(self) -> None:
        self.store.ingest(self.event("goal.started", goal_id="goal-a", work_state="running"))
        self.store.ingest(self.event("attempt.started", goal_id="goal-a", attempt_id="att-1",
                                     account_alias="account-1", provider="cline", model=MUSE,
                                     tier="free"))
        snapshot = build_snapshot(self.store)
        cell = self.cell(snapshot, route_key("account-1", "cline", MUSE, "free"))
        self.assertTrue(cell["in_use"])
        self.assertEqual(cell["in_use_liveness"], "live")
        self.assertEqual(cell["in_use_evidence"], "att-1")
        later = snapshot["generated_at"] + 10 * 60 * 1000
        stale = build_snapshot(self.store, now=later)
        self.assertEqual(self.cell(stale, route_key("account-1", "cline", MUSE, "free"))["in_use_liveness"],
                         "disconnected")
        self.assertIn(stale["service"]["status"], ("degraded", "connected"))

    def test_rebuild_replays_event_log_deterministically(self) -> None:
        events = [self.probe_failed("account-1", MUSE, reset_after_ms=300000, reset_known=1),
                  self.probe_ok("account-2", FLASH),
                  self.event("goal.started", goal_id="goal-a", work_state="running"),
                  self.event("attempt.started", goal_id="goal-a", attempt_id="att-9",
                             account_alias="account-2", provider="cline", model=FLASH,
                             tier="free"),
                  self.event("override.created", account_alias="account-3", provider="cline",
                             model=MUSE, tier="free", reason_code="manual.override",
                             safe_detail="maintenance window")]
        revision = 0
        for event in events:
            revision = self.store.ingest(event)["revision"]
        before = build_snapshot(self.store)
        rebuild(self.store._conn)
        after = build_snapshot(self.store)
        self.assertEqual(self.store.revision(), revision)
        self.assertEqual(before["state_revision"], after["state_revision"])
        self.assertEqual([c["state"] for c in before["cells"]],
                         [c["state"] for c in after["cells"]])
        self.assertEqual([c["route_key"] for c in before["cells"]],
                         [c["route_key"] for c in after["cells"]])
        self.assertEqual(before["decision"]["action"], after["decision"]["action"])


class DecisionTests(StoreCase):
    def _engine(self, snapshot_now: int) -> DecisionEngine:
        return DecisionEngine(load_canonical_policy(), snapshot_now)

    def test_free_first_launches_a_verified_free_leg(self) -> None:
        # Canonical order is model-major: the first free rung must be probed
        # before later rungs are considered, so a verified first rung launches.
        self.store.ingest(self.probe_ok("account-1", MUSE))
        decision = build_snapshot(self.store)["decision"]
        self.assertEqual(decision["action"], "LAUNCH")
        self.assertEqual(decision["route"]["tier"], TIER_FREE)
        self.assertEqual(decision["route"]["model"], MUSE)
        self.assertEqual(decision["route"]["account_alias"], "account-1")
        self.assertEqual(decision["reason_code"], "verified.launch")
        self.assertEqual(decision["evidence"]["state"], "AVAILABLE")

    def test_unknown_rung_is_probed_before_later_verified_rungs(self) -> None:
        """The ladder is ordered: an unknown earlier rung is probed first."""
        self.store.ingest(self.probe_ok("account-2", FLASH))
        decision = build_snapshot(self.store)["decision"]
        self.assertEqual(decision["action"], "PROBE")
        self.assertEqual(decision["route"]["model"], MUSE)
        self.assertEqual(decision["route"]["account_alias"], "account-1")

    def test_no_evidence_yields_probe_for_one_leg_only(self) -> None:
        decision = build_snapshot(self.store)["decision"]
        self.assertEqual(decision["action"], "PROBE")
        self.assertIsNotNone(decision["route"]["route_key"])
        self.assertEqual(decision["route"]["model"], MUSE)
        self.assertEqual(decision["route"]["account_alias"], "account-1")
        self.assertEqual(decision["route"]["tier"], TIER_FREE)

    def _exhaust(self, *, tiers: tuple[str, ...] = (TIER_FREE, TIER_SUBSCRIPTION)) -> None:
        for route in load_canonical_policy()["routes"]:
            if route["tier"] not in tiers:
                continue
            for account in route["accounts"]:
                self.store.ingest(self.probe_failed(
                    account, route["model"], reset_after_ms=3_600_000, reset_known=1,
                    provider=route["provider"], tier=route["tier"]))

    def test_all_exhausted_blocks_without_payg(self) -> None:
        self._exhaust()
        snapshot = build_snapshot(self.store)
        decision = snapshot["decision"]
        self.assertEqual(decision["action"], "BLOCKED")
        self.assertIsNone(decision["route"])
        self.assertEqual(decision["reason_code"], "routes.exhausted")
        self.assertTrue(decision["skipped"])
        self.assertTrue(all(
            item["reason_code"] in ("quota.confirmed", "manual.override")
            for item in decision["skipped"]))
        self.assertTrue(snapshot["policy"]["neverPayg"])

    def test_free_only_never_selects_subscription(self) -> None:
        self._exhaust(tiers=(TIER_FREE,))
        self.store.ingest(self.probe_ok("account-1", SUBSCRIPTION, provider="cline-pass",
                                        tier=TIER_SUBSCRIPTION))
        self.store.ingest(self.event("goal.started", goal_id="goal-free",
                                     work_state="running", strategy="free-only",
                                     free_only=True))
        snapshot = build_snapshot(self.store, goal_id="goal-free")
        self.assertEqual(snapshot["decision"]["action"], "BLOCKED")
        self.assertIsNone(snapshot["decision"]["route"])
        self.assertTrue(snapshot["decision"]["free_only"])
        self.assertTrue(snapshot["decision"]["subscription_blocked_by_free_only"])
        # The verified subscription leg is genuinely fresh, but FreeOnly wins.
        sub = self.cell(snapshot, route_key("account-1", "cline-pass", SUBSCRIPTION,
                                            "subscription"))
        self.assertEqual(sub["state"], "AVAILABLE")

    def test_subscription_fallback_used_when_free_is_exhausted(self) -> None:
        self._exhaust(tiers=(TIER_FREE,))
        self.store.ingest(self.probe_ok("account-1", SUBSCRIPTION, provider="cline-pass",
                                        tier=TIER_SUBSCRIPTION))
        snapshot = build_snapshot(self.store)
        self.assertEqual(snapshot["decision"]["action"], "LAUNCH")
        self.assertEqual(snapshot["decision"]["route"]["tier"], TIER_SUBSCRIPTION)
        self.assertEqual(snapshot["decision"]["route"]["model"], SUBSCRIPTION)
        self.assertEqual(snapshot["decision"]["route"]["provider"], "cline-pass")

    def test_auth_and_quota_are_separate_signals(self) -> None:
        self.store.ingest(self.probe_failed("account-1", MUSE, reason="auth.required"))
        self.store.ingest(self.probe_failed("account-2", MUSE, reason="quota.confirmed",
                                            reset_after_ms=600000, reset_known=1))
        snapshot = build_snapshot(self.store)
        auth = self.cell(snapshot, route_key("account-1", "cline", MUSE, "free"))
        quota = self.cell(snapshot, route_key("account-2", "cline", MUSE, "free"))
        self.assertEqual(auth["state"], "AUTH_BLOCKED")
        self.assertEqual(quota["state"], "QUOTA")
        self.assertEqual(auth["last_reason_code"], "auth.required")
        self.assertEqual(quota["last_reason_code"], "quota.confirmed")
        self.assertIsNone(auth["reset_after_ms"])
        self.assertEqual(classify_reason("auth.required"), "auth")
        self.assertEqual(classify_reason("quota.confirmed"), "quota")

    def test_transient_and_capability_are_distinct(self) -> None:
        self.store.ingest(self.probe_failed("account-1", MUSE, reason="transient.failure"))
        self.store.ingest(self.probe_failed("account-2", MUSE,
                                            reason="capability.not_offered"))
        snapshot = build_snapshot(self.store)
        self.assertEqual(self.cell(snapshot, route_key("account-1", "cline", MUSE, "free"))["state"],
                         "TRANSIENT")
        self.assertEqual(self.cell(snapshot, route_key("account-2", "cline", MUSE, "free"))["state"],
                         "CAPABILITY_UNAVAILABLE")

    def test_catalog_capability_never_fabricates_availability(self) -> None:
        self.store.ingest(self.event(
            "catalog.refreshed", source_component="cmo",
            safe_detail='{"models": [{"model": "%s", "capability": "available"}, '
                        '{"model": "%s", "capability": "unavailable"}]}' % (MUSE, FLASH)))
        snapshot = build_snapshot(self.store)
        self.assertEqual(snapshot["freshness"]["catalog_status"], "fresh")
        self.assertEqual(self.cell(snapshot, route_key("account-1", "cline", MUSE, "free"))["state"],
                         "UNKNOWN")
        self.assertEqual(self.cell(snapshot, route_key("account-1", "cline", FLASH, "free"))["state"],
                         "CAPABILITY_UNAVAILABLE")

    def test_catalog_failure_is_recorded_and_keeps_last_good(self) -> None:
        self.store.ingest(self.event(
            "catalog.refreshed", source_component="cmo",
            safe_detail='{"models": [{"model": "%s", "capability": "available"}]}' % MUSE))
        self.store.ingest(self.event("catalog.failed", source_component="cmo",
                                     reason_code="transient.failure",
                                     safe_detail="HTTP 503 from catalog"))
        snapshot = build_snapshot(self.store)
        self.assertEqual(snapshot["freshness"]["catalog_status"], "error")
        self.assertIsNotNone(snapshot["freshness"]["catalog_last_error_at"])
        self.assertEqual(snapshot["service"]["status"], "degraded")
        self.assertTrue(any("catalog" in issue for issue in snapshot["service"]["issues"]))
        # last-good capability survives, and is still only capability evidence.
        self.assertEqual(snapshot["catalog"][0]["capability"], "available")

    def test_override_forces_skip_then_clears(self) -> None:
        self._exhaust(tiers=(TIER_FREE,))
        self.store.ingest(self.probe_ok("account-2", FLASH))
        self.store.ingest(self.event("goal.started", goal_id="goal-free",
                                     work_state="running", free_only=True))
        before = build_snapshot(self.store, goal_id="goal-free")["decision"]
        self.assertEqual(before["action"], "LAUNCH")
        self.assertEqual(before["route"]["route_key"],
                         route_key("account-2", "cline", FLASH, "free"))
        self.store.ingest(self.event("override.created", account_alias="account-2",
                                     provider="cline", model=FLASH, tier="free",
                                     reason_code="manual.override",
                                     safe_detail="operator maintenance"))
        snapshot = build_snapshot(self.store, goal_id="goal-free")
        cell = self.cell(snapshot, route_key("account-2", "cline", FLASH, "free"))
        self.assertEqual(cell["state"], "FORCE_SKIP")
        self.assertEqual(cell["state_label"], "Manual override")
        self.assertTrue(cell["manual_override"])
        self.assertEqual(cell["manual_override_reason"], "operator maintenance")
        self.assertEqual(snapshot["decision"]["action"], "BLOCKED")
        skipped = {item["route_key"]: item for item in snapshot["decision"]["skipped"]}
        self.assertEqual(skipped[route_key("account-2", "cline", FLASH, "free")]["reason_code"],
                         "manual.override")
        self.assertTrue(snapshot["overrides"])
        override_id = snapshot["overrides"][0]["override_id"]
        self.store.ingest(self.event("override.cleared", source_component="cmo",
                                     reason_code="manual.override",
                                     safe_detail=override_id))
        cleared = build_snapshot(self.store, goal_id="goal-free")
        self.assertEqual(self.cell(cleared, route_key("account-2", "cline", FLASH, "free"))["state"],
                         "AVAILABLE")
        self.assertEqual(cleared["decision"]["action"], "LAUNCH")

    def test_recheck_requests_are_recorded_as_pending(self) -> None:
        self.store.ingest(self.event("route.recheck.requested", account_alias="account-1",
                                     provider="cline", model=MUSE, tier="free",
                                     reason_code="manual.override",
                                     safe_detail="operator asked for a bounded probe"))
        snapshot = build_snapshot(self.store)
        self.assertEqual(len(snapshot["recheck_requests"]), 1)
        self.assertEqual(snapshot["recheck_requests"][0]["status"], "pending")


class MultiGoalTests(StoreCase):
    def test_two_goals_are_tracked_independently(self) -> None:
        self.store.ingest(self.event("goal.started", goal_id="goal-a", repo="repo-a",
                                     strategy="free-first", work_state="running"))
        self.store.ingest(self.event("goal.started", goal_id="goal-b", repo="repo-b",
                                     strategy="free-only", work_state="running",
                                     free_only=True))
        snapshot = build_snapshot(self.store)
        goal_ids = {goal["goal_id"] for goal in snapshot["goals"]}
        self.assertEqual(goal_ids, {"goal-a", "goal-b"})
        selected = build_snapshot(self.store, goal_id="goal-b")
        self.assertEqual(selected["selected_goal_id"], "goal-b")
        self.assertTrue(selected["goals"])
        # FreeOnly is a modifier, not a strategy: the effective strategy stays
        # the canonical free-first ladder while free_only constrains the tail.
        self.assertEqual(selected["decision"]["strategy"], "free-first")
        self.assertTrue(selected["decision"]["free_only"])
        other = build_snapshot(self.store, goal_id="goal-a")
        self.assertFalse(other["decision"]["free_only"])
        self.store.ingest(self.event("goal.failed", goal_id="goal-a",
                                     reason_code="transient.failure",
                                     safe_detail="provider outage"))
        after = build_snapshot(self.store)
        by_id = {goal["goal_id"]: goal for goal in after["goals"]}
        self.assertEqual(by_id["goal-a"]["status"], "failed")
        self.assertEqual(by_id["goal-b"]["status"], "active")

    def test_context_escalation_is_reported_per_goal(self) -> None:
        self.store.ingest(self.event("goal.started", goal_id="goal-a", work_state="running"))
        self.store.ingest(self.event("goal.started", goal_id="goal-b", work_state="running",
                                     free_only=True))
        self.store.ingest(self.event("goal.context_escalated", goal_id="goal-a",
                                     reason_code="context.exhausted",
                                     safe_detail="context window 92% used"))
        self.store.ingest(self.event("goal.context_escalated", goal_id="goal-b",
                                     reason_code="context.exhausted",
                                     safe_detail="context window 92% used"))
        snapshot = build_snapshot(self.store)
        by_id = {goal["goal_id"]: goal for goal in snapshot["goals"]}
        self.assertEqual(by_id["goal-a"]["effective_strategy"], "standard")
        self.assertEqual(by_id["goal-a"]["reason_code"], "context.escalated_to_standard")
        # A FreeOnly Goal stays free-only after escalation.
        self.assertEqual(by_id["goal-b"]["effective_strategy"], "free-first")
        self.assertEqual(by_id["goal-b"]["reason_code"],
                         "context.escalation_denied_free_only")
        decision = context_escalation(by_id["goal-b"])
        self.assertEqual(decision["action"], "ESCALATE_DENIED")
        self.assertTrue(decision["preserve_session"])


if __name__ == "__main__":
    unittest.main()
