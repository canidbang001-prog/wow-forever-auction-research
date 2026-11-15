"""Scheduler runners: disposable profile and injected process launcher only."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class JobTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ['HERMES_HOME']) / 'cache/scratch' if os.environ.get('HERMES_HOME') else None
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.data = self.home / 'data/wow-forever-auction'
        self.source = self.data / 'report-runtime/test-version'
        self.source.mkdir(parents=True)
        for name in ('api_report_service.py','forever_item_updater.py'):
            (self.source/name).write_text('# never executed by injected launcher', encoding='utf-8')
        (self.data/'report-runtime.json').write_text(json.dumps(dict(schema_version=1,
            source_root=str(self.source), house_ids=[113,114], chat_id=6637003763)), encoding='utf-8')

    def test_jobs_launch_only_the_selected_addon_in_a_bounded_separate_process(self):
        self.assertIsNotNone(importlib.util.find_spec('api_report_jobs'), 'scheduler entry point is missing')
        from api_report_jobs import run_job
        calls = []
        def launcher(command, **kwargs):
            calls.append((command,kwargs))
            return subprocess.CompletedProcess(command, 0, '{"delivery":{"status":"skip_unchanged"}}', 'must-not-print')
        result = run_job('report', home=self.home, launcher=launcher)
        self.assertEqual(result['state'], 'success')
        command, options = calls[0]
        self.assertEqual(Path(command[1]), self.source/'api_report_service.py')
        self.assertIn('--send', command)
        self.assertEqual(command[command.index('--houses')+1], '113,114')
        self.assertEqual(options['timeout'], 150)
        self.assertNotIn('must-not-print', json.dumps(result))
        run_job('metadata', home=self.home, launcher=launcher)
        command = calls[1][0]
        self.assertEqual(Path(command[1]), self.source/'forever_item_updater.py')
        self.assertIn('--max-requests', command)
        self.assertIn('--budget-seconds', command)
        self.assertNotIn('blizzard_collector.py', ' '.join(command))


    def test_same_mode_overlap_is_skipped_without_launching_a_second_process(self):
        from api_report_jobs import run_job
        nested = []
        def launcher(command, **options):
            nested.append(run_job('metadata', home=self.home, launcher=lambda *a,**k:
                subprocess.CompletedProcess(a, 0, '', '')))
            return subprocess.CompletedProcess(command, 0, '', '')
        run_job('metadata', home=self.home, launcher=launcher)
        self.assertEqual(nested[0]['state'], 'skipped_overlap')


    def test_copied_cron_entrypoint_uses_its_basename_and_writes_sanitized_status(self):
        from unittest.mock import patch
        import api_report_jobs as module
        self.assertTrue(hasattr(module, 'main'), 'cron main entrypoint is missing')
        with patch.object(module, 'run_job', return_value={'state':'success','returncode':0}) as runner:
            with patch.object(module, '__file__', str(self.home/'wow_forever_metadata.py')):
                self.assertEqual(module.main(), 0)
        runner.assert_called_once_with('metadata')


if __name__ == '__main__':
    unittest.main()
