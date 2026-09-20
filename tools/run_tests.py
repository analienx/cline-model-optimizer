#!/usr/bin/env python3
"""Run the CMO v2 offline suite and write a machine-readable evidence report.

Usage:
    python tools/run_tests.py [--out artifacts/tests/report.json] [--browser]

``--browser`` additionally runs the CDP browser acceptance + DOM review scripts
(Node must be available and the flags for those scripts are read from the
environment: CMO_BASE_URL, CMO_CHROME, CMO_BROWSER_OUT).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULES = ("tests.test_cmo_core", "tests.test_cmo_http", "tests.test_cmo_compat")
PYTEST_FILES = ("tests/test_quarantine_recheck.py", "tests/test_snapshot_truth.py",
                "tests/test_recheck_worker.py", "tests/test_recheck_probe.py",
                "tests/test_policy_versions.py", "tests/test_policy_http.py",
                "tests/test_session_identity.py")

for _extra in (str(REPO), str(REPO / "python")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)


class _Collector(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: list[dict] = []
        self._started: dict[str, float] = {}

    def startTest(self, test) -> None:  # noqa: D102
        self._started[test.id()] = time.time()
        super().startTest(test)

    def _finish(self, test, outcome: str, detail: str = "") -> None:
        started = self._started.pop(test.id(), time.time())
        self.records.append({"id": test.id(), "outcome": outcome,
                             "duration_ms": int((time.time() - started) * 1000),
                             "detail": detail.strip()[:2000]})

    def addSuccess(self, test) -> None:  # noqa: D102
        super().addSuccess(test)
        self._finish(test, "pass")

    def addFailure(self, test, err) -> None:  # noqa: D102
        super().addFailure(test, err)
        self._finish(test, "fail", self._exc_info_to_string(err, test))

    def addError(self, test, err) -> None:  # noqa: D102
        super().addError(test, err)
        self._finish(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason) -> None:  # noqa: D102
        super().addSkip(test, reason)
        self._finish(test, "skip", reason)


def run_offline() -> dict:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([loader.loadTestsFromName(name) for name in MODULES])
    stream = open(os.devnull, "w", encoding="utf-8")
    runner = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=_Collector)
    started = time.time()
    result = runner.run(suite)
    stream.close()
    records = sorted(result.records, key=lambda r: r["id"])
    totals = {
        "run": result.testsRun,
        "passed": sum(1 for r in records if r["outcome"] == "pass"),
        "failed": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
    }
    records += run_pytest_files(started)
    totals["run"] = len(records)
    totals["passed"] = sum(1 for r in records if r["outcome"] == "pass")
    totals["failed"] = sum(1 for r in records if r["outcome"] == "fail")
    totals["errors"] = sum(1 for r in records if r["outcome"] == "error")
    totals["skipped"] = sum(1 for r in records if r["outcome"] == "skip")
    ok = totals["failed"] == 0 and totals["errors"] == 0 and totals["run"] > 0
    records = sorted(records, key=lambda r: r["id"])
    return {
        "schema": "cmo.test-report/v1",
        "suite": list(MODULES) + ["pytest:" + f for f in PYTEST_FILES],
        "python": sys.version.split()[0],
        "duration_ms": int((time.time() - started) * 1000),
        "totals": totals,
        "ok": ok,
        "tests": records,
    }


def run_pytest_files(started: float) -> list[dict]:
    """Collect pytest-file results (fixture quarantine, snapshot truth, worker)."""
    try:
        import pytest  # noqa: F401
    except ImportError:
        return [{"id": f"pytest:{name} (not collected: pytest missing)",
                 "outcome": "skip", "duration_ms": 0, "detail": "pytest not installed"}
                for name in PYTEST_FILES]
    records: list[dict] = []
    collected: list[str] = []

    class _Plugin:
        def pytest_runtest_logreport(self, report):
            if report.when != "call" and not (report.when == "setup" and report.skipped):
                return
            if report.when == "setup" and report.skipped:
                outcome, detail = "skip", str(report.longrepr)
            elif report.passed:
                outcome, detail = "pass", ""
            elif report.skipped:
                outcome, detail = "skip", str(report.longrepr)
            elif report.failed:
                outcome = "error" if "Error" in str(report.longrepr)[:500] else "fail"
                detail = str(report.longrepr)[-2000:]
            else:
                return
            records.append({"id": f"pytest:{report.nodeid}", "outcome": outcome,
                            "duration_ms": 0, "detail": detail})
            collected.append(report.nodeid)

    existing = [str(REPO / name) for name in PYTEST_FILES
                if (REPO / name).exists()]
    if not existing:
        return []
    import pytest as _pytest
    t0 = time.time()
    _pytest.main(["-q", "--no-header", "-p", "no:cacheprovider"] + existing,
                 plugins=[_Plugin()])
    elapsed = max(1, int((time.time() - t0) * 1000 / max(1, len(records))))
    for record in records:
        record["duration_ms"] = elapsed
    return records


def run_browser(artifacts: Path) -> dict:
    base = os.environ.get("CMO_BASE_URL", "http://127.0.0.1:4311")
    chrome = os.environ.get("CMO_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    out = Path(os.environ.get("CMO_BROWSER_OUT", str(artifacts)))
    out.mkdir(parents=True, exist_ok=True)
    reports: dict[str, dict] = {}
    for script, port in (("prove_live_update.mjs", 9411), ("review_ui.mjs", 9412)):
        path = REPO / "tools" / "browser" / script
        if not path.exists():
            reports[script] = {"ok": False, "error": "script missing"}
            continue
        proc = subprocess.run(
            ["node", str(path), "--base", base, "--chrome", chrome,
             "--out", str(out), "--cdp-port", str(port)],
            capture_output=True, text=True, timeout=300)
        reports[script] = {"ok": proc.returncode == 0,
                           "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-4000:]}
    return reports


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO / "artifacts" / "tests" / "report.json"))
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args(argv[1:])
    report = run_offline()
    if args.browser:
        report["browser"] = run_browser(Path(args.out).parent)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    totals = report["totals"]
    print(f"{'PASS' if report['ok'] else 'FAIL'} {totals['passed']}/{totals['run']} "
          f"passed, {totals['failed']} failed, {totals['errors']} errors, "
          f"{totals['skipped']} skipped in {report['duration_ms']}ms -> {target}")
    if not report["ok"]:
        for record in report["tests"]:
            if record["outcome"] in ("fail", "error"):
                print(f"  {record['outcome'].upper()}: {record['id']}\n{record['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
