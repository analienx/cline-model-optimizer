"""CMO v2 legacy migration: import Pi quota ledger + guardian/usage JSON.

History/evidence only. Never imports tokens. Manual depletion markers
become expired legacy annotations. Account aliases/masked identity only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .events import EventStore
from .projections import PROJECTION_SQL


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def migrate_legacy(sources: list[str], execute: bool, db_path: str | None) -> dict[str, Any]:
    plan: list[dict[str, Any]] = []
    warnings: list[str] = []

    for src in sources:
        path = Path(src)
        if not path.exists():
            warnings.append(f"source missing: {src}")
            continue
        try:
            doc = _load_json(str(path))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"unreadable source {src}: {exc}")
            continue

        if isinstance(doc, dict) and "routes" in doc and isinstance(doc["routes"], dict):
            # Pi quota ledger shape (quota-state.json): routes -> blockedUntil etc.
            for key, entry in doc["routes"].items():
                model = entry.get("model") or key.split("|", 2)[-1]
                account = key.split("|", 1)[0] if "|" in key else "account-1"
                if entry.get("reason") == "confirmed_free_quota_exhaustion":
                    plan.append({
                        "kind": "route_evidence", "model": model, "account": account,
                        "state": "QUOTA", "observed_at": entry.get("exhaustedAt"),
                        "reset_after_ms": None,  # provider reset unknown -> labelled unknown
                    })
        elif isinstance(doc, list):
            for item in doc:
                if isinstance(item, dict) and item.get("manual_depletion"):
                    # Manual depletion markers import only as expired legacy annotations.
                    plan.append({
                        "kind": "legacy_annotation_expired", "model": item.get("model"),
                        "account": item.get("account"), "note": "legacy manual marker (expired)",
                    })
        else:
            # guardian/usage JSON: history only.
            plan.append({"kind": "history_only", "source": str(path),
                         "records": len(doc) if isinstance(doc, list) else 1})

    result: dict[str, Any] = {
        "ok": True, "mode": "execute" if execute else "dry-run",
        "planned": len(plan), "plan": plan, "warnings": warnings,
        "tokens_imported": 0,
    }
    if execute:
        store = EventStore(db_path or (Path.home() / ".cmo" / "cmo-v2.sqlite3"))
        store._conn.executescript(PROJECTION_SQL)
        inserted = 0
        for item in plan:
            if item["kind"] == "route_evidence":
                was_new, _ = store.ingest({
                    "event_type": "route.probe.failed",
                    "source_component": "cmo-migrate",
                    "account_alias": item["account"], "model": item["model"],
                    "tier": "free", "reason_code": "QUOTA",
                    "safe_detail": "imported from legacy Pi quota ledger (reset time unknown)",
                })
                inserted += 1 if was_new else 0
                store._conn.execute(
                    "INSERT INTO route_state(model, account_alias, state, state_changed_at,"
                    " last_reason_code, evidence_source) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(model, account_alias) DO UPDATE SET state=excluded.state,"
                    " last_reason_code=excluded.last_reason_code,"
                    " evidence_source=excluded.evidence_source",
                    (item["model"], item["account"], "QUOTA", 0, "QUOTA",
                     "legacy-pi-quota-ledger"))
        store.set_meta("legacy_migration", "executed")
        store.close()
        result["ingested_events"] = inserted
    return result
