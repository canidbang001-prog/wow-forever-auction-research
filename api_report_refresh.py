"""Offline, read-only hourly reports beside (never replacing) the collector."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from api_market_observer import build_observations
from api_market_search import render_searchable_html
from api_report_summary import notification_fingerprint, render_summary
from common import AppError, ROOT, load_config, main_guard, parser
from forever_item_cache import load_item_cache


ARTIFACTS = dict(json='report-latest.json', html='report-latest.html',
                 summary='report-latest-summary.txt', status='report-status.json')


def validate_report_paths(output_root, protected_paths=()):
    """Canonical paths only. Canonical ancestors are trusted and must stay stable."""
    root = Path(output_root).resolve()
    if root.exists() and not root.is_dir():
        raise AppError('보고서 루트는 디렉터리여야 합니다.')
    if any((ancestor / '.git').exists() for ancestor in (root, *root.parents)):
        raise AppError('보고서는 Git 작업 트리 밖에 저장해야 합니다.')
    protected = [Path(path).resolve() for path in protected_paths if path is not None]
    names = list(ARTIFACTS.values()) + ['delivery-ledger.json', 'delivery.lock']
    checked = []
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise AppError('보고서 출력이 지정 루트를 벗어납니다.')
        for other in protected + checked:
            if path == other or (path.exists() and other.exists() and path.samefile(other)):
                raise AppError('보고서 출력이 입력 또는 다른 산출물과 겹칩니다.')
        checked.append(path)
    return root, {kind: checked[index] for index, kind in enumerate(ARTIFACTS)}


def atomic_write_text(path, text):
    """Replace a directory entry, never truncate a pre-existing hardlink target."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=f'.{path.name}.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_report(db_path, cfg, output_root, *, config_path=None, cache_path=None,
                   now=None, house_ids=None, limit=500, **thresholds):
    """Write JSON/HTML/text/status offline. Delivery has a separate entry point."""
    now = now or datetime.now(timezone.utc)
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise AppError('기존 관측 DB가 없습니다. 새 DB를 생성하지 않습니다.')
    protected = [db_path, config_path, cache_path, ROOT / 'config.json',
                 ROOT / 'config.example.json', ROOT / 'config.forever.example.json']
    protected.extend(Path(str(db_path) + suffix) for suffix in ('-wal', '-shm', '-journal'))
    root, paths = validate_report_paths(output_root, protected)
    cache = dict(status='missing', reason='cache_not_supplied', items={}, named_count=0,
                 rejected_count=0, old=False)
    if cache_path is not None:
        try:
            cache = load_item_cache(cache_path, now=now)
        except Exception:
            cache = dict(status='unreadable', reason='cache_load_error', items={},
                         named_count=0, rejected_count=0, old=False)
    names_status = {key: cache.get(key) for key in
                    ('status', 'reason', 'named_count', 'rejected_count', 'old')}
    db = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        report = build_observations(db, cfg, now=now, house_ids=house_ids, limit=limit,
                                    item_names=cache['items'], **thresholds)
    finally:
        db.close()
    report['names_status'] = names_status
    fingerprint = notification_fingerprint(report)
    summary = render_summary(report)
    status = dict(schema_version=1, generated_at=report['generated_at'], report_status='ok',
                  names_status=names_status, fingerprint=fingerprint,
                  delivery=dict(status='not_requested'))
    payloads = dict(json=json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
                    html=render_searchable_html(report), summary=summary,
                    status=json.dumps(status, ensure_ascii=False, indent=2, allow_nan=False))
    root.mkdir(parents=True, exist_ok=True)
    for kind, text in payloads.items():
        atomic_write_text(paths[kind], text)
    return dict(report=report, summary=summary, fingerprint=fingerprint, status=status,
                paths={kind: str(path) for kind, path in paths.items()})


def main(argv=None):
    p = parser(__doc__)
    p.add_argument('--output-root', required=True, help='Git 밖의 별도 시간별 보고서 디렉터리')
    p.add_argument('--cache', help='검증할 공식 Forever KR 한글 이름 캐시 (선택)')
    p.add_argument('--houses', help='비교할 API 경매장 양의 정수 목록: 113,114')
    p.add_argument('--limit', type=int, default=500)
    p.add_argument('--window-hours', type=float, default=72)
    p.add_argument('--max-age-hours', type=float, default=3)
    p.add_argument('--max-skew-minutes', type=float, default=15)
    p.add_argument('--min-pairs', type=int, default=3)
    args = p.parse_args(argv)
    if not args.db:
        raise AppError('--db로 기존 관측 DB를 명시해야 합니다.')
    houses = None
    if args.houses is not None:
        if not re.fullmatch(r'[1-9]\d*(,[1-9]\d*)*', args.houses):
            raise AppError('--houses는 113,114와 같은 양의 정수 목록입니다.')
        houses = [int(value) for value in args.houses.split(',')]
    cfg = load_config(args.config)
    result = refresh_report(args.db, cfg, args.output_root, config_path=args.config or ROOT / 'config.json',
                            cache_path=args.cache, house_ids=houses, limit=args.limit,
                            window_hours=args.window_hours, max_age_hours=args.max_age_hours,
                            max_skew_minutes=args.max_skew_minutes, min_pairs=args.min_pairs)
    print(json.dumps(dict(report_status='ok', fingerprint=result['fingerprint'], paths=result['paths']),
                     ensure_ascii=False))


if __name__ == '__main__':
    main_guard(main)
