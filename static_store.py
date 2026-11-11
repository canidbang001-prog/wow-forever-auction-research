"""Static evidence storage and migration. Unknown is NULL, never zero/empty proof."""
import hashlib
import json
from pathlib import Path

VENDORS = """CREATE TABLE vendor_items (
 offer_id INTEGER PRIMARY KEY, offer_key TEXT NOT NULL UNIQUE,
 item_id INTEGER NOT NULL REFERENCES items(item_id),
 vendor_price INTEGER CHECK(vendor_price>=0), source TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 npc_id INTEGER, npc_name TEXT, zone TEXT, map_id INTEGER, x REAL, y REAL,
 price_copper INTEGER CHECK(price_copper>=0), gold INTEGER, silver INTEGER, copper INTEGER,
 purchase_qty INTEGER CHECK(purchase_qty>0), price_basis TEXT NOT NULL DEFAULT 'unknown',
 stock_state TEXT NOT NULL DEFAULT 'unknown', stock_quantity INTEGER,
 currency_costs TEXT, reputation_required TEXT, reputation_discount_faction TEXT, faction TEXT,
 conditions TEXT, source_url TEXT, document_id INTEGER REFERENCES static_documents(id),
 verification TEXT NOT NULL DEFAULT 'unverified', active INTEGER NOT NULL DEFAULT 1
)"""


def migrate(db):
    schema=Path(__file__).with_name('static_schema.sql').read_text(encoding='utf-8')
    db.executescript(schema.split('CREATE TABLE IF NOT EXISTS static_queue')[0])
    columns = {r[1] for r in db.execute('PRAGMA table_info(vendor_items)')}
    if 'offer_id' not in columns:
        # Retain all old rows; one old candidate becomes one offer, without inventing an NPC.
        with db:
            db.execute('ALTER TABLE vendor_items RENAME TO vendor_items_v1')
            db.execute(VENDORS)
            db.execute("""INSERT INTO vendor_items(offer_key,item_id,vendor_price,source,note,price_basis)
                SELECT 'legacy:'||item_id,item_id,vendor_price,source,note,'legacy_unit_candidate'
                FROM vendor_items_v1""")
            db.execute('DROP TABLE vendor_items_v1')
    db.executescript(schema)
    columns={r[1] for r in db.execute('PRAGMA table_info(vendor_items)')}
    for name in ('retrieved_at','game_version','game_build_override'):
        if name not in columns:
            db.execute(f'ALTER TABLE vendor_items ADD COLUMN {name} TEXT')


def enqueue(db, url, provider, kind, entity_id=None, priority=50):
    db.execute('''INSERT INTO static_queue(url,provider,kind,entity_id,priority) VALUES (?,?,?,?,?)
        ON CONFLICT(url) DO UPDATE SET priority=MIN(priority,excluded.priority)''',
        (url,provider,kind,entity_id,priority))


def seed(db, item_id, reason, source_url, priority=50):
    from common import get_meta, utcnow
    if not isinstance(item_id,int) or item_id<=0:
        raise ValueError('invalid item ID')
    db.execute('INSERT OR IGNORE INTO items(item_id) VALUES (?)',(item_id,))
    db.execute('INSERT OR IGNORE INTO item_seeds VALUES (?,?,?,?)',(item_id,reason,source_url,utcnow()))
    enqueue(db,f'https://www.wowhead.com/forever/ko/item={item_id}&xml','wowhead','item_xml',item_id,priority)
    enqueue(db,f'https://foreverdb.net/item/{item_id}','foreverdb','item',item_id,priority+1)
    if get_meta(db,'forever_api_enabled'):
        enqueue(db,f'https://foreverdb.net/api/v1/items/{item_id}','foreverdb_api','api_item',item_id,80)


def fact(db, doc, kind, subject_type, subject_id, object_type=None, object_id=None,
         label=None, quantity=None, verification='unresolved', **details):
    payload=json.dumps(details,ensure_ascii=False,sort_keys=True)
    values=(kind,subject_type,str(subject_id),object_type,object_id,label,quantity,verification,payload)
    key=hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()
    db.execute('''INSERT OR IGNORE INTO static_facts(document_id,kind,subject_type,subject_id,
        object_type,object_id,label,quantity,verification,details,fact_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (doc,*values,key))


def coverage(db,url,kind,status,observed=0,expected=None,note=''):
    from common import utcnow
    db.execute('''INSERT INTO static_coverage VALUES (?,?,?,?,?,?,?) ON CONFLICT(url,relation_kind)
        DO UPDATE SET status=excluded.status,expected=excluded.expected,observed=excluded.observed,
        note=excluded.note,checked_at=excluded.checked_at''',(url,kind,status,expected,observed,note,utcnow()))


def offer(db, item_id, source, key, **values):
    from common import utcnow
    values.setdefault('retrieved_at',utcnow())
    values.setdefault('game_version','forever')
    values=dict(offer_key=key,item_id=item_id,source=source,**values)
    columns=','.join(values)
    updates=','.join(f'{k}=excluded.{k}' for k in values if k!='offer_key')
    db.execute(f'INSERT INTO vendor_items({columns}) VALUES ({",".join("?" for _ in values)}) '
               f'ON CONFLICT(offer_key) DO UPDATE SET {updates}',tuple(values.values()))


def summary(db):
    lines=['Data from ForeverDB.net (https://foreverdb.net)', '정적 정보 = 사실/관계 관측 건수 (고유 아이템 수와 다름)']
    for r in db.execute('''SELECT provider,kind,verification,COUNT(*) n FROM current_static_facts
                           GROUP BY provider,kind,verification ORDER BY provider,kind,verification'''):
        lines.append(f"{r['provider']} / {r['kind']} / {r['verification']}: {r['n']}")
    for r in db.execute('SELECT source,verification,COUNT(*) n FROM current_vendor_offers GROUP BY source,verification'):
        lines.append(f"{r['source']} / vendor_offer / {r['verification']}: {r['n']}")
    for r in db.execute('SELECT state,COUNT(*) n FROM static_queue GROUP BY state'):
        lines.append(f"큐 {r['state']}: {r['n']}")
    for r in db.execute('SELECT relation_kind,status,COUNT(*) n FROM static_coverage GROUP BY relation_kind,status'):
        lines.append(f"범위 {r['relation_kind']} / {r['status']}: {r['n']}페이지")
    for collection, in db.execute('SELECT DISTINCT collection FROM static_api_pages ORDER BY collection'):
        pages=db.execute('SELECT p.*,d.game_build FROM static_api_pages p JOIN static_documents d ON d.id=p.document_id '
                         'WHERE collection=? AND d.active=1 ORDER BY page_offset',(collection,)).fetchall()
        count=0; valid=True
        for page in pages:
            valid &= page['page_offset']==count and page['total']==pages[0]['total'] and page['game_build']==pages[0]['game_build']
            count+=page['observed']
        total=pages[0]['total'] if pages else 0
        state='complete' if pages and valid and count==total else 'partial'
        lines.append(f'API 목록 {collection}: {count}/{total} ({state})')
    return '\n'.join(lines)
