"""Updater tests use only disposable SQLite files and injected transports."""
import importlib.util
from contextlib import closing
import json
import os
from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tests.test_forever_item_cache import NOW, document, entry


class FakeTransport:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, item_id, remaining_seconds):
        self.calls.append((item_id, remaining_seconds))
        response = self.responses.get(item_id, {'id': item_id, 'name': f'공식 이름 {item_id}'})
        if isinstance(response, Exception):
            raise response
        return response


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'auction.sqlite3'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("CREATE TABLE items(item_id INTEGER PRIMARY KEY);"
                             "CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);"
                             "INSERT INTO meta VALUES('game_flavor','forever');")
            db.executemany('INSERT INTO items VALUES(?)', [(i,) for i in [1, 2, 10, 30, 40]])
        self.cache = self.root / 'metadata' / 'names.json'
        self.state = self.root / 'metadata' / 'state.json'
        self.candidates = self.root / 'report.json'
        self.config = self.root / 'config.json'
        self.config.write_text('{}', encoding='utf-8')
        self.cfg = dict(base_dir=self.root, game_flavor='forever', db_path=str(self.db))
        self.transport = FakeTransport()

    def update(self, **kwargs):
        from forever_item_updater import update_cache
        args = dict(db_path=self.db, cache_path=self.cache, state_path=self.state,
                    config_path=self.config, transport=self.transport, now=NOW, monotonic=lambda: 0.0)
        args.update(kwargs)
        try:
            return update_cache(self.cfg, **args)
        except Exception as exc:
            self.fail(f'updater must return a sanitized result, not raise {type(exc).__name__}')

    def test_wrong_ids_or_unsafe_payloads_never_attach_a_name_and_count_toward_cap(self):
        bad_bodies = [{'id': 2, 'name': '다른 아이템'}, {'id': True, 'name': '잘못됨'},
                      {'id': '1', 'name': '잘못됨'}, {'id': 1, 'name': {'en_US': 'English'}},
                      {'id': 1, 'name': 'item#1'}, {'id': 1, 'name': '가\u202e나'},
                      {'id': 1, 'name': '가', 'namespace': 'static-kr'},
                      {'id': 1, 'name': '가', 'locale': 'en_US'}, [], None]
        for body in bad_bodies:
            with self.subTest(body=body):
                self.cache.unlink(missing_ok=True)
                self.transport = FakeTransport({1: body})
                result = self.update(max_requests=1)
                self.assertEqual((result['requests'], result['updated_count']), (1, 0))
                self.assertEqual(len(self.transport.calls), 1)
                self.assertEqual(json.loads(self.cache.read_text())['items'], {})

    def test_official_response_is_whitelisted_and_keeps_class_zero(self):
        self.transport = FakeTransport({1: dict(id=1, name={'ko_KR': '공식명', 'en_US': 'not used'},
            item_class={'id': 0, 'name': '소모품'}, item_subclass={'id': 0},
            token='SECRET', headers={'Authorization': 'SECRET'}, raw_body='SECRET',
            _links={'self': {'href': 'https://evil/?token=SECRET'}})})
        result = self.update(max_requests=1)
        saved_items = json.loads(self.cache.read_text())['items']
        self.assertIn('1', saved_items, 'localized official KR metadata must be accepted')
        saved = saved_items['1']
        self.assertEqual((saved['name_ko'], saved['class_id'], saved['subclass_id']), ('공식명', 0, 0))
        self.assertNotIn('SECRET', self.cache.read_text() + self.state.read_text() + json.dumps(result))

    def test_uncached_candidates_precede_remaining_numeric_catalog(self):
        self.cache.parent.mkdir()
        self.cache.write_text(json.dumps(document({'2': entry(2)})), encoding='utf-8')
        self.candidates.write_text(json.dumps({'comparisons': [
            {'item_id': 40}, {'item_id': 10}, {'item_id': 2}, {'item_id': 40},
            {'item_id': True}, {'item_id': '30'}, {'item_id': 999}]}), encoding='utf-8')
        result = self.update(candidates_report=self.candidates, max_requests=3)
        self.assertEqual([i for i, _ in self.transport.calls], [10, 40, 1])
        self.assertEqual(result['requests'], 3)
        from forever_item_cache import load_item_cache
        self.assertEqual(set(load_item_cache(self.cache, now=NOW)['items']), {1, 2, 10, 40})

    def test_success_and_negative_ttls_are_separate_from_auction_freshness(self):
        self.cache.parent.mkdir()
        negative = entry(30, state='not_found', fetched_at=(NOW - timedelta(days=6)).isoformat())
        del negative['name_ko']
        expired_negative = entry(40, state='not_found', fetched_at=(NOW - timedelta(days=7)).isoformat())
        del expired_negative['name_ko']
        self.cache.write_text(json.dumps(document({
            '1': entry(1, fetched_at=(NOW - timedelta(hours=4)).isoformat()),
            '2': entry(2, fetched_at=(NOW - timedelta(days=30)).isoformat()),
            '10': entry(10, fetched_at=(NOW - timedelta(days=31)).isoformat()),
            '30': negative, '40': expired_negative})), encoding='utf-8')
        self.candidates.write_text(json.dumps({'comparisons': [{'item_id': 40}, {'item_id': 10}]}))
        result = self.update(candidates_report=self.candidates)
        self.assertEqual([i for i, _ in self.transport.calls], [10, 40])
        self.assertEqual(result['updated_count'], 2)

    def test_http_auth_and_rate_limit_stop_immediately_and_persist_cooldown(self):
        from urllib.error import HTTPError
        from io import BytesIO
        for code in (401, 403, 429):
            with self.subTest(code=code):
                self.cache.unlink(missing_ok=True)
                self.state.unlink(missing_ok=True)
                self.transport = FakeTransport({1: HTTPError('https://evil/?SECRET', code, 'SECRET',
                    {'Retry-After': '999999999999999999999999'}, BytesIO(b'SECRET'))})
                result = self.update()
                self.assertEqual([i for i, _ in self.transport.calls], [1])
                self.assertEqual(result['status'], f'http_{code}')
                saved = json.loads(self.state.read_text())
                from forever_item_cache import _timestamp
                wait = (_timestamp(saved['cooldown_until']) - NOW).total_seconds()
                self.assertGreaterEqual(wait, 60)
                self.assertLessEqual(wait, 86400)
                self.assertNotIn('SECRET', json.dumps(result) + self.state.read_text() + self.cache.read_text())
                self.transport.calls.clear()
                second = self.update()
                self.assertEqual(second['status'], 'cooldown')
                self.assertEqual(second['requests'], 0)
                self.assertEqual(self.transport.calls, [])

    def test_404_is_negative_cached_for_seven_days_without_stopping_batch(self):
        from urllib.error import HTTPError
        self.transport = FakeTransport({1: HTTPError('https://invalid/?SECRET', 404, 'SECRET', {}, None)})
        result = self.update(max_requests=2)
        self.assertEqual([i for i, _ in self.transport.calls], [1, 2])
        saved = json.loads(self.cache.read_text())['items']
        self.assertEqual(saved['1']['state'], 'not_found')
        self.assertNotIn('name_ko', saved['1'])
        self.assertEqual(result['not_found_count'], 1)
        self.transport = FakeTransport()
        self.update(now=NOW + timedelta(days=6), max_requests=1)
        self.assertEqual([i for i, _ in self.transport.calls], [10])
        self.transport.calls.clear()
        self.update(now=NOW + timedelta(days=7), max_requests=1)
        self.assertEqual([i for i, _ in self.transport.calls], [1])

    def test_run_budget_passes_remaining_time_and_stops_before_next_call(self):
        ticks = [0.0]
        def bounded(item_id, remaining):
            self.transport(item_id, remaining)
            ticks[0] += 6
            return {'id': item_id, 'name': '공식명'}
        result = self.update(transport=bounded, budget_seconds=5, monotonic=lambda: ticks[0])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][1], 5)
        self.assertEqual(result['status'], 'budget_exhausted')
        self.assertEqual(result['requests'], 1)

    def test_outputs_cannot_alias_inputs_each_other_sidecars_or_git_paths(self):
        import shutil
        from forever_item_updater import update_cache
        for output in ('cache_path', 'state_path'):
            for collision in ('db', 'config', 'env', 'candidate', 'wal', 'shm', 'journal',
                              'same-output', 'hardlink-db', 'hardlink-outputs', 'git'):
                with self.subTest(output=output, collision=collision), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                    root = Path(tmp)
                    db = root / 'db.sqlite3'
                    shutil.copyfile(self.db, db)
                    config, env, candidate = root / 'config.json', root / '.env', root / 'report.json'
                    config.write_text('{}')
                    env.write_text('DUMMY=not-a-credential')
                    candidate.write_text('{"comparisons":[]}')
                    targets = dict(db=db, config=config, env=env, candidate=candidate,
                                   wal=Path(str(db) + '-wal'), shm=Path(str(db) + '-shm'),
                                   journal=Path(str(db) + '-journal'))
                    args = dict(db_path=db, config_path=config, env_path=env, candidates_report=candidate,
                                cache_path=root / 'cache.json', state_path=root / 'state.json')
                    if collision in targets:
                        args[output] = targets[collision]
                    elif collision == 'same-output':
                        args[output] = args['state_path' if output == 'cache_path' else 'cache_path']
                    elif collision == 'hardlink-db':
                        args[output].hardlink_to(db)
                    elif collision == 'hardlink-outputs':
                        other = args['state_path' if output == 'cache_path' else 'cache_path']
                        other.write_text('{}')
                        args[output].hardlink_to(other)
                    else:
                        (root / 'repo' / '.git').mkdir(parents=True)
                        args[output] = root / 'repo' / 'untracked.json'
                    originals = {p: p.read_bytes() for p in targets.values() if p.exists()}
                    self.transport.calls.clear()
                    try:
                        result = update_cache(dict(self.cfg, base_dir=root), transport=self.transport, now=NOW, **args)
                    except Exception as exc:
                        self.fail(f'path validation must precede reads/writes: {type(exc).__name__}')
                    self.assertEqual(result['status'], 'invalid_paths')
                    self.assertEqual(result['requests'], 0)
                    self.assertEqual(self.transport.calls, [])
                    self.assertEqual(originals, {p: p.read_bytes() for p in originals})

    def test_atomic_replace_failure_preserves_previous_cache_and_cleans_tempfiles(self):
        import os
        self.cache.parent.mkdir()
        original = json.dumps(document({'40': entry(40)})).encode()
        self.cache.write_bytes(original)
        real_replace = os.replace
        attempted = []
        def fail_cache(source, target):
            attempted.append((Path(source), Path(target)))
            self.assertEqual(Path(source).parent, Path(target).parent)
            if Path(target) == self.cache:
                raise PermissionError('SECRET replacement error')
            return real_replace(source, target)
        with patch('os.replace', side_effect=fail_cache):
            result = self.update(max_requests=1)
        self.assertEqual(result['status'], 'write_error')
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertTrue(attempted)
        self.assertEqual(sorted(p.name for p in self.cache.parent.iterdir()), ['names.json', 'state.json'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_catalog_access_is_read_only_and_missing_or_wrong_flavor_db_fails_closed(self):
        original = self.db.read_bytes()
        real_connect = sqlite3.connect
        statements, connections = [], []
        def readonly(database_uri, *args, **kwargs):
            self.assertTrue(database_uri.endswith('?mode=ro'))
            self.assertIs(kwargs.get('uri'), True)
            con = real_connect(database_uri, *args, **kwargs)
            connections.append(database_uri)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute('DELETE FROM items')
            con.set_trace_callback(statements.append)
            return con
        with patch('sqlite3.connect', side_effect=readonly):
            self.update(max_requests=1)
        self.assertTrue(connections)
        self.assertFalse(any(sql.split()[0].upper() in ('CREATE', 'INSERT', 'DELETE', 'UPDATE') for sql in statements))
        self.assertEqual(self.db.read_bytes(), original)
        self.transport.calls.clear()
        missing = self.root / 'not-created.sqlite3'
        result = self.update(db_path=missing)
        self.assertEqual(result['status'], 'db_error')
        self.assertFalse(missing.exists())
        with closing(real_connect(self.db)) as db, db:
            db.execute("UPDATE meta SET value='retail'")
        result = self.update()
        self.assertEqual(result['status'], 'db_error')
        self.assertEqual(self.transport.calls, [])

    def test_corrupt_or_unreadable_cooldown_state_fails_closed_without_requests(self):
        self.state.parent.mkdir()
        for raw in ('{SECRET', '[]', 'null', '{}',
                    json.dumps(dict(schema_version=True, cooldown_until=None)),
                    json.dumps(dict(schema_version=1, updated_at=NOW.isoformat(),
                                    cooldown_until='2026-11-15T00:30:00'))):
            with self.subTest(raw=raw):
                self.state.write_text(raw)
                result = self.update()
                self.assertEqual(result['status'], 'state_error')
                self.assertEqual(self.state.read_text(), raw)
                self.assertEqual(self.transport.calls, [])
                self.assertNotIn('SECRET', json.dumps(result))
        self.state.write_text('{}')
        real_read = Path.read_text
        def unreadable(path, *args, **kwargs):
            if path == self.state:
                raise PermissionError('SECRET')
            return real_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', unreadable):
            self.assertEqual(self.update()['status'], 'state_error')
        self.assertEqual(self.transport.calls, [])

    def test_official_transport_reuses_client_with_private_settings_and_bounded_oauth(self):
        import forever_item_updater as updater
        self.assertTrue(hasattr(updater, 'OfficialItemTransport'), 'bounded official adapter is missing')
        import blizzard_collector as collector
        import common
        from io import BytesIO
        seen, created = [], []
        ticks = [0.0]
        original_http = collector.http_json
        original_limits = (common.HTTP_ATTEMPTS, common.HTTP_SOCKET_TIMEOUT, common.HTTP_DEADLINE)
        cfg = dict(self.cfg, blizzard_forever={'namespace': 'dynamic-forever-kr'})
        def factory(received, env_path=None):
            self.assertEqual(received['blizzard_forever']['namespace'], 'dynamic-forever-kr')
            api = collector.ForeverAuctions.__new__(collector.ForeverAuctions)
            api.settings = dict(namespace='dynamic-forever-kr', locale='ko_KR', region='kr')
            api.fixture, api.token, api.expires = None, None, 0
            api.client_id, api.secret = 'synthetic-client', 'synthetic-secret'
            api.request_interval, api._paced_at = 0, None
            created.append(api)
            return api
        class Response(BytesIO):
            headers = {}
        def opener(request, timeout):
            seen.append((request.full_url, timeout))
            if request.full_url == 'https://oauth.battle.net/token':
                ticks[0] += 3
                return Response(b'{"access_token":"synthetic-token","expires_in":3600}')
            return Response(b'{"id":1,"name":"official"}')
        transport = updater.OfficialItemTransport(cfg, client_factory=factory, opener=opener,
                                                  monotonic=lambda: ticks[0])
        self.assertEqual(transport(1, 5)['id'], 1)
        self.assertEqual([timeout for _, timeout in seen], [5, 2])
        self.assertEqual(seen[1][0], 'https://kr.api.blizzard.com/data/wow/item/1?namespace=static-forever-kr&locale=ko_KR')
        self.assertEqual(cfg['blizzard_forever']['namespace'], 'dynamic-forever-kr')
        self.assertEqual(created[0].settings['namespace'], 'static-forever-kr')
        self.assertIs(collector.http_json, original_http)
        self.assertEqual((common.HTTP_ATTEMPTS, common.HTTP_SOCKET_TIMEOUT, common.HTTP_DEADLINE), original_limits)

    def test_missing_credentials_returns_sanitized_configuration_error(self):
        with patch('blizzard_collector.ForeverAuctions', side_effect=RuntimeError('SECRET credential path')) as factory:
            result = self.update(transport=None, max_requests=1)
        self.assertEqual(result['status'], 'configuration_error')
        self.assertEqual(factory.call_count, 1)
        self.assertNotIn('SECRET', json.dumps(result) + self.state.read_text())
        self.transport.calls.clear()
        # No credentials are inspected on a cooldown-only run.
        state = json.loads(self.state.read_text())
        state['cooldown_until'] = (NOW + timedelta(minutes=10)).isoformat()
        self.state.write_text(json.dumps(state))
        with patch('blizzard_collector.ForeverAuctions', side_effect=AssertionError('must not initialize')) as factory:
            self.assertEqual(self.update(transport=None)['status'], 'cooldown')
            factory.assert_not_called()

    def test_transport_failure_is_sanitized_and_does_not_touch_auction_database(self):
        original = self.db.read_bytes()
        self.transport = FakeTransport({2: RuntimeError('SECRET token https://private/')})
        result = self.update()
        self.assertEqual(result['status'], 'transport_error')
        self.assertEqual(result['requests'], 2)
        self.assertEqual([i for i, _ in self.transport.calls], [1, 2])
        self.assertEqual(self.db.read_bytes(), original)
        self.assertNotIn('SECRET', json.dumps(result) + self.cache.read_text() + self.state.read_text())
        self.assertEqual(set(json.loads(self.cache.read_text())['items']), {'1'})

    def test_invalid_candidate_report_returns_sanitized_error_before_requests(self):
        for raw in ('{SECRET', '[]', '{"comparisons":{}}', '{"comparisons":null}'):
            with self.subTest(raw=raw):
                self.candidates.write_text(raw)
                result = self.update(candidates_report=self.candidates)
                self.assertEqual(result['status'], 'candidates_error')
                self.assertEqual(self.transport.calls, [])
                self.assertFalse(self.cache.exists())
                self.assertNotIn('SECRET', json.dumps(result))

    def test_deep_candidate_json_returns_sanitized_error_without_requests_or_writes(self):
        raw = ('{"comparisons":[{"item_id":40}],"unused":' +
               '[' * 20000 + '0' + ']' * 20000 + '}')
        self.candidates.write_text(raw, encoding='utf-8')
        for existing_outputs in (False, True):
            with self.subTest(existing_outputs=existing_outputs):
                if existing_outputs:
                    self.cache.parent.mkdir()
                    self.cache.write_text(json.dumps(document({'2': entry(2)})), encoding='utf-8')
                    self.state.write_text(json.dumps(dict(schema_version=1, updated_at=NOW.isoformat(),
                                                          cooldown_until=None)), encoding='utf-8')
                originals = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
                result = self.update(candidates_report=self.candidates)
                self.assertEqual(result, dict(status='candidates_error', requests=0, updated_count=0,
                                              reason='candidates_unavailable'))
                self.assertEqual(self.transport.calls, [])
                self.assertEqual(originals, {path: path.read_bytes() for path in self.root.rglob('*')
                                             if path.is_file()})

    def test_invalid_limits_fail_before_transport_or_output_and_zero_cap_is_offline(self):
        for limits in [dict(max_requests=-1), dict(max_requests=True), dict(max_requests=1.5),
                       dict(budget_seconds=-1), dict(budget_seconds=True),
                       dict(budget_seconds=float('nan')), dict(budget_seconds=float('inf')),
                       dict(budget_seconds='90')]:
            with self.subTest(limits=limits):
                result = self.update(**limits)
                self.assertEqual(result['status'], 'configuration_error')
                self.assertEqual(self.transport.calls, [])
                self.assertFalse(self.cache.exists())
        with patch('blizzard_collector.ForeverAuctions', side_effect=AssertionError('no credential access')) as factory:
            self.assertEqual(self.update(transport=None, max_requests=0)['requests'], 0)
            factory.assert_not_called()

    def test_retry_after_seconds_or_http_date_is_bounded_and_cooldown_expires(self):
        from email.utils import format_datetime
        from urllib.error import HTTPError
        from forever_item_cache import _timestamp
        for header, expected in [('120', 120), ('0', 60),
                                 (format_datetime(NOW + timedelta(hours=1), usegmt=True), 3600),
                                 ('9' * 5000, 86400), ('SECRET invalid', 900)]:
            with self.subTest(header=header[:30]):
                self.cache.unlink(missing_ok=True)
                self.state.unlink(missing_ok=True)
                self.transport = FakeTransport({1: HTTPError('https://invalid/', 429, '', {'Retry-After': header}, None)})
                self.update(max_requests=1)
                until = _timestamp(json.loads(self.state.read_text())['cooldown_until'])
                self.assertEqual((until - NOW).total_seconds(), expected)
                self.transport = FakeTransport()
                result = self.update(now=until, max_requests=1)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual([i for i, _ in self.transport.calls], [1])

    def test_transport_body_deadline_does_not_restart_after_connection_time(self):
        import blizzard_collector as collector
        import common
        from forever_item_updater import OfficialItemTransport
        from io import BytesIO
        ticks, deadlines = [0.0], []
        class Client:
            settings = {}
            def get(self, path):
                return collector.http_json('https://kr.api.blizzard.com' + path +
                    '?namespace=static-forever-kr&locale=ko_KR', {})
        class Response(BytesIO):
            headers = {}
        def opener(request, timeout):
            ticks[0] += 3
            return Response(b'{"id":1,"name":"official"}')
        real_read = common._read_body
        def read(response, deadline):
            deadlines.append(deadline)
            return real_read(response, deadline)
        transport = OfficialItemTransport(self.cfg, client_factory=lambda *a, **k: Client(),
                                          opener=opener, monotonic=lambda: ticks[0])
        with patch('time.monotonic', side_effect=lambda: ticks[0]), patch('common._read_body', side_effect=read):
            self.assertEqual(transport(1, 5)['id'], 1)
        self.assertEqual(deadlines, [5])

    def test_oauth_404_is_not_an_item_negative_and_http_errors_do_not_retry(self):
        import blizzard_collector as collector
        import common
        from forever_item_updater import OfficialItemTransport
        from urllib.error import HTTPError
        class Client:
            settings = {}
            def get(self, path):
                return collector.http_json('https://oauth.battle.net/token', {})
        for code, status in [(404, 'transport_error'), (401, 'http_401'), (403, 'http_403'), (429, 'http_429')]:
            with self.subTest(code=code):
                self.cache.unlink(missing_ok=True)
                self.state.unlink(missing_ok=True)
                seen = []
                old_http, old_timeout = collector.http_json, common.HTTP_SOCKET_TIMEOUT
                def opener(request, timeout):
                    seen.append(request.full_url)
                    raise HTTPError(request.full_url, code, 'SECRET', {'Retry-After': '120'}, None)
                transport = OfficialItemTransport(self.cfg, client_factory=lambda *a, **k: Client(), opener=opener)
                result = self.update(transport=transport)
                self.assertEqual(result['status'], status)
                self.assertEqual(len(seen), 1)
                self.assertEqual(json.loads(self.cache.read_text())['items'], {})
                self.assertIs(collector.http_json, old_http)
                self.assertEqual(common.HTTP_SOCKET_TIMEOUT, old_timeout)
                self.assertNotIn('SECRET', json.dumps(result) + self.state.read_text())

    def test_transport_does_not_follow_redirects_or_send_auth_to_other_urls(self):
        import forever_item_updater as updater
        import blizzard_collector as collector
        self.assertTrue(hasattr(updater, '_NoRedirect'), 'official transport must disable redirects')
        self.assertIsNone(updater._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil/'))
        class Client:
            settings = {}
            def get(self, path):
                return collector.http_json('https://evil/?SECRET', {'Authorization': 'Bearer synthetic'})
        opener = unittest.mock.Mock(return_value=None)
        transport = updater.OfficialItemTransport(self.cfg, client_factory=lambda *a, **k: Client(), opener=opener)
        self.assertEqual(self.update(transport=transport)['status'], 'transport_error')
        opener.assert_not_called()

    def test_cli_uses_common_base_args_and_explicit_cache_state_with_safe_summary(self):
        import forever_item_updater as updater
        self.assertTrue(hasattr(updater, 'build_parser'), 'updater CLI parser is missing')
        parser = updater.build_parser()
        defaults = parser.parse_args(['--cache', str(self.cache), '--state', str(self.state)])
        self.assertEqual((defaults.max_requests, defaults.budget_seconds), (40, 90))
        self.assertIsNone(defaults.env)
        from contextlib import redirect_stdout
        from io import StringIO
        out = StringIO()
        args = ['--config', str(self.config), '--db', str(self.db), '--env', str(self.root / 'synthetic.env'),
                '--cache', str(self.cache), '--state', str(self.state), '--max-requests', '1', '--budget-seconds', '5']
        with patch('common.load_config', return_value=self.cfg), \
                patch.object(updater, 'OfficialItemTransport', return_value=self.transport), redirect_stdout(out):
            self.assertEqual(updater.main(args), 0)
        result = json.loads(out.getvalue())
        self.assertEqual((result['status'], result['requests']), ('ok', 1))
        self.assertEqual(set(json.loads(self.cache.read_text())['items']), {'1'})

    def test_cli_configuration_errors_do_not_print_tracebacks_paths_or_secrets(self):
        import forever_item_updater as updater
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        out, err = StringIO(), StringIO()
        with patch('common.load_config', side_effect=RuntimeError('SECRET private config path')), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                code = updater.main(['--config', str(self.config), '--cache', str(self.cache), '--state', str(self.state)])
            except Exception as exc:
                self.fail(f'CLI must sanitize configuration errors: {type(exc).__name__}')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())['status'], 'configuration_error')
        self.assertNotIn('SECRET', out.getvalue() + err.getvalue())
        self.assertEqual(err.getvalue(), '')

    def test_unreadable_existing_cache_is_not_overwritten_or_refetched(self):
        self.cache.parent.mkdir()
        original = json.dumps(document()).encode()
        self.cache.write_bytes(original)
        real_read = Path.read_text
        def read(path, *args, **kwargs):
            if path == self.cache:
                raise PermissionError('SECRET')
            return real_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            result = self.update()
        self.assertEqual(result['status'], 'cache_error')
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.cache.read_bytes(), original)

    def test_git_lexical_output_is_rejected_even_if_it_resolves_outside_checkout(self):
        repo = self.root / 'linked-repo'
        (repo / '.git').mkdir(parents=True)
        alias = repo / 'linked-cache.json'
        real_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return self.cache if path == alias else real_resolve(path, *args, **kwargs)
        # Emulate a stable symlink without requiring Windows symlink privileges.
        with patch.object(Path, 'resolve', resolve):
            result = self.update(cache_path=alias)
        self.assertEqual(result['status'], 'invalid_paths')
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(self.cache.exists())

    def test_ambiguous_or_excessively_nested_state_cannot_bypass_cooldown(self):
        self.state.parent.mkdir()
        raw = json.dumps(dict(schema_version=1, updated_at=NOW.isoformat(),
                              cooldown_until=(NOW + timedelta(hours=1)).isoformat()))
        ambiguous = raw[:-1] + ', "cooldown_until": null}'
        for text in (ambiguous, '[' * 20000 + '0' + ']' * 20000):
            with self.subTest(text=text[:60]):
                self.transport.calls.clear()
                self.state.write_text(text)
                result = self.update()
                self.assertEqual(result['status'], 'state_error')
                self.assertEqual(self.transport.calls, [])
                self.assertEqual(self.state.read_text(), text)

    def test_cooldown_starts_when_the_response_arrives_not_at_run_start(self):
        from urllib.error import HTTPError
        ticks = [0.0]
        def transport(item_id, remaining):
            ticks[0] += 40
            raise HTTPError('https://invalid/', 429, '', {'Retry-After': '120'}, None)
        self.update(transport=transport, monotonic=lambda: ticks[0])
        saved = json.loads(self.state.read_text())
        from forever_item_cache import _timestamp
        self.assertEqual(_timestamp(saved['updated_at']), NOW + timedelta(seconds=40))
        self.assertEqual(_timestamp(saved['cooldown_until']), NOW + timedelta(seconds=160))

    def test_cli_db_override_keeps_common_current_directory_path_semantics(self):
        import forever_item_updater as updater
        from contextlib import redirect_stdout
        from io import StringIO
        with patch('common.load_config', return_value=self.cfg), \
                patch.object(updater, 'update_cache', return_value={'status': 'ok', 'requests': 0}) as update, \
                redirect_stdout(StringIO()):
            updater.main(['--config', str(self.config), '--db', 'explicit.sqlite3',
                          '--cache', str(self.cache), '--state', str(self.state)])
        self.assertEqual(update.call_args.kwargs['db_path'], Path('explicit.sqlite3').resolve())

    def test_catalog_ids_are_numeric_capped_and_saved_as_validated_public_names(self):
        self.assertIsNotNone(importlib.util.find_spec('forever_item_updater'),
                             'separate updater is not implemented')
        result = self.update(max_requests=2)
        self.assertEqual([i for i, _ in self.transport.calls], [1, 2])
        self.assertEqual((result['status'], result['requests'], result['updated_count']), ('ok', 2, 2))
        from forever_item_cache import load_item_cache
        loaded = load_item_cache(self.cache, now=NOW)
        self.assertEqual(set(loaded['items']), {1, 2})
        self.assertEqual(loaded['named_count'], 2)
        self.assertEqual(json.loads(self.state.read_text())['last_status'], 'ok')
