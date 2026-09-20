"""Compatibility exports for demonstrated file-based consumers.

Generated from one snapshot + revision so every consumer sees the same
evidence. All files are written atomically and stamped with the revision,
timestamp and a digest over the generated set. Legacy and v2 must never write
these files concurrently; the installer retires the legacy guardian writer
before enabling v2 exports.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import COMPAT_POLICY_SCHEMA, COMPAT_POLICY_VERSION, __version__
from .events import EventStore, ms_to_iso, now_ms
from .policy import (FAILURE_TAXONOMY, load_canonical_policy, policy_digest,
                     routes_for_strategy)
from .snapshot import build_snapshot

GENERATED_BY = "cmo-v2"

# Routes whose evidence windows exist in the policy defaults.
FALLBACK_RESET_MS = {"free": 15 * 60 * 1000, "subscription": 5 * 60 * 1000}


def _serialize(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=False) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: Any) -> bytes:
    """Atomically write ``payload`` and return the exact bytes written."""
    return _atomic_write_bytes(path, _serialize(payload))


def _atomic_write_bytes(path: Path, data: bytes) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return data


def _stamp(payload: dict[str, Any], revision: int, now: int, digest: str) -> dict[str, Any]:
    payload["GeneratedBy"] = GENERATED_BY
    payload["GeneratedAt"] = ms_to_iso(now)
    payload["GeneratedRevision"] = revision
    payload["GeneratedDigest"] = digest
    payload["CmoVersion"] = __version__
    return payload


def _foundry_route_policy(snapshot: dict[str, Any], now: int) -> dict[str, Any]:
    policy = load_canonical_policy()
    free_models: list[str] = []
    for route in policy["routes"]:
        if route["tier"] == "free" and route["model"] not in free_models:
            free_models.append(route["model"])
    free_route = []
    for rank, model in enumerate(free_models, start=1):
        provider = next(r["provider"] for r in policy["routes"]
                        if r["model"] == model and r["tier"] == "free")
        accounts = next(r["accounts"] for r in policy["routes"]
                        if r["model"] == model and r["tier"] == "free")
        free_route.append({"rank": rank, "model": model, "provider": provider,
                           "tier": "FREE", "aliases": _aliases(model),
                           "piAccounts": list(accounts)})
    route_queue = []
    seq = 0
    for route in policy["routes"]:
        if route["tier"] != "free":
            continue
        for account in route["accounts"]:
            seq += 1
            route_queue.append({"seq": seq, "model": route["model"],
                                "piAccount": account, "tier": "FREE", "legKind": "free"})
    for route in policy["routes"]:
        if route["tier"] != "subscription":
            continue
        seq += 1
        route_queue.append({"seq": seq, "model": route["model"], "piAccount": None,
                            "tier": "SUBSCRIPTION", "legKind": "subscription"})
    # Live eligibility per leg, from the same snapshot the dashboard renders.
    by_key = {cell["route_key"]: cell for cell in snapshot["cells"]}
    for leg in route_queue:
        cell = next((c for c in by_key.values()
                     if c["model"] == leg["model"]
                     and (leg["piAccount"] is None or c["account_alias"] == leg["piAccount"])),
                    None)
        leg["state"] = cell["state"] if cell else "UNKNOWN"
        leg["eligible"] = bool(cell and cell["state"] == "AVAILABLE" and not cell["in_use"])
    decision = snapshot["decision"]
    return {
        "schema": COMPAT_POLICY_SCHEMA,
        "policyVersion": COMPAT_POLICY_VERSION,
        "owner": policy["owner"],
        "description": ("Canonical free-first routing policy owned solely by cline-model-optimizer "
                        "for Agent Foundry v3. Projected from CMO v2 state at the recorded revision."),
        "piAccounts": [a["id"] if isinstance(a, dict) else a for a in policy["accounts"]],
        "orderRule": policy.get("orderRule") or (
            "model-major: each free model runs across Pi accounts 1,2,3 in order before "
            "advancing to the next free model. The subscription tail runs only after every "
            "free leg is exhausted."),
        "freeRoute": free_route,
        "routeQueue": route_queue,
        "subscriptionFallback": [
            {"provider": route["provider"], "model": route["model"], "thinking": "off"}
            for route in policy["routes"] if route["tier"] == "subscription"],
        "neverPayg": True,
        "allowedTiers": ["FREE", "SUBSCRIPTION"],
        "failureTaxonomy": {
            kind: {"meaning": spec["meaning"], "signals": list(spec["signals"])}
            for kind, spec in FAILURE_TAXONOMY.items()},
        "capability": {
            "pi": ("Pi launchers MAY rotate isolated account profiles per this queue; each profile "
                   "carries its own credentials and budget."),
            "cline": ("The Cline extension exposes only the ACTIVE login on disk; legs for other "
                      "logins stay UNKNOWN until that login authenticates on this machine."),
        },
        "ownership": {
            "router": "cline-model-optimizer (sole routing-policy owner for Agent Foundry v3)",
            "consumers": ["Agent Foundry v3 launchers (pin or validate this policy)",
                          "interop gateway (forwards route decisions, never reorders them)"],
            "nonOwners": ("Foundry and the interop gateway MUST NOT define competing model/account "
                          "orders; they consume this policy."),
        },
        "pinning": {
            "howTo": ("Pin policyVersion + GeneratedDigest; validate schema, owner, neverPayg and "
                      "allowedTiers before routing."),
        },
        "policyDigest": policy_digest(policy),
        "liveState": {
            "stateRevision": snapshot["state_revision"],
            "generatedAt": ms_to_iso(now),
            "decision": decision["action"],
            "selectedGoalId": snapshot.get("selected_goal_id"),
            # The leg the optimizer would use next (PROBE and LAUNCH both name one).
            "selectedRoute": (decision.get("route") or {}).get("route_key"),
            "selectedModel": (decision.get("route") or {}).get("model"),
            "selectedAccount": (decision.get("route") or {}).get("account_alias"),
            "bestAvailable": next((leg["model"] + "@" + str(leg["piAccount"])
                                   for leg in route_queue if leg["eligible"]), None),
            "reasonCode": decision.get("reason_code"),
            "blockedReason": (decision.get("reason_code")
                              if decision["action"] == "BLOCKED" else None),
            "catalogStatus": snapshot["freshness"]["catalog_status"],
            "ineligibleModels": sorted({leg["model"] for leg in route_queue
                                        if not leg["eligible"]}),
        },
    }


def _aliases(model: str) -> list[str]:
    return {
        "cline-free/muse-spark-1.3-contributor": ["muse-spark-1.3"],
        "cline-free/deepseek-v4.1-flash": ["deepseek/deepseek-v4.1-flash"],
    }.get(model, [])


def _model_routing(snapshot: dict[str, Any], revision: int, now: int,
                   digest: str) -> dict[str, Any]:
    policy = load_canonical_policy()
    strategies: dict[str, Any] = {}
    for name, spec in policy["strategies"].items():
        plan = [{"provider": route["provider"], "model": route["model"],
                 "thinking": route.get("thinking", "off")}
                for route in routes_for_strategy(name, policy)]
        strategies[name] = {"description": spec["description"], "plan": plan, "act": plan}
    free_models = [r["model"] for r in policy["routes"] if r["tier"] == "free"]
    return _stamp({
        "version": 5,
        "strategy": "free-first",
        "routingOwner": "cline-model-optimizer",
        "canonicalPolicy": "foundry-route-policy.json",
        "paygAllowed": False,
        "autoSwitch": {"enabled": True, "allowSubscriptionFallback": True,
                       "allowPaidFallback": False},
        "contextEscalation": {
            "enabled": True, "targetStrategy": "standard", "preserveGoal": True,
            "recordQuota": False,
            "description": ("When Pi cannot compact or summarize the active Goal context, resume "
                            "that same Goal on ClinePass subscription. This is a context-capacity "
                            "transition, not evidence that a free account quota was consumed."),
        },
        "freeSelection": {
            "maxFreeModels": len(free_models),
            "preferredFreeModels": free_models,
            "fillFromDynamic": True,
            "subscriptionSteps": [
                {"provider": r["provider"], "model": r["model"], "thinking": "off"}
                for r in policy["routes"] if r["tier"] == "subscription"],
        },
        "dynamic": {
            "enabled": True,
            "endpoint": "https://api.cline.bot/api/v1/ai/cline/recommended-models",
            "timeoutSeconds": 10,
            "ttlHours": 6,
            "fallbackFreeModels": [],
        },
        "strategies": strategies,
        "tierRules": {
            "freePrefixes": ["cline-free/", "z-ai/"],
            "freeSuffixes": [":free"],
            "freeModels": free_models,
            "subscriptionPrefixes": ["cline-pass/"],
        },
        "status": _status_block(snapshot),
    }, revision, now, digest)


def _status_block(snapshot: dict[str, Any]) -> dict[str, Any]:
    decision = snapshot["decision"]
    # The chosen leg lives under "route" (None when BLOCKED); there is no
    # "selected" key. reason_code/reason live at decision top level.
    route = decision.get("route") or {}
    return {
        "decision": decision["action"],
        "selectedRouteKey": route.get("route_key"),
        "reasonCode": decision.get("reason_code") or decision.get("reason"),
        "stateRevision": snapshot["state_revision"],
        "catalogStatus": snapshot["freshness"]["catalog_status"],
        "serviceStatus": snapshot["service"]["status"],
    }


def _guardian_status(snapshot: dict[str, Any], revision: int, now: int,
                     digest: str) -> dict[str, Any]:
    decision = snapshot["decision"]
    # The chosen leg lives under "route" (None when BLOCKED); there is no
    # "selected" key. reason_code/reason live at decision top level.
    selected = decision.get("route") or {}
    message = (decision.get("reason_code") or decision.get("reason")
               or "route decision recorded")
    tier = selected.get("tier", "NONE")
    if decision["action"] == "LAUNCH" and tier:
        status = "SUB-IN-USE" if tier == "subscription" else "OK-FREE-OPTIMAL"
    elif decision["action"] == "PROBE":
        status = "CHECKING"
    else:
        status = "NO-ROUTE"
    act = {
        "Provider": selected.get("provider"),
        "Model": selected.get("model"),
        "Tier": tier.upper(),
        "Optimal": decision["action"] == "LAUNCH" and tier == "free",
        "Message": message,
        "Recommendation": {
            "Optimal": decision["action"] == "LAUNCH" and tier == "free",
            "Message": message,
        },
        "Account": selected.get("account_alias"),
        "RouteKey": selected.get("route_key"),
    }
    plan = dict(act)
    models = _pi_route_availability(snapshot)
    best = next((m for m in models if m["State"] == "AVAILABLE"), None)
    return _stamp({
        "Timestamp": ms_to_iso(now),
        "Status": status,
        "Strategy": snapshot["decision"].get("strategy", "free-first"),
        "Act": act,
        "Plan": plan,
        "TierMinutes": _tier_minutes(snapshot),
        "TierDate": ms_to_iso(now)[:10],
        "TokenRemainingHours": None,
        "DynamicFree": {
            "Source": "cmo-v2-catalog",
            "Count": len({r["model"] for r in snapshot["catalog"]}),
            "Fresh": snapshot["freshness"]["catalog_status"] == "fresh",
            "FetchedAt": ms_to_iso(snapshot["freshness"]["catalog_seen_at"])
            if snapshot["freshness"]["catalog_seen_at"] else None,
            "Error": ("catalog " + snapshot["freshness"]["catalog_status"]
                      if snapshot["freshness"]["catalog_status"] in ("error", "stale") else None),
        },
        "PiRouteAvailability": {
            "Source": "cmo-v2-route-state",
            "ObservedAt": ms_to_iso(now),
            "Models": models,
            "BestAvailable": best,
        },
        "ServiceStatus": snapshot["service"],
        "EvidenceNote": ("Derived only from recorded CMO evidence; an unknown leg is never "
                         "reported as available or exhausted."),
    }, revision, now, digest)


def _tier_minutes(snapshot: dict[str, Any]) -> dict[str, float]:
    minutes = {"free": 0.0, "subscription": 0.0, "paid": 0.0}
    for attempt in snapshot["attempts"]:
        tier = (attempt.get("tier") or "").lower()
        if tier not in minutes:
            continue
        started = attempt.get("started_at")
        ended = attempt.get("ended_at") or snapshot["generated_at"]
        if not started:
            continue
        minutes[tier] = round(minutes[tier] + max(0, int(ended) - int(started)) / 60000.0, 1)
    return minutes


def _pi_route_availability(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    order: list[str] = []
    for cell in snapshot["cells"]:
        if cell["tier"] == "free" and cell["model"] not in order:
            order.append(cell["model"])
    models: list[dict[str, Any]] = []
    for model in order:
        cells = [c for c in snapshot["cells"] if c["model"] == model and c["tier"] == "free"]
        available = [c["account_alias"] for c in cells if c["state"] == "AVAILABLE"]
        quota = [c["account_alias"] for c in cells if c["state"] in ("QUOTA", "QUOTA_EXPIRED")]
        unknown = [c["account_alias"] for c in cells
                   if c["account_alias"] not in available and c["account_alias"] not in quota]
        if available:
            state = "AVAILABLE"
        elif quota and len(quota) == len(cells):
            state = "QUOTA"
        else:
            state = "UNKNOWN"
        models.append({"Model": model, "State": state, "AvailableAccounts": available,
                       "QuotaAccounts": quota, "UnknownAccounts": unknown})
    return models


def _quota_state(snapshot: dict[str, Any], revision: int, now: int,
                 digest: str) -> dict[str, Any]:
    """Pi account-router ledger shape (demonstrated consumer)."""
    routes: dict[str, Any] = {}
    for cell in snapshot["cells"]:
        if cell["state"] not in ("QUOTA", "QUOTA_EXPIRED", "AUTH_BLOCKED", "CAPABILITY_UNAVAILABLE"):
            continue
        key = f"{cell['account_alias']}|{cell['provider']}|{cell['model']}"
        stage = cell["tier"]
        observed = cell.get("observed_at") or now
        reset_after = cell.get("reset_after_ms")
        if not reset_after or not cell.get("reset_known"):
            reset_after = FALLBACK_RESET_MS.get(stage, 15 * 60 * 1000)
        routes[key] = {
            "account": cell["account_alias"],
            "stage": stage,
            "provider": cell["provider"],
            "model": cell["model"],
            "exhaustedAt": ms_to_iso(observed),
            "blockedUntil": ms_to_iso(int(observed) + int(reset_after)),
            "reason": cell.get("last_reason_code") or "confirmed_quota_exhaustion",
            "state": cell["state"],
        }
    return _stamp({"version": 1, "routes": routes}, revision, now, digest)


def _free_models(snapshot: dict[str, Any], revision: int, now: int,
                 digest: str) -> dict[str, Any]:
    free: list[str] = []
    pass_models: list[str] = []
    for row in snapshot["catalog"]:
        if row["capability"] != "available":
            continue
        (pass_models if row["model"].startswith("cline-pass/") else free).append(row["model"])
    return _stamp({
        "Source": "cmo-v2-catalog",
        "FetchedAt": ms_to_iso(snapshot["freshness"]["catalog_seen_at"])
        if snapshot["freshness"]["catalog_seen_at"] else None,
        "Fresh": snapshot["freshness"]["catalog_status"] == "fresh",
        "Error": ("catalog " + snapshot["freshness"]["catalog_status"]
                  if snapshot["freshness"]["catalog_status"] in ("error", "stale") else None),
        "Count": len(free) + len(pass_models),
        "free": sorted(free),
        "clinePass": sorted(pass_models),
    }, revision, now, digest)


def write_compatibility_exports(store: EventStore, out_dir: Path, *,
                                now: int | None = None,
                                include_quota_state_path: Path | None = None) -> list[str]:
    out_dir = Path(out_dir)
    now = now_ms() if now is None else int(now)
    snapshot = build_snapshot(store)
    revision = snapshot["state_revision"]
    payloads = {
        "foundry-route-policy.json": _foundry_route_policy(snapshot, now),
        "model-routing.json": _model_routing(snapshot, revision, now, ""),
        "guardian-status.json": _guardian_status(snapshot, revision, now, ""),
        "guardian-state.json": {"LastCheck": ms_to_iso(now),
                                "LastStatus": _guardian_status(snapshot, revision, now, "")["Status"],
                                "GeneratedBy": GENERATED_BY},
        "free-models.json": _free_models(snapshot, revision, now, ""),
        "quota-state.json": _quota_state(snapshot, revision, now, ""),
    }
    canonical = {name: {k: v for k, v in payload.items() if k != "GeneratedDigest"}
                 for name, payload in payloads.items()}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str)
                            .encode("utf-8")).hexdigest()
    written: list[str] = []
    file_digests: dict[str, str] = {}
    for name, payload in payloads.items():
        if name == "guardian-state.json":
            payload["GeneratedDigest"] = digest
            payload["GeneratedRevision"] = revision
        else:
            _stamp(payload, revision, now, digest)
        data = _atomic_write(out_dir / name, payload)
        file_digests[name] = hashlib.sha256(data).hexdigest()
        written.append(name)
    manifest = _stamp({
        "schema": "cmo.compat-manifest/v1",
        "files": file_digests,
        "setDigest": digest,
        "stateRevision": revision,
        "policyDigest": policy_digest(load_canonical_policy()),
        "writer": GENERATED_BY,
        "note": ("Legacy guardian writers must be disabled before v2 writes these files. The "
                 "installer records the previous writer and rollback restores it."),
    }, revision, now, digest)
    _atomic_write(out_dir / "cmo-compat-manifest.json", manifest)
    written.append("cmo-compat-manifest.json")
    if include_quota_state_path is not None:
        _atomic_write_bytes(include_quota_state_path, _serialize(payloads["quota-state.json"]))
        written.append(str(include_quota_state_path))
    return written
