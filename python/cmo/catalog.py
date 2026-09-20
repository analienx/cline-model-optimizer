"""Bounded provider catalog refresh (read-only, no credentials)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_CATALOG_URL = "https://api.cline.bot/api/v1/ai/cline/recommended-models"
CATALOG_TIMEOUT_S = 10


def fetch_catalog(url: str = DEFAULT_CATALOG_URL,
                  timeout: int = CATALOG_TIMEOUT_S) -> dict[str, Any]:
    """Fetch and normalize the provider catalog into {model: capability}."""
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "cmo/2.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    payload = json.loads(raw)
    models: dict[str, str] = {}
    buckets: dict[str, list[str]] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(value, list):
                continue
            ids: list[str] = []
            for item in value:
                if isinstance(item, str):
                    ids.append(item)
                elif isinstance(item, dict):
                    candidate = item.get("id") or item.get("model") or item.get("name")
                    if isinstance(candidate, str):
                        ids.append(candidate)
            buckets[key] = ids
            for model in ids:
                models[model] = "available"
    return {"models": models, "buckets": buckets, "source_url": url}


def refresh_catalog(store: Any, catalog_url: str = DEFAULT_CATALOG_URL) -> dict[str, Any]:
    """Fetch the provider catalog and record the outcome as evidence.

    Success ingests ``catalog.refreshed``; any failure ingests ``catalog.failed``
    and the last-good catalog rows are retained (never cleared on error).
    Returns ``{"ok": bool, ...}``; never raises.
    """
    import time
    timestamp = int(time.time() * 1000)
    try:
        catalog = fetch_catalog(catalog_url)
        models = [{"model": model, "capability": capability}
                  for model, capability in sorted(catalog["models"].items())]
        result = store.ingest({
            "event_type": "catalog.refreshed", "occurred_at": timestamp,
            "source_component": "cmo", "reason_code": "catalog.refreshed",
            "safe_detail": json.dumps({"models": models}),
        })
        return {"ok": True, "models": len(models),
                "source_url": catalog["source_url"], "result": result}
    except Exception as exc:  # network/provider failure: keep last-good catalog
        try:
            result = store.ingest({
                "event_type": "catalog.failed", "occurred_at": timestamp,
                "source_component": "cmo", "reason_code": "catalog.failed",
                "safe_detail": f"{type(exc).__name__}: {exc}",
            })
        except Exception:
            result = {}
        return {"ok": False, "error": type(exc).__name__,
                "detail": str(exc)[:200], "result": result}
