"""Conservative, evidence-linked Korean auction opportunity report."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import re
import statistics

from common import (AppError, config_path, connect, get_meta, load_config, main_guard,
                    parser, parse_time, run, utcnow)


def gold(copper):
    sign = "-" if copper < 0 else ""
    whole, fraction = divmod(abs(int(copper)), 10000)
    return f"{sign}{whole:,}.{fraction:04d}g"


def net_profit(cost, sale, qty=1):
    return sale * qty * 95 // 100 - cost


def ids_text(rows):
    return ",".join("#"+str(r["snapshot_id"]) for r in rows)


def realm_id(db, name):
    norm = lambda s: re.sub(r"\s+", "", s).casefold()
    if not name:
        return None
    matches = [r[0] for r in db.execute("SELECT realm_id,names FROM realms WHERE realm_id>0")
               if norm(name) in [norm(n) for n in json.loads(r[1])]]
    if len(matches) != 1:
        return None
    return matches[0]


def stats(rows, current, now, options):
    """Never combine regions/sources or treat cached minimums as medians."""
    recent = [r for r in rows if now - timedelta(days=7) <= r["time"] <= now]
    if not recent or current["time"] > now or (now-current["time"]).total_seconds() > options["max_age_hours"]*3600:
        return None, "현재 가격이 없거나 오래됨"
    span = (recent[-1]["time"]-recent[0]["time"]).total_seconds()/3600
    if len(recent) < options["min_snapshots"] or span < options["min_span_hours"]:
        return None, "최근 스냅샷 수/관측 기간 부족"
    decreases, hours, intervals = 0, 0, 0
    for before, after in zip(recent, recent[1:]):
        gap = (after["time"]-before["time"]).total_seconds()/3600
        if 0 < gap <= options["max_gap_hours"] and before["quantity"] is not None and after["quantity"] is not None:
            decreases += max(0, before["quantity"]-after["quantity"])
            hours += gap
            intervals += 1
    if intervals < options["min_snapshots"]-1 or hours < options["min_span_hours"]:
        return None, "수량 변화 관측 구간 부족"
    medians = [r["median_price"] for r in recent]
    median = statistics.median_low(medians)
    cv = statistics.pstdev(medians)/statistics.mean(medians)
    daily = decreases*24/hours
    risk = []
    if current["quantity"] < options["low_quantity"] or current["n_auctions"] < options["low_auctions"]:
        risk.append("주의: 매물 적음")
    if cv >= options["volatility_cv"]:
        risk.append("주의: 가격 변동 큼")
    if daily < 1:
        risk.append("주의: 회전 느림")
    return {"median": median, "sale": min(median, current["min_price"]), "daily": daily,
            "cv": cv, "risk": risk, "recent": recent, "hours": hours, "intervals": intervals}, None


class Market:
    def __init__(self, db, rid, now, options):
        self.db, self.rid, self.now, self.options = db, rid, now, options
        self.items = {r["item_id"]: dict(r) for r in db.execute("SELECT * FROM items")}
        self.history, self.current, self.cache = defaultdict(list), {}, {}
        for row in db.execute("""SELECT p.*,s.source,s.realm_id,s.fetched_at FROM prices p
            JOIN snapshots s ON p.snapshot_id=s.id WHERE s.source='api' AND s.realm_id IN (0,?)
            AND p.median_price IS NOT NULL AND COALESCE(p.observed_at,s.fetched_at)>=?
            ORDER BY COALESCE(p.observed_at,s.fetched_at),s.id""",
            (rid, (now-timedelta(days=30)).isoformat(timespec="seconds"))):
            r = dict(row)
            r["time"] = parse_time(r["observed_at"] or r["fetched_at"])
            if r["time"] <= now:
                self.history[r["realm_id"],r["item_id"]].append(r)
        for row in db.execute("SELECT * FROM latest_prices WHERE source='api' AND realm_id IN (0,?) ORDER BY realm_id DESC", (rid,)):
            r = dict(row)
            r["time"] = parse_time(r["observed_at"] or r["fetched_at"])
            self.current[r["item_id"]] = r  # Region commodities take precedence.
        self.vendors = {}
        for r in db.execute("SELECT * FROM current_vendor_offers WHERE vendor_price>0 ORDER BY vendor_price,offer_id"):
            self.vendors.setdefault(r['item_id'],dict(r))

    def name(self, item):
        return self.items.get(item, {}).get("name") or f"item#{item}"

    def place(self, current):
        return "한국 공통 경매장" if current["realm_id"] == 0 else f"내 서버 경매장({self.rid})"

    def fresh(self, current):
        return current and 0 <= (self.now-current["time"]).total_seconds() <= self.options["max_age_hours"]*3600

    def metrics(self, item):
        if item not in self.cache:
            current = self.current.get(item)
            if not current:
                result = (None, "최신 스냅샷에 매물 없음")
            elif current["median_price"] is None or current["quantity"] is None or current["n_auctions"] is None:
                result = (None, "중위가/수량/경매 수 미제공")
            else:
                result = stats(self.history[current["realm_id"], item], current, self.now, self.options)
            self.cache[item] = result
        return self.cache[item]

    def evidence(self, item, metrics):
        c = self.current[item]
        recent = metrics["recent"]
        return (f"api/realm={c['realm_id']}; 현재 #{c['snapshot_id']} ({c['time'].isoformat()}); "
                f"최근 7일 {len(recent)}개: {ids_text(recent)}; "
                f"관측 {recent[0]['time'].isoformat()} ~ {recent[-1]['time'].isoformat()}; "
                f"중위가 {gold(metrics['median'])}, 현재 최저가 {gold(c['min_price'])}; "
                f"수량 감소 {metrics['intervals']}구간/{metrics['hours']:.1f}시간, "
                f"CV={metrics['cv']:.2f}")

    def ingredient(self, item, qty):
        current, vendor = self.current.get(item), self.vendors.get(item)
        choices = []
        if self.fresh(current) and current["quantity"] is not None and current["quantity"] >= qty:
            choices.append((current["min_price"], self.place(current), f"api 스냅샷 #{current['snapshot_id']}", ""))
        if vendor:
            choices.append((vendor["vendor_price"], "NPC 상점", vendor["source"],
                            "주의: " + (vendor["note"] or "상점/단가/재고 확인 필요")))
        return min(choices, key=lambda v:v[0]) if choices else None


def make_entry(market, item, cost, qty, buy, metrics, warnings=(), extra="", label=None):
    profit = net_profit(cost, metrics["sale"], qty)
    risk = list(dict.fromkeys([*metrics["risk"], *warnings]))
    # Expected sell-through caps daily contribution; warnings reduce rank.
    score = max(0, profit) * min(1, metrics["daily"]/qty) / (1+len(risk))
    return {"name": label or market.name(item), "buy": buy, "cost": cost,
            "sale": metrics["sale"]*qty, "place": market.place(market.current[item]),
            "profit": profit, "margin": profit/cost if cost else 0,
            "daily": metrics["daily"], "risk": "; ".join(risk) or "—",
            "score": score, "evidence": market.evidence(item, metrics) + extra}


def known_recipes(cfg):
    p = config_path(cfg, "known_recipes")
    if not p.exists():
        return None
    result = set()
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#",1)[0].strip()
        if line:
            result.add(int(line))
    return result


def opportunities(market, cfg):
    result, excluded = [[],[],[]], [Counter(),Counter(),Counter()]
    options = cfg["analysis"]
    known = known_recipes(cfg)
    ingredients = defaultdict(list)
    for r in market.db.execute("SELECT * FROM reagents"):
        ingredients[r["recipe_id"]].append(r)
    for recipe in market.db.execute("SELECT * FROM recipes"):
        recipe_id, output = recipe["recipe_id"], recipe["crafted_item_id"]
        if known is not None and recipe_id not in known:
            excluded[0]["배우지 않은 레시피"] += 1
            continue
        if not output or not recipe["crafted_qty"] or not ingredients[recipe_id]:
            excluded[0]["제작 결과/수량/재료 미확인"] += 1
            continue
        if market.items.get(output, {}).get("binding") in ("ON_ACQUIRE", "BIND_ON_ACQUIRE", "QUEST"):
            excluded[0]["거래 불가 제작품"] += 1
            continue
        metrics, reason = market.metrics(output)
        if not metrics:
            excluded[0][reason] += 1
            continue
        cost, buy, evidence, warnings = 0, [], [], []
        for reagent in ingredients[recipe_id]:
            item, qty = reagent["item_id"], reagent["qty"]
            choice = market.ingredient(item, qty)
            if not choice:
                excluded[0]["재료 현재 가격/수량 부족"] += 1
                break
            unit, place, source, warning = choice
            cost += unit*qty
            buy.append(f"{market.name(item)}×{qty} ({place}, 개당 {gold(unit)})")
            evidence.append(f"{market.name(item)}: {source}")
            if warning:
                warnings.append(warning)
        else:
            if known is None:
                warnings.append("레시피 필요")
            warnings.append("주의: 품질/추가 재료/최저가 전량 구매 확인")
            if recipe["note"]:
                warnings.append(recipe["note"])
            action = " + ".join(buy) + f" → {market.name(output)}×{recipe['crafted_qty']} 제작"
            entry = make_entry(market, output, cost, recipe["crafted_qty"], action, metrics,
                               warnings, "; 재료 근거: " + "; ".join(evidence),
                               f"{market.name(output)}×{recipe['crafted_qty']} (레시피 {recipe_id})")
            if entry["profit"] >= options["min_profit_gold"]*10000 and entry["margin"] >= options["min_margin"]:
                result[0].append(entry)
            else:
                excluded[0]["수익 기준 미달"] += 1
    for item, vendor in market.vendors.items():
        metrics, reason = market.metrics(item)
        if not metrics:
            excluded[1][reason] += 1
            continue
        entry = make_entry(market, item, vendor["vendor_price"], 1,
                           f"NPC 상점에서 {market.name(item)}×1 구매 → 그대로 판매", metrics,
                           ["주의: " + (vendor["note"] or "NPC/재고/단가 확인 필요")],
                           f"; 상점 근거: {vendor['source']}; 전문기술 불필요")
        if entry["profit"] >= options["min_profit_gold"]*10000 and entry["margin"] >= options["min_margin"]:
            result[1].append(entry)
        else:
            excluded[1]["수익 기준 미달"] += 1
    for item, current in market.current.items():
        metrics, reason = market.metrics(item)
        if not metrics:
            excluded[2][reason] += 1
            continue
        windows = []
        history = market.history[current["realm_id"],item]
        for days in (7,30):
            baseline = [r for r in history if market.now-timedelta(days=days) <= r["time"] < current["time"]]
            span = (baseline[-1]["time"]-baseline[0]["time"]).total_seconds()/86400 if baseline else 0
            observed_days = len({r["time"].date() for r in baseline})
            if (len(baseline) < options["min_snapshots"] or span < days*options["min_window_coverage"]
                    or observed_days < math.ceil(days*options["min_window_coverage"])):
                continue
            median = statistics.median_low([r["median_price"] for r in baseline])
            discount = 1-current["min_price"]/median
            if discount >= options["dip_discount"]:
                windows.append((days, median, discount, baseline))
        if not windows:
            excluded[2]["7/30일 이력 부족 또는 할인 기준 미달"] += 1
            continue
        detail = "; ".join(f"{d}일 기준 {gold(m)} 대비 {disc:.1%} 할인, {len(rows)}개 [{ids_text(rows)}]"
                           for d,m,disc,rows in windows)
        entry = make_entry(market, item, current["min_price"], 1,
                           f"{market.place(current)}에서 {market.name(item)}×1 → 보유 관찰", metrics,
                           ["주의: 즉시 되팔기 손실, 반등 보장 없음"], "; " + detail)
        entry["score"] = max(w[2] for w in windows)*math.log1p(metrics["daily"])/(1+len(metrics["risk"]))
        result[2].append(entry)
    return result, excluded


def report(db, cfg, now):
    if cfg['game_flavor']=='forever':
        from forever_report import report as forever_report
        return forever_report(db,cfg,now)
    sample = get_meta(db, "data_mode") == "fixture"
    lines = ["# 와우 한국 리테일 경매장 기회 리포트" + (" [합성 샘플: 실제 시세 아님]" if sample else ""),
             f"생성 UTC: {now.isoformat()}; 서버: {cfg['realm_name'] or '미설정'}", "",
             "판매가 = min(최근 7일 스냅샷 중위가들의 하위 중위값, 현재 최저가). 순이익은 수수료 5% 차감 후 금액.",
             "수량 감소는 취소·만료·재등록과 구분할 수 없는 회전 추정치이며 실판매량이 아닙니다.",
             "보증금 손실·재등록 비용·품질/보너스·숙련도·재료 절약/추가 제작은 반영하지 않습니다.",
             "저점 매수는 같은 보수적 판매가로 즉시 되팔면 손실이므로 관찰용이며 매수 추천이 아닙니다.", ""]
    rid = realm_id(db, cfg["realm_name"])
    if rid is None:
        lines.append("데이터 부족: config.json의 realm_name과 한국 서버 목록을 먼저 설정/수집하세요.")
        return "\n".join(lines)+"\n"
    market = Market(db,rid,now,cfg["analysis"])
    results, excluded = opportunities(market,cfg)
    titles = ["제작 차익 (1회 제작 기준)", "상점템 되팔기 (1개 기준, 전문기술 불필요)", "시세 저점 관찰 (1개 기준)"]
    escape = lambda value: str(value).replace("|","/").replace("\n"," ")
    for title, entries, reasons in zip(titles,results,excluded):
        lines += [f"## {title}", ""]
        if not entries:
            lines.append("데이터 부족 또는 조건을 충족하는 후보 없음. 매수 추천 없음.")
        else:
            lines += ["| 아이템명 | 살 것 / 행동 | 비용 | 판매처 / 판매가(수수료 전) | 순이익 | 이익률 | 하루 판매 추정(개) | 주의 |",
                      "|---|---|---:|---|---:|---:|---:|---|"]
            top = sorted(entries,key=lambda e:(e["score"],e["profit"]),reverse=True)[:20]
            for idx, e in enumerate(top,1):
                cells = [f"[{idx}] {e['name']}",e["buy"],gold(e["cost"]),e["place"]+" / "+gold(e["sale"]),
                         gold(e["profit"]),f"{e['margin']:.1%}",f"{e['daily']:.1f}",e["risk"]]
                lines.append("| "+" | ".join(map(escape,cells))+" |")
            lines.append("")
            lines.extend(f"- 근거 [{idx}] {e['name']}: {e['evidence']}" for idx,e in enumerate(top,1))
        if reasons:
            lines += ["", "제외: " + "; ".join(f"{reason} {count}건" for reason,count in reasons.items())]
        lines.append("")
    lines += ["Auctionator의 m은 마지막 최저가이며 가중 중위가·현재 수량을 제공하지 않습니다. "
              "prices에는 보존하지만 수익/회전 분석은 공식 API 스냅샷만 사용합니다."]
    return "\n".join(lines)+"\n"


def main():
    p = parser(__doc__)
    p.add_argument("--output", help="UTF-8 Markdown 파일 저장")
    args = p.parse_args()
    cfg = load_config(args.config)
    db = connect(cfg,args.db,mode=None)
    with run(db,"analyze"):
        text = report(db,cfg,datetime.now(timezone.utc))
        if args.output:
            from pathlib import Path
            target = Path(args.output)
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(text,encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main_guard(main)
