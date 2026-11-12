"""Build a standalone local market browser from real SQLite evidence (stdlib only)."""
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from common import AppError, config_path, get_meta, load_config, main_guard, parser
from forever_report import known_spells
from profession_profile import character_profile, access


def decode(value, fallback=None):
    return json.loads(value) if value is not None else fallback


def vendor_purchase(offer, needed, build):
    """Only independently eligible, current live cash offers may lower craft cost."""
    if (offer['verification'] != 'source_reported_live' or not build
            or offer['build'] != build or offer['eligibility'] != 'eligible'
            or offer['costs'] != [] or offer['price'] is None
            or not offer['qty'] or offer['basis'] != 'per_purchase'):
        return None
    purchases = (needed + offer['qty'] - 1) // offer['qty']
    # A limited-stock sighting does not establish stock available to this character now.
    if offer['stock'] != 'unlimited_displayed':
        return None
    return {'cost': purchases * offer['price'], 'buy_qty': purchases * offer['qty'],
            'method': 'vendor', 'offer_id': offer['id']}


def craft_cost(reagents, items, offers, build):
    total, shopping, missing = 0, [], []
    for reagent in reagents:
        iid, qty = reagent['id'], reagent['qty']
        options = []
        price = items.get(iid, {}).get('price')
        if price is not None:
            options.append({'cost': price * qty, 'buy_qty': qty, 'method': 'auction',
                            'offer_id': None})
        for offer in offers.get(iid, []):
            option = vendor_purchase(offer, qty, build)
            if option is not None:
                options.append(option)
        selected = min(options, key=lambda r: r['cost']) if options else None
        shopping.append(dict(reagent, purchase=selected))
        if selected is None:
            missing.append(iid)
        else:
            total += selected['cost']
    return (total if reagents and not missing else None), shopping, missing


def build_market(db, cfg, now=None):
    now = now or datetime.now(timezone.utc)
    if get_meta(db, 'game_flavor') != 'forever':
        raise AppError('이 화면은 Forever DB 전용입니다. 리테일 DB와 합치지 않습니다.')
    rid = get_meta(db, 'auctionator_local_realm_id')
    if rid is None:
        raise AppError('Auctionator 가격을 먼저 수입하세요.')
    rid = int(rid)
    build = get_meta(db, 'foreverdb_api_build') or get_meta(db, 'foreverdb_build')
    snapshots = [dict(r) for r in db.execute(
        "SELECT id,fetched_at FROM snapshots WHERE source='auctionator' AND realm_id=? ORDER BY fetched_at,id", (rid,))]
    items = {}
    for row in db.execute('SELECT item_id,name,class_name,subclass_name,binding FROM items'):
        items[row['item_id']] = {'id': row['item_id'], 'name': row['name'] or f"item#{row['item_id']}",
            'category': row['class_name'], 'subclass': row['subclass_name'], 'binding': row['binding'], 'price': None,
            'observed': None, 'snapshot': None, 'history': [], 'snapshots': [], 'days': 0,
            'delta': None, 'vendor_count': 0, 'craft_uses': 0}
    # Full import times are deliberately distinct from day-precision scan observations.
    history = defaultdict(list)
    for row in db.execute('''SELECT p.item_id,p.min_price,p.observed_at,s.fetched_at,s.id
            FROM prices p JOIN snapshots s ON s.id=p.snapshot_id
            WHERE s.source='auctionator' AND s.realm_id=? ORDER BY s.fetched_at,s.id''', (rid,)):
        item = items[row['item_id']]
        item['snapshots'].append(row['id'])
        history[row['item_id']].append(row)
        point = {'price': row['min_price'], 'observed': row['observed_at'],
                 'imported': row['fetched_at'], 'snapshot': row['id']}
        if not item['history'] or (point['price'], point['observed']) != (
                item['history'][-1]['price'], item['history'][-1]['observed']):
            item['history'].append(point)
    for iid, records in history.items():
        items[iid]['days'] = len({r['observed_at'] for r in records if r['observed_at']})
    for row in db.execute("SELECT * FROM latest_prices WHERE source='auctionator' AND realm_id=?", (rid,)):
        item = items[row['item_id']]
        item.update(price=row['min_price'], observed=row['observed_at'], snapshot=row['snapshot_id'])
        records = history[row['item_id']]
        # Difference from previous stored file, not a fabricated 24h or median comparison.
        if len(records) > 1 and len(snapshots) > 1 and records[-2]['id'] == snapshots[-2]['id']:
            item['delta'] = (row['min_price'] / records[-2]['min_price'] - 1) * 100
    from market_history import attach_history
    coverage = attach_history(db, rid, items, now)
    coverage.update(latest_prices=sum(i['price'] is not None for i in items.values()),
                    stored_price_rows=sum(len(i['snapshots']) for i in items.values()),
                    static_items=len(items), snapshots=len(snapshots))
    offers, by_item, seen = [], defaultdict(list), set()
    rows = db.execute('''SELECT v.*,c.eligibility FROM current_vendor_offers v
            LEFT JOIN character_offer_checks c ON c.offer_id=v.offer_id AND c.character_key=?
            ORDER BY COALESCE(v.source_fetched_at,v.retrieved_at) DESC,v.offer_id DESC''',
            (cfg.get('character_key', 'default'),))
    for r in rows:
        conditions = decode(r['conditions'], {})
        o = {'id': r['offer_id'], 'item': r['item_id'], 'npc_id': r['npc_id'],
             'npc': r['npc_name'], 'zone': r['zone'], 'x': r['x'], 'y': r['y'],
             'price': r['price_copper'], 'qty': r['purchase_qty'], 'basis': r['price_basis'],
             'stock': r['stock_state'], 'stock_qty': r['stock_quantity'],
             'costs': decode(r['currency_costs']), 'side': conditions.get('npc_side'),
             'reputation': decode(r['reputation_required'], r['reputation_required']) if r['reputation_required'] and r['reputation_required'][0] in '[{' else r['reputation_required'],
             'discount': decode(r['reputation_discount_faction']), 'faction': r['faction'],
             'source': r['source'], 'url': r['source_url'],
             'build': r['game_build'] or r['game_build_override'],
             'date': r['source_fetched_at'] or r['retrieved_at'],
             'verification': r['verification'], 'eligibility': r['eligibility'] or 'unknown'}
        # Collapse duplicate observations only; different NPCs/prices/conditions remain separate.
        key = json.dumps({k: v for k, v in o.items() if k not in ('id', 'date', 'url')}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        offers.append(o)
        by_item[o['item']].append(o)
    for iid, values in by_item.items():
        if iid in items:
            items[iid]['vendor_count'] = len(values)

    known = known_spells(cfg)
    profile = character_profile(cfg)
    crafts, spells = [], set()
    # Each recipe's reagents and outputs come from the SAME document, never a cross-build union.
    facts = defaultdict(list)
    for r in db.execute("SELECT * FROM static_facts WHERE kind IN ('craft_output','craft_reagent')"):
        facts[(r['document_id'], r['subject_id'], r['kind'])].append(r)
    for r in db.execute('''SELECT * FROM current_static_facts
            WHERE kind='craft_recipe' AND provider='foreverdb_api'
            ORDER BY fetched_at DESC,id DESC'''):
        spell = int(r['subject_id'])
        if spell in spells:
            continue
        spells.add(spell)
        if known is not None and spell not in known:
            continue
        details = decode(r['details'], {})
        payload = details.get('payload', {})
        prefix = (r['document_id'], r['subject_id'])
        outputs = facts[prefix + ('craft_output',)]
        ingredients = facts[prefix + ('craft_reagent',)]
        # Distinct output IDs are not interchangeable yields. Keep ambiguous recipes visible.
        outputs = {f['object_id']: f for f in outputs if f['object_id']}
        output = next(iter(outputs.values())) if len(outputs) == 1 else None
        material_map = {}
        for f in ingredients:
            if f['object_id'] and f['quantity']:
                material_map[f['object_id']] = {'id': f['object_id'], 'qty': f['quantity']}
        materials = list(material_map.values())
        raw_materials = payload.get('reagents')
        complete = isinstance(raw_materials, list) and len(materials) == len(raw_materials) and bool(materials)
        cost, shopping, missing = craft_cost(materials, items, by_item, build)
        if not complete or r['verification'] != 'client_catalog':
            cost = None
        iid = output['object_id'] if output else None
        qty = output['quantity'] if output else None
        price = items.get(iid, {}).get('price')
        margin = price * qty * 95 // 100 - cost if price is not None and qty and cost is not None else None
        makes = payload.get('makes') or {}
        skill = payload.get('skill') or {}
        crafts.append({'spell': spell, 'name': r['label'], 'profession': details.get('profession'),
                       'item': iid, 'qty': qty, 'yield_min': makes.get('min'), 'yield_max': makes.get('max'),
                       'price': price, 'cost': cost, 'margin': margin, 'materials': shopping, 'missing': missing,
                       'complete': complete, 'known': known is not None, 'url': r['source_url'],
                       'build': r['game_build'], 'date': r['fetched_at'], 'verification': r['verification'],
                       'skill': skill.get('orange') if skill.get('orangeSource') == 'client' else None,
                       'skill_reference': skill.get('orange'), 'skill_source': skill.get('orangeSource'),
                       # API requiredLevel is not documented as the crafter's learning level.
                       # Keep its value without using it to exclude a character.
                       'required_level': None, 'source_level_reference': payload.get('requiredLevel'),
                       'service': details.get('profession') == 'enchanting' and payload.get('makes', 'unknown') is None,
                       'tools': payload.get('tools'), 'nearby': payload.get('needsNearby')})
        crafts[-1]['access'] = access(crafts[-1], profile)
        for reagent in materials:
            if reagent['id'] in items:
                items[reagent['id']]['craft_uses'] += 1

    data = {'generated': now.isoformat(timespec='seconds'), 'build': build,
            'mode': get_meta(db, 'data_mode'), 'market': {'region': cfg.get('market_region', 'unknown'),
            'ruleset': cfg.get('market_ruleset', cfg.get('auctionator_realm_key', '')),
            'faction': cfg.get('market_faction', 'unknown'), 'realm_id': rid,
            'scope_note': '지역·플레이 진영은 사용자 지정. SavedVariables의 PvP 키 자체에는 진영이 없어 기존 가격 기록을 진영별로 자동 분리하지 않습니다.'},
            'snapshots': snapshots, 'items': list(items.values()), 'vendors': offers, 'crafts': crafts,
            'known_recipes': known is not None, 'character': profile, 'coverage': coverage,
            'metrics': {'weighted_median': None, 'sales_per_day': None, 'recommended_deals': 0}}
    from action_planner import plan_actions
    data['actions'] = plan_actions(data, cfg, now)
    # Browser comparison choices must not be limited by the initial character profile
    # or the top-20 list. Learned-recipe and evidence constraints still apply.
    catalog_cfg = {k: v for k, v in cfg.items() if k not in ('character_professions', 'character_level')}
    data['action_catalog'] = plan_actions(data, catalog_cfg, now)
    return data


def render(data):
    # Prevent item names or upstream strings from terminating the inert JSON script tag.
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    for char, escaped in (('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'), ('\u2028', '\\u2028'), ('\u2029', '\\u2029')):
        payload = payload.replace(char, escaped)
    template = Path(__file__).with_name('market_template.html').read_text(encoding='utf-8')
    template = template.replace('__ACTION_SCRIPT__', Path(__file__).with_name('action_ui.js').read_text(encoding='utf-8'))
    template = template.replace('__PROFESSION_SCRIPT__', Path(__file__).with_name('profession_ui.js').read_text(encoding='utf-8'))
    template = template.replace('__ACTION_STYLE__', Path(__file__).with_name('action_style.css').read_text(encoding='utf-8'))
    return template.replace('__MARKET_DATA__', payload)


def main():
    p = parser(__doc__)
    p.add_argument('--output', default='reports/market.html')
    args = p.parse_args()
    cfg = load_config(args.config)
    path = Path(args.db).resolve() if args.db else config_path(cfg, 'db_path')
    if not path.is_file():
        raise AppError('DB가 없습니다. import_auctionator.py를 먼저 실행하세요.')
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        data = build_market(db, cfg)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(data), encoding='utf-8')
    from action_planner import action_report
    output.with_name(output.stem+'-actions.md').write_text(action_report(data), encoding='utf-8')
    priced = sum(i['price'] is not None for i in data['items'])
    costed = sum(c['cost'] is not None for c in data['crafts'])
    print(f'시장 화면: {output.resolve()}')
    print(f'실제 가격 {priced:,}개 / 정적 품목 {len(data["items"]):,}개 / 원가 계산 가능한 제작 {costed:,}개')
    print('가중 중위가·판매량: 데이터 부족. 가정 차액은 수익 추천이 아닙니다.')


if __name__ == '__main__':
    main_guard(main)
