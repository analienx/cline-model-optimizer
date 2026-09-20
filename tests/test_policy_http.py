"""Policy + account HTTP surface: versioned saves, rollback, account ops."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import StoreCase  # noqa: E402

from cmo.policy import load_canonical_policy, policy_digest  # noqa: E402
from cmo.server import CmoServer, Service  # noqa: E402

import http.client
import threading

LOOPBACK = "127.0.0.1"


class PolicyServerCase(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.service = Service(self.db_path, host=LOOPBACK, port=0,
                               artifact_digest="c" * 64,
                               catalog_url="http://127.0.0.1:1/unused")
        self.server = CmoServer((LOOPBACK, 0), self.service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method: str, path: str, body: object = None):
        conn = http.client.HTTPConnection(LOOPBACK, self.port, timeout=10)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Host": f"{LOOPBACK}:{self.port}"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read()
            try:
                return response.status, json.loads(data.decode())
            except ValueError:
                return response.status, {}
        finally:
            conn.close()


class PolicyEndpointTests(PolicyServerCase):
    def test_versions_empty_then_save_then_rollback(self):
        status, body = self.call("GET", "/api/policy/versions")
        assert status == 200 and body["versions"] == []
        doc = copy.deepcopy(load_canonical_policy())
        doc["accounts"][0]["priority"] = 7
        status, saved = self.call("POST", "/api/policy",
                                  {"doc": doc, "note": "reorder a1"})
        assert status == 200 and saved["ok"] and saved["result"]["version"] == 1
        status, snap = self.call("GET", "/api/snapshot")
        assert snap["policy"]["version"] == 1 and snap["policy"]["saved"] is True
        assert snap["policy"]["accounts"][0]["priority"] == 7
        # Stale compare-and-set is a conflict, not a silent overwrite.
        status, conflict = self.call("POST", "/api/policy",
                                     {"doc": doc, "expected_digest": "0" * 64})
        assert status == 409 and not conflict["ok"]
        # Rollback to a missing version fails closed.
        status, missing = self.call("POST", "/api/policy/rollback", {"version": 99})
        assert status == 400
        # Save a second version, then roll back to v1 as v3.
        doc2 = copy.deepcopy(load_canonical_policy())
        status, _ = self.call("POST", "/api/policy", {"doc": doc2})
        assert status == 200
        status, rolled = self.call("POST", "/api/policy/rollback", {"version": 1})
        assert status == 200 and rolled["result"]["rolled_back_to"] == 1
        status, snap = self.call("GET", "/api/snapshot")
        assert snap["policy"]["version"] == 3
        assert snap["policy"]["accounts"][0]["priority"] == 7

    def test_save_rejects_invalid_policy(self):
        doc = copy.deepcopy(load_canonical_policy())
        doc["routes"] = []
        status, body = self.call("POST", "/api/policy", {"doc": doc})
        assert status == 400
        status, snap = self.call("GET", "/api/snapshot")
        assert snap["policy"]["saved"] is False

    def test_dry_run_previews_without_mutating(self):
        doc = copy.deepcopy(load_canonical_policy())
        doc["accounts"][0]["enabled"] = False
        status, body = self.call("POST", "/api/policy",
                                 {"doc": doc, "dry_run": True})
        assert status == 200 and body["dry_run"]
        assert any(a["event_type"] == "account.updated" for a in body["audits"])
        status, snap = self.call("GET", "/api/snapshot")
        assert snap["policy"]["saved"] is False


class AccountEndpointTests(PolicyServerCase):
    def test_add_preview_then_add_then_verify(self):
        import os
        import tempfile
        previous = os.environ.get("PI_PROFILE_ROOT")
        with tempfile.TemporaryDirectory(prefix="cmo-profiles-") as profiles:
            agent = Path(profiles) / "account-9" / "agent"
            agent.mkdir(parents=True)
            (agent / "auth.json").write_text("{}", encoding="utf-8")
            (agent / "models.json").write_text("{}", encoding="utf-8")
            os.environ["PI_PROFILE_ROOT"] = profiles
            try:
                preview_status, preview = self.call(
                    "POST", "/api/accounts",
                    {"op": "add", "id": "account-9", "preview": True,
                     "attach_routes": ["z-ai/glm-5.3-flash"]})
                assert preview_status == 200 and preview["preview"]
                assert preview["audits"][0]["event_type"] == "account.added"
                status, added = self.call(
                    "POST", "/api/accounts",
                    {"op": "add", "id": "account-9", "priority": 0,
                     "attach_routes": ["z-ai/glm-5.3-flash"], "note": "add a9"})
                assert status == 200 and added["ok"]
                status, snap = self.call("GET", "/api/snapshot")
                ids = [a["id"] for a in snap["policy"]["accounts"]]
                assert "account-9" in ids
                glm_cells = [c for c in snap["cells"]
                             if c["model"] == "z-ai/glm-5.3-flash"
                             and c["account_alias"] == "account-9"]
                assert len(glm_cells) == 1
                status, verified = self.call("POST", "/api/accounts",
                                             {"op": "verify", "id": "account-9"})
                assert status == 200 and verified["check"]["status"] == "verified"
                status, snap = self.call("GET", "/api/snapshot")
                entry = next(a for a in snap["policy"]["accounts"]
                             if a["id"] == "account-9")
                assert entry["status"] == "verified"
            finally:
                if previous is None:
                    os.environ.pop("PI_PROFILE_ROOT", None)
                else:
                    os.environ["PI_PROFILE_ROOT"] = previous

    def test_add_rejects_duplicate_profile(self):
        status, body = self.call("POST", "/api/accounts",
                                 {"op": "add", "id": "account-9",
                                  "profile": "account-1"})
        assert status == 400 and "distinct profile" in body["error"]

    def test_remove_requires_preview_then_detach(self):
        status, blocked = self.call("POST", "/api/accounts",
                                    {"op": "remove", "id": "account-3"})
        assert status == 409 and blocked["attached_routes"]
        status, preview = self.call("POST", "/api/accounts",
                                    {"op": "remove", "id": "account-3",
                                     "preview": True})
        assert status == 200 and preview["attached_routes"]
        status, removed = self.call("POST", "/api/accounts",
                                    {"op": "remove", "id": "account-3",
                                     "detach": True})
        assert status == 200 and removed["ok"]
        status, snap = self.call("GET", "/api/snapshot")
        assert "account-3" not in [a["id"] for a in snap["policy"]["accounts"]]
        assert all(c["account_alias"] != "account-3" for c in snap["cells"])

    def test_move_reorders_routing(self):
        status, moved = self.call("POST", "/api/accounts",
                                  {"op": "move", "id": "account-3", "priority": -1})
        assert status == 400  # fail closed on bad priority
        status, moved = self.call("POST", "/api/accounts",
                                  {"op": "move", "id": "account-3", "priority": 0})
        assert status == 200 and moved["ok"]
        status, snap = self.call("GET", "/api/snapshot")
        first_cells = [c for c in snap["cells"]
                       if c["model"] == "cline-free/muse-spark-1.3-contributor"]
        assert first_cells[0]["account_alias"] == "account-1"  # tie: id order
        decision = snap["decision"]
        assert decision["route"]["account_alias"] in ("account-1", "account-3")
