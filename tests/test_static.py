"""Real-response fixtures and explicitly synthetic edge cases are separate classes."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from common import AppError, connect, load_config
from static_store import enqueue, migrate, seed
from static_parsers import money
from sync_static import initialise, store_response, valid_url


class Base(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cfg=load_config();self.cfg.update(game_flavor='forever',base_dir=self.root)
        self.db=connect(self.cfg,str(self.root/'test.db'),mode='fixture')
        self.cache=self.root/'cache';self.cache.mkdir()

    def tearDown(self):
        self.db.close();self.temp.cleanup()

    def import_real(self,name,kind,entity=None):
        root=Path(__file__).parent/'fixtures'/'real'
        meta=json.loads((root/(name+'.json')).read_text(encoding='utf-8'))
        provider='wowhead' if name.startswith('wowhead') else 'foreverdb'
        if entity and kind in ('item','item_xml','wowhead_sold_by'):seed(self.db,entity,'test',meta['url'])
        enqueue(self.db,meta['url'],provider,kind,entity)
        job=self.db.execute('SELECT * FROM static_queue WHERE url=?',(meta['url'],)).fetchone()
        self.assertTrue(store_response(self.db,job,(root/(name+'.html')).read_bytes(),self.cache,meta['fetched_at']))
        return job


class RealResponseTests(Base):
    def test_browser_table_npc_prices_and_stock(self):
        self.import_real('wowhead_sold_by','wowhead_sold_by',6328)
        rows=self.db.execute('SELECT * FROM vendor_items').fetchall()
        self.assertEqual(len(rows),9)
        row=next(r for r in rows if r['npc_id']==3027)
        self.assertEqual((row['gold'],row['silver'],row['copper'],row['price_copper']),(0,3,80,380))
        self.assertEqual(row['stock_state'],'unlimited_displayed')
        self.assertIsNone(row['purchase_qty']);self.assertIsNone(row['faction'])

    def test_multi_vendor_preserves_price_and_unknown_bundle(self):
        self.import_real('foreverdb_recipeitem','item',6328)
        rows=self.db.execute('SELECT * FROM current_vendor_offers').fetchall()
        self.assertEqual(len(rows),9)
        self.assertEqual({r['price_copper'] for r in rows},{400})
        self.assertTrue(all(r['purchase_qty'] is None and r['currency_costs'] is None and r['npc_id'] is None for r in rows))
        self.assertTrue(all(r['faction'] is None and '(' not in (r['zone'] or '') for r in rows))
        sides={json.loads(r['conditions']).get('npc_side') for r in rows}
        self.assertEqual(sides,{None,'Alliance','Horde'})
        self.assertEqual({r['verification'] for r in rows},{'source_reported_live'})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM prices').fetchone()[0],0)
        mapping=self.db.execute('SELECT * FROM recipe_mappings').fetchone()
        self.assertEqual((mapping['recipe_item_id'],mapping['craft_spell_id'],mapping['output_item_id']),(6328,7753,4592))
        self.assertIsNone(mapping['blizzard_recipe_id']);self.assertIsNone(mapping['output_qty'])

    def test_xml_teaching_is_not_every_spell_effect(self):
        self.import_real('wowhead_xml','item_xml',6328)
        rows=self.db.execute("SELECT object_id FROM static_facts WHERE kind='teaches_spell'").fetchall()
        self.assertEqual([r[0] for r in rows],[7753])
        reagent=self.db.execute("SELECT * FROM static_facts WHERE kind='craft_reagent'").fetchone()
        self.assertEqual(reagent['object_id'],6289);self.assertIsNone(reagent['quantity'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM vendor_items').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM prices').fetchone()[0],0)

    def test_profession_pagination_and_names_do_not_guess_ids(self):
        self.import_real('foreverdb_cooking','profession')
        coverage=self.db.execute("SELECT * FROM static_coverage WHERE relation_kind='recipe_page'").fetchone()
        self.assertEqual((coverage['expected'],coverage['observed']),(132,50))
        self.assertIsNotNone(self.db.execute("SELECT * FROM static_queue WHERE url='https://foreverdb.net/profession/cooking?p=2'").fetchone())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM static_facts WHERE kind='craft_reagent' AND object_id IS NOT NULL").fetchone()[0],0)

    def test_quest_npc_sighting_does_not_verify_classic_objectives(self):
        self.import_real('foreverdb_quest','quest',4161)
        row=self.db.execute("SELECT * FROM static_facts WHERE kind='quest_requires'").fetchone()
        self.assertEqual((row['label'],row['quantity'],row['verification']),('Small Spider Leg',7,'classic_reference'))
        self.assertIsNone(row['object_id'])

    def test_response_dedup_and_failed_refresh_preserves_facts(self):
        job=self.import_real('foreverdb_recipeitem','item',6328)
        self.import_real('foreverdb_recipeitem','item',6328)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM vendor_items').fetchone()[0],9)
        self.assertFalse(store_response(self.db,job,b'<html>blocked</html>',self.cache))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM current_vendor_offers').fetchone()[0],9)
        self.assertEqual(self.db.execute('SELECT state FROM static_queue WHERE url=?',(job['url'],)).fetchone()[0],'parse_error')


class SyntheticSafetyTests(Base):
    def test_money_units_and_non_gold(self):
        self.assertEqual(money('1g 23s 4c')['price_copper'],12304)
        self.assertIsNone(money('4 Merchant Favor'))
        self.assertIsNone(money('3s 100c'))

    def test_separate_flavor_db(self):
        cfg=dict(self.cfg,game_flavor='retail')
        with self.assertRaises(AppError):connect(cfg,str(self.root/'test.db'),mode='fixture')

    def test_non_forever_url_rejected(self):
        for url in ('https://www.wowhead.com/classic/item=1&xml','https://example.org/forever/item=1','http://foreverdb.net/item/1'):
            self.assertFalse(valid_url(url,'wowhead'))

    def test_catalogue_seeds_without_ah_are_retained(self):
        seed(self.db,999,'vendor_list','https://foreverdb.net/vendors')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM prices').fetchone()[0],0)
        self.assertIsNotNone(self.db.execute('SELECT item_id FROM items WHERE item_id=999').fetchone())

    def test_legacy_vendor_migration_is_lossless(self):
        db=sqlite3.connect(':memory:')
        db.executescript(Path('schema.sql').read_text(encoding='utf-8'))
        db.execute('INSERT INTO items(item_id) VALUES (1)')
        db.execute("INSERT INTO vendor_items VALUES (1,100,'csv:manual','limited')")
        migrate(db)
        self.assertEqual(db.execute('SELECT item_id,vendor_price,source,note FROM vendor_items').fetchone(),(1,100,'csv:manual','limited'))
        self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        db.close()


if __name__=='__main__':unittest.main()
