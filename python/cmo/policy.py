"""CMO v2 route-policy: cmo.route-policy/v2 validation and digest.

The policy schema makes PAYG impossible: only `free` and `subscription`
tiers exist, and every route entry must name a model plus account aliases.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

POLICY_SCHEMA = "cmo.route-policy/v2"

# Canonical route order (spec 7.2). Tiers are only FREE and SUBSCRIPTION.
TIER_FREE = "free"
TIER_SUBSCRIPTION = "subscription"
ALLOWED_TIERS = (TIER_FREE, TIER_SUBSCRIPTION)

CANONICAL_ROUTES: list[dict[str, Any]] = [
    {"model": "cline-free/muse-spark-1.3-contributor", "tier": TIER_FREE,
     "accounts": ["account-1", "account-2", "account-3"]},
    {"model": "z-ai/glm-5.3-flash", "tier": TIER_FREE,
     "accounts": ["account-1", "account-2", "account-3"]},
    {"model": "cline-free/deepseek-v4.1-flash", "tier": TIER_FREE,
     "accounts": ["account-1", "account-2", "account-3"]},
    {"model": "cline-pass/glm-5.3-flash", "tier": TIER_SUBSCRIPTION,
     "accounts": ["account-1"]},
    {"model": "cline-pass/deepseek-v4.1-flash", "tier": TIER_SUBSCRIPTION,
     "accounts": ["account-1"]},
]

FORBIDDEN_POLICY_KEYS = {"payg", "pay_as_you_go", "paid", "paid_providers"}

REQUIRED_TOP_KEYS = {"schema", "routes", "defaults"}


def canonical_policy_document() -> dict[str, Any]:
    """The canonical v2 policy document (single source of route truth)."""
    return {
        "schema": POLICY_SCHEMA,
        "routes": CANONICAL_ROUTES,
        "defaults": {
            "free_reset_after_ms": 15 * 60 * 1000,
            "subscription_reset_after_ms": 5 * 60 * 1000,
            "reset_time_known": False,
            "max_transient_retries": 2,
            "probe_order": "canonical",
            "stop_after_subscription_exhaustion": "BLOCKED",
        },
    }


class PolicyError(ValueError):
    """Raised when a policy document is invalid under cmo.route-policy/v2."""


def validate_policy(doc: Any) -> dict[str, Any]:
    """Validate a policy document; raise PolicyError on any violation."""
    if not isinstance(doc, dict):
        raise PolicyError("policy must be a JSON object")
    if set(doc) < REQUIRED_TOP_KEYS:
        missing = sorted(REQUIRED_TOP_KEYS - set(doc))
        raise PolicyError(f"missing required keys: {missing}")
    if doc["schema"] != POLICY_SCHEMA:
        raise PolicyError(f"schema must be {POLICY_SCHEMA!r}, got {doc['schema']!r}")

    forbidden = FORBIDDEN_POLICY_KEYS & set(doc)
    if forbidden:
        raise PolicyError(f"PAYG-bearing keys are forbidden by schema: {sorted(forbidden)}")

    routes = doc["routes"]
    if not isinstance(routes, list) or not routes:
        raise PolicyError("routes must be a non-empty list")
    for i, route in enumerate(routes):
        if not isinstance(route, dict):
            raise PolicyError(f"route[{i}] must be an object")
        if set(route) - {"model", "tier", "accounts"}:
            raise PolicyError(f"route[{i}] has unexpected keys: {sorted(set(route) - {'model', 'tier', 'accounts'})}")
        if not isinstance(route.get("model"), str) or not route["model"]:
            raise PolicyError(f"route[{i}].model must be a non-empty string")
        if route.get("tier") not in ALLOWED_TIERS:
            raise PolicyError(f"route[{i}].tier must be one of {ALLOWED_TIERS} (PAYG is impossible by schema)")
        accounts = route.get("accounts")
        if not isinstance(accounts, list) or not accounts or not all(isinstance(a, str) and a for a in accounts):
            raise PolicyError(f"route[{i}].accounts must be a non-empty list of non-empty strings")
        if len(set(accounts)) != len(accounts):
            raise PolicyError(f"route[{i}].accounts must not contain duplicates")

    defaults = doc["defaults"]
    if not isinstance(defaults, dict):
        raise PolicyError("defaults must be an object")
    for key in ("free_reset_after_ms", "subscription_reset_after_ms"):
        value = defaults.get(key)
        if not isinstance(value, int) or value <= 0:
            raise PolicyError(f"defaults.{key} must be a positive integer")

    seen = set()
    for route in routes:
        if route["model"] in seen:
            raise PolicyError(f"duplicate route model: {route['model']}")
        seen.add(route["model"])

    return doc


def load_canonical_policy() -> dict[str, Any]:
    return validate_policy(canonical_policy_document())


def policy_digest(doc: dict[str, Any]) -> str:
    """Stable sha256 digest of the canonical JSON encoding (v2 digest)."""
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
