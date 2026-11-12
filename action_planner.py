"""Conditional one-purchase trials and farming targets; no invented sales/yields."""
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from common import AppError
from profession_profile import character_profile, allowed, CLOTH


def canon_side(value):
    """Map API/config spellings. Unknown text is kept; only three sides are factions."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return {'horde': 'Horde', 'alliance': 'Alliance', 'both': 'Both'}.get(text.casefold(), text or None)


def _optional_int(value, label, lo, hi):
    """None means unknown. Bool is not an integer here."""
    if value is None:
        return None
    if type(value) is not int or value < lo or (hi is not None and value > hi):
        raise AppError(f'{label}은(는) {lo} 이상' + (f' {hi} 이하' if hi is not None else '') + ' 정수 또는 null입니다.')
    return value


def _setting_int(cfg, key, default, lo, hi):
    """A tuning knob. Null or a missing key uses the built-in default, not a guessed market rate."""
    if key not in cfg or cfg[key] is None:
        return default
    return _optional_int(cfg[key], key, lo, hi)


def market_settings(cfg):
    """Deposit and sell-through stay unknown when null. Other knobs have defaults."""
    cfg = cfg or {}
    listing = cfg.get('listing_cost', {})
    if listing is None:
        listing = {}
    if not isinstance(listing, dict):
        raise AppError('listing_cost는 객체이거나 null입니다.')
    return {
        'haircut_pct': _setting_int(cfg, 'stress_haircut_pct', 20, 0, 100),
        'min_distinct_days': _setting_int(cfg, 'min_distinct_days', 3, 1, 10000),
        'max_price_age_days': _setting_int(cfg, 'max_price_age_days', 3, 0, 10000),
        'trial_budget_copper': _setting_int(cfg, 'trial_budget_copper', 50000, 0, None),
        'total_budget_copper': _optional_int(cfg['total_budget_copper'], 'total_budget_copper', 0, None) if 'total_budget_copper' in cfg else None,
        'max_position_share_pct': _setting_int(cfg, 'max_position_share_pct', 20, 1, 100),
        'deposit_rate_pct': _optional_int(listing.get('deposit_rate_pct'), 'deposit_rate_pct', 0, 100),
        'deposit_copper_per_item': _optional_int(listing.get('deposit_copper_per_item'), 'deposit_copper_per_item', 0, None),
        'sell_through_pct': _optional_int(listing.get('sell_through_pct'), 'sell_through_pct', 0, 100),
    }


def sell_settings_for_item(settings, item):
    """Only an explicit scenario supplies sale probability; disappearance is not a sale."""
    if settings.get('sell_through_pct') is not None:
        return settings, '사용자 설정'
    return settings, '데이터 부족'


def _dates(item):
    if not item:
        return []
    raw = []
    if item.get('observed'):
        raw.append(item['observed'])
    for key in ('history', 'daily_history'):
        for point in item.get(key) or []:
            if point.get('observed'):
                raw.append(point['observed'])
    days = set()
    for value in raw:
        try:
            days.add(datetime.fromisoformat(value).date().isoformat())
        except (TypeError, ValueError):
            continue
    return days


def distinct_days(item):
    return len(_dates(item))


def price_age_seconds(item, now):
    if not item or not item.get('observed'):
        return None
    try:
        observed = datetime.fromisoformat(item['observed'])
    except (TypeError, ValueError):
        return None
    return int((now - observed).total_seconds())


def liquidity(item, now, settings):
    """Too few days or a stale latest price is 관찰 필요, never an optimistic pass."""
    days = distinct_days(item)
    age = price_age_seconds(item, now)
    limit = settings['max_price_age_days'] * 86400
    if not item or item.get('price') is None or age is None:
        status, reason = '데이터 부족', '가격 또는 관측일이 없습니다.'
    elif age < 0:
        status, reason = '데이터 부족', '관측일이 현재보다 뒤입니다.'
    elif age > limit:
        status, reason = '관찰 필요', f"최근 가격이 {settings['max_price_age_days']}일보다 오래되었습니다."
    elif days < settings['min_distinct_days']:
        status, reason = '관찰 필요', f"서로 다른 관측일이 {settings['min_distinct_days']}일보다 적습니다."
    else:
        status, reason = 'ok', None
    return {'distinct_days': days, 'price_age_seconds': age, 'latest_observed': item.get('observed') if item else None,
            'status': status, 'reason': reason, 'min_distinct_days': settings['min_distinct_days'],
            'max_price_age_days': settings['max_price_age_days']}


def sell_scenario(item, now, haircut=20, max_age_days=3):
    """A stress scenario, explicitly NOT a median or expected clearing price."""
    if not item or not item.get('price') or not item.get('observed'):
        return None
    cutoff = now - timedelta(days=7)
    observed = datetime.fromisoformat(item['observed'])
    # Age strictly greater than max_age_days is unusable. Equal to the limit still counts.
    if observed > now or observed < now - timedelta(days=max_age_days):
        return None
    history = [p for p in item['history'] if p['observed'] and
               cutoff <= datetime.fromisoformat(p['observed']) <= now]
    daily = [p for p in item.get('daily_history', []) if p['low'] is not None and
             cutoff <= datetime.fromisoformat(p['observed']) <= now]
    basis = '최근 7일의 관측 최저가 중 최솟값'
    if item.get('price_source') == 'blizzard':
        medians = sorted(p['median'] for p in history if p.get('median'))
        if not medians:
            return None
        recent_median = medians[(len(medians)-1)//2]
        reference = min(item['price'], recent_median)
        basis = '현재 최저가와 최근 7일 스냅샷별 수량 가중 중위가의 하위 중위 중 작은 값'
    else:
        reference = min([item['price']] + [p['price'] for p in history] + [p['low'] for p in daily])
    price = max(1, reference - 1)
    stress = price * (100-haircut) // 100
    return {'reference': reference, 'price': price, 'stress_price': stress,
            'source': item.get('price_source', 'auctionator'), 'house_id': item.get('price_house_id'),
            'basis': basis,
            'haircut_pct': haircut, 'observed': item['observed'],
            'snapshot': item['snapshot'], 'files': len(item['snapshots']),
            'points': len(history), 'days': len({p['observed'] for p in history+daily}),
            'distinct_days': distinct_days(item),
            'price_age_seconds': int((now - observed).total_seconds()),
            'daily_points': len(daily), 'daily_imports': sorted({p['import_id'] for p in daily})}


def listing_quote(sale, qty, cost, settings):
    """Fee once on the sale. Deposit loss only when deposit and sell-through are both known.

    Expected loss is the forfeited deposit of listings that expire before the first sale:
    ceil(deposit * (100 - p) / p). The successful listing's deposit is assumed returned.
    p = 0 has no finite expectation, so the loss stays 데이터 부족. This is not a Forever rule.
    """
    unknown = {'sale': sale, 'qty': qty, 'gross': None, 'ah_cut': None, 'after_fee': None,
               'buy_cost': cost, 'deposit': None, 'deposit_status': '데이터 부족',
               'expected_deposit_loss': None, 'expected_deposit_loss_status': '데이터 부족',
               'net_ex_deposit': None, 'official_net': None, 'official_net_status': '데이터 부족',
               'sell_through_pct': settings.get('sell_through_pct'),
               'loss_model': '보증금 조건과 체결 확률이 모두 있어야 기대 손실을 계산합니다.'}
    if sale is None or not qty or cost is None:
        return unknown
    gross = sale * qty
    after_fee = gross * 95 // 100
    ah_cut = gross - after_fee
    net_ex = after_fee - cost
    parts = []
    if settings.get('deposit_copper_per_item') is not None:
        parts.append(settings['deposit_copper_per_item'] * qty)
    if settings.get('deposit_rate_pct') is not None:
        parts.append(gross * settings['deposit_rate_pct'] // 100)
    deposit = sum(parts) if parts else None
    sell_p = settings.get('sell_through_pct')
    loss = None
    official = None
    if deposit is None or sell_p is None or sell_p <= 0:
        loss_status = '데이터 부족'
        note = '체결 확률 또는 보증금이 없습니다. 기대 손실을 0으로 두지 않습니다.'
        if sell_p == 0 and deposit is not None:
            note = '체결 확률 0%는 재등록 기댓값이 유한하지 않습니다. 데이터 부족.'
    else:
        # p == 100 → no failed listing. Otherwise ceil(deposit * (100-p) / p).
        loss = 0 if sell_p == 100 else (deposit * (100 - sell_p) + sell_p - 1) // sell_p
        official = net_ex - loss
        loss_status = 'known'
        note = '1회 성공 전까지 만료된 등록의 보증금만 잃는다고 본 사용자 설정. Forever 보증금 공식이 아닙니다.'
    return {'sale': sale, 'qty': qty, 'gross': gross, 'ah_cut': ah_cut, 'after_fee': after_fee,
            'buy_cost': cost, 'deposit': deposit,
            'deposit_status': 'known' if deposit is not None else '데이터 부족',
            'expected_deposit_loss': loss, 'expected_deposit_loss_status': loss_status,
            'net_ex_deposit': net_ex, 'official_net': official,
            'official_net_status': 'known' if official is not None else '데이터 부족',
            'sell_through_pct': sell_p, 'loss_model': note}


def position_plan(cost, unit_qty, settings):
    """A suggestion is one purchase/craft; both cash limits apply to that same unit."""
    share = settings['max_position_share_pct']
    trial = settings['trial_budget_copper']
    total = settings['total_budget_copper']
    base = {'trial_budget_copper': trial, 'total_budget_copper': total, 'position_share_pct': share,
            'unit_cost': cost, 'unit_qty': unit_qty}
    cap = total * share // 100 if total is not None else None
    limit = min(trial, cap) if cap is not None else trial
    fits = cost > 0 and cost <= limit
    return dict(base, capital_required=cost if fits else 0, suggested_units=int(fits),
                suggested_qty=unit_qty if fits else 0, position_cap_copper=cap,
                cap_status='known' if cap is not None else '데이터 부족', excluded=not fits,
                cap_note=f'1묶음/제작 1회만 시험. 시험 상한 {trial}동 · 품목 집중 한도 '
                         + (f'{cap}동. 둘 중 작은 한도 {limit}동 적용.' if cap is not None
                            else '총예산 미설정으로 미확인. 시험 상한만 적용.'))


def trial_math(cost, qty, scenario, target_margin=20):
    gross = scenario['price'] * qty
    net = gross * 95 // 100
    stress_net = scenario['stress_price'] * qty * 95 // 100
    return {'cost': cost, 'qty': qty, 'net': net, 'difference': net-cost,
            'stress_difference': stress_net-cost,
            'buy_ceiling': stress_net*100//(100+target_margin),
            'break_even': (cost*100 + 95*qty-1)//(95*qty),
            'target_margin_pct': target_margin}


def _gap_row(craft, material_id, reason, items, now, settings):
    material = items.get(material_id) if material_id is not None else None
    output = items.get(craft.get('item'))
    raw_liq = liquidity(material, now, settings) if material_id is not None else None
    out_liq = liquidity(output, now, settings) if output else None
    return {'spell': craft.get('spell'), 'material': material_id,
            'input_qty': next((m['qty'] for m in craft.get('materials') or [] if m['id'] == material_id), None),
            'output': craft.get('item'), 'output_qty': craft.get('qty'), 'status': '데이터 부족',
            'reason': reason, 'raw_net': None, 'other_cost': None, 'processed_net': None, 'gain': None,
            'stress_raw_net': None, 'stress_processed_net': None, 'stress_gain': None,
            'choice': None, 'stress_choice': None, 'recommendation': '데이터 부족',
            'liquidity': '데이터 부족', 'raw_sale': None, 'output_sale': None,
            'raw_distinct_days': None if raw_liq is None else raw_liq['distinct_days'],
            'output_distinct_days': None if out_liq is None else out_liq['distinct_days'],
            'raw_price_age_seconds': None if raw_liq is None else raw_liq['price_age_seconds'],
            'output_price_age_seconds': None if out_liq is None else out_liq['price_age_seconds'],
            'haircut_pct': settings['haircut_pct'],
            'unstressed_basis': '비교할 최근 가격이 없습니다. 스트레스 없는 차액을 만들지 않습니다.'}


def process_comparison(craft, material_id, items, now, haircut=20, max_age_days=3, settings=None):
    """Caught/owned inputs have the opportunity cost of selling them directly.

    `choice` / `gain` stay on the unstressed listing price (reference − 1 copper).
    The recommendation uses the 20% stress prices. Unstressed figures are labeled as such.
    """
    settings = settings or market_settings({})
    target = next((m for m in craft['materials'] if m['id'] == material_id), None)
    output_sale = sell_scenario(items.get(craft['item']), now, haircut, max_age_days)
    raw_sale = sell_scenario(items.get(material_id), now, haircut, max_age_days)
    if (not target or not raw_sale or not output_sale or craft['cost'] is None
            or not craft['qty'] or not craft['complete'] or
            any(m['purchase'] is None or (m['purchase']['method'] == 'auction'
                and sell_scenario(items.get(m['id']), now, haircut, max_age_days) is None) for m in craft['materials'])):
        return None
    raw_net = raw_sale['price']*target['qty']*95//100
    others = sum(m['purchase']['cost'] for m in craft['materials'] if m['id'] != material_id)
    made_net = output_sale['price']*craft['qty']*95//100 - others
    raw_stress = raw_sale['stress_price']*target['qty']*95//100
    made_stress = output_sale['stress_price']*craft['qty']*95//100 - others
    raw_liq = liquidity(items.get(material_id), now, settings)
    out_liq = liquidity(items.get(craft['item']), now, settings)
    # Any extra auction ingredient that is thin or stale also blocks a confident call.
    thin = raw_liq['status'] != 'ok' or out_liq['status'] != 'ok'
    if not thin:
        for material in craft['materials']:
            if material['id'] == material_id or material['purchase']['method'] != 'auction':
                continue
            if liquidity(items.get(material['id']), now, settings)['status'] != 'ok':
                thin = True
                break
    stress_choice = 'process' if made_stress > raw_stress else 'sell_raw'
    return {'spell': craft['spell'], 'material': material_id, 'input_qty': target['qty'],
            'output': craft['item'], 'output_qty': craft['qty'], 'raw_net': raw_net,
            'other_cost': others, 'processed_net': made_net, 'gain': made_net-raw_net,
            'stress_raw_net': raw_stress, 'stress_processed_net': made_stress,
            'stress_gain': made_stress-raw_stress, 'raw_sale': raw_sale, 'output_sale': output_sale,
            'choice': 'process' if made_net > raw_net else 'sell_raw', 'stress_choice': stress_choice,
            'recommendation': '관찰 필요' if thin else stress_choice, 'status': 'ok',
            'liquidity': '관찰 필요' if thin else 'ok',
            'raw_distinct_days': raw_liq['distinct_days'], 'output_distinct_days': out_liq['distinct_days'],
            'raw_price_age_seconds': raw_liq['price_age_seconds'],
            'output_price_age_seconds': out_liq['price_age_seconds'],
            'haircut_pct': haircut,
            'unstressed_basis': '등록 가정(참고 최저−1동)에 수수료 5%만 적용한 값. 20% 스트레스 시험이 아닙니다.'}


def alchemy_rows(craft, items, now, settings):
    """Herb vs potion only when the item record says subclass Herb. No invented reagents."""
    if craft.get('profession') != 'alchemy':
        return []
    materials = craft.get('materials') or []
    herbs, unknown = [], False
    for material in materials:
        item = items.get(material['id'])
        subclass = item.get('subclass') if item else None
        if subclass == 'Herb':
            herbs.append(material)
        elif subclass in (None, ''):
            unknown = True
    if not herbs:
        if unknown or not materials:
            return [_gap_row(craft, None, '약초 재료를 확인하지 못했습니다. 분류가 없는 아이템을 약초로 두지 않습니다.',
                             items, now, settings)]
        return []
    rows = []
    for material in herbs:
        result = process_comparison(craft, material['id'], items, now, settings['haircut_pct'],
                                    settings['max_price_age_days'], settings)
        if result is None:
            rows.append(_gap_row(craft, material['id'],
                                 '원물·결과물·추가 재료의 가격, 수량, 최근 관측이 모두 있어야 비교합니다.',
                                 items, now, settings))
        else:
            rows.append(result)
    return rows


BOUND = ('1', 'BIND_ON_ACQUIRE', 'BIND_ON_PICKUP', 'QUEST')


def _apply_money(calc, scenario, unit_qty, settings, liq, item=None):
    """Official net uses the stress sale. Unknown deposit loss is left unknown, not zero."""
    use, label = sell_settings_for_item(settings, item)
    stress = listing_quote(scenario['stress_price'], calc['qty'], calc['cost'], use)
    plain = listing_quote(scenario['price'], calc['qty'], calc['cost'], use)
    plan = position_plan(calc['cost'], unit_qty, settings)
    calc.update(plan, liquidity=liq, listing=plain, stress_listing=stress,
                official_net=stress['official_net'], official_net_status=stress['official_net_status'],
                ah_cut=stress['ah_cut'], deposit=stress['deposit'], deposit_status=stress['deposit_status'],
                expected_deposit_loss=stress['expected_deposit_loss'],
                expected_deposit_loss_status=stress['expected_deposit_loss_status'],
                net_ex_deposit=stress['net_ex_deposit'], unstressed_net_ex_deposit=plain['net_ex_deposit'],
                unstressed_ah_cut=plain['ah_cut'], unstressed_deposit=plain['deposit'],
                unstressed_basis='스트레스 없는 등록 가정. 20% 스트레스 시험이 아닙니다.',
                sell_through_pct=use.get('sell_through_pct'), sell_through_label=label)
    return plan


def _watch(kind, item_id, liq, reason, status='관찰 필요', **extra):
    row = {'kind': kind, 'item': item_id, 'status': status, 'reason': reason or liq['reason'],
           'distinct_days': liq['distinct_days'], 'price_age_seconds': liq['price_age_seconds'],
           'latest_observed': liq['latest_observed']}
    row.update(extra)
    return row


def plan_actions(data, cfg, now=None):
    now = now or datetime.now(timezone.utc)
    settings = market_settings(cfg)
    profile = character_profile(cfg)
    items = {i['id']: i for i in data['items']}
    # 'both' / unknown means the character faction is not established.
    side = canon_side(data['market'].get('faction'))
    if side not in ('Horde', 'Alliance'):
        side = None
    budget = settings['trial_budget_copper']
    haircut, max_age = settings['haircut_pct'], settings['max_price_age_days']
    vendors, crafts, watch, used = [], [], [], set()
    for v in data['vendors']:
        item = items.get(v['item'])
        if item is None or not side or canon_side(v.get('side')) not in (side, 'Both'):
            continue
        if (v['verification'] != 'source_reported_live' or v['build'] != data['build'] or
                v['costs'] != [] or v['price'] is None or not v['qty'] or
                v['basis'] != 'per_purchase' or v['eligibility'] == 'ineligible' or
                item.get('binding') in BOUND):
            continue
        liq = liquidity(item, now, settings)
        scenario = sell_scenario(item, now, haircut, max_age)
        if liq['status'] != 'ok' or not scenario:
            if liq['status'] == '관찰 필요':
                watch.append(_watch('vendor', v['item'], liq, liq['reason'], offer_id=v['id']))
            continue
        calc = trial_math(v['price'], v['qty'], scenario)
        calc['buy_ceiling'] = min(budget, calc['buy_ceiling'])
        if calc['cost'] > budget or calc['cost'] > calc['buy_ceiling'] or calc['stress_difference'] <= 0:
            continue
        plan = _apply_money(calc, scenario, v['qty'], settings, liq, item)
        if calc['official_net'] is not None and calc['official_net'] <= 0:
            watch.append(_watch('vendor', v['item'], liq, '가정 순이익이 0 이하입니다. 매수 추천에서 뺍니다.',
                                offer_id=v['id'], status='순이익 없음'))
            continue
        if plan['excluded']:
            watch.append(_watch('vendor', v['item'], liq, plan['cap_note'], offer_id=v['id'], status='한도 초과'))
            continue
        vendors.append(dict(calc, kind='vendor', item=v['item'], offer_id=v['id'], sale=scenario,
                            checks=['현재 NPC 구매 가능·재고', '귀속·거래 가능', '경매장 현재 등록가·경쟁 매물']))
    vendors.sort(key=lambda x: (-x['stress_difference'], x['cost']))
    vendors = [x for x in vendors if not (x['item'] in used or used.add(x['item']))][:20]
    for c in data['crafts']:
        if not allowed(c, profile):
            continue
        item = items.get(c['item'])
        if item is None or c['verification'] != 'client_catalog' or c['build'] != data['build']:
            continue
        if c['cost'] is None or c['cost'] > budget or not c['qty'] or not c['complete']:
            continue
        if item.get('binding') in BOUND:
            continue
        material_states, missing = [], False
        for material in c['materials']:
            if material['purchase'] is None:
                missing = True
                break
            if material['purchase']['method'] != 'auction':
                continue
            source = items.get(material['id'])
            state = liquidity(source, now, settings)
            priced = sell_scenario(source, now, haircut, max_age)
            if state['status'] == '데이터 부족' or (priced is None and state['status'] != '관찰 필요'):
                missing = True
                break
            material_states.append(state)
        if missing:
            continue
        liq = liquidity(item, now, settings)
        scenario = sell_scenario(item, now, haircut, max_age)
        states = material_states + [liq]
        if any(state['status'] == '관찰 필요' for state in states) or not scenario:
            if any(state['status'] == '관찰 필요' for state in states):
                reason = next(state['reason'] for state in states if state['status'] == '관찰 필요')
                watch.append(_watch('craft', c['item'], liq, reason, spell=c['spell']))
            continue
        calc = trial_math(c['cost'], c['qty'], scenario)
        calc['buy_ceiling'] = min(budget, calc['buy_ceiling'])
        if calc['cost'] > calc['buy_ceiling'] or calc['stress_difference'] <= 0:
            continue
        plan = _apply_money(calc, scenario, c['qty'], settings, liq, item)
        if calc['official_net'] is not None and calc['official_net'] <= 0:
            watch.append(_watch('craft', c['item'], liq, '가정 순이익이 0 이하입니다. 매수 추천에서 뺍니다.',
                                spell=c['spell'], status='순이익 없음'))
            continue
        if plan['excluded']:
            watch.append(_watch('craft', c['item'], liq, plan['cap_note'], spell=c['spell'], status='한도 초과'))
            continue
        crafts.append(dict(calc, kind='craft', item=c['item'], spell=c['spell'], sale=scenario,
                           checks=['레시피·숙련·도구', '각 재료의 현재 가격과 수량', '거래 가능·판매 경쟁']))
    crafts.sort(key=lambda x: (-x['stress_difference'], x['cost']))
    by_profession = {}
    for action in crafts:
        craft = next(c for c in data['crafts'] if c['spell'] == action['spell'])
        by_profession.setdefault(craft['profession'], []).append(action)
    services = [c['spell'] for c in data['crafts'] if c.get('service') and allowed(c, profile)
                and c['verification'] == 'client_catalog' and c['build'] == data['build']]
    comparisons = {'fish': [], 'cloth': [], 'alchemy': []}
    selected = dict(profile['primary'], **profile['secondary'])
    for c in data['crafts']:
        if c['verification'] != 'client_catalog' or c['build'] != data['build']:
            continue
        # A herbalist can compare selling herbs with the alchemy craft that uses them
        # without the craft becoming a buy recommendation.
        herb_compare = c.get('profession') == 'alchemy' and (
            not profile['configured'] or 'alchemy' in selected or 'herbalism' in selected)
        if allowed(c, profile):
            targets = (profile['fishing_watch_items'] if c['profession'] == 'cooking' and
                       ('fishing' in selected or not profile['configured']) else
                       CLOTH if c['profession'] in ('tailoring', 'first-aid') else [])
            for iid in targets:
                result = process_comparison(c, iid, items, now, haircut, max_age, settings)
                if result:
                    comparisons['fish' if c['profession'] == 'cooking' else 'cloth'].append(result)
        elif not herb_compare:
            continue
        if herb_compare:
            comparisons['alchemy'].extend(alchemy_rows(c, items, now, settings))
    for rows in comparisons.values():
        rows.sort(key=lambda row: (row.get('status') == '데이터 부족',
                                   -(row.get('stress_gain') if row.get('stress_gain') is not None else row.get('gain') or 0)))
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
        if i.get('binding') in BOUND:
            continue
        scenario = sell_scenario(i, now, haircut, max_age)
        if not scenario or (not fish and (not i.get('craft_uses') or scenario['stress_price'] < 100)):
            continue
        liq = liquidity(i, now, settings)
        net = Fraction(scenario['stress_price']*95, 100)
        need = -(-Fraction(100000, 1)//net) if net else None
        farms.append({'item': i['id'], 'subclass': i.get('subclass'), 'sale': scenario, 'route': route,
                      'net_per_unit': scenario['stress_price']*95//100,
                      'units_for_10g': int(need) if need else None,
                      'source_verified': False, 'yield_per_hour': None, 'gold_per_hour': None,
                      'liquidity': liq['status'], 'distinct_days': liq['distinct_days'],
                      'price_age_seconds': liq['price_age_seconds']})
    farms.sort(key=lambda x: -x['net_per_unit'])
    seen = set()
    unique_watch = []
    for row in watch:
        key = (row['kind'], row.get('item'), row.get('spell'), row.get('offer_id'))
        if key in seen:
            continue
        seen.add(key)
        unique_watch.append(row)
    shown = crafts[:20]
    deposit_known = settings['deposit_rate_pct'] is not None or settings['deposit_copper_per_item'] is not None
    sell_known = settings['sell_through_pct'] not in (None, 0) and deposit_known
    if sell_known:
        basis = '사용자 설정'
    else:
        basis = '데이터 부족'
    return {'vendors': vendors, 'crafts': shown, 'craft_pool': crafts, 'farms': farms,
            'by_profession': {p: rows[:20] for p, rows in by_profession.items()},
            'services': services, 'comparisons': comparisons, 'watch': unique_watch,
            'buy_recommendations': [a for a in vendors + crafts
                                    if a.get('official_net') is not None and a['official_net'] > 0],
            'budget': budget, 'trial_budget_copper': budget,
            'total_budget_copper': settings['total_budget_copper'],
            'position_share_pct': settings['max_position_share_pct'],
            'min_distinct_days': settings['min_distinct_days'],
            'max_price_age_days': settings['max_price_age_days'],
            'listing_cost': {'deposit_rate_pct': settings['deposit_rate_pct'],
                             'deposit_copper_per_item': settings['deposit_copper_per_item'],
                             'sell_through_pct': settings['sell_through_pct']},
            'sell_through_known': sell_known, 'sell_through_basis': basis, 'target_margin_pct': 20, 'haircut_pct': haircut,
            'listing_model': '기대 보증금 손실 = 올림(보증금 × (100−체결%) / 체결%). 성공 등록의 보증금은 돌아온다고 가정하며 Forever 공식이 아닙니다. 개당 보증금과 판매가 비율을 둘 다 적으면 더합니다. 하나라도 없으면 데이터 부족이고 0으로 빼지 않습니다.',
            'farming_goal': 100000, 'sample_limit': '1묶음 / 제작 1회',
            'recommendation_status': 'conditional_trial',
            'note': '매출과 회전율이 확인되지 않은 소량 시험 후보. 관측이 얇거나 오래된 후보는 관찰 필요이며 매수 추천에서 뺍니다. 확정 파밍 순위는 데이터 부족.'}


def action_report(data):
    """Portable human-readable actions with evidence and explicit assumptions."""
    items = {i['id']: i for i in data['items']}
    vendors = {v['id']: v for v in data['vendors']}
    crafts = {c['spell']: c for c in data['crafts']}
    gold = lambda n: f'{n/10000:.4f}g'
    show = lambda n: '데이터 부족' if n is None else gold(n)
    age = lambda seconds: '데이터 부족' if seconds is None else f'{seconds/86400:.1f}일'
    clean = lambda s: str(s).replace('|', '/').replace('\n', ' ')
    actions = data['actions']
    buys = actions.get('buy_recommendations') or []
    basis = actions.get('sell_through_basis') or ('사용자 설정' if actions.get('sell_through_known') else '데이터 부족')
    if not actions.get('sell_through_known'):
        official = '가정 순이익: 데이터 부족. 아래는 조건부 시험입니다. 보증금 기대 손실을 0으로 빼지 않습니다.'
    elif not buys:
        official = '가정 순이익을 계산할 설정은 있습니다. 조건을 통과한 매수 추천은 없습니다. 기대 손실을 0으로 채우지 않습니다.'
    else:
        official = f"가정 순이익 후보 {len(buys)}개. floor(스트레스 판매가×수량×0.95)−구매비용−기대 보증금 손실. 체결 확률은 사용자 설정이며 측정값이 아닙니다."
    out = ['# 한국 PvP 행동 후보', '', f"생성: {data['generated']} · 진영: {data['market']['faction']}", '',
           official,
           actions.get('listing_model', ''),
           '등록 가정: 확인한 공식 시장은 현재 최저가와 최근 7일 스냅샷별 수량 가중 중위가의 하위 중위 중 낮은 값. Auctionator는 최근 7일 관측 최저가 중 최솟값. 각각 1동 차감하며 체결가가 아닙니다.',
           f"수수료 5%는 판매 대금에 한 번. 가격 {actions.get('haircut_pct', 20)}% 하락 시에도 원가 대비 차액 20%가 남는지 검사합니다. 하락률은 설정값이며 예측 확률이 아닙니다.",
           f"시험 상한 {actions.get('trial_budget_copper', actions.get('budget'))}동. 총예산이 없으면 품목 집중 한도는 데이터 부족이고 시험 상한만 적용합니다.",
           f"관측일이 {actions.get('min_distinct_days', 3)}일보다 적거나 최근 가격이 {actions.get('max_price_age_days', 3)}일보다 오래되면 관찰 필요이며 매수 추천에서 뺍니다.",
           '판매량과 매물 수량 미확인. 판매 전 추가 매입 보류.', '']
    if canon_side(data['market'].get('faction')) not in ('Horde', 'Alliance'):
        out.append('상점 시험 후보를 만들지 않습니다. market_faction이 horde 또는 alliance가 아니면 NPC 진영을 추정하지 않습니다.')
        out.append('')
    for key, title in [('vendors', '상점 구매 후 시험 판매'), ('crafts', '배운 제작법으로 1회 시험')]:
        out += ['## '+title, '',
                '| 아이템 | 살 것 → 행동 | 구매 비용 | 수수료 5% | 보증금 | 기대 보증금 손실 | 보증금 제외 순이익 | 가정 순이익 | 제안 수량 | 필요 자금 | 관측일 | 최신 가격 | 근거 / 확인할 것 |',
                '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
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
            proof = (f"{s.get('source','auctionator')} 경매장 {s.get('house_id')}, #{s['snapshot']}, {s['files']}스냅샷/{s['days']}개 7일 기록, 서로 다른 관측일 {s.get('distinct_days')}일, "
                     f"일별 {s['daily_points']}행/파일근거 {s['daily_imports']}, 관측 {s['observed']}; "
                     f"{a.get('cap_note','')}; {source['url']}; 빌드 {source['build']}; 조회 {source['date']}; "
                     + ', '.join(a['checks'])
                     + (f"; 체결 {a.get('sell_through_pct')}% 추정(판매 또는 취소, 확정 판매 아님)"
                        if a.get('sell_through_label') == '추정' else ''))
            out.append('| '+' | '.join(map(clean,[items[a['item']]['name'], step, show(a['cost']), show(a.get('ah_cut')),
                        show(a.get('deposit')) if a.get('deposit_status') != '데이터 부족' else '데이터 부족',
                        show(a.get('expected_deposit_loss')) if a.get('expected_deposit_loss_status') != '데이터 부족' else '데이터 부족',
                        show(a.get('net_ex_deposit')), show(a.get('official_net')) if a.get('official_net_status') != '데이터 부족' else '데이터 부족',
                        str(a.get('suggested_qty')), show(a.get('capital_required')),
                        str(s.get('distinct_days')), age(s.get('price_age_seconds')), proof]))+' |')
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
    out += ['', '## 관찰 필요 · 매수 추천에서 제외', '']
    watched = actions.get('watch') or []
    if not watched:
        out.append('없음.')
    for row in watched[:20]:
        out.append(f"- {items[row['item']]['name']}: {row.get('status')} · {row.get('reason')} · 관측일 {row.get('distinct_days')} · 최신 가격 {age(row.get('price_age_seconds'))}")
    out += ['', '## 보유 생선·천·약초를 그대로 팔기 vs 가공', '',
            '판단은 20% 스트레스 가격입니다. 바로 옆의 증감은 스트레스 없는 등록 가정이며 스트레스 시험이 아닙니다.',
            '두 경로 모두 수수료 5%를 판매 쪽에 한 번 적용합니다. 약초는 아이템 분류가 Herb인 재료만 사용합니다.',
            '가격·수량·분류가 없으면 데이터 부족이며 수율을 만들지 않습니다.', '',
            '| 보유 재료 | 가공 | 스트레스 원물 순수입 | 스트레스 가공 순수입 | 스트레스 증감 | 판단 | 스트레스 없는 증감 | 관측일 | 근거 |',
            '|---|---|---:|---:|---:|---|---:|---|---|']
    for rows in data['actions']['comparisons'].values():
        for r in rows[:20]:
            craft = crafts.get(r.get('spell')) or {}
            craft_name = craft.get('name') or str(r.get('spell'))
            if r.get('status') == '데이터 부족' or not r.get('raw_sale'):
                material = '재료 미확인' if r.get('material') is None else items.get(r['material'], {}).get('name', 'item')
                out.append('| '+' | '.join(map(clean, [material, craft_name, '데이터 부족', '데이터 부족', '데이터 부족',
                    '데이터 부족', '데이터 부족', '데이터 부족', r.get('reason') or '데이터 부족']))+' |')
                continue
            out.append('| '+' | '.join(map(clean, [f"{items[r['material']]['name']} ×{r['input_qty']}",
                craft_name, show(r.get('stress_raw_net')), show(r.get('stress_processed_net')), show(r.get('stress_gain')),
                r.get('recommendation') or r.get('stress_choice'), show(r.get('gain')),
                f"원물 {r.get('raw_distinct_days')}일 / 결과 {r.get('output_distinct_days')}일",
                f"원재료 #{r['raw_sale']['snapshot']} / {r['raw_sale']['files']}파일, 결과물 #{r['output_sale']['snapshot']} / {r['output_sale']['files']}파일; {craft.get('url')}; {craft.get('build')}"]))+' |')
    out += ['', f"직접 마법부여 서비스 {len(data['actions']['services'])}개: 고객 견적은 미확인. 내 재료이면 합의 금액−재료비, 고객이 재료를 전부 제공하면 합의 금액이 차액입니다. 경매 수수료는 없고 시간·기타 비용은 별도입니다.", '',
            '## 파밍 비교', '',
            '확정 효율 순위: 데이터 부족. 장소·획득량·이동 포함 소요시간·실제 판매 결과가 필요합니다.',
            '아래는 개당 금액 순이며 파밍 추천 순위가 아닙니다. 20% 하락 등록가, 수수료 5%, 전량 판매, 추가 비용 0의 가정입니다.', '',
            '| 목표 재료 | 마지막 최저가 | 하방 시나리오 순수입/개 | 10g/시간에 필요한 개수 | 근거 |',
            '|---|---:|---:|---:|---|']
    for f in data['actions']['farms'][:20]:
        i = items[f['item']]
        out.append('| '+' | '.join(map(clean,[i['name'],gold(i['price']),gold(f['net_per_unit']),
                    str(f['units_for_10g'])+'개 획득·판매',
                    f"#{f['sale']['snapshot']} / {f['sale']['observed']}; 관측일 {f.get('distinct_days')} · 최신 {age(f.get('price_age_seconds'))}; 제작 용도 {i['craft_uses']}개 (판매량 아님); 획득처·시간당 수량 미확인"]))+' |')
    out += ['', '## 블리자드 Forever 경매장', '',
            '내 시장으로 확인한 경매장의 가격만 계산에 연결합니다. 소멸 비율은 판매 확률에 넣지 않습니다.',
            '시간대는 한국 시간입니다. 스냅샷이 모자라면 시간대와 소멸 지표는 데이터 부족입니다.',
            '만료 전에 사라진 매물은 판매 또는 취소의 추정입니다. 확정 판매가 아닙니다.']
    info = data.get('blizzard') or {}
    if info.get('note'):
        out.append(info['note'])
    if (info.get('match') or {}).get('note'):
        out.append(info['match']['note'])
    selected = info.get('selected_house_id')
    out.append(info.get('price_note', '공식 시장 가격 미확인. Auctionator 관측 유지.'))
    shown_rows = 0
    for item in data.get('items') or []:
        for house in (item.get('blizzard') or {}).get('houses') or []:
            if shown_rows >= 12:
                break
            hourly = house.get('hourly') or {}
            if hourly.get('status') == '추정':
                hours = (f"싼 시각 {', '.join(map(str, hourly.get('cheapest_hours_kst') or []))}시 / "
                         f"비싼 시각 {', '.join(map(str, hourly.get('expensive_hours_kst') or []))}시 (추정)")
            else:
                hours = '시간대 데이터 부족'
            if house.get('disappearance_status') == '추정':
                sold = f"소멸 추정 {house.get('disappearance_pct')}%"
            else:
                sold = '소멸 지표 데이터 부족'
            out.append(f"- {item.get('name') or item.get('id')} · 경매장 {house.get('house_id')}: "
                       f"최저 {gold(house['min_unit_price']) if house.get('min_unit_price') else '데이터 부족'}, "
                       f"관측일 {house.get('distinct_days')}, 최신 {age(house.get('price_age_seconds'))}, "
                       f"유동성 {house.get('liquidity')}, {hours}, {sold}")
            shown_rows += 1
        if shown_rows >= 12:
            break
    if shown_rows == 0:
        out.append('보관된 블리자드 가격이 없습니다. 데이터 부족.')
    return '\n'.join(out)+'\n'
