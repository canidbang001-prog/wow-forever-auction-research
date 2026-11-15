"""Orchestration contracts: fake sender only, no live credentials or network."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import unittest

from tests import test_api_report_refresh as fixtures


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefreshTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_report_is_written_before_explicit_send_and_status_records_outcome(self):
        self.assertIsNotNone(importlib.util.find_spec('api_report_service'),
                             'report/delivery orchestration is missing')
        from api_report_service import run_report_service
        output = self.fixture.root / 'service-output'
        sent = []
        def sender(summary):
            self.assertTrue((output / 'report-latest.html').is_file())
            self.assertTrue((output / 'report-latest.json').is_file())
            sent.append(summary)
            return dict(status='success', message_id=123)
        result = run_report_service(self.fixture.db_path, self.fixture.fixture.cfg, output,
                                    config_path=self.fixture.config_path, now=fixtures.fixtures.NOW,
                                    sender=sender)
        self.assertEqual(result['delivery']['status'], 'sent')
        self.assertEqual(len(sent), 1)
        status = json.loads((output / 'report-status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['report_status'], 'ok')
        self.assertEqual(status['delivery']['status'], 'sent')
        repeated = run_report_service(self.fixture.db_path, self.fixture.fixture.cfg, output,
                                      config_path=self.fixture.config_path, now=fixtures.fixtures.NOW,
                                      sender=sender)
        self.assertEqual(repeated['delivery']['status'], 'skip_unchanged')
        self.assertEqual(len(sent), 1)


    def test_fixture_acknowledgement_does_not_suppress_live_after_cooldown(self):
        from contextlib import closing
        from datetime import timedelta
        import sqlite3
        from api_report_service import run_report_service
        output = self.fixture.root / 'origin-isolation'
        sent = []
        def fake_sender(summary):
            sent.append(summary)
            return dict(status='success', message_id=len(sent))
        first = run_report_service(self.fixture.db_path, self.fixture.fixture.cfg, output,
                                   now=fixtures.fixtures.NOW, sender=fake_sender, max_age_hours=24)
        self.assertEqual(first['report']['mode'], 'fixture')
        self.assertEqual(first['delivery']['status'], 'sent')
        ledger = json.loads((output / 'delivery-ledger.json').read_text(encoding='utf-8'))
        self.assertEqual(ledger['last_acked_fingerprint'], first['fingerprint'])
        # Only the disposable fixture's origin changes; prices and source IDs do not.
        with closing(sqlite3.connect(self.fixture.db_path)) as db, db:
            db.execute("UPDATE meta SET value='live' WHERE key='data_mode'")
        second = run_report_service(self.fixture.db_path, self.fixture.fixture.cfg, output,
                                    now=fixtures.fixtures.NOW + timedelta(hours=6), sender=fake_sender,
                                    max_age_hours=24)
        self.assertEqual(second['report']['mode'], 'live')
        self.assertEqual(second['delivery']['status'], 'sent')
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])
        self.assertEqual(len(sent), 2)
        ledger = json.loads((output / 'delivery-ledger.json').read_text(encoding='utf-8'))
        self.assertEqual(ledger['last_acked_fingerprint'], second['fingerprint'])


    def test_real_telegram_sender_requires_live_origin_before_any_send_or_ledger(self):
        from contextlib import closing
        import sqlite3
        from unittest.mock import Mock
        from api_report_service import TelegramSender, run_report_service
        opener = Mock(side_effect=AssertionError('offline regression: no network'))
        real_sender = TelegramSender('9:synthetic_not_a_real_credential', 123, opener=opener)
        for mode in ('fixture', 'unknown', None, 'live'):
            with self.subTest(mode=mode):
                with closing(sqlite3.connect(self.fixture.db_path)) as db, db:
                    db.execute("DELETE FROM meta WHERE key='data_mode'")
                    if mode is not None:
                        db.execute("INSERT INTO meta(key,value) VALUES('data_mode',?)", (mode,))
                output = self.fixture.root / ('real-sender-' + str(mode))
                # Spy on the real class's callable seam so even RED cannot use HTTP.
                with patch.object(TelegramSender, '__call__',
                                  return_value=dict(status='success', message_id=1)) as send:
                    result = run_report_service(self.fixture.db_path, self.fixture.fixture.cfg, output,
                                                now=fixtures.fixtures.NOW, sender=real_sender)
                if mode == 'live':
                    self.assertEqual(result['delivery']['status'], 'sent')
                    send.assert_called_once_with(result['summary'])
                else:
                    self.assertEqual(result['delivery']['status'], 'nonlive_blocked')
                    send.assert_not_called()
                    self.assertFalse((output / 'delivery-ledger.json').exists())
                    self.assertFalse((output / 'delivery.lock').exists())
                self.assertTrue((output / 'report-latest.html').is_file())
                status = json.loads((output / 'report-status.json').read_text(encoding='utf-8'))
                self.assertEqual(status['delivery'], result['delivery'])
        opener.assert_not_called()


    def test_opt_in_factory_reads_only_selected_token_without_exposing_other_keys(self):
        import api_report_service as module
        self.assertTrue(hasattr(module, 'configured_telegram_sender'), 'opt-in sender factory is missing')
        fake = '9' + ':' + 'fixture_only_not_a_real_credential'
        path = self.fixture.root / 'synthetic.env'
        path.write_text('UNRELATED_SECRET=must_not_be_loaded\nTELEGRAM_BOT_TOKEN="' + fake + '"\n', encoding='utf-8')
        environment = {}
        with patch('api_report_service.TelegramSender') as factory:
            sender = module.configured_telegram_sender(6637003763, env_path=path, environ=environment)
        factory.assert_called_once_with(fake, 6637003763)
        self.assertIs(sender, factory.return_value)
        self.assertEqual(environment, {})
        self.assertIsNone(module.configured_telegram_sender(6637003763, env_path=self.fixture.root/'absent.env', environ={}))
        self.assertIsNone(module.configured_telegram_sender(6637003763, environ={'TELEGRAM_BOT_TOKEN':'invalid'}))


    def test_service_cli_generates_locally_without_implicit_credential_read_or_send(self):
        root = self.fixture.root
        self.fixture.config_path.write_text('{"game_flavor":"forever"}', encoding='utf-8')
        script = Path(__file__).resolve().parents[1] / 'api_report_service.py'
        output = root / 'service-cli'
        command = [sys.executable, str(script), '--config', str(self.fixture.config_path),
                   '--db', str(self.fixture.db_path), '--output-root', str(output), '--houses', '113,114']
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / 'report-latest.html').is_file(), 'service CLI did not run')
        self.assertEqual(json.loads(result.stdout)['delivery']['status'], 'telegram_unconfigured')
        self.assertFalse((output / 'delivery-ledger.json').exists())
        result = subprocess.run(command + ['--send'], capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertNotEqual(result.returncode, 0, 'send must require an explicit chat ID')


if __name__ == '__main__':
    unittest.main()
