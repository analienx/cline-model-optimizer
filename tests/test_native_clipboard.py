"""Account copy endpoint requires a registered identity and same-origin user action."""
import http.client
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_policy_http import PolicyServerCase

class NativeClipboardTests(PolicyServerCase):
    def post_copy(self, data, *, origin=None, content_type='application/json'):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        headers={'Host':f'127.0.0.1:{self.port}', 'Content-Type':content_type}
        if origin is not None: headers['Origin']=origin
        try:
            conn.request('POST','/api/simple/accounts/copy-command',
                         json.dumps(data),headers)
            res=conn.getresponse();return res.status,json.loads(res.read())
        finally: conn.close()

    def test_copy_rejects_missing_or_cross_site_origin(self):
        payload={'id':'account-1','stage':'provision'}
        assert self.post_copy(payload)[0]==403
        assert self.post_copy(payload,origin='https://bad.example')[0]==403
        assert self.post_copy(payload,origin='http://127.0.0.1:9999')[0]==403

    def test_registered_account_and_stage_are_required(self):
        origin=f'http://127.0.0.1:{self.port}'
        with TemporaryDirectory() as temp, patch.dict('os.environ', {'PI_PROFILE_ROOT':temp}):
            for id in ('account-4','account-1;shutdown','account-99'):
                assert self.post_copy({'id':id,'stage':'provision'},origin=origin)[0]==400
            assert self.post_copy({'id':'account-1','stage':'signin'},origin=origin)[0]==409

    def test_exact_command_is_written_without_execution_or_secret(self):
        origin=f'http://127.0.0.1:{self.port}'
        with TemporaryDirectory() as temp, patch.dict('os.environ', {'PI_PROFILE_ROOT':temp}), \
             patch('cmo.server.command_for',return_value='approved fixed account-1 setup command') as command, \
             patch('cmo.server.copy_native_text',return_value=True) as write:
            status,body=self.post_copy({'id':'account-1','stage':'provision'},origin=origin)
        assert status==200 and body=={'ok':True,'confirmed':True,'message':'Windows clipboard verified'}
        command.assert_called_once_with('account-1','provision')
        write.assert_called_once_with('approved fixed account-1 setup command')
        assert 'command' not in body and 'secret' not in json.dumps(body)

    def test_native_failure_does_not_claim_success(self):
        origin=f'http://127.0.0.1:{self.port}'
        with TemporaryDirectory() as temp, patch.dict('os.environ', {'PI_PROFILE_ROOT':temp}), \
             patch('cmo.server.command_for',return_value='safe'), \
             patch('cmo.server.copy_native_text',return_value=False):
            status,body=self.post_copy({'id':'account-1','stage':'provision'},origin=origin)
        assert status==503 and body['ok'] is False and body['confirmed'] is False
