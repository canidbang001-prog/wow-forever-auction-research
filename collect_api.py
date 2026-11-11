"""Collect regional commodities and connected-realm auctions once."""
from collections import defaultdict
import json
import re

from common import (AppError, Blizzard, cache_items, connect, header_time, load_config,
                    main_guard, parser, positive_int, run, snapshot, utcnow, weighted_median)


def aggregate(auctions, commodities, observed):
    groups, counts = defaultdict(lambda: defaultdict(int)), defaultdict(int)
    for auction in auctions:
        item_id = auction.get("item", {}).get("id")
        qty = auction.get("quantity")
        amount = auction.get("unit_price" if commodities else "buyout")
        if not all(positive_int(x) for x in (item_id, qty, amount)):
            continue
        price = amount if commodities else amount // qty
        if price <= 0:
            continue
        groups[item_id][price] += qty
        counts[item_id] += 1
    return [(item_id, min(levels), weighted_median(list(levels.items())),
             sum(levels.values()), counts[item_id], observed)
            for item_id, levels in groups.items()]


def sync_realms(db, api, realm_ids):
    index, _ = api.get("/data/wow/connected-realm/index")
    ids = []
    for entry in index["connected_realms"]:
        match = re.search(r"/connected-realm/(\d+)(?:\?|$)", entry["href"])
        if not match:
            raise AppError("connected-realm index 형식을 확인하세요.")
        realm_id = int(match[1])
        if realm_ids and realm_id not in realm_ids:
            continue
        info, _ = api.get(f"/data/wow/connected-realm/{realm_id}")
        names = json.dumps([r["name"] for r in info["realms"]], ensure_ascii=False)
        db.execute("INSERT INTO realms VALUES (?,?) ON CONFLICT(realm_id) DO UPDATE SET names=excluded.names",
                   (realm_id, names))
        ids.append(realm_id)
    if realm_ids and set(ids) != set(realm_ids):
        raise AppError("realm_ids 중 한국 connected-realm index에 없는 ID가 있습니다.")
    db.commit()
    return ids


def collect(db, api, realm_id):
    path = ("/data/wow/auctions/commodities" if realm_id == 0 else
            f"/data/wow/connected-realm/{realm_id}/auctions")
    previous = db.execute("SELECT last_modified FROM snapshots WHERE source='api' AND realm_id=? "
                          "ORDER BY fetched_at DESC,id DESC LIMIT 1", (realm_id,)).fetchone()
    body, modified = api.get(path, modified=previous[0] if previous else None)
    if body is None:
        print(f"서버 {realm_id}: 동일 스냅샷 (HTTP 304)")
        return
    observed = header_time(modified)
    if not isinstance(body.get("auctions"), list):
        raise AppError("경매 API auctions 배열 누락")
    rows = aggregate(body["auctions"], realm_id == 0, observed)
    with db:
        sid = snapshot(db, "api", realm_id, utcnow(), modified, rows, len(body["auctions"]))
    print(f"서버 {realm_id}: " + (f"스냅샷 #{sid}, 아이템 {len(rows)}개" if sid else "동일 스냅샷 제외"))


def main():
    args = parser(__doc__, fixture=True).parse_args()
    cfg = load_config(args.config)
    if args.fixture and not args.db:
        raise AppError("샘플 실행에는 별도 --db가 필요합니다.")
    db = connect(cfg, args.db, "fixture" if args.fixture else "live")
    with run(db, "collect_api"):
        api = Blizzard(cfg, args.fixture)
        ids = sync_realms(db, api, cfg["realm_ids"])
        failures = []
        for realm_id in [0, *ids]:
            try:
                collect(db, api, realm_id)
            except AppError as exc:
                failures.append(realm_id)
                print(f"서버 {realm_id}: {exc}")
        cache_items(db, api, cfg["item_cache_limit"])
        if failures:
            raise AppError(f"수집 실패 서버 {failures}; 성공 스냅샷은 보존했습니다.")


if __name__ == "__main__":
    main_guard(main)
