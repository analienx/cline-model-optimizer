"""ClinePass entitlement is verified per profile, never inferred from login."""
from __future__ import annotations
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from cmo.account_usage import account_usage

class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = patch.dict(os.environ, {'PI_PROFILE_ROOT': self.temp.name})
        self.patch.start();self.addCleanup(self.patch.stop)
        self.alias='account-4'
        self.file=Path(self.temp.name)/self.alias/'agent'/'auth.json'
        self.file.parent.mkdir(parents=True)

    def credentials(self, providers=None, expired=False):
        values=providers or ['cline']
        self.file.write_text(json.dumps({key:{'access':'private-token','expires':
            (time.time()-60 if expired else time.time()+3600)*1000} for key in values}),encoding='utf-8')

    def test_free_login_can_verify_active_plan_without_pass_provider(self):
        self.credentials(['cline'])
        me=(200,{'email':'test@example.test'})
        plan=(200,{'subscriptionId':'sub-1','currentPeriodEnd':'2099-01-01T00:00:00Z',
                   'plan':{'isActive':True}})
        windows=(200,{'limits':[{'type':'five_hour','percentUsed':14},
            {'type':'weekly','percentUsed':51},{'type':'monthly','percentUsed':72}]})
        with patch('cmo.account_usage._fetch',side_effect=[me,plan,windows]) as fetch:
            result=account_usage(self.alias,self.alias,'test@example.test')
        assert result['plan_status']=='active' and result['windows']['5h']['percent_used']==14
        assert result['windows']['weekly']['percent_used']==51
        assert len(fetch.call_args_list)==3 and 'private-token' not in str(result)

    def test_saved_pass_login_never_implies_a_subscription(self):
        self.credentials(['cline-pass'])
        with patch('cmo.account_usage._fetch',side_effect=[(200,{'email':'test@example.test'}),(200,{'plan':None,'subscriptionId':None})]) as fetch:
            result=account_usage(self.alias,self.alias,'test@example.test')
        assert result['plan_status']=='no-active-plan-confirmed'
        assert result['windows']=={} and len(fetch.call_args_list)==2

    def test_404_does_not_claim_no_subscription(self):
        self.credentials(['cline-pass'])
        with patch('cmo.account_usage._fetch',side_effect=[(200,{'email':'test@example.test'}),(404,None)]):
            result=account_usage(self.alias,self.alias,'test@example.test')
        assert result['plan_status']=='provider-unavailable' and result['windows']=={}

    def test_mismatched_email_never_displays_someone_elses_usage(self):
        self.credentials()
        with patch('cmo.account_usage._fetch',return_value=(200,{'email':'other@example.test'})) as fetch:
            result=account_usage(self.alias,self.alias,'test@example.test')
        assert result['plan_status']=='identity-mismatch' and not result['windows']
        fetch.assert_called_once()
        assert 'other@example.test' not in json.dumps(result)

    def test_expired_access_token_does_not_require_paid_login_or_probe(self):
        self.credentials(expired=True)
        with patch('cmo.account_usage._fetch') as fetch:
            result=account_usage(self.alias,self.alias,'test@example.test')
        assert result['plan_status']=='authentication-expired' and result['windows']=={}
        fetch.assert_not_called()

class UsageHttpTests(unittest.TestCase):
    def test_registered_account_only_and_no_credentials_in_http_response(self):
        from test_policy_http import PolicyServerCase
        class ServerCase(PolicyServerCase): pass
        case=ServerCase('runTest');case.setUp()
        try:
            assert case.call('GET','/api/simple/accounts/usage?id=account-99')[0]==404
            with patch('cmo.server.account_usage',return_value={
                'account_alias':'account-1','plan_status':'active',
                'windows':{'weekly':{'percent_used':57}},'checked_at':42}) as read:
                status,result=case.call('GET','/api/simple/accounts/usage?id=account-1')
                assert status==200 and result['windows']['weekly']['percent_used']==57
                assert 'private-token' not in json.dumps(result)
                read.assert_called_once()
        finally:case.doCleanups()
