"""Resumable Forever static collector. Only stdlib HTTP, documented XML and public SSR."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import xml.etree.ElementTree as ET

from common import AppError, connect, get_meta, load_config, load_env, main_guard, parser, run, set_meta, utcnow
from static_parsers import parse_html, parse_xml, parse_wowhead_sold_by
from static_store import coverage, enqueue, seed, summary
from forever_api import initialise_api, parse_api

ROOTS=[('https://foreverdb.net/vendors','foreverdb','vendors'),
       ('https://foreverdb.net/professions','foreverdb','professions'),
       ('https://foreverdb.net/quests','foreverdb','quests'),
       ('https://foreverdb.net/api/v1','foreverdb_api','api_docs')]


def valid_url(url,provider):
    u=urlsplit(url)
    if u.scheme!='https' or u.username or u.password or u.port or u.fragment:
        return False
    if provider=='wowhead':
        return u.hostname=='www.wowhead.com' and u.path.startswith('/forever/')
    return u.hostname=='foreverdb.net' and (u.path.startswith('/api/v1') if provider=='foreverdb_api' else True)


def initialise(db,cfg,force=False,patch=None,api_probe=False):
    for url,provider,kind in ROOTS:
        enqueue(db,url,provider,kind,priority=0)
    # These are inspected, real examples, not invented fixture records.
    for item in cfg.get('static_seed_items',[6328,4592,6289,5482]):
        seed(db,int(item),'configured_seed','config:static_seed_items',2)
    enqueue(db,'https://foreverdb.net/quest/4161-recipe-of-the-kaldorei','foreverdb','quest',4161,3)
    for row in db.execute('SELECT DISTINCT item_id FROM prices').fetchall():
        seed(db,row[0],'auctionator_or_api','local:prices',90)
    for row in db.execute('SELECT item_id FROM vendor_items UNION SELECT item_id FROM reagents '
                          'UNION SELECT crafted_item_id FROM recipes WHERE crafted_item_id IS NOT NULL').fetchall():
        seed(db,row[0],'local_vendor_or_recipe','local:catalogue',40)
    cutoff=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat(timespec='seconds')
    if db.execute("SELECT 1 FROM static_queue WHERE provider='wowhead' AND error='HTTP 403' LIMIT 1").fetchone():
        set_meta(db,'provider_blocked:wowhead',utcnow())
        db.execute("UPDATE static_queue SET state='blocked',error='provider HTTP 403; manual retry required' WHERE provider='wowhead' AND state='pending'")
    db.execute("UPDATE static_queue SET state='pending' WHERE state='done' AND checked_at<?",(cutoff,))
    # A crash before commit leaves pending/running work eligible on the next invocation.
    db.execute("UPDATE static_queue SET state='pending' WHERE state='running'")
    if force or (patch and patch!=get_meta(db,'requested_patch')):
        db.execute("UPDATE static_queue SET state='pending',retry_at=NULL WHERE state!='blocked'")
    if patch: set_meta(db,'requested_patch',patch)
    if api_probe:
        for path in ('meta','items?limit=1','vendors?seen=true&limit=1','professions','professions/cooking?limit=1','quests/4161'):
            enqueue(db,'https://foreverdb.net/api/v1/'+path,'foreverdb_api','api_probe',priority=1)
        if os.environ.get('FOREVERDB_API_KEY'):
            db.execute("UPDATE static_queue SET state='pending' WHERE provider='foreverdb_api' AND state='blocked'")
    if os.environ.get('FOREVERDB_API_KEY'):
        initialise_api(db)
    # Check the source build daily; item payloads still use the seven-day cache.
    daily=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec='seconds')
    db.execute("UPDATE static_queue SET state='pending' WHERE kind IN ('professions','api_meta') AND state='done' AND checked_at<?",(daily,))
    db.commit()


def retry_seconds(value):
    if value and value.isdigit(): return max(1,int(value))
    try:
        return max(1,int((parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()))
    except (TypeError,ValueError):
        return 300


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Validate before any credentials could leave the original host.
        return None


def fetch(job):
    if not valid_url(job['url'],job['provider']):
        raise AppError('수집 URL이 허용된 Forever 공급자 경로가 아닙니다.')
    headers={'User-Agent':'ForeverAuctionResearch/1.0 (Python; cached personal research)'}
    if job['provider']=='foreverdb_api' and job['kind']!='api_docs':
        key=os.environ.get('FOREVERDB_API_KEY')
        if not key: return -1,b'',None
        headers['X-API-Key']=key
    try:
        with build_opener(NoRedirect()).open(Request(job['url'],headers=headers),timeout=30) as response:
            if not valid_url(response.url,job['provider']):
                raise AppError('공급자 밖으로 이동한 응답을 사용하지 않습니다.')
            body=response.read(8*1024*1024+1)
            if len(body)>8*1024*1024: raise AppError('정적 응답 8 MiB 제한 초과')
            return response.status,body,None
    except HTTPError as exc:
        code,retry=exc.code,exc.headers.get('Retry-After')
        exc.close()
        return code,b'',retry
    except (URLError,TimeoutError,OSError):
        return 0,b'',None


def store_response(db,job,body,cache_dir,fetched_at=None):
    digest=hashlib.sha256(body).hexdigest()
    cache_dir.mkdir(parents=True,exist_ok=True)
    target=cache_dir/(digest+'.body')
    if not target.exists():
        target.write_bytes(body)
    now=fetched_at or utcnow()
    db.execute('''INSERT INTO static_documents(url,provider,fetched_at,sha256,cache_path,http_status,verification,active)
        VALUES (?,?,?,?,?,200,'response_observed',0) ON CONFLICT(url,sha256) DO UPDATE SET fetched_at=excluded.fetched_at''',
        (job['url'],job['provider'],now,digest,str(target.resolve())))
    doc=db.execute('SELECT id FROM static_documents WHERE url=? AND sha256=?',(job['url'],digest)).fetchone()[0]
    db.commit() # Keep raw evidence even when normalisation fails.
    try:
        with db:
            text=body.decode('utf-8-sig')
            if job['kind'] in ('api_docs','api_probe'):
                response=json.loads(text)
                if not isinstance(response.get('data'),(list,dict)) or not isinstance(response.get('meta'),dict):
                    raise ValueError('API envelope changed')
                # Actual authenticated payload schema has not been observed. Never guess nested field paths.
                coverage(db,job['url'],'api','documentation' if job['kind']=='api_docs' else 'schema_review_required',
                         note='Raw JSON cached. Protected API normalisation needs an observed, verified payload adapter.')
                build=None
            elif job['provider']=='foreverdb_api':
                build=parse_api(db,job,doc,json.loads(text))
            elif job['kind']=='wowhead_sold_by':
                build=parse_wowhead_sold_by(db,job,doc,text)
            else:
                build=(parse_xml if job['kind']=='item_xml' else parse_html)(db,job,doc,text)
            db.execute("DELETE FROM static_coverage WHERE url=? AND relation_kind=? AND status IN ('retry','blocked','missing','parse_error')",
                       (job['url'],job['kind']))
            db.execute('UPDATE static_documents SET active=0 WHERE url=?',(job['url'],))
            db.execute('UPDATE static_documents SET active=1,game_build=? WHERE id=?',(build,doc))
            db.execute("UPDATE static_queue SET state='done',checked_at=?,retry_at=NULL,error=NULL,document_id=? WHERE url=?",
                       (now,doc,job['url']))
            if job['provider']=='foreverdb_api' and build and fetched_at is None:
                previous=get_meta(db,'foreverdb_api_build')
                if previous and previous!=build:
                    # Refresh only evidence from a different build, including earlier pages in this run.
                    db.execute("""UPDATE static_queue SET state='pending' WHERE state='done' AND url!=?
                        AND provider IN ('foreverdb','foreverdb_api') AND document_id IN
                        (SELECT id FROM static_documents WHERE game_build IS NOT NULL AND game_build!=?)""",
                        (job['url'],build))
                    print(f'API 빌드 변경 {previous} → {build}: 이전 빌드 페이지 갱신 예약',flush=True)
                set_meta(db,'foreverdb_api_build',build)
                set_meta(db,'foreverdb_build',build)
            # Root profession data exposes the current source build without downloading the whole catalogue.
            if job['kind']=='professions' and build and not get_meta(db,'forever_api_enabled'):
                previous=get_meta(db,'foreverdb_build')
                if previous and previous!=build:
                    db.execute("UPDATE static_queue SET state='pending' WHERE state='done' AND url!=?",(job['url'],))
                    print(f'공급자 빌드 변경 {previous} → {build}: 정적 캐시 갱신 예약')
                set_meta(db,'foreverdb_build',build)
        return True
    except (ValueError,TypeError,KeyError,IndexError,ET.ParseError) as exc:
        with db:
            db.execute("UPDATE static_queue SET state='parse_error',checked_at=?,document_id=?,error=? WHERE url=?",
                       (now,doc,type(exc).__name__,job['url']))
            coverage(db,job['url'],job['kind'],'parse_error',note='Previous facts preserved; new raw response cached; parser review needed')
        return False


def collect(db,cfg,budget,cache_dir,interval=1.1,offline=False,provider=None,catalog_only=False,urls=None):
    cache_dir.mkdir(parents=True,exist_ok=True)
    attempted=ok=0
    selected = '' if urls is None else ' AND url IN ('+','.join('?' for _ in urls)+')'
    while attempted<budget:
        now=utcnow()
        job=db.execute("""SELECT * FROM static_queue WHERE (state='pending' OR
            (state='retry' AND retry_at<=?)) AND (? IS NULL OR provider=?)
            AND (?=0 OR kind IN ('api_meta','api_docs','api_items','api_vendors','api_vendor','api_professions','api_profession','api_quest'))
            """+selected+" ORDER BY priority,url LIMIT 1",
            (now,provider,provider,int(catalog_only),*(urls or []))).fetchone()
        if job is None: break
        if get_meta(db,'provider_blocked:'+job['provider']) and job['kind']!='api_docs':
            db.execute("UPDATE static_queue SET state='blocked',error=? WHERE url=?",
                       (get_meta(db,'provider_block_reason:'+job['provider']) or 'provider access blocked; manual retry required',job['url']))
            db.commit();continue
        if offline:
            doc=db.execute('SELECT * FROM static_documents WHERE url=? ORDER BY fetched_at DESC LIMIT 1',(job['url'],)).fetchone()
            if not doc: break
            status,body,retry=200,Path(doc['cache_path']).read_bytes(),None
        else:
            day=now[:10]
            usage_key='static_requests:'+day
            used=int(get_meta(db,usage_key) or 0)
            if used>=9500:
                print('일일 요청 안전 한도 9500 도달: 다음 UTC 날짜에 재개');break
            if attempted: time.sleep(max(1.1,interval))
            status,body,retry=fetch(job)
            set_meta(db,usage_key,used+1)
        attempted+=1
        db.execute('UPDATE static_queue SET attempts=attempts+1 WHERE url=?',(job['url'],));db.commit()
        if status==200:
            success=store_response(db,job,body,cache_dir);ok+=int(success)
            print(f'[{attempted}/{budget}] {job["provider"]} {job["kind"]}: '+('저장' if success else '파싱 확인 필요'),flush=True)
        else:
            state='blocked' if status in (-1,401,403) else 'missing' if status in (404,410) else 'retry'
            error='missing_key (no HTTP request)' if status==-1 else f'HTTP {status}'
            wait=retry_seconds(retry) if status==429 else 300
            due=(datetime.now(timezone.utc)+timedelta(seconds=wait)).isoformat(timespec='seconds')
            with db:
                db.execute('UPDATE static_queue SET state=?,checked_at=?,retry_at=?,error=? WHERE url=?',
                           (state,now,due,error,job['url']))
                coverage(db,job['url'],job['kind'],state,note=f'{error}; relationship is unknown, not absent')
            print(f'[{attempted}/{budget}] {job["provider"]} {job["kind"]}: {error} / {state}',flush=True)
            if status in (401,403):
                set_meta(db,'provider_blocked:'+job['provider'],now)
                reason=f'provider HTTP {status}; manual retry required'
                set_meta(db,'provider_block_reason:'+job['provider'],reason)
                db.execute("UPDATE static_queue SET state='blocked',error=? WHERE provider=? AND state='pending'",(reason,job['provider']))
                db.commit()
                if status==401: break
            if status==429:
                set_meta(db,'static_retry_after',due);db.commit();break
    return attempted,ok


def main():
    p=parser(__doc__)
    p.add_argument('--max-requests',type=int,default=60,help='bounded batch; rerun to resume')
    p.add_argument('--force',action='store_true',help='refresh cached successes/errors')
    p.add_argument('--patch',help='new client patch/build marker triggers refresh')
    p.add_argument('--api-probe',action='store_true',help='fetch small authenticated API samples; key required')
    p.add_argument('--status',action='store_true',help='counts only; no network')
    p.add_argument('--provider',choices=['wowhead','foreverdb','foreverdb_api'],help='only this provider')
    p.add_argument('--catalog-only',action='store_true',help='API catalogues, professions and vendor inventories; skip per-item details')
    p.add_argument('--url',action='append',help='only these already discovered queue URLs; repeat to select several')
    p.add_argument('--reparse-cache',action='store_true',help='replay latest cached API responses, preserving original retrieval dates')
    p.add_argument('--retry-errors',action='store_true')
    p.add_argument('--retry-provider',choices=['wowhead','foreverdb','foreverdb_api'],help='explicit retry after provider access restored')
    p.add_argument('--import-response',help='local actual response metadata JSON with adjacent .html body')
    args=p.parse_args();cfg=load_config(args.config)
    if cfg['game_flavor']!='forever': raise AppError('sync_static.py는 Forever DB만 지원합니다. --config config.forever.json 사용')
    if args.max_requests<0: raise AppError('--max-requests는 0 이상이어야 합니다.')
    load_env(cfg['base_dir']/'.env')
    db=connect(cfg,args.db)
    if args.status: print(summary(db));return
    with run(db,'sync_static'):
        initialise(db,cfg,args.force,args.patch,args.api_probe)
        if args.url:
            for url in args.url:
                job=db.execute('SELECT * FROM static_queue WHERE url=?',(url,)).fetchone()
                if not job or not valid_url(url,job['provider']):
                    raise AppError('--url은 이미 발견한 유효한 대기열 URL이어야 합니다.')
        cache_dir=cfg['base_dir']/cfg.get('static_cache','cache/static')
        if args.reparse_cache:
            jobs=db.execute("SELECT * FROM static_queue WHERE provider='foreverdb_api' AND kind NOT IN ('api_docs','api_probe')").fetchall()
            replayed=0
            for job in jobs:
                doc=db.execute('SELECT * FROM static_documents WHERE url=? ORDER BY fetched_at DESC,id DESC LIMIT 1',(job['url'],)).fetchone()
                if doc:
                    replayed+=int(store_response(db,job,Path(doc['cache_path']).read_bytes(),cache_dir,doc['fetched_at']))
            print(f'캐시 API {replayed}개 재해석: 새 HTTP 요청/새 관측일 아님')
        if args.import_response:
            path=Path(args.import_response)
            meta=json.loads(path.read_text(encoding='utf-8'))
            body=path.with_suffix('.html').read_bytes()
            browser_capture=meta.get('capture')=='browser_dom' and meta.get('kind')=='wowhead_sold_by'
            if (meta.get('status')!=200 and not browser_capture) or meta.get('bytes')!=len(body): raise AppError('실제 응답 메타데이터/길이 확인 실패')
            if browser_capture:
                seed(db,int(meta['entity_id']),'browser_observation',meta['url'])
                enqueue(db,meta['url'],'wowhead','wowhead_sold_by',int(meta['entity_id']),99)
            job=db.execute('SELECT * FROM static_queue WHERE url=?',(meta['url'],)).fetchone()
            if not job or not valid_url(meta['url'],job['provider']): raise AppError('대기열에 있는 공급자 URL만 가져올 수 있습니다.')
            if not store_response(db,job,body,cfg['base_dir']/cfg.get('static_cache','cache/static'),meta['fetched_at']):
                raise AppError('저장된 실제 응답 파싱 실패')
            if browser_capture:
                db.execute("UPDATE static_documents SET http_status=0,verification='browser_dom_observed' WHERE url=?",(meta['url'],))
                db.commit()
            print('저장된 실제 HTTP 응답 재사용 (새 네트워크 관측 아님)')
        if args.retry_provider:
            db.execute('DELETE FROM meta WHERE key=?',('provider_blocked:'+args.retry_provider,))
            db.execute('DELETE FROM meta WHERE key=?',('provider_block_reason:'+args.retry_provider,))
            db.execute("UPDATE static_queue SET state='pending' WHERE provider=? AND state='blocked'",(args.retry_provider,))
            db.commit()
        if args.retry_errors:
            db.execute("UPDATE static_queue SET state='pending',retry_at=NULL WHERE state IN ('parse_error','missing','retry')")
            db.commit()
        blocked_until=get_meta(db,'static_retry_after')
        if blocked_until and utcnow()<blocked_until:
            print(f'Retry-After 대기: {blocked_until} 이후 재실행');return
        attempted,ok=collect(db,cfg,args.max_requests,cache_dir,provider=args.provider,catalog_only=args.catalog_only,urls=args.url)
        print(f'이번 실행 {attempted}개 응답 / {ok}개 파싱. 미완료 큐는 다음 실행에서 재개.')
        print(summary(db))
        if not os.environ.get('FOREVERDB_API_KEY'):
            print('ForeverDB API 키 없음: .env FOREVERDB_API_KEY 필요. NPC ID/묶음/통화/평판 상세 보완 미완료.')


if __name__=='__main__':
    main_guard(main)
