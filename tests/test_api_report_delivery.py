"""Offline delivery contract tests; all state stays in Hermes scratch."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SCRATCH = Path(os.environ.get('TMPDIR') or 'C:/Users/home/AppData/Local/hermes/cache/scratch')
NOW = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)
FP = 'a' * 64
OTHER_FP = 'b' * 64


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH, prefix='delivery-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'

    def deliver(self, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec('api_report_delivery'),
                             'delivery module must implement public API')
        from api_report_delivery import deliver_summary
        values = dict(now=NOW)
        values.update(kwargs)
        return deliver_summary(self.root, FP, 'plain **text**', **values)

    def test_unconfigured_does_not_send_or_ack(self):
        self.assertEqual(self.deliver()['status'], 'telegram_unconfigured')
        self.assertFalse((self.root / 'delivery-ledger.json').exists())

    def read_ledger(self):
        return json.loads((self.root / 'delivery-ledger.json').read_text('utf-8'))

    def test_success_persists_ack_and_only_safe_fields(self):
        calls = []
        def sender(text):
            calls.append(text)
            return {'status': 'success', 'message_id': 7, 'token': 'NEVER_STORE'}
        self.assertEqual(self.deliver(sender=sender), {'status': 'sent', 'message_id': 7})
        self.assertEqual(calls, ['plain **text**'])
        self.assertEqual(self.read_ledger(), {
            'schema_version': 1,
            'last_acked_fingerprint': FP, 'last_acked_at': NOW.isoformat(),
            'last_attempt_fingerprint': FP, 'last_attempt_at': NOW.isoformat(),
            'last_attempt_outcome': 'success',
            'uncertain_fingerprint': None, 'uncertain_at': None,
            'consecutive_failures': 0, 'backoff_until': None,
        })

    def test_acked_fingerprint_is_never_resent(self):
        calls = []
        sender = lambda text: calls.append(text) or {'status': 'success', 'message_id': 9}
        self.deliver(sender=sender)
        self.assertEqual(self.deliver(sender=sender, now=NOW + timedelta(days=2))['status'],
                         'skip_unchanged')
        self.assertEqual(len(calls), 1)

    def test_changed_fingerprint_waits_for_ack_cooldown(self):
        from api_report_delivery import deliver_summary
        calls = []
        sender = lambda text: calls.append(text) or {'status': 'success', 'message_id': 11}
        self.deliver(sender=sender)
        self.assertEqual(deliver_summary(self.root, OTHER_FP, 'changed', sender=sender,
                         now=NOW + timedelta(seconds=21599))['status'], 'skip_cooldown')
        self.assertEqual(len(calls), 1)
        self.assertEqual(deliver_summary(self.root, OTHER_FP, 'changed', sender=sender,
                         now=NOW + timedelta(seconds=21600))['status'], 'sent')

    def test_attempt_is_durable_before_sender_runs(self):
        def sender(text):
            ledger = self.read_ledger()
            self.assertEqual(ledger['last_attempt_outcome'], 'in_progress')
            self.assertEqual(ledger['last_attempt_fingerprint'], FP)
            self.assertEqual(ledger['last_attempt_at'], NOW.isoformat())
            self.assertIsNone(ledger['last_acked_fingerprint'])
            return {'status': 'success', 'message_id': 3}
        self.assertEqual(self.deliver(sender=sender)['status'], 'sent')

    def test_failure_uses_exponential_backoff_across_fingerprints(self):
        from api_report_delivery import deliver_summary
        sender = lambda text: {'status': 'failure', 'message_id': 1, 'error': 'RAW_SECRET'}
        clock = NOW
        for index, expected in enumerate((1800, 3600, 7200, 14400, 21600, 21600), 1):
            fp = ('a' if index % 2 else 'b') * 64
            result = deliver_summary(self.root, fp, 'text', now=clock, sender=sender)
            self.assertEqual(result, {'status': 'failed'})
            ledger = self.read_ledger()
            self.assertIsNone(ledger['last_acked_fingerprint'])
            self.assertEqual(ledger['last_attempt_outcome'], 'failure')
            self.assertEqual(ledger['consecutive_failures'], index)
            next_try = clock + timedelta(seconds=expected)
            self.assertEqual(ledger['backoff_until'], next_try.isoformat())
            self.assertEqual(deliver_summary(self.root, OTHER_FP, 'other', sender=sender,
                             now=next_try - timedelta(seconds=1))['status'], 'skip_backoff')
            self.assertNotIn('RAW_SECRET', json.dumps(ledger))
            clock = next_try

    def test_uncertain_outcomes_never_ack_or_leak_errors(self):
        from api_report_delivery import deliver_summary
        import contextlib
        import io
        outcomes = [None, {}, {'status': 'uncertain'}, {'status': 'mystery'},
                    {'status': 'success', 'message_id': True},
                    {'status': 'success', 'message_id': 0},
                    {'status': 'success', 'message_id': 1.5},
                    {'status': 'success', 'message_id': '7'},
                    TimeoutError('RAW_SECRET'), ConnectionError('RAW_SECRET'),
                    RuntimeError('RAW_SECRET')]
        for index, outcome in enumerate(outcomes):
            with self.subTest(index=index):
                root = self.root / str(index)
                def sender(text):
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = deliver_summary(root, FP, 'text', now=NOW, sender=sender)
                self.assertEqual(result, {'status': 'uncertain'})
                ledger = json.loads((root / 'delivery-ledger.json').read_text('utf-8'))
                self.assertIsNone(ledger['last_acked_fingerprint'])
                self.assertEqual(ledger['uncertain_fingerprint'], FP)
                self.assertEqual(ledger['uncertain_at'], NOW.isoformat())
                self.assertEqual(ledger['last_attempt_outcome'], 'uncertain')
                self.assertEqual(output.getvalue(), '')
                self.assertNotIn('RAW_SECRET', json.dumps(ledger))

    def test_uncertain_suppresses_all_fingerprints_for_six_hours(self):
        from api_report_delivery import deliver_summary
        self.deliver(sender=lambda text: {'status': 'uncertain'})
        before = (self.root / 'delivery-ledger.json').read_bytes()
        calls = []
        sender = lambda text: calls.append(text) or {'status': 'success', 'message_id': 5}
        for fp in (FP, OTHER_FP):
            self.assertEqual(deliver_summary(self.root, fp, 'text', sender=sender,
                             now=NOW + timedelta(seconds=21599))['status'], 'skip_uncertain')
        self.assertEqual(calls, [])
        self.assertEqual((self.root / 'delivery-ledger.json').read_bytes(), before)
        self.assertEqual(deliver_summary(self.root, OTHER_FP, 'text', sender=sender,
                         now=NOW + timedelta(seconds=21600))['status'], 'sent')
        self.assertIsNone(self.read_ledger()['uncertain_fingerprint'])
        self.assertIsNone(self.read_ledger()['uncertain_at'])

    def test_success_resets_failure_backoff(self):
        self.deliver(sender=lambda text: {'status': 'failure'})
        self.assertEqual(self.deliver(sender=lambda text: {'status': 'success', 'message_id': 2},
                         now=NOW + timedelta(seconds=1800))['status'], 'sent')
        self.assertEqual(self.read_ledger()['consecutive_failures'], 0)
        self.assertIsNone(self.read_ledger()['backoff_until'])

    def test_invalid_inputs_are_rejected_before_io_or_send(self):
        from api_report_delivery import deliver_summary
        calls = []
        sender = lambda text: calls.append(text) or {'status': 'success', 'message_id': 4}
        invalids = [dict(fingerprint='A' * 64), dict(fingerprint='a' * 63),
                    dict(fingerprint='g' * 64), dict(fingerprint=None),
                    dict(summary=None), dict(summary='x' * 3501), dict(summary=''),
                    dict(now=datetime(2026, 1, 1)), dict(now='2026'),
                    dict(cooldown_seconds=True), dict(backoff_seconds=-1),
                    dict(in_progress_seconds=0)]
        for update in invalids:
            with self.subTest(update=update):
                args = dict(root=self.root, fingerprint=FP, summary='text', now=NOW,
                            sender=sender)
                args.update(update)
                self.assertEqual(deliver_summary(**args), {'status': 'invalid_input'})
        self.assertEqual(calls, [])
        self.assertFalse(self.root.exists())

    def test_invalid_ledger_fails_closed_without_rewriting(self):
        from api_report_delivery import deliver_summary
        self.deliver(sender=lambda text: {'status': 'success', 'message_id': 1})
        baseline = self.read_ledger()
        variants = ['{', '[]', '{"schema_version":1,"schema_version":1}',
                    json.dumps(dict(baseline, schema_version=True)),
                    json.dumps(dict(baseline, consecutive_failures=True)),
                    json.dumps(dict(baseline, extra='SECRET')),
                    json.dumps(dict(baseline, last_acked_at='bad')),
                    json.dumps(dict(baseline, last_acked_at='2026-11-15T00:00:00')),
                    json.dumps(dict(baseline, last_acked_at=(NOW + timedelta(days=1)).isoformat())),
                    json.dumps(dict(baseline, last_acked_fingerprint='z' * 64)),
                    json.dumps(dict(baseline, last_attempt_outcome='mystery')),
                    json.dumps(dict(baseline, last_attempt_at=None)),
                    json.dumps(dict(baseline, last_acked_fingerprint=None)),
                    json.dumps(dict(baseline, uncertain_fingerprint=FP)),
                    json.dumps(dict(baseline, backoff_until=NOW.isoformat())),
                    json.dumps({k: v for k, v in baseline.items() if k != 'schema_version'}),
                    'x' * 20000]
        for index, raw in enumerate(variants):
            with self.subTest(index=index):
                root = self.root / str(index)
                root.mkdir()
                path = root / 'delivery-ledger.json'
                path.write_text(raw, encoding='utf-8')
                before = path.read_bytes()
                calls = []
                sender = lambda text: calls.append(text) or {'status': 'success', 'message_id': 2}
                result = deliver_summary(root, OTHER_FP, 'text', sender=sender, now=NOW)
                self.assertEqual(result, {'status': 'ledger_invalid'})
                self.assertEqual(calls, [])
                self.assertEqual(path.read_bytes(), before)

    def test_crashed_in_progress_has_grace_then_uncertain_cooldown(self):
        class SimulatedCrash(BaseException):
            pass
        def crash(text):
            raise SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            self.deliver(sender=crash)
        self.assertEqual(self.read_ledger()['last_attempt_outcome'], 'in_progress')
        calls = []
        sender = lambda text: calls.append(text) or {'status': 'success', 'message_id': 8}
        self.assertEqual(self.deliver(sender=sender, now=NOW + timedelta(seconds=299))['status'],
                         'skip_in_progress')
        self.assertEqual(self.deliver(sender=sender, now=NOW + timedelta(seconds=300))['status'],
                         'skip_uncertain')
        self.assertEqual(self.read_ledger()['uncertain_at'], NOW.isoformat())
        self.assertEqual(self.read_ledger()['last_attempt_outcome'], 'uncertain')
        self.assertEqual(calls, [])
        self.assertEqual(self.deliver(sender=sender, now=NOW + timedelta(seconds=21600))['status'],
                         'sent')

    def test_overlap_allows_only_one_sender(self):
        from api_report_delivery import deliver_summary
        import concurrent.futures
        import threading
        entered, release = threading.Event(), threading.Event()
        calls = []
        def sender(text):
            calls.append(text)
            entered.set()
            if not release.wait(5):
                raise AssertionError('test release timeout')
            return {'status': 'success', 'message_id': 15}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(deliver_summary, self.root, FP, 'one', now=NOW, sender=sender)
            try:
                self.assertTrue(entered.wait(5))
                second = deliver_summary(self.root, OTHER_FP, 'two', now=NOW, sender=sender)
                self.assertEqual(second, {'status': 'delivery_locked'})
                self.assertEqual(calls, ['one'])
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)['status'], 'sent')
        lock = self.root / 'delivery.lock'
        self.assertTrue(lock.exists(), 'persistent lock inode must not be unlinked')

    def test_root_must_be_outside_git_and_protected_inputs(self):
        from api_report_delivery import deliver_summary
        base = Path(self.temp.name)
        repository = base / 'repo'
        (repository / '.git').mkdir(parents=True)
        protected = base / 'inputs'
        protected.mkdir()
        cases = [(repository / 'out', ()),
                 (protected / 'out', (protected,)),
                 (base / 'container', (base / 'container' / 'input.db',))]
        calls = []
        for root, paths in cases:
            with self.subTest(root=root):
                result = deliver_summary(root, FP, 'text', now=NOW, protected_paths=paths,
                                         sender=lambda text: calls.append(text) or
                                         {'status': 'success', 'message_id': 1})
                self.assertEqual(result, {'status': 'path_invalid'})
                self.assertFalse(root.exists())
        self.assertEqual(calls, [])

    def test_hardlink_aliases_of_inputs_and_sidecars_are_rejected(self):
        from api_report_delivery import deliver_summary
        base = Path(self.temp.name)
        db = base / 'input.db'
        db.write_text('{}', encoding='utf-8')
        wal = base / 'input.db-wal'
        wal.write_text('{}', encoding='utf-8')
        for index, (name, source) in enumerate((
                ('delivery-ledger.json', db), ('delivery.lock', db),
                ('delivery-ledger.json', wal), ('delivery.lock', wal))):
            with self.subTest(name=name, source=source.name):
                root = base / str(index)
                root.mkdir()
                os.link(source, root / name)
                before = source.read_bytes()
                calls = []
                result = deliver_summary(root, FP, 'text', now=NOW, protected_paths=(db,),
                                         sender=lambda text: calls.append(text) or
                                         {'status': 'success', 'message_id': 1})
                self.assertEqual(result, {'status': 'path_invalid'})
                self.assertEqual(calls, [])
                self.assertEqual(source.read_bytes(), before)
        self.root.mkdir()
        ledger = self.root / 'delivery-ledger.json'
        ledger.write_text('{}', encoding='utf-8')
        os.link(ledger, self.root / 'delivery.lock')
        self.assertEqual(self.deliver(sender=lambda text: {'status': 'success', 'message_id': 1}),
                         {'status': 'path_invalid'})

    def test_output_symlink_escape_is_rejected(self):
        from api_report_delivery import deliver_summary
        base = Path(self.temp.name)
        outside = base / 'outside'
        outside.mkdir()
        for name in ('delivery-ledger.json', 'delivery.lock'):
            with self.subTest(name=name):
                root = base / name
                root.mkdir()
                target = outside / name
                target.mkdir()
                (target / 'sentinel').write_text('{}', encoding='utf-8')
                try:
                    (root / name).symlink_to(target, target_is_directory=True)
                except OSError:
                    if os.name != 'nt':
                        raise
                    import subprocess
                    junction = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                                               str(root / name), str(target)],
                                              capture_output=True, timeout=5)
                    self.assertEqual(junction.returncode, 0, 'scratch junction creation failed')
                calls = []
                result = deliver_summary(root, FP, 'text', now=NOW,
                                         sender=lambda text: calls.append(text) or
                                         {'status': 'success', 'message_id': 1})
                self.assertEqual(result, {'status': 'path_invalid'})
                self.assertEqual(calls, [])
                self.assertEqual((target / 'sentinel').read_text('utf-8'), '{}')

    def test_atomic_write_failure_preserves_old_state_and_reports_attempt(self):
        from unittest.mock import patch
        import api_report_delivery as delivery
        self.deliver(sender=lambda text: {'status': 'failure'})
        path = self.root / 'delivery-ledger.json'
        old = path.read_bytes()
        calls = []
        sender = lambda text: calls.append(text) or {'status': 'success', 'message_id': 3}
        with patch.object(delivery.os, 'replace', side_effect=OSError('RAW_SECRET')):
            result = self.deliver(sender=sender, now=NOW + timedelta(seconds=1800))
        self.assertEqual(result, {'status': 'ledger_write_failed', 'attempted': False})
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(calls, [])
        original_replace = os.replace
        replacements = []
        def fail_ack(source, target):
            replacements.append((source, target))
            self.assertEqual(Path(source).parent, path.parent)
            if len(replacements) == 2:
                raise OSError('RAW_SECRET')
            return original_replace(source, target)
        with patch.object(delivery.os, 'replace', side_effect=fail_ack):
            result = self.deliver(sender=sender, now=NOW + timedelta(seconds=1800))
        self.assertEqual(result, {'status': 'ledger_write_failed', 'attempted': True})
        self.assertEqual(len(calls), 1)
        self.assertIsNone(self.read_ledger()['last_acked_fingerprint'])
        self.assertEqual(self.read_ledger()['last_attempt_outcome'], 'in_progress')
        self.assertEqual(list(self.root.glob('.delivery-*.tmp')), [])

    def test_crash_after_retry_preserves_uncertain_recovery(self):
        class Crash(BaseException):
            pass
        self.deliver(sender=lambda text: {'status': 'failure'})
        def crash(text):
            raise Crash()
        with self.assertRaises(Crash):
            self.deliver(sender=crash, now=NOW + timedelta(seconds=1800))
        self.assertEqual(self.deliver(sender=lambda text: {'status': 'success', 'message_id': 3},
                         now=NOW + timedelta(seconds=2100))['status'], 'skip_uncertain')

    def test_telegram_sender_posts_only_plain_text_json(self):
        import api_report_delivery as delivery
        import io
        self.assertTrue(hasattr(delivery, 'TelegramSender'), 'plain Telegram transport is missing')
        text = '**literal** <b>tag</b> [link](https://example.invalid) 한국어'
        calls = []
        def opener(request, *, timeout):
            calls.append((request, timeout))
            return io.BytesIO(json.dumps({'ok': True, 'result': {
                'message_id': 42, 'chat': {'id': -123}, 'text': text}}).encode('utf-8'))
        sender = delivery.TelegramSender('123:FAKE_TOKEN', '-123', opener=opener)
        self.assertEqual(sender(text), {'status': 'success', 'message_id': 42})
        self.assertEqual(len(calls), 1)
        request, timeout = calls[0]
        self.assertEqual(timeout, 15)
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.full_url, 'https://api.telegram.org/bot123:FAKE_TOKEN/sendMessage')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(json.loads(request.data.decode('utf-8')), {
            'chat_id': -123, 'text': text, 'entities': [], 'disable_notification': False,
            'protect_content': False, 'link_preview_options': {'is_disabled': True}})
        self.assertNotIn('FAKE_TOKEN', repr(sender))

    def test_telegram_success_requires_exact_echo_chat_and_integer_id(self):
        from api_report_delivery import TelegramSender
        import io
        baseline = {'ok': True, 'result': {'message_id': 9, 'chat': {'id': -123}, 'text': 'text'}}
        variants = [dict(baseline, ok=1), dict(baseline, ok='true'),
                    dict(baseline, result=None), dict(baseline, result=[]),
                    {'ok': True}, {}, []]
        for key, values in {'message_id': [True, 0, -1, 1.0, '9', None],
                            'chat': [{'id': -124}, {'id': '-123'}, {'id': True}, None],
                            'text': ['other', None, ['text']]}.items():
            for value in values:
                variants.append({'ok': True, 'result': dict(baseline['result'], **{key: value})})
        for index, body in enumerate(variants):
            with self.subTest(index=index):
                calls = []
                def opener(request, *, timeout):
                    calls.append(request)
                    return io.BytesIO(json.dumps(body).encode('utf-8'))
                result = TelegramSender('123:FAKE_TOKEN', -123, opener=opener)('text')
                self.assertEqual(result, {'status': 'uncertain'})
                self.assertEqual(len(calls), 1)

    def test_telegram_response_reads_are_bounded_and_malformed_is_uncertain(self):
        from api_report_delivery import TelegramSender
        import io
        bodies = [b'{', b'\xff', b'x' * 65537,
                  b'{"ok":true,"ok":false,"result":{"message_id":1,"chat":{"id":123},"text":"text"}}']
        for raw in bodies:
            with self.subTest(size=len(raw)):
                reads = []
                class Response(io.BytesIO):
                    def read(self, size=-1):
                        reads.append(size)
                        return super().read(size)
                result = TelegramSender('123:FAKE_TOKEN', 123,
                                        opener=lambda request, timeout: Response(raw))('text')
                self.assertEqual(result, {'status': 'uncertain'})
                self.assertEqual(reads, [65537])

    def test_telegram_http500_is_sanitized_failure_with_ledger_backoff(self):
        from api_report_delivery import TelegramSender
        from urllib.error import HTTPError
        import contextlib
        import io
        calls = []
        class ForbiddenBody(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError('HTTP error bodies must not be read')
        def opener(request, *, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 500, 'FAKE_TOKEN_PRIVATE', {}, ForbiddenBody(b'PRIVATE'))
        sender = TelegramSender('123:FAKE_TOKEN', 123, opener=opener)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.deliver(sender=sender), {'status': 'failed'})
            self.assertEqual(self.deliver(sender=sender, now=NOW + timedelta(seconds=60)),
                             {'status': 'skip_backoff'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('FAKE_TOKEN', json.dumps(self.read_ledger()))
        self.assertIsNone(self.read_ledger()['last_acked_fingerprint'])

    def test_telegram_invalid_text_never_opens_transport(self):
        from api_report_delivery import TelegramSender
        calls = []
        sender = TelegramSender('123:FAKE_TOKEN', 123,
                                opener=lambda request, timeout: calls.append(request))
        for text in (None, '', 7, 'x' * 3501, '\ud800'):
            with self.subTest(kind=type(text).__name__):
                self.assertEqual(sender(text), {'status': 'failure'})
        self.assertEqual(calls, [])

    def test_telegram_configuration_validation_never_echoes_credentials(self):
        from api_report_delivery import TelegramSender
        for token, chat in ((None, 123), ('SECRET/LEAK', 123), ('SECRET\nLEAK', 123),
                            ('123:FAKE_TOKEN', True), ('123:FAKE_TOKEN', 0),
                            ('123:FAKE_TOKEN', '@private'), ('123:FAKE_TOKEN', '001'),
                            ('123:FAKE_TOKEN', '1.0'), ('123:FAKE_TOKEN', None)):
            with self.subTest(chat_type=type(chat).__name__):
                with self.assertRaisesRegex(ValueError, '^invalid_telegram_configuration$'):
                    TelegramSender(token, chat, opener=lambda *args: None)

    def test_inconsistent_success_and_deep_json_fail_closed(self):
        from api_report_delivery import deliver_summary
        self.deliver(sender=lambda text: {'status': 'success', 'message_id': 1})
        baseline = self.read_ledger()
        invalids = [json.dumps(dict(baseline, consecutive_failures=1,
                                   backoff_until=(NOW + timedelta(seconds=60)).isoformat())),
                    json.dumps(dict(baseline, uncertain_fingerprint=FP,
                                   uncertain_at=NOW.isoformat())),
                    '[' * 1500 + '0' + ']' * 1500]
        for index, raw in enumerate(invalids):
            with self.subTest(index=index):
                path = self.root / 'delivery-ledger.json'
                path.write_text(raw, encoding='utf-8')
                calls = []
                result = deliver_summary(self.root, OTHER_FP, 'text', now=NOW,
                                         sender=lambda text: calls.append(text))
                self.assertEqual(result, {'status': 'ledger_invalid'})
                self.assertEqual(calls, [])
                self.assertEqual(path.read_text('utf-8'), raw)

    def test_telegram_explicit_api_rejection_is_failure_not_uncertainty(self):
        from api_report_delivery import TelegramSender
        import io
        calls = []
        def opener(request, *, timeout):
            calls.append(request)
            return io.BytesIO(b'{"ok":false,"error_code":400,"description":"PRIVATE"}')
        self.assertEqual(TelegramSender('123:FAKE_TOKEN', 123, opener=opener)('text'),
                         {'status': 'failure'})
        self.assertEqual(len(calls), 1)

    def test_default_telegram_transport_never_follows_redirects(self):
        import api_report_delivery as delivery
        from unittest.mock import patch
        self.assertTrue(hasattr(delivery, 'build_opener'), 'redirect-safe transport missing')
        with patch.object(delivery, 'build_opener') as build:
            sender = delivery.TelegramSender('123:FAKE_TOKEN', 123)
            build.assert_called_once()
            handler = build.call_args.args[0]
            redirected = handler.redirect_request(None, None, 302, 'redirect', {},
                                                   'https://elsewhere.invalid')
            self.assertIsNone(redirected)
            build.return_value.open.assert_not_called()

    def test_previous_ack_is_deduplicated_during_other_uncertain_attempt(self):
        from api_report_delivery import deliver_summary
        self.deliver(sender=lambda text: {'status': 'success', 'message_id': 1})
        deliver_summary(self.root, OTHER_FP, 'other', now=NOW + timedelta(seconds=21600),
                        sender=lambda text: {'status': 'uncertain'})
        calls = []
        self.assertEqual(self.deliver(now=NOW + timedelta(seconds=25200),
                         sender=lambda text: calls.append(text))['status'], 'skip_unchanged')
        self.assertEqual(calls, [])

    def test_zero_backoff_can_retry_without_invalidating_ledger(self):
        self.assertEqual(self.deliver(sender=lambda text: {'status': 'failure'},
                         backoff_seconds=0)['status'], 'failed')
        self.assertEqual(self.deliver(sender=lambda text: {'status': 'success', 'message_id': 1},
                         backoff_seconds=0)['status'], 'sent')

    def test_telegram_non200_response_cannot_be_acked(self):
        from api_report_delivery import TelegramSender
        import io
        for code, expected in ((500, 'failure'), (400, 'failure'), (302, 'uncertain')):
            with self.subTest(code=code):
                class Response(io.BytesIO):
                    status = code
                    def read(self, size=-1):
                        raise AssertionError('non-200 body must not be read')
                calls = []
                def opener(request, *, timeout):
                    calls.append(request)
                    return Response(b'PRIVATE')
                result = TelegramSender('123:FAKE_TOKEN', 123, opener=opener)('text')
                self.assertEqual(result, {'status': expected})
                self.assertEqual(len(calls), 1)

    def test_http_error_cleanup_cannot_expose_transport_exception(self):
        from api_report_delivery import TelegramSender
        from urllib.error import HTTPError
        class BrokenCleanup(HTTPError):
            def close(self):
                super().close()
                raise RuntimeError('PRIVATE_FAKE_TOKEN_URL')
        def opener(request, *, timeout):
            raise BrokenCleanup(request.full_url, 500, 'PRIVATE', {}, None)
        sender = TelegramSender('123:FAKE_TOKEN', 123, opener=opener)
        self.assertEqual(sender('text'), {'status': 'failure'})

    def test_os_lock_is_nonblocking_across_processes_and_released_on_crash(self):
        from api_report_delivery import deliver_summary
        import subprocess
        import sys
        import time
        ready = Path(self.temp.name) / 'ready'
        release = Path(self.temp.name) / 'release'
        child_code = (
            "import os,time; from pathlib import Path; from datetime import datetime; "
            "from api_report_delivery import deliver_summary\n"
            "def sender(text):\n"
            f"    Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
            "    deadline=time.monotonic()+10\n"
            f"    while not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(.01)\n"
            "    os._exit(0)\n"
            f"deliver_summary({str(self.root)!r}, {FP!r}, 'text', "
            f"now=datetime.fromisoformat({NOW.isoformat()!r}), sender=sender)\n"
        )
        process = subprocess.Popen([sys.executable, '-B', '-c', child_code],
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(), 'child did not reach sender')
            calls = []
            result = deliver_summary(self.root, OTHER_FP, 'text',
                                     now=NOW + timedelta(seconds=301),
                                     sender=lambda text: calls.append(text))
            self.assertEqual(result, {'status': 'delivery_locked'})
            self.assertEqual(calls, [])
        finally:
            release.write_text('release', encoding='utf-8')
            try:
                out, err = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual((out, err), (b'', b''))
        self.assertEqual(self.deliver(now=NOW + timedelta(seconds=300),
                         sender=lambda text: {'status': 'success', 'message_id': 1})['status'],
                         'skip_uncertain')
        self.assertTrue((self.root / 'delivery.lock').exists())

    def test_default_time_and_non_utc_time_are_stored_as_utc(self):
        from api_report_delivery import deliver_summary
        before = datetime.now(timezone.utc)
        self.assertEqual(deliver_summary(self.root, FP, 'text',
                         sender=lambda text: {'status': 'success', 'message_id': 1})['status'], 'sent')
        after = datetime.now(timezone.utc)
        stored = datetime.fromisoformat(self.read_ledger()['last_acked_at'])
        self.assertEqual(stored.utcoffset(), timedelta(0))
        self.assertTrue(before <= stored <= after)
        other_root = Path(self.temp.name) / 'offset'
        deliver_summary(other_root, FP, 'text', now=NOW.astimezone(timezone(timedelta(hours=9))),
                        sender=lambda text: {'status': 'success', 'message_id': 1})
        ledger = json.loads((other_root / 'delivery-ledger.json').read_text('utf-8'))
        self.assertEqual(ledger['last_acked_at'], NOW.isoformat())

    def test_telegram_connection_losses_are_uncertain_without_retry_or_output(self):
        from api_report_delivery import TelegramSender
        from urllib.error import URLError
        import contextlib
        import io
        for exception in (TimeoutError('PRIVATE_URL'), ConnectionResetError('PRIVATE_URL'),
                          URLError('PRIVATE_URL'), RuntimeError('PRIVATE_URL')):
            with self.subTest(kind=type(exception).__name__):
                calls = []
                def opener(request, *, timeout):
                    calls.append(request)
                    raise exception
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    result = TelegramSender('123:FAKE_TOKEN', 123, opener=opener)('text')
                self.assertEqual(result, {'status': 'uncertain'})
                self.assertEqual(len(calls), 1)
                self.assertEqual(output.getvalue(), '')

    def test_failure_or_uncertainty_write_error_leaves_durable_in_progress(self):
        import api_report_delivery as delivery
        from unittest.mock import patch
        original_replace = os.replace
        for index, outcome in enumerate(('failure', 'uncertain')):
            with self.subTest(outcome=outcome):
                root = Path(self.temp.name) / str(index)
                count = []
                def fail_final(source, target):
                    count.append(source)
                    if len(count) == 2:
                        raise OSError('PRIVATE')
                    return original_replace(source, target)
                with patch.object(delivery.os, 'replace', side_effect=fail_final):
                    result = delivery.deliver_summary(root, FP, 'text', now=NOW,
                                                       sender=lambda text: {'status': outcome})
                self.assertEqual(result, {'status': 'ledger_write_failed', 'attempted': True})
                ledger = json.loads((root / 'delivery-ledger.json').read_text('utf-8'))
                self.assertEqual(ledger['last_attempt_outcome'], 'in_progress')
                self.assertIsNone(ledger['last_acked_fingerprint'])
                self.assertEqual(delivery.deliver_summary(root, OTHER_FP, 'text',
                                 now=NOW + timedelta(seconds=300),
                                 sender=lambda text: {'status': 'success', 'message_id': 1})['status'],
                                 'skip_uncertain')

    def test_maximum_length_plain_text_is_echo_validated(self):
        from api_report_delivery import TelegramSender
        import io
        text = '가' * 3500
        requests = []
        def opener(request, *, timeout):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'ok': True, 'result': {
                'message_id': 1, 'chat': {'id': 123}, 'text': text}},
                ensure_ascii=False).encode('utf-8'))
        from api_report_delivery import deliver_summary
        self.assertEqual(deliver_summary(self.root, FP, text, now=NOW,
                         sender=TelegramSender('123:FAKE_TOKEN', 123, opener=opener)),
                         {'status': 'sent', 'message_id': 1})
        self.assertEqual(requests[0]['text'], text)

    def test_invalid_utf8_ledger_is_not_replaced(self):
        self.root.mkdir()
        path = self.root / 'delivery-ledger.json'
        path.write_bytes(b'\xff')
        calls = []
        self.assertEqual(self.deliver(sender=lambda text: calls.append(text)),
                         {'status': 'ledger_invalid'})
        self.assertEqual(calls, [])
        self.assertEqual(path.read_bytes(), b'\xff')

    def test_output_hardlinks_into_protected_directories_are_rejected(self):
        from api_report_delivery import deliver_summary
        protected = Path(self.temp.name) / 'cache'
        protected.mkdir()
        source = protected / 'input.json'
        source.write_text('{}', encoding='utf-8')
        for index, name in enumerate(('delivery-ledger.json', 'delivery.lock')):
            with self.subTest(name=name):
                root = Path(self.temp.name) / str(index)
                root.mkdir()
                os.link(source, root / name)
                calls = []
                result = deliver_summary(root, FP, 'text', now=NOW, protected_paths=(protected,),
                                         sender=lambda text: calls.append(text) or
                                         {'status': 'success', 'message_id': 1})
                self.assertEqual(result, {'status': 'path_invalid'})
                self.assertEqual(calls, [])
                self.assertEqual(source.read_text('utf-8'), '{}')


if __name__ == '__main__':
    unittest.main()
