"""Daily min-price ranges and maximum availability, never trade/sales estimates."""
from datetime import datetime, timedelta

from common import AppError


def attach_history(db, realm_id, items, now):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auctionator_daily'").fetchone() is None:
        raise AppError('일별 이력 테이블이 없습니다. import_auctionator.py를 다시 실행하세요.')
    for item in items.values():
        item['daily_history'] = []
    latest = db.execute('''SELECT id,fetched_at,snapshot_id,latest_count,daily_count,skipped_count
        FROM auctionator_imports WHERE realm_id=? ORDER BY fetched_at DESC,id DESC LIMIT 1''',
        (realm_id,)).fetchone()
    rows = db.execute('''WITH ranked AS (
        SELECT d.*,f.fetched_at,f.snapshot_id,ROW_NUMBER() OVER (
          PARTITION BY d.item_id,d.scan_day ORDER BY f.fetched_at DESC,f.id DESC) rank
        FROM auctionator_daily d JOIN auctionator_imports f ON f.id=d.import_id WHERE f.realm_id=?
    ) SELECT * FROM ranked WHERE rank=1 ORDER BY item_id,scan_day''', (realm_id,))
    count = 0
    for r in rows:
        item = items.get(r['item_id'])
        if item is None:
            continue
        item['daily_history'].append(dict(day=r['scan_day'], observed=r['observed_at'],
            low=r['low_price'], high=r['high_price'], max_quantity=r['max_quantity'],
            import_id=r['import_id'], imported=r['fetched_at'], snapshot=r['snapshot_id']))
        count += 1
    for i in items.values():
        i['ranges'] = {}
        for days in (7, 30):
            history = [p for p in i['daily_history'] if
                       now-timedelta(days=days) <= datetime.fromisoformat(p['observed']) <= now]
            lows = [p['low'] for p in history if p['low'] is not None]
            highs = [p['high'] for p in history if p['high'] is not None]
            i['ranges'][str(days)] = dict(days=len(history), low=min(lows) if lows else None,
                high=max(highs) if highs else None, import_ids=sorted({p['import_id'] for p in history}))
    return dict(latest_import=dict(latest) if latest else None, daily_rows=count,
                daily_items=sum(bool(i['daily_history']) for i in items.values()))
