"""Pure price-observation fingerprints and text; no files, credentials, or network."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from auction_house_display import house_label


def render_summary(report):
    """Plain text only; generation never attempts or acknowledges a delivery."""
    def gold(copper):
        return f'{copper // 10000}.{copper % 10000:04d}g'

    zone = timezone(timedelta(hours=report['timezone_offset_hours']))
    thresholds = report['thresholds']
    names = report.get('names_status', {'status': 'missing'})
    if not isinstance(names, dict):
        names = {'status': str(names)}
    if report.get('mode') == 'fixture':
        origin = '[합성 테스트 데이터]'
    elif report.get('mode') == 'live':
        origin = '[실제 수집 데이터]'
    else:
        origin = '[출처 미확인 데이터]'
    lines = [origin, 'WoW Forever KR 상품 시세 관측 (거래 권고 아님)',
             f"신선도 기준 {thresholds['max_age_hours']:g}시간 · 관측 창 {thresholds['window_hours']:g}시간",
             f"이름 캐시: {names.get('status', 'missing')} (표시 정보만)"]
    for house in report['houses'][:8]:
        epoch = house.get('source_epoch')
        stamp = '미확인'
        if epoch is not None:
            try:
                stamp = datetime.fromtimestamp(epoch, zone).isoformat(timespec='minutes')
            except (OSError, OverflowError, ValueError, TypeError):
                pass
        reason = ','.join(house['reasons']) or house['status']
        lines.append(f"시장 {house_label(house['house_id'])} · 원본 {stamp} · {reason}")
    if len(report['houses']) > 8:
        lines.append(f"추가 시장 {len(report['houses']) - 8}개는 HTML 보고서에 표시합니다.")
    lines.append('반복 관측 표시는 독립 원본 갱신의 근거이며 체결 정보가 아닙니다.')
    if not report['comparisons']:
        lines.append('현재 조건을 통과한 가격차 없음. 시장 상태·시각차를 확인하세요.')
    for row in report['comparisons'][:8]:
        low, high = row['source_quote'], row['target_quote']
        name = row.get('name_ko') or f"item#{row['item_id']}"
        status = '반복 관측' if row['status'] == 'repeated_gap' else '일반 관측'
        line = (f"{name} (item#{row['item_id']}) · {status}\n"
                f"{house_label(row['lower_quote_house'])} {gold(low['min_unit_price'])} · 최저가 수량 {low['qty_at_min']} / 총 {low['total_qty']}\n"
                f"{house_label(row['higher_quote_house'])} {gold(high['min_unit_price'])} · 최저가 수량 {high['qty_at_min']} / 총 {high['total_qty']}\n"
                f"개당 원시 호가차 {gold(row['gap_copper'])}")
        if len('\n\n'.join(lines + [line])) > 3500:
            break
        lines.append(line)
    return '\n\n'.join(lines)


def notification_fingerprint(report):
    """Only the top thirty price observations, identity and acceptance affect chat."""
    rows = []
    fields = ('item_id', 'lower_quote_house', 'higher_quote_house', 'source_quote',
              'target_quote', 'gap_copper', 'status')
    quote_fields = ('min_unit_price', 'qty_at_min', 'total_qty', 'listing_count')
    for row in report['comparisons'][:30]:
        projected = {key: row[key] for key in fields}
        for key in ('source_quote', 'target_quote'):
            projected[key] = {field: row[key][field] for field in quote_fields}
        rows.append(projected)
    pairs = sorted((dict(house_ids=sorted(pair['house_ids']), status=pair['status'],
                         reasons=sorted(pair['reasons'])) for pair in report['pairs']),
                   key=lambda pair: pair['house_ids'])
    thresholds = {key: report['thresholds'][key] for key in
                  ('window_hours', 'min_pairs', 'max_age_hours', 'max_skew_minutes')}
    payload = dict(fingerprint_schema_version=2, mode=report.get('mode', 'unknown'),
                   game_flavor=report['game_flavor'],
                   region=report['region'], ruleset=report['ruleset'],
                   house_ids=sorted(house['house_id'] for house in report['houses']),
                   thresholds=thresholds, pairs=pairs, rows=rows)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()
