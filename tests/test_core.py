from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analyze import Market, net_profit, report, stats
from collect_api import aggregate
from common import AppError, cache_items, connect, load_config, refresh_vendors, snapshot, weighted_median
from import_auctionator import check_flavor, check_retail, latest_rows
from savedvariables import decode_cbor, price_database
from sync_recipes import recipe_row, sync
from demo import prepare
from common import Blizzard
from collect_api import collect
from analyze import opportunities


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg.update(base_dir=Path(self.temp.name),realm_name="테스트")
        self.db = connect(self.cfg,str(Path(self.temp.name)/"test.db"),mode="fixture")
        self.db.execute("INSERT INTO realms VALUES (1,'[\"테스트\"]')")
        self.db.commit()
        self.now = datetime(2026,11,11,tzinfo=timezone.utc)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_weighted_median_and_tie_are_integer(self):
        self.assertEqual(weighted_median([(1,1),(100,100)]),100)
        self.assertEqual(weighted_median([(1,1),(100,1)]),1)

    def test_buyout_is_per_unit_and_bids_are_excluded(self):
        data = [{"item":{"id":1},"buyout":1001,"quantity":10},
                {"item":{"id":1},"bid":5,"quantity":100},
                {"item":{"id":1},"buyout":100,"quantity":0}]
        self.assertEqual(aggregate(data,False,"time"),[(1,100,100,10,1,"time")])

    def test_snapshot_dedup_and_latest_excludes_disappeared_item(self):
        rows = [(1,100,100,10,2,None),(2,200,200,10,2,None)]
        with self.db:
            self.assertIsNotNone(snapshot(self.db,"api",1,"2026-11-10T00:00:00+00:00","a",rows,4))
            self.assertIsNone(snapshot(self.db,"api",1,"2026-11-10T01:00:00+00:00","a",rows,4))
            snapshot(self.db,"api",1,"2026-11-11T00:00:00+00:00","b",rows[:1],2)
        self.assertEqual([r[0] for r in self.db.execute("SELECT item_id FROM latest_prices")],[1])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],2)

    def test_cache_hard_cap_300(self):
        self.db.executemany("INSERT INTO items(item_id) VALUES (?)",((i,) for i in range(1,306)))
        class API:
            calls=0
            def get(self,path,static=False):
                self.calls+=1
                return {"name":"아이템","purchase_price":0,"sell_price":0},None
        api=API()
        cache_items(self.db,api,999)
        self.assertEqual(api.calls,300)

    def test_manual_vendor_override_survives_refresh(self):
        self.db.execute("INSERT INTO items(item_id,purchase_price,class_id) VALUES (1,100,7)")
        refresh_vendors(self.db)
        self.db.execute("UPDATE vendor_items SET source='csv:수동',vendor_price=123,note='한정 판매'")
        refresh_vendors(self.db)
        row=self.db.execute("SELECT * FROM vendor_items").fetchone()
        self.assertEqual((row["vendor_price"],row["note"]),(123,"한정 판매"))

    def test_demo_cannot_pollute_live_db(self):
        with self.assertRaises(AppError):
            connect(self.cfg,str(Path(self.temp.name)/"test.db"),mode="live")

    def test_cbor_bytes_keys_and_lua_escapes(self):
        self.assertEqual(decode_cbor(b"\xa1\x41m\x19\x01\x90"),{"m":400})
        data=price_database(b'AUCTIONATOR_PRICE_DATABASE = {["__dbversion"]=8,["R"]="\\161Gversion\\002"}')
        self.assertEqual(data["R"]["version"],2)

    def test_lua_code_is_never_executed(self):
        with self.assertRaises(AppError):
            price_database(b'AUCTIONATOR_PRICE_DATABASE = os.execute("bad")')
        with self.assertRaises(AppError):
            decode_cbor(b"\xa1\x41m")

    def test_auctionator_only_latest_min_not_daily_max_volume(self):
        data={"version":2,"42":{"m":400,"h":{"2500":700,"2501":400},"a":{"2501":999}},
              "42:7":{"m":900,"h":{"2501":900}}}
        rows,skipped=latest_rows(data,9)
        self.assertEqual(rows[0][:5],(42,400,None,None,None))
        self.assertEqual(skipped,1)

    def test_nonretail_rejected(self):
        folder=Path(self.temp.name)/"_forever_"
        folder.mkdir()
        (folder/".flavor.info").write_text("Product Flavor!STRING:0\nwowf\n")
        with self.assertRaises(AppError):
            check_retail(folder/"WTF"/"Auctionator.lua")

    def test_forever_path_error_names_forever_folder(self):
        folder=Path(self.temp.name)/"copied"
        folder.mkdir()
        with self.assertRaises(AppError) as caught:
            check_flavor(folder/"Auctionator.lua","forever")
        self.assertIn("_forever_",str(caught.exception))
        self.assertNotIn("_retail_",str(caught.exception))

    def test_conservative_dip_resale_is_loss(self):
        self.assertEqual(net_profit(600000,min(1200000,600000)),-30000)
        self.assertEqual(net_profit(420000,1000000),530000)

    def observations(self):
        return [{"snapshot_id":i,"time":self.now-timedelta(hours=(8-i)*3),
                 "median_price":100,"quantity":100-i*5,"n_auctions":10,"min_price":90} for i in range(9)]

    def test_turnover_uses_elapsed_time(self):
        rows=self.observations()
        result,error=stats(rows,rows[-1],self.now,self.cfg["analysis"])
        self.assertIsNone(error)
        self.assertEqual(result["daily"],40)
        self.assertEqual(result["sale"],90)

    def test_sparse_and_stale_data_do_not_recommend(self):
        rows=self.observations()
        self.assertIsNone(stats(rows[:2],rows[1],self.now,self.cfg["analysis"])[0])
        self.assertIsNone(stats(rows,rows[-1],self.now+timedelta(days=1),self.cfg["analysis"])[0])

    def test_missing_intervals_do_not_inflate_sales(self):
        rows=self.observations()
        for i,row in enumerate(rows):
            row["time"]=self.now-timedelta(hours=(8-i)*12)
        self.assertIsNone(stats(rows,rows[-1],self.now,self.cfg["analysis"])[0])

    def test_recipe_unknown_output_and_quantity_excluded(self):
        row,_=recipe_row({"id":1,"name":"R","alliance_crafted_item":{"id":2},"reagents":[]},"P","T")
        self.assertIsNone(row[4])
        self.assertIsNone(row[5])

    def test_failed_recipe_fetch_preserves_previous_catalog(self):
        self.db.execute("INSERT INTO recipes(recipe_id,name,profession,skill_tier) VALUES (1,'old','P','T')")
        self.db.commit()
        class API:
            def get(self,path,static=False):
                if path.endswith("index"):
                    return {"professions":[{"id":1,"name":"P"}]},None
                raise AppError("failure")
        with self.assertRaises(AppError):
            sync(self.db,API())
        self.assertEqual(self.db.execute("SELECT name FROM recipes").fetchone()[0],"old")

    def test_auctionator_does_not_create_fake_liquidity(self):
        observed=self.now.isoformat()
        with self.db:
            snapshot(self.db,"auctionator",1,observed,"x",[(42,100,None,None,None,observed)])
        market=Market(self.db,1,self.now,self.cfg["analysis"])
        self.assertEqual(market.current,{})
        self.assertIn("데이터 부족",report(self.db,self.cfg,self.now))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        with redirect_stdout(io.StringIO()):
            directory,config,fixture,_=prepare(Path(self.temp.name))
            self.cfg=load_config(config)
            self.db=connect(self.cfg,mode="fixture")
            api=Blizzard(self.cfg,fixture)
            collect(self.db,api,0)
            collect(self.db,api,4242)
            sync(self.db,api)
            cache_items(self.db,api,300)
        self.now=datetime.now(timezone.utc)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_full_report_cost_and_fee_and_evidence(self):
        groups,_=opportunities(Market(self.db,4242,self.now,self.cfg["analysis"]),self.cfg)
        self.assertEqual(groups[0][0]["cost"],420000)
        self.assertEqual(groups[0][0]["profit"],530000)
        self.assertEqual(groups[1][0]["profit"],75000)
        self.assertEqual(groups[2][0]["profit"],-30000)
        self.assertIn("7일 기준",groups[2][0]["evidence"])
        self.assertIn("30일 기준",groups[2][0]["evidence"])
        self.assertIn("#482",groups[0][0]["evidence"])
        self.assertIn("레시피 필요",groups[0][0]["risk"])

    def test_empty_known_recipes_means_none_known(self):
        (Path(self.temp.name)/"known_recipes.txt").write_text("")
        groups,excluded=opportunities(Market(self.db,4242,self.now,self.cfg["analysis"]),self.cfg)
        self.assertEqual(groups[0],[])
        self.assertEqual(excluded[0]["배우지 않은 레시피"],1)

    def test_known_recipe_filters_and_removes_warning(self):
        (Path(self.temp.name)/"known_recipes.txt").write_text("900101 # learned\n")
        groups,_=opportunities(Market(self.db,4242,self.now,self.cfg["analysis"]),self.cfg)
        self.assertEqual(len(groups[0]),1)
        self.assertNotIn("레시피 필요",groups[0][0]["risk"])

    def test_sparse_old_history_does_not_claim_30_day_baseline(self):
        cutoff=(self.now-timedelta(days=10)).isoformat()
        with self.db:
            self.db.execute("DELETE FROM snapshots WHERE fetched_at<? AND id NOT IN (1,2,3,4)",(cutoff,))
        groups,_=opportunities(Market(self.db,4242,self.now,self.cfg["analysis"]),self.cfg)
        self.assertIn("7일 기준",groups[2][0]["evidence"])
        self.assertNotIn("30일 기준",groups[2][0]["evidence"])

    def test_disappeared_output_is_not_recommended(self):
        with self.db:
            snapshot(self.db,"api",4242,self.now.isoformat(),"empty",[],0)
        groups,_=opportunities(Market(self.db,4242,self.now,self.cfg["analysis"]),self.cfg)
        self.assertEqual(groups[0],[])


if __name__=="__main__":
    unittest.main()
