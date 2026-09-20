"""Bounded recheck worker: drains queued ``route.recheck.requested`` rows.

Exactly one leg per request. The worker claims the oldest pending request per
route, ingests ``route.recheck.started``, runs one bounded probe through the
configured probe executor, then ingests the settlement event
(``route.recheck.succeeded`` / ``.failed`` / ``.blocked``). Settlement events
also move route state, so a recheck can never stay ``pending`` forever: every
pass either advances a request or records why it cannot run.

Probe executor contract (``CMO_PROBE_COMMAND``, optional):
``<command> <route_key> <account_alias> <provider> <model> <tier>`` with a
bounded timeout. Exit 0 means the route served a real probe; exit 10 means
provider-reported quota; exit 11 means auth failure; any other exit (or a
timeout, or no command configured) settles the request as ``blocked``/``failed``
with an explicit reason — never as a synthetic success.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .events import EventStore, now_ms
from .policy import TIER_FREE, route_key

RECHECK_INTERVAL_S = 5
RECHECK_TIMEOUT_S = 60
RECHECK_COOLDOWN_MS = 5 * 60 * 1000
STALE_RUNNING_MS = 2 * 60 * 1000

EXIT_OK = 0
EXIT_QUOTA = 10
EXIT_AUTH = 11


def split_route_key(value: str) -> tuple[str, str, str, str]:
    """Split ``account|provider|model|tier``; raises ValueError when malformed."""
    parts = (value or "").split("|")
    if len(parts) != 4 or not all(part.strip() for part in parts):
        raise ValueError(f"malformed route_key: {value!r}")
    return parts[0], parts[1], parts[2], parts[3]


def _probe_command() -> list[str] | None:
    raw = os.environ.get("CMO_PROBE_COMMAND", "").strip()
    return raw.split() if raw else None


def run_probe(route_key_value: str, timeout_s: int = RECHECK_TIMEOUT_S) -> dict[str, Any]:
    """Run one bounded probe for a route. Never fabricates success.

    Returns ``{"outcome": "succeeded"|"failed"|"blocked", "reason": str,
    "detail": str}`` where ``reason`` is a taxonomy-safe code and ``detail``
    carries the human explanation (stored in ``safe_detail``).
    """
    try:
        account, provider, model, tier = split_route_key(route_key_value)
    except ValueError as exc:
        return {"outcome": "blocked", "reason": "recheck.malformed_route",
                "detail": f"malformed route_key: {exc}"}
    command = _probe_command()
    if not command:
        return {"outcome": "blocked", "reason": "recheck.no_probe_executor",
                "detail": ("CMO_PROBE_COMMAND is not configured; a real Pi route "
                           "probe cannot run, so the request is parked as blocked "
                           "instead of reported successful")}
    try:
        proc = subprocess.run(command + [route_key_value, account, provider, model, tier],
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"outcome": "failed", "reason": "recheck.timeout",
                "detail": "probe exceeded its bounded timeout"}
    except OSError as exc:
        return {"outcome": "blocked", "reason": "probe.executor_unavailable",
                "detail": f"probe executor could not start: {exc}"}
    tail = (proc.stderr or proc.stdout or "")[-300:].strip().replace("\n", " ")
    if proc.returncode == EXIT_OK:
        return {"outcome": "succeeded", "reason": "recheck.probe_succeeded"}
    if proc.returncode == EXIT_QUOTA:
        return {"outcome": "failed", "reason": "quota.probe_confirmed"}
    if proc.returncode == EXIT_AUTH:
        return {"outcome": "failed", "reason": "auth.probe_failed",
                "detail": tail or "probe reported auth failure"}
    code = f"probe.exit_{proc.returncode}"
    return {"outcome": "failed", "reason": code, "detail": tail or code}


def _route_identity(route_key_value: str) -> dict[str, str]:
    account, provider, model, tier = split_route_key(route_key_value)
    return {"route_key": route_key_value, "account_alias": account,
            "provider": provider, "model": model, "tier": tier}


def _settle(store: EventStore, route_key_value: str, outcome: str, reason: str,
            source: str = "cmo-worker", detail: str | None = None) -> None:
    identity = _route_identity(route_key_value)
    event_type = {"succeeded": "route.recheck.succeeded",
                  "failed": "route.recheck.failed"}.get(outcome, "route.recheck.blocked")
    store.ingest({
        "event_type": event_type, "occurred_at": now_ms(),
        "source_component": source, "reason_code": reason,
        "safe_detail": (detail or reason), **identity,
    })


def _cooldown_active(store: EventStore, route_key_value: str, now: int) -> bool:
    row = store._conn.execute(
        "SELECT finished_at FROM recheck_requests WHERE route_key=? "
        "AND status LIKE 'finished:%' ORDER BY finished_at DESC LIMIT 1",
        (route_key_value,)).fetchone()
    return bool(row and row["finished_at"] and now - int(row["finished_at"]) < RECHECK_COOLDOWN_MS)


def step(store: EventStore,
         probe: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Advance at most one recheck pass. Returns a summary; never raises."""
    now = now_ms()
    report: dict[str, Any] = {"claimed": 0, "settled": 0, "timed_out": 0, "skipped": 0}
    try:
        pending = store.recheck_requests(status="pending")
    except Exception as exc:
        report["error"] = f"read failed: {exc}"
        return report
    # Time out stale running rows left behind by a dead worker.
    try:
        for row in store.recheck_requests(status="running"):
            started = row.get("started_at") or row.get("requested_at") or 0
            if started and now - int(started) > STALE_RUNNING_MS:
                try:
                    _route_identity(row.get("route_key") or "")
                except ValueError:
                    continue
                store.ingest({
                    "event_type": "route.recheck.failed", "occurred_at": now,
                    "source_component": "cmo-worker", "reason_code": "recheck.timeout",
                    "safe_detail": "recheck worker pass timed out; safe to retry",
                    **_route_identity(row["route_key"]),
                })
                report["timed_out"] += 1
    except Exception as exc:
        report["error"] = f"timeout sweep failed: {exc}"
        return report
    if not pending:
        return report
    # Oldest request first; one leg per pass.
    pending.sort(key=lambda r: (r.get("requested_at") or 0))
    request = pending[0]
    route_key_value = request.get("route_key") or ""
    try:
        identity = _route_identity(route_key_value)
    except ValueError:
        store.ingest({
            "event_id": (request.get("request_id") or "") + ":malformed",
            "event_type": "route.recheck.blocked", "occurred_at": now,
            "source_component": "cmo-worker", "reason_code": "recheck.malformed_route",
            "safe_detail": f"malformed route_key {route_key_value!r}",
            "account_alias": "unknown", "provider": "unknown", "model": "unknown",
            "tier": TIER_FREE,
        })
        report["settled"] += 1
        return report
    if _cooldown_active(store, route_key_value, now):
        report["skipped"] += 1
        return report
    store.ingest({
        "event_type": "route.recheck.started", "occurred_at": now,
        "source_component": "cmo-worker", "reason_code": "route.recheck.started",
        "safe_detail": f"claimed {request.get('request_id')}", **identity,
    })
    report["claimed"] += 1
    try:
        result = (probe or run_probe)(route_key_value)
    except Exception as exc:
        result = {"outcome": "failed", "reason": f"probe.error: {type(exc).__name__}"}
    outcome = result.get("outcome") or "blocked"
    reason = str(result.get("reason") or f"recheck.{outcome}")
    detail = str(result.get("detail") or reason)
    # A recheck never authorizes spend: subscription legs keep their tier label
    # but the probe itself must be a free, bounded status check.
    _settle(store, route_key_value, outcome, reason, detail=detail)
    report["settled"] += 1
    report["outcome"] = outcome
    report["reason"] = reason
    return report


class RecheckWorker(threading.Thread):
    """Daemon thread draining the recheck queue every ``interval_s``."""

    def __init__(self, db_path: str | Path, interval_s: int = RECHECK_INTERVAL_S,
                 catalog_url: str | None = None,
                 catalog_interval_s: int = 15 * 60) -> None:
        super().__init__(name="cmo-recheck", daemon=True)
        self.db_path = Path(db_path)
        self.interval_s = interval_s
        self.catalog_url = catalog_url
        self.catalog_interval_s = catalog_interval_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - exercised via step() in tests
        from .catalog import DEFAULT_CATALOG_URL, refresh_catalog
        last_catalog = 0.0
        while not self._stop.wait(self.interval_s):
            try:
                with EventStore(self.db_path) as store:
                    step(store)
                    if self.catalog_url is not False:
                        now = time.monotonic()
                        if now - last_catalog >= self.catalog_interval_s:
                            try:
                                refresh_catalog(
                                    store, self.catalog_url or DEFAULT_CATALOG_URL)
                            except Exception:
                                pass
                            last_catalog = now
            except Exception:
                continue


def route_key_for(account: str, provider: str, model: str, tier: str) -> str:
    return route_key(account, provider, model, tier)
