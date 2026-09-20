"""Shared snapshot builder.

One implementation powers the CLI, the HTTP API and the compatibility
exports, so every surface reports the same evidence at the same revision.
"""

from __future__ import annotations

import time
from typing import Any

from . import SNAPSHOT_SCHEMA, __version__
from .decision import DecisionEngine
from .events import (FIXTURE_GOAL_IDS, FIXTURE_SOURCES, ms_to_iso, now_ms)
from .policy import (REASON_OVERRIDE, STRATEGY_FREE_FIRST, TIER_FREE,
                     TIER_SUBSCRIPTION, load_canonical_policy, policy_digest,
                     route_cells)

STATE_LABELS = {
    "AVAILABLE": "Recently verified",
    "QUOTA": "Quota exhausted",
    "QUOTA_EXPIRED": "Checking",
    "AUTH_BLOCKED": "Sign-in required",
    "TRANSIENT": "Temporary failure",
    "PROBING": "Checking",
    "UNKNOWN": "Unknown",
    "STALE": "Stale",
    "CAPABILITY_UNAVAILABLE": "Not offered",
    "FORCE_SKIP": "Manual override",
}

DISCONNECTED_AFTER_MS = 90_000


def build_snapshot(store: Any, *, now: int | None = None,
                   goal_id: str | None = None,
                   strategy: str | None = None,
                   artifact_digest: str | None = None,
                   started_at: int | None = None,
                   pid: int | None = None) -> dict[str, Any]:
    now = now_ms() if now is None else int(now)
    policy, policy_version, policy_saved = store.active_policy_doc()
    defaults = policy["defaults"]
    ttl = int(defaults["evidence_ttl_ms"])

    route_state = store.route_state()
    goals = store.goals()
    attempts = store.attempts()
    overrides = store.overrides(now)
    catalog_rows = store.catalog()
    revision = store.revision()

    catalog = {row["model"]: row["capability"] for row in catalog_rows}
    catalog_seen = max([int(r["catalog_seen_at"] or 0) for r in catalog_rows] or [0])
    catalog_last_error_at = store.meta("catalog_last_error_at") or ""
    catalog_fresh = bool(catalog_seen) and (now - catalog_seen) <= int(
        defaults["catalog_refresh_ms"])
    # Retain last-good capability on error but surface the failure honestly.
    catalog_status = "unknown"
    if catalog_seen and catalog_fresh:
        catalog_status = "fresh"
    elif catalog_seen:
        catalog_status = "stale"
    if catalog_last_error_at:
        try:
            # A failure reported at or after the last success must be surfaced.
            # ``catalog.refreshed`` clears this marker, so equality can only
            # happen when the failure event arrived after the last success.
            if int(catalog_last_error_at) >= catalog_seen:
                catalog_status = "error"
        except ValueError:
            pass

    active_overrides = {(o.get("route_key")): o for o in overrides}
    try:
        history = store.recheck_history(limit=10)
        recheck_rows = store.recheck_requests(status="running") + \
            store.recheck_requests(status="pending")
    except Exception:
        history = []
        recheck_rows = []
    recheck_by_route = {}
    for req in recheck_rows:
        recheck_by_route.setdefault(req.get("route_key"), req)
    goals = _derive_goal_liveness(goals, now)
    active_attempts: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        if attempt.get("status") != "running":
            continue
        last = attempt.get("last_heartbeat_at") or attempt.get("started_at") or 0
        attempt["liveness"] = ("live" if now - int(last) <= DISCONNECTED_AFTER_MS else
                               "disconnected")
        if attempt.get("route_key"):
            active_attempts.setdefault(attempt["route_key"], attempt)

    state_by_key = {r["route_key"]: r for r in route_state}

    cells: list[dict[str, Any]] = []
    for cell in route_cells(policy):
        row = state_by_key.get(cell["route_key"], {})
        cells.append(_build_cell(cell, row, now, ttl, active_overrides, active_attempts,
                                 catalog, catalog_fresh, defaults,
                                 recheck_by_route=recheck_by_route))

    selected_goal = _select_goal(goals, goal_id)
    effective_strategy = strategy or (selected_goal or {}).get("effective_strategy") \
        or STRATEGY_FREE_FIRST
    free_only = bool((selected_goal or {}).get("free_only"))

    engine = DecisionEngine(policy, now)
    decision = engine.decide(route_state, overrides, strategy=effective_strategy,
                             goal_id=(selected_goal or {}).get("goal_id"),
                             free_only=free_only, catalog=catalog,
                             catalog_fresh=catalog_fresh)

    service = _service_status(store, now, revision, artifact_digest, started_at, pid,
                              catalog_status)

    return {
        "schema": SNAPSHOT_SCHEMA,
        "cmo_version": __version__,
        "generated_at": now,
        "generated_at_iso": ms_to_iso(now),
        "state_revision": revision,
        "policy": {
            "schema": policy["schema"],
            "policyVersion": policy["policyVersion"],
            "owner": policy["owner"],
            "neverPayg": policy["neverPayg"],
            "digest": policy_digest(policy),
            "version": policy_version,
            "saved": policy_saved,
            "accounts": policy.get("accounts", []),
            "routes": policy["routes"],
            "strategies": policy["strategies"],
            "defaults": defaults,
            "failureTaxonomy": policy["failureTaxonomy"],
        },
        "service": service,
        "freshness": {
            "evidence_ttl_ms": ttl,
            "browser_clock_ms": int(defaults["browser_freshness_ms"]),
            "browser_freshness_ms": int(defaults["browser_freshness_ms"]),
            "evaluated_at": now,
            "catalog_status": catalog_status,
            "catalog_seen_at": catalog_seen or None,
            "catalog_age_ms": (now - catalog_seen) if catalog_seen else None,
            "catalog_last_error_at": int(catalog_last_error_at)
            if catalog_last_error_at else None,
        },
        "goals": goals,
        "selected_goal_id": (selected_goal or {}).get("goal_id"),
        "data_quality": _data_quality(store, now),
        "attempts": attempts,
        "free_matrix": _free_matrix(policy, cells),
        "subscription": [c for c in cells if c["tier"] == TIER_SUBSCRIPTION],
        "cells": cells,
        "catalog": catalog_rows,
        "overrides": overrides,
        "recheck_requests": (recheck_rows if isinstance(recheck_rows, list) else []) + [
            r for r in history
            if str(r.get("status", "")) not in ("pending", "running")
        ][:10],
        "decision": decision,
        "recent_events": store.events(limit=50),
    }


def _derive_goal_liveness(goals: list[dict[str, Any]], now: int) -> list[dict[str, Any]]:
    """Derive display liveness for goals without rewriting stored status.

    A goal still stored as ``active`` but with no heartbeat/session activity
    beyond ``DISCONNECTED_AFTER_MS`` is shown as ``disconnected`` so the
    dashboard never presents a stale goal as live. The stored row is left
    untouched; callers must emit a real ``goal.disconnected`` event to change it.
    """
    derived = []
    for goal in goals:
        view = dict(goal)
        last = (view.get("last_activity_at") or view.get("updated_at")
                or view.get("started_at") or 0)
        view["last_activity_age_ms"] = (now - int(last)) if last else None
        view["liveness"] = "live"
        if view.get("status") == "active" and last \
                and now - int(last) > DISCONNECTED_AFTER_MS:
            view["liveness"] = "disconnected"
            view["display_status"] = "disconnected"
        else:
            view["display_status"] = view.get("status")
        derived.append(view)
    return derived


def _data_quality(store: Any, now: int) -> dict[str, Any]:
    """Summarize evidence provenance so fixture data can never pass as live.

    Counts events by source family. Sources used by browser acceptance and
    synthetic harnesses are reported separately as ``fixture_events``; the
    ``banner`` is non-null when such rows exist in the live database and they
    must be quarantined (see ``migration.quarantine_fixtures``) before the
    dashboard can be treated as trustworthy.
    """
    fixture_sources = tuple(sorted(FIXTURE_SOURCES))
    fixture_goals = set(FIXTURE_GOAL_IDS)
    counts: dict[str, int] = {}
    fixture_events = 0
    truncated_identities = 0
    try:
        rows = store.events(limit=5000)
    except Exception:
        rows = []
    for event in rows:
        source = event.get("source_component") or "unknown"
        counts[source] = counts.get(source, 0) + 1
        if source in fixture_sources or event.get("goal_id") in fixture_goals:
            fixture_events += 1
        account = event.get("account_alias") or ""
        model = event.get("model") or ""
        if event.get("event_type", "").startswith(("route.", "attempt.")) and \
                (account in ("a",) or model in ("m",)):
            truncated_identities += 1
    banner = None
    if fixture_events:
        banner = (f"{fixture_events} fixture/synthetic event(s) are present in the live "
                  "evidence store; route states derived from them are not trustworthy. "
                  "Run fixture quarantine before relying on this dashboard.")
    elif truncated_identities:
        banner = (f"{truncated_identities} event(s) carry truncated route identities; "
                  "affected cells cannot be trusted until the source is fixed.")
    return {
        "sources": counts,
        "fixture_events": fixture_events,
        "truncated_identities": truncated_identities,
        "banner": banner,
    }


def _select_goal(goals: list[dict[str, Any]], goal_id: str | None) -> dict[str, Any] | None:
    if goal_id:
        for goal in goals:
            if goal["goal_id"] == goal_id:
                return goal
    active = [g for g in goals if g.get("status") in ("active", "paused", "blocked",
                                                      "disconnected")]
    live = [g for g in active if g.get("liveness", "live") == "live"]
    if live:
        return live[0]
    if active:
        return active[0]
    return goals[0] if goals else None


def _build_cell(cell: dict[str, str], row: dict[str, Any], now: int, ttl: int,
                overrides: dict[str, dict[str, Any]],
                active_attempts: dict[str, dict[str, Any]],
                catalog: dict[str, str], catalog_fresh: bool,
                defaults: dict[str, Any],
                recheck_by_route: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    state = (row.get("state") or "UNKNOWN").upper()
    observed = row.get("observed_at")
    age = (now - int(observed)) if observed else None
    reset_after = row.get("reset_after_ms")
    changed = int(row.get("state_changed_at") or 0)
    quota_expired = bool(reset_after) and changed and now >= changed + int(reset_after)
    display_state = state
    if state == "AVAILABLE" and (age is None or age > ttl):
        display_state = "STALE"
    elif state == "QUOTA" and quota_expired:
        display_state = "QUOTA_EXPIRED"
    catalog_capability = catalog.get(cell["model"])
    if state in ("UNKNOWN", "STALE") and catalog_fresh and catalog_capability == "unavailable":
        display_state = "CAPABILITY_UNAVAILABLE"

    override = overrides.get(cell["route_key"])
    if override and (not override.get("expires_at") or int(override["expires_at"]) > now):
        display_state = "FORCE_SKIP"

    attempt = active_attempts.get(cell["route_key"])
    in_use = attempt is not None

    return {
        "route_key": cell["route_key"],
        "account_alias": cell["account_alias"],
        "provider": cell["provider"],
        "model": cell["model"],
        "tier": cell["tier"],
        "state": display_state,
        "stored_state": state,
        "state_label": STATE_LABELS.get(display_state, "Unknown"),
        "state_changed_at": changed or None,
        "observed_at": int(observed) if observed else None,
        "observed_at_iso": ms_to_iso(int(observed)) if observed else None,
        "received_at": row.get("received_at"),
        "age_ms": age,
        "freshness": ("none" if age is None else "fresh" if age <= ttl else "stale"),
        "validity": "valid" if age is not None and age <= ttl else "expired",
        "reset_after_ms": reset_after,
        "reset_known": row.get("reset_known"),
        "reset_at": row.get("reset_at"),
        "reset_at_iso": ms_to_iso(row.get("reset_at")) if row.get("reset_at") else None,
        "next_check_at": row.get("next_check_at"),
        "next_check_at_iso": (ms_to_iso(row.get("next_check_at"))
                              if row.get("next_check_at") else None),
        "last_reason_code": row.get("last_reason_code"),
        "capability": row.get("capability"),
        "catalog_capability": catalog_capability,
        "transient_retries": row.get("transient_retries") or 0,
        "evidence_source": row.get("evidence_source"),
        "source_event_id": row.get("source_event_id"),
        "in_use": in_use,
        "in_use_evidence": (attempt.get("attempt_id") if attempt else None),
        "in_use_liveness": (attempt.get("liveness") if attempt else None),
        "manual_override": bool(override),
        "manual_override_reason": (override.get("reason") if override else None),
        "manual_override_expires_at": (override.get("expires_at") if override else None),
        "unknown_reason": _unknown_reason(state, row, age, ttl, catalog_fresh,
                                            catalog_capability),
        "recheck": _recheck_view((recheck_by_route or {}).get(cell["route_key"])),
    }


def _recheck_view(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not request:
        return None
    return {
        "request_id": request.get("request_id"),
        "status": request.get("status"),
        "requested_at": request.get("requested_at"),
        "requested_by": request.get("requested_by"),
        "reason": request.get("reason") or request.get("last_error"),
    }


def _unknown_reason(state: str, row: dict[str, Any], age: int | None, ttl: int,
                    catalog_fresh: bool, catalog_capability: str | None) -> str | None:
    """Explain *why* a route has no usable evidence instead of bare UNKNOWN."""
    if state not in ("UNKNOWN", "STALE", "QUOTA_EXPIRED"):
        return None
    source = row.get("evidence_source") if row else None
    if source in FIXTURE_SOURCES:
        return ("evidence comes from a synthetic/fixture source; "
                "quarantine fixture data before trusting this route")
    if not row or not row.get("observed_at"):
        if row and row.get("auth_pending"):
            return "account sign-in not completed; no provider observation exists yet"
        return "never observed: no probe or attempt evidence recorded for this route"
    if age is not None and age > ttl:
        return ("evidence expired: last observation is older than the evidence TTL; "
                "request a recheck to refresh it")
    if catalog_fresh and catalog_capability == "unavailable":
        return "provider catalog reports this model as unavailable"
    if not source:
        return "observation has no recorded source; provenance unknown"
    return None


def _free_matrix(policy: dict[str, Any],
                 cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {c["route_key"]: c for c in cells}
    matrix: list[dict[str, Any]] = []
    for route in policy["routes"]:
        if route["tier"] != TIER_FREE:
            continue
        from .policy import route_key
        accounts = {}
        for account in route["accounts"]:
            rk = route_key(account, route["provider"], route["model"], route["tier"])
            accounts[account] = by_key.get(rk)
        matrix.append({"model": route["model"], "provider": route["provider"],
                       "tier": TIER_FREE, "accounts": accounts})
    return matrix


def _service_status(store: Any, now: int, revision: int, artifact_digest: str | None,
                    started_at: int | None, pid: int | None,
                    catalog_status: str) -> dict[str, Any]:
    db_ok = True
    try:
        store.revision()
    except Exception:  # pragma: no cover - defensive
        db_ok = False
    last_event = None
    events = store.events(limit=1)
    if events:
        last_event = events[0].get("received_at")
    disconnected = [a for a in store.attempts()
                    if a.get("status") == "running"
                    and now - int(a.get("last_heartbeat_at") or a.get("started_at") or 0)
                    > DISCONNECTED_AFTER_MS]
    status = "connected"
    issues: list[str] = []
    if not db_ok:
        status = "offline"
        issues.append("state database is unreachable")
    if catalog_status in ("error", "stale"):
        if status == "connected":
            status = "degraded"
        issues.append(f"catalog {catalog_status}")
    if disconnected:
        if status == "connected":
            status = "degraded"
        issues.append(f"{len(disconnected)} attempt(s) disconnected")
    return {
        "status": status,
        "ok": db_ok,
        "issues": issues,
        "db": "ok" if db_ok else "error",
        "state_revision": revision,
        "artifact_digest": artifact_digest,
        "started_at": started_at,
        "started_at_iso": ms_to_iso(started_at) if started_at else None,
        "pid": pid,
        "uptime_ms": (now - started_at) if started_at else None,
        "last_event_received_at": last_event,
        "last_event_received_iso": ms_to_iso(last_event) if last_event else None,
        "catalog_status": catalog_status,
    }
