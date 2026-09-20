"""HTTP/SSE surface and loopback security tests.

Proves the dashboard API is real, that mutation is refused off-loopback and
cross-origin, and that a state change reaches an already-open SSE client inside
the documented latency budget.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import StoreCase, scan_for_secrets  # noqa: E402

from cmo import SCHEMA_NAME, SCHEMA_VERSION  # noqa: E402
from cmo.server import CmoServer, Service, serve  # noqa: E402
from cmo.snapshot import build_snapshot  # noqa: E402

LOOPBACK = "127.0.0.1"


class ServerCase(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.state_root = self.db_path.parent.parent
        self.service = Service(self.db_path, host=LOOPBACK, port=0,
                               artifact_digest="a" * 64,
                               catalog_url="http://127.0.0.1:1/unused")
        self.server = CmoServer((LOOPBACK, 0), self.service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        # addCleanup is LIFO: shutdown must run before server_close before join.
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    # -- helpers --------------------------------------------------------

    def request(self, method: str, path: str, body: object = None,
                host: str | None = None, origin: str | None = None,
                raw_body: bytes | None = None) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection(LOOPBACK, self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host or f"{LOOPBACK}:{self.port}")
            if origin is not None:
                conn.putheader("Origin", origin)
            payload = raw_body
            if payload is None and body is not None:
                payload = json.dumps(body).encode("utf-8")
                conn.putheader("Content-Type", "application/json")
            if payload is not None:
                conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders(payload)
            response = conn.getresponse()
            data = response.read()
            try:
                parsed = json.loads(data.decode("utf-8"))
            except ValueError:
                parsed = {}
            return response.status, parsed, data
        finally:
            conn.close()


class HealthAndReadTests(ServerCase):
    def test_health_reports_real_identity(self) -> None:
        status, body, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["schema"], "cmo.health/v2")
        self.assertEqual(body["state_schema"]["name"], SCHEMA_NAME)
        self.assertEqual(body["state_schema"]["version"], SCHEMA_VERSION)
        self.assertEqual(body["artifact_digest"], "a" * 64)
        self.assertIsNone(body["installed_artifact_digest"])
        self.assertEqual(len(body["policy_digest"]), 64)
        self.assertFalse(body["has_events"])

    def test_health_flags_installed_artifact_mismatch(self) -> None:
        (self.state_root / "installed.json").write_text(
            json.dumps({"artifact_digest": "b" * 64}), encoding="utf-8")
        status, body, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertFalse([c for c in body["checks"] if c["name"] == "artifact"][0]["ok"])

    def test_snapshot_and_events_and_recheck_endpoints(self) -> None:
        status, snapshot, _ = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["schema"], "cmo.snapshot/v2")
        # 9 free legs (3 models x 3 accounts) + 2 authorized subscription legs.
        self.assertEqual(len(snapshot["cells"]), 11)
        self.assertEqual(len(snapshot["free_matrix"]), 3)
        self.assertEqual({m["model"] for m in snapshot["free_matrix"]}, {
            "cline-free/muse-spark-1.3-contributor", "z-ai/glm-5.3-flash",
            "cline-free/deepseek-v4.1-flash"})
        self.assertEqual(len(snapshot["subscription"]), 2)
        self.assertEqual(snapshot["decision"]["action"], "PROBE")
        status, events, _ = self.request("GET", "/api/events?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(events["schema"], "cmo.events/v1")
        status, recheck, _ = self.request("GET", "/api/recheck")
        self.assertEqual(status, 200)
        self.assertEqual(recheck["requests"], [])

    def test_unknown_path_is_404(self) -> None:
        status, body, _ = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_static_assets_are_served_from_the_package(self) -> None:
        for path, marker, ctype in (
            ("/", b"CMO trustworthy status", "text/html"),
            ("/app.js", b"EventSource", "application/javascript"),
            ("/style.css", b":root", "text/css"),
        ):
            conn = http.client.HTTPConnection(LOOPBACK, self.port, timeout=10)
            conn.request("GET", path)
            response = conn.getresponse()
            data = response.read()
            headers = dict(response.getheaders())
            conn.close()
            self.assertEqual(response.status, 200, path)
            self.assertIn(marker, data, path)
            self.assertIn(ctype, headers.get("Content-Type", ""), path)
            self.assertEqual(headers.get("Cache-Control"), "no-store", path)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff", path)


class MutationSecurityTests(ServerCase):
    def test_non_loopback_host_is_refused(self) -> None:
        for method, path in (("POST", "/api/events"), ("POST", "/api/override"),
                             ("DELETE", "/api/override/x")):
            status, body, _ = self.request(method, path, body={"events": []},
                                           host="evil.example.com")
            self.assertEqual(status, 403, f"{method} {path}")
            self.assertIn("refused", body["error"])
        self.assertEqual(build_snapshot(self.store)["state_revision"], 0)

    def test_cross_origin_is_refused(self) -> None:
        status, _, _ = self.request("POST", "/api/events", body={"events": []},
                                    origin="https://evil.example.com")
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/events", body={"events": []},
                                    origin=f"http://{LOOPBACK}:{self.port}")
        self.assertEqual(status, 200)

    def test_oversized_body_is_refused_without_state_change(self) -> None:
        status, body, _ = self.request("POST", "/api/events",
                                       raw_body=b"{" + b" " * (1024 * 1024 + 16))
        self.assertEqual(status, 400)
        self.assertIn("too large", body["error"])
        self.assertEqual(build_snapshot(self.store)["state_revision"], 0)

    def test_invalid_json_and_invalid_event_are_rejected(self) -> None:
        status, _, _ = self.request("POST", "/api/events", raw_body=b"{not json")
        self.assertEqual(status, 400)
        status, body, _ = self.request("POST", "/api/events",
                                       body={"event_type": "route.explode"})
        self.assertEqual(status, 400)
        self.assertIn("unknown event_type", body["error"])

    def test_duplicate_conflict_returns_409(self) -> None:
        event = self.probe_ok("account-1", "cline-free/muse-spark-1.3-contributor",
                              event_id="http-fixed")
        status, _, _ = self.request("POST", "/api/events", body=event)
        self.assertEqual(status, 200)
        status, _, _ = self.request("POST", "/api/events", body=event)
        self.assertEqual(status, 200)
        status, body, _ = self.request("POST", "/api/events",
                                       body=dict(event, reason_code="capability.not_offered"))
        self.assertEqual(status, 409)
        self.assertIn("different payload", body["error"])

    def test_batch_ingest_reports_partial_outcomes_without_losing_the_batch(self) -> None:
        good = self.probe_ok("account-2", "cline-free/muse-spark-1.3-contributor",
                             event_id="batch-good")
        bad = {"event_id": "batch-bad", "event_type": "route.explode"}
        status, body, _ = self.request("POST", "/api/events", body={"events": [good, bad]})
        # 207 tells the caller the batch was only partly applied.
        self.assertEqual(status, 207)
        self.assertIs(body["ok"], False)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["failures"], [{"event": "batch-bad",
                                             "error": body["failures"][0]["error"]}])
        self.assertIn("unknown event_type", body["failures"][0]["error"])
        self.assertEqual(body["state_revision"], 1)
        snapshot = self.request("GET", "/api/snapshot")[1]
        cell = [c for c in snapshot["cells"]
                if c["route_key"].startswith("account-2|cline|cline-free/muse")][0]
        self.assertEqual(cell["state"], "AVAILABLE")

    def test_batch_ingest_of_a_bare_array_is_accepted(self) -> None:
        payload = [self.probe_ok("account-1", "z-ai/glm-5.3-flash", event_id="arr-1"),
                   self.probe_ok("account-2", "z-ai/glm-5.3-flash", event_id="arr-2")]
        status, body, _ = self.request("POST", "/api/events", body=payload)
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        self.assertEqual(len(body["results"]), 2)


class IngestAndDashboardTests(ServerCase):
    def test_ingest_moves_the_exact_route_projection(self) -> None:
        route_key = "account-2|cline|cline-free/muse-spark-1.3-contributor|free"
        before = self.request("GET", "/api/snapshot")[1]
        self.assertEqual([c for c in before["cells"] if c["route_key"] == route_key][0]["state"],
                         "UNKNOWN")
        status, body, _ = self.request("POST", "/api/events", body=self.probe_ok(
            "account-2", "cline-free/muse-spark-1.3-contributor"))
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], "cmo.ingest/v1")
        after = self.request("GET", "/api/snapshot")[1]
        cell = [c for c in after["cells"] if c["route_key"] == route_key][0]
        self.assertEqual(cell["state"], "AVAILABLE")
        self.assertEqual(cell["state_label"], "Recently verified")
        self.assertEqual(after["state_revision"], before["state_revision"] + 1)
        untouched = [c for c in after["cells"] if c["route_key"] != route_key]
        self.assertTrue(all(c["state"] == "UNKNOWN" for c in untouched))

    def test_override_endpoint_requires_a_reason_and_round_trips(self) -> None:
        payload = {"account_alias": "account-1", "provider": "cline",
                   "model": "cline-free/muse-spark-1.3-contributor", "tier": "free"}
        status, body, _ = self.request("POST", "/api/override", body=payload)
        self.assertEqual(status, 400)
        self.assertIn("reason", body["error"])
        status, _, _ = self.request("POST", "/api/override",
                                    body=dict(payload, reason="operator maintenance"))
        self.assertEqual(status, 200)
        snapshot = self.request("GET", "/api/snapshot")[1]
        cell = [c for c in snapshot["cells"]
                if c["route_key"].startswith("account-1|cline|cline-free/muse")][0]
        self.assertEqual(cell["state"], "FORCE_SKIP")
        override_id = snapshot["overrides"][0]["override_id"]
        status, _, _ = self.request("DELETE", f"/api/override/{override_id}")
        self.assertEqual(status, 200)
        cleared = self.request("GET", "/api/snapshot")[1]
        cell = [c for c in cleared["cells"]
                if c["route_key"].startswith("account-1|cline|cline-free/muse")][0]
        self.assertEqual(cell["state"], "UNKNOWN")

    def test_recheck_endpoint_records_one_pending_request(self) -> None:
        status, _, _ = self.request("POST", "/api/route/recheck", body={
            "route_key": "account-1|cline|cline-free/muse-spark-1.3-contributor|free",
            "account_alias": "account-1", "provider": "cline",
            "model": "cline-free/muse-spark-1.3-contributor", "tier": "free",
            "reason": "operator asked for a bounded probe"})
        self.assertEqual(status, 200)
        snapshot = self.request("GET", "/api/snapshot")[1]
        self.assertEqual(len(snapshot["recheck_requests"]), 1)
        self.assertEqual(snapshot["recheck_requests"][0]["status"], "pending")

    def test_catalog_failure_is_reported_honestly(self) -> None:
        status, body, _ = self.request("POST", "/api/catalog/refresh", body={})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        snapshot = self.request("GET", "/api/snapshot")[1]
        self.assertEqual(snapshot["freshness"]["catalog_status"], "error")
        self.assertEqual(snapshot["service"]["status"], "degraded")
        events = self.request("GET", "/api/events?event_type=catalog.failed")[1]
        self.assertEqual(len(events["events"]), 1)

    def test_responses_never_contain_credential_shaped_text(self) -> None:
        secret = ("auth failed token=gho_abcdefghijklmnopqrstuvwxyz0123456789 "
                  "api_key=sk-live-abcdefghijklmnopqrstuvwxyz user@example.com")
        self.request("POST", "/api/events", body=self.probe_failed(
            "account-1", "cline-free/muse-spark-1.3-contributor",
            reason="auth.required", safe_detail=secret))
        blob = b""
        for path in ("/api/health", "/api/snapshot", "/api/events", "/"):
            blob += self.request("GET", path)[2]
        blob += self.db_path.read_bytes()
        text = blob.decode("utf-8", "ignore")
        for sample in ("gho_abcdefghijklmnopqrstuvwxyz", "sk-live-abcdefghijklmnopqrstuvwxyz",
                       "user@example.com"):
            self.assertNotIn(sample, text)
        self.assertEqual(scan_for_secrets(text), [])


class StreamTests(ServerCase):
    def _open_stream(self):
        conn = http.client.HTTPConnection(LOOPBACK, self.port, timeout=15)
        conn.request("GET", "/api/stream", headers={"Host": f"{LOOPBACK}:{self.port}"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        return conn, response

    def _next_revision(self, response, deadline_s: float) -> int | None:
        end = time.time() + deadline_s
        while time.time() < end:
            line = response.fp.readline()
            if not line:
                return None
            if line.startswith(b"data:"):
                payload = json.loads(line[5:].decode("utf-8"))
                return int(payload["revision"])
        return None

    def test_stream_delivers_revision_within_budget(self) -> None:
        conn, response = self._open_stream()
        try:
            first = self._next_revision(response, 10)
            self.assertIsNotNone(first)
            started = time.time()
            status, _, _ = self.request("POST", "/api/events", body=self.probe_ok(
                "account-3", "cline-free/muse-spark-1.3-contributor"))
            self.assertEqual(status, 200)
            revision = self._next_revision(response, 10)
            elapsed = time.time() - started
            self.assertIsNotNone(revision)
            self.assertEqual(revision, first + 1)
            self.assertLessEqual(elapsed, 2.0,
                                 f"SSE revision latency {elapsed:.2f}s exceeded the 2s budget")
        finally:
            conn.close()


class BindTests(unittest.TestCase):
    def test_non_loopback_bind_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            serve("ignored.sqlite3", host="0.0.0.0", port=0)


if __name__ == "__main__":
    unittest.main()
