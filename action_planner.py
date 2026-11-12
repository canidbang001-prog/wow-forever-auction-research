"""Conditional one-purchase trials and farming targets; no invented sales/yields."""
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from profession_profile import character_profile, allowed, CLOTH


def sell_scenario(item, now, haircut=20):
    """A stress scenario, explicitly NOT a median or expected clearing price."""
    if not item or not item.get('price') or not item.get('observed'):
        return None
    cutoff = now - timedelta(days=7)
    observed = datetime.fromisoformat(item['observed'])
    if observed < now - timedelta(days=2) or observed > now:
        return None
    history = [p for p in item['history'] if p['observed'] and
               cutoff <= datetime.fromisoformat(p['observed']) <= now]
    daily = [p for p in item.get('daily_history', []) if p['low'] is not None and
             cutoff <= datetime.fromisoformat(p['observed']) <= now]
    reference = min([item['price']] + [p['price'] for p in history] + [p['low'] for p in daily])
    price = max(1, reference - 1)
    stress = price * (100-haircut) // 100
    return {'reference': reference, 'price': price, 'stress_price': stress,
            'haircut_pct': haircut, 'observed': item['observed'],
            'snapshot': item['snapshot'], 'files': len(item['snapshots']),
            'points': len(history), 'days': len({p['observed'] for p in history+daily}),
            'daily_points': len(daily), 'daily_imports': sorted({p['import_id'] for p in daily})}


def trial_math(cost, qty, scenario, target_margin=20):
    gross = scenario['price'] * qty
    net = gross * 95 // 100
    stress_net = scenario['stress_price'] * qty * 95 // 100
    return {'cost': cost, 'qty': qty, 'net': net, 'difference': net-cost,
            'stress_difference': stress_net-cost,
            'buy_ceiling': stress_net*100//(100+target_margin),
            'break_even': (cost*100 + 95*qty-1)//(95*qty),
            'target_margin_pct': target_margin}


def process_comparison(craft, material_id, items, now):
    """Caught/owned inputs have the opportunity cost of selling them directly."""
    target = next((m for m in craft['materials'] if m['id'] == material_id), None)
    output_sale = sell_scenario(items.get(craft['item']), now)
    raw_sale = sell_scenario(items.get(material_id), now)
    if (not target or not raw_sale or not output_sale or craft['cost'] is None
            or not craft['qty'] or not craft['complete'] or
            any(m['purchase'] is None or (m['purchase']['method'] == 'auction'
                and sell_scenario(items.get(m['id']), now) is None) for m in craft['materials'])):
        return None
    raw_net = raw_sale['price']*target['qty']*95//100
    others = sum(m['purchase']['cost'] for m in craft['materials'] if m['id'] != material_id)
    made_net = output_sale['price']*craft['qty']*95//100 - others
    return {'spell': craft['spell'], 'material': material_id, 'input_qty': target['qty'],
            'output': craft['item'], 'output_qty': craft['qty'], 'raw_net': raw_net,
            'other_cost': others, 'processed_net': made_net, 'gain': made_net-raw_net,
            'raw_sale': raw_sale, 'output_sale': output_sale,
            'choice': 'process' if made_net > raw_net else 'sell_raw'}


def plan_actions(data, cfg, now=None):
    now = now or datetime.now(timezone.utc)
    profile = character_profile(cfg)
    items = {i['id']: i for i in data['items']}
    side = {'horde': 'Horde', 'alliance': 'Alliance'}.get(data['market']['faction'])
    budget = 50000  # Trial cash cap, 5g; a setting, not a measured market quantity.
    vendors, crafts, used = [], [], set()
    for v in data['vendors']:
        item = items.get(v['item'])
        s = sell_scenario(item, now)
        if (not s or not side or v['side'] not in (side, 'Both') or
                v['verification'] != 'source_reported_live' or v['build'] != data['build'] or
                v['costs'] != [] or v['price'] is None or not v['qty'] or
                v['basis'] != 'per_purchase' or v['eligibility'] == 'ineligible'):
            continue
        if item.get('binding') in ('1', 'BIND_ON_ACQUIRE', 'BIND_ON_PICKUP', 'QUEST'):
            continue
        calc = trial_math(v['price'], v['qty'], s)
        calc['buy_ceiling'] = min(budget, calc['buy_ceiling'])
        if calc['cost'] > budget or calc['cost'] > calc['buy_ceiling'] or calc['stress_difference'] <= 0:
            continue
        vendors.append(dict(calc, item=v['item'], offer_id=v['id'], sale=s,
                            checks=['현재 NPC 구매 가능·재고', '귀속·거래 가능', '경매장 현재 등록가·경쟁 매물']))
    vendors.sort(key=lambda x: (-x['stress_difference'], x['cost']))
    vendors = [x for x in vendors if not (x['item'] in used or used.add(x['item']))][:20]
    for c in data['crafts']:
        if not allowed(c, profile):
            continue
        item = items.get(c['item'])
        s = sell_scenario(item, now)
        if (not s or c['verification'] != 'client_catalog' or c['build'] != data['build'] or
                c['cost'] is None or c['cost'] > budget or not c['qty'] or not c['complete']):
            continue
        if item.get('binding') in ('1', 'BIND_ON_ACQUIRE', 'BIND_ON_PICKUP', 'QUEST'):
            continue
        # All ingredient prices must be recent too; missing prices are never free.
        if any(m['purchase'] is None or
               (m['purchase']['method'] == 'auction' and sell_scenario(items.get(m['id']), now) is None)
               for m in c['materials']):
            continue
        calc = trial_math(c['cost'], c['qty'], s)
        calc['buy_ceiling'] = min(budget, calc['buy_ceiling'])
        if calc['cost'] > calc['buy_ceiling'] or calc['stress_difference'] <= 0:
            continue
        crafts.append(dict(calc, item=c['item'], spell=c['spell'], sale=s,
                           checks=['레시피·숙련·도구', '각 재료의 현재 가격과 수량', '거래 가능·판매 경쟁']))
    crafts.sort(key=lambda x: (-x['stress_difference'], x['cost']))
    by_profession = {}
    for a in crafts:
        c = next(c for c in data['crafts'] if c['spell'] == a['spell'])
        by_profession.setdefault(c['profession'], []).append(a)
    services = [c['spell'] for c in data['crafts'] if c.get('service') and allowed(c, profile)
                and c['verification'] == 'client_catalog' and c['build'] == data['build']]
    comparisons = {'fish': [], 'cloth': []}
    selected = dict(profile['primary'], **profile['secondary'])
    for c in data['crafts']:
        if not allowed(c, profile) or c['verification'] != 'client_catalog' or c['build'] != data['build']:
            continue
        targets = (profile['fishing_watch_items'] if c['profession'] == 'cooking' and
                   ('fishing' in selected or not profile['configured']) else
                   CLOTH if c['profession'] in ('tailoring', 'first-aid') else [])
        for iid in targets:
            result = process_comparison(c, iid, items, now)
            if result:
                comparisons['fish' if c['profession'] == 'cooking' else 'cloth'].append(result)
    for rows in comparisons.values():
        rows.sort(key=lambda x: -x['gain'])
    farms = []
    for i in data['items']:
        # Actual source subtype, not a name-based claim about farm location or drop rate.
        fish = i['id'] in profile['fishing_watch_items'] and ('fishing' in selected or not profile['configured'])
        if not fish and (i.get('category') != 'Trade Goods' or i.get('subclass') not in ('Herb', 'Cloth', 'Elemental', 'Leather', 'Metal & Stone', 'Enchanting')):
            continue
        if profile['configured'] and not fish:
            required = {'Herb': 'herbalism', 'Metal & Stone': 'mining', 'Leather': 'skinning', 'Enchanting': 'enchanting'}.get(i.get('subclass'))
            if required and required not in selected:
                continue
        if fish:
            route = 'fishing_candidate'
        elif i.get('subclass') == 'Enchanting':
            route = 'disenchant_unverified'
        else:
            route = 'material_unverified'
        if i.get('binding') in ('1', 'BIND_ON_ACQUIRE', 'BIND_ON_PICKUP', 'QUEST'):
            continue
        s = sell_scenario(i, now)
        if not s or (not fish and (not i.get('craft_uses') or s['stress_price'] < 100)):
            continue
        net = Fraction(s['stress_price']*95, 100)
        need = -(-Fraction(100000, 1)//net) if net else None
        farms.append({'item': i['id'], 'subclass': i.get('subclass'), 'sale': s, 'route': route,
                      'net_per_unit': s['stress_price']*95//100,
                      'units_for_10g': int(need) if need else None,
                      'source_verified': False, 'yield_per_hour': None, 'gold_per_hour': None})
    farms.sort(key=lambda x: -x['net_per_unit'])
    return {'vendors': vendors, 'crafts': crafts[:20], 'craft_pool': crafts, 'farms': farms,
            'by_profession': {p: rows[:20] for p, rows in by_profession.items()},
            'services': services, 'comparisons': comparisons,
            'budget': budget, 'target_margin_pct': 20, 'haircut_pct': 20,
            'farming_goal': 100000, 'sample_limit': '1묶음 / 제작 1회',
            'recommendation_status': 'conditional_trial',
            'note': '매출과 회전율이 확인되지 않은 소량 시험 후보. 수익 추천과 확정 파밍 순위는 데이터 부족.'}


def action_report(data):
    """Portable human-readable actions with evidence and explicit assumptions."""
    items = {i['id']: i for i in data['items']}
    vendors = {v['id']: v for v in data['vendors']}
    crafts = {c['spell']: c for c in data['crafts']}
    gold = lambda n: f'{n/10000:.4f}g'
    clean = lambda s: str(s).replace('|', '/').replace('\n', ' ')
    out = ['# 한국 PvP 행동 후보', '', f"생성: {data['generated']} · 진영: {data['market']['faction']}", '',
           '수익 추천: 데이터 부족. 아래는 구매 조건과 현재 매물을 확인한 뒤 1묶음/제작 1회만 시험할 조건부 후보입니다.',
           '등록 가정은 최근 7일의 마지막 최저가 기록과 일별 최저가 중 가장 낮은 값에서 1동 차감. 중위가·체결가가 아닙니다.',
           '수수료 5% 차감. 가격 20% 하락 시에도 원가 대비 차액 20%가 남는지 검사합니다. 20%는 설정값이며 예측 확률이 아닙니다.',
           '시험 원가 5g 이하. 보증금 손실·이동·학습 비용 별도. 판매량과 매물 수량 미확인; 판매 전 추가 매입 보류.', '']
    for key, title in [('vendors', '상점 구매 후 시험 판매'), ('crafts', '배운 제작법으로 1회 시험')]:
        out += ['## '+title, '', '| 아이템 | 살 것 → 행동 | 비용 | 등록 가정/개 | 전량 판매 차액 | 20% 하락 시 차액 | 구매 상한 | 근거 / 확인할 것 |',
                '|---|---|---:|---:|---:|---:|---:|---|']
        for a in data['actions'][key]:
            if key == 'vendors':
                v = vendors[a['offer_id']]
                step = f"{v['npc']} / {v['zone']} ({v['x']},{v['y']})에서 {v['qty']}개 한 묶음 구매 → 보유 → 내 경매장 등록"
                source = v
            else:
                c = crafts[a['spell']]
                step = ', '.join(f"{items[m['id']]['name']} ×{m['qty']}" for m in c['materials'])
                step += f" 구매 → spell:{c['spell']} 1회 제작 → {c['qty']}개 등록"
                source = c
            s = a['sale']
            proof = f"#{s['snapshot']}, {s['files']}가격파일/{s['days']}관측일, 일별 {s['daily_points']}행/파일근거 {s['daily_imports']}, 관측 {s['observed']}; {source['url']}; 빌드 {source['build']}; 조회 {source['date']}; "+', '.join(a['checks'])
            out.append('| '+' | '.join(map(clean,[items[a['item']]['name'],step,gold(a['cost']),gold(s['price']),
                        gold(a['difference']),gold(a['stress_difference']),gold(a['buy_ceiling']),proof]))+' |')
        if not data['actions'][key]:
            out += ['조건을 충족한 후보 없음.']
        out += ['']
    out += ['## 설정한 전문기술별 비교', '',
            '이 문서는 config의 기본 선택으로 생성합니다. 브라우저 체크박스 변경은 HTML에 즉시 적용되고 이 문서를 수정하지 않습니다.',
            '숙련 null은 미확인입니다. 보유 기술을 골랐어도 레시피·요구 숙련·레벨 확인이 필요합니다.', '',
            '| 전문기술 | 후보 | 요구 숙련 | 1회 원가 | 판매 가정 차액 | 가격 20% 하락 시 |',
            '|---|---|---:|---:|---:|---:|']
    for profession, rows in data['actions']['by_profession'].items():
        for a in rows[:3]:
            c = crafts[a['spell']]
            out.append('| '+' | '.join(map(clean, [profession, c['name'], c.get('skill') or '미확인',
                       gold(a['cost']), gold(a['difference']), gold(a['stress_difference'])]))+' |')
    out += ['', '## 보유 생선·천을 그대로 팔기 vs 가공', '',
            '두 경로 모두 수수료 5% 차감. 가공 순수입은 추가 재료 구입비 차감 후이며 보유 원재료의 판매 기회비용을 비교합니다.', '',
            '| 보유 재료 | 가공 | 그대로 판매 순수입 | 가공 후 순수입 | 가공 증감 | 근거 |',
            '|---|---|---:|---:|---:|---|']
    for rows in data['actions']['comparisons'].values():
        for r in rows[:20]:
            c = crafts[r['spell']]
            out.append('| '+' | '.join(map(clean, [f"{items[r['material']]['name']} ×{r['input_qty']}",
                c['name'], gold(r['raw_net']), gold(r['processed_net']), gold(r['gain']),
                f"원재료 #{r['raw_sale']['snapshot']} / {r['raw_sale']['files']}파일, 결과물 #{r['output_sale']['snapshot']} / {r['output_sale']['files']}파일; {c['url']}; {c['build']}"]))+' |')
    out += ['', f"직접 마법부여 서비스 {len(data['actions']['services'])}개: 고객 견적은 미확인. 내 재료이면 합의 금액−재료비, 고객이 재료를 전부 제공하면 합의 금액이 차액입니다. 경매 수수료는 없고 시간·기타 비용은 별도입니다.", '',
            '## 파밍 비교', '',
            '확정 효율 순위: 데이터 부족. 장소·획득량·이동 포함 소요시간·실제 판매 결과가 필요합니다.',
            '아래는 개당 금액 순이며 파밍 추천 순위가 아닙니다. 20% 하락 등록가, 수수료 5%, 전량 판매, 추가 비용 0의 가정입니다.', '',
            '| 목표 재료 | 마지막 최저가 | 하방 시나리오 순수입/개 | 10g/시간에 필요한 개수 | 근거 |',
            '|---|---:|---:|---:|---|']
    for f in data['actions']['farms'][:20]:
        i = items[f['item']]
        out.append('| '+' | '.join(map(clean,[i['name'],gold(i['price']),gold(f['net_per_unit']),
                    str(f['units_for_10g'])+'개 획득·판매',f"#{f['sale']['snapshot']} / {f['sale']['observed']}; 제작 용도 {i['craft_uses']}개 (판매량 아님); 획득처·시간당 수량 미확인"]))+' |')
    return '\n'.join(out)+'\n'
