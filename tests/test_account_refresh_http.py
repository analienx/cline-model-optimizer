"""HTTP renewal gate: one registered profile and explicit same-origin POST."""
from __future__ import annotations
import http.client
import json
from unittest.mock import patch
from test_policy_http import PolicyServerCase

class RenewalHttpTests(PolicyServerCase):
    def post(self, payload, origin=None, site='same-origin'):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        host=f'127.0.0.1:{self.port}'
        headers={'Host':host,'Origin':origin or 'http://'+host,
                 'Content-Type':'application/json','Sec-Fetch-Site':site}
        try:
            conn.request('POST','/api/simple/accounts/refresh-usage',
                         body=json.dumps(payload).encode(),headers=headers)
            reply=conn.getresponse()
            return reply.status,json.loads(reply.read())
        finally:
            conn.close()

    def test_single_registered_profile_only_and_no_secret_response(self):
        with patch('cmo.server.renew_cline',return_value='renewed') as renewal,\
             patch('cmo.server.account_usage',return_value={'account_alias':'account-1',
                    'plan_status':'no-active-plan-confirmed','windows':{}}) as usage:
            status,result=self.post({'id':'account-1'})
            assert status==200 and result['account_alias']=='account-1'
            renewal.assert_called_once_with('account-1')
            usage.assert_called_once()
            assert 'access' not in json.dumps(result)
            assert self.post({'id':'account-99'})[0]==404
            assert self.post({'id':'account-1','provider':'cline-pass'})[0]==400
            assert self.post({'id':'../../account-2'})[0]==400
            renewal.assert_called_once()

    def test_cross_origin_and_cross_site_refused_without_refresh(self):
        with patch('cmo.server.renew_cline') as renewal:
            assert self.post({'id':'account-1'},origin='http://evil.invalid')[0]==403
            assert self.post({'id':'account-1'},site='cross-site')[0]==403
            renewal.assert_not_called()
