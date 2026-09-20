"""Recheck probe protocol: real Pi probe CLI shape and total status mapping."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cmo import recheck
from cmo.recheck import (_agent_dir, _from_probe_status, _parse_probe_output,
                         _probe_script, run_probe)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PI_MODEL_PROBE", raising=False)
    monkeypatch.delenv("CMO_PROBE_COMMAND", raising=False)
    monkeypatch.delenv("PI_PROFILE_ROOT", raising=False)


def test_probe_script_prefers_pi_model_probe(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PI_MODEL_PROBE", "/x/pi-model-probe.mjs")
    monkeypatch.setenv("CMO_PROBE_COMMAND", "/y/legacy --flag")
    assert _probe_script() == ["/x/pi-model-probe.mjs"]
    monkeypatch.delenv("PI_MODEL_PROBE")
    assert _probe_script() == ["/y/legacy", "--flag"]
    monkeypatch.delenv("CMO_PROBE_COMMAND")
    assert _probe_script() is None


def test_agent_dir_rejects_unsafe_alias(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PI_PROFILE_ROOT", str(tmp_path))
    good = _agent_dir("account-1")
    assert good is not None and good.name == "agent"
    assert good.parent.name == "account-1"
    assert _agent_dir("../evil") is None
    assert _agent_dir("") is None
    assert _agent_dir("a" * 33) is None


def test_parse_probe_output_trailing_json():
    out = "some log noise\n{\"provider\":\"cline\",\"model\":\"m\",\"status\":\"healthy\"}\n"
    assert _parse_probe_output(out) == {"provider": "cline", "model": "m",
                                        "status": "healthy"}
    assert _parse_probe_output("no json here") is None
    assert _parse_probe_output("{\"status\":\"bogus\"}") is None
    assert _parse_probe_output("{not json}\n{\"status\":\"quota\"}")["status"] == "quota"


def test_from_probe_status_is_total():
    assert _from_probe_status({"status": "healthy"})["outcome"] == "succeeded"
    quota = _from_probe_status({"status": "quota", "resetAfterMs": 60000,
                                "detail": "slow down"})
    assert quota["outcome"] == "failed" and quota["reason"] == "quota.probe_confirmed"
    assert quota["reset_after_ms"] == 60000
    assert _from_probe_status({"status": "auth"})["reason"] == "auth.probe_failed"
    assert _from_probe_status({"status": "model"})["reason"] == "capability.not_offered"
    assert _from_probe_status({"status": "timeout"})["reason"] == "transient.failure"
    assert _from_probe_status({"status": "error"})["outcome"] == "failed"


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_probe(monkeypatch: pytest.MonkeyPatch, proc: _Proc,
                   calls: list, script: str = "/x/probe"):
    monkeypatch.setenv("PI_MODEL_PROBE", script)
    monkeypatch.setenv("PI_PROFILE_ROOT", "/profiles")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return proc

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_run_probe_invokes_real_cli_shape(monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    body = json.dumps({"provider": "cline", "model": "m1", "status": "healthy",
                       "detail": "served 12 tokens"})
    _install_probe(monkeypatch, _Proc(0, stdout=f"log line\n{body}\n"), calls)
    result = run_probe("account-1|cline|m1|free", timeout_s=5)
    assert result["outcome"] == "succeeded" and result["reason"] == "probe.ok"
    argv = calls[0]
    assert argv[0] == "/x/probe"
    assert "--provider" in argv and "cline" in argv
    assert "--model" in argv and "m1" in argv
    assert "--agent-dir" in argv
    agent_idx = argv.index("--agent-dir") + 1
    assert argv[agent_idx].replace("\\", "/").endswith("account-1/agent")
    assert argv[-4:] == ["--thinking", "off", "--timeout-ms", "5000"]


def test_run_probe_refuses_silence_as_success(monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    _install_probe(monkeypatch, _Proc(0, stdout="all quiet\n"), calls)
    result = run_probe("account-1|cline|m1|free")
    assert result["outcome"] == "blocked"
    assert result["reason"] == "probe.unparseable_output"


def test_run_probe_nonzero_exit_is_blocked(monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    _install_probe(monkeypatch, _Proc(3, stderr="boom"), calls)
    result = run_probe("account-1|cline|m1|free")
    assert result["outcome"] == "blocked"
    assert result["reason"] == "probe.exit_3"


def test_run_probe_nonzero_exit_with_status_json_settles(monkeypatch: pytest.MonkeyPatch):
    # The canonical probe exits 1 for every classified non-healthy outcome
    # while still printing the trailing JSON status line; the worker must
    # settle by mapping, not park as blocked.
    calls: list = []
    body = json.dumps({"provider": "cline", "model": "m1", "status": "quota",
                       "detail": "429 daily free limit", "resetAfterMs": 42000000})
    _install_probe(monkeypatch, _Proc(1, stdout=f"noise\n{body}\n"), calls)
    result = run_probe("account-1|cline|m1|free")
    assert result["outcome"] == "failed"
    assert result["reason"] == "quota.probe_confirmed"
    assert result["reset_after_ms"] == 42000000


def test_run_probe_nonzero_exit_auth_json_settles(monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    body = json.dumps({"status": "auth", "detail": "login required"})
    _install_probe(monkeypatch, _Proc(1, stdout=body), calls)
    result = run_probe("account-1|cline|m1|free")
    assert result["outcome"] == "failed"
    assert result["reason"] == "auth.probe_failed"


def test_run_probe_nonzero_exit_unparseable_stays_blocked(monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    _install_probe(monkeypatch, _Proc(1, stdout="traceback noise\n", stderr=""), calls)
    result = run_probe("account-1|cline|m1|free")
    assert result["outcome"] == "blocked"
    assert result["reason"] == "probe.exit_1"


def test_run_probe_timeout_is_blocked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PI_MODEL_PROBE", "/x/probe")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_probe("account-1|cline|m1|free", timeout_s=1)
    assert result["outcome"] == "blocked" and result["reason"] == "recheck.timeout"


def test_run_probe_without_executor_blocks(monkeypatch: pytest.MonkeyPatch):
    result = run_probe("account-1|cline|m1|free")
    assert result["outcome"] == "blocked"
    assert result["reason"] == "recheck.no_probe_executor"


def test_run_probe_quota_maps_to_failed_with_reset(monkeypatch: pytest.MonkeyPatch):
    calls: list = []
    body = json.dumps({"status": "quota", "resetAfterMs": 120000})
    _install_probe(monkeypatch, _Proc(0, stdout=body), calls)
    result = run_probe("account-2|cline|m1|free")
    assert result["outcome"] == "failed"
    assert result["reason"] == "quota.probe_confirmed"
    assert result["reset_after_ms"] == 120000


def test_step_settles_quota_probe_to_quota_state(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch):
    from cmo.events import EventStore
    from cmo.recheck import step
    monkeypatch.setenv("CMO_TEST_MODE", "isolated")
    db = tmp_path / "test-recheck-quota-probe.sqlite3"
    store = EventStore(db)
    try:
        store.ingest({"event_id": "rq-q", "event_type": "route.recheck.requested",
                      "occurred_at": 1_700_000_000_000, "source_component": "ui",
                      "account_alias": "account-2", "provider": "cline",
                      "model": "m1", "tier": "free"})
        body = json.dumps({"status": "quota", "resetAfterMs": 60000})
        calls: list = []
        _install_probe(monkeypatch, _Proc(0, stdout=body), calls)
        report = step(store)
        assert report["outcome"] == "failed"
        rows = store.route_state()
        assert rows and rows[0]["state"] == "QUOTA"
        assert rows[0]["reset_after_ms"] == 60000
    finally:
        store.close()
