# CMO v2 interface contract

Cline Model Optimizer v2 is the sole routing, account and model status
authority for Agent Foundry v3. The Pi launcher consumes CMO decisions and
reports telemetry to CMO. Pi never keeps a competing route table.

Never pay-as-you-go. Only `free` and `subscription` tiers exist; the schema
rejects anything else.

## State location

| Item | Path |
| --- | --- |
| State root | `%LOCALAPPDATA%\ClineModelOptimizer` |
| SQLite state | `%LOCALAPPDATA%\ClineModelOptimizer\state\cmo-v2.sqlite3` |
| Artifact | `%LOCALAPPDATA%\ClineModelOptimizer\v2\cmo.pyz` |
| Install receipt | `%LOCALAPPDATA%\ClineModelOptimizer\installed.json` |
| Compatibility exports | `%LOCALAPPDATA%\ClineModelOptimizer\*.json` |
| Pi quota ledger projection | `%USERPROFILE%\.pi\supervisor-routing\quota-state.json` |

## HTTP API (loopback only, default `127.0.0.1:4311`)

Mutating requests must carry a loopback `Host` header and, if they send
`Origin`, a loopback origin. Everything else is refused with 403.

### `GET /api/health`

```json
{
  "schema": "cmo.health/v2",
  "ok": true,
  "version": "2.0.0",
  "state_revision": 6,
  "state_schema": {"name": "cmo.state/v2", "version": 2, "found": "2"},
  "artifact_digest": "<sha256 of the running cmo.pyz>",
  "installed_artifact_digest": "<sha256 recorded by the installer>",
  "policy_digest": "<sha256 of the canonical policy>",
  "started_at": 1789845157201,
  "uptime_ms": 1824,
  "pid": 52604,
  "has_events": true,
  "checks": [{"name": "state", "ok": true, "detail": "revision=6"}]
}
```

`artifact_digest != installed_artifact_digest` means the running service is not
the installed artifact; the dashboard shows a mismatch and `/api/health`
returns 503.

### `GET /api/snapshot[?goal_id=<id>][&strategy=<name>]`

Returns `cmo.snapshot/v2`. The fields Pi needs:

```json
{
  "schema": "cmo.snapshot/v2",
  "state_revision": 6,
  "generated_at": 1789845198811,
  "policy": {"policyVersion": "2.0.0", "owner": "cline-model-optimizer",
             "neverPayg": true, "digest": "<sha256>"},
  "freshness": {"evidence_ttl_ms": 900000, "browser_freshness_ms": 15000,
                "catalog_status": "fresh"},
  "selected_goal_id": "goal-cmo-trustworthy-20260919",
  "decision": {
    "action": "LAUNCH | PROBE | BLOCKED",
    "strategy": "free-first",
    "free_only": false,
    "selected": {"route_key": "account-1|cline|cline-free/deepseek-v4.1-flash|free",
                 "account_alias": "account-1", "provider": "cline",
                 "model": "cline-free/deepseek-v4.1-flash", "tier": "free",
                 "reason_code": "probe.ok"},
    "blocked_reason": null,
    "skipped": [{"route_key": "...", "reason_code": "quota.confirmed", "detail": "..."}]
  },
  "cells": [{"route_key": "...", "state": "AVAILABLE|QUOTA|AUTH_BLOCKED|TRANSIENT|PROBING|UNKNOWN|STALE|CAPABILITY_UNAVAILABLE|FORCE_SKIP",
             "freshness": "fresh|stale|none", "in_use": false}],
  "catalog": [{"model": "...", "capability": "available"}],
  "overrides": [], "recheck_requests": []
}
```

Semantics:

* `LAUNCH` — the selected leg has valid evidence and must be used.
* `PROBE` — no leg has valid evidence; run one bounded provider probe on
  `selected` and report the result. Never sweep all nine legs.
* `BLOCKED` — nothing is authorized right now; report and wait.

Route identity is `account_alias|provider|normalized_model|tier`. Pi must use
the exact `route_key` when reporting evidence.

### `POST /api/events`

Body is one `cmo.event/v1` object or `{"events": [ ... ]}`.

```json
{
  "event_type": "route.probe.failed",
  "occurred_at": 1789845198811,
  "source_component": "pi",
  "source_instance": "pi-account-router",
  "goal_id": "goal-...",
  "attempt_id": "att-...",
  "account_alias": "account-1",
  "provider": "cline",
  "model": "cline-free/muse-spark-1.3-contributor",
  "tier": "free",
  "reason_code": "quota.confirmed",
  "reset_after_ms": 900000,
  "reset_known": 1,
  "safe_detail": "provider reported inference cap"
}
```

Response: `{"schema": "cmo.ingest/v1", "result": {...}, "state_revision": 7}`.
`duplicate: true` means the event id was already applied; ingestion is
idempotent, so retries are safe.

Event types: `goal.started`, `goal.resumed`, `goal.paused`, `goal.blocked`,
`goal.cancelled`, `goal.disconnected`, `goal.completed`, `goal.failed`,
`goal.context_escalated`, `route.probe.started`, `route.probe.succeeded`,
`route.probe.failed`, `attempt.started`, `attempt.heartbeat`,
`attempt.completed`, `attempt.failed`, `catalog.refreshed`, `catalog.failed`,
`route.recheck.requested`, `override.created`, `override.cleared`,
`override.expired`, `service.heartbeat`.

Reason codes: `quota.confirmed`, `quota.reset_unknown`, `auth.required`,
`transient.failure`, `capability.not_offered`, `context.exhausted`,
`manual.override`, `probe.ok`, `unknown`.

Free-text details are scrubbed (JWTs, `key=value` secrets and long blobs are
redacted, e-mails masked). Never send credentials, tokens or raw provider
bodies.

### `GET /api/events?limit=&event_type=&source=&since_seq=`

`{"schema": "cmo.events/v1", "events": [...], "state_revision": 6}`

### `GET /api/stream`

Server-sent events. Each state change emits
`event: revision` / `data: {"revision": 7, "generated_at": ...}` within the
`defaults.sse_max_latency_ms` budget (2000 ms). Clients re-fetch the snapshot
when the revision changes.

### Bounded mutations

| Endpoint | Effect |
| --- | --- |
| `POST /api/catalog/refresh` | one bounded provider catalog read; records `catalog.refreshed` or an honest `catalog.failed` |
| `POST /api/route/recheck` | records one pending `route.recheck.requested` for a single leg |
| `POST /api/override` | `override.created`; requires account, provider, model, tier and an explicit reason |
| `DELETE /api/override/<id>` | `override.cleared` |

There is no endpoint that fabricates provider evidence, changes billing, reads
credentials or executes arbitrary commands.

## CLI

```
cmo validate-policy
cmo route decide [--goal-id ID] [--strategy NAME]
cmo event ingest --file FILE|-            # {"events":[...]} or one event
cmo snapshot [--goal-id ID] [--strategy NAME]
cmo doctor
cmo serve [--host 127.0.0.1] [--port 4311] [--artifact-digest SHA]
cmo catalog refresh
cmo recheck --account A --provider P --model M --tier free|subscription
cmo override add|remove|list
cmo exports write --out-dir DIR
cmo db rebuild                            # deterministic replay from the event log
cmo outbox drain --path FILE              # ingest a JSONL outbox, keep failures
cmo migrate [--source PATH] [--dry-run]
cmo context-escalate --goal-id ID
```

Every command prints stable JSON containing `policy_digest`, `state_revision`
and the state schema. Nonzero exit means the operation failed.

`cmo outbox drain` is the durable offline path: Pi appends JSONL events to a
local outbox when the service is unreachable and drains it later. Drain is
idempotent and leaves unparsable or conflicting lines in the file.

## Compatibility exports

`cmo exports write --out-dir DIR` writes, atomically and at one revision:

| File | Consumer |
| --- | --- |
| `foundry-route-policy.json` | `tools/pi/pi-routing-policy.mjs` (schema `foundry-route-policy/v1`) |
| `model-routing.json` | legacy CMO PowerShell config readers |
| `guardian-status.json` | legacy dashboard/report readers |
| `guardian-state.json` | `CmoCore.ps1` account-minute reader |
| `free-models.json` | dynamic free-model cache reader |
| `quota-state.json` | `tools/pi/pi-quota-ledger.mjs` ledger projection |
| `cmo-compat-manifest.json` | audit manifest (set digest, revision, writer) |

Every file carries `GeneratedBy`/`GeneratedAt`/`GeneratedRevision`/
`GeneratedDigest`. Legacy and v2 writers must never run at the same time; the
installer retires the legacy guardian writer and rollback restores it.

## Installation

Named helpers (reviewed, reversible):

* `tools/install/Install-CmoV2.ps1` — verifies the artifact digest, backs up
  the previous installation, installs `v2/cmo.pyz`, updates the shortcut,
  registers the `ClineModelOptimizer-Service` logon task and disables the
  legacy guardian task.
* `tools/install/Rollback-CmoV2.ps1` — stops the v2 service, restores the
  previous shortcut/task/files and re-enables the legacy guardian.

Both are idempotent and write their actions to a JSON receipt under
`%LOCALAPPDATA%\ClineModelOptimizer\install-logs\`.
