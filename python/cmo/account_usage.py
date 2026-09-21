"""Per-profile, read-only Cline plan and usage checks; never infer a plan from OAuth."""
from __future__ import annotations
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = 'https://api.cline.bot/api/v1/users/me'
WINDOWS = {'five_hour': '5h', 'weekly': 'weekly', 'monthly': 'monthly'}


def _fetch(suffix: str, token: str) -> tuple[int, object]:
    request = Request(API + suffix, headers={
        'Authorization': 'Bearer ' + (token if token.startswith('workos:') else 'workos:' + token),
        'Accept': 'application/json', 'X-CLIENT-TYPE': 'pi',
        'User-Agent': 'pi-cline-oauth-extension'})
    try:
        with urlopen(request, timeout=8) as response:
            document = json.loads(response.read(262144))
            if not isinstance(document, dict): return response.status, None
            return response.status, document.get('data', document)
    except HTTPError as error:
        return error.code, None
    except (URLError, TimeoutError, OSError, ValueError):
        return 0, None


def _result(alias: str, status: str, **extra: object) -> dict:
    return {'account_alias': alias, 'plan_status': status,
            'checked_at': int(time.time() * 1000), 'windows': {}, **extra}


def account_usage(alias: str, profile: str, label: str) -> dict:
    """One on-demand account read; no token refresh, chargeable request, or shared browser state."""
    root = Path(os.environ.get('PI_PROFILE_ROOT', '').strip() or
                str(Path.home() / '.pi' / 'supervisor-accounts'))
    try:
        doc = json.loads((root / profile / 'agent' / 'auth.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return _result(alias, 'authentication-unavailable')
    if not isinstance(doc, dict): return _result(alias, 'authentication-unavailable')
    auth = doc.get('cline') or doc.get('cline-pass')
    if not isinstance(auth, dict) or not isinstance(auth.get('access'), str):
        return _result(alias, 'authentication-unavailable')
    if float(auth.get('expires') or 0) <= time.time() * 1000 + 30000:
        return _result(alias, 'authentication-expired')
    token = auth['access']
    code, user = _fetch('', token)
    if code == 401: return _result(alias, 'authentication-expired')
    if code != 200 or not isinstance(user, dict):
        return _result(alias, 'provider-unavailable')
    actual_email = user.get('email')
    if '@' in label and isinstance(actual_email, str) and actual_email.casefold() != label.casefold():
        return _result(alias, 'identity-mismatch')
    if '@' not in label or not isinstance(actual_email, str):
        return _result(alias, 'identity-unavailable')
    code, data = _fetch('/plan', token)
    if code == 401: return _result(alias, 'authentication-expired')
    if code != 200 or not isinstance(data, dict):
        return _result(alias, 'provider-unavailable')
    plan = data.get('plan')
    if not isinstance(plan, dict) or not data.get('subscriptionId'):
        return _result(alias, 'no-active-plan-confirmed')
    try:
        from datetime import datetime, timezone
        period_end = datetime.fromisoformat(str(data['currentPeriodEnd']).replace('Z', '+00:00'))
        period_active = period_end > datetime.now(timezone.utc)
    except (KeyError, ValueError, TypeError):
        period_active = False
    if plan.get('isActive') is not True or not period_active:
        return _result(alias, 'no-active-plan-confirmed')
    code, limits = _fetch('/plan/usage-limits', token)
    if code != 200 or not isinstance(limits, dict):
        return _result(alias, 'active', usage_status='authentication-expired' if code == 401 else 'unavailable')
    windows = {}
    for row in limits.get('limits', []):
        if not isinstance(row, dict) or row.get('type') not in WINDOWS:
            continue
        value = row.get('percentUsed')
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1000:
            continue
        reset = row.get('resetsAt')
        windows[WINDOWS[row['type']]] = {'percent_used': value,
            'resets_at': reset if isinstance(reset, str) and len(reset) <= 40 else None}
    return _result(alias, 'active', usage_status='available' if len(windows) == 3 else 'partial',
                   windows=windows)
