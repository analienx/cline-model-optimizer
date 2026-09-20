"""Simple dashboard: bounded free-model preferences and account registry."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_policy_http import PolicyServerCase

MUSE='cline-free/muse-spark-1.3-contributor'
GLM='z-ai/glm-5.3-flash'
DEEP='cline-free/deepseek-v4.1-flash'
SOLAR='cline-free/solar-pro4'

class SimpleDashboardTests(PolicyServerCase):
    def test_model_order_four_limit_and_free_catalog_gate(self):
        _, snap=self.call('GET','/api/snapshot')
        catalog={'models':{SOLAR:'available'},'buckets':{'free':[SOLAR]},'source_url':'test'}
        with patch('cmo.server.fetch_catalog',return_value=catalog):
            status, result=self.call('POST','/api/simple/models',{'models':[SOLAR,GLM,MUSE,DEEP], 'expected_digest':snap['policy']['digest']})
        assert status==200 and result['ok']
        _, changed=self.call('GET','/api/snapshot')
        enabled=[r['model'] for r in changed['policy']['routes'] if r['tier']=='free' and r.get('enabled',True)]
        assert enabled==[SOLAR,GLM,MUSE,DEEP]
        assert changed['policy']['defaults']['max_enabled_free_models']==4
        status,_=self.call('POST','/api/simple/models',{'models':[SOLAR,GLM,MUSE,DEEP,'other']})
        assert status==400
        status,_=self.call('POST','/api/simple/models',{'models':[SOLAR,SOLAR]})
        assert status==400
    def test_five_accounts_and_nonverifying_email_label(self):
        for n in (4,5):
            status, result=self.call('POST','/api/accounts', {'op':'add','id':f'account-{n}',
                'label':f'contact{n}@example.test','profile':f'account-{n}'})
            assert status==200 and result['ok']
        status, denied=self.call('POST','/api/accounts', {'op':'add','id':'account-6',
            'label':'six@example.test','profile':'account-6'})
        assert status==400 and 'five' in denied['error']
        _, snap=self.call('GET','/api/snapshot')
        assert len(snap['policy']['accounts'])==5
        account5=next(a for a in snap['policy']['accounts'] if a['id']=='account-5')
        assert account5['label']=='contact5@example.test'
        assert account5['status']=='tracked'
    def test_default_and_advanced_pages_exist(self):
        for path, marker in (('/',b'Free router status'),('/advanced',b'Cline Model Optimizer'),('/simple.js',b'function renderRouter')):
            status, _ = self.call('GET',path)
            assert status==200
