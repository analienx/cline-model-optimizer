"""CMO route policy: ``cmo.route-policy/v2`` validation, defaults and digest.

The schema makes pay-as-you-go impossible: only ``free`` and ``subscription``
tiers exist, every route names an explicit provider, and the canonical model
order is model-major (each model tried on account-1, account-2, account-3
before advancing to the next model).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

POLICY_SCHEMA = "cmo.route-policy/v2"

TIER_FREE = "free"
TIER_SUBSCRIPTION = "subscription"
ALLOWED_TIERS = (TIER_FREE, TIER_SUBSCRIPTION)

PROVIDER_CLINE = "cline"
PROVIDER_CLINE_PASS = "cline-pass"
ALLOWED_PROVIDERS = (PROVIDER_CLINE, PROVIDER_CLINE_PASS)

# The one canonical free ladder and subscription tail (spec: model-major).
CANONICAL_ROUTES: list[dict[str, Any]] = [
    {"model": "cline-free/muse-spark-1.3-contributor", "provider": PROVIDER_CLINE,
     "tier": TIER_FREE, "accounts": ["account-1", "account-2", "account-3"],
     "thinking": "off"},
    {"model": "z-ai/glm-5.3-flash", "provider": PROVIDER_CLINE,
     "tier": TIER_FREE, "accounts": ["account-1", "account-2", "account-3"],
     "thinking": "off"},
    {"model": "cline-free/deepseek-v4.1-flash", "provider": PROVIDER_CLINE,
     "tier": TIER_FREE, "accounts": ["account-1", "account-2", "account-3"],
     "thinking": "off"},
    {"model": "cline-pass/glm-5.3-flash", "provider": PROVIDER_CLINE_PASS,
     "tier": TIER_SUBSCRIPTION, "accounts": ["account-1"],
     "thinking": "off"},
    {"model": "cline-pass/deepseek-v4.1-flash", "provider": PROVIDER_CLINE_PASS,
     "tier": TIER_SUBSCRIPTION, "accounts": ["account-1"],
     "thinking": "off"},
]

FORBIDDEN_POLICY_KEYS = {"payg", "pay_as_you_go", "paid", "paid_providers", "allowedTiers"}
REQUIRED_TOP_KEYS = {"schema", "routes", "defaults"}
ALLOWED_TOP_KEYS = REQUIRED_TOP_KEYS | {
    "owner", "policyVersion", "neverPayg", "tiers", "accounts", "strategies",
    "failureTaxonomy", "orderRule", "description",
}

# A route entry may carry only these keys; anything else is rejected so that
# a paid fallback cannot be smuggled into the policy document.
ROUTE_KEYS = {"model", "provider", "tier", "accounts", "thinking", "aliases", "rank"}

STRATEGY_FREE_FIRST = "free-first"
STRATEGY_STANDARD = "standard"
FOCUSED_STRATEGIES = ("muse-flash", "glm-flash", "deepseek-flash")
ALL_STRATEGIES = (STRATEGY_FREE_FIRST, STRATEGY_STANDARD) + FOCUSED_STRATEGIES

# Nodes that a focused strategy keeps for each canonical model.
FOCUSED_MODEL_MATCH = {
    "muse-flash": "muse-spark",
    "glm-flash": "glm-5.3-flash",
    "deepseek-flash": "deepseek-v4.1-flash",
}

# Reason taxonomy shared with the Pi adapter (no invented percentages).
REASON_QUOTA_CONFIRMED = "quota.confirmed"
REASON_QUOTA_UNKNOWN = "quota.reset_unknown"
REASON_AUTH = "auth.required"
REASON_TRANSIENT = "transient.failure"
REASON_CAPABILITY = "capability.not_offered"
REASON_CONTEXT = "context.exhausted"
REASON_OVERRIDE = "manual.override"
REASON_UNKNOWN = "unknown"

FAILURE_TAXONOMY: dict[str, dict[str, Any]] = {
    "quota": {
        "states": ["QUOTA"],
        "reason_codes": [REASON_QUOTA_CONFIRMED, REASON_QUOTA_UNKNOWN],
        "meaning": "Free/subscription budget for this route is exhausted for now. "
                   "Advance to the next account, then the next model.",
        "signals": ["INFERENCE_CAP_ERROR", "inference cap", "status code 429",
                    "quota exceeded", "resource exhausted",
                    "cline_free_promotion_ended"],
    },
    "auth": {
        "states": ["AUTH_BLOCKED"],
        "reason_codes": [REASON_AUTH],
        "meaning": "Credential invalid, expired or missing. Do not advance the "
                   "queue as if budget were spent; surface re-sign-in.",
        "signals": ["401", "403", "unauthorized", "forbidden", "token expired",
                    "invalid_token", "auth expired", "re-sign in", "sign in"],
    },
    "transient": {
        "states": ["TRANSIENT"],
        "reason_codes": [REASON_TRANSIENT],
        "meaning": "Network or server hiccup. Retry the same route within the "
                   "bounded recovery rule; do not rotate as if quota were spent.",
        "signals": ["timeout", "timed out", "connection", "reset by peer", "502",
                    "503", "504", "temporarily unavailable", "fetch failed",
                    "unreachable", "econnreset", "econnrefused"],
    },
    "capability": {
        "states": ["CAPABILITY_UNAVAILABLE"],
        "reason_codes": [REASON_CAPABILITY],
        "meaning": "The catalog reports the model is not offered. Only a fresh "
                   "explicit catalog omission is capability evidence.",
        "signals": ["not offered", "no such model", "unknown model",
                    "model not found", "unsupported model"],
    },
    "context": {
        "states": [],
        "reason_codes": [REASON_CONTEXT],
        "meaning": "The active Goal context could not be compacted. Preserve the "
                   "exact Goal/session; this is not quota exhaustion.",
        "signals": ["to-compaction failed", "summarization failed",
                    "context window exceeded", "context overflow",
                    "compaction exhausted", "generation hit the token cap"],
    },
}


class PolicyError(ValueError):
    """Raised when a policy document is invalid under cmo.route-policy/v2."""


def canonical_policy_document() -> dict[str, Any]:
    """The canonical v2 policy document (single source of route truth)."""
    return {
        "schema": POLICY_SCHEMA,
        "owner": "cline-model-optimizer",
        "policyVersion": "2.0.0",
        "neverPayg": True,
        "tiers": list(ALLOWED_TIERS),
        "accounts": [
            {"id": "account-1", "enabled": True},
            {"id": "account-2", "enabled": True},
            {"id": "account-3", "enabled": True},
        ],
        "strategies": {
            STRATEGY_FREE_FIRST: {
                "description": "Model-major free ladder then authorized "
                               "ClinePass subscription tail. Never PAYG.",
                "tiers": ["free", "subscription"],
                "routeOrder": "canonical",
            },
            STRATEGY_STANDARD: {
                "description": "Authorized ClinePass subscription only.",
                "tiers": ["subscription"],
                "routeOrder": "canonical",
            },
            "muse-flash": {
                "description": "Focused: Muse Spark 1.3 only.",
                "tiers": ["free", "subscription"],
                "models": ["cline-free/muse-spark-1.3-contributor"],
            },
            "glm-flash": {
                "description": "Focused: GLM 5.3 Flash only.",
                "tiers": ["free", "subscription"],
                "models": ["z-ai/glm-5.3-flash", "cline-pass/glm-5.3-flash"],
            },
            "deepseek-flash": {
                "description": "Focused: DeepSeek V4.1 Flash only.",
                "tiers": ["free", "subscription"],
                "models": ["cline-free/deepseek-v4.1-flash",
                           "cline-pass/deepseek-v4.1-flash"],
            },
        },
        "routes": CANONICAL_ROUTES,
        "defaults": {
            "evidence_ttl_ms": 15 * 60 * 1000,
            "catalog_refresh_ms": 15 * 60 * 1000,
            "catalog_startup_refresh": True,
            "heartbeat_ms": 30 * 1000,
            "disconnected_after_ms": 90 * 1000,
            "browser_freshness_ms": 15 * 1000,
            "sse_max_latency_ms": 2000,
            "free_reset_after_ms": 15 * 60 * 1000,
            "subscription_reset_after_ms": 5 * 60 * 1000,
            "reset_time_known": False,
            "max_transient_retries": 2,
            "probe_lease_ms": 60 * 1000,
            "probe_timeout_ms": 30 * 1000,
            "success_ttl_ms": 15 * 60 * 1000,
        },
        "failureTaxonomy": FAILURE_TAXONOMY,
    }


def validate_policy(doc: Any) -> dict[str, Any]:
    """Validate a policy document; raise PolicyError on any violation."""
    if not isinstance(doc, dict):
        raise PolicyError("policy must be a JSON object")
    missing = sorted(REQUIRED_TOP_KEYS - set(doc))
    if missing:
        raise PolicyError(f"missing required keys: {missing}")
    if doc["schema"] != POLICY_SCHEMA:
        raise PolicyError(f"schema must be {POLICY_SCHEMA!r}, got {doc['schema']!r}")

    forbidden = FORBIDDEN_POLICY_KEYS & set(doc)
    if forbidden:
        raise PolicyError(f"PAYG-bearing keys are forbidden by schema: {sorted(forbidden)}")
    unexpected_top = set(doc) - ALLOWED_TOP_KEYS
    if unexpected_top:
        raise PolicyError(f"unexpected top-level keys: {sorted(unexpected_top)}")
    if doc.get("neverPayg") is not True:
        raise PolicyError("neverPayg must be true; PAYG is impossible by schema")
    if "tiers" in doc and list(doc["tiers"]) != list(ALLOWED_TIERS):
        raise PolicyError(f"tiers must be exactly {list(ALLOWED_TIERS)}; PAYG is impossible")

    routes = doc["routes"]
    if not isinstance(routes, list) or not routes:
        raise PolicyError("routes must be a non-empty list")
    seen_models: set[str] = set()
    for i, route in enumerate(routes):
        if not isinstance(route, dict):
            raise PolicyError(f"route[{i}] must be an object")
        unexpected = set(route) - ROUTE_KEYS
        if unexpected:
            raise PolicyError(f"route[{i}] has unexpected keys: {sorted(unexpected)}")
        model = route.get("model")
        if not isinstance(model, str) or not model:
            raise PolicyError(f"route[{i}].model must be a non-empty string")
        if model in seen_models:
            raise PolicyError(f"duplicate route model: {model}")
        seen_models.add(model)
        if route.get("tier") not in ALLOWED_TIERS:
            raise PolicyError(
                f"route[{i}].tier must be one of {ALLOWED_TIERS} (PAYG is impossible by schema)")
        provider = route.get("provider")
        if provider not in ALLOWED_PROVIDERS:
            raise PolicyError(f"route[{i}].provider must be one of {ALLOWED_PROVIDERS}")
        accounts = route.get("accounts")
        if (not isinstance(accounts, list) or not accounts
                or not all(isinstance(a, str) and a for a in accounts)):
            raise PolicyError(f"route[{i}].accounts must be a non-empty list of non-empty strings")
        if len(set(accounts)) != len(accounts):
            raise PolicyError(f"route[{i}].accounts must not contain duplicates")
        if route.get("tier") == TIER_FREE and provider != PROVIDER_CLINE:
            raise PolicyError(f"route[{i}] free routes must use provider {PROVIDER_CLINE}")
        if route.get("tier") == TIER_SUBSCRIPTION and provider != PROVIDER_CLINE_PASS:
            raise PolicyError(
                f"route[{i}] subscription routes must use provider {PROVIDER_CLINE_PASS}")

    defaults = doc["defaults"]
    if not isinstance(defaults, dict):
        raise PolicyError("defaults must be an object")
    for key in ("evidence_ttl_ms", "free_reset_after_ms", "subscription_reset_after_ms",
                "max_transient_retries", "heartbeat_ms", "disconnected_after_ms"):
        value = defaults.get(key)
        if not isinstance(value, int) or value < 0 or (value == 0 and key != "max_transient_retries"):
            raise PolicyError(f"defaults.{key} must be a positive integer")

    strategies = doc.get("strategies")
    if not isinstance(strategies, dict) or not strategies:
        raise PolicyError("strategies must be a non-empty object")
    for name in ALL_STRATEGIES:
        if name not in strategies:
            raise PolicyError(f"strategies.{name} must be defined")

    return doc


def load_canonical_policy() -> dict[str, Any]:
    return validate_policy(canonical_policy_document())


def policy_digest(doc: dict[str, Any]) -> str:
    """Stable sha256 digest of the canonical JSON encoding."""
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def route_key(account_alias: str, provider: str, model: str, tier: str) -> str:
    """Canonical route identity: alias + provider + normalized model + tier."""
    normalized = (model or "").strip().lower()
    return f"{(account_alias or '').strip().lower()}|{(provider or '').strip().lower()}|{normalized}|{(tier or '').strip().lower()}"


def parse_route_key(key: str) -> dict[str, str]:
    parts = (key or "").split("|")
    if len(parts) != 4:
        raise PolicyError(f"invalid route key: {key!r}")
    return {"account_alias": parts[0], "provider": parts[1],
            "model": parts[2], "tier": parts[3]}


def all_routes(policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return list((policy or load_canonical_policy())["routes"])


def route_cells(policy: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Every (route, account) cell as a canonical ordered list."""
    cells: list[dict[str, str]] = []
    for route in all_routes(policy):
        for account in route["accounts"]:
            cells.append({
                "route_key": route_key(account, route["provider"], route["model"], route["tier"]),
                "account_alias": account,
                "provider": route["provider"],
                "model": route["model"],
                "tier": route["tier"],
            })
    return cells


def routes_for_strategy(strategy: str, policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Ordered routes for a strategy; focused strategies filter by model."""
    policy = policy or load_canonical_policy()
    routes = all_routes(policy)
    if strategy == STRATEGY_STANDARD:
        return [r for r in routes if r["tier"] == TIER_SUBSCRIPTION]
    if strategy in FOCUSED_STRATEGIES:
        needle = FOCUSED_MODEL_MATCH[strategy]
        return [r for r in routes if needle in r["model"]]
    return routes
