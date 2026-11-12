"""Authenticated real responses (developer ID removed) and separately labelled edge cases."""
import json
from pathlib import Path
import unittest
from datetime import datetime, timezone

from tests.test_static import Base
from static_store import coverage, enqueue
from sync_static import store_response, NoRedirect
from common import AppError, set_meta
from forever_report import report, known_spells


class ApiBase(Base):
    def fixture(self,name,kind,entity=None):
        root=Path(__file__).parent/'fixtures'/'real'
        meta=json.loads((root/(name+'.meta.json')).read_text(encoding='utf-8'))
        body=(root/(name+'.body.json')).read_bytes()
        enqueue(self.db,meta['url'],'foreverdb_api',kind,entity)
        job=self.db.execute('SELECT * FROM static_queue WHERE url=?',(meta['url'],)).fetchone()
        return job,body,meta['fetched_at']

    def ingest(self,args):
        return store_response(self.db,args[0],args[1],self.cache,args[2])


class AuthenticatedRealResponseTests(ApiBase):
    def test_vendor_inventory_price_bundle_location_missing_costs(self):
        self.assertTrue(self.ingest(self.fixture('api_vendor','api_vendor',54)))
        rows=self.db.execute('SELECT * FROM current_vendor_offers').fetchall()
        self.assertEqual(len(rows),8)
        r=next(r for r in rows if r['item_id']==2488)
        self.assertEqual((r['npc_id'],r['map_id'],r['x'],r['y']),(54,1429,41.6,65.8))
        self.assertEqual((r['price_copper'],r['purchase_qty'],r['stock_state']),(536,1,'unlimited_displayed'))
        self.assertIsNone(r['currency_costs']);self.assertIsNone(r['reputation_required']);self.assertIsNone(r['faction'])
        self.assertEqual(json.loads(r['reputation_discount_faction'])['id'],72)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM character_offer_checks').fetchone()[0],0)

    def test_profession_ids_quantities_currency_and_reward_not_requirement(self):
        args=self.fixture('api_cooking','api_profession')
        self.assertTrue(self.ingest(args));self.assertTrue(self.ingest(args))
        r=self.db.execute('SELECT * FROM recipe_mappings WHERE craft_spell_id=7753').fetchone()
        self.assertEqual((r['recipe_item_id'],r['output_item_id'],r['output_qty']),(6328,4592,1))
        self.assertIsNone(r['blizzard_recipe_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recipe_mappings WHERE craft_spell_id=7753').fetchone()[0],1)
        hidden=self.db.execute('SELECT * FROM recipe_mappings WHERE recipe_item_id=6891').fetchone()
        self.assertEqual(hidden['craft_spell_id'],8604)
        self.assertEqual(hidden['verification'],'hidden_client_reference')
        reagent=self.db.execute("SELECT object_id,quantity FROM static_facts WHERE kind='craft_reagent' AND subject_id='7753'").fetchone()
        self.assertEqual(tuple(reagent),(6289,1))
        self.assertGreater(self.db.execute("SELECT COUNT(*) FROM vendor_items WHERE currency_costs='[]'").fetchone()[0],0)
        self.assertGreater(self.db.execute("SELECT COUNT(*) FROM static_facts WHERE kind='recipe_quest_source'").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM static_facts WHERE kind='quest_requires'").fetchone()[0],0)
        self.assertIsNotNone(self.db.execute("SELECT * FROM static_queue WHERE url LIKE '%cooking?limit=100&offset=100'").fetchone())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM prices').fetchone()[0],0)

    def test_item_effects_do_not_guess_teaching_spell(self):
        self.assertTrue(self.ingest(self.fixture('api_item','api_item',6328)))
        self.assertEqual(self.db.execute('SELECT sell_price FROM items WHERE item_id=6328').fetchone()[0],100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recipe_mappings').fetchone()[0],0)

    def test_quest_live_sighting_does_not_invent_objectives(self):
        self.assertTrue(self.ingest(self.fixture('api_quest','api_quest',4161)))
        self.assertEqual(self.db.execute("SELECT status FROM static_coverage WHERE relation_kind='quest_requires'").fetchone()[0],'unsupported')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM static_facts WHERE kind='quest_requires'").fetchone()[0],0)


class SyntheticApiSafetyTests(ApiBase):
    def changed(self,name,kind,entity,mutate):
        job,body,date=self.fixture(name,kind,entity);data=json.loads(body);mutate(data)
        return job,json.dumps(data).encode(),date

    def test_truncation_preserves_previous_facts_and_date(self):
        original=self.fixture('api_cooking','api_profession');self.assertTrue(self.ingest(original))
        changed=self.changed('api_cooking','api_profession',None,lambda d:d['data']['recipes'].pop())
        self.assertFalse(self.ingest(changed))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM current_static_facts WHERE kind='craft_recipe'").fetchone()[0],100)
        self.assertEqual(self.db.execute('SELECT fetched_at FROM static_documents WHERE active=1').fetchone()[0],original[2])
        coverage(self.db,original[0]['url'],'api_profession','retry',note='synthetic HTTP 502')
        self.db.commit()
        self.assertTrue(self.ingest(original))
        self.assertIsNone(self.db.execute("SELECT 1 FROM static_coverage WHERE relation_kind='api_profession'").fetchone())

    def test_next_page_cannot_skip_loop_or_send_key_elsewhere(self):
        for nxt in ('https://example.org/api/v1/professions/cooking?limit=100&offset=100',
                    '/api/v1/professions/cooking?limit=100&offset=0',
                    '/api/v1/professions/cooking?limit=100&offset=200'):
            args=self.changed('api_cooking','api_profession',None,lambda d:d['meta'].update(next=nxt))
            self.assertFalse(self.ingest(args))
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://example.org'))

    def test_new_build_schedules_older_documents_without_changing_cache_dates(self):
        self.assertTrue(self.ingest(self.fixture('api_vendor','api_vendor',54)))
        set_meta(self.db,'foreverdb_api_build','1.60.1.70862');self.db.commit()
        job,body,date=self.changed('api_item','api_item',6328,lambda d:d['meta'].update(build='1.60.1.70888'))
        self.assertTrue(store_response(self.db,job,body,self.cache))
        self.assertEqual(self.db.execute("SELECT state FROM static_queue WHERE kind='api_vendor'").fetchone()[0],'pending')
        self.assertEqual(self.db.execute("SELECT fetched_at FROM static_documents WHERE url LIKE '%/vendors/54'").fetchone()[0],self.fixture('api_vendor','api_vendor',54)[2])

    def test_npc_seen_does_not_verify_each_offer(self):
        args=self.changed('api_vendor','api_vendor',54,lambda d:d['data']['sells'][0].update(seenInForever=False,classicData=True))
        self.assertTrue(self.ingest(args))
        self.assertEqual(self.db.execute('SELECT verification FROM vendor_items WHERE item_id=2488').fetchone()[0],'classic_reference')

    def test_beta_catalog_retained_without_overwriting_price_fields(self):
        args=self.changed('api_item','api_item',6328,lambda d:d['data'].update(betaEvent=True))
        self.assertTrue(self.ingest(args))
        self.assertEqual(self.db.execute('SELECT verification FROM static_facts').fetchone()[0],'beta_reference')
        self.assertIsNone(self.db.execute('SELECT sell_price FROM items').fetchone()[0])

    def test_variable_yield_keeps_range_without_fixed_output(self):
        def vary(d): d['data']['recipes'][0]['makes'].update(min=1,max=3,count=2)
        self.assertTrue(self.ingest(self.changed('api_cooking','api_profession',None,vary)))
        r=self.db.execute("SELECT quantity,details FROM static_facts WHERE kind='craft_output' AND subject_id='1229737'").fetchone()
        self.assertIsNone(r['quantity']);self.assertEqual(json.loads(r['details'])['payload']['max'],3)

    def test_report_filters_known_spells_without_fabricating_profit(self):
        self.assertTrue(self.ingest(self.fixture('api_cooking','api_profession')))
        self.db.execute("INSERT INTO realms VALUES (-1,'[\"PvP\"]')")
        set_meta(self.db,'auctionator_local_realm_id',-1)
        self.db.execute("INSERT INTO snapshots VALUES (1,'auctionator',-1,'2026-11-11T15:00:00+00:00','synthetic',NULL)")
        for iid in (4592,6290):
            self.db.execute("INSERT INTO prices VALUES (1,?,1000,NULL,NULL,NULL,'2026-11-11T15:00:00+00:00')",(iid,))
        self.cfg['auctionator_realm_key']='PvP'
        (self.root/self.cfg['known_recipes']).write_text('spell:7753\n',encoding='utf-8')
        text=report(self.db,self.cfg,datetime.now(timezone.utc))
        self.assertIn('| spell:7753 |',text);self.assertNotIn('| spell:7751 |',text)
        self.assertEqual(text.count('수익 추천 0개'),3)
        self.assertIn('하루 판매 추정: 계산 불가',text)
        (self.root/self.cfg['known_recipes']).write_text('7753\n',encoding='utf-8')
        with self.assertRaises(AppError) as exc: known_spells(self.cfg)
        self.assertIn('Blizzard recipe ID',str(exc.exception))


if __name__=='__main__':unittest.main()
