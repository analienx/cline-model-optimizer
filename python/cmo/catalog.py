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
