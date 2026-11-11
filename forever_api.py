"""Adapters for observed ForeverDB v1 JSON. Source labels are not live guarantees."""
import hashlib
import json
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from common import get_meta, set_meta
from static_store import coverage, enqueue, fact, offer, seed

BASE = 'https://foreverdb.net/api/v1/'


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def integer(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError('Invalid API integer')
    return value


def catalog_status(row, build):
    if row.get('betaEvent'): return 'beta_reference'
    if row.get('speculation'): return 'speculative'
    if row.get('dataFromBuild') and row['dataFromBuild'] != build: return 'older_build_reference'
    return 'client_catalog'


def api_job(db, path, kind, entity=None, priority=10):
    enqueue(db, BASE + path, 'foreverdb_api', kind, entity, priority)


def item_seed(db, row, job, reason, field='itemId'):
    ident = integer(row[field])
    seed(db, ident, reason, job['url'], 70)
    db.execute('UPDATE items SET name=COALESCE(name,?) WHERE item_id=?', (row.get('name'), ident))
    api_job(db, f'items/{ident}', 'api_item', ident, 80)
    return ident


def pagination(db, job, doc, meta, rows, ids):
    """Reject short pages, repeated IDs, changed filters and non-forward cursors."""
    total, limit, offset = (integer(meta[k], 0 if k != 'limit' else 1) for k in ('total','limit','offset'))
    parts = urlsplit(job['url']); query = parse_qs(parts.query)
    if limit > 100 or offset != int(query.get('offset', ['0'])[0]) or limit != int(query.get('limit', ['25'])[0]):
        raise ValueError('Pagination metadata mismatch')
    if len(rows) != min(limit, max(0, total-offset)) or len(set(ids)) != len(ids):
        raise ValueError('Truncated or duplicate API page')
    nxt = meta['next']
    filters = {k: v for k,v in query.items() if k != 'offset'}
    collection = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(filters.items()), doseq=True), ''))
    if offset+len(rows) < total:
        if not isinstance(nxt,str): raise ValueError('Missing API next page')
        next_url = urljoin(BASE, nxt); target = urlsplit(next_url); nq = parse_qs(target.query)
        if (target.scheme,target.netloc,target.path) != (parts.scheme,parts.netloc,parts.path) or target.fragment:
            raise ValueError('API next page left collection')
        if {k:v for k,v in nq.items() if k!='offset'} != filters or nq.get('offset') != [str(offset+len(rows))]:
            raise ValueError('API next cursor skipped or repeated rows')
        if limit==100:  # One-row probes remain samples; bulk jobs use the documented maximum.
            enqueue(db,next_url,'foreverdb_api',job['kind'],priority=job['priority'])
    elif nxt is not None:
        raise ValueError('Unexpected API next page')
    for old in db.execute('SELECT p.ids FROM static_api_pages p JOIN static_documents d ON d.id=p.document_id '
                          'WHERE p.collection=? AND p.page_offset!=? AND d.active=1 AND d.game_build=? AND p.total=?',
                          (collection,offset,meta.get('build'),total)):
        if set(ids).intersection(json.loads(old[0])): raise ValueError('Repeated IDs across API pages')
    # Catalogue row count can change even within one build. Revisit earlier pages before claiming completeness.
    db.execute("""UPDATE static_queue SET state='pending' WHERE state='done' AND url IN
        (SELECT p.url FROM static_api_pages p JOIN static_documents d ON d.id=p.document_id
         WHERE p.collection=? AND (p.total!=? OR d.game_build IS NOT ?))""",(collection,total,meta.get('build')))
    db.execute('''INSERT INTO static_api_pages VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
        document_id=excluded.document_id,collection=excluded.collection,page_offset=excluded.page_offset,
        total=excluded.total,observed=excluded.observed,ids=excluded.ids''',
        (job['url'],doc,collection,offset,total,len(rows),dump(ids)))
    coverage(db,job['url'],'api_page','page_observed',len(rows),total,
             'One page checked; collection completeness requires contiguous pages with consistent total/build')


def item(db, job, doc, row, build):
    ident = item_seed(db,row,job,'api_catalog','id')
    status = catalog_status(row,build)
    fact(db,doc,'item_basic','item',ident,label=row['name'],verification=status,payload=row)
    if status == 'client_catalog':
        db.execute('''UPDATE items SET class_id=?,class_name=?,subclass_id=?,subclass_name=?,
            sell_price=COALESCE(?,sell_price),binding=COALESCE(?,binding) WHERE item_id=?''',
            (row.get('classId'),row.get('class'),row.get('subclassId'),row.get('subclass'),
             row.get('sellPrice'),str(row['bonding']) if row.get('bonding') is not None else None,ident))
    # Item effects do not establish teaching relationships. Profession records do.


def vendor_offer(db, job, doc, npc, row, ident, limited_field='limited'):
    price=row.get('price'); stack=row.get('stack')
    if price is not None: integer(price,0)
    if stack is not None: integer(stack)
    if row.get('notOnListPlayersSaw'): status='not_seen_on_list'
    elif row.get('classicData') or row.get('seenInForever') is False: status='classic_reference'
    elif row.get('seenInForever') is True: status='source_reported_live'
    else: status='unverified'
    costs=row.get('costs')
    if costs is not None and not isinstance(costs,list): raise ValueError('Currency cost schema changed')
    limited=row.get(limited_field)
    stock='limited_displayed' if limited is True else 'unlimited_displayed' if limited is False else 'unknown'
    npc_id=integer(npc.get('npcId',npc.get('id')))
    key=hashlib.sha256(dump([doc,npc_id,ident,row]).encode()).hexdigest()
    offer(db,ident,'foreverdb_api','api:'+key,document_id=doc,npc_id=npc_id,npc_name=npc['name'],
          zone=npc.get('zone'),map_id=npc.get('mapId'),x=npc.get('x'),y=npc.get('y'),
          price_copper=price,gold=price//10000 if price is not None else None,
          silver=price//100%100 if price is not None else None,copper=price%100 if price is not None else None,
          purchase_qty=stack,price_basis='per_purchase',stock_state=stock,stock_quantity=row.get('limitedStockCount'),
          currency_costs=dump(costs) if costs is not None else None,
          reputation_discount_faction=dump(npc['reputationFaction']) if npc.get('reputationFaction') else None,
          # NPC side and reputation discount are not confirmed purchase requirements.
          faction=None,reputation_required=None,conditions=dump({'npc_side':npc.get('side',npc.get('faction')),
          'offer':row,'npc_seen':npc.get('seenInForever'),'source':npc.get('source')}),
          source_url=job['url'],verification=status,note='NPC side is not character eligibility; missing costs/requirements remain unknown')


def profession(db,job,doc,data,build):
    recipes=data['recipes']
    for r in recipes:
        spell=integer(r['spellId']); status=catalog_status(r,build)
        fact(db,doc,'craft_recipe','spell',spell,label=r['name'],verification=status,
             profession=data['slug'],payload=r)
        makes=r['makes']; out=qty=None
        if makes is not None:
            out=item_seed(db,makes,job,'api_craft_output')
            qty=makes.get('count')
            if qty is not None: integer(qty)
            # A variable yield is not a guaranteed count.
            if makes.get('min') != makes.get('max'): qty=None
            fact(db,doc,'craft_output','spell',spell,'item',out,makes.get('name'),qty,status,payload=makes)
        if not isinstance(r['reagents'],list): raise ValueError('Unknown recipe reagents')
        for reagent in r['reagents']:
            iid=item_seed(db,reagent,job,'api_craft_reagent'); count=integer(reagent['count'])
            fact(db,doc,'craft_reagent','spell',spell,'item',iid,reagent.get('name'),count,status)
        learned=r['learnedFrom']; sources=learned.get('sources') or {}
        for recipe_item in learned['recipeItems']:
            iid=item_seed(db,recipe_item,job,'api_recipe_item')
            fact(db,doc,'teaches_spell','item',iid,'spell',spell,r['name'],verification=status)
            db.execute('''INSERT INTO recipe_mappings(document_id,recipe_item_id,craft_spell_id,output_item_id,
                verification,output_qty) VALUES (?,?,?,?,?,?)''',(doc,iid,spell,out,status,qty))
        hidden=learned.get('recipeItemsHidden',[])
        if not isinstance(hidden,list): raise ValueError('Hidden recipe IDs schema changed')
        for iid in hidden:
            integer(iid)
            item_seed(db,{'itemId':iid},job,'api_hidden_recipe_item')
            hidden_status='hidden_client_reference' if status=='client_catalog' else status
            fact(db,doc,'teaches_spell','item',iid,'spell',spell,r['name'],verification=hidden_status,
                 note='Client references this recipe item but its details are hidden; availability unconfirmed')
            db.execute('''INSERT INTO recipe_mappings(document_id,recipe_item_id,craft_spell_id,output_item_id,
                verification,output_qty) VALUES (?,?,?,?,?,?)''',(doc,iid,spell,out,hidden_status,qty))
        if not learned['recipeItems'] and not hidden:
            db.execute('''INSERT INTO recipe_mappings(document_id,craft_spell_id,output_item_id,verification,output_qty)
                VALUES (?,?,?,?,?)''',(doc,spell,out,status,qty))
        source_item=sources.get('recipeItem')
        if source_item:
            iid=item_seed(db,source_item,job,'api_vendor_recipe')
            for side in (sources.get('vendors') or {}).values():
                for npc in side:
                    vendor_offer(db,job,doc,npc,npc,iid,'limitedStock')
                    api_job(db,f'vendors/{integer(npc["npcId"])}','api_vendor',npc['npcId'],20)
        for quest in sources.get('quests',[]):
            qid=integer(quest['questId'])
            api_job(db,f'quests/{qid}','api_quest',qid,60)
            # A recipe reward is NOT an item required to complete the quest.
            fact(db,doc,'recipe_quest_source','spell',spell,'quest',qid,quest.get('name'),
                 verification='client_catalog' if quest.get('inForeverClient') else 'unresolved',payload=quest)


def parse_api(db,job,doc,response):
    data,meta=response['data'],response['meta']
    if not isinstance(meta,dict) or not isinstance(data,(list,dict)): raise ValueError('API envelope changed')
    build=meta.get('build')
    kind=job['kind']
    # Make cache replays idempotent, including nullable recipe mapping columns.
    db.execute('DELETE FROM static_facts WHERE document_id=?',(doc,))
    db.execute('DELETE FROM recipe_mappings WHERE document_id=?',(doc,))
    if kind=='api_meta':
        integer(data['items'],0) if isinstance(data['items'],int) else None
        coverage(db,job['url'],'api_meta','observed',1)
    elif kind in ('api_items','api_vendors'):
        if not isinstance(data,list): raise ValueError('Expected API list')
        ids=[integer(r['id']) for r in data]
        pagination(db,job,doc,meta,data,ids)
        for r in data:
            if kind=='api_items': item(db,job,doc,r,build)
            else:
                fact(db,doc,'vendor_npc','npc',r['id'],label=r['name'],verification=
                     'source_reported_live' if r.get('seenInForever') else 'classic_reference',payload=r)
                api_job(db,f'vendors/{r["id"]}','api_vendor',r['id'],20)
    elif kind=='api_item':
        if data['id']!=job['entity_id']: raise ValueError('Item identity mismatch')
        item(db,job,doc,data,build);coverage(db,job['url'],'item_basic','observed',1)
        coverage(db,job['url'],'quest_requires','unsupported',note='Item details expose no required-quest relationship')
    elif kind=='api_vendor':
        if data['id']!=job['entity_id'] or not isinstance(data['sells'],list): raise ValueError('Vendor schema changed')
        for row in data['sells']:
            iid=item_seed(db,row,job,'api_vendor_inventory')
            vendor_offer(db,job,doc,data,row,iid)
        coverage(db,job['url'],'vendor_offers','observed',len(data['sells']),note='Documented full inventory; missing fields remain NULL')
    elif kind=='api_professions':
        if len(data)!=meta['total']: raise ValueError('Truncated profession index')
        for p in data:
            slug=p['slug']
            if not slug or any(c not in 'abcdefghijklmnopqrstuvwxyz-' for c in slug): raise ValueError('Invalid profession slug')
            api_job(db,f'professions/{slug}?limit=100','api_profession',priority=5)
        coverage(db,job['url'],'profession_index','observed',len(data),meta['total'])
    elif kind=='api_profession':
        if urlsplit(job['url']).path.rsplit('/',1)[1]!=data['slug']: raise ValueError('Profession identity mismatch')
        pagination(db,job,doc,meta,data['recipes'],[integer(r['spellId']) for r in data['recipes']])
        profession(db,job,doc,data,build)
        coverage(db,job['url'],'craft','observed',len(data['recipes']),meta['total'])
    elif kind=='api_quest':
        if data['id']!=job['entity_id']: raise ValueError('Quest identity mismatch')
        fact(db,doc,'quest_basic','quest',data['id'],label=data['name'],verification=
             {'forever':'source_reported_live','classic':'classic_reference'}.get(data.get('source'),'unresolved'),payload=data)
        coverage(db,job['url'],'quest_requires','unsupported',note='Observed API has starts/ends/chain; required items absent, not zero')
    else: raise ValueError('Unknown API adapter')
    db.execute("DELETE FROM static_coverage WHERE url=? AND relation_kind IN ('api','api_probe')",(job['url'],))
    return build


def initialise_api(db):
    if not get_meta(db,'foreverdb_api_build') and get_meta(db,'foreverdb_build'):
        set_meta(db,'foreverdb_api_build',get_meta(db,'foreverdb_build'))
    for path,kind,priority in [('meta','api_meta',0),('items?limit=100','api_items',30),
                               ('vendors?seen=true&limit=100','api_vendors',4),('professions','api_professions',2)]:
        api_job(db,path,kind,priority=priority)
    api_job(db,'quests/4161','api_quest',4161,6)
    # Upgrade earlier probe jobs and replay their original responses without refetching.
    for job in db.execute("SELECT * FROM static_queue WHERE kind='api_probe'").fetchall():
        path=urlsplit(job['url']).path.removeprefix('/api/v1/')
        root=path.split('/')[0]; detail='/' in path
        kind={'meta':'api_meta','items':'api_item' if detail else 'api_items',
              'vendors':'api_vendor' if detail else 'api_vendors',
              'professions':'api_profession' if detail else 'api_professions','quests':'api_quest'}[root]
        entity=int(path.rsplit('/',1)[1]) if detail and root in ('items','vendors','quests') else None
        # Probe lists of one row are evidence only; canonical 100-row pages cover the catalogue.
        db.execute('UPDATE static_queue SET kind=?,entity_id=? WHERE url=?',(kind,entity,job['url']))
    db.execute("UPDATE static_queue SET state='pending' WHERE provider='foreverdb_api' AND error='missing_key (no HTTP request)'")
    set_meta(db,'forever_api_enabled','1')
