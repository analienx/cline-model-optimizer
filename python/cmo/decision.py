"""CMO v2 decision engine: canonical free-first route, never PAYG.

Route states: UNKNOWN PROBING AVAILABLE QUOTA AUTH_BLOCKED TRANSIENT
CAPABILITY_UNAVAILABLE STALE.
"""

from __future__ import annotations

import time
from typing import Any

from .policy import TIER_FREE, TIER_SUBSCRIPTION, load_canonical_policy

ROUTE_STATES = (
    "UNKNOWN", "PROBING", "AVAILABLE", "QUOTA", "AUTH_BLOCKED",
    "TRANSIENT", "CAPABILITY_UNAVAILABLE", "STALE",
)

FREE_DEFAULT_RESET_MS = 15 * 60 * 1000
SUBSCRIPTION_DEFAULT_RESET_MS = 5 * 60 * 1000


class DecisionEngine:
    """Deterministic canonical-order decision engine over route_state rows."""

    def __init__(self, policy: dict[str, Any] | None = None, now_ms: int | None = None):
        self.policy = policy or load_canonical_policy()
        self._now = now_ms if now_ms is not None else (lambda: int(time.time() * 1000))

    # ---- state helpers -------------------------------------------------

    @staticmethod
    def _is_quota_reset(row: dict[str, Any], now: int) -> bool:
        reset_after = row.get("reset_after_ms")
        if not reset_after:
            return False
        return now >= int(row.get("state_changed_at") or 0) + int(reset_after)

    def decide(self, route_states: list[dict[str, Any]], overrides: list[dict[str, Any]],
               strategy: str = "free-first", goal_id: str | None = None) -> dict[str, Any]:
        """Return {action, route, reason, evidence} for the next launch.

        route_states rows: {model, account_alias, state, state_changed_at,
        reset_after_ms, last_probe_at, capability}. overrides rows:
        {model, account_alias, kind ('FORCE_SKIP'), expires_at, reason}.
        """
        now = self._now()
        active_skips = {
            (o.get("model"), o.get("account_alias"))
            for o in overrides
            if o.get("kind") == "FORCE_SKIP" and (not o.get("expires_at") or o["expires_at"] > now)
        }
        # Account-1 -> 2 -> 3 before any model advance; Muse -> GLM -> DeepSeek -> ClinePass.
        free_routes = [r for r in self.policy["routes"] if r["tier"] == TIER_FREE]
        sub_routes = [r for r in self.policy["routes"] if r["tier"] == TIER_SUBSCRIPTION]

        state_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for row in route_states or []:
            state_by_key[(row.get("model"), row.get("account_alias"))] = row

        def unavailable(row: dict[str, Any]) -> bool:
            st = (state_by_key.get((route["model"], account)) or {}).get("state", "UNKNOWN")
            if st == "AVAILABLE":
                return False
            if st == "QUOTA" and not self._is_quota_reset(state_by_key[(route["model"], account)], now):
                return True
            if st in ("AUTH_BLOCKED", "QUOTA", "CAPABILITY_UNAVAILABLE"):
                return True
            return False

        def candidate(row: dict[str, Any], route: dict[str, Any], account: str):
            """Return (acceptable, action, reason) for one route cell."""
            st = row.get("state", "UNKNOWN")
            if st == "AVAILABLE":
                return True, "LAUNCH", "recent AVAILABLE launches directly"
            if st == "QUOTA":
                if self._is_quota_reset(row, now):
                    return True, "LAUNCH", "confirmed quota cooldown elapsed (provider reset)"
                return False, None, "quota in confirmed cooldown"
            if st == "TRANSIENT":
                retries = int(row.get("transient_retries") or 0)
                if retries < int(self.policy["defaults"].get("max_transient_retries", 2)):
                    return True, "LAUNCH", f"transient retry {retries + 1} within bounded recovery rule"
                return False, None, "transient retry bound exhausted"
            if st in ("UNKNOWN", "STALE"):
                return True, "PROBE", f"{st} route probed in canonical order"
            return False, None, f"{st} is skipped without being called quota"

        if strategy == "standard":
            for route in sub_routes:
                for account in route["accounts"]:
                    if (route["model"], account) in active_skips:
                        continue
                    ok, action, reason = candidate(state_by_key.get((route["model"], account)) or {},
                                                   route, account)
                    if ok:
                        return self._launch(route, account,
                                            state_by_key.get((route["model"], account)) or {},
                                            f"standard strategy: {reason}", action=action)
            return {"action": "BLOCKED", "route": None,
                    "reason": "subscription exhausted under standard strategy (stop BLOCKED)"}

        # free-first (canonical)
        for route in free_routes:
            for account in route["accounts"]:
                if (route["model"], account) in active_skips:
                    continue
                row = state_by_key.get((route["model"], account)) or {}
                ok, action, reason = candidate(row, route, account)
                if ok:
                    return self._launch(route, account, row, reason, action=action)
        for route in sub_routes:
            for account in route["accounts"]:
                if (route["model"], account) in active_skips:
                    continue
                row = state_by_key.get((route["model"], account)) or {}
                ok, action, reason = candidate(row, route, account)
                if ok:
                    return self._launch(route, account, row,
                                        "free routes unavailable; ClinePass subscription tail: " + reason,
                                        action=action)
        return {"action": "BLOCKED", "route": None,
                "reason": "all free and subscription routes exhausted (stop BLOCKED)"}

    @staticmethod
    def _launch(route: dict[str, Any], account: str, row: dict[str, Any],
                reason: str, action: str = "LAUNCH") -> dict[str, Any]:
        return {
            "action": action,
            "route": {"model": route["model"], "tier": route["tier"], "account_alias": account},
            "reason": reason,
            "evidence": {
                "observed_state": row.get("state", "UNKNOWN"),
                "last_probe_at": row.get("last_probe_at"),
                "reset_after_ms": row.get("reset_after_ms"),
            },
        }

    @staticmethod
    def context_escalation(goal: dict[str, Any]) -> dict[str, Any]:
        """Context exhaustion: only that Goal becomes standard; session preserved; no quota recorded."""
        return {
            "action": "ESCALATE",
            "goal_id": goal.get("goal_id"),
            "new_strategy": "standard",
            "preserve_session": True,
            "record_quota": False,
            "reason": "context exhaustion changes only this Goal to standard",
        }
