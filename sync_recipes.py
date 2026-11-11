"""Synchronize profession recipes at most once per seven days."""
from collections import Counter
from datetime import datetime, timezone, timedelta
from common import (AppError, Blizzard, cache_items, connect, ensure_items, get_meta,
                    load_config, main_guard, parser, parse_time, run, set_meta, utcnow)


def recipe_row(detail, profession, tier):
    crafted = detail.get("crafted_item", {})
    note = []
    if not crafted:
        # Faction-specific outputs cannot be represented by one crafted_item_id.
        note.append("단일 제작 결과물 없음/진영별 결과물: 분석 제외")
    qty_data = detail.get("crafted_quantity", {})
    qty = qty_data.get("value", qty_data.get("minimum"))
    if qty_data.get("maximum") != qty_data.get("minimum") and "minimum" in qty_data:
        note.append("제작 수량 범위의 최솟값 사용")
    if not isinstance(qty, int) or qty <= 0:
        qty = None
        note.append("제작 수량 미확인: 분석 제외")
    ingredients = Counter()
    for entry in detail.get("reagents", []):
        item_id, amount = entry["reagent"]["id"], entry["quantity"]
        if not isinstance(amount, int) or amount <= 0:
            raise AppError("레시피 재료 수량 오류")
        ingredients[item_id] += amount
    return (detail["id"], detail["name"], profession, tier, crafted.get("id"), qty,
            "; ".join(note)), ingredients


def sync(db, api):
    index, _ = api.get("/data/wow/profession/index", static=True)
    collected = {}
    for profession in index["professions"]:
        prof, _ = api.get(f"/data/wow/profession/{profession['id']}", static=True)
        for tier in prof.get("skill_tiers", []):
            detail, _ = api.get(f"/data/wow/profession/{profession['id']}/skill-tier/{tier['id']}", static=True)
            for category in detail.get("categories", []):
                for ref in category.get("recipes", []):
                    if ref["id"] not in collected:
                        recipe, _ = api.get(f"/data/wow/recipe/{ref['id']}", static=True)
                        collected[ref["id"]] = recipe_row(recipe, profession["name"], tier["name"])
        print(f"전문기술 {profession['name']}: 누적 {len(collected)}개")
    if not collected:
        raise AppError("레시피가 0개여서 기존 목록을 보존했습니다.")
    # All fetches must succeed before replacing the previous complete catalogue.
    with db:
        db.execute("DELETE FROM reagents")
        db.execute("DELETE FROM recipes")
        for row, ingredients in collected.values():
            ensure_items(db, [*ingredients, *([row[4]] if row[4] else [])])
            db.execute("INSERT INTO recipes VALUES (?,?,?,?,?,?,?)", row)
            db.executemany("INSERT INTO reagents VALUES (?,?,?)",
                           ((row[0], item_id, qty) for item_id, qty in ingredients.items()))
        set_meta(db, "recipes_synced_at", utcnow())
    print(f"레시피 {len(collected)}개 저장")


def main():
    p = parser(__doc__, fixture=True)
    p.add_argument("--force", action="store_true", help="7일 주기 무시")
    args = p.parse_args()
    cfg = load_config(args.config)
    if args.fixture and not args.db:
        raise AppError("샘플 실행에는 별도 --db가 필요합니다.")
    db = connect(cfg, args.db, "fixture" if args.fixture else "live")
    with run(db, "sync_recipes"):
        previous = get_meta(db, "recipes_synced_at")
        if previous and not args.force and datetime.now(timezone.utc) - parse_time(previous) < timedelta(days=7):
            print("레시피: 최근 7일 이내 동기화 완료, 건너뜁니다.")
            return
        api = Blizzard(cfg, args.fixture)
        sync(db, api)
        cache_items(db, api, cfg["item_cache_limit"])


if __name__ == "__main__":
    main_guard(main)
