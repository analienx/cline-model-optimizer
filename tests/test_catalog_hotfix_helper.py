"""Bounded CMO release helper must fail closed before any runtime mutation."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'tools/install/Promote-CmoCatalogHotfix.py'
spec = importlib.util.spec_from_file_location('cmo_hotfix', MODULE)
hotfix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hotfix)


class HotfixGuardTests(unittest.TestCase):
    def test_plan_is_hash_bound_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = {'old_pid': 111, 'old_digest': hotfix.OLD_SHA,
                        'new_digest': hotfix.NEW_SHA}
            with patch.object(hotfix, 'AUDIT', Path(tmp)), \
                 patch.object(hotfix, 'identity', return_value=expected), \
                 patch('builtins.print'):
                hotfix.plan()
            plans = list(Path(tmp).glob('cmo-catalog-hotfix-plan-*.json'))
            self.assertEqual(len(plans), 1)
            record = json.loads(plans[0].read_text())
            self.assertEqual(record['expected'], expected)
            self.assertEqual(record['operation'], 'catalog-artifact-only')
            self.assertEqual(hotfix.sha(plans[0]), hashlib.sha256(plans[0].read_bytes()).hexdigest())
    def test_wrong_plan_hash_never_inspects_or_changes_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cmo-catalog-hotfix-plan-test.json'
            path.write_text('{}')
            with patch.object(hotfix, 'AUDIT', Path(tmp)), \
                 patch.object(hotfix, 'identity') as identity:
                with self.assertRaisesRegex(RuntimeError, 'plan path or content hash'):
                    hotfix.apply(path, '0' * 64)
                identity.assert_not_called()

    def test_exact_pid_identity_required_before_stop(self):
        with patch.object(hotfix, 'command', return_value={
                 'ProcessId': 999, 'Name': 'python.exe',
                 'CommandLine': 'python other.py'}), \
             patch.object(hotfix.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'PID identity drift'):
                hotfix.stop_exact(999, hotfix.OLD_SHA)
            run.assert_not_called()

    def test_unknown_new_artifact_refused(self):
        with patch.object(hotfix, 'sha', side_effect=['old', 'new']):
            with self.assertRaisesRegex(RuntimeError, 'artifact differs'):
                hotfix.identity()


if __name__ == '__main__':
    unittest.main()
