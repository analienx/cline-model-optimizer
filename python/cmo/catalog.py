"""Bounded provider catalog refresh; catalog records are typed, never free text."""
from __future__ import annotations

import json
import urllib.request
from typing import Any

DEFAULT_CATALOG_URL = "https://api.cline.bot/api/v1/ai/cline/recommended-models"
CATALOG_TIMEOUT_S = 10


def fetch_catalog(url: str = DEFAULT_CATALOG_URL,
                  timeout: int = CATALOG_TIMEOUT_S) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "cmo/2.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("catalog response must be an object")
    models: dict[str, str] = {}
    buckets: dict[str, list[str]] = {}
    known = ("recommended", "free", "clinePass", "clineCloud")
    if not any(isinstance(payload.get(key), list) for key in known):
        raise ValueError("catalog response has no recognized model categories")
    for key in known:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        ids: list[str] = []
        for item in value:
            candidate = (item if isinstance(item, str) else
                         (item.get("id") or item.get("model") or item.get("name"))
                         if isinstance(item, dict) else None)
            if isinstance(candidate, str) and candidate.strip():
                ids.append(candidate)
                models[candidate] = "available"
        buckets[key] = ids
    if not models:
        raise ValueError("catalog response contained no usable model identifiers")
    return {"models": models, "buckets": buckets, "source_url": url}


def refresh_catalog(store: Any, catalog_url: str = DEFAULT_CATALOG_URL) -> dict[str, Any]:
    """Commit individual typed model evidence and one final refresh marker.

    No full provider response is put in safe_detail (which is intentionally
    capped and redacted for user-visible diagnostics). An interrupted refresh
    is reported as failure and leaves last-good rows in place.
    """
    import time
    timestamp = int(time.time() * 1000)
    try:
        catalog = fetch_catalog(catalog_url)
        models = sorted(catalog["models"].items())
        from .events import normalize_event
        records = []
        for model, capability in models:
            record = {"event_type": "catalog.refreshed", "occurred_at": timestamp,
                      "source_component": "cmo", "model": model,
                      "capability": capability, "reason_code": "catalog.model"}
            normalize_event(record)  # Validate ALL entries before writing any.
            records.append(record)
        # A complete marker must be last. It makes freshness truthful and
        # retires models absent from this successful provider response.
        records.append({"event_type": "catalog.refreshed", "occurred_at": timestamp,
                        "source_component": "cmo", "reason_code": "catalog.refreshed",
                        "safe_detail": '{"models": []}'})
        results = store.ingest_many(records)
        return {"ok": True, "models": len(models),
                "source_url": catalog["source_url"], "result": results[-1]}
    except Exception as exc:
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
