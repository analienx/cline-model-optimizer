"""Renew one Pi Cline credential using Pi's auth.json.lock convention.

Never refresh ClinePass, run a model, or return credentials to the dashboard.
The target is a registered, isolated profile selected by the server.
"""
from __future__ import annotations
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROFILE_RE = re.compile(r'account-[A-Za-z0-9][A-Za-z0-9_-]{0,31}\Z')
REFRESH_URL = 'https://api.cline.bot/api/v1/auth/refresh'


def _profile_path(profile: str) -> Path | None:
    if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
        return None
    root = Path(os.environ.get('PI_PROFILE_ROOT', '').strip() or
                str(Path.home() / '.pi' / 'supervisor-accounts')).resolve()
    auth = root / profile / 'agent' / 'auth.json'
    return auth if (auth.is_file() and auth.resolve() == auth.absolute()
                    and auth.resolve().is_relative_to(root)) else None


def _lock_file(path: Path) -> bool:
    """Cooperate with Pi's proper-lockfile mkdir lock; do not steal stale locks."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.mkdir(str(path) + '.lock')
            return True
        except FileExistsError:
            time.sleep(0.1)
    return False


def renew_cline(profile: str) -> str:
    """Return a safe status; mutate only this profile's `cline` credential."""
    auth_path = _profile_path(profile)
    if auth_path is None:
        return 'profile-unavailable'
    if not _lock_file(auth_path):
        return 'profile-busy'
    try:
        try:
            doc = json.loads(auth_path.read_text(encoding='utf-8'))
            current = doc.get('cline') if isinstance(doc, dict) else None
            if not isinstance(current, dict) or current.get('type') != 'oauth':
                return 'sign-in-required'
            if float(current.get('expires') or 0) > time.time() * 1000 + 30000:
                return 'already-current'
            refresh = current.get('refresh')
            if not isinstance(refresh, str) or not refresh:
                return 'sign-in-required'
            request = Request(REFRESH_URL,
                data=json.dumps({'refreshToken': refresh, 'grantType': 'refresh_token'}).encode(),
                headers={'Accept': 'application/json', 'Content-Type': 'application/json',
                         'User-Agent': 'pi-cline-oauth-extension', 'X-CLIENT-TYPE': 'pi'},
                method='POST')
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read(65536))
            data = payload.get('data') if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or not payload.get('success') or not isinstance(data, dict):
                return 'renewal-unavailable'
            access = data.get('accessToken')
            rotated = data.get('refreshToken') or refresh
            expiry = data.get('expiresAt')
            if not all(isinstance(s, str) and s for s in (access, rotated, expiry)):
                return 'renewal-unavailable'
            deadline = datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp() * 1000 - 30000
            if deadline <= time.time() * 1000:
                return 'renewal-unavailable'
            doc['cline'] = {**current, 'access': access, 'refresh': rotated, 'expires': deadline}
            _replace_locked(auth_path, doc)
            return 'renewed'
        except (HTTPError, URLError, OSError, ValueError, TypeError, KeyError):
            return 'renewal-unavailable'
    finally:
        os.rmdir(str(auth_path) + '.lock')


def _replace_locked(auth_path: Path, doc: dict) -> None:
    """Replace under the same path-based lock Pi uses; preserve other providers."""
    previous = auth_path.stat()
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=auth_path.parent,
                                         prefix='.auth-refresh-', suffix='.tmp',
                                         delete=False) as out:
            temp_path = out.name
            json.dump(doc, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp_path, previous.st_mode)
        os.replace(temp_path, auth_path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
