"""Bounded recheck worker: drains queued ``route.recheck.requested`` rows.

Exactly one leg per request. The worker claims the oldest pending request per
route, ingests ``route.recheck.started``, runs one bounded probe through the
configured probe executor, then ingests the settlement event
(``route.recheck.succeeded`` / ``.failed`` / ``.blocked``). Settlement events
also move route state, so a recheck can never stay ``pending`` forever: every
pass either advances a request or records why it cannot run.

Probe executor protocol (real Pi probe, ``tools/pi/pi-model-probe.mjs``):
``<probe> --provider <provider> --model <model> --agent-dir <dir>``
``--thinking off --timeout-ms <ms>`` where ``<probe>`` is ``$PI_MODEL_PROBE``
(or the legacy ``$CMO_PROBE_COMMAND`` raw split) and ``<dir>`` is
``$PI_PROFILE_ROOT/<alias>/agent`` (default ``~/.pi/supervisor-accounts``).
The probe prints a trailing JSON line
``{provider, model, status, detail, resetAfterMs}`` with ``status`` in
``healthy|quota|auth|model|error|timeout``. The mapping is total: every probe
result settles to exactly one of ``succeeded``/``failed``/``blocked`` with a
taxonomy reason code — never a synthetic success.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .events import EventStore, now_ms
from .policy import (ALIAS_RE, REASON_AUTH_PROBE, REASON_CAPABILITY,
                     REASON_QUOTA_PROBE, REASON_TRANSIENT, TIER_FREE, route_key)

RECHECK_INTERVAL_S = 5
RECHECK_TIMEOUT_S = 60
RECHECK_COOLDOWN_MS = 5 * 60 * 1000
STALE_RUNNING_MS = 2 * 60 * 1000

EXIT_OK = 0


def split_route_key(value: str) -> tuple[str, str, str, str]:
    """Split ``account|provider|model|tier``; raises ValueError when malformed."""
    parts = (value or "").split("|")
    if len(parts) != 4 or not all(part.strip() for part in parts):
        raise ValueError(f"malformed route_key: {value!r}")
    return parts[0], parts[1], parts[2], parts[3]


PROBE_STATUSES = ("healthy", "quota", "auth", "model", "error", "timeout")


def _probe_script() -> list[str] | None:
    """Resolve the probe executable: ``$PI_MODEL_PROBE`` (preferred) or the
    legacy ``$CMO_PROBE_COMMAND`` raw split. Returns None when unconfigured."""
    explicit = os.environ.get("PI_MODEL_PROBE", "").strip()
    if explicit:
        return [explicit]
    raw = os.environ.get("CMO_PROBE_COMMAND", "").strip()
    return raw.split() if raw else None


def _agent_dir(account: str) -> Path | None:
    """Profile agent dir for an account alias; None when the alias is unsafe."""
    if not ALIAS_RE.match(account or ""):
        return None
    root = os.environ.get("PI_PROFILE_ROOT", "").strip() or str(
        Path.home() / ".pi" / "supervisor-accounts")
    return Path(root) / account / "agent"


def _parse_probe_output(text: str) -> dict[str, Any] | None:
    """Parse the trailing JSON status line from probe stdout."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("status") in PROBE_STATUSES:
            return data
    return None


def _from_probe_status(data: dict[str, Any]) -> dict[str, Any]:
    """Map a probe JSON status to a settlement (total function)."""
    status = str(data.get("status") or "error")
    detail = str(data.get("detail") or status)[:300].replace("\n", " ")
    if status == "healthy":
        return {"outcome": "succeeded", "reason": "probe.ok",
                "detail": detail or "probe served a real response"}
    if status == "quota":
        reset = data.get("resetAfterMs")
        try:
            reset_ms = int(reset) if reset is not None else 0
        except (TypeError, ValueError):
            reset_ms = 0
        return {"outcome": "failed", "reason": REASON_QUOTA_PROBE,
                "detail": detail or "provider reported quota exhaustion",
                "reset_after_ms": max(0, reset_ms)}
    if status == "auth":
        return {"outcome": "failed", "reason": REASON_AUTH_PROBE,
                "detail": detail or "probe reported auth failure"}
    if status == "model":
        return {"outcome": "failed", "reason": REASON_CAPABILITY,
                "detail": detail or "probe reported unknown model"}
    return {"outcome": "failed", "reason": REASON_TRANSIENT,
            "detail": detail or f"probe reported {status}"}
def run_probe(route_key_value: str, timeout_s: int = RECHECK_TIMEOUT_S) -> dict[str, Any]:
    """Run one bounded Pi probe for a route. Never fabricates success.

    Invokes ``<probe> --provider <provider> --model <model> --agent-dir <dir>``
    ``--thinking off --timeout-ms <ms>`` and parses the trailing JSON status
    line. Returns ``{"outcome": "succeeded"|"failed"|"blocked", "reason":
    str, "detail": str}`` where ``reason`` is a taxonomy-safe code and
    ``detail`` carries the human explanation (stored in ``safe_detail``).
    """
    try:
        account, provider, model, tier = split_route_key(route_key_value)
    except ValueError as exc:
        return {"outcome": "blocked", "reason": "recheck.malformed_route",
                "detail": f"malformed route_key: {exc}"}
    script = _probe_script()
    if not script:
        return {"outcome": "blocked", "reason": "recheck.no_probe_executor",
                "detail": ("PI_MODEL_PROBE/CMO_PROBE_COMMAND is not configured; "
                           "a real Pi route probe cannot run, so the request is "
                           "parked as blocked instead of reported successful")}
    agent_dir = _agent_dir(account)
    if agent_dir is None:
        return {"outcome": "blocked", "reason": "recheck.malformed_route",
                "detail": f"account alias {account!r} is not a safe profile name"}
    command = (script + ["--provider", provider, "--model", model,
                         "--agent-dir", str(agent_dir), "--thinking", "off",
                         "--timeout-ms", str(int(timeout_s * 1000))])
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"outcome": "blocked", "reason": "recheck.timeout",
                "detail": "probe exceeded its bounded timeout; route stays parked"}
    except OSError as exc:
        return {"outcome": "blocked", "reason": "probe.executor_unavailable",
                "detail": f"probe executor could not start: {exc}"}
    if proc.returncode not in (EXIT_OK,):
        tail = (proc.stderr or proc.stdout or "")[-300:].strip().replace("\n", " ")
        return {"outcome": "blocked",
                "reason": f"probe.exit_{proc.returncode}",
                "detail": tail or f"probe exited {proc.returncode}"}
    parsed = _parse_probe_output(proc.stdout or "")
    if parsed is None:
        return {"outcome": "blocked", "reason": "probe.unparseable_output",
                "detail": ("probe exited 0 but emitted no trailing JSON status "
                           "line; refusing to treat silence as success")}
    return _from_probe_status(parsed)


def _route_identity(route_key_value: str) -> dict[str, str]:
    account, provider, model, tier = split_route_key(route_key_value)
    return {"route_key": route_key_value, "account_alias": account,
            "provider": provider, "model": model, "tier": tier}


def _settle(store: EventStore, route_key_value: str, outcome: str, reason: str,
            source: str = "cmo-worker", detail: str | None = None,
            reset_after_ms: int = 0) -> None:
    identity = _route_identity(route_key_value)
    event_type = {"succeeded": "route.recheck.succeeded",
                  "failed": "route.recheck.failed"}.get(outcome, "route.recheck.blocked")
    payload: dict[str, Any] = {
        "event_type": event_type, "occurred_at": now_ms(),
        "source_component": source, "reason_code": reason,
        "safe_detail": (detail or reason), **identity,
    }
    if outcome == "failed" and reason == REASON_QUOTA_PROBE and reset_after_ms > 0:
        payload["reset_after_ms"] = int(reset_after_ms)
    store.ingest(payload)


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
    try:
        reset_ms = int(result.get("reset_after_ms") or 0)
    except (TypeError, ValueError):
        reset_ms = 0
    # A recheck never authorizes spend: subscription legs keep their tier label
    # but the probe itself must be a free, bounded status check.
    _settle(store, route_key_value, outcome, reason, detail=detail,
            reset_after_ms=max(0, reset_ms))
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
