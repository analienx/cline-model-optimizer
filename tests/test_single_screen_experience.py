"""One-screen dashboard: preserve truthful free-model and account-wide usage evidence."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'python' / 'cmo' / 'web'


def test_advanced_navigation_is_retired_without_losing_evidence():
    html = (ROOT / 'simple.html').read_text(encoding='utf-8')
    retired = (ROOT / 'advanced.html').read_text(encoding='utf-8')
    assert 'href="/advanced"' not in html
    assert 'id="router-evidence"' in html
    assert html.index('src="/simple.js"') < html.index('src="/advanced.js"')
    assert 'http-equiv="refresh" content="0;url=/"' in retired


def test_accounts_and_sessions_have_disclosures_and_no_invented_free_percent():
    source = (ROOT / 'advanced.js').read_text(encoding='utf-8')
    assert 'new Set()' in source and 'account-disclosure' in source
    assert "rows.length <= 5" in source and 'Show first five' in source
    assert 'Free model availability' in source
    assert 'Plan usage · account-wide' in source
    assert 'remaining free quota not reported' in source
    assert "category === 'exhausted'" in source
    assert "state.usage[account.id]?.plan_status === 'active'" in source
    assert 'The last model check failed sign-in' in source
    assert 'Request free check' in source
    assert "tier: 'free'" in source
    assert '/api/simple/resume' not in source


def test_single_screen_keeps_account_setup_and_saved_goal_controls():
    basic = (ROOT / 'simple.js').read_text(encoding='utf-8')
    assert 'Start sign-in on Windows' in basic
    assert 'Check & resume' in basic
    assert 'renderAccountRemoval' in basic
