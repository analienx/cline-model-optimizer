"""New-account setup never launches unregistered, active or cross-site accounts."""
import http.client
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_policy_http import PolicyServerCase

class OnboardingTests(PolicyServerCase):
    def request(self, body, origin=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        headers={'Host':f'127.0.0.1:{self.port}','Content-Type':'application/json',
                 'Sec-Fetch-Site':'same-origin'}
        if origin: headers['Origin']=origin
        try:
            conn.request('POST','/api/simple/accounts/start-onboarding',json.dumps(body),headers)
            r=conn.getresponse()
            return r.status,json.loads(r.read())
        finally: conn.close()

    def add_disabled(self):
        code,_=self.call('POST','/api/accounts',{'op':'add','id':'account-4',
                     'label':'new@example.com','profile':'account-4','enabled':False})
        assert code==200

    def test_origin_alias_registration_and_disabled_guard(self):
        origin=f'http://127.0.0.1:{self.port}'
        assert self.request({'id':'account-1'})[0]==403
        assert self.request({'id':'account-1'},'https://other.example')[0]==403
        assert self.request({'id':'account-4\\'},origin)[0]==400
        assert self.request({'id':'account-4'},origin)[0]==400
        assert self.request({'id':'account-1'},origin)[0]==409
        self.add_disabled()
        with patch('cmo.server.launch_account_onboarding') as launch:
            assert self.request({'id':'account-4','extra':True},origin)[0]==400
            launch.assert_not_called()

    def test_one_window_no_claim_of_login_or_route(self):
        self.add_disabled();origin=f'http://127.0.0.1:{self.port}'
        fake=type('FakeProcess',(),{'poll':lambda self:None})()
        with TemporaryDirectory() as tmp, patch.dict(os.environ,{'PI_PROFILE_ROOT':tmp}), \
             patch('cmo.server.launch_account_onboarding',return_value=fake) as launch:
            first=self.request({'id':'account-4'},origin)
            again=self.request({'id':'account-4'},origin)
            _,status=self.call('GET','/api/simple/accounts/readiness')
        assert first[0]==202 and first[1]['started']
        assert again[0]==202 and again[1]['already_open']
        assert 'verified' not in json.dumps(first[1]).lower()
        assert any(a['id']=='account-4' and a['setup_running'] and not a['cline_saved']
                   for a in status['accounts'])
        launch.assert_called_once_with('account-4')

    def test_existing_auth_refuses_new_setup(self):
        self.add_disabled();origin=f'http://127.0.0.1:{self.port}'
        with TemporaryDirectory() as tmp:
            agent=Path(tmp)/'account-4'/'agent';agent.mkdir(parents=True)
            (agent/'auth.json').write_text('{"cline":{"token":"private"}}')
            with patch.dict(os.environ,{'PI_PROFILE_ROOT':tmp}), \
                 patch('cmo.server.launch_account_onboarding') as launch:
                code,response=self.request({'id':'account-4'},origin)
        assert code==409 and 'private' not in json.dumps(response)
        launch.assert_not_called()


def test_windows_launch_uses_fixed_script_and_argv():
    from cmo.account_onboarding import launch_account_onboarding
    with patch('cmo.account_onboarding.os.name','nt'), \
         patch('cmo.account_onboarding.HELPER_ROOT') as root, \
         patch('cmo.account_onboarding.subprocess.Popen') as popen:
        root.__truediv__.return_value.is_file.return_value=True
        launch_account_onboarding('account-5')
        args=popen.call_args.args[0]
        assert args[:4]==['powershell.exe','-NoProfile','-NoExit','-File']
        assert args[-2:]==['-Account','account-5']
        assert popen.call_args.kwargs.get('shell',False) is False
    try: launch_account_onboarding('account-5\\')
    except ValueError: pass
    else: raise AssertionError('invalid account alias accepted')
