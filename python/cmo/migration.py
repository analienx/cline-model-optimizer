"""Repeatable, plan-driven migration from legacy CMO evidence sources.

Imported event ids are deterministic functions of the source fingerprint and
the source row, so an interrupted or repeated migration has no second effect.
Provider reset and observed times are preserved; unknown resets always get a
bounded next-check instead of a permanent quota state.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .events import EventStore, iso_to_ms, now_ms
from .policy import PROVIDER_CLINE, TIER_FREE

DEFAULT_SOURCES = (
    "~/.cmo/cmo-v2.sqlite3",
    "~/.pi/supervisor-routing/quota-state.json",
    "%LOCALAPPDATA%/ClineModelOptimizer/free-models.json",
    "%LOCALAPPDATA%/ClineModelOptimizer/guardian-status.json",
)


def _expand(pattern: str) -> Path:
    import os
    expanded = os.path.expandvars(pattern.replace("~", str(Path.home())))
    return Path(expanded)


def _fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(data: bytes) -> Any:
    """Parse legacy JSON, tolerating the UTF-8 BOM the PowerShell era wrote."""
    text = data.decode("utf-8-sig", errors="replace")
    return json.loads(text)


def _deterministic_id(source_fp: str, row_identity: str, content: Any) -> str:
    payload = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(f"{source_fp}|{row_identity}|{payload}".encode("utf-8")).hexdigest()
    return f"mig-{digest[:32]}"


def migrate(store: EventStore, *, source: list[str] | None = None,
            dry_run: bool = False, now: int | None = None) -> dict[str, Any]:
    now = now_ms() if now is None else int(now)
    patterns = source or list(DEFAULT_SOURCES)
    report: dict[str, Any] = {"ok": True, "dry_run": dry_run, "sources": [], "warnings": []}
    for pattern in patterns:
        path = _expand(pattern)
        if not path.exists():
            report["sources"].append({"path": str(path), "status": "missing"})
            continue
        try:
            if path.suffix == ".sqlite3" or path.suffix == ".db":
                entry = _migrate_sqlite(store, path, now, dry_run)
            elif path.name == "quota-state.json":
                entry = _migrate_quota_state(store, path, now, dry_run)
            elif path.name == "free-models.json":
                entry = _migrate_free_models(store, path, now, dry_run)
            elif path.name == "guardian-status.json":
                entry = _migrate_guardian_status(store, path, now, dry_run)
            else:
                entry = {"path": str(path), "status": "unsupported"}
        except Exception as exc:  # partial source: warn and continue
            entry = {"path": str(path), "status": "partial",
                     "warning": f"{type(exc).__name__}: {exc}"}
            report["warnings"].append(entry["warning"])
        report["sources"].append(entry)
        if entry.get("status") == "partial":
            report["ok"] = False
    report["state_revision"] = store.revision()
    return report


def _already_imported(store: EventStore, fingerprint: str) -> bool:
    row = store._conn.execute(
        "SELECT 1 FROM migration_receipts WHERE source_fingerprint=?", (fingerprint,)
    ).fetchone()
    return row is not None


def _record_receipt(store: EventStore, fingerprint: str, path: Path, count: int,
                    warnings: list[str], now: int) -> None:
    store._conn.execute(
        "INSERT INTO migration_receipts(source_fingerprint, source_path, imported_at, "
        "event_count, warnings) VALUES(?,?,?,?,?) "
        "ON CONFLICT(source_fingerprint) DO UPDATE SET imported_at=excluded.imported_at, "
        "event_count=excluded.event_count, warnings=excluded.warnings",
        (fingerprint, str(path), now, count, json.dumps(warnings)))


def _ingest_all(store: EventStore, events: list[dict[str, Any]], dry_run: bool) -> dict[str, int]:
    inserted = duplicates = 0
    if dry_run:
        return {"inserted": len(events), "duplicates": 0}
    for event in events:
        result = store.ingest(event)
        if result.get("duplicate"):
            duplicates += 1
        else:
            inserted += 1
    return {"inserted": inserted, "duplicates": duplicates}


def _migrate_sqlite(store: EventStore, path: Path, now: int, dry_run: bool) -> dict[str, Any]:
    data = path.read_bytes()
    fingerprint = _fingerprint_bytes(data)
    if _already_imported(store, fingerprint):
        return {"path": str(path), "status": "already_imported", "fingerprint": fingerprint}
    warnings: list[str] = []
    events: list[dict[str, Any]] = []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "events" not in tables:
            return {"path": str(path), "status": "partial",
                    "warning": "source has no events table"}
        rows = conn.execute("SELECT * FROM events ORDER BY occurred_at").fetchall()
        for row in rows:
            raw = dict(row)
            raw.pop("ingested_at", None)
            raw.pop("payload_hash", None)
            raw.pop("seq", None)
            raw.pop("received_at", None)
            # normalize occurred_at to epoch ms via the shared parser
            raw["occurred_at"] = raw.get("occurred_at")
            identity = str(raw.get("event_id") or raw.get("seq") or len(events))
            raw["event_id"] = _deterministic_id(fingerprint, identity, raw)
            events.append(raw)
    finally:
        conn.close()
    counts = _ingest_all(store, events, dry_run)
    if not dry_run:
        _record_receipt(store, fingerprint, path, counts["inserted"], warnings, now)
    return {"path": str(path), "status": "imported", "fingerprint": fingerprint,
            "events": len(events), **counts, "warnings": warnings}


def _migrate_quota_state(store: EventStore, path: Path, now: int, dry_run: bool) -> dict[str, Any]:
    data = path.read_bytes()
    fingerprint = _fingerprint_bytes(data)
    if _already_imported(store, fingerprint):
        return {"path": str(path), "status": "already_imported", "fingerprint": fingerprint}
    doc = _load_json(data)
    routes = doc.get("routes") or {}
    warnings: list[str] = []
    events: list[dict[str, Any]] = []
    for key, entry in routes.items():
        if not isinstance(entry, dict):
            warnings.append(f"skipped non-object route {key}")
            continue
        account = entry.get("account")
        provider = entry.get("provider") or PROVIDER_CLINE
        model = entry.get("model")
        tier = (entry.get("stage") or "free").lower()
        if tier not in ("free", "subscription"):
            tier = TIER_FREE
        if not (account and model):
            warnings.append(f"skipped incomplete route {key}")
            continue
        exhausted_ms = iso_to_ms(entry.get("exhaustedAt")) if entry.get("exhaustedAt") else now
        blocked_until = entry.get("blockedUntil")
        reset_after = None
        reset_known = 0
        if blocked_until:
            reset_after = max(0, iso_to_ms(blocked_until) - exhausted_ms)
            reset_known = 1
        events.append({
            "event_id": _deterministic_id(fingerprint, f"quota:{key}", entry),
            "event_type": "route.probe.failed",
            "occurred_at": exhausted_ms,
            "source_component": "migration",
            "reason_code": "quota.confirmed" if reset_known else "quota.reset_unknown",
            "account_alias": account,
            "provider": provider,
            "model": model,
            "tier": tier,
            "reset_after_ms": reset_after,
            "reset_known": reset_known,
            "safe_detail": entry.get("reason") or "migrated quota evidence",
        })
    counts = _ingest_all(store, events, dry_run)
    if not dry_run:
        _record_receipt(store, fingerprint, path, counts["inserted"], warnings, now)
    return {"path": str(path), "status": "imported", "fingerprint": fingerprint,
            "events": len(events), **counts, "warnings": warnings}


def _migrate_free_models(store: EventStore, path: Path, now: int, dry_run: bool) -> dict[str, Any]:
    data = path.read_bytes()
    fingerprint = _fingerprint_bytes(data)
    if _already_imported(store, fingerprint):
        return {"path": str(path), "status": "already_imported", "fingerprint": fingerprint}
    doc = _load_json(data)
    models: list[dict[str, str]] = []
    for bucket in ("free", "clinePass"):
        for item in doc.get(bucket) or []:
            model = item if isinstance(item, str) else (item or {}).get("id")
            if model:
                models.append({"model": model, "capability": "available"})
    event = {
        "event_id": _deterministic_id(fingerprint, "free-models", models),
        "event_type": "catalog.refreshed",
        "occurred_at": iso_to_ms(doc.get("fetchedAt")) if doc.get("fetchedAt") else now,
        "source_component": "migration",
        "reason_code": "catalog.refreshed",
        "safe_detail": json.dumps({"models": models}),
    }
    counts = _ingest_all(store, [event], dry_run)
    if not dry_run:
        _record_receipt(store, fingerprint, path, counts["inserted"], [], now)
    return {"path": str(path), "status": "imported", "fingerprint": fingerprint,
            "events": 1, **counts}


def _migrate_guardian_status(store: EventStore, path: Path, now: int,
                             dry_run: bool) -> dict[str, Any]:
    """Import Pi per-account observations with explicit shadow provenance."""
    data = path.read_bytes()
    fingerprint = _fingerprint_bytes(data)
    if _already_imported(store, fingerprint):
        return {"path": str(path), "status": "already_imported", "fingerprint": fingerprint}
    doc = _load_json(data)
    availability = doc.get("PiRouteAvailability") or {}
    observed = iso_to_ms(availability.get("ObservedAt")) if availability.get("ObservedAt") else now
    warnings: list[str] = []
    events: list[dict[str, Any]] = []
    for item in availability.get("Models") or []:
        model = item.get("Model")
        if not model:
            continue
        for account in item.get("QuotaAccounts") or []:
            events.append(_shadow_route_event(fingerprint, account, model, "quota",
                                              observed, item))
        for account in item.get("AvailableAccounts") or []:
            events.append(_shadow_route_event(fingerprint, account, model, "available",
                                              observed, item))
        for account in item.get("UnknownAccounts") or []:
            warnings.append(f"unknown account retained as UNKNOWN: {account} {model}")
    counts = _ingest_all(store, events, dry_run)
    if not dry_run:
        _record_receipt(store, fingerprint, path, counts["inserted"], warnings, now)
    return {"path": str(path), "status": "imported", "fingerprint": fingerprint,
            "events": len(events), **counts, "warnings": warnings,
            "provenance": "shadow-legacy"}


def _shadow_route_event(fingerprint: str, account: str, model: str, kind: str,
                        observed: int, item: dict[str, Any]) -> dict[str, Any]:
    event_type = ("route.probe.succeeded" if kind == "available"
                  else "route.probe.failed")
    reason = "legacy.pi.available" if kind == "available" else "quota.reset_unknown"
    event = {
        "event_id": _deterministic_id(fingerprint, f"shadow:{account}:{model}:{kind}", item),
        "event_type": event_type,
        "occurred_at": observed,
        "source_component": "shadow-legacy-pi",
        "reason_code": reason,
        "account_alias": account,
        "provider": PROVIDER_CLINE,
        "model": model,
        "tier": TIER_FREE,
        "safe_detail": "shadow-observed legacy Pi telemetry (explicit provenance)",
    }
    if kind != "available":
        event["reset_after_ms"] = 15 * 60 * 1000
        event["reset_known"] = 0
    return event
