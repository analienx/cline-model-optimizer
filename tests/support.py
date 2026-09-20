"""Shared helpers for the CMO v2 test suite."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = REPO_ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from cmo.events import EventStore, now_ms  # noqa: E402

SECRET_SAMPLES = [
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    "token=gho_abcdefghijklmnopqrstuvwxyz0123456789",
    "api_key=sk-live-abcdefghijklmnopqrstuvwxyz",
    "authorization=Bearer abcdefghijklmnopqrstuvwxyz0123",
    "user@example.com",
]


class StoreCase(unittest.TestCase):
    """Base case providing a temporary, closed at teardown, event store."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cmo-test-")
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "state" / "cmo-v2.sqlite3"
        self.store = EventStore(self.db_path)
        self.addCleanup(self._cleanup_tmp)
        self.addCleanup(self.store.close)
        self.now = now_ms()

    def _cleanup_tmp(self) -> None:
        """Remove the temp tree, tolerating Windows file-lock races."""
        for _ in range(20):
            try:
                self._tmp.cleanup()
                return
            except (PermissionError, OSError):
                time.sleep(0.1)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers --------------------------------------------------------

    def event(self, event_type: str, **extra: object) -> dict:
        base = {
            "event_type": event_type,
            "occurred_at": extra.pop("occurred_at", self.now),
            "source_component": extra.pop("source_component", "pi"),
        }
        base.update(extra)
        return base

    def probe_failed(self, account: str, model: str, reason: str = "quota.confirmed",
                     provider: str = "cline", tier: str = "free",
                     **extra: object) -> dict:
        return self.event(
            "route.probe.failed", account_alias=account, provider=provider, model=model,
            tier=tier, reason_code=reason, **extra)

    def probe_ok(self, account: str, model: str, provider: str = "cline",
                 tier: str = "free", **extra: object) -> dict:
        return self.event(
            "route.probe.succeeded", account_alias=account, provider=provider, model=model,
            tier=tier, reason_code="probe.ok", **extra)

    def cell(self, snapshot: dict, route_key: str) -> dict:
        for cell in snapshot["cells"]:
            if cell["route_key"] == route_key:
                return cell
        raise AssertionError(f"route {route_key} missing from snapshot")

    def json_text(self, snapshot: dict) -> str:
        return json.dumps(snapshot, sort_keys=True, default=str)


def scan_for_secrets(text: str) -> list[str]:
    """Return the secret-shaped samples that survived into ``text``."""
    found = []
    if "eyJhbGciOi" in text or "dBjftJeZ4CVP" in text:
        found.append("jwt")
    if "gho_abcdefghijklmnopqrstuvwxyz" in text:
        found.append("github-token")
    if "sk-live-abcdefghijklmnopqrstuvwxyz" in text:
        found.append("api-key")
    if "Bearer abcdefghijklmnopqrstuvwxyz" in text:
        found.append("bearer")
    if "user@example.com" in text:
        found.append("email")
    return found


def env_for_state_root(root: Path) -> dict:
    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(root)
    return env
