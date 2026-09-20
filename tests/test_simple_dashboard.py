"""Simple dashboard: bounded free-model preferences and account registry."""
from __future__ import annotations
import sys
import os
import json
import tempfile
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
    def test_advanced_uses_the_same_shell_not_legacy_dashboard(self):
        from urllib.request import urlopen
        with urlopen(f"http://127.0.0.1:{self.port}/advanced", timeout=5) as res:
            html = res.read().decode("utf-8")
        assert 'stylesheet" href="/simple.css"' in html
        assert 'script src="/advanced.js"' in html
        assert 'script src="/app.js"' not in html
        for path in ("/advanced.js", "/api/simple/accounts/readiness"):
            assert self.call("GET", path)[0] == 200

    def test_profile_readiness_never_returns_credentials_or_infers_live_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            agent = root / "account-1" / "agent"
            agent.mkdir(parents=True)
            (agent / "auth.json").write_text(json.dumps({
                "cline": {"access": "secret-free-token", "refresh": "secret-refresh"},
                "cline-pass": {"access": "secret-pass-token"}}), encoding="utf-8")
            with patch.dict(os.environ, {"PI_PROFILE_ROOT": temp}):
                status, result = self.call("GET", "/api/simple/accounts/readiness")
        assert status == 200 and result["schema"] == "cmo.account-readiness/v1"
        account = next(a for a in result["accounts"] if a["id"] == "account-1")
        assert account["profile_exists"] and account["cline_saved"] and account["pass_saved"]
        assert account["live_auth_verified"] is False
        assert account["pass_entitlement_verified"] is False
        assert "secret" not in json.dumps(result)
        missing = next(a for a in result["accounts"] if a["id"] == "account-2")
        assert not missing["profile_exists"] and not missing["cline_saved"]

    def test_advanced_copy_does_not_display_fallback_question_marks(self):
        for page in ("advanced.html", "advanced.js", "simple.js"):
            body = (Path(__file__).resolve().parents[1] / "python" / "cmo" / "web" / page).read_text(encoding="utf-8")
            if page == "advanced.html":
                assert " ? " not in body
            assert " ? legacy" not in body
            assert " ? recheck needed" not in body
            assert "View 5h / week / month limits ?" not in body
            assert " ? live login" not in body
    def test_default_and_advanced_pages_exist(self):
        for path, marker in (('/',b'Free router status'),('/advanced',b'Cline Model Optimizer'),('/simple.js',b'function renderRouter')):
            status, _ = self.call('GET',path)
            assert status==200


def test_account_onboarding_has_visible_clipboard_action_and_readable_fallback():
    source=(Path(__file__).resolve().parents[1]/'python/cmo/web/simple.js').read_text(encoding='utf-8')
    css=(Path(__file__).resolve().parents[1]/'python/cmo/web/simple.css').read_text(encoding='utf-8')
    assert 'Copy sign-in command' in source and 'Copy setup command' in source
    assert 'navigator.clipboard.writeText(command)' in source
    assert "document.execCommand('copy')" in source
    assert "outcome='unconfirmed'" in source
    assert 'navigator.clipboard.readText()' in source
    assert 'field.focus();field.select()' in source
    assert "el('textarea','account-command')" in source
    assert 'Automatic copy was not confirmed' in source
    assert 'accountSetupCommand(account.id,stage)' in source
    assert 'account-connect-button' in css
    assert 'Command & manual copy' in source and 'Show command & instructions' in source and 'Refresh setup' in source
    assert 'Run the Pi sign-in helper in PowerShell' not in source
