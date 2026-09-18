"""CMO v2 command-line interface.

Commands (spec 7.1):
  cmo.pyz policy validate --json
  cmo.pyz route decide --goal-id ... --strategy ... --json
  cmo.pyz event ingest --stdin
  cmo.pyz snapshot --json
  cmo.pyz doctor --json
  cmo.pyz migrate legacy --dry-run|--execute
  cmo.pyz serve --bind 127.0.0.1 --port 4311
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .decision import DecisionEngine, ROUTE_STATES
from .events import EventStore
from .policy import PolicyError, load_canonical_policy, policy_digest
from .projections import PROJECTION_SQL

DEFAULT_DB = Path.home() / ".cmo" / "cmo-v2.sqlite3"


def _json(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)


def _open_store(db_path: str | None) -> EventStore:
    store = EventStore(db_path or DEFAULT_DB)
    store._conn.executescript(PROJECTION_SQL)
    return store


def cmd_policy_validate(_args) -> int:
    try:
        doc = load_canonical_policy()
    except PolicyError as exc:
        print(_json({"ok": False, "schema": "cmo.route-policy/v2", "error": str(exc)}))
        return 2
    print(_json({"ok": True, "schema": doc["schema"], "digest": policy_digest(doc),
                 "routes": doc["routes"]}))
    return 0


def cmd_route_decide(args) -> int:
    store = _open_store(args.db)
    try:
        rows = [dict(r) for r in store._conn.execute("SELECT * FROM route_state").fetchall()]
        overrides = [dict(r) for r in store._conn.execute(
            "SELECT * FROM overrides WHERE expires_at IS NULL OR expires_at > strftime('%s','now')*1000"
        ).fetchall()]
        engine = DecisionEngine()
        decision = engine.decide(rows, overrides, strategy=args.strategy or "free-first",
                                 goal_id=args.goal_id)
        print(_json(decision))
        return 0 if decision["action"] != "BLOCKED" else 1
    finally:
        store.close()


def cmd_event_ingest(args) -> int:
    store = _open_store(args.db)
    try:
        raw = json.load(sys.stdin)
        batch = raw if isinstance(raw, list) else [raw]
        inserted, skipped = 0, 0
        for item in batch:
            was_new, _ = store.ingest(item)
            inserted += 1 if was_new else 0
            skipped += 0 if was_new else 1
        print(_json({"ok": True, "ingested": inserted, "duplicates_skipped": skipped}))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(_json({"ok": False, "error": str(exc)}))
        return 2
    finally:
        store.close()


def cmd_snapshot(args) -> int:
    store = _open_store(args.db)
    try:
        conn = store._conn
        snapshot = {
            "schema": "cmo.snapshot/v1",
            "policy_digest": policy_digest(load_canonical_policy()),
            "route_state": [dict(r) for r in conn.execute(
                "SELECT * FROM route_state ORDER BY model, account_alias")],
            "goals": [dict(r) for r in conn.execute("SELECT * FROM goals ORDER BY goal_id")],
            "attempts": [dict(r) for r in conn.execute(
                "SELECT * FROM attempts ORDER BY started_at")],
            "catalog_models": [dict(r) for r in conn.execute("SELECT * FROM catalog_models")],
            "overrides": [dict(r) for r in conn.execute("SELECT * FROM overrides")],
            "recent_events": store.events(limit=50),
        }
        print(_json(snapshot))
        return 0
    finally:
        store.close()


def cmd_doctor(args) -> int:
    checks: dict[str, dict] = {}
    try:
        doc = load_canonical_policy()
        checks["policy"] = {"ok": True, "digest": policy_digest(doc)}
    except PolicyError as exc:
        checks["policy"] = {"ok": False, "error": str(exc)}

    try:
        store = _open_store(args.db)
        conn = store._conn
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        cells = conn.execute("SELECT COUNT(*) c FROM route_state").fetchone()["c"]
        checks["database"] = {"ok": mode == "wal", "journal_mode": mode, "route_cells": cells}
        cells_ok = cells == 9  # 3 free models x 3 accounts
        checks["free_matrix"] = {"ok": cells_ok, "cells": cells, "expected": 9}
        store.close()
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "error": str(exc)}

    checks["states"] = {"ok": True, "allowed": list(ROUTE_STATES)}
    ok = all(c.get("ok") for c in checks.values())
    print(_json({"ok": ok, "version": __version__, "checks": checks}))
    return 0 if ok else 1


def cmd_migrate_legacy(args) -> int:
    """Import Pi quota ledger + guardian/usage JSON as history/evidence only.

    Tokens are never imported. Manual depletion markers import as expired
    legacy annotations.
    """
    from .migration import migrate_legacy
    result = migrate_legacy(sources=args.sources or [], execute=args.execute,
                            db_path=args.db)
    print(_json(result))
    return 0 if result.get("ok") else 2


def cmd_serve(args) -> int:
    from .server import serve
    serve(bind=args.bind, port=args.port, db_path=args.db)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cmo", description="Cline Model Optimizer v2")
    parser.add_argument("--version", action="version", version=f"cmo {__version__}")
    parser.add_argument("--db", help="SQLite store path (default ~/.cmo/cmo-v2.sqlite3)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("policy")
    psub = p.add_subparsers(dest="subcommand", required=True)
    pv = psub.add_parser("validate")
    pv.add_argument("--json", action="store_true", default=True)
    pv.set_defaults(func=cmd_policy_validate)

    p = sub.add_parser("route")
    psub = p.add_subparsers(dest="subcommand", required=True)
    pd = psub.add_parser("decide")
    pd.add_argument("--goal-id")
    pd.add_argument("--strategy", choices=["free-first", "standard"])
    pd.add_argument("--json", action="store_true", default=True)
    pd.set_defaults(func=cmd_route_decide)

    p = sub.add_parser("event")
    psub = p.add_subparsers(dest="subcommand", required=True)
    pe = psub.add_parser("ingest")
    pe.add_argument("--stdin", action="store_true", default=True)
    pe.set_defaults(func=cmd_event_ingest)

    p = sub.add_parser("snapshot")
    p.add_argument("--json", action="store_true", default=True)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("doctor")
    p.add_argument("--json", action="store_true", default=True)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("migrate")
    psub = p.add_subparsers(dest="subcommand", required=True)
    pm = psub.add_parser("legacy")
    pm.add_argument("--dry-run", action="store_true")
    pm.add_argument("--execute", action="store_true")
    pm.add_argument("--sources", nargs="*", help="legacy JSON/ledger paths")
    pm.set_defaults(func=cmd_migrate_legacy)

    p = sub.add_parser("serve")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4311)
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
