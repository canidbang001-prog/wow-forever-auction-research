"""Synthetic regressions for source conflicts, stale/missing quotes and trial sizing."""
from contextlib import redirect_stdout
import io

from action_planner import plan_actions, position_plan, market_settings
from blizzard_collector import store_snapshot, upsert_houses, forever_settings, collect_house
from common import AppError, ensure_items, set_meta
from market_dashboard import build_market, craft_cost
from market_quotes import purchase_quote
from tests.test_actions import NOW, item
from tests.test_blizzard import priced
from tests.test_round2 import bundle, vendor, listing
from tests.test_static import Base


class MarketQuoteTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg.update(market_region='kr', market_faction='horde',
                        blizzard_forever={'house_id': 113, 'market_confirmed': True})
        set_meta(self.db, 'auctionator_local_realm_id', -1)
        set_meta(self.db, 'foreverdb_api_build', 'new')
        self.db.execute("INSERT INTO realms VALUES (-1, '[\"PvP\"]')")
        ensure_items(self.db, [1])
        self.db.execute("INSERT INTO snapshots VALUES (1,'auctionator',-1,'2026-11-12T00:00:00+00:00','raw',NULL)")
        self.db.execute("INSERT INTO prices VALUES (1,1,10000,NULL,NULL,NULL,'2026-11-12T00:00:00+00:00')")
        upsert_houses(self.db, 1, [{'id': 113, 'name': '호드', 'side': 'horde'}], NOW.isoformat())

    def snapshot(self, stamp, price=100, qty=10, lm_a=None, lm_c=None):
        listings = [] if price is None else [priced('commodity', 1, 1, price, qty, 'LONG')]
        return store_snapshot(self.db, 113, 1, stamp, listings, 0, len(listings), lm_a, lm_c)

    def data(self):
        return build_market(self.db, self.cfg, NOW)

    def test_confirmed_api_price_replaces_old_quote_before_costs_and_recommendations(self):
        for day in (10, 11, 12):
            self.snapshot(f'2026-11-{day}T00:00:00+00:00')
        data = self.data()
        row = next(i for i in data['items'] if i['id'] == 1)
        self.assertEqual((row['price'], row['median_price'], row['quantity']), (100, 100, 10))
        self.assertEqual(row['auctionator']['price'], 10000)
        self.assertEqual(row['price_source'], 'blizzard')
        cost, _, _ = craft_cost([{'id': 1, 'qty': 2}], {1: row}, {}, 'new')
        self.assertEqual(cost, 200)
        actions = plan_actions(bundle([row], vendors=[vendor()]), listing(
            deposit_copper_per_item=100, sell_through_pct=100), NOW)
        self.assertEqual(actions['vendors'], [])

    def test_unconfirmed_house_never_replaces_auctionator(self):
        self.snapshot('2026-11-12T00:00:00+00:00')
        self.cfg['blizzard_forever']['market_confirmed'] = False
        row = next(i for i in self.data()['items'] if i['id'] == 1)
        self.assertEqual(row['price'], 10000)
        self.assertEqual(row['price_source'], 'auctionator')

    def test_latest_empty_house_does_not_revive_an_old_item(self):
        self.snapshot('2026-11-11T00:00:00+00:00')
        self.snapshot('2026-11-12T00:00:00+00:00', price=None)
        row = next(i for i in self.data()['items'] if i['id'] == 1)
        self.assertIsNone(row['price'])
        self.assertIsNone(row['blizzard']['houses'][0]['min_unit_price'])
        self.assertEqual(row['blizzard']['houses'][0]['quantity'], 0)
        self.assertEqual(row['auctionator']['price'], 10000)

    def test_missing_response_array_does_not_overwrite_latest_market(self):
        self.snapshot('2026-11-11T00:00:00+00:00')
        class BadResponse:
            def get(self, path, modified=None):
                return ({'auctions': []} if path.endswith('/commodities') else {}), 'new'
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertIsNone(collect_house(
                self.db, BadResponse(), 1, {'id': 113, 'side': 'horde'}, NOW.isoformat()))
        text = buf.getvalue()
        self.assertIn('매물 목록 없음', text)
        self.assertNotIn('오류', text)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM blizzard_snapshots').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT min_unit_price FROM blizzard_item_prices').fetchone()[0], 100)
        check = self.db.execute('SELECT stored, note, snapshot_id FROM blizzard_checks').fetchone()
        self.assertEqual((check['stored'], check['note'], check['snapshot_id']), (0, '매물 목록 없음', None))

    def test_old_last_modified_cannot_be_rejuvenated_by_fetch_time(self):
        self.snapshot('2026-11-12T00:00:00+00:00',
                      lm_a='Sun, 01 Nov 2026 00:00:00 GMT', lm_c='Thu, 12 Nov 2026 00:00:00 GMT')
        row = next(i for i in self.data()['items'] if i['id'] == 1)
        self.assertIsNone(row['price'])
        self.assertEqual(row['blizzard']['houses'][0]['liquidity'], '관찰 필요')

    def test_confirmed_market_does_not_require_an_auctionator_import(self):
        self.db.execute("DELETE FROM meta WHERE key='auctionator_local_realm_id'")
        self.snapshot('2026-11-12T00:00:00+00:00')
        row = next(i for i in self.data()['items'] if i['id'] == 1)
        self.assertEqual(row['price'], 100)
        self.assertIsNone(row['auctionator']['price'])

    def test_confirmed_market_without_api_data_has_no_fallback_price(self):
        row = next(i for i in self.data()['items'] if i['id'] == 1)
        self.assertIsNone(row['price'])

    def test_wrong_faction_or_region_is_rejected(self):
        self.snapshot('2026-11-12T00:00:00+00:00')
        self.cfg['market_faction'] = 'alliance'
        with self.assertRaises(AppError):
            self.data()
        self.cfg.update(market_faction='horde', market_region='us')
        with self.assertRaises(AppError):
            self.data()

    def test_confirmation_requires_explicit_house_not_single_collection_id(self):
        with self.assertRaises(AppError):
            forever_settings({'blizzard_forever': {'house_ids': [113], 'market_confirmed': True}})

    def test_purchase_cost_follows_available_depth_and_whole_auction_stacks(self):
        row = dict(price=100, price_source='blizzard', price_levels=[
            dict(kind='commodity', unit_price=100, quantity=1),
            dict(kind='commodity', unit_price=200, quantity=10)])
        self.assertEqual(purchase_quote(row, 6)['cost'], 1100)
        self.assertIsNone(purchase_quote(row, 12))
        row['price_levels'] = [dict(kind='auction', unit_price=100, quantity=5)]
        self.assertEqual(purchase_quote(row, 2), dict(cost=500, buy_qty=5, method='auction', offer_id=None))

    def test_large_capital_does_not_expand_one_trial_or_desync_profit_quantity(self):
        cfg = dict(listing(deposit_copper_per_item=100, sell_through_pct=40), total_budget_copper=10000000)
        action = plan_actions(bundle([item()], vendors=[vendor()]), cfg, NOW)['vendors'][0]
        self.assertEqual((action['qty'], action['suggested_qty'], action['suggested_units']), (1, 1, 1))
        self.assertEqual(action['capital_required'], action['cost'])
        self.assertLessEqual(action['capital_required'], action['trial_budget_copper'])
        self.assertEqual(action['listing']['qty'], action['suggested_qty'])
        self.assertTrue(position_plan(50001, 1, market_settings(cfg))['excluded'])
