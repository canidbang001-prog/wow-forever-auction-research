"""Build an explicitly synthetic DB and run every script without API keys."""
import argparse
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import subprocess
import sys

from common import ROOT, connect, load_config, main_guard, snapshot


def prepare(directory):
    directory = directory.resolve()
    directory.mkdir(parents=True,exist_ok=True)
    dbpath = directory / "demo.db"
    if dbpath.exists():
        raise ValueError("샘플 DB가 이미 있습니다. 새 --dir를 지정하세요.")
    now = datetime.now(timezone.utc).replace(microsecond=0)-timedelta(minutes=1)
    modified = format_datetime(now,usegmt=True)
    item_names = {900001:"샘플 광석",900002:"샘플 빈 병",900003:"샘플 비약",900004:"샘플 저점 재료"}
    cfg = {"db_path":"demo.db","realm_name":"데모서버","realm_ids":[4242],
           "auctionator_path":"_retail_/WTF/Account/DEMO/SavedVariables/Auctionator.lua",
           "auctionator_realm_key":"데모서버","auctionator_realm_id":4242}
    config_file = directory/"config.json"
    config_file.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding="utf-8")
    lua = directory/cfg["auctionator_path"]
    lua.parent.mkdir(parents=True,exist_ok=True)
    epoch = datetime(2020,1,1,tzinfo=timezone(timedelta(hours=9)))
    day = (now-epoch).days
    lua.write_text('AUCTIONATOR_PRICE_DATABASE = { ["__dbversion"] = 8, ["데모서버"] = {\n'
                   '["version"]=2, ["900003"]={ ["m"]=1000000, ["h"]={ ["'+str(day)+
                   '"]=1000000 }, ["l"]={}, ["a"]={ ["'+str(day)+'"]=100 } } } }\n',encoding="utf-8")
    fixtures = {}
    def add(path,body,modified=None):
        fixtures[path] = {"body":body,"last_modified":modified}
    add("/data/wow/connected-realm/index",{"connected_realms":[{"href":"https://kr.api.blizzard.com/data/wow/connected-realm/4242"}]})
    add("/data/wow/connected-realm/4242",{"realms":[{"id":123,"name":"데모서버"}]})
    auctions = []
    for item,low,med in [(900001,200000,250000),(900002,100000,120000),(900004,600000,1000000)]:
        for i in range(10):
            auctions.append({"id":len(auctions)+1,"item":{"id":item},"quantity":10,
                             "unit_price":low if i==0 else med})
    add("/data/wow/auctions/commodities",{"auctions":auctions},modified)
    add("/data/wow/connected-realm/4242/auctions",{"auctions":[
        {"id":i+1,"item":{"id":900003},"quantity":10,"buyout":10000000 if i==0 else 12000000}
        for i in range(10)]},modified)
    for item,name in item_names.items():
        add(f"/data/wow/item/{item}",{"id":item,"name":name,"purchase_price":20000 if item==900002 else 0,
            "sell_price":100,"item_class":{"id":7,"name":"직업용품"},
            "item_subclass":{"id":0,"name":"샘플"}})
    add("/data/wow/profession/index",{"professions":[{"id":171,"name":"샘플 연금술"}]})
    add("/data/wow/profession/171",{"skill_tiers":[{"id":1,"name":"샘플 숙련"}]})
    add("/data/wow/profession/171/skill-tier/1",{"categories":[{"recipes":[{"id":900101}]}]})
    add("/data/wow/recipe/900101",{"id":900101,"name":"샘플 비약 제작","crafted_item":{"id":900003},
        "crafted_quantity":{"value":1},"reagents":[{"reagent":{"id":900001},"quantity":2},
        {"reagent":{"id":900002},"quantity":1}]})
    fixture_file = directory/"api.json"
    fixture_file.write_text(json.dumps(fixtures,ensure_ascii=False),encoding="utf-8")
    vendor_file = directory/"vendors.csv"
    vendor_file.write_text("item_id,vendor_price,source,note\n900002,20000,합성 샘플,NPC 샘플 상인; 재고 확인 필요\n",encoding="utf-8")
    db = connect(load_config(config_file),mode="fixture")
    db.execute("INSERT INTO realms VALUES (?,?)",(4242,'["데모서버"]'))
    # 30 days of synthetic observations, every three hours; never written to ah.db.
    with db:
        for i in range(240):
            stamp = now-timedelta(hours=(240-i)*3)
            observed = stamp.isoformat(timespec="seconds")
            quantity = 500-(i%8)*25
            rows = [(900001,200000,250000,quantity,10,observed),
                    (900002,100000,120000,quantity,10,observed),
                    (900004,1000000,1200000,quantity,10,observed)]
            snapshot(db,"api",0,observed,format_datetime(stamp,usegmt=True),rows,30)
            snapshot(db,"api",4242,observed,format_datetime(stamp,usegmt=True),
                     [(900003,1000000,1200000,quantity,10,observed)],10)
    db.close()
    return directory,config_file,fixture_file,vendor_file


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir",default="demo")
    args = p.parse_args()
    directory,config,fixture,vendors = prepare(Path(args.dir))
    base = ["--config",str(config),"--db",str(directory/"demo.db")]
    commands = [
        ("collect_api.py",[*base,"--fixture",str(fixture)]),
        ("collect_api.py",[*base,"--fixture",str(fixture)]),
        ("sync_recipes.py",[*base,"--fixture",str(fixture)]),
        ("import_auctionator.py",[*base,"--sample"]),
        ("import_auctionator.py",[*base,"--sample"]),
        ("import_vendors.py",[*base,str(vendors)]),
        ("analyze.py",[*base,"--output",str(directory/"report.md")])]
    for script,arguments in commands:
        print(f"\n>>> {script} [합성 샘플 검증]",flush=True)
        subprocess.run([sys.executable,str(ROOT/script),*arguments],check=True)


if __name__ == "__main__":
    main_guard(main)
