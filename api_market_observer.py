"""Read-only, API-only Forever commodity observations, never trade recommendations."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape
from itertools import combinations
import json
import math
from pathlib import Path
import re
import sqlite3
import tempfile

from blizzard_collector import forever_settings, median_lower, select_houses
from common import AppError, ROOT, config_path, get_meta, load_config, main_guard, parse_time, parser


def snapshot_times(snapshot, now_epoch):
    """Invalid timestamps remain visible, but can never supply safe comparisons."""
    reasons, source_epoch, fetched_epoch = [], None, None
    try:
        raw_epoch = snapshot['fetched_epoch']
        if type(raw_epoch) not in (int, float) or not math.isfinite(raw_epoch):
            raise ValueError('invalid epoch type or non-finite epoch')
        datetime.fromtimestamp(raw_epoch, timezone.utc)
        if int(parse_time(snapshot['fetched_at']).timestamp()) != raw_epoch:
            raise ValueError('epoch mismatch')
        fetched_epoch = raw_epoch
    except (AppError, ValueError, TypeError, AttributeError, OverflowError, OSError):
        reasons.append('invalid_fetch_time')
    if fetched_epoch is not None and fetched_epoch > now_epoch:
        reasons.append('future_fetch')
    try:
        stamp = parsedate_to_datetime(snapshot['commodities_last_modified'])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('naive source timestamp')
        source_epoch = stamp.timestamp()
        if source_epoch > now_epoch:
            reasons.append('future_source')
        if fetched_epoch is not None and source_epoch > fetched_epoch:
            reasons.append('source_after_fetch')
    except (TypeError, ValueError, OverflowError, AttributeError, OSError):
        reasons.append('invalid_source_time')
    return source_epoch, reasons


def commodity_quotes(db, snapshot_id):
    """Recompute exact commodity prices; auction stack prices are not interchangeable."""
    quotes = {}
    rows = db.execute('''SELECT item_id,unit_price,SUM(quantity) qty,COUNT(*) listings
        FROM blizzard_listings WHERE snapshot_id=? AND kind='commodity'
        GROUP BY item_id,unit_price ORDER BY item_id,unit_price''', (snapshot_id,))
    for row in rows:
        if type(row['item_id']) is not int or row['item_id'] <= 0:
            raise AppError('DB commodity item_id는 양의 정수여야 합니다.')
        quote = quotes.setdefault(row['item_id'], dict(min_unit_price=row['unit_price'],
                                  qty_at_min=0, total_qty=0, listing_count=0))
        if row['unit_price'] == quote['min_unit_price']:
            quote['qty_at_min'] += row['qty']
        quote['total_qty'] += row['qty']
        quote['listing_count'] += row['listings']
    return quotes


def load_history(db, house_id, ruleset, now_epoch, window_hours):
    cutoff = now_epoch - window_hours * 3600
    rows = db.execute('''SELECT * FROM blizzard_snapshots WHERE house_id=? AND ruleset=?
        AND fetched_epoch BETWEEN ? AND ? ORDER BY fetched_epoch,id''',
                      (house_id, ruleset, cutoff, now_epoch)).fetchall()
    times = [snapshot_times(row, now_epoch) for row in rows]
    valid_times = [epoch for epoch, reasons in times if not reasons and epoch is not None]
    # Header overlap cannot establish whether omitted quote maps agree with retained ones.
    truncated_lms = ({epoch for epoch, _ in times[:-168] if epoch is not None}
                     & {epoch for epoch, _ in times[-168:] if epoch is not None})
    updates, conflicts, snapshot_ids = {}, set(), {}
    for row, (epoch, reasons) in zip(rows[-168:], times[-168:]):
        if reasons or epoch is None or epoch in truncated_lms:
            continue
        # Same-LM consistency is independent of source-time history eligibility.
        quotes = commodity_quotes(db, row['id'])
        snapshot_ids.setdefault(epoch, []).append(row['id'])
        if epoch in updates and quotes != updates[epoch]['quotes']:
            conflicts.add(epoch)
        updates[epoch] = dict(snapshot_id=row['id'], fetched_epoch=row['fetched_epoch'],
                              source_epoch=epoch, source_time=row['commodities_last_modified'],
                              quotes=quotes)
    for epoch in conflicts:
        updates.pop(epoch, None)
    updates = {epoch: update for epoch, update in updates.items() if epoch >= cutoff}
    total = db.execute('SELECT COUNT(*) FROM blizzard_snapshots WHERE house_id=? AND ruleset=?',
                       (house_id, ruleset)).fetchone()[0]
    return sorted(updates.values(), key=lambda r: (r['source_epoch'], r['snapshot_id'])), dict(
        snapshots_total=total, window_snapshots=len(rows), distinct_updates=len(updates),
        truncated=len(rows) > 168, max_valid_source_epoch=max(valid_times) if valid_times else None,
        lm_history_truncated=sorted(truncated_lms),
        lm_conflicts=[dict(source_epoch=t, snapshot_ids=snapshot_ids[t]) for t in sorted(conflicts)])


def pair_history(left, right, max_skew_seconds):
    """Deterministic one-to-one matching once per canonical undirected house pair."""
    used, pairs = set(), []
    for a in left:
        eligible = [b for b in right if b['snapshot_id'] not in used
                    and abs(a['source_epoch'] - b['source_epoch']) <= max_skew_seconds
                    and abs(a['fetched_epoch'] - b['fetched_epoch']) <= max_skew_seconds]
        if not eligible:
            continue
        b = min(eligible, key=lambda r: (abs(a['source_epoch'] - r['source_epoch']),
                                        abs(a['fetched_epoch'] - r['fetched_epoch']), r['snapshot_id']))
        used.add(b['snapshot_id'])
        pairs.append((a, b))
    return pairs


def item_history(paired, iid, reverse, current_ids, offset_hours, min_days, min_pairs):
    samples, days, streak, positives = [], set(), 0, 0
    for a, b in paired:
        if iid not in a['quotes'] or iid not in b['quotes']:
            streak = 0
            continue
        source, target = (b, a) if reverse else (a, b)
        p, q = source['quotes'][iid]['min_unit_price'], target['quotes'][iid]['min_unit_price']
        positive = q > p
        positives += positive
        streak = streak + 1 if positive else 0
        day = datetime.fromtimestamp(min(a['source_epoch'], b['source_epoch']),
                                     timezone(timedelta(hours=offset_hours))).date().isoformat()
        days.add(day)
        samples.append(dict(source_snapshot_id=source['snapshot_id'], reference_snapshot_id=target['snapshot_id'],
                            source_time=source['source_time'], reference_time=target['source_time'],
                            source_price=p, reference_price=q, gap_copper=q - p))
    current_matched = bool(samples and (samples[-1]['source_snapshot_id'],
                                       samples[-1]['reference_snapshot_id']) == current_ids)
    evidence = dict(paired_observations=len(samples), positive_pairs=positives,
                    consecutive_positive_pairs=streak, current_pair_matched=current_matched,
                    distinct_days=len(days), history_readiness='ready' if len(days) >= min_days else 'insufficient_history')
    trends = {}
    for field in ('source_price', 'reference_price'):
        values = [r[field] for r in samples]
        trends[field] = dict(first=values[0], latest=values[-1], lower_median=median_lower(values),
                            change_bps=(values[-1] - values[0]) * 10000 // values[0]) if values else None
    return evidence, samples[-128:], trends, current_matched and streak >= min_pairs


def build_observations(db, cfg, now=None, house_ids=None, limit=30, max_age_hours=3,
                       max_skew_minutes=15, window_hours=72, min_pairs=3, *,
                       item_names=None, query=None):
    """Keep the legacy schema unless names or query explicitly enables selection.

    item_names contains integer-keyed entries already validated by the cache
    loader. Even {} enables feature mode. names_status is always a dict in this
    mode: provided/missing here; the CLI replaces provided with the loader's
    status envelope minus items. Selection counts refer to comparison rows,
    including multiple house pairs for one item, not unique item IDs.
    """
    now = now or datetime.now(timezone.utc)
    if get_meta(db, 'game_flavor') != 'forever' or cfg.get('game_flavor', 'forever') != 'forever':
        raise AppError('Forever DB 전용입니다. 제품이 확인되지 않은 DB나 리테일 DB는 사용할 수 없습니다.')
    if now.tzinfo is None or now.utcoffset() is None:
        raise AppError('now에는 UTC 오프셋이 필요합니다.')
    for name, value in (('window_hours', window_hours), ('max_age_hours', max_age_hours),
                        ('max_skew_minutes', max_skew_minutes)):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise AppError(name + '는 유한한 양수여야 합니다.')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise AppError('limit은 1~500 정수입니다.')
    if type(min_pairs) is not int or min_pairs < 2:
        raise AppError('min_pairs는 2 이상 정수입니다.')
    min_days = cfg.get('min_distinct_days', 3)
    if type(min_days) is not int or min_days < 1:
        raise AppError('min_distinct_days는 1 이상 정수입니다.')
    if house_ids is not None and (not isinstance(house_ids, list) or not house_ids
                                 or any(type(i) is not int or i <= 0 for i in house_ids)):
        raise AppError('house_ids는 비어 있지 않은 양의 정수 배열입니다.')
    settings = forever_settings(cfg)
    known = [dict(r) for r in db.execute('''SELECT house_id AS id,name,side
        FROM blizzard_houses WHERE ruleset=? ORDER BY house_id''', (settings['ruleset'],))]
    if any(type(house['id']) is not int or house['id'] <= 0 for house in known):
        raise AppError('DB house_id는 양의 정수여야 합니다.')
    selected = select_houses(known, house_ids or settings['house_ids'], settings['include_alliance'])
    selected.sort(key=lambda house: house['id'])
    houses, histories = [], {}
    for house in selected:
        # Select by stable ingestion order before validating any stored timestamps.
        latest = db.execute('''SELECT * FROM blizzard_snapshots WHERE ruleset=? AND house_id=?
            ORDER BY id DESC LIMIT 1''', (settings['ruleset'], house['id'])).fetchone()
        source_epoch, reasons = snapshot_times(latest, now.timestamp()) if latest else (None, ['no_snapshot'])
        fetched_epoch = latest['fetched_epoch'] if latest and 'invalid_fetch_time' not in reasons else None
        if fetched_epoch is not None and now.timestamp() - fetched_epoch > max_age_hours * 3600:
            reasons.append('aged_fetch')
        if source_epoch is not None and now.timestamp() - source_epoch > max_age_hours * 3600:
            reasons.append('aged_source_data')
        history, coverage = load_history(db, house['id'], settings['ruleset'], now.timestamp(), window_hours)
        histories[house['id']] = history
        if any(c['source_epoch'] == source_epoch for c in coverage['lm_conflicts']):
            reasons.append('lm_conflict')
        if source_epoch in coverage['lm_history_truncated']:
            reasons.append('lm_history_truncated')
        if source_epoch is not None and coverage['max_valid_source_epoch'] is not None and source_epoch < coverage['max_valid_source_epoch']:
            reasons.append('source_regressed')
        metadata = dict(latest) if latest else None
        for field, value in list(metadata.items()) if metadata else []:
            if type(value) is float and not math.isfinite(value):
                metadata[field] = dict(type='non_finite_float', value=repr(value))
            elif type(value) is bytes:
                metadata[field] = dict(type='bytes', value=repr(value))
        houses.append(dict(house_id=house['id'], name=house['name'], side=house['side'],
                           access='unverified', latest=metadata,
                           source_epoch=source_epoch, fetched_epoch=fetched_epoch,
                           status='blocked' if reasons else 'available', reasons=reasons,
                           coverage=coverage, quotes=commodity_quotes(db, latest['id']) if latest else {}))
    names = {r['item_id']: r['name'] for r in db.execute('SELECT item_id,name FROM items')}
    comparisons, pairs = [], []
    for left, right in combinations(houses, 2):
        reasons = []
        source_skew = (abs(left['source_epoch'] - right['source_epoch'])
                       if left['source_epoch'] is not None and right['source_epoch'] is not None else None)
        fetch_skew = (abs(left['fetched_epoch'] - right['fetched_epoch'])
                      if left['fetched_epoch'] is not None and right['fetched_epoch'] is not None else None)
        if left['status'] == 'blocked' or right['status'] == 'blocked':
            reasons.append('house_unavailable')
        if source_skew is not None and source_skew > max_skew_minutes * 60:
            reasons.append('source_skew')
        if fetch_skew is not None and fetch_skew > max_skew_minutes * 60:
            reasons.append('fetch_skew')
        pairs.append(dict(house_ids=[left['house_id'], right['house_id']], source_skew_seconds=source_skew,
                          fetch_skew_seconds=fetch_skew, status='blocked' if reasons else 'available', reasons=reasons))
        if reasons:
            continue
        paired = pair_history(histories[left['house_id']], histories[right['house_id']], max_skew_minutes * 60)
        for iid in left['quotes'].keys() & right['quotes'].keys():
            a, b = left['quotes'][iid], right['quotes'][iid]
            if a['min_unit_price'] == b['min_unit_price']:
                continue
            low, high = (left, right) if a['min_unit_price'] < b['min_unit_price'] else (right, left)
            source, target = low['quotes'][iid], high['quotes'][iid]
            gap = target['min_unit_price'] - source['min_unit_price']
            evidence, samples, trends, repeated = item_history(
                paired, iid, low is right, (low['latest']['id'], high['latest']['id']),
                settings['offset_hours'], cfg.get('min_distinct_days', 3), min_pairs)
            comparisons.append(dict(item_id=iid, name=names.get(iid) or f'item#{iid}',
                                    lower_quote_house=low['house_id'], higher_quote_house=high['house_id'],
                                    source_quote=source, target_quote=target, gap_copper=gap,
                                    gap_bps=gap * 10000 // source['min_unit_price'],
                                    evidence=evidence, history=samples, trends=trends,
                                    source_snapshot_id=low['latest']['id'], reference_snapshot_id=high['latest']['id'],
                                    status='repeated_gap' if repeated else 'observation'))
    comparisons.sort(key=lambda r: (-r['gap_copper'], r['item_id'], r['lower_quote_house'], r['higher_quote_house']))
    selected_rows, selection = comparisons[:limit], None
    if item_names is not None or query is not None:
        from api_market_search import select_observations
        selected_rows, selection = select_observations(comparisons, item_names=item_names, query=query, limit=limit)
    result = dict(schema_version=1, generated_at=now.isoformat(), mode=get_meta(db, 'data_mode') or 'unknown',
                game_flavor=get_meta(db, 'game_flavor'), region=settings['region'], ruleset=settings['ruleset'],
                timezone_offset_hours=settings['offset_hours'],
                thresholds=dict(window_hours=window_hours, max_age_hours=max_age_hours,
                                max_skew_minutes=max_skew_minutes, min_pairs=min_pairs,
                                min_distinct_days=cfg.get('min_distinct_days', 3), max_snapshots_per_house=168),
                warnings=['등록 호가 관찰입니다. 실제 판매량·판매 확률·실현 수익이 아닙니다.',
                          '경매장 접근·물품 이동·중립/진영 구분은 미확인입니다. 수수료·보증금·이동 비용 미반영.',
                          'commodity 매물만 비교합니다. 장비·일반 경매 매물과 Auctionator는 혼합하지 않습니다.'],
                candidate_count=len(comparisons), returned_count=len(selected_rows),
                houses=houses, pairs=pairs, comparisons=selected_rows)
    if selection is not None:
        result['selection'] = selection
        result['names_status'] = (dict(status='provided', reason=None) if item_names is not None
                                  else dict(status='missing', reason='cache_not_supplied'))
    return result


def gold(copper):
    return f'{copper // 10000}.{copper % 10000:04d}g'


def render(data):
    """Portable HTML: all external strings escaped, no scripts or remote resources."""
    e = lambda value: escape(str(value), quote=True)
    zone = timezone(timedelta(hours=data['timezone_offset_hours']))

    def local(epoch):
        try:
            if type(epoch) in (int, float) and math.isfinite(epoch):
                return datetime.fromtimestamp(epoch, zone).isoformat(timespec='seconds')
        except (ValueError, TypeError, OverflowError, OSError):
            pass
        return '미확인'

    parts = ['<!doctype html><html lang="ko"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Forever API 시세 관찰</title><style>',
             'body{font:15px/1.6 system-ui,sans-serif;max-width:1200px;margin:32px auto;padding:0 18px;background:#10151e;color:#e4eaf4}',
             'h1,h2,h3{line-height:1.3}small,.muted{color:#a8b6ca}.notice{padding:16px;background:#263040;border-left:4px solid #d6ad55}',
             '.grid{display:flex;gap:12px;flex-wrap:wrap}.card,article{background:#18212e;border:1px solid #334155;border-radius:10px;padding:18px;margin:14px 0}',
             '.card{flex:1;min-width:230px}.ok{color:#7dd3b0}.bad{color:#f0b180}table{border-collapse:collapse;width:100%;font-size:13px}',
             'td,th{text-align:left;border-bottom:1px solid #334155;padding:7px;white-space:nowrap}.scroll{overflow:auto}summary{cursor:pointer}code{color:#bcd9ff}',
             '</style><h1>Forever API 시세 관찰</h1>',
             f'<p class="muted">생성 {e(local(parse_time(data["generated_at"]).timestamp()))} · {e(data["mode"])} · {e(data["region"])} / ruleset {e(data["ruleset"])}</p>',
             '<div class="notice">' + '<br>'.join(e(s) for s in data['warnings']) + '</div>',
             '<h2>시장별 관측 상태</h2><div class="grid">']
    for h in data['houses']:
        coverage, latest = h['coverage'], h['latest']
        messages = ', '.join(h['reasons']) or '시각·신선도 확인'
        if coverage['truncated']:
            messages += ' · history_truncated (일부 이력만 분석)'
        parts.append(f'<section class="card"><h3>#{e(h["house_id"])} {e(h["name"])}</h3>'
                     f'<p>{e(h["side"])} · 접근 미확인<br><span class="{"bad" if h["status"] == "blocked" else "ok"}">{e(h["status"])}</span>: {e(messages)}</p>'
                     f'<p>수집 {e(local(h["fetched_epoch"]) if latest else "없음")}<br>상품 원본 {e(local(h["source_epoch"]))}<br>'
                     f'누적 스냅샷 {coverage["snapshots_total"]} · 창 안 독립 갱신 {coverage["distinct_updates"]}<br>'
                     f'현재 commodity {len(h["quotes"])}종</p></section>')
    parts.append('</div><h2>시장 간 등록 호가 차이</h2>')
    parts.append(f'<p>관측 후보 {data["candidate_count"]}개 중 {data["returned_count"]}개 표시. 개당 가격차 순이며 거래 추천 순위가 아닙니다.</p>')
    if not data['comparisons']:
        parts.append('<p>조건을 통과한 가격차가 없습니다. 아래 시각차·시장 상태를 확인하세요.</p>')
    for c in data['comparisons']:
        a, b, proof = c['source_quote'], c['target_quote'], c['evidence']
        parts.append(f'<article><h3>{e(c["name"])} <small>item#{e(c["item_id"])} · #{e(c["lower_quote_house"])} → #{e(c["higher_quote_house"])}</small></h3>'
                     f'<p><b>{gold(a["min_unit_price"])} → {gold(b["min_unit_price"])}</b> · 개당 원시 호가차 {gold(c["gap_copper"])} ({c["gap_bps"] / 100:.2f}%)</p>'
                     f'<p>낮은 시장: 최저가 수량 {a["qty_at_min"]} / 총 {a["total_qty"]}개 · 높은 시장: 최저가 수량 {b["qty_at_min"]} / 총 {b["total_qty"]}개</p>'
                     f'<p>{e(c["status"])} · 정합 표본 {proof["paired_observations"]}회 / 현재 방향 양수 {proof["positive_pairs"]}회 / 말미 연속 {proof["consecutive_positive_pairs"]}회 · {proof["distinct_days"]}일 · {e(proof["history_readiness"])}</p>')
        parts.append('<details><summary>근거 시계열 보기 — 거래가 아닌 각 스냅샷 최저가</summary>')
        parts.append('<p class="muted">첫 표본→마지막 표본 비교이며 24시간 수익률이 아닙니다. 중앙값도 거래량 가중값이 아닙니다.</p>')
        parts.append('<div class="scroll"><table><tr><th>낮은 시장의 원본 시각</th><th>해당 시점 호가</th><th>높은 시장의 원본 시각</th><th>해당 시점 호가</th><th>스냅샷 ID</th></tr>')
        for point in c['history']:
            parts.append(f'<tr><td>{e(point["source_time"])}</td><td>{gold(point["source_price"])}</td>'
                         f'<td>{e(point["reference_time"])}</td><td>{gold(point["reference_price"])}</td>'
                         f'<td>{e(point["source_snapshot_id"])} / {e(point["reference_snapshot_id"])}</td></tr>')
        parts.append('</table></div></details></article>')
    parts.append('<details><summary>시각 정합 판정·설정</summary><pre>' + e(json.dumps(
        dict(pairs=data['pairs'], thresholds=data['thresholds']), ensure_ascii=False, indent=2)) + '</pre></details></html>')
    return '\n'.join(parts)


def validate_output_path(output, db_path, cfg_path):
    """Return the checked canonical path; its ancestors must remain trusted/stable."""
    output, db_path, cfg_path = Path(output).resolve(), Path(db_path).resolve(), Path(cfg_path).resolve()
    protected = [db_path, cfg_path, ROOT / 'config.example.json', ROOT / 'config.forever.example.json']
    protected.extend(Path(str(db_path) + suffix) for suffix in ('-wal', '-shm', '-journal'))
    for path in protected:
        if output == path or (output.exists() and path.exists() and output.samefile(path)):
            raise AppError('출력 경로가 입력 DB·설정 또는 SQLite 보조 파일과 겹칩니다.')
    return output


def main(argv=None):
    p = parser(__doc__)
    p.add_argument('--houses', help='명시적으로 비교할 경매장 ID: 113,114 (설정은 변경하지 않음)')
    p.add_argument('--format', choices=('json', 'html'), default='json')
    p.add_argument('--output', help='JSON 생략 시 stdout, HTML 생략 시 reports/api-market.html')
    p.add_argument('--window-hours', type=float, default=72)
    p.add_argument('--max-age-hours', type=float, default=3)
    p.add_argument('--max-skew-minutes', type=float, default=15)
    p.add_argument('--min-pairs', type=int, default=3)
    p.add_argument('--limit', type=int, default=30)
    p.add_argument('--query', help='최대 64자: 정확한 아이템 ID 또는 공식 캐시 이름 (출력 제한 전 검색)')
    p.add_argument('--cache', help='검증할 Forever 한국어 이름 캐시 JSON (오프라인 읽기 전용)')
    args = p.parse_args(argv)
    houses = None
    if args.houses is not None:
        if not re.fullmatch(r'[1-9]\d*(,[1-9]\d*)*', args.houses):
            raise AppError('--houses는 113,114와 같은 양의 정수 목록입니다.')
        houses = [int(i) for i in args.houses.split(',')]
    cfg = load_config(args.config)
    settings = forever_settings(cfg)
    override = args.db or settings['db_path']
    path = (Path(override) if override else config_path(cfg, 'db_path'))
    if not path.is_absolute():
        path = cfg['base_dir'] / path
    path = path.resolve()
    if not path.is_file():
        raise AppError('기존 Forever API DB가 없습니다. DB를 생성하거나 마이그레이션하지 않습니다.')
    output = Path(args.output) if args.output else (Path('reports/api-market.html') if args.format == 'html' else None)
    if output is not None:
        output = validate_output_path(output, path, Path(args.config).resolve() if args.config else ROOT / 'config.json')
        if args.cache is not None:
            output = validate_output_path(output, path, args.cache)
    feature_options = {}
    cache = None
    if args.query is not None:
        feature_options['query'] = args.query
    if args.cache is not None:
        from forever_item_cache import load_item_cache
        cache = load_item_cache(args.cache)
        feature_options['item_names'] = cache['items']
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        data = build_observations(db, cfg, house_ids=houses, limit=args.limit,
                                  window_hours=args.window_hours, max_age_hours=args.max_age_hours,
                                  max_skew_minutes=args.max_skew_minutes, min_pairs=args.min_pairs,
                                  **feature_options)
    finally:
        db.close()
    if cache is not None:
        data['names_status'] = {key: value for key, value in cache.items() if key != 'items'}
    if args.format == 'html':
        if feature_options:
            from api_market_search import render_searchable_html
            payload = render_searchable_html(data)
        else:
            payload = render(data)
    else:
        payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    if output is None:
        print(payload)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                             prefix=f'.{output.name}.', suffix='.tmp', delete=False) as report:
                temporary = Path(report.name)
                report.write(payload)
            temporary.replace(output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(f'API 관찰 보고서: {output.resolve()}')


if __name__ == '__main__':
    main_guard(main)
