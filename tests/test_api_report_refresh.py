"""Read-only report integration on a disposable SQLite fixture, never live data."""
from datetime import timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from unittest.mock import patch
import ast

from tests import test_api_observer as fixtures


class RefreshTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ['HERMES_HOME']) / 'cache/scratch' if os.environ.get('HERMES_HOME') else None
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = fixtures.ObserverTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.snapshot(113, [fixtures.listing(7, 100, 3)])
        self.fixture.snapshot(114, [fixtures.listing(7, 200, 4)])
        self.fixture.db.commit()
        self.db_path = self.root / 'input.sqlite3'
        target = sqlite3.connect(self.db_path)
        self.fixture.db.backup(target)
        target.close()
        self.config_path = self.root / 'config.json'
        self.config_path.write_text('{}', encoding='utf-8')
        self.canonical = self.root / 'api-market-113-114.html'
        self.canonical.write_text('canonical artifact must remain untouched', encoding='utf-8')

    def test_refresh_generates_separate_artifacts_without_writing_inputs_or_ledger(self):
        self.assertIsNotNone(importlib.util.find_spec('api_report_refresh'),
                             'offline report refresh module is missing')
        from api_report_refresh import refresh_report
        protected = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in (self.db_path, self.config_path, self.canonical)}
        result = refresh_report(self.db_path, self.fixture.cfg, self.root / 'hourly',
                                config_path=self.config_path, now=fixtures.NOW, house_ids=[113,114])
        document = json.loads(Path(result['paths']['json']).read_text(encoding='utf-8'))
        self.assertEqual(document['returned_count'], 1)
        self.assertEqual(document['mode'], 'fixture')
        self.assertEqual(document['comparisons'][0]['source_quote']['min_unit_price'], 100)
        self.assertEqual(document['names_status']['status'], 'missing')
        self.assertIn('거래 권고 아님', Path(result['paths']['summary']).read_text(encoding='utf-8'))
        self.assertIn('문서', Path(result['paths']['html']).read_text(encoding='utf-8'))
        status = json.loads(Path(result['paths']['status']).read_text(encoding='utf-8'))
        self.assertEqual(status['report_status'], 'ok')
        self.assertEqual(status['delivery']['status'], 'not_requested')
        self.assertFalse((self.root / 'hourly' / 'delivery-ledger.json').exists())
        for path, digest in protected.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


    def test_output_aliases_to_inputs_and_git_directories_are_rejected(self):
        from api_report_refresh import refresh_report
        from common import AppError
        output = self.root / 'alias-output'
        output.mkdir()
        os.link(self.db_path, output / 'report-latest.json')
        before = self.db_path.read_bytes()
        with self.assertRaises(AppError):
            refresh_report(self.db_path, self.fixture.cfg, output,
                           config_path=self.config_path, now=fixtures.NOW)
        self.assertEqual(before, self.db_path.read_bytes())
        git_root = self.root / 'synthetic-git'
        (git_root / '.git').mkdir(parents=True)
        with self.assertRaises(AppError):
            refresh_report(self.db_path, self.fixture.cfg, git_root / 'reports', now=fixtures.NOW)
        self.assertFalse((git_root / 'reports').exists())


    def test_cli_writes_report_and_missing_input_never_creates_a_database(self):
        self.config_path.write_text('{"game_flavor":"forever"}', encoding='utf-8')
        script = Path(__file__).resolve().parents[1] / 'api_report_refresh.py'
        output = self.root / 'cli-output'
        command = [sys.executable, str(script), '--config', str(self.config_path),
                   '--db', str(self.db_path), '--output-root', str(output), '--houses', '113,114']
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / 'report-latest.html').is_file(), 'CLI did not generate a report')
        self.assertEqual(json.loads(result.stdout)['report_status'], 'ok')
        self.assertFalse((output / 'delivery-ledger.json').exists())
        missing = self.root / 'must-not-create.sqlite3'
        command[command.index('--db')+1] = str(missing)
        failed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse(missing.exists())


    def test_unexpected_cache_error_is_local_diagnostic_and_prices_survive(self):
        from api_report_refresh import refresh_report
        from api_market_observer import build_observations
        baseline = build_observations(self.fixture.db, self.fixture.cfg, now=fixtures.NOW)
        with patch('api_report_refresh.load_item_cache', side_effect=RuntimeError('do-not-leak-fake-secret')):
            result = refresh_report(self.db_path, self.fixture.cfg, self.root / 'cache-error',
                                    cache_path=self.root / 'cache.json', now=fixtures.NOW)
        self.assertEqual(result['report']['names_status']['status'], 'unreadable')
        for key in ('source_quote', 'target_quote', 'gap_copper', 'history', 'evidence'):
            self.assertEqual(result['report']['comparisons'][0][key], baseline['comparisons'][0][key])
        for output in result['paths'].values():
            self.assertNotIn('do-not-leak-fake-secret', Path(output).read_text(encoding='utf-8'))

    def test_failed_atomic_replace_preserves_previous_artifact_and_cleans_temporary(self):
        from api_report_refresh import atomic_write_text
        path = self.root / 'previous.html'
        path.write_text('last complete report', encoding='utf-8')
        with patch.object(Path, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                atomic_write_text(path, 'new report')
        self.assertEqual(path.read_text(encoding='utf-8'), 'last complete report')
        self.assertEqual(list(self.root.glob('.previous.html.*.tmp')), [])

    def test_offline_import_boundaries_do_not_introduce_credential_clients(self):
        root = Path(__file__).resolve().parents[1]
        forbidden = {'forever_item_updater', 'api_report_delivery', 'urllib', 'requests', 'httpx', 'socket'}
        for filename in ('api_market_observer.py', 'api_market_search.py', 'api_report_refresh.py', 'api_report_summary.py'):
            tree = ast.parse((root / filename).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or '').split('.')[0], forbidden, filename)
                    self.assertTrue({'ForeverAuctions','http_json','load_env'}.isdisjoint(
                        alias.name for alias in node.names), filename)
                elif isinstance(node, ast.Import):
                    self.assertTrue(forbidden.isdisjoint(alias.name.split('.')[0] for alias in node.names), filename)


if __name__ == '__main__':
    unittest.main()
