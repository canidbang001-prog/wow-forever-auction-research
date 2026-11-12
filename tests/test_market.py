"""Synthetic arithmetic/scope checks; real response parsing lives in test_forever_api."""
from datetime import datetime, timezone
import json
import unittest

from common import set_meta
from market_dashboard import build_market, craft_cost, render, vendor_purchase
from tests.test_static import Base
from static_store import fact


class ArithmeticTests(unittest.TestCase):
    def offer(self, **overrides):
        return dict(id=1, verification='source_reported_live', build='live-build',
                    eligibility='eligible', costs=[], price=100, qty=5,
                    basis='per_purchase', stock='unlimited_displayed', **overrides)

    def test_whole_bundles_and_unpriced_materials(self):
        offer = self.offer()
        result = vendor_purchase(offer, 6, 'live-build')
        self.assertEqual((result['cost'], result['buy_qty']), (200, 10))
        cost, shopping, missing = craft_cost([{'id': 1, 'qty': 6}], {1: {'price': 40}}, {1: [offer]}, 'live-build')
        self.assertEqual(cost, 200)
        self.assertEqual(shopping[0]['purchase']['method'], 'vendor')
        self.assertEqual(missing, [])
        cost, _, missing = craft_cost([{'id': 2, 'qty': 2}], {}, {}, 'live-build')
        self.assertIsNone(cost)
        self.assertEqual(missing, [2])

    def test_unknown_currency_access_old_build_and_limited_stock_not_cheap(self):
        for change in ({'costs': None}, {'costs': [{'count': 30, 'name': 'Favor'}], 'price': 0},
                       {'build': 'old'}, {'eligibility': 'unknown'}, {'stock': 'limited'},
                       {'verification': 'classic_reference'}, {'qty': None}):
            with self.subTest(change=change):
                offer = self.offer(); offer.update(change)
                self.assertIsNone(vendor_purchase(offer, 1, 'live-build'))

    def test_inert_json_cannot_break_into_html(self):
        html = render({'items': [{'name': '</script><script>alert(1)</script>&'}]})
        payload = html.split('<script id="market-data" type="application/json">')[1].split('</script>')[0]
        self.assertNotIn('<', payload)
        self.assertEqual(json.loads(payload)['items'][0]['name'], '</script><script>alert(1)</script>&')


class MarketScopeTests(Base):
    def setup_market(self):
        self.cfg['auctionator_realm_key'] = 'PvP'
        set_meta(self.db, 'auctionator_local_realm_id', -1)
        set_meta(self.db, 'foreverdb_api_build', 'new')
        self.db.execute("INSERT INTO realms VALUES (-1,'[\"PvP\"]'),(-2,'[\"Other\"]')")
        for iid in (1, 2, 3):
            self.db.execute('INSERT INTO items(item_id,name) VALUES (?,?)', (iid, f'Item {iid}'))
        for sid, rid in ((1, -1), (2, -1), (3, -2)):
            self.db.execute("INSERT INTO snapshots VALUES (?,'auctionator',?,?,?,NULL)",
                            (sid, rid, f'2026-11-11T1{sid}:00:00+00:00', str(sid)))
            self.db.execute('INSERT INTO prices VALUES (?,1,?,NULL,NULL,NULL,?)',
                            (sid, 100 if rid == -1 else 9999, '2026-11-10T15:00:00+00:00'))

    def test_reimports_not_extra_history_and_other_realms_excluded(self):
        self.setup_market()
        data = build_market(self.db, self.cfg)
        item = next(x for x in data['items'] if x['id'] == 1)
        self.assertEqual((item['price'], item['days'], len(item['history'])), (100, 1, 1))
        self.assertEqual(item['snapshots'], [1, 2])
        self.assertEqual(len(data['snapshots']), 2)
        self.assertIsNone(next(x for x in data['items'] if x['id'] == 2)['price'])
        self.assertIsNone(data['metrics']['weighted_median'])
        self.assertIsNone(data['metrics']['sales_per_day'])
        self.assertEqual(data['market']['faction'], 'unknown')

    def test_classic_skill_and_undocumented_level_never_become_character_requirements(self):
        self.setup_market()
        self.db.execute('''INSERT INTO static_documents(id,url,provider,game_build,fetched_at,
            sha256,cache_path,http_status,verification) VALUES (1,?,?,?,?,?,?,?,?)''',
            ('https://foreverdb.net/api/v1/professions/tailoring', 'foreverdb_api', 'new',
             '2026-11-11T12:00:00+00:00', '1', 'test', 200, 'client_catalog'))
        fact(self.db, 1, 'craft_recipe', 'spell', 123, label='Classic threshold',
             verification='client_catalog', profession='tailoring', payload={
                 'skill': {'orange': 205, 'orangeSource': 'classic'}, 'requiredLevel': 40,
                 'reagents': [{'itemId': 1, 'count': 1}], 'makes': {'itemId': 2, 'count': 1}})
        craft = build_market(self.db, self.cfg)['crafts'][0]
        self.assertIsNone(craft['skill'])
        self.assertEqual(craft['skill_reference'], 205)
        self.assertEqual(craft['skill_source'], 'classic')
        self.assertIsNone(craft['required_level'])
        self.assertEqual(craft['source_level_reference'], 40)

    def test_same_document_reagents_missing_price_and_known_recipe_filter(self):
        self.setup_market()
        for doc, build, day in ((1, 'old', '10'), (2, 'new', '11')):
            self.db.execute('''INSERT INTO static_documents(id,url,provider,game_build,fetched_at,
                sha256,cache_path,http_status,verification) VALUES (?,?,?,?,?,?,?,?,?)''',
                (doc, f'https://foreverdb.net/api/v1/professions/p{doc}', 'foreverdb_api', build,
                 f'2026-11-{day}T12:00:00+00:00', str(doc), 'test', 200, 'client_catalog'))
            fact(self.db, doc, 'craft_recipe', 'spell', 123, label='Test craft', verification='client_catalog',
                 profession='cooking', payload={'reagents': [{'itemId': doc, 'count': 1}], 'makes': {'itemId': 1, 'count': 1}})
            fact(self.db, doc, 'craft_output', 'spell', 123, 'item', 1, quantity=1, verification='client_catalog')
            fact(self.db, doc, 'craft_reagent', 'spell', 123, 'item', doc, quantity=1, verification='client_catalog')
        data = build_market(self.db, self.cfg)
        self.assertEqual(len(data['crafts']), 1)
        craft = data['crafts'][0]
        self.assertEqual(craft['build'], 'new')
        self.assertEqual(craft['missing'], [2])
        self.assertIsNone(craft['cost'])
        self.assertIsNone(craft['margin'])
        (self.root / self.cfg['known_recipes']).write_text('spell:456\n', encoding='utf-8')
        self.assertEqual(build_market(self.db, self.cfg)['crafts'], [])


if __name__ == '__main__':
    unittest.main()
