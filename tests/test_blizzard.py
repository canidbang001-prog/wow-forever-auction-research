"""Synthetic Blizzard Forever fixtures. No network, no secrets, no live prices."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from http.client import IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError

from action_planner import plan_actions
from blizzard_collector import (
    aggregate_prices, attach_blizzard, collect_once, commodity_path, disappeared_before_expiry,
    hourly_pattern, item_sell_through, main, match_price_maps, parse_index, parse_listings,
    select_houses, selected_house_id, sell_through_estimate, store_snapshot, summary_lines,
    upsert_houses,
)
from common import (AppError, Blizzard, DEFAULT_USER_AGENT, HTTP_ATTEMPTS, HTTP_DEADLINE,
                    connect, ensure_items, http_json, set_meta)
from forever_report import report
from market_dashboard import build_market
from tests.test_actions import NOW, item
from tests.test_round2 import bundle, listing, report_of, vendor
from tests.test_static import Base


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests' / 'fixtures' / 'blizzard_forever.json'
INDEX = '/data/wow/ruleset/1/auctions/index'
FAKE_ID = 'test-id-value'
FAKE_SECRET = 'test-secret-value'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def write_cfg(root):
    cfg = json.loads((ROOT / 'config.forever.example.json').read_text(encoding='utf-8'))
    path = root / 'forever.json'
    path.write_text(json.dumps(cfg), encoding='utf-8')
    return path


def priced(kind, listing_id, item_id, price, qty, time_left):
    return {'kind': kind, 'listing_id': listing_id, 'item_id': item_id,
            'unit_price': price, 'quantity': qty, 'time_left': time_left}


class _JsonResponse:
    def __init__(self, payload, last_modified='Wed, 12 Nov 2026 00:00:00 GMT'):
        self.headers = {'Last-Modified': last_modified}
        self._raw = json.dumps(payload).encode()

    def read(self, _n=-1):
        data, self._raw = self._raw, b''
        return data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _http_error(code, body=b'{"error":"BODY-MARKER"}', headers=None):
    return HTTPError('https://kr.api.blizzard.com/data/wow/connected-realm', code, 'err',
                     {} if headers is None else dict(headers), io.BytesIO(body))


def _swap_env(**pairs):
    saved = {key: os.environ.get(key) for key in pairs}
    for key, value in pairs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return saved


def _restore_env(saved):
    _swap_env(**saved)


def sided_fixture(rows, auction_lm='Wed, 12 Nov 2026 00:13:00 GMT',
                  commodity_lm='Wed, 12 Nov 2026 00:13:01 GMT'):
    fixture = {INDEX: {'body': {'auctions': [
        {'id': house_id, 'name': name} for house_id, name, _auction, _commodity in rows]}}}
    for house_id, _name, auction_body, commodity_body in rows:
        fixture[f'/data/wow/ruleset/1/auctions/{house_id}'] = {'last_modified': auction_lm, 'body': auction_body}
        fixture[commodity_path(1, house_id)] = {'last_modified': commodity_lm, 'body': commodity_body}
    return fixture


LINKS = {'_links': {'self': {'href': 'https://example.invalid/auctions'}}}
PRICED_AUCTIONS = {'auctions': [
    {'id': 10, 'item': {'id': 8}, 'buyout': 400, 'quantity': 4, 'time_left': 'LONG'}]}
PRICED_COMMODITIES = {'auctions': [
    {'id': 1, 'item': {'id': 7}, 'quantity': 2, 'unit_price': 50, 'time_left': 'LONG'}]}


class ParseTests(unittest.TestCase):
    def test_index_selects_horde_and_blackwater_including_ids_past_118(self):
        houses = parse_index(load_fixture()[INDEX]['body'])
        self.assertEqual([h['id'] for h in houses], [110, 112, 113, 115, 116, 119, 121, 125])
        self.assertNotIn(122, [h['id'] for h in houses])
        self.assertEqual([h['id'] for h in select_houses(houses)], [113, 115, 116, 119, 125])
        self.assertEqual([h['id'] for h in select_houses(houses, include_alliance=True)],
                         [110, 112, 113, 115, 116, 119, 125])
        self.assertEqual(select_houses(houses, [110])[0]['side'], 'alliance')
        self.assertEqual(next(h['side'] for h in houses if h['id'] == 121), 'unknown')
        self.assertEqual(next(h['side'] for h in houses if h['id'] == 119), 'horde')
        with self.assertRaises(AppError):
            select_houses(houses, [122])

    def test_href_id_and_blackwater_wins_over_other_faction_words(self):
        houses = parse_index({'auctions': [
            {'key': {'href': 'https://kr.api.blizzard.com/data/wow/ruleset/1/auctions/120?namespace=dynamic-forever-kr'},
             'name': {'ko_KR': '호드', 'en_US': 'Horde'}},
            {'href': 'https://kr.api.blizzard.com/data/wow/ruleset/1/auctions/126', 'name': '검은바다 호드'},
            {'name': '이름 없음'},
            'skip',
        ]})
        self.assertEqual([(h['id'], h['side']) for h in houses], [(120, 'horde'), (126, 'blackwater')])
        with self.assertRaises(AppError):
            parse_index({})
        with self.assertRaises(AppError):
            parse_index({'auctions': None})

    def test_missing_auctions_is_unknown_and_explicit_empty_array_is_empty(self):
        self.assertEqual(parse_listings({'auctions': []}, 'auction'), ([], 0))
        for body in ({}, {'auctions': None}):
            with self.assertRaises(AppError):
                parse_listings(body, 'commodity')
        with self.assertRaises(AppError):
            parse_listings(None, 'auction')
        with self.assertRaises(AppError):
            parse_listings({'auctions': {}}, 'commodity')

    def test_commodity_unit_price_is_not_divided_and_stack_buyout_is_floored(self):
        auctions, n_auctions = parse_listings(load_fixture()['/data/wow/ruleset/1/auctions/113']['body'], 'auction')
        self.assertEqual(n_auctions, 5)
        self.assertEqual([(row['listing_id'], row['unit_price'], row['quantity']) for row in auctions],
                         [(10, 250, 4), (12, 3, 3)])
        commodities, n_commodities = parse_listings(
            load_fixture()['/data/wow/ruleset/1/auctions/116/commodities']['body'], 'commodity')
        self.assertEqual(n_commodities, 2)
        self.assertEqual([(row['unit_price'], row['quantity']) for row in commodities], [(80, 20)])
        self.assertNotEqual(commodities[0]['unit_price'], 80 // 20)
        skipped, count = parse_listings({'auctions': [
            {'id': 1, 'item': {'id': 8}, 'unit_price': 999, 'quantity': 1, 'time_left': 'LONG'},
            {'id': 2, 'item': {'id': 8}, 'buyout': 10, 'quantity': 3, 'time_left': 'LONG'},
        ]}, 'auction')
        self.assertEqual(count, 2)
        self.assertEqual(skipped[0]['unit_price'], 3)
        dupes, raw = parse_listings({'auctions': [
            {'id': 1, 'item': {'id': 1}, 'quantity': 2, 'unit_price': 10, 'time_left': {'type': 'VERY_LONG'}},
            {'id': 1, 'item': {'id': 1}, 'quantity': 9, 'unit_price': 99, 'time_left': 'LONG'},
        ]}, 'commodity')
        self.assertEqual(raw, 2)
        self.assertEqual([(row['unit_price'], row['quantity'], row['time_left']) for row in dupes],
                         [(10, 2, 'VERY_LONG')])
        no_price, _ = parse_listings({'auctions': [
            {'id': 1, 'item': {'id': 1}, 'buyout': 50, 'quantity': 1, 'time_left': 'LONG'},
        ]}, 'commodity')
        self.assertEqual(no_price, [])

    def test_aggregates_min_median_p25_quantity_and_count(self):
        commodities, _ = parse_listings(
            load_fixture()['/data/wow/ruleset/1/auctions/113/commodities']['body'], 'commodity')
        auctions, _ = parse_listings(load_fixture()['/data/wow/ruleset/1/auctions/113']['body'], 'auction')
        by_item = {row[0]: row[1:] for row in aggregate_prices(commodities + auctions)}
        self.assertEqual(by_item[7], (100, 300, 200, 16, 4))
        self.assertEqual(by_item[8], (3, 250, 3, 7, 2))

    def test_disappearance_bounds(self):
        self.assertFalse(disappeared_before_expiry('SHORT', 1))
        self.assertFalse(disappeared_before_expiry('VERY_LONG', 0))
        self.assertFalse(disappeared_before_expiry('VERY_LONG', -1))
        self.assertTrue(disappeared_before_expiry('MEDIUM', 1799))
        self.assertFalse(disappeared_before_expiry('MEDIUM', 1800))
        self.assertTrue(disappeared_before_expiry('LONG', 7199))
        self.assertFalse(disappeared_before_expiry('long', 7200))
        self.assertTrue(disappeared_before_expiry('VERY_LONG', 43199))
        self.assertFalse(disappeared_before_expiry('VERY_LONG', 43200))
        self.assertFalse(disappeared_before_expiry('', 100))

    def test_sell_through_threshold(self):
        thin = sell_through_estimate(1, 18, 6, 3)
        self.assertIsNone(thin['disappearance_pct'])
        self.assertEqual(thin['disappearance_status'], '데이터 부족')
        short_days = sell_through_estimate(5, 20, 6, 2)
        self.assertEqual(short_days['disappearance_status'], '데이터 부족')
        met = sell_through_estimate(9, 35, 6, 3)
        self.assertEqual(met['disappearance_pct'], 20)
        self.assertEqual(met['disappearance_status'], '추정')
        zero = sell_through_estimate(0, 20, 6, 3)
        self.assertEqual(zero['disappearance_pct'], 0)
        self.assertEqual(zero['disappearance_status'], '추정')

    def test_hourly_kst_pattern(self):
        self.assertEqual(hourly_pattern([
            ('2026-11-10T00:00:00+00:00', 100),
            ('2026-11-10T01:00:00+00:00', 100),
            ('2026-11-10T02:00:00+00:00', 100),
        ])['status'], '데이터 부족')
        one_each = [(f'2026-11-10T{hour:02d}:00:00+00:00', 100 + hour) for hour in range(12)]
        self.assertEqual(hourly_pattern(one_each, 0)['status'], '데이터 부족')
        points = []
        hours = {0: (10, 10, 1000), 1: (100, 100, 100), 2: (200, 200, 200), 3: (300, 300, 300)}
        for day, utc_day in enumerate(('09', '10', '11')):
            for hour, prices in hours.items():
                points.append((f'2026-11-{utc_day}T{15 + hour:02d}:00:00+00:00', prices[day]))
        pattern = hourly_pattern(points, 9)
        self.assertEqual(pattern['status'], '추정')
        self.assertEqual(pattern['cheapest_hours_kst'], [0])
        self.assertEqual(pattern['expensive_hours_kst'], [3])
        self.assertEqual((pattern['cheapest_median'], pattern['expensive_median']), (10, 300))
        flat = [(iso, 100) for iso, _price in points]
        self.assertEqual(hourly_pattern(flat, 9)['status'], '데이터 부족')
        self.assertIn('같습니다', hourly_pattern(flat, 9)['note'])

    def test_house_match_metric_ties_and_missing_auctionator(self):
        auctionator = {1: 100, 2: 100, 3: 100, 4: 100, 5: 100}
        identical = {'house_id': 113, 'side': 'horde', 'name': '호드', 'prices': dict(auctionator)}
        doubled = {'house_id': 116, 'side': 'blackwater', 'name': '검은바다',
                   'prices': {item_id: price * 2 for item_id, price in auctionator.items()}}
        mixed = {'house_id': 119, 'side': 'horde', 'name': '호드',
                 'prices': {1: 100, 2: 100, 3: 101, 4: 101, 5: 200}}
        ranked = match_price_maps(auctionator, [doubled, mixed, identical])
        self.assertEqual(ranked['status'], '추정')
        self.assertEqual(ranked['house_id'], 113)
        self.assertEqual(ranked['median_relative_error_bp'], 0)
        self.assertEqual(ranked['shared_items'], 5)
        self.assertEqual(next(row['median_relative_error_bp'] for row in ranked['candidates']
                              if row['house_id'] == 119), 100)
        self.assertEqual(next(row['median_relative_error_bp'] for row in ranked['candidates']
                              if row['house_id'] == 116), 10000)
        tie = match_price_maps(auctionator, [
            {'house_id': 200, 'prices': dict(auctionator)},
            {'house_id': 116, 'prices': dict(auctionator)},
        ])
        self.assertEqual(tie['house_id'], 116)
        self.assertEqual(sorted(tie['tied_house_ids']), [116, 200])
        self.assertIn('확정이 아닙니다', tie['note'])
        missing = match_price_maps({}, [identical])
        self.assertIsNone(missing['house_id'])
        self.assertIn('Auctionator', missing['note'])
        self.assertIn('추정하지 않습니다', missing['note'])
        thin = match_price_maps({1: 100}, [identical])
        self.assertIsNone(thin['house_id'])
        self.assertIn('5개', thin['note'])
        empty = match_price_maps(auctionator, [])
        self.assertIsNone(empty['house_id'])
        self.assertIn('스냅샷이 없습니다', empty['note'])

    def test_selected_house_requires_one_id(self):
        self.assertEqual(selected_house_id({'blizzard_forever': {'house_id': 113, 'house_ids': [114, 115]}}), 113)
        self.assertEqual(selected_house_id({'blizzard_forever': {'house_ids': [114]}}), 114)
        self.assertIsNone(selected_house_id({'blizzard_forever': {'house_ids': [113, 114]}}))
        self.assertIsNone(selected_house_id({}))
        with self.assertRaises(AppError):
            selected_house_id({'blizzard_forever': {'ruleset': True}})


class StoreTests(Base):
    def _capture(self, fn):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = fn()
        return result, buf.getvalue()

    def test_last_modified_partial_change_copies_unchanged_side(self):
        fixture = load_fixture()
        from blizzard_collector import ForeverAuctions
        api = ForeverAuctions(self.cfg, fixture)
        self._capture(lambda: collect_once(self.db, api, self.cfg, now='2026-11-12T00:00:00+00:00'))
        self.assertEqual([row[0] for row in self.db.execute(
            'SELECT house_id FROM blizzard_snapshots ORDER BY house_id')], [113, 115, 116, 119, 125])
        self.assertEqual(self.db.execute(
            "SELECT side FROM blizzard_houses WHERE house_id=110").fetchone()[0], 'alliance')
        self.assertIsNone(self.db.execute(
            'SELECT id FROM blizzard_snapshots WHERE house_id=110').fetchone())
        self.assertEqual(self.db.execute(
            "SELECT side FROM blizzard_houses WHERE house_id=121").fetchone()[0], 'unknown')
        self.assertIsNone(self.db.execute('SELECT house_id FROM blizzard_houses WHERE house_id=122').fetchone())
        prices = {row['item_id']: row for row in self.db.execute(
            '''SELECT p.* FROM blizzard_item_prices p JOIN blizzard_snapshots s ON s.id=p.snapshot_id
               WHERE s.house_id=113''')}
        self.assertEqual((prices[7]['min_unit_price'], prices[7]['median_unit_price'], prices[7]['p25_unit_price'],
                          prices[7]['quantity'], prices[7]['listing_count']), (100, 300, 200, 16, 4))
        self.assertEqual((prices[8]['min_unit_price'], prices[8]['median_unit_price'], prices[8]['p25_unit_price'],
                          prices[8]['quantity'], prices[8]['listing_count']), (3, 250, 3, 7, 2))
        counts = self.db.execute(
            'SELECT n_auctions,n_commodities,n_priced,auctions_last_modified FROM blizzard_snapshots WHERE house_id=113'
        ).fetchone()
        self.assertEqual((counts['n_auctions'], counts['n_commodities'], counts['n_priced']), (5, 4, 6))
        self.assertEqual(counts['auctions_last_modified'], 'Wed, 12 Nov 2026 00:13:00 GMT')
        self.assertTrue(self.db.execute(
            'SELECT fetched_at FROM blizzard_snapshots WHERE house_id=113').fetchone()[0].endswith('+00:00'))
        stack = self.db.execute(
            '''SELECT unit_price FROM blizzard_listings l JOIN blizzard_snapshots s ON s.id=l.snapshot_id
               WHERE s.house_id=113 AND l.listing_id=12''').fetchone()[0]
        self.assertEqual(stack, 3)
        self.assertIsNone(self.db.execute(
            '''SELECT listing_id FROM blizzard_listings l JOIN blizzard_snapshots s ON s.id=l.snapshot_id
               WHERE s.house_id=113 AND l.listing_id=11''').fetchone())
        commodity = self.db.execute(
            '''SELECT p.min_unit_price,p.quantity,l.unit_price FROM blizzard_item_prices p
               JOIN blizzard_snapshots s ON s.id=p.snapshot_id
               JOIN blizzard_listings l ON l.snapshot_id=s.id AND l.item_id=p.item_id
               WHERE s.house_id=116 AND p.item_id=9''').fetchone()
        self.assertEqual((commodity['min_unit_price'], commodity['quantity'], commodity['unit_price']), (80, 20, 80))
        self.assertEqual(self.db.execute(
            '''SELECT COUNT(*) FROM blizzard_item_prices p JOIN blizzard_snapshots s ON s.id=p.snapshot_id
               WHERE s.house_id=115''').fetchone()[0], 0)
        gear = self.db.execute(
            '''SELECT p.min_unit_price,l.time_left,s.n_auctions FROM blizzard_item_prices p
               JOIN blizzard_snapshots s ON s.id=p.snapshot_id
               JOIN blizzard_listings l ON l.snapshot_id=s.id
               WHERE s.house_id=119''').fetchone()
        self.assertEqual((gear['min_unit_price'], gear['time_left'], gear['n_auctions']), (45, 'MEDIUM', 2))

        fixture[commodity_path(1, 113)]['last_modified'] = 'Thu, 12 Nov 2026 01:00:00 GMT'
        fixture[commodity_path(1, 113)]['body']['auctions'][0]['unit_price'] = 150
        _chosen, text = self._capture(lambda: collect_once(
            self.db, api, self.cfg, now='2026-11-12T01:00:00+00:00'))
        self.assertIn('경매장 113', text)
        self.assertIn('스냅샷', text)
        self.assertIn('Last-Modified 동일', text)
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM blizzard_snapshots WHERE house_id=113').fetchone()[0], 2)
        self.assertEqual(self.db.execute(
            'SELECT COUNT(*) FROM blizzard_snapshots WHERE house_id=116').fetchone()[0], 1)
        newest = self.db.execute(
            '''SELECT p.item_id,p.min_unit_price FROM blizzard_item_prices p
               JOIN blizzard_snapshots s ON s.id=p.snapshot_id
               WHERE s.house_id=113 ORDER BY s.fetched_at DESC, s.id DESC LIMIT 2''').fetchall()
        self.assertEqual({row['item_id']: row['min_unit_price'] for row in newest}, {7: 150, 8: 3})
        copied = self.db.execute(
            '''SELECT l.unit_price FROM blizzard_listings l
               JOIN blizzard_snapshots s ON s.id=l.snapshot_id
               WHERE s.house_id=113 AND s.fetched_at='2026-11-12T01:00:00+00:00' AND l.listing_id=10''').fetchone()
        self.assertEqual(copied['unit_price'], 250)
        self.assertEqual(self.db.execute(
            '''SELECT p.min_unit_price FROM blizzard_item_prices p JOIN blizzard_snapshots s ON s.id=p.snapshot_id
               WHERE s.house_id=113 AND s.fetched_at='2026-11-12T00:00:00+00:00' AND p.item_id=7''').fetchone()[0], 100)

    def test_missing_last_modified_does_not_skip(self):
        from blizzard_collector import ForeverAuctions
        fixture = {
            INDEX: {'body': {'auctions': [{'id': 113, 'name': '호드'}]}},
            '/data/wow/ruleset/1/auctions/113': {'body': {'auctions': []}},
            commodity_path(1, 113): {'body': {'auctions': [
                {'id': 1, 'item': {'id': 1}, 'quantity': 1, 'unit_price': 10, 'time_left': 'LONG'}]}},
        }
        api = ForeverAuctions(self.cfg, fixture)
        self._capture(lambda: collect_once(self.db, api, self.cfg, now='2026-11-12T00:00:00+00:00'))
        _chosen, text = self._capture(lambda: collect_once(self.db, api, self.cfg, now='2026-11-12T01:00:00+00:00'))
        self.assertNotIn('생략', text)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM blizzard_checks WHERE stored=1').fetchone()[0], 2)

    def test_duplicate_last_modified_pair_is_rejected(self):
        with self.db:
            upsert_houses(self.db, 1, [{'id': 113, 'name': '호드', 'side': 'horde'}], '2026-11-12T00:00:00+00:00')
            store_snapshot(self.db, 113, 1, '2026-11-12T00:00:00+00:00', [], 0, 0, 'same', 'same')
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:
                store_snapshot(self.db, 113, 1, '2026-11-12T01:00:00+00:00', [], 0, 0, 'same', 'same')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 1)

    def _history(self, house_id, times):
        with self.db:
            upsert_houses(self.db, 1, [{'id': house_id, 'name': '호드', 'side': 'horde'}], times[0])
            for index, stamp in enumerate(times):
                listings = [priced('commodity', 100 + n, 1, 100, 1, 'VERY_LONG') for n in range(7)]
                if index % 2 == 0:
                    listings += [priced('commodity', 200 + n, 1, 100, 1, 'VERY_LONG') for n in range(3)]
                    listings.append(priced('commodity', 300, 1, 100, 1, 'SHORT'))
                else:
                    listings.append(priced('commodity', 400, 1, 100, 1, 'LONG'))
                store_snapshot(self.db, house_id, 1, stamp, listings, 0, len(listings), f'A{house_id}-{index}',
                               f'C{house_id}-{index}')

    def test_disappearance_is_activity_only_and_never_fills_sale_probability(self):
        times = ['2026-11-10T00:00:00+00:00', '2026-11-10T01:00:00+00:00',
                 '2026-11-11T00:00:00+00:00', '2026-11-11T01:00:00+00:00',
                 '2026-11-12T00:00:00+00:00', '2026-11-12T01:00:00+00:00']
        self._history(113, times)
        estimate = item_sell_through(self.db, 113, 1, 1)
        self.assertEqual((estimate['disappeared'], estimate['survived'], estimate['classified']), (9, 35, 44))
        self.assertEqual(estimate['disappearance_pct'], 20)
        self.assertNotIn('sell_through_pct', estimate)
        row = item()
        attach_blizzard(self.db, dict(self.cfg, blizzard_forever={'house_id': 113}), {1: row}, NOW, None)
        self.assertEqual(row['blizzard']['houses'][0]['disappearance_pct'], 20)
        result = plan_actions(bundle([row], vendors=[vendor()]), listing(deposit_copper_per_item=100), NOW)
        self.assertIsNone(result['vendors'][0]['official_net'])
        self.assertIsNone(result['vendors'][0]['expected_deposit_loss'])
        self.assertEqual(result['buy_recommendations'], [])
        self.assertEqual(result['sell_through_basis'], '데이터 부족')

    def test_short_history_activity_stays_unknown(self):
        self._history(114, ['2026-11-12T00:00:00+00:00', '2026-11-12T01:00:00+00:00'])
        estimate = item_sell_through(self.db, 114, 1, 1)
        self.assertIsNone(estimate['disappearance_pct'])
        self.assertEqual(estimate['disappearance_status'], '데이터 부족')

    def test_only_explicit_user_probability_applies_even_with_legacy_stale_estimates(self):
        row = item()
        row['blizzard'] = {'selected_house_id': 113, 'houses': [{
            'house_id': 113, 'sell_through_pct': 100, 'sell_through_status': '추정',
            'disappearance_pct': 100, 'disappearance_status': '추정',
            'observed': '2026-10-01T00:00:00+00:00', 'liquidity': '관찰 필요'}]}
        for pct, loss in [(None, None), (0, None), (10, 900), (100, 0)]:
            cfg = listing(deposit_copper_per_item=100, sell_through_pct=pct)
            result = plan_actions(bundle([row], vendors=[vendor()]), cfg, NOW)
            action = result['vendors'][0]
            self.assertEqual(action['sell_through_pct'], pct)
            self.assertEqual(action['expected_deposit_loss'], loss)
            if pct in (None, 0):
                self.assertIsNone(action['official_net'])
                self.assertEqual(result['buy_recommendations'], [])
            else:
                self.assertEqual(action['official_net'], 5079-loss)
                self.assertEqual(action['sell_through_label'], '사용자 설정')

    def test_dashboard_keeps_auctionator_price_and_does_not_guess_a_house(self):
        self.db.execute("INSERT INTO realms VALUES (-1, '[\"PvP\"]')")
        set_meta(self.db, 'auctionator_local_realm_id', -1)
        ensure_items(self.db, [7])
        self.db.execute("INSERT INTO snapshots VALUES (1,'auctionator',-1,'2026-11-12T00:00:00+00:00','synthetic',NULL)")
        self.db.execute("INSERT INTO prices VALUES (1,7,1000,NULL,NULL,NULL,'2026-11-12T00:00:00+00:00')")
        with self.db:
            upsert_houses(self.db, 1, [{'id': 113, 'name': '호드', 'side': 'horde'}], '2026-11-12T00:00:00+00:00')
            store_snapshot(self.db, 113, 1, '2026-11-12T00:00:00+00:00',
                           [priced('auction', 1, 7, 250, 1, 'LONG')], 1, 0, 'A', 'C')
        data = build_market(self.db, self.cfg, NOW)
        found = next(entry for entry in data['items'] if entry['id'] == 7)
        self.assertEqual(found['price'], 1000)
        house = found['blizzard']['houses'][0]
        self.assertEqual(house['min_unit_price'], 250)
        self.assertEqual(house['hourly']['status'], '데이터 부족')
        self.assertEqual(house['disappearance_status'], '데이터 부족')
        self.assertEqual(house['liquidity'], '관찰 필요')
        self.assertEqual(house['distinct_days'], 1)
        self.assertIsNone(data['blizzard']['match']['house_id'])
        self.assertIsNone(data['metrics']['weighted_median'])
        self.assertIsNone(data['metrics']['sales_per_day'])
        self.assertEqual(data['actions']['buy_recommendations'], [])
        text = report_of(data, data['actions'])
        self.assertIn('소멸 지표 데이터 부족', text)
        self.assertIn('시간대 데이터 부족', text)
        lines = summary_lines(self.db, self.cfg, -1)
        self.assertTrue(any('추정하지 않습니다' in line for line in lines))
        self.assertNotIn('수익 추천 0개', '\n'.join(lines))

    def test_retail_source_check_and_missing_table_do_not_invent_data(self):
        self.db.execute("INSERT INTO realms VALUES (9, '[\"x\"]')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO snapshots(source,realm_id,fetched_at,last_modified) VALUES ('blizzard',9,?,?)",
                ('2026-11-12T00:00:00+00:00', 'x'))
        with self.assertRaises(AppError):
            Blizzard({'game_flavor': 'forever'})
        raw = sqlite3.connect(':memory:')
        raw.row_factory = sqlite3.Row
        summary = attach_blizzard(raw, {}, {1: {'id': 1}}, NOW, None)
        self.assertIn('테이블이 없습니다', summary['match']['note'])
        self.assertEqual(item_sell_through(raw, 113, 1, 1)['disappearance_status'], '데이터 부족')
        self.assertTrue(any('테이블이 없습니다' in line for line in summary_lines(raw, {}, None)))
        raw.close()

    def test_forever_report_keeps_three_unknown_profit_lines(self):
        self.cfg['auctionator_realm_key'] = 'PvP'
        text = report(self.db, self.cfg, NOW)
        self.assertEqual(text.count('수익 추천 0개'), 3)
        self.assertIn('블리자드 Forever 경매장', text)
        self.assertIn('매물 소멸은 판매 확률로 사용하지 않습니다', text)
        self.assertNotIn('config.forever.json', text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = write_cfg(self.root)
        self.db_path = self.root / 'ah.db'
        self.env_before = {key: os.environ.get(key) for key in ('BNET_CLIENT_ID', 'BNET_CLIENT_SECRET')}
        os.environ.pop('BNET_CLIENT_ID', None)
        os.environ.pop('BNET_CLIENT_SECRET', None)

    def tearDown(self):
        for key, value in self.env_before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def run_main(self, extra):
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(['--config', str(self.cfg), '--db', str(self.db_path), *extra])
        return buf.getvalue()

    def open_db(self):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def test_two_runs_skip_unchanged_without_network(self):
        with patch('blizzard_collector.http_json', side_effect=AssertionError('network')) as http:
            first = self.run_main(['--fixture', str(FIXTURE)])
            second = self.run_main(['--fixture', str(FIXTURE)])
        self.assertFalse(http.called)
        self.assertIn('호드+검은바다, 얼라이언스 제외', first)
        self.assertIn('스냅샷', first)
        self.assertNotIn('생략', first)
        self.assertIn('Last-Modified 동일', second)
        db = self.open_db()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 5)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM blizzard_checks').fetchone()[0], 10)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM blizzard_checks WHERE stored=0').fetchone()[0], 5)
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM blizzard_checks WHERE note='Last-Modified 동일'").fetchone()[0], 5)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM blizzard_snapshots WHERE house_id=110').fetchone()[0], 0)
        self.assertEqual({row[0] for row in db.execute('SELECT item_id FROM items')}, {7, 8, 9})
        db.close()

    def test_fixture_mode_does_not_read_env_or_store_secrets(self):
        env = self.root / 'bnet.env'
        env.write_text(f'BNET_CLIENT_ID={FAKE_ID}\nBNET_CLIENT_SECRET={FAKE_SECRET}\n', encoding='utf-8')
        with patch('blizzard_collector.http_json', side_effect=AssertionError('network')):
            text = self.run_main(['--fixture', str(FIXTURE), '--env', str(env)])
        self.assertNotIn(FAKE_SECRET, text)
        self.assertNotIn(FAKE_ID, text)
        self.assertNotIn('Authorization', text)
        self.assertNotIn('BNET_CLIENT_SECRET', os.environ)
        blob = self.db_path.read_bytes()
        self.assertNotIn(FAKE_SECRET.encode(), blob)
        self.assertNotIn(FAKE_ID.encode(), blob)
        self.assertNotIn(b'Authorization', blob)

    def test_retail_config_does_not_open_a_database(self):
        retail_db = self.root / 'retail.db'
        with self.assertRaises(AppError) as caught:
            main(['--config', str(ROOT / 'config.example.json'), '--db', str(retail_db), '--fixture', str(FIXTURE)])
        self.assertIn('forever', str(caught.exception))
        self.assertFalse(retail_db.exists())

    def test_fixture_requires_db_and_match_does_not_guess(self):
        with self.assertRaises(AppError) as caught:
            main(['--config', str(self.cfg), '--fixture', str(FIXTURE)])
        self.assertIn('--db', str(caught.exception))
        with patch('blizzard_collector.http_json', side_effect=AssertionError('network')) as http:
            text = self.run_main(['--fixture', str(FIXTURE), '--match'])
        self.assertFalse(http.called)
        self.assertIn('Auctionator', text)
        self.assertIn('추정 경매장 없음', text)
        db = self.open_db()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 0)
        db.close()

    def test_alliance_and_explicit_ids_are_opt_in(self):
        raw = json.loads(self.cfg.read_text(encoding='utf-8'))
        raw['blizzard_forever']['include_alliance'] = True
        self.cfg.write_text(json.dumps(raw), encoding='utf-8')
        text = self.run_main(['--fixture', str(FIXTURE)])
        self.assertIn('호드+검은바다+얼라이언스', text)
        db = self.open_db()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 7)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM blizzard_snapshots WHERE house_id IN (110,112)').fetchone()[0], 2)
        db.close()
        for suffix in ('', '-wal', '-shm'):
            sidecar = Path(str(self.db_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        explicit = self.run_main(['--fixture', str(FIXTURE), '--houses', '110,121'])
        self.assertIn('설정한 경매장', explicit)
        db = self.open_db()
        self.assertEqual(sorted(row[0] for row in db.execute('SELECT house_id FROM blizzard_snapshots')), [110, 121])
        db.close()
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(AppError) as caught:
                main(['--config', str(self.cfg), '--db', str(self.root / 'missing.db'),
                      '--fixture', str(FIXTURE), '--houses', '122'])
        self.assertIn('122', str(caught.exception))
        self.assertIn('색인', str(caught.exception))
        missing = sqlite3.connect(self.root / 'missing.db')
        self.assertEqual(missing.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 0)
        self.assertEqual(missing.execute('SELECT COUNT(*) FROM blizzard_houses').fetchone()[0], 0)
        note = missing.execute('SELECT note FROM runs').fetchone()[0]
        self.assertEqual(note.split(': ', 1)[1], 'AppError')
        self.assertNotIn('122', note)
        missing.close()

    def test_house_error_keeps_earlier_snapshots_and_logs_the_type_only(self):
        data = load_fixture()
        del data[commodity_path(1, 125)]
        broken = self.root / 'broken.json'
        broken.write_text(json.dumps(data), encoding='utf-8')
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(AppError) as caught:
                main(['--config', str(self.cfg), '--db', str(self.db_path), '--fixture', str(broken)])
        text = buf.getvalue()
        self.assertIn('경매장 125: 오류 AppError', text)
        self.assertNotIn('샘플 API', text)
        self.assertIn('보존했습니다', str(caught.exception))
        db = self.open_db()
        self.assertEqual(sorted(row[0] for row in db.execute('SELECT house_id FROM blizzard_snapshots')),
                         [113, 115, 116, 119])
        note = db.execute('SELECT note FROM runs').fetchone()[0]
        self.assertEqual(note.split(': ', 1)[1], 'AppError')
        self.assertNotIn('샘플', note)
        db.close()


class HttpJsonTests(unittest.TestCase):
    def test_403_then_200_retries_and_keeps_user_agent(self):
        seen = []
        responses = [
            _http_error(403, headers={'X-Test': 'HEADER-MARKER'}),
            _JsonResponse({'auctions': []}, 'Wed, 12 Nov 2026 01:00:00 GMT'),
        ]

        def fake(req, timeout=60):
            seen.append(req.get_header('User-agent'))
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        caller = {}
        with patch('common.time.sleep') as sleep, patch('common.urlopen', side_effect=fake):
            body, modified = http_json('https://kr.api.blizzard.com/data/wow/item/1', caller)
        self.assertEqual(body, {'auctions': []})
        self.assertEqual(modified, 'Wed, 12 Nov 2026 01:00:00 GMT')
        self.assertEqual(seen, [DEFAULT_USER_AGENT, DEFAULT_USER_AGENT])
        self.assertEqual(caller, {})
        self.assertEqual(sleep.call_count, 1)

    def test_exhausted_403_omits_body_headers_and_credentials(self):
        body = b'{"error":"BODY-MARKER","access_token":"fixture-token","client_secret":"SECRET-MARKER"}'
        responses = [_http_error(403, body=body, headers={'X-Test': 'HEADER-MARKER', 'Retry-After': '0'})
                     for _ in range(HTTP_ATTEMPTS)]
        with patch('common.time.sleep') as sleep, patch('common.urlopen', side_effect=responses):
            with self.assertRaises(AppError) as caught:
                http_json('https://kr.api.blizzard.com/data/wow/item/1',
                          {'Authorization': 'Bearer fixture-token'})
        message = str(caught.exception)
        self.assertIn('403', message)
        for secret in ('BODY-MARKER', 'HEADER-MARKER', 'fixture-token', 'SECRET-MARKER',
                       'Authorization', 'Bearer', 'client_secret'):
            self.assertNotIn(secret, message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(sleep.call_count, HTTP_ATTEMPTS - 1)

    def test_429_honors_retry_after_and_503_uses_backoff(self):
        responses = [
            _http_error(429, headers={'Retry-After': '2'}),
            _JsonResponse({'ok': 1}),
            _http_error(503),
            _JsonResponse({'ok': 2}),
        ]
        sleeps = []
        with patch('common.random.uniform', return_value=0), \
                patch('common.time.sleep', side_effect=sleeps.append), \
                patch('common.urlopen', side_effect=responses):
            first, _ = http_json('https://kr.api.blizzard.com/data/wow/item/1', {})
            second, _ = http_json('https://kr.api.blizzard.com/data/wow/item/1', {})
        self.assertEqual((first, second), ({'ok': 1}, {'ok': 2}))
        self.assertEqual(sleeps, [2, 1])

    def test_400_401_and_404_are_not_retried(self):
        for code in (400, 401, 404):
            with patch('common.time.sleep') as sleep, \
                    patch('common.urlopen', side_effect=_http_error(code)) as opened:
                with self.assertRaises(AppError) as caught:
                    http_json('https://kr.api.blizzard.com/data/wow/item/1', {})
            self.assertIn(str(code), str(caught.exception))
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(sleep.call_count, 0)

    def test_explicit_user_agent_is_preserved(self):
        seen = []

        def fake(req, timeout=60):
            seen.append(req.get_header('User-agent'))
            return _JsonResponse({'ok': True})

        custom = {'user-agent': 'CustomAgent/9'}
        with patch('common.urlopen', side_effect=fake):
            http_json('https://kr.api.blizzard.com/data/wow/item/1', custom)
            http_json('https://kr.api.blizzard.com/data/wow/item/1', {'User-Agent': 'Other/1'})
        self.assertEqual(seen, ['CustomAgent/9', 'Other/1'])
        self.assertEqual(custom, {'user-agent': 'CustomAgent/9'})

    def test_304_returns_none_without_retry(self):
        with patch('common.time.sleep') as sleep, \
                patch('common.urlopen', side_effect=_http_error(304, body=b'BODY-MARKER')) as opened:
            self.assertEqual(http_json('https://kr.api.blizzard.com/data/wow/item/1', {}), (None, None))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(sleep.call_count, 0)

    def test_remote_disconnect_then_200_retries_and_succeeds(self):
        responses = [
            RemoteDisconnected('Remote end closed connection without response'),
            _JsonResponse({'ok': True}),
        ]
        with patch('common.time.sleep') as sleep, patch('common.urlopen', side_effect=responses) as opened:
            body, modified = http_json('https://kr.api.blizzard.com/data/wow/item/1', {})
        self.assertEqual(body, {'ok': True})
        self.assertEqual(modified, 'Wed, 12 Nov 2026 00:00:00 GMT')
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_urlerror_and_incomplete_read_are_retried(self):
        responses = [
            URLError(RemoteDisconnected('Remote end closed connection without response')),
            IncompleteRead(b'{"ok":', expected=20),
            _JsonResponse({'ok': 1}),
        ]
        with patch('common.time.sleep') as sleep, patch('common.urlopen', side_effect=responses):
            body, _ = http_json('https://kr.api.blizzard.com/data/wow/item/1', {})
        self.assertEqual(body, {'ok': 1})
        self.assertEqual(sleep.call_count, 2)

    def test_persistent_disconnect_fails_after_max_attempts(self):
        with patch('common.time.sleep') as sleep, \
                patch('common.urlopen', side_effect=RemoteDisconnected('closed')) as opened:
            with self.assertRaises(AppError) as caught:
                http_json('https://kr.api.blizzard.com/data/wow/item/1', {})
        message = str(caught.exception)
        self.assertIn('네트워크', message)
        self.assertNotIn('closed', message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(opened.call_count, HTTP_ATTEMPTS)
        self.assertEqual(sleep.call_count, HTTP_ATTEMPTS - 1)

    def test_slow_body_exceeding_deadline_is_retried_then_fails(self):
        class SlowResponse:
            headers = {'Last-Modified': 'Wed, 12 Nov 2026 00:00:00 GMT'}

            def read(self, n=-1):
                clock['t'] += HTTP_DEADLINE + 1
                return b'{"ok":'

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        clock = {'t': 0.0}

        def fake(req, timeout=60):
            return SlowResponse()

        with patch('common.time.monotonic', side_effect=lambda: clock['t']), \
                patch('common.time.sleep') as sleep, \
                patch('common.urlopen', side_effect=fake) as opened:
            with self.assertRaises(AppError) as caught:
                http_json('https://kr.api.blizzard.com/data/wow/item/1', {})
        self.assertIn('네트워크', str(caught.exception))
        self.assertNotIn('{"ok":', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(opened.call_count, HTTP_ATTEMPTS)
        self.assertEqual(sleep.call_count, HTTP_ATTEMPTS - 1)


class UnpublishedHouseTests(Base):
    def _capture(self, fn):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = fn()
        return result, buf.getvalue()

    def test_missing_auctions_array_skips_without_failing_the_run(self):
        from blizzard_collector import ForeverAuctions
        fixture = sided_fixture([
            (113, '호드', PRICED_AUCTIONS, PRICED_COMMODITIES),
            (115, '호드', dict(LINKS), dict(LINKS)),
            (118, '호드', {'id': 118}, dict(LINKS)),
            (120, '호드', {'auctions': None, '_links': {}}, {'auctions': []}),
            (125, '호드', {'auctions': []}, {'auctions': []}),
        ])
        api = ForeverAuctions(self.cfg, fixture)
        _chosen, text = self._capture(lambda: collect_once(
            self.db, api, self.cfg, now='2026-11-13T00:00:00+00:00'))
        self.assertNotIn('오류', text)
        for house_id in (115, 118, 120):
            self.assertIn(f'경매장 {house_id}: 매물 목록 없음, 스냅샷 생략 (확인 #', text)
        self.assertEqual(sorted(row[0] for row in self.db.execute(
            'SELECT house_id FROM blizzard_snapshots')), [113, 125])
        empty = self.db.execute(
            'SELECT n_auctions, n_commodities, n_priced FROM blizzard_snapshots WHERE house_id=125').fetchone()
        self.assertEqual((empty['n_auctions'], empty['n_commodities'], empty['n_priced']), (0, 0, 0))
        price = self.db.execute('''SELECT p.item_id, p.min_unit_price FROM blizzard_item_prices p
            JOIN blizzard_snapshots s ON s.id=p.snapshot_id WHERE s.house_id=113 ORDER BY p.item_id''').fetchall()
        self.assertEqual([(row['item_id'], row['min_unit_price']) for row in price], [(7, 50), (8, 100)])
        notes = {row['house_id']: (row['stored'], row['note'], row['snapshot_id']) for row in self.db.execute(
            'SELECT house_id, stored, note, snapshot_id FROM blizzard_checks')}
        for house_id in (115, 118, 120):
            self.assertEqual(notes[house_id][0], 0)
            self.assertEqual(notes[house_id][1], '매물 목록 없음')
            self.assertIsNone(notes[house_id][2])
        self.assertEqual(notes[125][0], 1)
        self.assertEqual(notes[113][0], 1)

    def test_later_missing_auctions_keeps_the_priced_snapshot(self):
        from blizzard_collector import ForeverAuctions
        fixture = sided_fixture([(113, '호드', PRICED_AUCTIONS, PRICED_COMMODITIES)])
        api = ForeverAuctions(self.cfg, fixture)
        self._capture(lambda: collect_once(self.db, api, self.cfg, now='2026-11-12T00:00:00+00:00'))
        fixture['/data/wow/ruleset/1/auctions/113'] = {
            'last_modified': 'Thu, 13 Nov 2026 00:00:00 GMT', 'body': {'id': 113}}
        fixture[commodity_path(1, 113)] = {
            'last_modified': 'Thu, 13 Nov 2026 00:00:01 GMT', 'body': dict(LINKS)}
        _chosen, text = self._capture(lambda: collect_once(
            self.db, api, self.cfg, now='2026-11-13T00:00:00+00:00'))
        self.assertIn('경매장 113: 매물 목록 없음, 스냅샷 생략 (확인 #', text)
        self.assertNotIn('오류', text)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 1)
        prices = self.db.execute(
            'SELECT item_id, min_unit_price FROM blizzard_item_prices ORDER BY item_id').fetchall()
        self.assertEqual([(row['item_id'], row['min_unit_price']) for row in prices], [(7, 50), (8, 100)])
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM blizzard_checks WHERE note='매물 목록 없음' AND stored=0").fetchone()[0], 1)

    def test_explicit_empty_arrays_still_store_an_empty_snapshot(self):
        from blizzard_collector import ForeverAuctions
        api = ForeverAuctions(self.cfg, sided_fixture([
            (115, '호드', {'auctions': []}, {'auctions': []})]))
        _chosen, text = self._capture(lambda: collect_once(
            self.db, api, self.cfg, now='2026-11-13T00:00:00+00:00'))
        self.assertNotIn('매물 목록 없음', text)
        self.assertNotIn('오류', text)
        row = self.db.execute(
            'SELECT n_auctions, n_commodities, n_priced FROM blizzard_snapshots WHERE house_id=115').fetchone()
        self.assertEqual((row['n_auctions'], row['n_commodities'], row['n_priced']), (0, 0, 0))
        self.assertEqual(self.db.execute("SELECT note FROM blizzard_checks").fetchone()[0], '저장')

    def test_unpublished_side_does_not_store_a_partial_snapshot(self):
        from blizzard_collector import ForeverAuctions
        api = ForeverAuctions(self.cfg, sided_fixture([
            (113, '호드', PRICED_AUCTIONS, dict(LINKS))]))
        _chosen, text = self._capture(lambda: collect_once(
            self.db, api, self.cfg, now='2026-11-13T00:00:00+00:00'))
        self.assertIn('매물 목록 없음', text)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM blizzard_item_prices').fetchone()[0], 0)

    def test_non_list_auctions_still_fails_without_hiding_the_sibling(self):
        from blizzard_collector import ForeverAuctions
        api = ForeverAuctions(self.cfg, sided_fixture([
            (113, '호드', {'auctions': {}}, {'auctions': []}),
            (116, '검은바다', PRICED_AUCTIONS, {'auctions': []}),
        ]))
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(AppError) as caught:
                collect_once(self.db, api, self.cfg, now='2026-11-13T00:00:00+00:00')
        text = buf.getvalue()
        self.assertIn('경매장 113: 오류 AppError', text)
        self.assertNotIn('매물 목록 없음', text)
        self.assertIn('113', str(caught.exception))
        self.assertNotIn('116', str(caught.exception))
        self.assertEqual([row[0] for row in self.db.execute('SELECT house_id FROM blizzard_snapshots')], [116])

    def test_exhausted_403_skips_one_house_and_keeps_the_other(self):
        from blizzard_collector import ForeverAuctions
        saved = _swap_env(BNET_CLIENT_ID=FAKE_ID, BNET_CLIENT_SECRET=FAKE_SECRET)
        try:
            api = ForeverAuctions(self.cfg)
            api.request_interval = 0
            api.token = 'fixture-token'
            api.expires = time.time() + 3600
            calls = []

            def fake(req, timeout=60):
                url = req.full_url
                calls.append(url)
                if '/auctions/116' in url and '/commodities' not in url:
                    raise _http_error(
                        403, body=b'{"error":"BODY-MARKER","access_token":"fixture-token"}',
                        headers={'X-Test': 'HEADER-MARKER'})
                if '/auctions/index?' in url:
                    return _JsonResponse({'auctions': [
                        {'id': 116, 'name': '검은바다'}, {'id': 113, 'name': '호드'}]})
                if '/auctions/113/commodities' in url:
                    return _JsonResponse(PRICED_COMMODITIES)
                if '/auctions/113?' in url or '/auctions/113' in url:
                    return _JsonResponse(PRICED_AUCTIONS)
                raise AssertionError(url.split('?', 1)[0])

            buf = io.StringIO()
            with patch('common.urlopen', side_effect=fake), patch('common.time.sleep') as sleep:
                with redirect_stdout(buf):
                    with self.assertRaises(AppError) as caught:
                        collect_once(self.db, api, self.cfg, now='2026-11-13T00:00:00+00:00')
            text = buf.getvalue()
            message = str(caught.exception)
            context = str(caught.exception.__context__ or '')
            self.assertIn('116', message)
            self.assertNotIn('113', message)
            self.assertIn('보존했습니다', message)
            self.assertIn('경매장 116: 오류 AppError', text)
            self.assertIn('경매장 113', text)
            for secret in (FAKE_ID, FAKE_SECRET, 'fixture-token', 'BODY-MARKER', 'HEADER-MARKER',
                           'Authorization', 'Bearer'):
                self.assertNotIn(secret, text)
                self.assertNotIn(secret, message)
                self.assertNotIn(secret, context)
            failed = [url for url in calls if '/auctions/116' in url and '/commodities' not in url]
            self.assertEqual(len(failed), HTTP_ATTEMPTS)
            self.assertEqual(sleep.call_count, HTTP_ATTEMPTS - 1)
            self.assertEqual([row[0] for row in self.db.execute(
                'SELECT house_id FROM blizzard_snapshots')], [113])
            self.assertEqual(self.db.execute('''SELECT min_unit_price FROM blizzard_item_prices p
                JOIN blizzard_snapshots s ON s.id=p.snapshot_id
                WHERE s.house_id=113 AND p.item_id=8''').fetchone()[0], 100)
            blob = b''.join(path.read_bytes() for path in (
                self.root / 'test.db', Path(str(self.root / 'test.db') + '-wal'),
                Path(str(self.root / 'test.db') + '-shm')) if path.exists())
            self.assertNotIn(FAKE_SECRET.encode(), blob)
            self.assertNotIn(b'fixture-token', blob)
            self.assertNotIn(b'BODY-MARKER', blob)
        finally:
            _restore_env(saved)

    def test_fixture_get_does_not_sleep_and_live_get_paces(self):
        from blizzard_collector import ForeverAuctions
        api = ForeverAuctions(self.cfg, sided_fixture([
            (113, '호드', {'auctions': []}, {'auctions': []})]))
        self.assertEqual(api.request_interval, 0)
        with patch('blizzard_collector.time.sleep') as slept:
            api.get(INDEX)
            api.get('/data/wow/ruleset/1/auctions/113')
        self.assertEqual(slept.call_count, 0)
        saved = _swap_env(BNET_CLIENT_ID=FAKE_ID, BNET_CLIENT_SECRET=FAKE_SECRET)
        try:
            live = ForeverAuctions(self.cfg)
            live.token = 'fixture-token'
            live.expires = time.time() + 3600
            self.assertEqual(live.request_interval, 0.1)
            with patch('blizzard_collector.http_json', return_value=({'auctions': []}, 'lm')), \
                    patch('blizzard_collector.time.monotonic', side_effect=[10.0, 10.0, 10.1]), \
                    patch('blizzard_collector.time.sleep') as slept:
                live.get(INDEX)
                live.get('/data/wow/ruleset/1/auctions/113')
            self.assertEqual(slept.call_count, 1)
            self.assertEqual(slept.call_args.args[0], 0.1)
            live.request_interval = 0
            with patch('blizzard_collector.http_json', return_value=({'auctions': []}, None)), \
                    patch('blizzard_collector.time.sleep') as slept:
                live.get(INDEX)
            self.assertEqual(slept.call_count, 0)
        finally:
            _restore_env(saved)

    def test_persistent_disconnect_skips_one_house_and_keeps_the_other(self):
        from blizzard_collector import ForeverAuctions
        saved = _swap_env(BNET_CLIENT_ID=FAKE_ID, BNET_CLIENT_SECRET=FAKE_SECRET)
        try:
            api = ForeverAuctions(self.cfg)
            api.request_interval = 0
            api.token = 'fixture-token'
            api.expires = time.time() + 3600
            calls = []

            def fake(req, timeout=60):
                url = req.full_url
                calls.append(url)
                if '/auctions/116' in url and '/commodities' not in url:
                    raise RemoteDisconnected('Remote end closed connection without response')
                if '/auctions/index?' in url:
                    return _JsonResponse({'auctions': [
                        {'id': 116, 'name': '검은바다'}, {'id': 113, 'name': '호드'}]})
                if '/auctions/113/commodities' in url:
                    return _JsonResponse(PRICED_COMMODITIES)
                if '/auctions/113?' in url or '/auctions/113' in url:
                    return _JsonResponse(PRICED_AUCTIONS)
                raise AssertionError(url.split('?', 1)[0])

            buf = io.StringIO()
            with patch('common.urlopen', side_effect=fake), patch('common.time.sleep') as sleep:
                with redirect_stdout(buf):
                    with self.assertRaises(AppError) as caught:
                        collect_once(self.db, api, self.cfg, now='2026-11-14T00:00:00+00:00')
            text = buf.getvalue()
            message = str(caught.exception)
            context = str(caught.exception.__context__ or '')
            self.assertIn('116', message)
            self.assertNotIn('113', message)
            self.assertIn('보존했습니다', message)
            self.assertIn('경매장 116: 오류 AppError', text)
            self.assertIn('경매장 113', text)
            self.assertNotIn('Remote end closed', text)
            self.assertNotIn('Remote end closed', message)
            for secret in (FAKE_ID, FAKE_SECRET, 'fixture-token', 'Authorization', 'Bearer'):
                self.assertNotIn(secret, text)
                self.assertNotIn(secret, message)
                self.assertNotIn(secret, context)
            failed = [url for url in calls if '/auctions/116' in url and '/commodities' not in url]
            self.assertEqual(len(failed), HTTP_ATTEMPTS)
            self.assertEqual(sleep.call_count, HTTP_ATTEMPTS - 1)
            self.assertEqual([row[0] for row in self.db.execute(
                'SELECT house_id FROM blizzard_snapshots')], [113])
            priced = self.db.execute(
                "SELECT min_unit_price FROM blizzard_item_prices p "
                "JOIN blizzard_snapshots s ON s.id=p.snapshot_id "
                "WHERE s.house_id=113 AND p.item_id=8").fetchone()
            self.assertEqual(priced[0], 100)
        finally:
            _restore_env(saved)

    def test_run_deadline_skips_remaining_houses_without_failing(self):
        from blizzard_collector import ForeverAuctions
        api = ForeverAuctions(self.cfg, sided_fixture([
            (113, '호드', PRICED_AUCTIONS, PRICED_COMMODITIES),
            (116, '검은바다', PRICED_AUCTIONS, {'auctions': []}),
        ]))
        clock = {'n': 0}

        def mono():
            clock['n'] += 1
            return 0.0 if clock['n'] < 3 else 10_000.0

        buf = io.StringIO()
        with patch('blizzard_collector.time.monotonic', side_effect=mono), redirect_stdout(buf):
            collect_once(self.db, api, self.cfg, now='2026-11-14T00:00:00+00:00')
        text = buf.getvalue()
        self.assertIn('경매장 116: 실행 시간 한도 초과로 이번 주기에서 생략', text)
        self.assertIn('경매장 113', text)
        self.assertNotIn('오류', text)
        self.assertEqual([row[0] for row in self.db.execute(
            'SELECT house_id FROM blizzard_snapshots')], [113])


if __name__ == '__main__':
    unittest.main()
