"""Hourly Blizzard WoW Forever KR auction snapshots. Stdlib only. No invented prices.

Default houses are every Horde and Blackwater (검은바다) id on the ruleset index.
Alliance houses are skipped unless house_ids or include_alliance says otherwise.
A listing that vanishes before its time_left bucket can expire is an estimate of
sold-or-cancelled, never a confirmed sale.
"""
import base64
import json
import os
from collections import defaultdict
from datetime import timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import time

from common import (AppError, connect, ensure_items, get_meta, http_json, load_config, load_env,
                    main_guard, parse_time, parser, positive_int, run, utcnow, weighted_median)


# Public API time_left buckets. Count a disappearance only when the gap is shorter
# than the bucket minimum, so the listing cannot have expired yet. SHORT is always
# too short to prove that. The result is sold or cancelled, not a confirmed sale.
TIME_LEFT_MIN_SECONDS = {'MEDIUM': 30 * 60, 'LONG': 2 * 3600, 'VERY_LONG': 12 * 3600}
MIN_SELL_SNAPSHOTS = 6
MIN_SELL_DISTINCT_DAYS = 3
MIN_SELL_CLASSIFIED = 20
MIN_HOURLY_SNAPSHOTS = 12
MIN_HOURLY_DAYS = 2
MIN_HOURLY_DISTINCT_HOURS = 4
MIN_HOURLY_PER_HOUR = 2
MIN_MATCH_ITEMS = 5
LIVE_REQUEST_INTERVAL = 0.1
COLLECT_DEADLINE = 8 * 60
SIDE_WORDS = (
    ('blackwater', ('검은바다', 'blackwater')),
    ('alliance', ('얼라이언스', 'alliance')),
    ('horde', ('호드', 'horde')),
)
SIDE_LABEL = {'horde': '호드', 'alliance': '얼라이언스', 'blackwater': '검은바다', 'unknown': '미확인'}


def forever_settings(cfg):
    """Safe defaults. house_ids empty means discover Horde + Blackwater from the index."""
    block = (cfg or {}).get('blizzard_forever') or {}
    if not isinstance(block, dict):
        raise AppError('blizzard_forever는 객체입니다.')
    ruleset = block.get('ruleset', 1)
    if type(ruleset) is not int or isinstance(ruleset, bool) or ruleset <= 0:
        raise AppError('blizzard_forever.ruleset은 양의 정수입니다.')
    namespace = block.get('namespace', 'dynamic-forever-kr')
    locale = block.get('locale', 'ko_KR')
    region = block.get('region', 'kr')
    for label, value, pattern in (
            ('namespace', namespace, r'[A-Za-z0-9_-]+'),
            ('locale', locale, r'[A-Za-z0-9_]+'),
            ('region', region, r'[a-z]{2}')):
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise AppError(f'blizzard_forever.{label} 형식이 아닙니다.')
    include = block.get('include_alliance', False)
    if not isinstance(include, bool):
        raise AppError('include_alliance는 true 또는 false입니다.')
    confirmed = block.get('market_confirmed', False)
    if not isinstance(confirmed, bool):
        raise AppError('blizzard_forever.market_confirmed는 true 또는 false입니다.')
    house_id = block.get('house_id', None)
    if house_id is not None and (type(house_id) is not int or isinstance(house_id, bool) or house_id <= 0):
        raise AppError('blizzard_forever.house_id는 양의 정수 또는 null입니다.')
    if confirmed and house_id is None:
        raise AppError('내 시장을 확인했다면 blizzard_forever.house_id를 명시하세요.')
    house_ids = block.get('house_ids') or []
    if (not isinstance(house_ids, list) or
            any(type(i) is not int or isinstance(i, bool) or i <= 0 for i in house_ids)):
        raise AppError('blizzard_forever.house_ids는 양의 정수 배열입니다.')
    db_path = block.get('db_path', None)
    if db_path is not None and not isinstance(db_path, str):
        raise AppError('blizzard_forever.db_path는 문자열 또는 null입니다.')
    offset = (cfg or {}).get('scan_timezone_offset_hours', 9)
    if type(offset) is not int or isinstance(offset, bool) or not -14 <= offset <= 14:
        raise AppError('scan_timezone_offset_hours는 -14 이상 14 이하 정수입니다.')
    return {'ruleset': ruleset, 'namespace': namespace, 'locale': locale, 'region': region,
            'include_alliance': include, 'house_id': house_id, 'house_ids': list(house_ids),
            'db_path': db_path, 'offset_hours': offset, 'market_confirmed': confirmed}


def selected_house_id(cfg):
    """A display selection only; prices additionally require market_confirmed and an explicit id."""
    settings = forever_settings(cfg)
    if settings['house_id']:
        return settings['house_id']
    if len(settings['house_ids']) == 1:
        return settings['house_ids'][0]
    return None


def name_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = []
        preferred = value.get('ko_KR')
        if isinstance(preferred, str):
            parts.append(preferred)
        for key, text in value.items():
            if key != 'ko_KR' and isinstance(text, str):
                parts.append(text)
        return ' '.join(parts)
    return ''


def classify_side(name):
    text = (name or '').casefold()
    for side, words in SIDE_WORDS:
        if any(word.casefold() in text for word in words):
            return side
    return 'unknown'


def parse_index(body):
    """Index shape: auctions[{id or key.href, name}]. Names are strings or locale maps."""
    if not isinstance(body, dict) or not isinstance(body.get('auctions'), list):
        raise AppError('경매장 색인 auctions 배열이 없습니다.')
    houses = []
    seen = set()
    for row in body['auctions']:
        if not isinstance(row, dict):
            continue
        house_id = row.get('id')
        if not positive_int(house_id):
            href = ''
            key = row.get('key')
            if isinstance(key, dict) and isinstance(key.get('href'), str):
                href = key['href']
            elif isinstance(row.get('href'), str):
                href = row['href']
            match = re.search(r'/auctions/(\d+)(?:\?|$)', href)
            house_id = int(match.group(1)) if match else None
        if not positive_int(house_id) or house_id in seen:
            continue
        seen.add(house_id)
        name = name_text(row.get('name'))
        houses.append({'id': house_id, 'name': name, 'side': classify_side(name)})
    return houses


def select_houses(houses, house_ids=None, include_alliance=False):
    """Default is every Horde and Blackwater house on the index, keyed by id. No fixed 113–118 list."""
    if house_ids:
        wanted = []
        found = {house['id']: house for house in houses}
        missing = []
        for house_id in house_ids:
            if house_id not in found:
                missing.append(house_id)
            elif found[house_id] not in wanted:
                wanted.append(found[house_id])
        if missing:
            raise AppError('설정한 경매장 ID가 색인에 없습니다: ' + ','.join(str(i) for i in missing))
        return wanted
    sides = {'horde', 'blackwater'}
    if include_alliance:
        sides.add('alliance')
    return [house for house in houses if house['side'] in sides]


def _time_left(value):
    if isinstance(value, dict):
        value = value.get('type') or value.get('name') or ''
    if not isinstance(value, str):
        return ''
    return value.strip().upper()


def parse_listings(body, kind):
    """Priced listings only. A missing/null array is not evidence of an empty house.

    Non-commodity unit price is floor(buyout / quantity). Commodity unit_price is
    already per item and is not divided. Bid-only and non-integer prices are omitted.
    """
    if body is None:
        raise AppError('경매 응답이 없습니다.')
    if not isinstance(body, dict):
        raise AppError('경매 응답 형식 오류')
    rows = body.get('auctions')
    if not isinstance(rows, list):
        raise AppError('auctions 배열이 아닙니다.')
    listings, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = row.get('item') if isinstance(row.get('item'), dict) else {}
        item_id, qty = item.get('id'), row.get('quantity')
        if kind == 'commodity':
            amount = row.get('unit_price')
            if not all(positive_int(x) for x in (item_id, qty, amount)):
                continue
            price = amount
        else:
            amount = row.get('buyout')
            if not all(positive_int(x) for x in (item_id, qty, amount)):
                continue
            price = amount // qty
            if price <= 0:
                continue
        listing_id = row.get('id')
        if not positive_int(listing_id) or (kind, listing_id) in seen:
            continue
        seen.add((kind, listing_id))
        listings.append({'kind': kind, 'listing_id': listing_id, 'item_id': item_id,
                         'unit_price': price, 'quantity': qty, 'time_left': _time_left(row.get('time_left'))})
    return listings, len(rows)


def weighted_p25(levels):
    """Lower 25th percentile by quantity. Same rank rule as the lower weighted median."""
    total = sum(qty for _, qty in levels)
    if total <= 0:
        raise AppError('수량 가중 분위수에 양수 수량이 필요합니다.')
    threshold, seen = (total + 3) // 4, 0
    for price, qty in sorted(levels):
        seen += qty
        if seen >= threshold:
            return price
    raise AppError('하위 25% 가격을 계산하지 못했습니다.')


def aggregate_prices(listings):
    """Per item: min, quantity-weighted lower median, p25, total quantity, priced listing count."""
    groups, counts = defaultdict(lambda: defaultdict(int)), defaultdict(int)
    for row in listings:
        groups[row['item_id']][row['unit_price']] += row['quantity']
        counts[row['item_id']] += 1
    rows = []
    for item_id, levels in groups.items():
        pairs = list(levels.items())
        rows.append((item_id, min(levels), weighted_median(pairs), weighted_p25(pairs),
                     sum(levels.values()), counts[item_id]))
    return rows


def disappeared_before_expiry(time_left, gap_seconds):
    minimum = TIME_LEFT_MIN_SECONDS.get((time_left or '').upper(), 0)
    return minimum > 0 and gap_seconds > 0 and gap_seconds < minimum


def sell_through_estimate(disappeared, survived, snapshots, distinct_days):
    """Activity ratio only. Not a per-listing sale probability; cancellations are included."""
    classified = disappeared + survived
    base = {'disappeared': disappeared, 'survived': survived, 'classified': classified,
            'snapshots': snapshots, 'distinct_days': distinct_days}
    if (snapshots < MIN_SELL_SNAPSHOTS or distinct_days < MIN_SELL_DISTINCT_DAYS
            or classified < MIN_SELL_CLASSIFIED):
        return dict(base, disappearance_pct=None, disappearance_status='데이터 부족',
                    note=(f'스냅샷 {MIN_SELL_SNAPSHOTS}개, 서로 다른 날 {MIN_SELL_DISTINCT_DAYS}일, '
                          f'판정 매물 {MIN_SELL_CLASSIFIED}건이 필요합니다. 판매 확률로 사용하지 않습니다.'))
    pct = (disappeared * 100) // classified
    return dict(base, disappearance_pct=pct, disappearance_status='추정',
                note='연속 관측에서 만료 전 사라진 매물의 비율. 취소 포함·동일 매물 반복 집계·관측 간격 의존. 판매 확률이나 보증금 계산에 사용하지 않습니다.')


def local_parts(iso, offset_hours):
    shifted = parse_time(iso) + timedelta(hours=offset_hours)
    return shifted.hour, shifted.date().isoformat()


def hourly_pattern(points, offset_hours=9):
    """Cheapest and most expensive KST hours from snapshot minimums. Thin history stays unknown."""
    buckets, days = defaultdict(list), set()
    for iso, price in points:
        if not positive_int(price):
            continue
        hour, day = local_parts(iso, offset_hours)
        buckets[hour].append(price)
        days.add(day)
    usable = {hour: prices for hour, prices in buckets.items() if len(prices) >= MIN_HOURLY_PER_HOUR}
    enough = (len(points) >= MIN_HOURLY_SNAPSHOTS and len(days) >= MIN_HOURLY_DAYS
              and len(usable) >= MIN_HOURLY_DISTINCT_HOURS)
    empty = {'status': '데이터 부족', 'cheapest_hours_kst': [], 'expensive_hours_kst': [],
             'cheapest_median': None, 'expensive_median': None, 'snapshots': len(points),
             'distinct_days': len(days), 'hours_observed': len(buckets),
             'note': (f'한국 시간 비교에는 스냅샷 {MIN_HOURLY_SNAPSHOTS}개, {MIN_HOURLY_DAYS}일, '
                      f'서로 다른 시각 {MIN_HOURLY_DISTINCT_HOURS}개, 시각마다 {MIN_HOURLY_PER_HOUR}회가 필요합니다.')}
    if not enough:
        return empty
    medians = {hour: weighted_median([(price, 1) for price in prices]) for hour, prices in usable.items()}
    low, high = min(medians.values()), max(medians.values())
    if low == high:
        return dict(empty, snapshots=len(points), distinct_days=len(days), hours_observed=len(buckets),
                    note='시각별 최저가 중위가 같습니다. 싼 시각과 비싼 시각을 나누지 않습니다.')
    return {'status': '추정', 'cheapest_hours_kst': sorted(h for h, price in medians.items() if price == low),
            'expensive_hours_kst': sorted(h for h, price in medians.items() if price == high),
            'cheapest_median': low, 'expensive_median': high, 'snapshots': len(points),
            'distinct_days': len(days), 'hours_observed': len(usable),
            'note': '각 시각에 모인 스냅샷 최저가의 하위 중위가입니다. 그 시각에 팔린다는 뜻이 아닙니다.'}


def median_lower(values):
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def match_price_maps(auctionator, houses):
    """Median relative error of shared minimums. Missing Auctionator prices do not pick a house."""
    if not auctionator:
        return {'status': '데이터 부족', 'house_id': None, 'candidates': [], 'tied_house_ids': [],
                'note': 'Auctionator 가격이 없습니다. 경매장을 추정하지 않습니다.'}
    if not houses:
        return {'status': '데이터 부족', 'house_id': None, 'candidates': [], 'tied_house_ids': [],
                'note': '비교할 블리자드 스냅샷이 없습니다. 경매장을 추정하지 않습니다.'}
    candidates = []
    for house in houses:
        errors = []
        prices = house.get('prices') or {}
        for item_id, seen in auctionator.items():
            other = prices.get(item_id)
            if not positive_int(seen) or not positive_int(other):
                continue
            errors.append(abs(other - seen) * 10000 // seen)
        if len(errors) < MIN_MATCH_ITEMS:
            continue
        candidates.append({'house_id': house['house_id'], 'side': house.get('side'), 'name': house.get('name'),
                           'shared_items': len(errors), 'median_relative_error_bp': median_lower(errors)})
    if not candidates:
        return {'status': '데이터 부족', 'house_id': None, 'candidates': [], 'tied_house_ids': [],
                'note': f'같은 아이템이 {MIN_MATCH_ITEMS}개 이상 겹치는 경매장이 없습니다. 경매장을 추정하지 않습니다.'}
    candidates.sort(key=lambda row: (row['median_relative_error_bp'], -row['shared_items'], row['house_id']))
    best = candidates[0]
    tied = [row['house_id'] for row in candidates
            if row['median_relative_error_bp'] == best['median_relative_error_bp']
            and row['shared_items'] == best['shared_items']]
    note = 'Auctionator 최저가와 겹친 아이템의 중앙 상대오차가 가장 작은 경매장입니다. 소속 확정이 아닌 추정입니다.'
    if len(tied) > 1:
        note = '중앙 상대오차와 겹친 아이템 수가 같은 경매장이 둘 이상입니다. 가장 작은 번호만 골랐으며 확정이 아닙니다.'
    return {'status': '추정', 'house_id': best['house_id'], 'shared_items': best['shared_items'],
            'median_relative_error_bp': best['median_relative_error_bp'], 'candidates': candidates,
            'tied_house_ids': tied, 'note': note}


def index_path(ruleset):
    return f'/data/wow/ruleset/{ruleset}/auctions/index'


def auction_path(ruleset, house_id):
    return f'/data/wow/ruleset/{ruleset}/auctions/{house_id}'


def commodity_path(ruleset, house_id):
    return f'/data/wow/ruleset/{ruleset}/auctions/{house_id}/commodities'


class ForeverAuctions:
    """Client-credentials client for dynamic-forever-*. Retail Blizzard stays on dynamic-kr."""

    def __init__(self, cfg, fixture=None, env_path=None):
        settings = forever_settings(cfg)
        self.settings = settings
        self.fixture = None
        if fixture is not None:
            self.fixture = fixture if isinstance(fixture, dict) else json.loads(
                Path(fixture).read_text(encoding='utf-8'))
        self.token = None
        self.expires = 0
        self.client_id = ''
        self.secret = ''
        self._paced_at = None
        # Fixture runs stay instant. Live calls leave a short gap so a tight loop is less likely to trip a WAF.
        self.request_interval = 0.0 if self.fixture is not None else LIVE_REQUEST_INTERVAL
        if self.fixture is None:
            if env_path:
                load_env(Path(env_path))
            load_env(cfg['base_dir'] / '.env')
            self.client_id = os.environ.get('BNET_CLIENT_ID', '')
            self.secret = os.environ.get('BNET_CLIENT_SECRET', '')
            if not self.client_id or not self.secret:
                raise AppError('.env에 BNET_CLIENT_ID / BNET_CLIENT_SECRET을 설정하세요. 값은 로그에 남기지 않습니다.')

    def _pace(self):
        interval = self.request_interval or 0
        if interval <= 0:
            return
        now = time.monotonic()
        if self._paced_at is not None:
            wait = interval - (now - self._paced_at)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
        self._paced_at = now

    def get(self, path, modified=None):
        if self.fixture is not None:
            if path not in self.fixture:
                raise AppError('샘플 API 응답에 경로가 없습니다.')
            entry = self.fixture[path]
            stamp = entry.get('last_modified')
            if modified and stamp and modified == stamp:
                return None, stamp
            return entry.get('body'), stamp
        if not self.token or time.time() >= self.expires:
            self._pace()
            basic = base64.b64encode((self.client_id + ':' + self.secret).encode()).decode()
            body, _ = http_json('https://oauth.battle.net/token',
                                {'Authorization': 'Basic ' + basic,
                                 'Content-Type': 'application/x-www-form-urlencoded'},
                                b'grant_type=client_credentials')
            token = body.get('access_token') if isinstance(body, dict) else None
            if not isinstance(token, str) or not token:
                raise AppError('토큰 응답에 access_token이 없습니다. 본문은 기록하지 않습니다.')
            self.token = token
            try:
                expires_in = int(body.get('expires_in') or 0)
            except (TypeError, ValueError):
                expires_in = 0
            self.expires = time.time() + expires_in - 60
        headers = {'Authorization': 'Bearer ' + self.token}
        if modified:
            headers['If-Modified-Since'] = modified
        query = f'namespace={self.settings["namespace"]}&locale={self.settings["locale"]}'
        url = f'https://{self.settings["region"]}.api.blizzard.com{path}?{query}'
        self._pace()
        return http_json(url, headers)


def upsert_houses(db, ruleset, houses, updated_at):
    db.executemany('''INSERT INTO blizzard_houses(house_id,ruleset,name,side,updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(house_id, ruleset) DO UPDATE SET name=excluded.name, side=excluded.side,
        updated_at=excluded.updated_at''',
        ((house['id'], ruleset, house['name'], house['side'], updated_at) for house in houses))


def _latest_snapshot(db, house_id, ruleset):
    return db.execute('''SELECT * FROM blizzard_snapshots WHERE house_id=? AND ruleset=?
        ORDER BY fetched_at DESC, id DESC LIMIT 1''', (house_id, ruleset)).fetchone()


def _load_listings(db, snapshot_id, kind):
    return [{'kind': kind, 'listing_id': row['listing_id'], 'item_id': row['item_id'],
             'unit_price': row['unit_price'], 'quantity': row['quantity'], 'time_left': row['time_left']}
            for row in db.execute('SELECT * FROM blizzard_listings WHERE snapshot_id=? AND kind=?',
                                  (snapshot_id, kind))]


def record_check(db, house_id, ruleset, checked_at, auctions_lm, commodities_lm, stored, snapshot_id, note):
    cursor = db.execute('''INSERT INTO blizzard_checks(house_id,ruleset,checked_at,auctions_last_modified,
        commodities_last_modified,stored,snapshot_id,note) VALUES (?,?,?,?,?,?,?,?)''',
        (house_id, ruleset, checked_at, auctions_lm, commodities_lm, stored, snapshot_id, note))
    return cursor.lastrowid


def store_snapshot(db, house_id, ruleset, fetched_at, listings, n_auctions, n_commodities,
                   auctions_lm, commodities_lm):
    prices = aggregate_prices(listings)
    ensure_items(db, (row[0] for row in prices))
    epoch = int(parse_time(fetched_at).timestamp())
    cursor = db.execute('''INSERT INTO blizzard_snapshots(house_id,ruleset,fetched_at,fetched_epoch,
        auctions_last_modified,commodities_last_modified,n_auctions,n_commodities,n_priced)
        VALUES (?,?,?,?,?,?,?,?,?)''',
        (house_id, ruleset, fetched_at, epoch, auctions_lm, commodities_lm,
         n_auctions, n_commodities, len(listings)))
    snapshot_id = cursor.lastrowid
    db.executemany('''INSERT INTO blizzard_item_prices VALUES (?,?,?,?,?,?,?)''',
                   ((snapshot_id, *row) for row in prices))
    db.executemany('''INSERT INTO blizzard_listings VALUES (?,?,?,?,?,?,?)''',
                   ((snapshot_id, row['kind'], row['listing_id'], row['item_id'], row['unit_price'],
                     row['quantity'], row['time_left']) for row in listings))
    return snapshot_id


def _unchanged(body, stamp, previous):
    """304 or the same Last-Modified as the stored snapshot. A missing body on the first fetch is not a skip."""
    if previous and body is None:
        return True
    return bool(previous and stamp and stamp == previous)


def _listings_unpublished(body):
    """A 200 JSON object whose auctions field is missing or null. Not a confirmed empty market."""
    return isinstance(body, dict) and body.get('auctions') is None


def _skip_unpublished(db, house_id, ruleset, fetched_at, auctions_lm, commodities_lm):
    with db:
        check_id = record_check(db, house_id, ruleset, fetched_at, auctions_lm, commodities_lm,
                                0, None, '매물 목록 없음')
    print(f'경매장 {house_id}: 매물 목록 없음, 스냅샷 생략 (확인 #{check_id})', flush=True)
    return None


def collect_house(db, api, ruleset, house, fetched_at):
    house_id = house['id']
    previous = _latest_snapshot(db, house_id, ruleset)
    prev_auctions = previous['auctions_last_modified'] if previous else None
    prev_commodities = previous['commodities_last_modified'] if previous else None
    auctions_body, auctions_lm = api.get(auction_path(ruleset, house_id), modified=prev_auctions)
    commodities_body, commodities_lm = api.get(commodity_path(ruleset, house_id), modified=prev_commodities)
    auctions_same = _unchanged(auctions_body, auctions_lm, prev_auctions)
    commodities_same = _unchanged(commodities_body, commodities_lm, prev_commodities)
    if auctions_body is None:
        auctions_lm = auctions_lm or prev_auctions
    if commodities_body is None:
        commodities_lm = commodities_lm or prev_commodities
    if previous and auctions_same and commodities_same:
        with db:
            check_id = record_check(db, house_id, ruleset, fetched_at, auctions_lm, commodities_lm, 0, None,
                                    'Last-Modified 동일')
        print(f'경매장 {house_id}: Last-Modified 동일 ({auctions_lm or "없음"} / {commodities_lm or "없음"}), '
              f'스냅샷 생략 (확인 #{check_id})', flush=True)
        return None
    if auctions_same:
        auction_rows = _load_listings(db, previous['id'], 'auction')
        n_auctions = previous['n_auctions']
        auctions_lm = auctions_lm or prev_auctions
        auctions_unpublished = False
    elif _listings_unpublished(auctions_body):
        auction_rows, n_auctions, auctions_unpublished = None, None, True
    else:
        auction_rows, n_auctions = parse_listings(auctions_body, 'auction')
        auctions_unpublished = False
    if commodities_same:
        commodity_rows = _load_listings(db, previous['id'], 'commodity')
        n_commodities = previous['n_commodities']
        commodities_lm = commodities_lm or prev_commodities
        commodities_unpublished = False
    elif _listings_unpublished(commodities_body):
        commodity_rows, n_commodities, commodities_unpublished = None, None, True
    else:
        commodity_rows, n_commodities = parse_listings(commodities_body, 'commodity')
        commodities_unpublished = False
    # One unpublished side is enough to skip. Storing the other side would invent a 0-count
    # market or replace a real snapshot with a partial one.
    if auctions_unpublished or commodities_unpublished:
        return _skip_unpublished(db, house_id, ruleset, fetched_at, auctions_lm, commodities_lm)
    listings = auction_rows + commodity_rows
    with db:
        snapshot_id = store_snapshot(db, house_id, ruleset, fetched_at, listings, n_auctions, n_commodities,
                                     auctions_lm, commodities_lm)
        record_check(db, house_id, ruleset, fetched_at, auctions_lm, commodities_lm, 1, snapshot_id, '저장')
    print(f'경매장 {house_id} {SIDE_LABEL.get(house["side"], house["side"])}: 스냅샷 #{snapshot_id}, '
          f'경매 {n_auctions}, 재료 {n_commodities}, 단가 있음 {len(listings)}, '
          f'Last-Modified 경매 {auctions_lm or "없음"} / 재료 {commodities_lm or "없음"}', flush=True)
    return snapshot_id


def collect_once(db, api, cfg, house_ids=None, now=None):
    settings = forever_settings(cfg)
    ruleset = settings['ruleset']
    fetched_at = now or utcnow()
    body, _ = api.get(index_path(ruleset))
    houses = parse_index(body)
    chosen = select_houses(houses, house_ids if house_ids is not None else settings['house_ids'],
                           settings['include_alliance'])
    with db:
        upsert_houses(db, ruleset, houses, fetched_at)
    explicit = bool(house_ids or settings['house_ids'])
    if explicit:
        label = '설정한 경매장'
    elif settings['include_alliance']:
        label = '호드+검은바다+얼라이언스'
    else:
        label = '호드+검은바다, 얼라이언스 제외'
    print(f'색인 {len(houses)}개, 수집 {len(chosen)}개 ({label}).', flush=True)
    failures = []
    deadline = time.monotonic() + COLLECT_DEADLINE
    for index, house in enumerate(chosen):
        if time.monotonic() >= deadline:
            for skipped in chosen[index:]:
                print(f'경매장 {skipped["id"]}: 실행 시간 한도 초과로 이번 주기에서 생략', flush=True)
            break
        try:
            collect_house(db, api, ruleset, house, fetched_at)
        except AppError as exc:
            failures.append(house['id'])
            print(f'경매장 {house["id"]}: 오류 {type(exc).__name__}', flush=True)
    if failures:
        raise AppError(f'수집 실패 경매장 {failures}; 성공한 스냅샷은 보존했습니다.')
    return chosen


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def item_sell_through(db, house_id, ruleset, item_id):
    if not _table(db, 'blizzard_snapshots'):
        return sell_through_estimate(0, 0, 0, 0)
    observed = db.execute('''SELECT s.fetched_at FROM blizzard_item_prices p
        JOIN blizzard_snapshots s ON s.id=p.snapshot_id
        WHERE s.house_id=? AND s.ruleset=? AND p.item_id=?''', (house_id, ruleset, item_id)).fetchall()
    days = {parse_time(row['fetched_at']).date().isoformat() for row in observed}
    outcome = db.execute('''
        WITH ordered AS (
          SELECT id, fetched_epoch,
                 LAG(id) OVER (ORDER BY fetched_at, id) AS prev_id,
                 LAG(fetched_epoch) OVER (ORDER BY fetched_at, id) AS prev_epoch
          FROM blizzard_snapshots WHERE house_id=? AND ruleset=?
        )
        SELECT
          SUM(CASE WHEN b.listing_id IS NOT NULL THEN 1 ELSE 0 END) AS survived,
          SUM(CASE WHEN b.listing_id IS NULL AND (o.fetched_epoch - o.prev_epoch) > 0 AND (
                (a.time_left='MEDIUM' AND (o.fetched_epoch - o.prev_epoch) < 1800) OR
                (a.time_left='LONG' AND (o.fetched_epoch - o.prev_epoch) < 7200) OR
                (a.time_left='VERY_LONG' AND (o.fetched_epoch - o.prev_epoch) < 43200)
              ) THEN 1 ELSE 0 END) AS disappeared
        FROM ordered o
        JOIN blizzard_listings a ON a.snapshot_id=o.prev_id AND a.item_id=?
        LEFT JOIN blizzard_listings b ON b.snapshot_id=o.id AND b.kind=a.kind AND b.listing_id=a.listing_id
        WHERE o.prev_id IS NOT NULL''', (house_id, ruleset, item_id)).fetchone()
    disappeared = int(outcome['disappeared'] or 0)
    survived = int(outcome['survived'] or 0)
    return sell_through_estimate(disappeared, survived, len(observed), len(days))


def _outcome_map(db, ruleset):
    rows = db.execute('''
        WITH ordered AS (
          SELECT id, house_id, fetched_epoch,
                 LAG(id) OVER (PARTITION BY house_id ORDER BY fetched_at, id) AS prev_id,
                 LAG(fetched_epoch) OVER (PARTITION BY house_id ORDER BY fetched_at, id) AS prev_epoch
          FROM blizzard_snapshots WHERE ruleset=?
        )
        SELECT o.house_id, a.item_id,
          SUM(CASE WHEN b.listing_id IS NOT NULL THEN 1 ELSE 0 END) AS survived,
          SUM(CASE WHEN b.listing_id IS NULL AND (o.fetched_epoch - o.prev_epoch) > 0 AND (
                (a.time_left='MEDIUM' AND (o.fetched_epoch - o.prev_epoch) < 1800) OR
                (a.time_left='LONG' AND (o.fetched_epoch - o.prev_epoch) < 7200) OR
                (a.time_left='VERY_LONG' AND (o.fetched_epoch - o.prev_epoch) < 43200)
              ) THEN 1 ELSE 0 END) AS disappeared
        FROM ordered o
        JOIN blizzard_listings a ON a.snapshot_id=o.prev_id
        LEFT JOIN blizzard_listings b ON b.snapshot_id=o.id AND b.kind=a.kind AND b.listing_id=a.listing_id
        WHERE o.prev_id IS NOT NULL
        GROUP BY o.house_id, a.item_id''', (ruleset,))
    return {(row['house_id'], row['item_id']): (int(row['disappeared'] or 0), int(row['survived'] or 0))
            for row in rows}


def match_houses(db, auctionator_realm_id, ruleset=1):
    if not _table(db, 'blizzard_snapshots'):
        return match_price_maps({}, [])
    auctionator = {}
    if auctionator_realm_id is not None:
        auctionator = {row['item_id']: row['min_price'] for row in db.execute(
            '''SELECT item_id, min_price FROM latest_prices
               WHERE source='auctionator' AND realm_id=? AND min_price>0''', (int(auctionator_realm_id),))}
    grouped = defaultdict(dict)
    meta = {}
    for row in db.execute('''
            WITH ranked AS (
              SELECT s.id, s.house_id, h.name, h.side,
                     ROW_NUMBER() OVER (PARTITION BY s.house_id ORDER BY s.fetched_at DESC, s.id DESC) AS rank
              FROM blizzard_snapshots s JOIN blizzard_houses h
                ON h.house_id=s.house_id AND h.ruleset=s.ruleset
              WHERE s.ruleset=?)
            SELECT r.house_id, r.name, r.side, p.item_id, p.min_unit_price
            FROM ranked r JOIN blizzard_item_prices p ON p.snapshot_id=r.id
            WHERE r.rank=1''', (ruleset,)):
        grouped[row['house_id']][row['item_id']] = row['min_unit_price']
        meta[row['house_id']] = {'name': row['name'], 'side': row['side']}
    houses = [{'house_id': house_id, 'prices': prices, **meta[house_id]} for house_id, prices in grouped.items()]
    return match_price_maps(auctionator, houses)


def attach_blizzard(db, cfg, items, now, auctionator_realm_id):
    """Attach per-house evidence; market_quotes chooses a confirmed market for calculations."""
    from action_planner import liquidity, market_settings
    settings = forever_settings(cfg)
    selected = selected_house_id(cfg)
    tuned = market_settings(cfg)
    summary = {'selected_house_id': selected, 'ruleset': settings['ruleset'],
               'market_confirmed': settings['market_confirmed'], 'latest_houses': [],
               'match': {'status': '데이터 부족', 'house_id': None, 'note': '블리자드 스냅샷이 없습니다.',
                         'candidates': [], 'tied_house_ids': []},
               'assumptions': ('단가는 즉시구매를 수량으로 나눈 내림, 또는 재료 unit_price입니다. '
                               '입찰만 있는 매물은 빠집니다. 시간대는 설정 시간대(기본 한국 시간)입니다. '
                               '관측일과 최신 가격 나이는 오늘 할 일과 같은 기준입니다. '
                               '소멸 건수는 판매 또는 취소의 추정입니다.')}
    for item in items.values():
        item['blizzard'] = {'houses': [], 'selected_house_id': selected}
    if not _table(db, 'blizzard_snapshots'):
        summary['match']['note'] = '블리자드 스냅샷 테이블이 없습니다.'
        return summary
    summary['match'] = match_houses(db, auctionator_realm_id, settings['ruleset'])
    summary['latest_houses'] = [dict(row) for row in db.execute('''
        WITH ranked AS (
          SELECT s.*, h.name, h.side, ROW_NUMBER() OVER
            (PARTITION BY s.house_id ORDER BY s.fetched_at DESC,s.id DESC) AS rank
          FROM blizzard_snapshots s JOIN blizzard_houses h
            ON h.house_id=s.house_id AND h.ruleset=s.ruleset WHERE s.ruleset=?)
        SELECT * FROM ranked WHERE rank=1''', (settings['ruleset'],))]
    house_latest = {row['house_id']: row for row in summary['latest_houses']}
    series = defaultdict(lambda: defaultdict(list))
    latest = {}
    names = {}
    for row in db.execute('''SELECT h.house_id, h.name, h.side, s.id AS snapshot_id, s.fetched_at,
            s.auctions_last_modified,s.commodities_last_modified, p.item_id, p.min_unit_price,
            p.median_unit_price, p.p25_unit_price, p.quantity, p.listing_count
            FROM blizzard_item_prices p
            JOIN blizzard_snapshots s ON s.id=p.snapshot_id
            JOIN blizzard_houses h ON h.house_id=s.house_id AND h.ruleset=s.ruleset
            WHERE s.ruleset=? ORDER BY s.fetched_at, s.id''', (settings['ruleset'],)):
        names[row['house_id']] = {'name': row['name'], 'side': row['side']}
        point = {'observed': observation_time(row), 'price': row['min_unit_price'],
                 'median': row['median_unit_price'], 'quantity': row['quantity'],
                 'snapshot': row['snapshot_id'], 'imported': row['fetched_at']}
        series[row['item_id']][row['house_id']].append(point)
        latest[(row['item_id'], row['house_id'])] = row
    outcomes = _outcome_map(db, settings['ruleset']) if latest else {}
    for item_id, houses in series.items():
        item = items.get(item_id)
        if item is None:
            continue
        blocks = []
        for house_id, points in houses.items():
            row = latest[(item_id, house_id)]
            present = row['snapshot_id'] == house_latest[house_id]['id']
            observed = observation_time(row)
            pseudo = {'price': row['min_unit_price'] if present else None, 'observed': observed,
                      'history': [{'observed': point['observed'], 'price': point['price']} for point in points]}
            guard = liquidity(pseudo, now, tuned)
            disappeared, survived = outcomes.get((house_id, item_id), (0, 0))
            days = {parse_time(point['observed']).date().isoformat() for point in points}
            estimate = sell_through_estimate(disappeared, survived, len(points), len(days))
            blocks.append({'house_id': house_id, 'name': names[house_id]['name'],
                           'snapshot_id': row['snapshot_id'], 'present_latest': present, 'history': points,
                           'side': names[house_id]['side'], 'side_label': SIDE_LABEL.get(names[house_id]['side'], '미확인'),
                           'min_unit_price': row['min_unit_price'] if present else None,
                           'median_unit_price': row['median_unit_price'] if present else None,
                           'p25_unit_price': row['p25_unit_price'] if present else None,
                           'quantity': row['quantity'] if present else 0,
                           'listing_count': row['listing_count'] if present else 0, 'observed': observed,
                           'distinct_days': guard['distinct_days'], 'price_age_seconds': guard['price_age_seconds'],
                           'liquidity': guard['status'], 'liquidity_reason': guard['reason'],
                           'hourly': hourly_pattern([(point['observed'], point['price']) for point in points],
                                                    settings['offset_hours']),
                           'disappearance_pct': estimate['disappearance_pct'],
                           'disappearance_status': estimate['disappearance_status'],
                           'disappearance_note': estimate['note'],
                           'disappeared': disappeared, 'survived': survived, 'classified': estimate['classified']})
        blocks.sort(key=lambda block: block['house_id'])
        item['blizzard'] = {'houses': blocks, 'selected_house_id': selected}
    return summary


def observation_time(row):
    """Use the older endpoint timestamp; reusing a 304 must not refresh old quotes."""
    times = [parse_time(row['fetched_at'])]
    for key in ('auctions_last_modified', 'commodities_last_modified'):
        if row[key]:
            try:
                value = parsedate_to_datetime(row[key])
                if value.tzinfo is not None:
                    times.append(value)
            except (TypeError, ValueError, OverflowError):
                pass
    return min(times).isoformat()


def summary_lines(db, cfg, auctionator_realm_id):
    settings = forever_settings(cfg)
    lines = ['', '## 블리자드 Forever 경매장', '',
             '내 시장으로 확인한 공식 스냅샷만 계산에 사용합니다. 매물 소멸은 판매 확률로 사용하지 않습니다.',
             '기본 수집은 색인의 호드와 검은바다입니다. 얼라이언스는 설정하기 전에는 건너뜁니다.',
             f'네임스페이스 {settings["namespace"]}, 룰셋 {settings["ruleset"]}, 지역 {settings["region"]}.']
    if not _table(db, 'blizzard_snapshots'):
        lines.append('데이터 부족: 스냅샷 테이블이 없습니다.')
        return lines
    snaps = db.execute('SELECT COUNT(*) FROM blizzard_snapshots WHERE ruleset=?', (settings['ruleset'],)).fetchone()[0]
    checks = db.execute('SELECT COUNT(*) FROM blizzard_checks WHERE ruleset=?', (settings['ruleset'],)).fetchone()[0]
    skipped = db.execute(
        "SELECT COUNT(*) FROM blizzard_checks WHERE ruleset=? AND stored=0 AND note=?",
        (settings['ruleset'], 'Last-Modified 동일')).fetchone()[0]
    unpublished = db.execute(
        "SELECT COUNT(*) FROM blizzard_checks WHERE ruleset=? AND stored=0 AND note=?",
        (settings['ruleset'], '매물 목록 없음')).fetchone()[0]
    if snaps == 0:
        lines.append('저장한 스냅샷이 없습니다. 데이터 부족.')
    else:
        lines.append(f'스냅샷 {snaps}개, 확인 {checks}회, Last-Modified가 같아 생략한 확인 {skipped}회.')
    if unpublished:
        lines.append(f'매물 목록이 없어 스냅샷을 생략한 확인 {unpublished}회. 빈 시장으로 저장하지 않았습니다.')
    matched = match_houses(db, auctionator_realm_id, settings['ruleset'])
    lines.append(matched['note'])
    if matched.get('house_id'):
        lines.append(f"추정 경매장 {matched['house_id']}, 중앙 상대오차 {matched['median_relative_error_bp']}bp, "
                     f"겹친 아이템 {matched['shared_items']}개. 확정이 아닙니다.")
    selected = selected_house_id(cfg)
    lines.append('계산용 공식 시장 확인: ' + (str(settings['house_id']) if settings['market_confirmed'] else '미확인. Auctionator 관측 유지.') + ' · 소멸률은 활동 참고 전용입니다.')
    lines.append(f'소멸 추정 기준: 스냅샷 {MIN_SELL_SNAPSHOTS}개, 서로 다른 날 {MIN_SELL_DISTINCT_DAYS}일, '
                 f'판정 매물 {MIN_SELL_CLASSIFIED}건. 시간대 기준: 스냅샷 {MIN_HOURLY_SNAPSHOTS}개, '
                 f'{MIN_HOURLY_DAYS}일, 시각 {MIN_HOURLY_DISTINCT_HOURS}개. 모자라면 데이터 부족.')
    return lines


def _db_override(cfg, args_db):
    if args_db:
        return args_db
    path = forever_settings(cfg)['db_path']
    if not path:
        return None
    candidate = Path(path)
    return str(candidate if candidate.is_absolute() else cfg['base_dir'] / candidate)


def _house_override(text):
    if not text:
        return None
    ids = []
    for part in text.split(','):
        part = part.strip()
        if not part.isdigit() or int(part) <= 0:
            raise AppError('경매장 ID는 양의 정수 목록입니다.')
        ids.append(int(part))
    return ids


def main(argv=None):
    tool = parser(__doc__, fixture=True)
    tool.add_argument('--env', help='BNET_CLIENT_ID / BNET_CLIENT_SECRET 파일. 값은 출력하지 않습니다.')
    tool.add_argument('--match', action='store_true', help='저장된 스냅샷과 Auctionator 최저가를 비교합니다. 네트워크 없음.')
    tool.add_argument('--houses', help='이번 실행만 수집할 경매장 ID. 쉼표로 구분. 색인에 있어야 합니다.')
    args = tool.parse_args(argv)
    cfg = load_config(args.config)
    if cfg.get('game_flavor', 'retail') != 'forever':
        raise AppError('블리자드 Forever 수집은 game_flavor=forever 설정에서만 실행합니다. 리테일 DB는 변경하지 않습니다.')
    if args.fixture and not args.db:
        raise AppError('샘플 실행에는 별도 --db가 필요합니다.')
    db = connect(cfg, _db_override(cfg, args.db), 'fixture' if args.fixture else 'live')
    try:
        if args.match:
            realm = get_meta(db, 'auctionator_local_realm_id')
            matched = match_houses(db, int(realm) if realm is not None else None, forever_settings(cfg)['ruleset'])
            print(matched['note'])
            if matched.get('house_id'):
                print(f"추정 경매장 {matched['house_id']} · 겹친 아이템 {matched['shared_items']} · "
                      f"중앙 상대오차 {matched['median_relative_error_bp']}bp · 추정")
            else:
                print('추정 경매장 없음.')
            return
        with run(db, 'blizzard_collector'):
            api = ForeverAuctions(cfg, args.fixture, args.env)
            collect_once(db, api, cfg, _house_override(args.houses))
    finally:
        db.close()


if __name__ == '__main__':
    main_guard(main)
