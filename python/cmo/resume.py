"""Explicit, narrowly scoped local Pi Goal resume for the Shiftio pilot.

Never accept shell commands, file paths, objectives, or provider choices from HTTP.
The existing supervised launcher verifies saved Goal lineage and chooses a route.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any

SHIFTIO_REPO = 'analienx/Shiftio'
WORKSPACE = Path(os.environ.get('CMO_WORKSPACE_ROOT', 'C:/Workspace'))
PROFILE = Path.home()/'.pi'/'supervisor-sessions'
TELEMETRY = Path(os.environ.get('ProgramData', 'C:/ProgramData'))/'Analienx'/'runner'/'agents'
RESUME_LOCK = threading.Lock()
ACTIVE_LAUNCHES: dict[str, subprocess.Popen] = {}

class ResumeRejected(ValueError):
    pass

def _read_telemetry(goal_id: str) -> dict[str, Any]:
    if not re.fullmatch(r'[0-9a-f-]{36}', goal_id):
        raise ResumeRejected('invalid Goal identifier')
    matches=[]
    for path in TELEMETRY.glob('pi-*.json'):
        try:
            doc=json.loads(path.read_text(encoding='utf-8-sig'))
            if doc.get('goal_id')==goal_id and doc.get('repo')==SHIFTIO_REPO:
                matches.append((str(doc.get('updated_at') or ''),doc))
        except (OSError,ValueError):
            continue
    if not matches:
        raise ResumeRejected('No verified Shiftio launch record for this Goal')
    return max(matches,key=lambda row:row[0])[1]
def _saved_goal(session: Path, goal_id: str) -> dict[str, Any]:
    if not session.is_file() or session.suffix!='.jsonl':
        raise ResumeRejected('Saved Pi session file is missing')
    root=PROFILE.resolve()
    if not session.resolve().is_relative_to(root):
        raise ResumeRejected('Session is outside the approved Pi supervisor directory')
    with session.open('rb') as stream:
        stream.seek(0,2)
        stream.seek(max(0,stream.tell()-262144))
        lines=stream.read().decode('utf-8',errors='replace').splitlines()
    for line in reversed(lines):
        try:
            record=json.loads(line)
        except ValueError:
            continue
        goal=(record.get('data') or {}).get('goal') if record.get('customType')=='goal-state' else None
        if isinstance(goal,dict):
            if goal.get('id')!=goal_id or goal.get('status') not in ('active','paused','blocked'):
                raise ResumeRejected('Saved Goal lineage or resumable status does not match')
            if not isinstance(goal.get('text'),str) or not goal['text'].strip():
                raise ResumeRejected('Saved Goal has no objective')
            return goal
    raise ResumeRejected('No recent persisted Goal state could be verified')

def _no_competing_router() -> None:
    if os.name!='nt':
        raise ResumeRejected('Pi resume is available only on the authorized Windows host')
    command=("$ErrorActionPreference='Stop'; "
        "$rows=@(Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.Contains('pi-account-router.mjs') "
        "-and $_.CommandLine.Contains('analienx/Shiftio') }); "
        "Write-Output $rows.Count")
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],
            capture_output=True,text=True,timeout=12,check=True)
        count=int(result.stdout.strip())
    except (OSError,ValueError,subprocess.SubprocessError) as exc:
        raise ResumeRejected('Could not verify Pi process ownership; resume refused') from exc
    if count:
        raise ResumeRejected('A Shiftio Pi router is already running; no duplicate was started')
def request_resume(goal_id: str, decision: dict[str,Any]) -> dict[str,Any]:
    """Request the existing supervised launcher; never claim a completed resume."""
    if not isinstance(decision,dict):
        raise ResumeRejected('CMO route decision is unavailable')
    route=decision.get('route') or {}
    if (decision.get('action') not in ('PROBE','LAUNCH') or
            route.get('tier')!='free' or decision.get('free_only') is not True):
        raise ResumeRejected('No eligible free-only route; recheck after the quota cooldown')
    with RESUME_LOCK:
        running=ACTIVE_LAUNCHES.get(goal_id)
        if running is not None and running.poll() is None:
            raise ResumeRejected('A resume request is already in progress')
        record=_read_telemetry(goal_id)
        if int(record.get('issue') or 0)!=56:
            raise ResumeRejected('Shiftio Goal issue identity does not match')
        session=Path(str(record.get('session_file') or ''))
        saved=_saved_goal(session,goal_id)
        repo=WORKSPACE/'worktrees'/'shiftio-pilot-e2e-20260920'
        launcher=WORKSPACE/'repos'/'config'/'tools'/'pi'/'Start-PiExecutor.ps1'
        if not repo.is_dir() or not (repo/'.git').exists() or not launcher.is_file():
            raise ResumeRejected('Approved Shiftio checkout or supervised launcher is unavailable')
        _no_competing_router()
        audit=PROFILE/'resume-audit'
        audit.mkdir(parents=True,exist_ok=True)
        log_file=audit/f'resume-{goal_id}.log'
        args=['powershell.exe','-NoProfile','-NonInteractive','-File',str(launcher),
              '-RepoPath',str(repo),'-Repo',SHIFTIO_REPO,'-Issue','56',
              '-Objective',saved['text'],'-ResumeSession',str(session),
              '-ResumeGoalId',goal_id,'-ResumeGoal','-Strategy','free-first',
              '-FreeOnly','-GoalHandshakeTimeoutSeconds','120']
        try:
            with log_file.open('a',encoding='utf-8') as output:
                launched=subprocess.Popen(args,cwd=str(repo),stdin=subprocess.DEVNULL,
                    stdout=output,stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError as exc:
            raise ResumeRejected('Supervised launcher could not be started') from exc
        ACTIVE_LAUNCHES[goal_id]=launched
        return {'ok':True,'status':'requested','goal_id':goal_id,
                'message':'Supervised resume requested; await a verified Goal handshake.'}
