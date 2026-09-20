"""Resume action must fail closed and never convert a request into a false success."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_policy_http import PolicyServerCase
from cmo.resume import ResumeRejected, request_resume

class ResumeDashboardTests(PolicyServerCase):
    def test_requires_only_a_goal_id(self):
        for payload in ({},{'goal_id':123},{'goal_id':'one','command':'start'}):
            status,_=self.call('POST','/api/simple/resume',payload)
            assert status==400
    def test_blocked_route_does_not_launch_anything(self):
        with patch('cmo.resume.subprocess.Popen') as launcher:
            try:
                request_resume('1452826a-661e-4a0d-b88a-6054c2de5a25',
                    {'action':'BLOCKED','free_only':True,'route':None})
            except ResumeRejected as exc:
                assert 'No eligible free-only route' in str(exc)
            else:
                raise AssertionError('blocked route was incorrectly accepted')
            launcher.assert_not_called()
    def test_api_distinguishes_request_from_confirmed_execution(self):
        result={'ok':True,'status':'requested','goal_id':'known','message':'Await verified handshake'}
        with patch('cmo.server.request_resume',return_value=result) as resume:
            status,body=self.call('POST','/api/simple/resume',{'goal_id':'known'})
        assert status==202 and body['status']=='requested'
        assert 'running' not in body.values()
        resume.assert_called_once()
