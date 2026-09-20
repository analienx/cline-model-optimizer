"""Loopback HTTP + SSE server for the live CMO dashboard.

Binds 127.0.0.1 only. Mutating endpoints require a loopback Host header and,
when present, a loopback Origin. SSE emits real revision data events.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import SCHEMA_NAME, SCHEMA_VERSION, __version__
from .catalog import DEFAULT_CATALOG_URL, refresh_catalog
from .db import connect, get_meta
from .events import EventStore, EventValidationError, EventConflictError, ms_to_iso
from .recheck import RecheckWorker
from .policy import (ALIAS_RE, PolicyError, load_canonical_policy, policy_digest,
                     validate_policy)
from .snapshot import build_snapshot

MAX_BODY_BYTES = 1024 * 1024
SSE_POLL_S = 0.5
SSE_HEARTBEAT_S = 15


class Service:
    """Runtime identity shared by every request handler."""

    def __init__(self, db_path: str | Path, *, host: str = "127.0.0.1", port: int = 4311,
                 artifact_digest: str | None = None, catalog_url: str = DEFAULT_CATALOG_URL):
        self.db_path = Path(db_path)
        self.host = host
        self.port = port
        self.artifact_digest = artifact_digest
        self.catalog_url = catalog_url
        self.started_at = int(time.time() * 1000)
        self.pid = os.getpid()

    def state_root(self) -> Path:
        parent = self.db_path.parent
        return parent.parent if parent.name == "state" else parent

    def store(self) -> EventStore:
        return EventStore(self.db_path)

    def snapshot(self, goal_id: str | None = None, strategy: str | None = None) -> dict:
        with self.store() as store:
            snapshot = build_snapshot(store, goal_id=goal_id, strategy=strategy,
                                      artifact_digest=self.artifact_digest,
                                      started_at=self.started_at, pid=self.pid)
        snapshot["installed_artifact_digest"] = _installed_identity(
            self.state_root()).get("artifact_digest")
        return snapshot


_ASSET_CACHE: dict[str, tuple[bytes, str]] = {}


def _asset(name: str) -> tuple[bytes, str]:
    if name not in _ASSET_CACHE:
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(Path(name).suffix, "application/octet-stream")
        data = resource_files("cmo").joinpath("web").joinpath(name).read_bytes()
        _ASSET_CACHE[name] = (data, content_type)
    return _ASSET_CACHE[name]


class CmoHandler(BaseHTTPRequestHandler):
    server_version = f"cmo/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> Service:
        return self.server.service  # type: ignore[attr-defined]

    # -- plumbing -------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default noise
        pass

    def _send(self, code: int, body: bytes, content_type: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):  # pragma: no cover
            pass

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload, sort_keys=True, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _loopback_host(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]", "::1")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.hostname in ("127.0.0.1", "localhost", "::1")

    def _mutation_allowed(self) -> bool:
        return self._loopback_host() and self._origin_ok()

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        if length > MAX_BODY_BYTES:
            raise EventValidationError("request body too large")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise EventValidationError(f"invalid JSON body: {exc}") from exc

    # -- routing --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            body, ctype = _asset("index.html")
            self._send(200, body, ctype)
        elif path == "/app.js":
            body, ctype = _asset("app.js")
            self._send(200, body, ctype)
        elif path == "/style.css":
            body, ctype = _asset("style.css")
            self._send(200, body, ctype)
        elif path == "/api/snapshot":
            goal_id = (query.get("goal_id") or [None])[0]
            strategy = (query.get("strategy") or [None])[0]
            self._json(200, self.service.snapshot(goal_id, strategy))
        elif path == "/api/events":
            self._handle_events(query)
        elif path == "/api/health":
            self._handle_health()
        elif path == "/api/recheck":
            with self.service.store() as store:
                self._json(200, {"schema": "cmo.recheck/v1",
                                 "requests": store.recheck_requests()})
        elif path == "/api/policy/versions":
            with self.service.store() as store:
                self._json(200, {"schema": "cmo.policy/v1",
                                 "versions": store.policy_versions(),
                                 "active": store.active_policy_doc()[1:]})
        elif path == "/api/policy/canonical":
            self._json(200, {"schema": "cmo.policy/v1",
                             "doc": load_canonical_policy()})
        elif path == "/api/stream":
            self._handle_stream()
        else:
            self._json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._mutation_allowed():
            self._json(403, {"error": "cross-origin or non-loopback mutation refused"})
            return
        try:
            body = self._read_body()
        except EventValidationError as exc:
            self._json(400, {"error": str(exc)})
            return
        try:
            if path == "/api/events":
                self._ingest(body)
            elif path == "/api/catalog/refresh":
                self._refresh_catalog()
            elif path == "/api/route/recheck":
                self._request_recheck(body)
            elif path == "/api/override":
                self._create_override(body)
            elif path == "/api/policy":
                self._save_policy(body)
            elif path == "/api/policy/rollback":
                self._rollback_policy(body)
            elif path == "/api/accounts":
                self._mutate_account(body)
            else:
                self._json(404, {"error": "not found", "path": path})
        except EventConflictError as exc:
            self._json(409, {"error": str(exc)})
        except EventValidationError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:  # pragma: no cover - defensive
            self._json(500, {"error": "internal error", "detail": type(exc).__name__})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._mutation_allowed():
            self._json(403, {"error": "cross-origin or non-loopback mutation refused"})
            return
        if path.startswith("/api/override/"):
            override_id = path.rsplit("/", 1)[-1]
            with self.service.store() as store:
                store.ingest({"event_type": "override.cleared", "attempt_id": override_id,
                              "occurred_at": int(time.time() * 1000),
                              "source_component": "ui"})
            self._json(200, {"ok": True, "cleared": override_id})
        else:
            self._json(404, {"error": "not found", "path": path})

    # -- handlers -------------------------------------------------------

    def _handle_events(self, query: dict[str, list[str]]) -> None:
        def first(key: str) -> str | None:
            return (query.get(key) or [None])[0]

        limit = int(first("limit") or 200)
        limit = max(1, min(limit, 1000))
        since_seq = int(first("since_seq") or 0)
        with self.service.store() as store:
            rows = store.events(limit=limit, event_type=first("event_type"),
                                goal_id=first("goal_id"), account_alias=first("account"),
                                model=first("model"), source=first("source"),
                                outcome=first("outcome"), since_seq=since_seq)
            self._json(200, {"schema": "cmo.events/v1", "events": rows,
                             "state_revision": store.revision()})

    def _handle_health(self) -> None:
        checks: list[dict[str, Any]] = []
        ok = True
        try:
            with self.service.store() as store:
                revision = store.revision()
                schema_version = store.meta("schema_version")
                event_count = store.events(limit=1)
            checks.append({"name": "state", "ok": True, "detail": f"revision={revision}"})
        except Exception as exc:  # pragma: no cover - defensive
            ok = False
            revision = None
            schema_version = None
            event_count = []
            checks.append({"name": "state", "ok": False, "detail": type(exc).__name__})
        policy = load_canonical_policy()
        installed = _installed_identity(self.service.state_root())
        checks.append({"name": "policy", "ok": True, "detail": policy_digest(policy)[:12]})
        artifact_ok = True
        if installed.get("artifact_digest") and self.service.artifact_digest:
            artifact_ok = installed["artifact_digest"] == self.service.artifact_digest
        checks.append({"name": "artifact",
                       "ok": artifact_ok,
                       "detail": (self.service.artifact_digest or "unknown")[:12]})
        if not artifact_ok:
            ok = False
        self._json(200 if ok else 503, {
            "schema": "cmo.health/v2",
            "ok": ok,
            "service": "cmo-v2",
            "version": __version__,
            "state_schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION,
                             "found": schema_version},
            "state_revision": revision,
            "artifact_digest": self.service.artifact_digest,
            "installed_artifact_digest": installed.get("artifact_digest"),
            "policy_digest": policy_digest(policy),
            "started_at": self.service.started_at,
            "uptime_ms": int(time.time() * 1000) - self.service.started_at,
            "pid": self.service.pid,
            "has_events": bool(event_count),
            "checks": checks,
        })

    def _handle_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_revision = -1
        last_heartbeat = time.time()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                time.sleep(SSE_POLL_S)
                try:
                    conn = connect(self.service.db_path)
                    row = conn.execute(
                        "SELECT value FROM metadata WHERE key='state_revision'").fetchone()
                    revision = int(row["value"]) if row else 0
                    conn.close()
                except Exception:
                    revision = last_revision
                if revision != last_revision:
                    last_revision = revision
                    payload = json.dumps({"schema": "cmo.stream/v1", "revision": revision,
                                          "generated_at": int(time.time() * 1000)})
                    self.wfile.write(f"event: revision\ndata: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last_heartbeat = time.time()
                elif time.time() - last_heartbeat >= SSE_HEARTBEAT_S:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.time()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError,
                OSError):  # pragma: no cover - client disconnect
            pass

    def _ingest(self, body: Any) -> None:
        if body is None:
            raise EventValidationError("event body is required")
        if isinstance(body, list):
            self._ingest_batch(body)
            return
        if isinstance(body, dict) and isinstance(body.get("events"), list):
            self._ingest_batch(body["events"])
            return
        if not isinstance(body, dict):
            raise EventValidationError(
                "event body must be an object, an array, or {events: []}")
        with self.service.store() as store:
            result = store.ingest(body)
            self._json(200, {"schema": "cmo.ingest/v1", "ok": True, "result": result,
                             "state_revision": store.revision()})

    def _ingest_batch(self, events: list[Any]) -> None:
        """Apply every event; report per-event outcomes instead of losing the batch.

        A batch with any rejected event answers 207 with ``ok: false`` and the
        ``failures`` list so a caller can retry exactly those lines.
        """
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        with self.service.store() as store:
            for event in events:
                try:
                    results.append(store.ingest(event))
                except (EventValidationError, EventConflictError) as exc:
                    label = event.get("event_id") if isinstance(event, dict) else None
                    failures.append({"event": label, "error": str(exc)})
            self._json(207 if failures else 200, {
                "schema": "cmo.ingest/v1",
                "ok": not failures,
                "results": results,
                "failures": failures,
                "state_revision": store.revision(),
            })

    def _refresh_catalog(self) -> None:
        with self.service.store() as store:
            outcome = refresh_catalog(store, self.service.catalog_url)
        if outcome["ok"]:
            self._json(200, {"ok": True, "models": outcome["models"],
                             "source_url": outcome["source_url"]})
        else:
            self._json(200, {"ok": False, "error": outcome["error"],
                             "detail": outcome.get("detail", "")})

    def _request_recheck(self, body: Any) -> None:
        if not isinstance(body, dict) or not body.get("route_key"):
            raise EventValidationError("route_key is required")
        with self.service.store() as store:
            result = store.ingest({
                "event_type": "route.recheck.requested",
                "occurred_at": int(time.time() * 1000),
                "source_component": "ui",
                "route_key": None,
                "account_alias": body.get("account_alias"),
                "provider": body.get("provider"),
                "model": body.get("model"),
                "tier": body.get("tier"),
                "reason_code": "route.recheck.requested",
                "safe_detail": body.get("reason") or "bounded recheck requested from UI",
            })
            self._json(200, {"ok": True, "result": result})

    def _create_override(self, body: Any) -> None:
        if not isinstance(body, dict):
            raise EventValidationError("override body must be an object")
        for field in ("account_alias", "provider", "model", "tier"):
            if not body.get(field):
                raise EventValidationError(f"override requires {field}")
        reason = (body.get("reason") or "").strip()
        if not reason:
            raise EventValidationError("FORCE_SKIP requires an explicit reason")
        expires_in = body.get("expires_in_ms")
        expires_in = int(expires_in) if expires_in else 30 * 60 * 1000
        with self.service.store() as store:
            result = store.ingest({
                "event_type": "override.created",
                "occurred_at": int(time.time() * 1000),
                "source_component": "ui",
                "account_alias": body["account_alias"],
                "provider": body["provider"],
                "model": body["model"],
                "tier": body["tier"],
                "reason_code": "manual.override",
                "reset_after_ms": expires_in,
                "safe_detail": reason,
            })
            self._json(200, {"ok": True, "result": result})

    # -- versioned policy + accounts ------------------------------------

    @staticmethod
    def _policy_diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
        """Derive account audit events from a policy edit (no mutation)."""
        old_accounts = {a.get("id"): a for a in (old.get("accounts") or [])
                        if isinstance(a, dict)}
        new_accounts = {a.get("id"): a for a in (new.get("accounts") or [])
                        if isinstance(a, dict)}
        now = int(time.time() * 1000)
        audits: list[dict[str, Any]] = []
        for alias in sorted(set(new_accounts) - set(old_accounts)):
            entry = new_accounts[alias]
            audits.append({
                "event_type": "account.added", "occurred_at": now,
                "source_component": "ui", "account_alias": alias,
                "reason_code": "account.added",
                "safe_detail": (f"account {alias} registered "
                                  f"(priority {entry.get('priority', '?')}, "
                                  f"{'enabled' if entry.get('enabled', True) else 'disabled'})"),
            })
        for alias in sorted(set(old_accounts) - set(new_accounts)):
            audits.append({
                "event_type": "account.removed", "occurred_at": now,
                "source_component": "ui", "account_alias": alias,
                "reason_code": "account.removed",
                "safe_detail": f"account {alias} removed from policy",
            })
        for alias in sorted(set(old_accounts) & set(new_accounts)):
            before, after = old_accounts[alias], new_accounts[alias]
            changes = [k for k in ("enabled", "priority", "status", "label", "profile")
                       if before.get(k) != after.get(k)]
            if changes:
                audits.append({
                    "event_type": "account.updated", "occurred_at": now,
                    "source_component": "ui", "account_alias": alias,
                    "reason_code": "account.updated",
                    "safe_detail": f"account {alias} changed: {', '.join(changes)}",
                })
        return audits

    def _catalog_gate(self, store: EventStore, doc: dict[str, Any],
                      current: dict[str, Any]) -> tuple[bool, list[str]]:
        """Catalog gate for newly introduced models.

        Returns ``(fresh, unverified_models)``. A model absent from a *fresh*
        catalog raises ``EventValidationError``; when the catalog is unknown,
        new models are allowed but flagged ``catalog_unverified`` — unknown
        catalog is never presented as 'unavailable'.
        """
        current_models = {r.get("model") for r in (current.get("routes") or [])}
        fresh = False
        known: set[str] = set()
        try:
            rows = store.catalog()
            seen = [r.get("catalog_seen_at") or 0 for r in rows]
            fresh = bool(rows) and max(seen + [0]) > 0
            known = {str(r.get("model")) for r in rows}
        except Exception:
            pass
        unverified = [str(r.get("model")) for r in (doc.get("routes") or [])
                      if r.get("model") not in current_models
                      and str(r.get("model")) not in known]
        if fresh:
            missing = [m for m in unverified if m]
            if missing:
                raise EventValidationError(
                    f"fresh catalog omits new model(s) {missing}; not activated")
        return fresh, unverified

    def _save_policy(self, body: Any) -> None:
        if not isinstance(body, dict) or not isinstance(body.get("doc"), dict):
            raise EventValidationError("policy save requires a doc object")
        note = str(body.get("note") or "")[:256]
        expected = body.get("expected_digest")
        try:
            candidate = validate_policy(
                json.loads(json.dumps(body["doc"])))
        except PolicyError as exc:
            raise EventValidationError(f"policy rejected: {exc}") from exc
        with self.service.store() as store:
            current, _, _ = store.active_policy_doc()
            if body.get("dry_run"):
                audits = self._policy_diff(current, candidate)
                _, unverified = self._catalog_gate(store, candidate, current)
                self._json(200, {"ok": True, "dry_run": True, "audits": audits,
                                 "catalog_unverified": unverified})
                return
            _, unverified = self._catalog_gate(store, candidate, current)
            try:
                saved = store.save_policy_version(
                    candidate, created_by="ui",
                    note=note, expected_digest=expected)
            except EventConflictError as exc:
                self._json(409, {"ok": False, "error": str(exc)})
                return
            for audit in self._policy_diff(current, candidate):
                try:
                    store.ingest(audit)
                except (EventValidationError, EventConflictError):
                    pass
            self._json(200, {"ok": True, "result": saved,
                             "catalog_unverified": unverified,
                             "state_revision": store.revision()})

    def _rollback_policy(self, body: Any) -> None:
        if not isinstance(body, dict) or body.get("version") is None:
            raise EventValidationError("policy rollback requires a version")
        try:
            version = int(body["version"])
        except (TypeError, ValueError) as exc:
            raise EventValidationError("version must be an integer") from exc
        with self.service.store() as store:
            try:
                saved = store.rollback_policy(version, created_by="ui")
            except PolicyError as exc:
                raise EventValidationError(str(exc)) from exc
            self._json(200, {"ok": True, "result": saved,
                             "state_revision": store.revision()})

    @staticmethod
    def _profile_check(alias: str, profile: str) -> dict[str, Any]:
        """Credential-safe profile verification: existence checks only.

        Never reads file contents — no tokens, auth blobs, or secrets are
        opened. Returns status plus the raw checks as UI evidence.
        """
        root = os.environ.get("PI_PROFILE_ROOT", "").strip() or str(
            Path.home() / ".pi" / "supervisor-accounts")
        profile_dir = Path(root) / profile
        agent_dir = profile_dir / "agent"
        checks = {
            "profile_dir": profile_dir.is_dir(),
            "agent_dir": agent_dir.is_dir(),
            "auth_present": (agent_dir / "auth.json").exists(),
            "models_present": (agent_dir / "models.json").exists(),
        }
        if checks["auth_present"] and checks["agent_dir"]:
            status = "verified"
        elif checks["agent_dir"]:
            status = "signin-needed"
        elif checks["profile_dir"]:
            status = "profile"
        else:
            status = "tracked"
        return {"status": status, "checks": checks,
                "profile_dir": str(profile_dir)}

    def _mutate_account(self, body: Any) -> None:
        if not isinstance(body, dict) or not body.get("op"):
            raise EventValidationError("account mutation requires an op")
        op = str(body["op"])
        if op not in ("add", "update", "move", "remove", "verify"):
            raise EventValidationError(f"unknown account op: {op}")
        alias = str(body.get("id") or body.get("alias") or "")
        if not alias or not ALIAS_RE.match(alias):
            raise EventValidationError(
                f"account id must match {ALIAS_RE.pattern}")
        with self.service.store() as store:
            current, _, _ = store.active_policy_doc()
            doc = json.loads(json.dumps(current))
            accounts = [a for a in (doc.get("accounts") or []) if isinstance(a, dict)]
            by_id = {a.get("id"): a for a in accounts}
            routes = doc.get("routes") or []
            attached = [r.get("model") for r in routes
                        if isinstance(r, dict) and alias in (r.get("accounts") or [])]
            if op == "add":
                if alias in by_id:
                    self._json(409, {"ok": False,
                                     "error": f"account {alias} already registered"})
                    return
                priorities = [a.get("priority", 0) for a in accounts
                              if isinstance(a.get("priority"), int)]
                entry: dict[str, Any] = {
                    "id": alias, "label": str(body.get("label") or alias)[:64],
                    "enabled": bool(body.get("enabled", True)),
                    "priority": body.get("priority", max(priorities + [-1]) + 1),
                    "profile": str(body.get("profile") or alias)[:64],
                    "status": "tracked",
                }
                if not isinstance(entry["priority"], int) or entry["priority"] < 0:
                    raise EventValidationError("priority must be a non-negative integer")
                siblings = {a.get("profile", a.get("id")) for a in accounts}
                if entry["profile"] in siblings:
                    raise EventValidationError(
                        f"profile {entry['profile']!r} is already mapped by another "
                        "account; each alias maps to a distinct profile")
                attach = body.get("attach_routes") or []
                if body.get("attach_all_routes"):
                    attach = [r.get("model") for r in routes if isinstance(r, dict)]
                wanted = {str(m) for m in attach}
                known_models = {str(r.get("model")) for r in routes
                                if isinstance(r, dict)}
                unknown = sorted(wanted - known_models)
                if unknown:
                    raise EventValidationError(f"unknown route model(s): {unknown}")
                accounts.append(entry)
                for route in routes:
                    if isinstance(route, dict) and route.get("model") in wanted \
                            and alias not in (route.get("accounts") or []):
                        route["accounts"] = list(route.get("accounts") or []) + [alias]
            elif op == "update":
                if alias not in by_id:
                    raise EventValidationError(f"unknown account: {alias}")
                entry = by_id[alias]
                if body.get("status") == "verified":
                    raise EventValidationError(
                        "status 'verified' is only granted by the verify op")
                for key in ("label", "profile", "status"):
                    if body.get(key) is not None:
                        entry[key] = str(body[key])[:64]
                if body.get("enabled") is not None:
                    entry["enabled"] = bool(body["enabled"])
                if body.get("priority") is not None:
                    if not isinstance(body["priority"], int) or body["priority"] < 0:
                        raise EventValidationError(
                            "priority must be a non-negative integer")
                    entry["priority"] = body["priority"]
                siblings = {a.get("profile", a.get("id")) for a in accounts
                            if a.get("id") != alias}
                if entry.get("profile", alias) in siblings:
                    raise EventValidationError(
                        f"profile {entry.get('profile')!r} is already mapped by "
                        "another account")
            elif op == "move":
                if alias not in by_id:
                    raise EventValidationError(f"unknown account: {alias}")
                if not isinstance(body.get("priority"), int) or body["priority"] < 0:
                    raise EventValidationError(
                        "move requires a non-negative integer priority")
                by_id[alias]["priority"] = body["priority"]
            elif op == "remove":
                if alias not in by_id:
                    raise EventValidationError(f"unknown account: {alias}")
                detach = bool(body.get("detach"))
                if attached and not detach and not body.get("preview"):
                    self._json(409, {"ok": False,
                                     "error": (f"account {alias} is attached to "
                                               f"route(s) {attached}; preview the impact "
                                               "then remove with detach:true"),
                                     "attached_routes": attached})
                    return
                if detach:
                    for route in routes:
                        if isinstance(route, dict) and alias in (route.get("accounts") or []):
                            route["accounts"] = [a for a in route["accounts"]
                                                 if a != alias]
                accounts = [a for a in accounts if a.get("id") != alias]
            elif op == "verify":
                if alias not in by_id:
                    raise EventValidationError(f"unknown account: {alias}")
                entry = by_id[alias]
                profile = str(entry.get("profile") or alias)
                siblings = {a.get("profile", a.get("id")) for a in accounts
                            if a.get("id") != alias}
                if profile in siblings:
                    raise EventValidationError(
                        f"profile {profile!r} is mapped by another account; "
                        "verification refused")
                check = self._profile_check(alias, profile)
                entry["status"] = check["status"]
                note = str(body.get("note") or "verified via dashboard")[:256]
                if body.get("preview"):
                    self._json(200, {"ok": True, "preview": True, "op": op,
                                     "id": alias, "check": check})
                    return
                try:
                    validate_policy(doc)
                except PolicyError as exc:
                    raise EventValidationError(f"policy rejected: {exc}") from exc
                saved = store.save_policy_version(doc, created_by="ui", note=note)
                store.ingest({
                    "event_type": "account.verified",
                    "occurred_at": int(time.time() * 1000),
                    "source_component": "ui", "account_alias": alias,
                    "reason_code": "account.verified",
                    "safe_detail": (f"account {alias} profile check: "
                                      f"{check['status']}")[:512],
                })
                self._json(200, {"ok": True, "result": saved,
                                 "check": check,
                                 "state_revision": store.revision()})
                return
            preview_doc = dict(doc)
            preview_doc["accounts"] = accounts
            if op == "remove" and body.get("preview") and attached and not detach:
                # Preview visualizes the impact *including* the detach the
                # operator must confirm; the real save still fails closed
                # without explicit detach:true.
                detached_routes = []
                for route in routes:
                    rest = dict(route) if isinstance(route, dict) else route
                    if isinstance(rest, dict) and alias in (rest.get("accounts") or []):
                        rest["accounts"] = [a for a in rest["accounts"] if a != alias]
                    detached_routes.append(rest)
                preview_doc["routes"] = detached_routes
            try:
                candidate = validate_policy(preview_doc)
            except PolicyError as exc:
                raise EventValidationError(f"policy rejected: {exc}") from exc
            if body.get("preview"):
                self._json(200, {"ok": True, "preview": True, "op": op,
                                 "id": alias,
                                 "audits": self._policy_diff(current, candidate),
                                 "attached_routes": attached,
                                 "requires_detach": bool(attached and op == "remove"
                                                           and not detach),
                                 "candidate": candidate})
                return
            saved = store.save_policy_version(
                candidate, created_by="ui",
                note=str(body.get("note") or f"account {op}: {alias}")[:256])
            for audit in self._policy_diff(current, candidate):
                try:
                    store.ingest(audit)
                except (EventValidationError, EventConflictError):
                    pass
            self._json(200, {"ok": True, "result": saved,
                             "attached_routes": attached,
                             "state_revision": store.revision()})


def _installed_identity(state_root: Path) -> dict[str, Any]:
    marker = state_root / "installed.json"
    if not marker.exists():
        return {}
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


class CmoServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: Service):
        self.service = service
        super().__init__(address, CmoHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that hangs up mid-response is normal, not a server fault."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 4311,
          artifact_digest: str | None = None,
          catalog_url: str = DEFAULT_CATALOG_URL) -> None:
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("CMO dashboard binds loopback only (spec: 127.0.0.1:4311)")
    service = Service(db_path, host=host, port=port, artifact_digest=artifact_digest,
                      catalog_url=catalog_url)
    with service.store() as store:
        store.set_meta("service_started_at", str(service.started_at))
        store.set_meta("service_pid", str(service.pid))
        if artifact_digest:
            store.set_meta("service_artifact_digest", artifact_digest)
    httpd = CmoServer((host, port), service)
    worker = RecheckWorker(db_path, catalog_url=catalog_url)
    worker.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        worker.stop()
        httpd.server_close()
