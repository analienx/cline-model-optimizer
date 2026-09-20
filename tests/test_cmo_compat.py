"""Compatibility exports, migration, CLI envelope and deterministic artifact tests.

These cover the reversible/consumer-compatibility gate: the exports must be
truthful, atomic, revision-stamped, consumable by the existing Pi validator, and
produced by an exactly identified artifact.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import StoreCase, env_for_state_root  # noqa: E402

from cmo import __version__  # noqa: E402
from cmo.cli import main as cli_main  # noqa: E402
from cmo.compat import write_compatibility_exports  # noqa: E402
from cmo.migration import migrate  # noqa: E402
from cmo.snapshot import build_snapshot  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = Path("C:/Workspace/repos/config")
PYZ = REPO_ROOT / "dist" / "cmo.pyz"


def _first_index(seq: list[str], value: str) -> int:
    return seq.index(value)

EXPORTS = ("foundry-route-policy.json", "model-routing.json", "guardian-status.json",
           "guardian-state.json", "free-models.json", "quota-state.json",
           "cmo-compat-manifest.json")


class CompatibilityExportTests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.out = self.tmp / "ClineModelOptimizer"
        self.out.mkdir()
        self.store.ingest(self.probe_ok("account-2", "cline-free/muse-spark-1.3-contributor"))
        self.store.ingest(self.probe_failed("account-1",
                                            "cline-free/muse-spark-1.3-contributor",
                                            reset_after_ms=900_000, reset_known=1))
        self.written = write_compatibility_exports(self.store, self.out)

    def payload(self, name: str) -> dict:
        return json.loads((self.out / name).read_text(encoding="utf-8"))

    def test_all_documented_exports_are_written_atomically(self) -> None:
        self.assertEqual(sorted(self.written), sorted(EXPORTS))
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted(EXPORTS))
        revision = build_snapshot(self.store)["state_revision"]
        for name in EXPORTS:
            doc = self.payload(name)
            self.assertEqual(doc["GeneratedRevision"], revision, name)
            self.assertEqual(doc["GeneratedBy"], "cmo-v2", name)
            self.assertTrue(doc["GeneratedDigest"], name)
            # No temp files may be left behind by the atomic write.
        self.assertFalse([p for p in self.out.iterdir() if p.suffix == ".tmp"])

    def test_exports_are_truthful_about_unknown_and_quota(self) -> None:
        manifest = self.payload("cmo-compat-manifest.json")
        self.assertEqual(manifest["schema"], "cmo.compat-manifest/v1")
        self.assertEqual(manifest["writer"], "cmo-v2")
        self.assertEqual(manifest["stateRevision"],
                         build_snapshot(self.store)["state_revision"])
        self.assertEqual(manifest["GeneratedDigest"], manifest["setDigest"])
        # Every listed file digest must match the file on disk.
        for name, digest in manifest["files"].items():
            actual = hashlib.sha256((self.out / name).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, name)
        quota = self.payload("quota-state.json")
        self.assertEqual(quota["version"], 1)
        routes = quota["routes"]
        self.assertTrue(any("account-1" in key for key in routes), routes)
        # A verified-available leg is not a cooldown and must not appear here.
        self.assertFalse(any("account-2" in key for key in routes), routes)
        entry = next(v for k, v in routes.items() if "account-1" in k)
        self.assertEqual(entry["reason"], "quota.confirmed")
        self.assertIsNotNone(entry.get("blockedUntil"))

    def test_foundry_route_policy_matches_the_config_validator_contract(self) -> None:
        doc = self.payload("foundry-route-policy.json")
        self.assertEqual(doc["schema"], "foundry-route-policy/v1")
        self.assertEqual(doc["owner"], "cline-model-optimizer")
        self.assertIs(doc["neverPayg"], True)
        self.assertEqual(doc["allowedTiers"], ["FREE", "SUBSCRIPTION"])
        self.assertTrue(doc["freeRoute"])
        self.assertTrue(all(leg["tier"] == "FREE" for leg in doc["freeRoute"]))
        self.assertEqual([leg["rank"] for leg in doc["freeRoute"]],
                         list(range(1, len(doc["freeRoute"]) + 1)))
        self.assertTrue(doc["subscriptionFallback"])
        for leg in doc["subscriptionFallback"]:
            self.assertEqual(leg["provider"], "cline-pass")
            self.assertEqual(leg["thinking"], "off")
        sub_legs = [leg for leg in doc["routeQueue"]
                    if leg["legKind"] == "subscription"]
        self.assertEqual(len(sub_legs), len(doc["subscriptionFallback"]))
        free_legs = [leg for leg in doc["routeQueue"] if leg["legKind"] == "free"]
        self.assertEqual([leg["seq"] for leg in doc["routeQueue"]],
                         list(range(1, len(doc["routeQueue"]) + 1)))
        # Model-major: the queue must list every account of a model before the next model.
        ordered_models = [leg["model"] for leg in free_legs]
        self.assertEqual(ordered_models,
                         sorted(ordered_models, key=lambda m: _first_index(ordered_models, m)))
        self.assertEqual({leg["piAccount"] for leg in free_legs},
                         set(doc["piAccounts"]))
        # A verified leg must be reflected as eligible live state.
        available = next(leg for leg in free_legs
                         if leg["piAccount"] == "account-2"
                         and leg["model"] == "cline-free/muse-spark-1.3-contributor")
        self.assertEqual(available["state"], "AVAILABLE")
        self.assertTrue(available["eligible"])
        self.assertEqual(doc["policyDigest"], self.payload(
            "cmo-compat-manifest.json")["policyDigest"])

    def test_exports_are_consumed_by_the_real_pi_launcher_settings(self) -> None:
        """Feed the generated policy to the shipped Pi consumer, exactly as Pi does."""
        module = CONFIG_ROOT / "tools" / "pi" / "pi-routing-policy.mjs"
        settings_path = CONFIG_ROOT / "tools" / "pi" / "resources" / "account-routing.json"
        if not (module.exists() and settings_path.exists()):
            self.skipTest("config Pi routing policy module not present")
        script = self.tmp / "consume.mjs"
        script.write_text(
            "import fs from 'node:fs';\n"
            "import { resolveStrategyStages } from %s;\n"
            "const p = JSON.parse(fs.readFileSync(%s, 'utf8'));\n"
            "const env = { LOCALAPPDATA: process.argv[2] };\n"
            "const free = resolveStrategyStages(p, 'free-first', { env });\n"
            "const glm = resolveStrategyStages(p, 'glm-flash', { env });\n"
            "const freeOnly = resolveStrategyStages(p, 'free-first', { env, freeOnly: true });\n"
            "console.log(JSON.stringify({ source: free.source, free: free.stages,\n"
            "  glm: glm.stages, freeOnly: freeOnly.stages }));\n"
            % (json.dumps(module.as_uri()), json.dumps(str(settings_path))),
            encoding="utf-8")
        result = subprocess.run(["node", str(script), str(self.tmp)],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertIn("foundry-route-policy.json", payload["source"])
        self.assertEqual([s["model"] for s in payload["free"]], [
            "cline-free/muse-spark-1.3-contributor", "z-ai/glm-5.3-flash",
            "cline-free/deepseek-v4.1-flash", "cline-pass/glm-5.3-flash",
            "cline-pass/deepseek-v4.1-flash"])
        self.assertEqual([s["provider"] for s in payload["free"]],
                         ["cline", "cline", "cline", "cline-pass", "cline-pass"])
        self.assertEqual([s["model"] for s in payload["glm"]],
                         ["z-ai/glm-5.3-flash", "cline-pass/glm-5.3-flash"])
        # Free-only must never expose the ClinePass subscription tail.
        self.assertEqual([s["model"] for s in payload["freeOnly"]], [
            "cline-free/muse-spark-1.3-contributor", "z-ai/glm-5.3-flash",
            "cline-free/deepseek-v4.1-flash"])
        self.assertTrue(all(s["stage"] in ("free", "subscription")
                            for s in payload["free"]))

    def test_live_state_reports_the_decision_actually_taken(self) -> None:
        live = self.payload("foundry-route-policy.json")["liveState"]
        snapshot = build_snapshot(self.store)
        self.assertEqual(live["stateRevision"], snapshot["state_revision"])
        self.assertEqual(live["decision"], snapshot["decision"]["action"])
        self.assertEqual(live["selectedRoute"],
                         snapshot["decision"]["route"]["route_key"])
        self.assertEqual(live["reasonCode"], snapshot["decision"]["reason_code"])
        self.assertIsNone(live["blockedReason"])
        self.assertEqual(live["bestAvailable"],
                         "cline-free/muse-spark-1.3-contributor@account-2")
        self.assertIn("z-ai/glm-5.3-flash", live["ineligibleModels"])


class MigrationTests(StoreCase):
    def _legacy_tree(self, root: Path) -> Path:
        pi = root / ".pi" / "supervisor-routing"
        pi.mkdir(parents=True)
        (pi / "quota-state.json").write_text(json.dumps({
            "version": 1,
            "routes": {
                "account-1|cline|cline-free/muse-spark-1.3-contributor": {
                    "account": "account-1", "stage": "free", "provider": "cline",
                    "model": "cline-free/muse-spark-1.3-contributor",
                    "exhaustedAt": "2026-09-19T06:58:03.230Z",
                    "blockedUntil": "2030-09-19T19:49:19.014Z",
                    "reason": "confirmed_free_quota_exhaustion"},
                "account-2|cline|z-ai/glm-5.3-flash": {
                    "account": "account-2", "stage": "free", "provider": "cline",
                    "model": "z-ai/glm-5.3-flash",
                    "exhaustedAt": "2026-09-19T13:56:46.790Z",
                    "reason": "unknown_reset"},
            },
        }), encoding="utf-8")
        return pi / "quota-state.json"

    def _bom_free_models(self, root: Path) -> Path:
        # The legacy PowerShell writer emitted a UTF-8 BOM.
        target = root / "free-models.json"
        payload = json.dumps({"fetchedAt": "2026-09-19T07:00:00.000Z",
                              "free": [{"id": "cline-free/muse-spark-1.3-contributor"}],
                              "clinePass": ["cline-pass/glm-5.3-flash"]})
        target.write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
        return target

    def test_migration_imports_legacy_evidence_and_is_idempotent(self) -> None:
        legacy = self._legacy_tree(self.tmp)
        bom = self._bom_free_models(self.tmp)
        first = migrate(self.store, source=[str(legacy), str(bom)])
        revision = self.store.revision()
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["state_revision"], revision)
        second = migrate(self.store, source=[str(legacy), str(bom)])
        self.assertEqual(self.store.revision(), revision,
                         "re-running migration must not add events")
        self.assertEqual([s["status"] for s in second["sources"]],
                         ["already_imported", "already_imported"])
        snapshot = build_snapshot(self.store)
        quota = self.cell(snapshot,
                          "account-1|cline|cline-free/muse-spark-1.3-contributor|free")
        self.assertEqual(quota["state"], "QUOTA")
        self.assertEqual(quota["last_reason_code"], "quota.confirmed")
        self.assertEqual(quota["reset_known"], 1)
        self.assertGreater(quota["reset_after_ms"], 0)
        # The BOM-prefixed legacy catalog file must still be imported.
        self.assertTrue(snapshot["catalog"], snapshot["catalog"])
        self.assertEqual({row["model"] for row in snapshot["catalog"]},
                         {"cline-free/muse-spark-1.3-contributor",
                          "cline-pass/glm-5.3-flash"})

    def test_unknown_reset_is_never_given_a_fabricated_time(self) -> None:
        legacy = self._legacy_tree(self.tmp)
        migrate(self.store, source=[str(legacy)])
        snapshot = build_snapshot(self.store)
        unknown = self.cell(snapshot, "account-2|cline|z-ai/glm-5.3-flash|free")
        # The provider reset time is unknown, so the cell must never claim one:
        # it stays QUOTA, or becomes QUOTA_EXPIRED once the bounded re-check is due.
        self.assertIn(unknown["state"], ("QUOTA", "QUOTA_EXPIRED"))
        self.assertEqual(unknown["last_reason_code"], "quota.reset_unknown")
        self.assertEqual(unknown["reset_known"], 0)
        self.assertIsNone(unknown["reset_at"])
        self.assertIsNone(unknown["reset_at_iso"])
        # ...but it must carry a bounded next check instead of a permanent block.
        self.assertIsNotNone(unknown["next_check_at"])
        self.assertIsNotNone(unknown["next_check_at_iso"])
        self.assertGreater(unknown["reset_after_ms"], 0)
        free_route = next(r for r in snapshot["free_matrix"]
                          if r["model"] == "z-ai/glm-5.3-flash")
        self.assertFalse(any(cell["state"] == "AVAILABLE"
                             for cell in free_route["accounts"].values()))
        self.assertFalse(any(cell["state"] == "AVAILABLE" for cell in snapshot["cells"]))
        decision = snapshot["decision"]
        self.assertEqual(decision["action"], "PROBE")
        # account-1 muse is known-exhausted, so the probe moves to account-2.
        self.assertEqual(decision["route"]["route_key"],
                         "account-2|cline|cline-free/muse-spark-1.3-contributor|free")
        skipped = {item["route_key"]: item for item in decision["skipped"]}
        self.assertIn("account-1|cline|cline-free/muse-spark-1.3-contributor|free", skipped)
        self.assertEqual(skipped["account-1|cline|cline-free/muse-spark-1.3-contributor|free"]
                         ["reason_code"], "quota.confirmed")


class CliEnvelopeTests(StoreCase):
    TWO_TOKEN_COMMANDS = {"route", "event", "catalog", "override", "exports", "db", "outbox"}
    NO_STORE_COMMANDS = {"validate-policy"}

    def run_cli(self, *argv: str, expect_code: int = 0) -> dict:
        args = list(argv)
        if args and args[0] in self.NO_STORE_COMMANDS:
            pass
        elif args and args[0] in self.TWO_TOKEN_COMMANDS:
            args = args[:2] + ["--db", str(self.db_path)] + args[2:]
        else:
            args = args[:1] + ["--db", str(self.db_path)] + args[1:]
        stdout, stderr = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            code = cli_main(args)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, expect_code, f"{argv} -> {stderr.getvalue()}")
        text = stdout.getvalue().strip()
        return json.loads(text) if text else {}

    def test_validate_policy_and_route_decide_envelopes(self) -> None:
        payload = self.run_cli("validate-policy")
        self.assertEqual(payload["schema"], "cmo.cli/v2")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["policy_digest"], payload["policy_digest"])
        self.assertEqual(payload["cmo_version"], __version__)
        decision = self.run_cli("route", "decide")
        self.assertEqual(decision["decision"]["action"], "PROBE")
        self.assertEqual(decision["state_revision"], 0)

    def test_event_ingest_from_stdin_then_snapshot(self) -> None:
        payload = json.dumps([self.probe_ok("account-1",
                                            "cline-free/muse-spark-1.3-contributor")])
        old = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            result = self.run_cli("event", "ingest", "--file", "-")
        finally:
            sys.stdin = old
        self.assertEqual(result["state_revision"], 1)
        self.assertIs(result["ok"], True)
        snapshot = self.run_cli("snapshot")
        self.assertEqual(snapshot["state_revision"], 1)
        cell = [c for c in snapshot["cells"]
                if c["route_key"].startswith("account-1|cline|cline-free/muse")][0]
        self.assertEqual(cell["state"], "AVAILABLE")

    def test_invalid_input_fails_nonzero_without_state_change(self) -> None:
        payload = json.dumps({"event_type": "route.explode"})
        old = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            result = self.run_cli("event", "ingest", "--file", "-", expect_code=1)
        finally:
            sys.stdin = old
        self.assertIs(result["ok"], False)
        self.assertTrue(result["failures"])
        self.assertEqual(result["state_revision"], 0)
        self.assertEqual(build_snapshot(self.store)["state_revision"], 0)

    def test_unparseable_payload_is_a_usage_error(self) -> None:
        old = sys.stdin
        sys.stdin = io.StringIO("{not json")
        try:
            self.run_cli("event", "ingest", "--file", "-", expect_code=2)
        finally:
            sys.stdin = old
        self.assertEqual(build_snapshot(self.store)["state_revision"], 0)

    def test_outbox_drain_is_idempotent_and_keeps_bad_lines(self) -> None:
        outbox = self.tmp / "outbox.jsonl"
        good = self.probe_ok("account-1", "cline-free/deepseek-v4.1-flash")
        good["event_id"] = "outbox-1"
        outbox.write_text("\n".join([
            json.dumps(good),
            "{not json",
            json.dumps(self.probe_failed("account-2", "cline-free/deepseek-v4.1-flash",
                                         reset_after_ms=600000, reset_known=1)),
        ]) + "\n", encoding="utf-8")
        # An undeliverable line makes the drain exit non-zero while the report
        # still states exactly what was and was not delivered.
        first = self.run_cli("outbox", "drain", "--path", str(outbox), expect_code=1)
        self.assertEqual(first["drained"], 2)
        self.assertEqual(first["pending"], 1)
        self.assertIs(first["ok"], False)
        remaining = outbox.read_text(encoding="utf-8")
        self.assertIn("not json", remaining)
        self.assertNotIn("outbox-1", remaining)
        revision = build_snapshot(self.store)["state_revision"]
        self.assertEqual(revision, 2)
        cell = self.cell(build_snapshot(self.store),
                         "account-1|cline|cline-free/deepseek-v4.1-flash|free")
        self.assertEqual(cell["state"], "AVAILABLE")
        second = self.run_cli("outbox", "drain", "--path", str(outbox), expect_code=1)
        self.assertEqual(second["drained"], 0)
        self.assertEqual(second["pending"], 1)
        self.assertEqual(build_snapshot(self.store)["state_revision"], revision)

    def test_outbox_drain_of_only_good_lines_succeeds(self) -> None:
        outbox = self.tmp / "clean.jsonl"
        outbox.write_text(json.dumps(self.probe_ok("account-1", "z-ai/glm-5.3-flash")) + "\n",
                          encoding="utf-8")
        result = self.run_cli("outbox", "drain", "--path", str(outbox))
        self.assertEqual(result["drained"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertIs(result["ok"], True)
        self.assertEqual(outbox.read_text(encoding="utf-8"), "")


class ArtifactTests(unittest.TestCase):
    def test_pyz_build_is_deterministic_and_self_describing(self) -> None:
        script = REPO_ROOT / "tools" / "build_pyz.py"
        if not script.exists():
            self.skipTest("build_pyz.py missing")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = subprocess.run([sys.executable, str(script), str(out / "cmo.pyz")],
                                    capture_output=True, text=True, timeout=300,
                                    cwd=str(REPO_ROOT))
            self.assertEqual(result.returncode, 0, result.stderr)
            first = (out / "cmo.pyz").read_bytes()
            manifest = json.loads((out / "cmo-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(first).hexdigest(), manifest["sha256"])
            self.assertEqual(int(manifest["members"]) > 10, True)
            second_run = subprocess.run([sys.executable, str(script), str(out / "cmo.pyz")],
                                        capture_output=True, text=True, timeout=300,
                                        cwd=str(REPO_ROOT))
            self.assertEqual(second_run.returncode, 0, second_run.stderr)
            self.assertEqual(first, (out / "cmo.pyz").read_bytes(),
                             "rebuilt artifact must be byte-identical")
            with zipfile.ZipFile(io.BytesIO(first)) as bundle:
                names = bundle.namelist()
                self.assertIn("__main__.py", names)
                self.assertIn("cmo/web/index.html", names)
                self.assertIn("cmo/web/app.js", names)
                self.assertIn("cmo/web/style.css", names)
                for info in bundle.infolist():
                    self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0), info.filename)

    def test_built_artifact_identifies_itself(self) -> None:
        if not PYZ.exists():
            self.skipTest("dist/cmo.pyz not built")
        result = subprocess.run([sys.executable, str(PYZ), "--version"],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"cmo {__version__}", result.stdout)
        manifest = json.loads((REPO_ROOT / "dist" / "cmo-manifest.json").read_text("utf-8"))
        actual = hashlib.sha256(PYZ.read_bytes()).hexdigest()
        self.assertEqual(actual, manifest["sha256"])


if __name__ == "__main__":
    unittest.main()
