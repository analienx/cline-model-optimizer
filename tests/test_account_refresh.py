"""Isolated renewal tests: no real provider requests or user profile writes."""
from __future__ import annotations
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from cmo.account_refresh import renew_cline

class RenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,{'PI_PROFILE_ROOT':self.temp.name})
        self.env.start();self.addCleanup(self.env.stop)
        self.profile=Path(self.temp.name)/'account-4'/'agent'
        self.profile.mkdir(parents=True)
        self.auth=self.profile/'auth.json'
        self.original={'cline':{'type':'oauth','access':'old-access','refresh':'old-refresh',
                  'expires':(time.time()-60)*1000},
                  'cline-pass':{'type':'oauth','access':'pass-untouched','refresh':'pass-untouched',
                  'expires':(time.time()+3600)*1000}}
        self.auth.write_text(json.dumps(self.original),encoding='utf-8')

    def test_expired_free_token_renews_with_other_provider_preserved(self):
        from datetime import datetime,timedelta,timezone
        expiry=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        data={'success':True,'data':{'accessToken':'renewed-access',
              'refreshToken':'renewed-refresh','expiresAt':expiry}}
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def read(self,_limit): return json.dumps(data).encode()
        with patch('cmo.account_refresh.urlopen',return_value=Response()) as call:
            assert renew_cline('account-4')=='renewed'
        saved=json.loads(self.auth.read_text())
        assert saved['cline']['access']=='renewed-access'
        assert saved['cline']['refresh']=='renewed-refresh'
        assert saved['cline-pass']==self.original['cline-pass']
        assert call.call_count==1 and not Path(str(self.auth)+'.lock').exists()

    def test_busy_lock_and_invalid_profile_never_touch_auth(self):
        lock=Path(str(self.auth)+'.lock');lock.mkdir()
        with patch('cmo.account_refresh.urlopen') as call:
            assert renew_cline('account-4')=='profile-busy'
            assert renew_cline('account-4\\..\\account-1')=='profile-unavailable'
            call.assert_not_called()
        lock.rmdir()
        assert json.loads(self.auth.read_text())==self.original

    def test_failed_refresh_preserves_original_json(self):
        from urllib.error import HTTPError
        with patch('cmo.account_refresh.urlopen',side_effect=HTTPError(
                'https://api.cline.bot/api/v1/auth/refresh',401,'expired',{},None)):
            assert renew_cline('account-4')=='renewal-unavailable'
        assert json.loads(self.auth.read_text())==self.original
        assert not Path(str(self.auth)+'.lock').exists()

    def test_current_token_performs_no_request_or_write(self):
        self.original['cline']['expires']=(time.time()+3600)*1000
        self.auth.write_text(json.dumps(self.original))
        before=self.auth.read_bytes()
        with patch('cmo.account_refresh.urlopen') as call:
            assert renew_cline('account-4')=='already-current'
            call.assert_not_called()
        assert self.auth.read_bytes()==before

    def test_no_cline_does_not_refresh_optional_pass(self):
        self.auth.write_text(json.dumps({'cline-pass':self.original['cline-pass']}))
        with patch('cmo.account_refresh.urlopen') as call:
            assert renew_cline('account-4')=='sign-in-required'
            call.assert_not_called()

    def test_malformed_provider_response_preserves_login(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*_): return False
            def read(self,_limit): return b'[]'
        with patch('cmo.account_refresh.urlopen',return_value=Response()):
            assert renew_cline('account-4')=='renewal-unavailable'
        assert json.loads(self.auth.read_text())==self.original
        assert not Path(str(self.auth)+'.lock').exists()
