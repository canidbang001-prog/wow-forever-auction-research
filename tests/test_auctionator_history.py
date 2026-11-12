"""Synthetic histories exercise source semantics and import provenance, not sales."""
from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3
import unittest

from common import AppError, snapshot
from import_auctionator import daily_rows, latest_rows, store_daily
from market_history import attach_history
from action_planner import sell_scenario
from tests.test_static import Base
from tests.test_actions import item, NOW


def source(low=700):
    return {'version': 2, '42': {'m': 800, 'l': {'2506': low},
            'h': {'2506': 900, '2507': 800}, 'a': {'2506': 12, '2507': 0}}}


class ParseDailyTests(unittest.TestCase):
    def test_low_fallback_and_max_quantity_do_not_become_median_or_sales(self):
        data = source()
        rows = daily_rows(data, 9)
        self.assertEqual(rows[0][3:], (700, 900, 12))
        self.assertEqual(rows[1][3:], (800, 800, 0))
        self.assertEqual(rows[1][2], '2026-11-11T15:00:00+00:00')
        self.assertEqual(latest_rows(data, 9)[0][0][2:5], (None, None, None))

    def test_empty_cbor_maps_quantity_only_days_and_missing_quantity(self):
        data = {'1': {'h': {'2506': 300}, 'l': [], 'a': {'2507': 40}}}
        rows = daily_rows(data, 9)
        self.assertEqual(rows[0][3:], (300, 300, None))
        self.assertEqual(rows[1][3:], (None, None, 40))

    def test_malformed_ranges_stop_instead_of_erasing_history(self):
        for change in ({'l': {'2506': 1000}}, {'h': {'not-a-day': 5}}, {'a': {'2507': -1}}, {'h': [1]}):
            data = source(); data['42'].update(change)
            with self.subTest(change=change), self.assertRaises(AppError):
                daily_rows(data, 9)

    def test_daily_low_changes_scenario_without_inventing_median(self):
        i = item()
        i['daily_history'] = [dict(low=5000, observed='2026-11-11T15:00:00+00:00', import_id=7)]
        s = sell_scenario(i, NOW)
        self.assertEqual(s['reference'], 5000)
        self.assertEqual(s['price'], 4999)
        self.assertEqual(s['daily_imports'], [7])
        self.assertEqual(s['daily_points'], 1)


class StoreDailyTests(Base):
    def prepare(self):
        self.db.execute("INSERT INTO realms VALUES(-1,'[\"PvP\"]'),(-2,'[\"Other\"]')")
        data = source(); rows, skipped = latest_rows(data, 9)
        sid = snapshot(self.db, 'auctionator', -1, NOW.isoformat(), 'same-min', rows)
        return data, rows, sid

    def test_daily_only_change_deduplicates_prices_but_preserves_file_evidence(self):
        data, rows, sid = self.prepare()
        stat = SimpleNamespace(st_mtime_ns=123)
        first = store_daily(self.db, -1, sid, data, rows, daily_rows(data, 9), stat, 0)
        self.assertIsNone(store_daily(self.db, -1, sid, data, rows, daily_rows(data, 9), stat, 0))
        data = source(600)
        second = store_daily(self.db, -1, sid, data, rows, daily_rows(data, 9), stat, 0)
        self.assertNotEqual(first, second)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM auctionator_daily').fetchone()[0], 4)
        items = {42: {}}
        coverage = attach_history(self.db, -1, items, NOW)
        self.assertEqual(coverage['daily_rows'], 2)
        self.assertEqual(items[42]['daily_history'][0]['low'], 600)
        self.assertEqual(items[42]['daily_history'][0]['import_id'], second)
        self.assertEqual(items[42]['ranges']['7']['days'], 2)

    def test_other_market_and_old_dates_never_fill_current_baseline(self):
        data, rows, sid = self.prepare()
        data = {'version': 2, '42': {'m': 800, 'h': {'2300': 1}, 'l': [], 'a': []}}
        store_daily(self.db, -1, sid, data, rows, daily_rows(data, 9), SimpleNamespace(st_mtime_ns=1), 0)
        other_sid = snapshot(self.db, 'auctionator', -2, NOW.isoformat(), 'other', rows)
        data = source(5)
        store_daily(self.db, -2, other_sid, data, rows, daily_rows(data, 9), SimpleNamespace(st_mtime_ns=2), 0)
        items = {42: {}}
        self.assertEqual(attach_history(self.db, -1, items, NOW)['daily_rows'], 1)
        self.assertIsNone(items[42]['ranges']['7']['low'])
        self.assertIsNone(items[42]['ranges']['30']['low'])

    def test_missing_item_or_table_does_not_invent_history(self):
        data, rows, sid = self.prepare()
        store_daily(self.db, -1, sid, data, rows, daily_rows(data, 9), SimpleNamespace(st_mtime_ns=1), 0)
        self.assertEqual(attach_history(self.db, -1, {}, NOW)['daily_rows'], 0)
        bare = sqlite3.connect(':memory:')
        bare.row_factory = sqlite3.Row
        with self.assertRaises(AppError) as caught:
            attach_history(bare, -1, {}, NOW)
        self.assertIn('import_auctionator', str(caught.exception))
        bare.close()


if __name__ == '__main__':
    unittest.main()
