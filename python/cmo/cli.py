"""CMO v2 command line interface.

Stable JSON on stdout. Nonzero exit codes on failure. Every response carries
the policy digest, state schema and state revision for auditability.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import SCHEMA_NAME, SCHEMA_VERSION, __version__
from .db import connect, ensure_schema, read_revision, set_meta
from .decision import DecisionEngine, context_escalation
from .events import (EventConflictError, EventStore, EventValidationError, now_ms,
                     rebuild)
from .policy import (PolicyError, load_canonical_policy, policy_digest,
                     route_key, validate_policy)
from .snapshot import build_snapshot


def default_state_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "ClineModelOptimizer"
    return Path.home() / ".cmo"


def default_db_path() -> Path:
    env = os.environ.get("CMO_DB")
    if env:
        return Path(env)
    return default_state_root() / "state" / "cmo-v2.sqlite3"


def default_asset_root() -> Path:
    return Path(__file__).resolve().parent / "web"


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------

def emit(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def fail(message: str, code: int = 2) -> int:
    sys.stderr.write(json.dumps({"error": message, "ok": False}) + "\n")
    return code


def envelope(store: EventStore | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "cmo.cli/v2",
        "cmo_version": __version__,
        "state_schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
    }
    policy = load_canonical_policy()
    payload["policy_digest"] = policy_digest(policy)
    if store is not None:
        payload["state_revision"] = store.revision()
        payload["state_schema"]["found"] = store.meta("schema_version")
    if extra:
        payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_validate_policy(args: argparse.Namespace) -> int:
    if args.policy:
        try:
            doc = json.loads(Path(args.policy).read_text(encoding="utf-8"))
            validate_policy(doc)
        except (OSError, ValueError, PolicyError) as exc:
            return fail(f"invalid policy: {exc}")
    else:
        doc = load_canonical_policy()
    emit(envelope(None, {"ok": True, "policy_digest": policy_digest(doc),
                         "policy_schema": doc["schema"]}))
    return 0


def cmd_route_decide(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        snapshot = build_snapshot(store, goal_id=args.goal_id, strategy=args.strategy)
        decision = snapshot["decision"]
        emit(envelope(store, {"ok": decision["action"] != "BLOCKED",
                              "decision": decision,
                              "selected_goal_id": snapshot["selected_goal_id"],
                              "evaluated_at": snapshot["freshness"]["evaluated_at"]}))
    return 0


def cmd_event_ingest(args: argparse.Namespace) -> int:
    raw_text = ""
    if args.file and args.file != "-":
        raw_text = Path(args.file).read_text(encoding="utf-8")
    else:
        raw_text = sys.stdin.read()
    if not raw_text.strip():
        return fail("no event payload supplied")
    try:
        payload = json.loads(raw_text)
    except ValueError as exc:
        return fail(f"invalid JSON: {exc}")
    events: Any
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict) and isinstance(payload.get("events"), list):
        events = payload["events"]
    else:
        events = [payload]
    with _open_store(args) as store:
        results = []
        failures = []
        for event in events:
            try:
                results.append(store.ingest(event))
            except (EventValidationError, EventConflictError) as exc:
                label = event.get("event_id") if isinstance(event, dict) else None
                failures.append({"event": label, "error": str(exc)})
        emit(envelope(store, {"ok": not failures, "results": results,
                              "failures": failures}))
    return 0 if not failures else 1


def cmd_snapshot(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        snapshot = build_snapshot(store, goal_id=args.goal_id, strategy=args.strategy)
    emit(snapshot)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []
    ok = True
    db_path = _db_path(args)
    try:
        with _open_store(args) as store:
            checks.append({"name": "state_open", "ok": True, "detail": str(db_path)})
            found = store.meta("schema_version")
            checks.append({"name": "schema_version", "ok": found is not None,
                           "detail": found})
            if found is None:
                ok = False
            revision = store.revision()
            checks.append({"name": "state_revision", "ok": True, "detail": revision})
            counts = {
                "events": store.events(limit=1)[0]["seq"] if store.events(limit=1) else 0,
                "route_state": len(store.route_state()),
                "goals": len(store.goals()),
                "attempts": len(store.attempts()),
            }
            checks.append({"name": "projection_counts", "ok": True, "detail": counts})
            snapshot = build_snapshot(store)
            checks.append({"name": "snapshot", "ok": snapshot["schema"] == "cmo.snapshot/v2",
                           "detail": snapshot["decision"]["action"]})
            payload = envelope(store, {"ok": ok, "checks": checks,
                                       "db_path": str(db_path),
                                       "service": snapshot["service"],
                                       "policy_digest": snapshot["policy"]["digest"]})
    except Exception as exc:
        payload = envelope(None, {"ok": False, "checks": checks,
                                  "error": f"{type(exc).__name__}: {exc}",
                                  "db_path": str(db_path)})
        emit(payload)
        return 1
    emit(payload)
    return 0 if ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve
    serve(_db_path(args), host=args.host, port=args.port,
          artifact_digest=args.artifact_digest, catalog_url=args.catalog_url)
    return 0


def cmd_catalog_refresh(args: argparse.Namespace) -> int:
    from .catalog import fetch_catalog
    timestamp = now_ms()
    with _open_store(args) as store:
        try:
            catalog = fetch_catalog(args.url)
            models = [{"model": model, "capability": capability}
                      for model, capability in sorted(catalog["models"].items())]
            store.ingest({"event_type": "catalog.refreshed", "occurred_at": timestamp,
                          "source_component": "cmo", "reason_code": "catalog.refreshed",
                          "safe_detail": json.dumps({"models": models})})
            emit(envelope(store, {"ok": True, "models": models,
                                  "source_url": catalog["source_url"]}))
            return 0
        except Exception as exc:
            store.ingest({"event_type": "catalog.failed", "occurred_at": timestamp,
                          "source_component": "cmo", "reason_code": "catalog.failed",
                          "safe_detail": f"{type(exc).__name__}: {exc}"})
            emit(envelope(store, {"ok": False, "error": type(exc).__name__,
                                  "detail": str(exc)[:200]}))
            return 1


def cmd_route_recheck(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        result = store.ingest({
            "event_type": "route.recheck.requested", "occurred_at": now_ms(),
            "source_component": "cli", "account_alias": args.account,
            "provider": args.provider, "model": args.model, "tier": args.tier,
            "reason_code": "route.recheck.requested",
            "safe_detail": args.reason or "bounded recheck requested from CLI",
        })
        emit(envelope(store, {"ok": True, "result": result}))
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        if args.action == "add":
            if not args.reason:
                return fail("FORCE_SKIP requires --reason")
            result = store.ingest({
                "event_type": "override.created", "occurred_at": now_ms(),
                "source_component": "cli", "account_alias": args.account,
                "provider": args.provider, "model": args.model, "tier": args.tier,
                "reason_code": "manual.override",
                "reset_after_ms": int(args.expires_in_minutes) * 60 * 1000,
                "safe_detail": args.reason,
            })
            emit(envelope(store, {"ok": True, "result": result,
                                  "overrides": store.overrides()}))
        elif args.action == "remove":
            result = store.ingest({
                "event_type": "override.cleared", "occurred_at": now_ms(),
                "source_component": "cli", "attempt_id": args.override_id,
                "safe_detail": args.override_id,
            })
            emit(envelope(store, {"ok": True, "result": result,
                                  "overrides": store.overrides()}))
        else:
            emit(envelope(store, {"ok": True, "overrides": store.overrides()}))
    return 0


def cmd_exports_write(args: argparse.Namespace) -> int:
    from .compat import write_compatibility_exports
    with _open_store(args) as store:
        written = write_compatibility_exports(store, Path(args.out_dir), now=now_ms())
        emit(envelope(store, {"ok": True, "written": written,
                              "out_dir": str(args.out_dir)}))
    return 0


def cmd_db_rebuild(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        result = rebuild(store._conn)
        emit(envelope(store, {"ok": True, "replay": result}))
    return 0


def cmd_outbox_drain(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        emit({"ok": True, "drained": 0, "pending": 0, "path": str(path)})
        return 0
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    pending: list[str] = []
    drained = 0
    duplicates = 0
    with _open_store(args) as store:
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                pending.append(line)
                continue
            try:
                result = store.ingest(event)
                if result.get("duplicate"):
                    duplicates += 1
                drained += 1
            except (EventValidationError, EventConflictError) as exc:
                pending.append(json.dumps({"error": str(exc), "line": line}))
        if pending:
            path.write_text("\n".join(pending) + "\n", encoding="utf-8")
        else:
            path.write_text("", encoding="utf-8")
        emit(envelope(store, {"ok": not pending, "drained": drained,
                              "duplicates": duplicates, "pending": len(pending),
                              "path": str(path)}))
    return 0 if not pending else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    from .migration import migrate
    with _open_store(args) as store:
        report = migrate(store, source=args.source, dry_run=args.dry_run,
                         now=now_ms())
        emit(envelope(store, {"ok": report["ok"], "report": report}))
    return 0 if report["ok"] else 1


def cmd_context_escalate(args: argparse.Namespace) -> int:
    with _open_store(args) as store:
        goal = next((g for g in store.goals() if g["goal_id"] == args.goal_id), None)
        if goal is None:
            return fail(f"unknown goal_id: {args.goal_id}")
        payload = context_escalation(goal)
        store.ingest({
            "event_type": "goal.context_escalated", "occurred_at": now_ms(),
            "source_component": "cli", "goal_id": args.goal_id,
            "reason_code": payload["reason_code"],
        })
        emit(envelope(store, {"ok": True, "escalation": payload}))
    return 0


# ---------------------------------------------------------------------------
# store / parser
# ---------------------------------------------------------------------------

class _StoreContext:
    def __init__(self, path: Path):
        self.path = path
        self.store: EventStore | None = None

    def __enter__(self) -> EventStore:
        self.store = EventStore(self.path)
        return self.store

    def __exit__(self, *exc: object) -> None:
        if self.store:
            self.store.close()


def _db_path(args: argparse.Namespace) -> Path:
    if getattr(args, "db", None):
        return Path(args.db)
    if getattr(args, "state_root", None):
        return Path(args.state_root) / "state" / "cmo-v2.sqlite3"
    return default_db_path()


def _open_store(args: argparse.Namespace) -> _StoreContext:
    return _StoreContext(_db_path(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cmo", description="CMO v2 route authority")
    parser.add_argument("--version", action="version", version=f"cmo {__version__}")

    def add_store_args(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--db", help="explicit SQLite state path")
        sub.add_argument("--state-root", help="state root (default %%LOCALAPPDATA%%/ClineModelOptimizer)")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate-policy")
    p.add_argument("--policy")
    p.set_defaults(func=cmd_validate_policy)

    p = sub.add_parser("route")
    rsub = p.add_subparsers(dest="route_command", required=True)
    d = rsub.add_parser("decide")
    add_store_args(d)
    d.add_argument("--goal-id")
    d.add_argument("--strategy")
    d.set_defaults(func=cmd_route_decide)

    p = sub.add_parser("event")
    esub = p.add_subparsers(dest="event_command", required=True)
    ing = esub.add_parser("ingest")
    add_store_args(ing)
    ing.add_argument("--file")
    ing.set_defaults(func=cmd_event_ingest)

    p = sub.add_parser("snapshot")
    add_store_args(p)
    p.add_argument("--goal-id")
    p.add_argument("--strategy")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("doctor")
    add_store_args(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve")
    add_store_args(p)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4311)
    p.add_argument("--artifact-digest")
    p.add_argument("--catalog-url", default="https://api.cline.bot/api/v1/ai/cline/recommended-models")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("catalog")
    csub = p.add_subparsers(dest="catalog_command", required=True)
    cr = csub.add_parser("refresh")
    add_store_args(cr)
    cr.add_argument("--url", default="https://api.cline.bot/api/v1/ai/cline/recommended-models")
    cr.set_defaults(func=cmd_catalog_refresh)

    p = sub.add_parser("recheck")
    add_store_args(p)
    p.add_argument("--account", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--tier", required=True, choices=["free", "subscription"])
    p.add_argument("--reason")
    p.set_defaults(func=cmd_route_recheck)

    p = sub.add_parser("override")
    osub = p.add_subparsers(dest="action", required=True)
    for action in ("add", "remove", "list"):
        o = osub.add_parser(action)
        add_store_args(o)
        if action == "add":
            o.add_argument("--account", required=True)
            o.add_argument("--provider", required=True)
            o.add_argument("--model", required=True)
            o.add_argument("--tier", required=True, choices=["free", "subscription"])
            o.add_argument("--reason", required=True)
            o.add_argument("--expires-in-minutes", type=int, default=30)
        if action == "remove":
            o.add_argument("--override-id", required=True)
        o.set_defaults(func=cmd_override)

    p = sub.add_parser("exports")
    xsub = p.add_subparsers(dest="exports_command", required=True)
    xw = xsub.add_parser("write")
    add_store_args(xw)
    xw.add_argument("--out-dir", required=True)
    xw.set_defaults(func=cmd_exports_write)

    p = sub.add_parser("db")
    dsub = p.add_subparsers(dest="db_command", required=True)
    dr = dsub.add_parser("rebuild")
    add_store_args(dr)
    dr.set_defaults(func=cmd_db_rebuild)

    p = sub.add_parser("outbox")
    usub = p.add_subparsers(dest="outbox_command", required=True)
    ud = usub.add_parser("drain")
    add_store_args(ud)
    ud.add_argument("--path", required=True)
    ud.set_defaults(func=cmd_outbox_drain)

    p = sub.add_parser("migrate")
    add_store_args(p)
    p.add_argument("--source", action="append", default=None,
                   help="legacy source file/dir (repeatable)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("context-escalate")
    add_store_args(p)
    p.add_argument("--goal-id", required=True)
    p.set_defaults(func=cmd_context_escalate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
