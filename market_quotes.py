"""Choose one confirmed market; retain Auctionator evidence without blending markets."""
from collections import defaultdict
from datetime import timedelta

from common import AppError, parse_time
from blizzard_collector import forever_settings
from action_planner import market_settings


def activate_quotes(db, cfg, items, summary, now):
    settings = forever_settings(cfg)
    for item in items.values():
        item['price_source'] = 'auctionator'
        item['price_house_id'] = None
    summary['price_note'] = '계산 기준: Auctionator 관측. 공식 경매장 확인 전에는 시장을 자동으로 섞지 않습니다.'
    if not settings['market_confirmed']:
        return
    house_id = settings['house_id']
    latest = next((h for h in summary['latest_houses'] if h['house_id'] == house_id), None)
    faction = str(cfg.get('market_faction', '')).lower()
    if (settings['region'] != cfg.get('market_region') or
            settings['namespace'] != 'dynamic-forever-' + settings['region']):
        raise AppError('확인한 공식 시장의 지역/제품과 설정이 다릅니다.')
    if latest and (faction not in ('horde', 'alliance') or latest['side'] != faction):
        raise AppError('확인한 공식 경매장의 진영이 내 시장과 다릅니다. 중립 시장도 별도로 확인해야 합니다.')
    cutoff = now - timedelta(days=market_settings(cfg)['max_price_age_days'])
    levels = defaultdict(list)
    if latest:
        for row in db.execute('''SELECT item_id,kind,unit_price,quantity FROM blizzard_listings
                                 WHERE snapshot_id=? ORDER BY unit_price,listing_id''', (latest['id'],)):
            levels[row['item_id']].append(dict(row))
    for item in items.values():
        # These remain available in details, but cannot silently revive absent/stale API listings.
        item['auctionator'] = {key: item.get(key) for key in (
            'price', 'observed', 'snapshot', 'snapshots', 'history', 'daily_history', 'ranges', 'days', 'delta')}
        house = next((h for h in item['blizzard']['houses'] if h['house_id'] == house_id), None)
        usable = bool(house and house['present_latest'] and
                      cutoff <= parse_time(house['observed']) <= now)
        history = house['history'] if house else []
        item.update(price_source='blizzard', price_house_id=house_id,
                    price=house['min_unit_price'] if usable else None,
                    median_price=house['median_unit_price'] if usable else None,
                    quantity=house['quantity'] if usable else None,
                    observed=house['observed'] if house else None,
                    snapshot=latest['id'] if latest else None,
                    snapshots=[p['snapshot'] for p in history], history=history,
                    daily_history=[], ranges={}, delta=None,
                    days=len({parse_time(p['observed']).date() for p in history}),
                    price_levels=levels[item['id']] if usable else [])
    summary['price_note'] = (f'계산 기준: 내 시장으로 확인한 공식 경매장 #{house_id}. '
                             '최근 전체 스냅샷의 매물만 사용하며 미관측·오래된 가격은 추천에서 제외합니다.')


def purchase_quote(item, needed):
    if not item or item.get('price') is None:
        return None
    if item.get('price_source') != 'blizzard':
        return {'cost': item['price'] * needed, 'buy_qty': needed, 'method': 'auction', 'offer_id': None}
    cost, bought = 0, 0
    for level in item.get('price_levels', []):
        # Non-commodity auctions are whole listings; commodity orders allow partial quantities.
        take = level['quantity'] if level['kind'] == 'auction' else min(needed-bought, level['quantity'])
        cost += take * level['unit_price']
        bought += take
        if bought >= needed:
            return {'cost': cost, 'buy_qty': bought, 'method': 'auction', 'offer_id': None}
    return None  # Observed supply cannot fill the required quantity.
