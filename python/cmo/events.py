"""Event ingestion: ``cmo.event/v1`` validation, scrubbing and transactional
projection updates.

Ingest and projection update are a single transaction. Duplicate event ids
with an identical payload are a no-op; the same id with a different payload is
rejected. Late evidence stays in history but never overwrites newer state.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import (bump_revision, connect, ensure_schema, read_revision,
                 run_transaction, set_meta)
from .policy import (FAILURE_TAXONOMY, REASON_AUTH, REASON_CAPABILITY,
                     REASON_CONTEXT, REASON_QUOTA_CONFIRMED, REASON_QUOTA_UNKNOWN,
                     REASON_TRANSIENT, TIER_FREE, TIER_SUBSCRIPTION, route_key)

EVENT_SCHEMA = "cmo.event/v1"

EVENT_FIELDS = (
    "event_id", "event_type", "occurred_at", "source_component", "source_instance",
    "goal_id", "attempt_id", "strategy", "account_alias", "provider", "model",
    "tier", "reason_code", "reset_after_ms", "reset_known", "capability",
    "repo", "work_state", "free_only", "safe_detail",
)

EVENT_TYPES = {
    "goal.started", "goal.resumed", "goal.paused", "goal.blocked", "goal.cancelled",
    "goal.disconnected", "goal.completed", "goal.failed", "goal.context_escalated",
    "route.probe.started", "route.probe.succeeded", "route.probe.failed",
    "attempt.started", "attempt.heartbeat", "attempt.completed", "attempt.failed",
    "catalog.refreshed", "catalog.failed",
    "route.recheck.requested", "route.recheck.started", "route.recheck.succeeded",
    "route.recheck.failed", "route.recheck.blocked",
    "override.created", "override.cleared", "override.expired",
    "service.heartbeat",
}

GOAL_TERMINAL = {"goal.completed", "goal.failed"}
GOAL_STATUS = {
    "goal.started": "active",
    "goal.resumed": "active",
    "goal.paused": "paused",
    "goal.blocked": "blocked",
    "goal.cancelled": "cancelled",
    "goal.disconnected": "disconnected",
    "goal.completed": "completed",
    "goal.failed": "failed",
}

_ROUTE_EVENTS = {
    "route.probe.started", "route.probe.succeeded", "route.probe.failed",
    "route.recheck.started", "route.recheck.succeeded", "route.recheck.failed",
    "route.recheck.blocked",
    "attempt.started", "attempt.heartbeat", "attempt.completed", "attempt.failed",
}

STRICT_STRING = re.compile(r"^[A-Za-z0-9 ._@/:+\\\-]{0,256}$")
_EMAIL = re.compile(r"([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*(@[A-Za-z0-9.\-]+)")
_JWT = re.compile(r"[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{8,}")
_SECRET_KV = re.compile(
    r"(?i)\b(bearer|token|api[_-]?key|apikey|secret|password|passwd|authorization|"
    r"client[_-]?secret|refresh[_-]?token|access[_-]?token)\b\s*[:=]?\s*[^\s,;]+")
_LONG_BLOB = re.compile(r"[A-Za-z0-9+/_\-]{32,}")

MAX_DETAIL = 512


class EventValidationError(ValueError):
    """Raised when an inbound event violates the cmo.event/v1 contract."""


class EventConflictError(ValueError):
    """Raised when an existing event id is reused with a different payload."""


def now_ms() -> int:
    return int(time.time() * 1000)


# Sources that only ever produce synthetic evidence. Rows from these sources
# must never shape live route state; see ``is_test_db`` and
# ``migration.quarantine_fixtures``.
FIXTURE_SOURCES = frozenset({
    "browser-acceptance", "fixture", "synthetic-fixture", "goal-42-harness",
})

FIXTURE_GOAL_IDS = frozenset({"goal-42"})

# Single-character route identities are the signature of the truncated
# browser-acceptance fixture (account "a", model "m").
TRUNCATED_IDENTITIES = frozenset({"a", "m"})


def is_test_db(db_path: str | Path) -> bool:
    """True only when the database is provably an isolated test database.

    Both conditions must hold: ``CMO_TEST_MODE=isolated`` in the environment
    *and* a db path that is clearly isolated (a temp dir, or a filename
    containing ``test`` or ``isolated``). Anything else is a live database
    and fixture evidence is rejected there.
    """
    import os
    import tempfile
    if os.environ.get("CMO_TEST_MODE") != "isolated":
        return False
    text = str(db_path).lower().replace("\\", "/")
    if "test" in text or "isolated" in text:
        return True
    try:
        tmp = os.path.realpath(tempfile.gettempdir()).lower().replace("\\", "/")
        real = os.path.realpath(str(db_path)).lower().replace("\\", "/")
        if real == tmp or real.startswith(tmp.rstrip("/") + "/"):
            return True
    except OSError:
        pass
    return False


def _reject_fixture_evidence(event: dict[str, Any], db_path: str | Path) -> None:
    """Reject synthetic/fixture evidence on live databases.

    Raises :class:`EventValidationError` when a fixture source, fixture goal
    id, or truncated route identity targets a non-test database. Isolated
    test databases (see :func:`is_test_db`) still accept them so the browser
    acceptance suite keeps working against throwaway state.
    """
    if is_test_db(db_path):
        return
    source = event.get("source_component")
    if source in FIXTURE_SOURCES:
        raise EventValidationError(
            f"fixture source {source!r} is not accepted on the live evidence store")
    if event.get("goal_id") in FIXTURE_GOAL_IDS:
        raise EventValidationError(
            "fixture goal_id 'goal-42' is not accepted on the live evidence store")
    if event.get("event_type") in _ROUTE_EVENTS:
        account = event.get("account_alias") or ""
        model = event.get("model") or ""
        if account in TRUNCATED_IDENTITIES or model in TRUNCATED_IDENTITIES:
            raise EventValidationError(
                "truncated route identity is not accepted on the live evidence store")
    return int(time.time() * 1000)


def iso_to_ms(value: Any) -> int:
    if value is None:
        return now_ms()
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number * 1000) if number < 1e12 else int(number)
    text = str(value).strip()
    if not text:
        return now_ms()
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        number = float(text)
        return int(number * 1000) if number < 1e12 else int(number)
    cleaned = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise EventValidationError(f"invalid occurred_at: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def ms_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(value) / 1000.0))


def scrub_detail(value: Any) -> str | None:
    """Sanitize a free-text detail: drop secrets and mask identities."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    text = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _JWT.sub("[redacted-jwt]", text)
    text = _SECRET_KV.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _LONG_BLOB.sub("[redacted]", text)
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)
    if len(text) > MAX_DETAIL:
        text = text[: MAX_DETAIL - 3] + "..."
    return text


def _clean_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not STRICT_STRING.match(text):
        raise EventValidationError(f"{field} contains unsupported characters")
    return text


def classify_reason(reason_code: Any, detail: str | None = None) -> str:
    """Map a reason code or raw signal text to the shared taxonomy category."""
    code = (str(reason_code or "")).strip().lower()
    haystack = f"{code} {detail or ''}".lower()
    if code in (REASON_QUOTA_CONFIRMED, REASON_QUOTA_UNKNOWN):
        return "quota"
    if code in (REASON_AUTH, REASON_TRANSIENT, REASON_CAPABILITY, REASON_CONTEXT):
        return code.split(".")[0] if code.startswith("context") else {
            REASON_AUTH: "auth", REASON_TRANSIENT: "transient",
            REASON_CAPABILITY: "capability", REASON_CONTEXT: "context"}[code]
    for category, spec in FAILURE_TAXONOMY.items():
        if code in spec["reason_codes"]:
            return category
    for category, spec in FAILURE_TAXONOMY.items():
        for signal in spec["signals"]:
            if signal.lower() in haystack:
                return category
    return "unknown"


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate/normalize one inbound event; scrub anything outside the schema."""
    if not isinstance(raw, dict):
        raise EventValidationError("event must be a JSON object")
    event_type = raw.get("event_type")
    if event_type not in EVENT_TYPES:
        raise EventValidationError(f"unknown event_type: {event_type!r}")

    event: dict[str, Any] = {}
    for field in EVENT_FIELDS:
        event[field] = raw.get(field)

    event["event_id"] = _clean_string(event["event_id"], "event_id") or _new_event_id()
    event["occurred_at"] = iso_to_ms(event["occurred_at"])

    for field in ("source_component", "source_instance", "goal_id", "attempt_id",
                  "strategy", "account_alias", "provider", "model", "reason_code",
                  "capability", "repo", "work_state"):
        event[field] = _clean_string(event[field], field)

    tier = event["tier"]
    if tier is not None:
        tier = str(tier).strip().lower()
        if tier not in (TIER_FREE, TIER_SUBSCRIPTION):
            raise EventValidationError(
                f"tier must be free|subscription (PAYG impossible), got {tier!r}")
    event["tier"] = tier

    reset_after = event.get("reset_after_ms")
    if reset_after is not None:
        try:
            reset_after = int(reset_after)
        except (TypeError, ValueError) as exc:
            raise EventValidationError("reset_after_ms must be an integer or null") from exc
        if reset_after < 0:
            raise EventValidationError("reset_after_ms must not be negative")
    event["reset_after_ms"] = reset_after

    event["reset_known"] = _optional_bool(event.get("reset_known"))
    event["free_only"] = _optional_bool(event.get("free_only"))
    event["safe_detail"] = scrub_detail(event.get("safe_detail"))

    if event_type in _ROUTE_EVENTS:
        missing = [f for f in ("account_alias", "provider", "model", "tier")
                   if not event.get(f)]
        if missing:
            raise EventValidationError(
                f"{event_type} requires route identity fields: {missing}")
        event["route_key"] = route_key(event["account_alias"], event["provider"],
                                       event["model"], event["tier"])
    else:
        event["route_key"] = None
        if event.get("account_alias") and event.get("model") and event.get("provider") \
                and event.get("tier"):
            event["route_key"] = route_key(event["account_alias"], event["provider"],
                                           event["model"], event["tier"])
    return event


def _optional_bool(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return 1
    if text in ("false", "0", "no"):
        return 0
    raise EventValidationError(f"expected boolean, got {value!r}")


def _new_event_id() -> str:
    # Must be collision-proof for rapidly created events in one process:
    # time_ns()+id(object()) can repeat because CPython reuses the address of
    # an immediately collected temporary.
    return "evt-" + secrets.token_hex(12)


def payload_hash(event: dict[str, Any]) -> str:
    payload = {field: event.get(field) for field in EVENT_FIELDS}
    if payload.get("safe_detail") is None and event.get("safe_detail") is not None:
        payload["safe_detail"] = event["safe_detail"]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EventStore:
    """SQLite event store with transactional, idempotent ingestion."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn = connect(self.db_path)
        ensure_schema(self._conn)

    # -- lifecycle ------------------------------------------------------

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def __enter__(self) -> "EventStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- ingest ---------------------------------------------------------

    def ingest(self, raw: dict[str, Any]) -> dict[str, Any]:
        event = normalize_event(raw)
        _reject_fixture_evidence(event, self.db_path)
        digest = payload_hash(event)
        received = now_ms()

        outcome: dict[str, Any] = {}

        def work() -> None:
            existing = self._conn.execute(
                "SELECT payload_hash FROM events WHERE event_id=?", (event["event_id"],)
            ).fetchone()
            if existing is not None:
                if existing["payload_hash"] != digest:
                    raise EventConflictError(
                        f"event_id {event['event_id']} already exists with a different payload")
                outcome.update({"inserted": False, "duplicate": True,
                                "projection": "duplicate"})
                return

            columns = ["event_id", "event_type", "occurred_at", "received_at"] + [
                f for f in EVENT_FIELDS if f not in ("event_id", "event_type", "occurred_at")]
            values = [event["event_id"], event["event_type"], event["occurred_at"], received]
            values += [event.get(f) for f in columns[4:]]
            self._conn.execute(
                "INSERT INTO events (" + ",".join(columns) + ", payload_hash) VALUES ("
                + ",".join("?" for _ in columns) + ", ?)",
                tuple(values) + (digest,))
            projection = apply_event(self._conn, event, received)
            bump_revision(self._conn, 1)
            outcome.update({"inserted": True, "duplicate": False,
                            "projection": projection})

        run_transaction(self._conn, work)
        outcome["schema"] = EVENT_SCHEMA
        outcome["event_id"] = event["event_id"]
        outcome["revision"] = read_revision(self._conn)
        outcome["received_at"] = received
        return outcome

    def ingest_many(self, raws: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.ingest(raw) for raw in raws]

    # -- reads ----------------------------------------------------------

    def events(self, limit: int = 200, event_type: str | None = None,
               goal_id: str | None = None, account_alias: str | None = None,
               model: str | None = None, source: str | None = None,
               outcome: str | None = None, since_seq: int = 0) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events WHERE seq > ?"
        args: list[Any] = [since_seq]
        if event_type:
            sql += " AND event_type = ?"
            args.append(event_type)
        if goal_id:
            sql += " AND goal_id = ?"
            args.append(goal_id)
        if account_alias:
            sql += " AND account_alias = ?"
            args.append(account_alias)
        if model:
            sql += " AND model = ?"
            args.append(model)
        if source:
            sql += " AND source_component = ?"
            args.append(source)
        if outcome:
            sql += " AND event_type LIKE ?"
            args.append(f"%{outcome}%")
        sql += " ORDER BY occurred_at DESC, seq DESC LIMIT ?"
        args.append(limit)
        return [_event_row(r) for r in self._conn.execute(sql, tuple(args)).fetchall()]

    def route_state(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM route_state ORDER BY model, account_alias")]

    def goals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM goals ORDER BY COALESCE(last_activity_at, started_at, 0) DESC")]

    def attempts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM attempts ORDER BY COALESCE(last_heartbeat_at, started_at, 0) DESC")]

    def overrides(self, now: int | None = None) -> list[dict[str, Any]]:
        now = now_ms() if now is None else now
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM overrides WHERE expires_at IS NULL OR expires_at > ? "
            "ORDER BY created_at DESC", (now,))]

    def catalog(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM catalog_models ORDER BY model")]

    def recheck_requests(self, status: str = "pending") -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM recheck_requests WHERE status=? ORDER BY requested_at DESC",
            (status,))]

    def revision(self) -> int:
        return read_revision(self._conn)

    def meta(self, key: str) -> str | None:
        from .db import get_meta
        return get_meta(self._conn, key)

    def set_meta(self, key: str, value: str) -> None:
        set_meta(self._conn, key, value)


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["occurred_iso"] = ms_to_iso(data.get("occurred_at"))
    data["received_iso"] = ms_to_iso(data.get("received_at"))
    return data


# ---------------------------------------------------------------------------
# Projection handlers
# ---------------------------------------------------------------------------

def apply_event(conn: sqlite3.Connection, event: dict[str, Any],
                received: int) -> str:
    """Apply one normalized event to the projections. Returns the outcome."""
    etype = event["event_type"]
    if etype in GOAL_STATUS:
        return _apply_goal(conn, event, received)
    if etype == "goal.context_escalated":
        return _apply_context_escalation(conn, event, received)
    if etype in ("route.probe.started", "route.probe.succeeded"):
        return _apply_route_success(conn, event, received)
    if etype in ("route.probe.failed", "attempt.failed"):
        return _apply_route_failure(conn, event, received)
    if etype == "attempt.started":
        return _apply_attempt_started(conn, event, received)
    if etype == "attempt.heartbeat":
        return _apply_attempt_heartbeat(conn, event, received)
    if etype == "attempt.completed":
        return _apply_attempt_completed(conn, event, received)
    if etype == "catalog.refreshed":
        return _apply_catalog_refreshed(conn, event, received)
    if etype == "catalog.failed":
        set_meta(conn, "catalog_last_error_at", str(event["occurred_at"]))
        set_meta(conn, "catalog_last_error_reason", event.get("reason_code") or "catalog.failed")
        return "applied"
    if etype == "override.created":
        return _apply_override_created(conn, event)
    if etype == "route.recheck.requested":
        return _apply_recheck_requested(conn, event)
    if etype == "route.recheck.started":
        return _apply_recheck_started(conn, event)
    if etype in ("route.recheck.succeeded", "route.recheck.failed",
                 "route.recheck.blocked"):
        return _apply_recheck_settled(conn, event, received)
    if etype in ("override.cleared", "override.expired"):
        return _apply_override_cleared(conn, event)
    if etype == "service.heartbeat":
        return _apply_service_heartbeat(conn, event)
    return "noop"


def _apply_recheck_requested(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    """Queue a recheck request; duplicate pending rows for a route collapse."""
    route_key_value = event.get("route_key")
    if not route_key_value:
        return "noop"
    existing = conn.execute(
        "SELECT request_id FROM recheck_requests WHERE route_key=? AND status='pending'",
        (route_key_value,)).fetchone()
    if existing is not None:
        return "duplicate"
    conn.execute(
        "INSERT INTO recheck_requests(request_id, route_key, requested_at, requested_by, "
        "status, reason) VALUES(?,?,?,?, 'pending', ?) ON CONFLICT(request_id) DO NOTHING",
        (event["event_id"], route_key_value, event["occurred_at"],
         event.get("source_component") or "ui", event.get("safe_detail")))
    return "applied"


def _apply_recheck_started(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    route_key_value = event.get("route_key")
    if not route_key_value:
        return "noop"
    row = conn.execute(
        "SELECT request_id FROM recheck_requests WHERE route_key=? AND status='pending' "
        "ORDER BY requested_at ASC LIMIT 1", (route_key_value,)).fetchone()
    if row is None:
        return "noop"
    conn.execute(
        "UPDATE recheck_requests SET status='running', started_at=?, attempt=attempt+1 "
        "WHERE request_id=?", (event["occurred_at"], row["request_id"]))
    return "applied"


def _apply_recheck_settled(conn: sqlite3.Connection, event: dict[str, Any],
                           received: int) -> str:
    outcome = {"route.recheck.succeeded": "finished:succeeded",
               "route.recheck.failed": "finished:failed",
               "route.recheck.blocked": "finished:blocked"}[event["event_type"]]
    route_key_value = event.get("route_key")
    if not route_key_value:
        return "noop"
    row = conn.execute(
        "SELECT request_id FROM recheck_requests WHERE route_key=? AND status='running' "
        "ORDER BY started_at ASC LIMIT 1", (route_key_value,)).fetchone()
    if row is None:
        return "noop"
    conn.execute(
        "UPDATE recheck_requests SET status=?, finished_at=?, last_error=? "
        "WHERE request_id=?",
        (outcome, event["occurred_at"],
         event.get("reason_code") or event.get("safe_detail"), row["request_id"]))
    # A settled recheck is real observation: let it move route state too.
    if event["event_type"] == "route.recheck.succeeded":
        return _apply_route_success(conn, event, received)
    if event["event_type"] == "route.recheck.failed":
        return _apply_route_failure(conn, event, received)
    return "applied"


def _ensure_route_row(conn: sqlite3.Connection, event: dict[str, Any],
                     received: int) -> dict[str, Any]:
    rk = event["route_key"]
    row = conn.execute("SELECT * FROM route_state WHERE route_key=?", (rk,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO route_state(route_key, account_alias, provider, model, tier, state, "
            "state_changed_at, observed_at, received_at, source_event_id, evidence_source) "
            "VALUES(?,?,?,?,?,'UNKNOWN',?,?,?,?,?)",
            (rk, event["account_alias"], event["provider"], event["model"], event["tier"],
             event["occurred_at"], event["occurred_at"], received,
             event["event_id"], event.get("source_component")))
        row = conn.execute("SELECT * FROM route_state WHERE route_key=?", (rk,)).fetchone()
    return dict(row)


def _is_late(row: dict[str, Any], event: dict[str, Any]) -> bool:
    previous = row.get("state_changed_at") or 0
    return int(event["occurred_at"]) < int(previous)


def _apply_route_success(conn: sqlite3.Connection, event: dict[str, Any],
                         received: int) -> str:
    row = _ensure_route_row(conn, event, received)
    if _is_late(row, event):
        return "late"
    if event["event_type"] == "route.probe.started":
        state = "PROBING"
        clear_reset = False
    else:
        state = "AVAILABLE"
        clear_reset = True
    sets = ["state=?", "state_changed_at=?", "observed_at=?", "received_at=?",
            "source_event_id=?", "evidence_source=?", "last_reason_code=?",
            "transient_retries=0"]
    args: list[Any] = [state, event["occurred_at"], event["occurred_at"], received,
                       event["event_id"], event.get("source_component") or "pi",
                       event.get("reason_code")]
    if clear_reset:
        sets += ["reset_after_ms=NULL", "reset_known=NULL", "reset_at=NULL",
                 "next_check_at=NULL"]
    if event.get("capability"):
        sets.append("capability=?")
        args.append(event["capability"])
    args.append(event["route_key"])
    conn.execute(f"UPDATE route_state SET {', '.join(sets)} WHERE route_key=?", tuple(args))
    return "applied"


def _apply_route_failure(conn: sqlite3.Connection, event: dict[str, Any],
                         received: int) -> str:
    row = _ensure_route_row(conn, event, received)
    if _is_late(row, event):
        return "late"
    category = classify_reason(event.get("reason_code"), event.get("safe_detail"))
    now = int(event["occurred_at"])
    if category == "context":
        # Context exhaustion is a Goal-level transition, never route quota.
        set_meta(conn, "last_context_escalation_at", str(now))
        return "noop"
    state_map = {
        "quota": "QUOTA",
        "auth": "AUTH_BLOCKED",
        "transient": "TRANSIENT",
        "capability": "CAPABILITY_UNAVAILABLE",
        "unknown": "TRANSIENT",
    }
    state = state_map.get(category, "TRANSIENT")
    sets = ["state=?", "state_changed_at=?", "observed_at=?", "received_at=?",
            "source_event_id=?", "evidence_source=?", "last_reason_code=?"]
    args: list[Any] = [state, now, now, received, event["event_id"],
                       event.get("source_component") or "pi",
                       event.get("reason_code") or f"{category}.unknown"]
    if category == "quota":
        reset_after = event.get("reset_after_ms")
        reset_known = event.get("reset_known")
        if reset_after and reset_known:
            sets += ["reset_after_ms=?", "reset_known=1", "reset_at=?", "next_check_at=NULL"]
            args += [int(reset_after), now + int(reset_after)]
        else:
            default_reset = (15 * 60 * 1000) if event["tier"] == TIER_FREE else (5 * 60 * 1000)
            sets += ["reset_after_ms=?", "reset_known=0", "reset_at=NULL", "next_check_at=?"]
            args += [int(reset_after or default_reset), now + default_reset]
    elif category == "transient":
        retries = int(row.get("transient_retries") or 0) + 1
        sets.append("transient_retries=?")
        args.append(retries)
    args.append(event["route_key"])
    conn.execute(f"UPDATE route_state SET {', '.join(sets)} WHERE route_key=?", tuple(args))
    return "applied"


def _apply_attempt_started(conn: sqlite3.Connection, event: dict[str, Any],
                           received: int) -> str:
    attempt_id = event.get("attempt_id")
    if not attempt_id:
        return "noop"
    goal_id = event.get("goal_id")
    if goal_id:
        _ensure_goal(conn, goal_id, event)
    conn.execute(
        "INSERT INTO attempts(attempt_id, goal_id, route_key, model, provider, account_alias, "
        "tier, status, started_at, last_heartbeat_at, reason_code, session_ref) "
        "VALUES(?,?,?,?,?,?,?,'running',?,?,?,?) "
        "ON CONFLICT(attempt_id) DO UPDATE SET status='running', "
        "last_heartbeat_at=excluded.last_heartbeat_at",
        (attempt_id, goal_id, event.get("route_key"), event.get("model"),
         event.get("provider"), event.get("account_alias"), event.get("tier"),
         event["occurred_at"], event["occurred_at"], event.get("reason_code"),
         event.get("work_state")))
    if goal_id:
        conn.execute("UPDATE goals SET attempt_count=attempt_count+1, last_activity_at=?, "
                     "updated_at=? WHERE goal_id=?",
                     (event["occurred_at"], event["occurred_at"], goal_id))
    return "applied"


def _apply_attempt_heartbeat(conn: sqlite3.Connection, event: dict[str, Any],
                             received: int) -> str:
    attempt_id = event.get("attempt_id")
    if not attempt_id:
        return "noop"
    conn.execute("UPDATE attempts SET last_heartbeat_at=? WHERE attempt_id=?",
                 (event["occurred_at"], attempt_id))
    if event.get("goal_id"):
        conn.execute("UPDATE goals SET last_activity_at=?, updated_at=? WHERE goal_id=?",
                     (event["occurred_at"], event["occurred_at"], event["goal_id"]))
    return "applied"


def _apply_attempt_completed(conn: sqlite3.Connection, event: dict[str, Any],
                             received: int) -> str:
    attempt_id = event.get("attempt_id")
    if attempt_id:
        conn.execute("UPDATE attempts SET status='completed', ended_at=?, "
                     "last_heartbeat_at=? WHERE attempt_id=?",
                     (event["occurred_at"], event["occurred_at"], attempt_id))
    if event.get("goal_id"):
        conn.execute("UPDATE goals SET last_activity_at=?, updated_at=? WHERE goal_id=?",
                     (event["occurred_at"], event["occurred_at"], event["goal_id"]))
    # A verified successful execution supersedes older quota evidence.
    if event.get("route_key"):
        return _apply_route_success(conn, {**event, "event_type": "route.probe.succeeded"},
                                    received)
    return "applied"


def _ensure_goal(conn: sqlite3.Connection, goal_id: str, event: dict[str, Any]) -> None:
    row = conn.execute("SELECT goal_id FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
    if row is None:
        strategy = event.get("strategy") or "free-first"
        free_only = event.get("free_only")
        free_only = 0 if free_only is None else int(free_only)
        conn.execute(
            "INSERT INTO goals(goal_id, repo, strategy, effective_strategy, free_only, status, "
            "session_ref, started_at, updated_at, last_activity_at, reason_code) "
            "VALUES(?,?,?,?,?,'active',?,?,?,?,?)",
            (goal_id, event.get("repo"), strategy,
             "free-first" if free_only else strategy, free_only,
             event.get("work_state"), event["occurred_at"], event["occurred_at"],
             event["occurred_at"], event.get("reason_code")))


def _apply_goal(conn: sqlite3.Connection, event: dict[str, Any], received: int) -> str:
    goal_id = event.get("goal_id")
    if not goal_id:
        return "noop"
    _ensure_goal(conn, goal_id, event)
    status = GOAL_STATUS[event["event_type"]]
    sets = ["status=?", "updated_at=?", "last_activity_at=?"]
    args: list[Any] = [status, event["occurred_at"], event["occurred_at"]]
    if event.get("strategy") and status == "active":
        sets.append("strategy=?")
        args.append(event["strategy"])
        if not event.get("free_only"):
            sets.append("effective_strategy=?")
            args.append(event["strategy"])
    if event.get("free_only") is not None:
        sets.append("free_only=?")
        args.append(int(event["free_only"]))
    if event.get("reason_code"):
        sets.append("reason_code=?")
        args.append(event["reason_code"])
    if event.get("work_state"):
        sets.append("session_ref=?")
        args.append(event["work_state"])
    if event.get("repo"):
        sets.append("repo=?")
        args.append(event["repo"])
    args.append(goal_id)
    conn.execute(f"UPDATE goals SET {', '.join(sets)} WHERE goal_id=?", tuple(args))
    return "applied"


def _apply_context_escalation(conn: sqlite3.Connection, event: dict[str, Any],
                              received: int) -> str:
    goal_id = event.get("goal_id")
    if not goal_id:
        return "noop"
    _ensure_goal(conn, goal_id, event)
    row = conn.execute("SELECT free_only FROM goals WHERE goal_id=?", (goal_id,)).fetchone()
    if row is not None and int(row["free_only"] or 0) == 1:
        conn.execute("UPDATE goals SET reason_code='context.escalation_denied_free_only', "
                     "updated_at=?, last_activity_at=? WHERE goal_id=?",
                     (event["occurred_at"], event["occurred_at"], goal_id))
        return "applied"
    conn.execute("UPDATE goals SET effective_strategy='standard', "
                 "reason_code='context.escalated_to_standard', updated_at=?, last_activity_at=? "
                 "WHERE goal_id=?",
                 (event["occurred_at"], event["occurred_at"], goal_id))
    set_meta(conn, "last_context_escalation_at", str(event["occurred_at"]))
    return "applied"


def _apply_catalog_refreshed(conn: sqlite3.Connection, event: dict[str, Any],
                             received: int) -> str:
    payload = event.get("safe_detail")
    models: list[dict[str, Any]] = []
    if payload:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict) and isinstance(parsed.get("models"), list):
                models = parsed["models"]
            elif isinstance(parsed, list):
                models = parsed
        except (ValueError, TypeError):
            models = []
    seen = int(event["occurred_at"])
    for item in models:
        if not isinstance(item, dict):
            continue
        model = item.get("model")
        if not model:
            continue
        capability = item.get("capability") or "available"
        conn.execute(
            "INSERT INTO catalog_models(model, capability, catalog_seen_at, source) "
            "VALUES(?,?,?,?) ON CONFLICT(model) DO UPDATE SET capability=excluded.capability, "
            "catalog_seen_at=excluded.catalog_seen_at, source=excluded.source",
            (str(model), str(capability), seen, event.get("source_component")))
    set_meta(conn, "catalog_last_success_at", str(seen))
    set_meta(conn, "catalog_last_error_at", "")
    set_meta(conn, "catalog_last_error_reason", "")
    return "applied"


def _apply_override_created(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    model = event.get("model")
    if not model:
        return "noop"
    expires = event.get("reset_after_ms")
    expires_at = None
    if expires:
        expires_at = int(event["occurred_at"]) + int(expires)
    conn.execute(
        "INSERT INTO overrides(override_id, route_key, model, provider, account_alias, tier, "
        "kind, reason, created_at, expires_at, source_event_id) "
        "VALUES(?,?,?,?,?,?, 'FORCE_SKIP', ?, ?, ?, ?) "
        "ON CONFLICT(override_id) DO NOTHING",
        (event["event_id"], event.get("route_key"), model, event.get("provider"),
         event.get("account_alias"), event.get("tier"),
         event.get("safe_detail") or event.get("reason_code") or "manual override",
         event["occurred_at"], expires_at, event["event_id"]))
    return "applied"


def _apply_override_cleared(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    detail = event.get("safe_detail") or ""
    override_id = event.get("attempt_id") or detail
    if override_id:
        conn.execute("DELETE FROM overrides WHERE override_id=?", (override_id,))
    if event.get("route_key"):
        conn.execute("DELETE FROM overrides WHERE route_key=?", (event["route_key"],))
    return "applied"


def _apply_service_heartbeat(conn: sqlite3.Connection, event: dict[str, Any]) -> str:
    instance = event.get("source_instance") or "default"
    conn.execute(
        "INSERT INTO service_heartbeats(instance, started_at, heartbeat_at, artifact_digest, pid) "
        "VALUES(?,?,?,?,?) ON CONFLICT(instance) DO UPDATE SET heartbeat_at=excluded.heartbeat_at, "
        "artifact_digest=excluded.artifact_digest, pid=excluded.pid",
        (instance, event["occurred_at"], event["occurred_at"], event.get("capability"),
         None))
    return "applied"


def rebuild(conn: sqlite3.Connection) -> dict[str, Any]:
    """Replay the event history into deterministic projections."""
    rows = conn.execute("SELECT * FROM events ORDER BY occurred_at, seq").fetchall()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in ("goals", "attempts", "route_state", "catalog_models", "overrides",
                      "service_heartbeats", "recheck_requests"):
            conn.execute(f"DELETE FROM {table}")
        count = 0
        for row in rows:
            event = dict(row)
            event.setdefault("route_key", route_key(
                event.get("account_alias") or "", event.get("provider") or "",
                event.get("model") or "", event.get("tier") or ""))
            apply_event(conn, event, int(event.get("received_at") or event["occurred_at"]))
            count += 1
        set_meta(conn, "state_revision", str(count))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"replayed": count, "revision": count}
