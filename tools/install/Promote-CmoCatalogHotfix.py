"""Bounded artifact-only CMO catalog hotfix with exact-identity rollback.

Unlike Install-CmoV2, this never rewrites compatibility route exports,
account profiles, SQLite state, or other user preferences. Windows only.
Usage: python Promote-CmoCatalogHotfix.py plan
       python Promote-CmoCatalogHotfix.py apply PLAN_PATH SHA256
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / 'AppData/Local/ClineModelOptimizer'
LIVE = ROOT / 'v2/cmo.pyz'
NEW = Path('C:/Workspace/worktrees/cmo-live-catalog-repair-20260920/dist/cmo-catalog-fixed.pyz')
AUDIT = Path('C:/Workspace/cmo-desktop-shortcut-20260920')
START = Path.home() / 'AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/ClineModelOptimizer-Service.cmd'
DB = ROOT / 'state/cmo-v2.sqlite3'
PY = Path.home() / 'AppData/Local/Programs/Python/Python312/python.exe'
PROBE = 'node C:/Workspace/repos/config/tools/pi/pi-model-probe.mjs'
OLD_SHA = '70303087ddbc28c39cf102bc3f14e13dcd6e6f86d05ae4cd8296bd377263d5d2'
NEW_SHA = '4be4bb7b1d45c94858a42240f822452887020ecf529c44b904c3f009eb6f9c23'
def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def health() -> dict:
    with urllib.request.urlopen('http://127.0.0.1:4311/api/health', timeout=4) as res:
        value = json.load(res)
    if not value.get('ok') or value.get('service') != 'cmo-v2':
        raise RuntimeError('CMO health/identity failed')
    return value


def snapshot() -> dict:
    with urllib.request.urlopen('http://127.0.0.1:4311/api/snapshot', timeout=5) as res:
        return json.load(res)


def command(pid: int) -> dict:
    if not isinstance(pid, int) or pid < 1:
        raise RuntimeError('invalid CMO pid')
    code = ('Get-CimInstance Win32_Process -Filter "ProcessId=%d" | '
            'Select-Object ProcessId,Name,ExecutablePath,CommandLine | ConvertTo-Json -Compress') % pid
    output = subprocess.run(['powershell.exe', '-NoProfile', '-Command', code],
                            capture_output=True, text=True, timeout=10, check=True).stdout
    data = json.loads(output) if output.strip() else None
    return data or {}
def identity() -> dict:
    if os.name != 'nt':
        raise RuntimeError('Windows-only bounded CMO service operation')
    if sha(LIVE) != OLD_SHA or sha(NEW) != NEW_SHA:
        raise RuntimeError('current/staged CMO artifact differs from reviewed source')
    service = health()
    if service.get('artifact_digest') != OLD_SHA:
        raise RuntimeError('running CMO digest is not the expected prior release')
    pid = service.get('pid')
    proc = command(pid)
    cmdline = str(proc.get('CommandLine') or '').lower()
    if (proc.get('ProcessId') != pid or proc.get('Name') != 'python.exe'
            or str(proc.get('ExecutablePath') or '').casefold() != str(PY).casefold()
            or str(LIVE).casefold() not in cmdline
            or '--port 4311' not in cmdline or '--artifact-digest ' + OLD_SHA not in cmdline):
        raise RuntimeError('live CMO process identity mismatch')
    startup = START.read_text(encoding='utf-8')
    if startup.count(OLD_SHA) != 1 or PROBE not in startup:
        raise RuntimeError('CMO startup command or provider probe changed')
    state = snapshot()
    if state['policy']['neverPayg'] is not True or len(state['catalog']) < 5:
        raise RuntimeError('live CMO policy or catalog baseline unexpected')
    return {'old_pid': pid, 'old_digest': OLD_SHA, 'new_digest': NEW_SHA,
            'startup_sha': sha(START), 'db_sha': None,
            'policy_digest': service['policy_digest'],
            'old_command': proc['CommandLine'], 'catalog_count': len(state['catalog'])}
def plan() -> None:
    observed = identity()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = AUDIT / ('cmo-catalog-hotfix-' + stamp)
    if target.exists():
        raise RuntimeError('release receipt path already exists')
    record = {'schema': 'cmo.catalog-hotfix-plan/v1', 'source_sha':
              '24d5cc167dee94dd95b503d81bb1862fbe566c01',
              'created_at': int(time.time()), 'expected': observed,
              'receipt_dir': str(target), 'artifact_source': str(NEW),
              'target': str(LIVE), 'startup': str(START),
              'state_db': str(DB), 'operation': 'catalog-artifact-only'}
    blob = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(blob).hexdigest()
    path = AUDIT / ('cmo-catalog-hotfix-plan-' + stamp + '.json')
    with path.open('xb') as stream:
        stream.write(blob)
    print(json.dumps({'plan_path': str(path), 'plan_sha256': digest,
                      'running_pid': observed['old_pid'],
                      'from': OLD_SHA, 'to': NEW_SHA, 'no_exports_or_profiles': True}))


def await_health(expected_pid: int, expected_sha: str) -> dict:
    for _ in range(60):
        try:
            h = health()
            if h.get('pid') == expected_pid and h.get('artifact_digest') == expected_sha:
                return h
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError('new service did not prove exact PID and artifact')
def start_service(digest: str) -> int:
    env = dict(os.environ)
    env['CMO_PROBE_COMMAND'] = PROBE
    child = subprocess.Popen([str(PY), str(LIVE), 'serve', '--port', '4311',
                              '--artifact-digest', digest],
                             cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                             close_fds=True)
    return child.pid


def stop_exact(pid: int, digest: str, original_command: str | None = None) -> None:
    proc = command(pid)
    cmd = str(proc.get('CommandLine') or '')
    if (proc.get('Name') != 'python.exe' or proc.get('ProcessId') != pid
            or str(LIVE).casefold() not in cmd.casefold()
            or '--artifact-digest ' + digest not in cmd
            or (original_command is not None and cmd != original_command)):
        raise RuntimeError('PID identity drift: refusing to stop service')
    subprocess.run(['taskkill.exe', '/PID', str(pid), '/F'],
                   check=True, capture_output=True, timeout=10)
    for _ in range(24):
        if not command(pid):
            return
        time.sleep(0.25)
    raise RuntimeError('exact CMO process did not exit')
def apply(plan_file: Path, plan_hash: str) -> None:
    if (plan_file.parent.resolve() != AUDIT.resolve()
            or not plan_file.name.startswith('cmo-catalog-hotfix-plan-')
            or plan_file.suffix != '.json'
            or sha(plan_file) != plan_hash):
        raise RuntimeError('plan path or content hash mismatch; no mutation')
    record = json.loads(plan_file.read_text(encoding='utf-8'))
    if (record.get('schema') != 'cmo.catalog-hotfix-plan/v1'
            or record.get('operation') != 'catalog-artifact-only'
            or time.time() - record['created_at'] > 600
            or time.time() < record['created_at']
            or record.get('source_sha') != '24d5cc167dee94dd95b503d81bb1862fbe566c01'
            or record.get('artifact_source') != str(NEW)
            or record.get('target') != str(LIVE)
            or record.get('startup') != str(START)
            or record.get('state_db') != str(DB)):
        raise RuntimeError('plan expired or deviated from named bounded operation')
    expected = record['expected']
    if identity() != expected:
        raise RuntimeError('service/artifact/startup/policy changed since planning')
    dest = Path(record['receipt_dir'])
    if dest.parent.resolve() != AUDIT.resolve() or dest.exists():
        raise RuntimeError('receipt directory invalid or already exists')
    dest.mkdir(mode=0o700)
    old_copy = dest / 'previous-cmo.pyz'
    startup_copy = dest / 'previous-startup.cmd'
    db_copy = dest / 'state-before.sqlite3'
    shutil.copy2(LIVE, old_copy)
    shutil.copy2(START, startup_copy)
    with sqlite3.connect(str(DB), timeout=15) as src:
        with sqlite3.connect(str(db_copy)) as dst:
            src.backup(dst)
    if (sha(old_copy) != OLD_SHA or sha(startup_copy) != expected['startup_sha']
            or sha(NEW) != NEW_SHA or identity() != expected):
        raise RuntimeError('backup or service drift; old process remains running')
    result = {'schema': 'cmo.catalog-hotfix-receipt/v1', 'old_pid': expected['old_pid'],
              'new_pid': None, 'old_digest': OLD_SHA, 'new_digest': NEW_SHA,
              'backup_db_sha256': sha(db_copy), 'plan_sha256': plan_hash,
              'policy_digest': expected['policy_digest'], 'status': 'started'}
    stopped = False
    try:
        stop_exact(expected['old_pid'], OLD_SHA, expected['old_command'])
        stopped = True
        shutil.copy2(NEW, LIVE)
        if sha(LIVE) != NEW_SHA:
            raise RuntimeError('staged new artifact checksum mismatch')
        original = startup_copy.read_bytes()
        modified = original.replace(OLD_SHA.encode(), NEW_SHA.encode())
        if modified == original or modified.count(NEW_SHA.encode()) != 1:
            raise RuntimeError('startup entry replacement refused')
        START.write_bytes(modified)
        new_pid = start_service(NEW_SHA)
        result['new_pid'] = new_pid
        new_health = await_health(new_pid, NEW_SHA)
        live = snapshot()
        if (new_health.get('policy_digest') != expected['policy_digest']
                or live['policy']['neverPayg'] is not True
                or len(live['catalog']) < expected['catalog_count']):
            raise RuntimeError('new service changed policy or lost catalog')
        result.update(status='healthy-new', after_catalog=len(live['catalog']),
                      state_revision=new_health['state_revision'])
    except Exception as exc:
        result['failure'] = str(exc)[:280]
        if stopped:
            try:
                if result['new_pid'] and command(result['new_pid']):
                    stop_exact(result['new_pid'], NEW_SHA)
                shutil.copy2(old_copy, LIVE)
                shutil.copy2(startup_copy, START)
                if sha(LIVE) != OLD_SHA or sha(START) != expected['startup_sha']:
                    raise RuntimeError('rollback files failed checksum')
                prior_pid = start_service(OLD_SHA)
                await_health(prior_pid, OLD_SHA)
                result.update(status='rolled-back-old', rollback_pid=prior_pid)
            except Exception as rollback_error:
                result.update(status='rollback-failed',
                              rollback_error=str(rollback_error)[:280])
        else:
            result['status'] = 'old-untouched'
    finally:
        (dest / 'receipt.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result, indent=2))
    if result['status'] != 'healthy-new':
        raise RuntimeError('CMO hotfix release failed: ' + result['status'])
def main() -> None:
    args = sys.argv[1:]
    if args == ['plan']:
        plan()
    elif len(args) == 3 and args[0] == 'apply':
        apply(Path(args[1]), args[2])
    else:
        raise SystemExit('Usage: Promote-CmoCatalogHotfix.py plan | apply PATH SHA256')


if __name__ == '__main__':
    main()
