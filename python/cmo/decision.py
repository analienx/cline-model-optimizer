"""CMO decision engine: canonical free-first route, never PAYG.

Returns the chosen route plus the ordered list of skipped candidates with
machine-readable reason codes so the UI can explain *why* a route (including
the ClinePass subscription tail) was selected at selection time.
"""

from __future__ import annotations

from typing import Any

from .policy import (REASON_AUTH, REASON_CAPABILITY, REASON_OVERRIDE,
                     REASON_QUOTA_CONFIRMED, REASON_QUOTA_UNKNOWN, REASON_TRANSIENT,
                     TIER_FREE, TIER_SUBSCRIPTION, load_canonical_policy,
                     routes_for_strategy, STRATEGY_FREE_FIRST, STRATEGY_STANDARD)

ROUTE_STATES = (
    "UNKNOWN", "PROBING", "AVAILABLE", "QUOTA", "AUTH_BLOCKED", "TRANSIENT",
    "CAPABILITY_UNAVAILABLE", "STALE",
)

CATEGORY_BY_STATE = {
    "AVAILABLE": "verified",
    "QUOTA": "quota",
    "AUTH_BLOCKED": "auth",
    "TRANSIENT": "transient",
    "CAPABILITY_UNAVAILABLE": "capability",
    "PROBING": "probing",
    "UNKNOWN": "unknown",
    "STALE": "stale",
}


class DecisionEngine:
    """Deterministic canonical-order decision engine over route_state rows."""

    def __init__(self, policy: dict[str, Any] | None = None,
                 now_ms: int | None = None):
        self.policy = policy or load_canonical_policy()
        self._now = now_ms

    def now(self) -> int:
        if self._now is not None:
            return int(self._now)
        import time
        return int(time.time() * 1000)

    # -- helpers --------------------------------------------------------

    def _effective_state(self, row: dict[str, Any], now: int) -> str:
        """Fold time into a stored state: AVAILABLE ages to STALE."""
        state = (row.get("state") or "UNKNOWN").upper()
        if state == "AVAILABLE":
            ttl = int(self.policy["defaults"]["success_ttl_ms"])
            observed = row.get("observed_at")
            if observed is None or now - int(observed) > ttl:
                return "STALE"
        return state

    def decide(self, route_states: list[dict[str, Any]], overrides: list[dict[str, Any]],
               strategy: str = STRATEGY_FREE_FIRST, goal_id: str | None = None,
               free_only: bool = False,
               catalog: dict[str, str] | None = None,
               catalog_fresh: bool = True) -> dict[str, Any]:
        now = self.now()
        catalog = catalog or {}
        active_skips = {
            (o.get("route_key") or f"{o.get('account_alias')}|{o.get('provider')}|{o.get('model')}")
            for o in overrides
            if o.get("kind") == "FORCE_SKIP"
            and (not o.get("expires_at") or int(o["expires_at"]) > now)
        }
        state_by_key = {(r.get("route_key")): r for r in (route_states or [])}

        effective_strategy = strategy
        if strategy == STRATEGY_STANDARD and free_only:
            return self._blocked("standard strategy is withheld by FreeOnly",
                                 "free_only.blocks_subscription", strategy, free_only,
                                 goal_id, now, [])

        routes = routes_for_strategy(effective_strategy, self.policy)
        if free_only:
            routes = [r for r in routes if r["tier"] == TIER_FREE]

        skipped: list[dict[str, Any]] = []
        for route in routes:
            for account in route["accounts"]:
                rk = self._route_key(account, route)
                row = state_by_key.get(rk)
                if rk in active_skips:
                    skipped.append(self._skip(rk, route, account, "FORCE_SKIP",
                                              REASON_OVERRIDE,
                                              "active FORCE_SKIP override with reason"))
                    continue
                if row is None:
                    # UNKNOWN seeded route: probe in canonical order.
                    return self._selected("PROBE", route, account, rk, row,
                                          "UNKNOWN", "route is UNKNOWN; probe in canonical order",
                                          "unknown.probe", effective_strategy, free_only,
                                          goal_id, now, skipped)
                state = self._effective_state(row, now)
                capability = catalog.get(route["model"])
                if state == "AVAILABLE":
                    return self._selected("LAUNCH", route, account, rk, row, state,
                                          "recently verified success launches directly",
                                          "verified.launch", effective_strategy, free_only,
                                          goal_id, now, skipped)
                if state == "QUOTA":
                    reset_after = row.get("reset_after_ms")
                    changed = int(row.get("state_changed_at") or 0)
                    expired = bool(reset_after) and now >= changed + int(reset_after)
                    if expired:
                        return self._selected("PROBE", route, account, rk, row, state,
                                              "quota cooldown elapsed; eligible for one "
                                              "bounded recheck (reset still unknown)",
                                              REASON_QUOTA_UNKNOWN, effective_strategy,
                                              free_only, goal_id, now, skipped)
                    skipped.append(self._skip(rk, route, account, state,
                                              REASON_QUOTA_CONFIRMED,
                                              "confirmed quota cooldown is still active"))
                    continue
                if state == "TRANSIENT":
                    retries = int(row.get("transient_retries") or 0)
                    if retries < int(self.policy["defaults"]["max_transient_retries"]):
                        return self._selected("LAUNCH", route, account, rk, row, state,
                                              f"transient retry {retries + 1} within the "
                                              "bounded recovery rule (same route)",
                                              REASON_TRANSIENT, effective_strategy,
                                              free_only, goal_id, now, skipped)
                    skipped.append(self._skip(rk, route, account, state, REASON_TRANSIENT,
                                              "transient retry bound exhausted; blocked"))
                    continue
                if state == "AUTH_BLOCKED":
                    skipped.append(self._skip(rk, route, account, state, REASON_AUTH,
                                              "sign-in required; route paused for repair"))
                    continue
                if state == "CAPABILITY_UNAVAILABLE":
                    skipped.append(self._skip(rk, route, account, state, REASON_CAPABILITY,
                                              "explicit catalog omission; capability unavailable"))
                    continue
                if state == "PROBING":
                    skipped.append(self._skip(rk, route, account, state, "probe.in_flight",
                                              "another probe holds this route (lease)"))
                    continue
                # UNKNOWN / STALE
                if capability == "unavailable" and catalog_fresh:
                    skipped.append(self._skip(rk, route, account, state, REASON_CAPABILITY,
                                              "fresh catalog omits this model"))
                    continue
                return self._selected("PROBE", route, account, rk, row, state,
                                      f"{state} route probed in canonical order",
                                      "stale.probe" if state == "STALE" else "unknown.probe",
                                      effective_strategy, free_only, goal_id, now, skipped)

        reason = ("all free and subscription routes exhausted (stop BLOCKED)"
                  if not free_only else "all free routes blocked and FreeOnly forbids "
                  "the subscription tail (stop BLOCKED)")
        return self._blocked(reason, "routes.exhausted", effective_strategy, free_only,
                             goal_id, now, skipped,
                             subscription_blocked_by_free_only=free_only
                             and any(r["tier"] == TIER_SUBSCRIPTION
                                     for r in routes_for_strategy(strategy, self.policy)))

    # -- result builders ------------------------------------------------

    @staticmethod
    def _route_key(account: str, route: dict[str, Any]) -> str:
        from .policy import route_key
        return route_key(account, route["provider"], route["model"], route["tier"])

    def _skip(self, rk: str, route: dict[str, Any], account: str, state: str,
              reason_code: str, reason: str) -> dict[str, Any]:
        return {
            "route_key": rk,
            "model": route["model"],
            "provider": route["provider"],
            "account_alias": account,
            "tier": route["tier"],
            "state": state,
            "category": CATEGORY_BY_STATE.get(state, "unknown"),
            "reason_code": reason_code,
            "reason": reason,
        }

    def _selected(self, action: str, route: dict[str, Any], account: str, rk: str,
                  row: dict[str, Any] | None, state: str, reason: str, reason_code: str,
                  strategy: str, free_only: bool, goal_id: str | None, now: int,
                  skipped: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "action": action,
            "strategy": strategy,
            "free_only": free_only,
            "goal_id": goal_id,
            "selected_at": now,
            "reason_code": reason_code,
            "reason": reason,
            "route": {
                "route_key": rk,
                "model": route["model"],
                "provider": route["provider"],
                "tier": route["tier"],
                "account_alias": account,
                "thinking": route.get("thinking", "off"),
            },
            "evidence": {
                "state": state,
                "observed_at": (row or {}).get("observed_at"),
                "reset_after_ms": (row or {}).get("reset_after_ms"),
                "reset_known": (row or {}).get("reset_known"),
                "source_event_id": (row or {}).get("source_event_id"),
                "evidence_source": (row or {}).get("evidence_source"),
            },
            "skipped": skipped,
            "interrupt_healthy_attempt": False,
            "re_evaluate_at": None,
        }

    @staticmethod
    def _blocked(reason: str, reason_code: str, strategy: str, free_only: bool,
                 goal_id: str | None, now: int, skipped: list[dict[str, Any]],
                 **extra: Any) -> dict[str, Any]:
        result = {
            "action": "BLOCKED",
            "strategy": strategy,
            "free_only": free_only,
            "goal_id": goal_id,
            "selected_at": now,
            "reason_code": reason_code,
            "reason": reason,
            "route": None,
            "evidence": {},
            "skipped": skipped,
            "interrupt_healthy_attempt": False,
            "re_evaluate_at": None,
        }
        result.update(extra)
        return result


def context_escalation(goal: dict[str, Any]) -> dict[str, Any]:
    """Context exhaustion changes only this Goal; free-only Goals are protected."""
    free_only = bool(goal.get("free_only"))
    if free_only:
        return {
            "action": "ESCALATE_DENIED",
            "goal_id": goal.get("goal_id"),
            "new_strategy": goal.get("effective_strategy") or STRATEGY_FREE_FIRST,
            "preserve_session": True,
            "record_quota": False,
            "reason_code": "context.escalation_denied_free_only",
            "reason": "context exhaustion cannot escalate a FreeOnly Goal to subscription",
        }
    return {
        "action": "ESCALATE",
        "goal_id": goal.get("goal_id"),
        "new_strategy": STRATEGY_STANDARD,
        "preserve_session": True,
        "record_quota": False,
        "reason_code": "context.escalated_to_standard",
        "reason": "context exhaustion changes only this Goal to standard; session preserved",
    }
