"""API-only observations: synthetic evidence, no network or operational database."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from blizzard_collector import store_snapshot, upsert_houses
from common import set_meta

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 15, 4, tzinfo=timezone.utc)


def listing(iid, price, qty=1, ident=1, kind='commodity'):
    return dict(kind=kind, listing_id=ident, item_id=iid, unit_price=price,
                quantity=qty, time_left='LONG')


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript((ROOT / 'schema.sql').read_text(encoding='utf-8'))
        set_meta(self.db, 'game_flavor', 'forever')
        set_meta(self.db, 'data_mode', 'fixture')
        self.cfg = {'blizzard_forever': {}, 'min_distinct_days': 3}
        upsert_houses(self.db, 1, [dict(id=113, name='호드', side='horde'),
                                  dict(id=114, name='호드', side='horde')], NOW.isoformat())

    def tearDown(self):
        self.db.close()

    def snapshot(self, house, rows, ago=60, lm=None, ruleset=1, auction_lm=None):
        fetched = NOW - timedelta(minutes=ago)
        stamp = lm if lm is not None else format_datetime(fetched - timedelta(minutes=1), usegmt=True)
        return store_snapshot(self.db, house, ruleset, fetched.isoformat(), rows,
                              sum(r['kind'] == 'auction' for r in rows),
                              sum(r['kind'] == 'commodity' for r in rows), auction_lm or stamp, stamp)

    def observe(self, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec('api_market_observer'),
                             'API-only observation feature is missing')
        from api_market_observer import build_observations
        return build_observations(self.db, self.cfg, now=NOW, **kwargs)

    def test_api_only_commodity_observation_without_auctionator_or_confirmed_market(self):
        self.snapshot(113, [listing(7, 100, 2, 1), listing(7, 100, 3, 2),
                            listing(7, 150, 7, 3), listing(7, 1, 20, 4, 'auction')])
        self.snapshot(114, [listing(7, 200, 4)])
        data = self.observe()
        houses = {h['house_id']: h for h in data['houses']}
        self.assertEqual(houses[113]['quotes'][7], {
            'min_unit_price': 100, 'qty_at_min': 5, 'total_qty': 12, 'listing_count': 3})
        gap = data['comparisons'][0]
        self.assertEqual((gap['item_id'], gap['lower_quote_house'], gap['higher_quote_house']), (7, 113, 114))
        self.assertEqual(gap['gap_copper'], 100)
        self.assertEqual(gap['status'], 'observation')
        self.assertEqual(data['mode'], 'fixture')
        self.assertEqual(houses[113]['access'], 'unverified')
        self.assertIsNone(self.db.execute("SELECT value FROM meta WHERE key='auctionator_local_realm_id'").fetchone())
        self.assertFalse(self.cfg['blizzard_forever'].get('market_confirmed', False))

    def test_invalid_or_aged_source_blocks_comparison_instead_of_reusing_prices(self):
        cases = [
            ('aged_source_data', 30, format_datetime(NOW - timedelta(hours=5), usegmt=True)),
            ('future_fetch', -5, format_datetime(NOW - timedelta(minutes=1), usegmt=True)),
            ('invalid_source_time', 30, 'not-a-date'),
            ('invalid_source_time', 30, 'Sun, 15 Nov 2026 03:00:00'),
            ('source_after_fetch', 30, format_datetime(NOW - timedelta(minutes=5), usegmt=True)),
        ]
        for reason, ago, stamp in cases:
            with self.subTest(reason=reason, stamp=stamp):
                self.db.execute('DELETE FROM blizzard_snapshots')
                self.snapshot(113, [listing(7, 100)], ago=ago, lm=stamp)
                self.snapshot(114, [listing(7, 200)], ago=30)
                data = self.observe()
                self.assertEqual(data['comparisons'], [])
                house = next(h for h in data['houses'] if h['house_id'] == 113)
                self.assertIn(reason, house['reasons'])
                self.assertEqual(house['status'], 'blocked')

    def test_source_or_fetch_skew_blocks_pair_even_when_both_houses_are_fresh(self):
        for fetch_skew in (False, True):
            with self.subTest(fetch_skew=fetch_skew):
                self.db.execute('DELETE FROM blizzard_snapshots')
                first = format_datetime(NOW - timedelta(minutes=60), usegmt=True)
                second = first if fetch_skew else format_datetime(NOW - timedelta(minutes=20), usegmt=True)
                self.snapshot(113, [listing(7, 100)], ago=50 if fetch_skew else 10, lm=first)
                self.snapshot(114, [listing(7, 200)], ago=10, lm=second)
                data = self.observe()
                self.assertEqual(data['comparisons'], [])
                self.assertEqual(data['pairs'][0]['status'], 'blocked')
                self.assertIn('fetch_skew' if fetch_skew else 'source_skew', data['pairs'][0]['reasons'])

    def test_independent_history_pairs_and_current_direction_streak(self):
        for prices, expected_status, streak in [([200, 200, 200], 'repeated_gap', 3),
                                                 ([200, 50, 200], 'observation', 1)]:
            with self.subTest(prices=prices):
                self.db.execute('DELETE FROM blizzard_snapshots')
                for ago, higher in zip((180, 120, 60), prices):
                    self.snapshot(113, [listing(7, 100)], ago=ago)
                    self.snapshot(114, [listing(7, higher)], ago=ago)
                lm = format_datetime(NOW - timedelta(minutes=61), usegmt=True)
                for house, price in ((113, 100), (114, prices[-1])):
                    self.snapshot(house, [listing(7, price)], ago=30, lm=lm, auction_lm='changed-auctions-only')
                data = self.observe()
                gap = data['comparisons'][0]
                self.assertEqual(gap['status'], expected_status)
                self.assertEqual(gap['evidence']['paired_observations'], 3)
                self.assertEqual(gap['evidence']['consecutive_positive_pairs'], streak)
                self.assertEqual(gap['evidence']['history_readiness'], 'insufficient_history')
                self.assertEqual(len(gap['history']), 3)
                self.assertEqual(data['houses'][0]['coverage']['distinct_updates'], 3)

    def test_explicit_house_order_preserves_canonical_matching_evidence(self):
        for house, price, source_times in (
                (113, 100, ((2, 20), (3, 20), (3, 30))),
                (114, 200, ((2, 20), (3, 10), (3, 21)))):
            for hour, minute in source_times:
                source = NOW.replace(hour=hour, minute=minute)
                fetched = source + timedelta(minutes=1)
                self.snapshot(house, [listing(7, price)], ago=(NOW - fetched).total_seconds() / 60)
        orders = ([113, 114], [114, 113])
        results = [self.observe(house_ids=order) for order in orders]
        for order, data in zip(orders, results):
            with self.subTest(house_ids=order):
                gap = data['comparisons'][0]
                self.assertEqual((gap['evidence']['paired_observations'],
                                  gap['evidence']['current_pair_matched'], gap['status']),
                                 (2, False, 'observation'))
                self.assertEqual(data['pairs'][0]['house_ids'], [113, 114])
                self.assertEqual(data['comparisons'], results[0]['comparisons'])

    def test_missing_item_breaks_consecutive_direction_evidence(self):
        for ago in (300, 240, 180, 120, 60):
            self.snapshot(113, [listing(8 if ago == 120 else 7, 100)], ago=ago)
            self.snapshot(114, [listing(7, 200)], ago=ago)
        gap = self.observe()['comparisons'][0]
        self.assertEqual(gap['evidence']['paired_observations'], 4)
        self.assertEqual(gap['evidence']['consecutive_positive_pairs'], 1)
        self.assertEqual(gap['status'], 'observation')

    def test_conflicting_same_lm_or_regressed_latest_lm_never_resurrects_old_price(self):
        for conflict in (True, False):
            with self.subTest(conflict=conflict):
                self.db.execute('DELETE FROM blizzard_snapshots')
                self.snapshot(113, [listing(7, 100)], ago=60)
                lm = format_datetime(NOW - timedelta(minutes=61 if conflict else 91), usegmt=True)
                newest = self.snapshot(113, [listing(7, 90 if conflict else 100)], ago=30,
                                       lm=lm, auction_lm='new-auction-update')
                self.snapshot(114, [listing(7, 200)], ago=30)
                data = self.observe()
                self.assertEqual(data['comparisons'], [])
                house = data['houses'][0]
                self.assertEqual(house['latest']['id'], newest)
                self.assertIn('lm_conflict' if conflict else 'source_regressed', house['reasons'])

    def test_same_lm_consistency_is_checked_before_source_history_window_filter(self):
        stamp = format_datetime(NOW - timedelta(hours=2), usegmt=True)
        for conflict in (True, False):
            with self.subTest(conflict=conflict):
                self.db.execute('DELETE FROM blizzard_snapshots')
                first = self.snapshot(113, [listing(7, 100)], ago=30, lm=stamp,
                                      auction_lm=format_datetime(NOW - timedelta(minutes=30), usegmt=True))
                newest = self.snapshot(113, [listing(7, 90 if conflict else 100)], ago=20, lm=stamp,
                                       auction_lm=format_datetime(NOW - timedelta(minutes=20), usegmt=True))
                self.snapshot(114, [listing(7, 200)], ago=20, lm=stamp)
                for window in (72, 1):
                    with self.subTest(window_hours=window):
                        data = self.observe(window_hours=window)
                        house = data['houses'][0]
                        self.assertEqual(house['latest']['id'], newest)
                        self.assertEqual(house['coverage']['window_snapshots'], 2)
                        if conflict:
                            self.assertEqual(data['comparisons'], [])
                            self.assertEqual(house['status'], 'blocked')
                            self.assertIn('lm_conflict', house['reasons'])
                            self.assertEqual(house['coverage']['lm_conflicts'], [dict(
                                source_epoch=(NOW - timedelta(hours=2)).timestamp(),
                                snapshot_ids=[first, newest])])
                        else:
                            self.assertEqual(house['status'], 'available')
                            self.assertEqual(house['coverage']['lm_conflicts'], [])
                            gap = data['comparisons'][0]
                            self.assertEqual(gap['gap_copper'], 100)
                            self.assertEqual(house['coverage']['distinct_updates'], 0 if window == 1 else 1)
                            self.assertEqual(gap['evidence']['paired_observations'], 0 if window == 1 else 1)
                            if window == 1:
                                self.assertEqual(gap['history'], [])
                                self.assertIsNone(gap['trends']['source_price'])
                                self.assertIsNone(gap['trends']['reference_price'])
                                self.assertEqual(gap['status'], 'observation')

    def test_invalid_latest_fetch_metadata_blocks_without_crashing_json_or_html(self):
        from api_market_observer import render, snapshot_times
        cases = [
            ('text_epoch', 'fetched_epoch', 'not-a-time'),
            ('blob_epoch', 'fetched_epoch', b'not-a-time'),
            ('unrepresentable_epoch', 'fetched_epoch', 9223372036854775807),
            ('nonfinite_real_epoch', 'fetched_epoch', float('inf')),
            ('fractional_epoch', 'fetched_epoch', (NOW - timedelta(minutes=20)).timestamp() + 0.5),
            ('malformed_iso', 'fetched_at', 'not-a-date'),
            ('blob_iso', 'fetched_at', b'not-a-date'),
            ('naive_iso', 'fetched_at', '2026-11-15T03:40:00'),
            ('iso_epoch_mismatch', 'fetched_at', (NOW - timedelta(minutes=19)).isoformat()),
            ('unrepresentable_iso', 'fetched_at', '0001-01-01T00:00:00+14:00'),
        ]
        for label, column, raw in cases:
            with self.subTest(case=label):
                self.db.execute('DELETE FROM blizzard_snapshots')
                self.snapshot(113, [listing(7, 80)], ago=60)
                newest = self.snapshot(113, [listing(7, 100)], ago=20)
                self.snapshot(114, [listing(7, 200)], ago=20)
                self.db.execute(f'UPDATE blizzard_snapshots SET {column}=? WHERE id=?', (raw, newest))
                row = self.db.execute('SELECT * FROM blizzard_snapshots WHERE id=?', (newest,)).fetchone()
                self.assertEqual(row[column], raw, 'Fixture must retain the exact malformed value')
                if label == 'nonfinite_real_epoch':
                    self.assertIs(type(row[column]), float, 'SQLite must retain a non-finite REAL')
                data = self.observe()
                encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
                html = render(data)
                house, healthy = data['houses']
                self.assertEqual(house['status'], 'blocked')
                self.assertIn('invalid_fetch_time', house['reasons'])
                self.assertEqual(house['latest']['id'], newest, 'Do not replace invalid latest with older snapshot')
                self.assertEqual(house['quotes'][7]['min_unit_price'], 100)
                self.assertIsNone(house.get('fetched_epoch', 'missing validated time'))
                self.assertEqual(healthy['status'], 'available')
                self.assertEqual(data['comparisons'], [])
                self.assertEqual(data['pairs'][0]['status'], 'blocked')
                self.assertIsNone(data['pairs'][0]['fetch_skew_seconds'])
                self.assertEqual(data['pairs'][0]['source_skew_seconds'], 0)
                self.assertIn('invalid_fetch_time', html)
                self.assertIn('수집 미확인', html)
                metadata = json.loads(encoded)['houses'][0]['latest']
                if label == 'nonfinite_real_epoch':
                    self.assertEqual(metadata[column], {'type': 'non_finite_float', 'value': 'inf'})
                elif type(raw) is bytes:
                    self.assertEqual(metadata[column], {'type': 'bytes', 'value': repr(raw)})
                else:
                    self.assertEqual(metadata[column], raw)
                source_epoch, reasons = snapshot_times(row, NOW.timestamp())
                self.assertEqual(source_epoch, (NOW - timedelta(minutes=21)).timestamp())
                self.assertIn('invalid_fetch_time', reasons)

    def test_invalid_newest_epoch_sorting_below_old_snapshot_never_falls_back(self):
        from api_market_observer import snapshot_times
        older = self.snapshot(113, [listing(7, 100)], ago=20)
        newest = self.snapshot(113, [listing(7, 90)], ago=10)
        self.snapshot(114, [listing(7, 200)], ago=10)
        damaged_epoch = 1794709800
        self.db.execute('UPDATE blizzard_snapshots SET fetched_epoch=? WHERE id=?', (damaged_epoch, newest))
        damaged = self.db.execute('SELECT * FROM blizzard_snapshots WHERE id=?', (newest,)).fetchone()
        older_epoch = self.db.execute('SELECT fetched_epoch FROM blizzard_snapshots WHERE id=?', (older,)).fetchone()[0]
        self.assertGreater(newest, older)
        self.assertLess(damaged['fetched_epoch'], older_epoch)
        self.assertEqual(damaged['fetched_at'], (NOW - timedelta(minutes=10)).isoformat())
        self.assertIn('invalid_fetch_time', snapshot_times(damaged, NOW.timestamp())[1])

        data = self.observe()
        house, healthy = data['houses']
        self.assertEqual(house['latest']['id'], newest, 'Select latest by ingestion order, not an unvalidated epoch')
        self.assertEqual(house['latest']['fetched_epoch'], damaged_epoch)
        self.assertEqual(house['quotes'][7]['min_unit_price'], 90)
        self.assertEqual(house['status'], 'blocked')
        self.assertIn('invalid_fetch_time', house['reasons'])
        self.assertIsNone(house['fetched_epoch'])
        self.assertEqual(house['coverage']['distinct_updates'], 1)
        self.assertEqual(healthy['status'], 'available')
        self.assertEqual(data['comparisons'], [])
        self.assertEqual(data['pairs'][0]['house_ids'], [113, 114])
        self.assertEqual(data['pairs'][0]['status'], 'blocked')
        self.assertIn('house_unavailable', data['pairs'][0]['reasons'])
        self.assertIsNone(data['pairs'][0]['fetch_skew_seconds'])
        self.assertEqual(data['pairs'][0]['source_skew_seconds'], 0)

    def test_blob_last_modified_headers_preserve_json_safe_raw_metadata(self):
        queries = {
            'commodities_last_modified': 'UPDATE blizzard_snapshots SET commodities_last_modified=? WHERE id=?',
            'auctions_last_modified': 'UPDATE blizzard_snapshots SET auctions_last_modified=? WHERE id=?',
        }
        from api_market_observer import render
        for column, query in queries.items():
            with self.subTest(column=column):
                self.db.execute('DELETE FROM blizzard_snapshots')
                newest = self.snapshot(113, [listing(7, 100)], ago=20)
                self.snapshot(114, [listing(7, 200)], ago=20)
                raw = b'not-a-date'
                self.db.execute(query, (raw, newest))
                data = self.observe()
                encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
                metadata = json.loads(encoded)['houses'][0]['latest']
                self.assertEqual(metadata[column], {'type': 'bytes', 'value': repr(raw)})
                html = render(data)
                if column == 'commodities_last_modified':
                    self.assertEqual(data['houses'][0]['status'], 'blocked')
                    self.assertIn('invalid_source_time', html)
                else:
                    self.assertEqual(data['houses'][0]['status'], 'available')

    def test_rejects_invalid_observation_bounds_and_naive_clock(self):
        from api_market_observer import build_observations
        from common import AppError
        cases = [dict(limit=0), dict(limit=True), dict(window_hours=0), dict(window_hours=float('nan')),
                 dict(max_age_hours=float('inf')), dict(max_skew_minutes=-1), dict(min_pairs=1),
                 dict(house_ids=[]), dict(house_ids=[True]), dict(now=NOW.replace(tzinfo=None))]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(AppError):
                build_observations(self.db, self.cfg, **dict({'now': NOW}, **kwargs))

    def test_rejects_invalid_history_day_threshold_from_configuration(self):
        from common import AppError
        for value in (0, -1, True, 1.5, '3', None):
            with self.subTest(value=value):
                self.cfg['min_distinct_days'] = value
                with self.assertRaises(AppError):
                    self.observe()

    def test_retail_or_unidentified_database_cannot_be_presented_as_forever(self):
        from common import AppError
        for flavor in ('retail', 'unknown'):
            set_meta(self.db, 'game_flavor', flavor)
            with self.subTest(flavor=flavor), self.assertRaises(AppError):
                self.observe()

    def test_cli_emits_json_and_portable_html_from_existing_api_database(self):
        self.snapshot(113, [listing(7, 100)])
        self.snapshot(114, [listing(7, 200)])
        self.db.commit()
        scratch = str(Path(os.environ['HERMES_HOME']) / 'cache/scratch') if os.environ.get('HERMES_HOME') else None
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            base = Path(temp)
            db_path, cfg_path = base / 'fixture.sqlite3', base / 'config.json'
            target = sqlite3.connect(db_path)
            try:
                self.db.backup(target)
            finally:
                target.close()
            cfg_path.write_text(json.dumps(dict(self.cfg, db_path=str(db_path), game_flavor='forever')), encoding='utf-8')
            command = [sys.executable, str(ROOT / 'api_market_observer.py'), '--config', str(cfg_path), '--houses', '113,114']
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip().startswith('{'), 'JSON CLI output is missing')
            self.assertEqual(len(json.loads(result.stdout)['houses']), 2)
            output = base / 'report.html'
            result = subprocess.run(command + ['--format', 'html', '--output', str(output)],
                                    capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(output.is_file())
            html = output.read_text(encoding='utf-8')
            self.assertIn('API', html)
            self.assertIn('실제 판매', html)
            self.assertNotIn('<script', html.lower())
            self.assertNotIn('src="https://', html)

    def test_output_cannot_overwrite_inputs_sqlite_sidecars_or_hardlink_aliases(self):
        import api_market_observer as observer
        from common import AppError
        self.assertTrue(hasattr(observer, 'validate_output_path'), 'Input/output alias protection is missing')
        scratch = str(Path(os.environ['HERMES_HOME']) / 'cache/scratch') if os.environ.get('HERMES_HOME') else None
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            base = Path(temp)
            db_path, cfg_path = base / 'data.sqlite3', base / 'config.json'
            db_path.write_bytes(b'database-must-not-change')
            cfg_path.write_bytes(b'config-must-not-change')
            alias = base / 'alias.html'
            os.link(db_path, alias)
            for path in [db_path, cfg_path, alias, *(Path(str(db_path) + suffix) for suffix in ('-wal', '-shm', '-journal'))]:
                with self.subTest(path=path), self.assertRaises(AppError):
                    observer.validate_output_path(path, db_path, cfg_path)
            observer.validate_output_path(base / 'report.html', db_path, cfg_path)
            self.assertEqual(db_path.read_bytes(), b'database-must-not-change')

    def test_cli_output_replaces_hardlink_planted_during_analysis_without_truncating_input(self):
        from unittest.mock import patch
        import api_market_observer as observer
        self.snapshot(113, [listing(7, 100)])
        self.snapshot(114, [listing(7, 200)])
        self.db.commit()
        scratch = str(Path(os.environ['HERMES_HOME']) / 'cache/scratch') if os.environ.get('HERMES_HOME') else None
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            base = Path(temp)
            db_path, output = base / 'fixture.sqlite3', base / 'report.json'
            target = sqlite3.connect(db_path)
            try:
                self.db.backup(target)
            finally:
                target.close()
            before = db_path.read_bytes()
            build = observer.build_observations

            def build_with_planted_hardlink(*args, **kwargs):
                os.link(db_path, output)
                return build(*args, **dict(kwargs, now=NOW))

            with patch.object(observer, 'load_config', return_value=dict(self.cfg, base_dir=base)), \
                    patch.object(observer, 'build_observations', side_effect=build_with_planted_hardlink):
                observer.main(['--config', str(base / 'fixture.config.json'), '--db', str(db_path),
                               '--output', str(output)])
            self.assertEqual(db_path.read_bytes(), before, 'Report output truncated the input DB')
            self.assertFalse(output.samefile(db_path))
            self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['returned_count'], 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows directory-junction regression')
    def test_cli_output_uses_validated_canonical_path_after_junction_retarget(self):
        from unittest.mock import patch
        import api_market_observer as observer
        self.snapshot(113, [listing(7, 100)])
        self.snapshot(114, [listing(7, 200)])
        self.db.commit()
        scratch = str(Path(os.environ['HERMES_HOME']) / 'cache/scratch') if os.environ.get('HERMES_HOME') else None
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            base = Path(temp)
            inputs, safe, reports = base / 'inputs', base / 'original-reports', base / 'reports'
            inputs.mkdir()
            safe.mkdir()
            db_path = inputs / 'fixture.sqlite3'
            target = sqlite3.connect(db_path)
            try:
                self.db.backup(target)
            finally:
                target.close()
            before = db_path.read_bytes()

            def junction_to(destination):
                result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                                         str(reports), str(destination)], capture_output=True,
                                        text=True, encoding='utf-8', errors='replace', timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(reports.resolve(), destination.resolve())

            build = observer.build_observations

            def build_with_retargeted_junction(*args, **kwargs):
                os.rmdir(reports)  # Remove only the junction, never its destination.
                junction_to(inputs)
                return build(*args, **dict(kwargs, now=NOW))

            try:
                junction_to(safe)
                with patch.object(observer, 'load_config', return_value=dict(self.cfg, base_dir=base)), \
                        patch.object(observer, 'build_observations', side_effect=build_with_retargeted_junction):
                    observer.main(['--config', str(base / 'fixture.config.json'), '--db', str(db_path),
                                   '--output', str(reports / 'fixture.sqlite3')])
                self.assertTrue(db_path.read_bytes() == before,
                                'Report output replaced the input DB after Windows junction retargeting')
                output = safe / 'fixture.sqlite3'
                self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['returned_count'], 1)
                self.assertEqual({path.name for path in safe.iterdir()}, {'fixture.sqlite3'})
                self.assertEqual({path.name for path in inputs.iterdir()}, {'fixture.sqlite3'})
            finally:
                if reports.is_dir():
                    os.rmdir(reports)

    def test_cli_output_failure_preserves_old_report_and_cleans_temporary_file(self):
        from unittest.mock import patch
        import api_market_observer as observer
        self.snapshot(113, [listing(7, 100)])
        self.snapshot(114, [listing(7, 200)])
        data = self.observe()
        self.db.commit()
        scratch = str(Path(os.environ['HERMES_HOME']) / 'cache/scratch') if os.environ.get('HERMES_HOME') else None
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            base = Path(temp)
            db_path, output = base / 'fixture.sqlite3', base / 'report.json'
            target = sqlite3.connect(db_path)
            try:
                self.db.backup(target)
            finally:
                target.close()
            output.write_bytes(b'previous complete report')
            original_files = {path.name for path in base.iterdir()}
            make_temp = observer.tempfile.NamedTemporaryFile

            def temporary_with_partial_write_failure(*args, **kwargs):
                report = make_temp(*args, **kwargs)
                write = report.write

                def fail_write(payload):
                    write(payload[:10])
                    raise OSError('fixture write failure')

                report.write = fail_write
                return report

            for failure in ('write', 'replace'):
                operation = (patch.object(observer.tempfile, 'NamedTemporaryFile',
                                          side_effect=temporary_with_partial_write_failure)
                             if failure == 'write' else
                             patch.object(Path, 'replace', side_effect=OSError('fixture replace failure')))
                with self.subTest(failure=failure), operation, \
                        patch.object(observer, 'load_config', return_value=dict(self.cfg, base_dir=base)), \
                        patch.object(observer, 'build_observations', return_value=data):
                    with self.assertRaisesRegex(OSError, f'fixture {failure} failure'):
                        observer.main(['--config', str(base / 'fixture.config.json'), '--db', str(db_path),
                                       '--output', str(output)])
                    self.assertEqual(output.read_bytes(), b'previous complete report')
                    self.assertEqual({path.name for path in base.iterdir()}, original_files)

    def test_default_and_explicit_scope_never_mix_rulesets_or_mutate_configuration(self):
        import copy
        upsert_houses(self.db, 1, [dict(id=116, name='검은바다', side='blackwater'),
                                  dict(id=118, name='얼라이언스', side='alliance')], NOW.isoformat())
        upsert_houses(self.db, 2, [dict(id=999, name='다른 룰셋', side='horde')], NOW.isoformat())
        saved = copy.deepcopy(self.cfg)
        self.assertEqual([h['house_id'] for h in self.observe()['houses']], [113, 114, 116])
        self.assertEqual([h['house_id'] for h in self.observe(house_ids=[118])['houses']], [118])
        self.assertEqual(self.cfg, saved)

    def test_observer_issues_no_database_writes_and_escapes_html_metadata(self):
        self.snapshot(113, [listing(7, 100)])
        self.snapshot(114, [listing(7, 200)])
        self.db.execute('UPDATE items SET name=? WHERE item_id=7', ('</h3><script>alert(1)</script>',))
        self.db.execute('UPDATE blizzard_houses SET name=? WHERE house_id=113', ('<img src=x onerror=alert(2)>',))
        self.db.commit()
        before = self.db.serialize()
        denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_TABLE}
        self.db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
        data = self.observe()
        self.assertEqual(self.db.serialize(), before)
        from api_market_observer import render
        html = render(data)
        self.assertNotIn('<script', html.lower())
        self.assertNotIn('<img', html.lower())
        self.assertIn('&lt;script&gt;', html)

    def test_default_scope_rejects_string_database_house_ids(self):
        from common import AppError
        for ids in (('<svg/onload=alert(1)>', 'marketB'), (0, 114), (-1, 114),
                    (113.5, 114), (b'house', 114)):
            with self.subTest(ids=ids):
                self.db.execute('DELETE FROM blizzard_snapshots')
                self.db.execute('DELETE FROM blizzard_houses')
                for house_id, price in zip(ids, (100, 200)):
                    self.db.execute('''INSERT INTO blizzard_houses
                        (house_id,ruleset,name,side,updated_at) VALUES (?,1,'Horde','horde',?)''',
                                    (house_id, NOW.isoformat()))
                    self.snapshot(house_id, [listing(7, price)])
                with self.assertRaisesRegex(AppError, 'house_id'):
                    self.observe()

    def test_rejects_nonpositive_or_noninteger_database_commodity_item_ids(self):
        from common import AppError
        self.snapshot(113, [listing(7, 100)])
        self.snapshot(114, [listing(7, 200)])
        self.db.commit()
        # Simulate persisted malformed IDs: INTEGER affinity is not a type check.
        self.db.execute('PRAGMA foreign_keys=OFF')
        for item_id in ('<svg/onload=alert(1)>', 'itemB', b'item', 7.5, 0, -7):
            with self.subTest(item_id=item_id):
                self.db.execute('UPDATE blizzard_listings SET item_id=?', (item_id,))
                with self.assertRaisesRegex(AppError, 'item_id'):
                    self.observe()

    def test_renderer_escapes_every_displayed_identifier(self):
        import copy
        from html import escape
        from api_market_observer import render
        self.snapshot(113, [listing(7, 100)])
        self.snapshot(114, [listing(7, 200)])
        original = self.observe()
        paths = [('houses', 0, 'house_id'),
                 ('comparisons', 0, 'item_id'),
                 ('comparisons', 0, 'lower_quote_house'),
                 ('comparisons', 0, 'higher_quote_house'),
                 ('comparisons', 0, 'history', 0, 'source_snapshot_id'),
                 ('comparisons', 0, 'history', 0, 'reference_snapshot_id')]
        payload = '<svg/onload=alert("id")>'
        for path in paths:
            with self.subTest(path=path):
                data = copy.deepcopy(original)
                target = data
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = payload
                html = render(data)
                self.assertIn(escape(payload, quote=True), html)
                self.assertNotIn('<svg', html.lower())

    def test_history_matching_nearest_ties_and_no_snapshot_reuse(self):
        from api_market_observer import pair_history
        def point(ident, source, fetched=None):
            return dict(snapshot_id=ident, source_epoch=source, fetched_epoch=fetched if fetched is not None else source)
        pairs = pair_history([point(1, 100), point(2, 200)],
                             [point(3, 100), point(4, 105), point(5, 200)], 10)
        self.assertEqual([(a['snapshot_id'], b['snapshot_id']) for a, b in pairs], [(1, 3), (2, 5)])
        tied = pair_history([point(1, 100, 102)], [point(9, 90, 99), point(8, 110, 103)], 15)
        self.assertEqual(tied[0][1]['snapshot_id'], 8)

    def test_lm_overlap_across_history_bound_blocks_unverified_current_comparison(self):
        from api_market_observer import load_history
        stamp = format_datetime(NOW - timedelta(minutes=169), usegmt=True)
        for ago in range(168, -1, -1):
            newest = self.snapshot(113, [listing(7, 100 if ago == 168 else 90)], ago=ago,
                                   lm=stamp, auction_lm=format_datetime(NOW - timedelta(minutes=ago), usegmt=True))
        self.snapshot(114, [listing(7, 200)], ago=0, lm=stamp)
        data = self.observe()
        self.assertEqual(data['comparisons'], [])
        house = data['houses'][0]
        self.assertEqual(house['status'], 'blocked')
        self.assertIn('lm_history_truncated', house['reasons'])
        self.assertNotIn('lm_conflict', house['reasons'])
        self.assertEqual(house['latest']['id'], newest)
        self.assertEqual(house['quotes'][7]['min_unit_price'], 90)
        self.assertEqual(house['coverage']['window_snapshots'], 169)
        self.assertEqual(house['coverage']['distinct_updates'], 0)
        self.assertEqual(house['coverage']['lm_conflicts'], [])
        self.assertEqual(house['coverage']['lm_history_truncated'],
                         [(NOW - timedelta(minutes=169)).timestamp()])
        queries = []
        self.db.set_trace_callback(queries.append)
        try:
            history, coverage = load_history(self.db, 113, 1, NOW.timestamp(), 72)
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual(history, [])
        self.assertTrue(coverage['truncated'])
        self.assertLessEqual(sum('FROM blizzard_listings' in sql for sql in queries), 168)

    def test_history_is_bounded_and_explicitly_reports_truncation(self):
        for ago in range(170, 0, -1):
            self.snapshot(113, [listing(7, 100)], ago=ago)
        data = self.observe(house_ids=[113])
        self.assertTrue(data['houses'][0]['coverage']['truncated'])
        self.assertEqual(data['houses'][0]['coverage']['window_snapshots'], 170)
        self.assertEqual(data['houses'][0]['coverage']['distinct_updates'], 168)
