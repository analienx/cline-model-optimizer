"""No real login is launched by automated tests."""
import http.client
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_policy_http import PolicyServerCase


class DirectSigninTests(PolicyServerCase):
    def post(self, body, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Host': f'127.0.0.1:{self.port}', 'Content-Type':'application/json',
                   'Sec-Fetch-Site': 'same-origin'}
        if origin is not None: headers['Origin'] = origin
        try:
            conn.request('POST','/api/simple/accounts/start-signin', json.dumps(body), headers)
            response=conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_cross_origin_and_unregistered_are_refused(self):
        assert self.post({'id':'account-1'})[0]==403
        assert self.post({'id':'account-1'}, 'https://evil.test')[0]==403
        assert self.post({'id':'account-4\\'}, f'http://127.0.0.1:{self.port}')[0]==400
        assert self.post({'id':'account-99'}, f'http://127.0.0.1:{self.port}')[0]==400
    def test_missing_profile_refused_without_launch(self):
        origin=f'http://127.0.0.1:{self.port}'
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'PI_PROFILE_ROOT':tmp}), \
             patch('cmo.server.launch_account_signin') as launch:
            status,body=self.post({'id':'account-1'}, origin)
        assert status==409 and 'profile' in body['error'].lower()
        launch.assert_not_called()

    def test_one_window_per_account_and_no_auth_claim(self):
        origin=f'http://127.0.0.1:{self.port}'
        with TemporaryDirectory() as tmp:
            (Path(tmp)/'account-1'/'agent').mkdir(parents=True)
            process=type('FakeProcess', (), {'poll': lambda self: None})()
            with patch.dict(os.environ, {'PI_PROFILE_ROOT':tmp}), \
                 patch('cmo.server.launch_account_signin', return_value=process) as launch:
                first=self.post({'id':'account-1'},origin)
                second=self.post({'id':'account-1'},origin)
        assert first[0]==202 and first[1]['started'] and 'verified' not in str(first[1]).lower()
        assert second[0]==202 and second[1]['already_open'] and not second[1]['started']
        launch.assert_called_once_with('account-1')

    def test_saved_login_refuses_duplicate_signin(self):
        origin=f'http://127.0.0.1:{self.port}'
        with TemporaryDirectory() as tmp:
            agent=Path(tmp)/'account-1'/'agent';agent.mkdir(parents=True)
            (agent/'auth.json').write_text(json.dumps({'cline':{'token':'do-not-return'}}))
            with patch.dict(os.environ, {'PI_PROFILE_ROOT':tmp}), \
                 patch('cmo.server.launch_account_signin') as launch:
                code,body=self.post({'id':'account-1'},origin)
        assert code==409 and 'token' not in str(body)
        launch.assert_not_called()


def test_windows_launch_uses_separate_fixed_arguments():
    from cmo.account_signin import launch_account_signin
    with patch('cmo.account_signin.os.name', 'nt'), \
         patch('cmo.account_signin.HELPER_ROOT') as root, \
         patch('cmo.account_signin.subprocess.Popen') as popen:
        root.__truediv__.return_value.is_file.return_value=True
        process=launch_account_signin('account-4')
        args=popen.call_args.args[0]
        assert args[:4]==['powershell.exe','-NoProfile','-NoExit','-File']
        assert args[-3:]==['-Account','account-4','-CurrentWindow']
        assert args[-2]!='account-4\\'
        assert popen.call_args.kwargs['creationflags'] != 0
        assert process is popen.return_value
    try: launch_account_signin('account-4\\')
    except ValueError: pass
    else: raise AssertionError('invalid trailing backslash accepted')
