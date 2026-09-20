"""CMO v2 - trustworthy account/model status and live route dashboard.

Python 3.12 standard library only. CMO is the sole routing authority:
policy, evidence, SQLite projections, decisions, catalog refresh, migration,
compatibility exports, HTTP API and the web UI all live in this package.
"""

from __future__ import annotations

__version__ = "2.0.0"

# SQLite schema generation for the evidence/projection store.
SCHEMA_VERSION = 2
SCHEMA_NAME = "cmo.state/v2"

# Snapshot contract emitted by CLI, HTTP API and compatibility exports.
SNAPSHOT_SCHEMA = "cmo.snapshot/v2"

# Version of the generated compatibility policy projection consumed by the
# Pi launcher adapter (tools/pi/pi-routing-policy.mjs in analienx/config).
COMPAT_POLICY_SCHEMA = "foundry-route-policy/v1"
COMPAT_POLICY_VERSION = "2.0.0"
