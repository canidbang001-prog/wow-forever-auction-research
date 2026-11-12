"""Parsers for inspected public SSR pages and Wowhead's documented item XML.

Never parse comments/scripts or equate missing relationship sections with empty lists.
"""
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from static_store import coverage, enqueue, fact, offer, seed


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag,self.attrs,self.children=tag,dict(attrs),[]

    def text(self):
        return ''.join(c.text() if isinstance(c,Node) else c for c in self.children).strip()

    def walk(self,tag=None):
        for c in self.children:
            if isinstance(c,Node) and c.tag not in ('script','style'):
                if tag is None or c.tag==tag:
                    yield c
                yield from c.walk(tag)


class Tree(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root=Node();self.stack=[self.root];self.feed(text)

    def handle_starttag(self,tag,attrs):
        n=Node(tag,attrs);self.stack[-1].children.append(n)
        if tag not in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'):
            self.stack.append(n)

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs);self.handle_endtag(tag)

    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:
                del self.stack[i:];break

    def handle_data(self,data):
        self.stack[-1].children.append(data)


def section(root,title):
    """Only the named section; avoids trainers, rewards, footer and comments."""
    for parent in [root,*root.walk()]:
        children=[c for c in parent.children if isinstance(c,Node)]
        for i,n in enumerate(children):
            if n.tag in ('h2','h3') and n.text()==title:
                result=[]
                for child in children[i+1:]:
                    if child.tag in ('h1','h2') or child.tag==n.tag:
                        break
                    result.append(child)
                return result
    return []


def list_rows(nodes):
    return [li for n in nodes if n.tag in ('ul','ol') for li in n.walk('li')]


def links(root,pattern):
    result=[]
    for a in root.walk('a'):
        m=re.fullmatch(pattern,a.attrs.get('href',''))
        if m:
            result.append((a,m))
    return result


def money(value):
    """Strict display units, NOT an assumed per-item price."""
    if not re.fullmatch(r'\s*(?:\d+\s*[gsc]\s*)+',value):
        return None
    units={}
    for amount,unit in re.findall(r'(\d+)\s*([gsc])',value):
        if unit in units:
            return None
        units[unit]=int(amount)
    g,s,c=(units.get(k,0) for k in 'gsc')
    if s>=100 or c>=100:
        return None
    return dict(price_copper=g*10000+s*100+c,gold=g,silver=s,copper=c)


def parse_xml(db,job,doc,text):
    root=ET.fromstring(text)
    item=root.find('item');item_id=job['entity_id']
    if item is None or int(item.attrib['id'])!=item_id or not item.findtext('name'):
        raise ValueError('item XML identity/schema mismatch')
    status='forever_branch_unverified'
    equip=json.loads('{'+(item.findtext('jsonEquip') or '')+'}')
    cls,sub=item.find('class'),item.find('subclass')
    values={k:equip.get(k) for k in ('buyprice','sellprice','reqskill','reqskillrank')}
    values.update(name=item.findtext('name'),class_id=int(cls.attrib['id']) if cls is not None else None,
                  subclass_id=int(sub.attrib['id']) if sub is not None else None)
    fact(db,doc,'item_basic','item',item_id,label=values['name'],verification=status,**values)
    db.execute('''UPDATE items SET name=?,purchase_price=?,sell_price=?,class_id=?,class_name=?,
        subclass_id=?,subclass_name=? WHERE item_id=?''',
        (values['name'],values['buyprice'],values['sellprice'],values['class_id'],cls.text if cls is not None else None,
         values['subclass_id'],sub.text if sub is not None else None,item_id))
    tip=Tree(item.findtext('htmlTooltip') or '').root
    for a,m in links(tip,r'https://www\.wowhead\.com/forever/[^ ]*item=(\d+)(?:/[^ ]*)?'):
        seed(db,int(m[1]),'tooltip_link',job['url'],45)
    # Only an explicit teaching link on a recipe item. Spell effects are not recipes.
    teach=[(a,m) for a,m in links(tip,r'https://www\.wowhead\.com/forever/[^ ]*spell=(\d+)(?:/[^ ]*)?')
           if '배웁니다' in a.text() or 'Teaches' in a.text()]
    reagents=[n for n in tip.walk() if 'whtt-reagents' in n.attrs.get('class','').split()]
    reagent_links=[(a,m) for n in reagents for a,m in links(n,r'.*item=(\d+)(?:/[^ ]*)?')]
    reagent_ids={id(a) for a,_ in reagent_links}
    outputs=[(a,m) for a,m in links(tip,r'.*item=(\d+)(?:/[^ ]*)?') if id(a) not in reagent_ids]
    if values['class_id']==9 and len(teach)==1 and len(outputs)==1:
        spell=int(teach[0][1][1]);output=int(outputs[0][1][1])
        fact(db,doc,'teaches_spell','item',item_id,'craft_spell',spell,verification=status)
        fact(db,doc,'craft_output','craft_spell',spell,'item',output,label=outputs[0][0].text(),verification=status)
        db.execute('''INSERT OR IGNORE INTO recipe_mappings(document_id,recipe_item_id,craft_spell_id,
            output_item_id,verification) VALUES (?,?,?,?,?)''',(doc,item_id,spell,output,status))
        for a,m in reagent_links:
            fact(db,doc,'craft_reagent','craft_spell',spell,'item',int(m[1]),label=a.text(),
                 verification=status,quantity_note='Tooltip link only; quantity not inferred')
    coverage(db,job['url'],'item_basic','observed',1)
    coverage(db,job['url'],'craft','partial' if teach else 'unknown',len(reagent_links),
             note='XML tooltip is not a complete relationship feed; output/reagent quantities unverified')
    for kind in ('vendors','quest_requires'):
        coverage(db,job['url'],kind,'unsupported',note='Documented XML does not supply this relationship list')
    return None


def parse_html(db,job,doc,text):
    root=Tree(text).root
    mains=list(root.walk('main'))
    if len(mains)!=1 or not list(mains[0].walk('h1')):
        raise ValueError('missing main/h1: changed markup or error page')
    main=mains[0];url=job['url'];kind=job['kind'];entity=job['entity_id']
    title=next(main.walk('h1')).text()
    if re.search(r'not found|access denied|just a moment',title,re.I):
        raise ValueError('soft missing/block page')
    data_nodes=[n for n in main.walk('p') if 'ssr-data' in n.attrs.get('class','').split()]
    build=re.search(r'build (\d+\.\d+\.\d+\.\d+)',data_nodes[0].text()) if data_nodes else None
    build=build[1] if build else None
    # Links from the main content are seeds only, never proof of a relationship.
    for a,m in links(main,r'/item/(\d+)'):
        seed(db,int(m[1]),kind+'_link',url,35 if kind=='vendors' else 55)
    if kind=='professions':
        refs=links(main,r'/profession/([a-z-]+)')
        if not refs: raise ValueError('profession index empty')
        for a,m in refs:
            enqueue(db,urljoin(url,a.attrs['href']),'foreverdb','profession',priority=10)
        coverage(db,url,'profession_index','observed',len({m[1] for _,m in refs}),note='Linked professions only')
    elif kind=='profession':
        rows=list_rows(section(main,'Recipes'))
        count=re.search(r'Showing\s+([\d,]+)[–-]([\d,]+) of ([\d,]+) recipes',main.text())
        advertised=re.search(r'([\d,]+) recipes in the WoW Forever client',main.text())
        expected=int((count[3] if count else advertised[1]).replace(',','')) if count or advertised else None
        if not rows and expected!=0: raise ValueError('recipe list missing')
        if count and len(rows)!=int(count[2].replace(',',''))-int(count[1].replace(',',''))+1:
            raise ValueError('recipe page count mismatch')
        for li in rows:
            rowtext=li.text()
            if ': ' not in rowtext:
                raise ValueError('recipe row format changed')
            recipe_label,inputs=rowtext.rsplit(': ',1)
            row_id=urlsplit(url).path+':'+recipe_label
            result=links(li,r'/item/(\d+)')
            fact(db,doc,'craft_output','recipe_row',row_id,'item',int(result[0][1][1]) if len(result)==1 else None,
                 label=recipe_label,verification='client_catalog',output_quantity='unknown',craft_spell_id=None)
            for reagent in inputs.split(', '):
                m=re.fullmatch(r'(\d+) (.+)',reagent)
                fact(db,doc,'craft_reagent','recipe_row',row_id,'item',label=m[2] if m else reagent,
                     quantity=int(m[1]) if m and int(m[1])>0 else None,verification='unresolved',
                     reason='SSR provides reagent names; no item ID. No name-based ID guessing.')
        nexts=[a for a in main.walk('a') if a.text().startswith('Next') and re.fullmatch(r'\?p=\d+',a.attrs.get('href',''))]
        for a in nexts:
            enqueue(db,urljoin(url,a.attrs['href']),'foreverdb','profession',priority=11)
        pagination_ok=expected is not None and (len(rows)==expected or bool(count))
        missing_next=bool(count and int(count[2].replace(',',''))<expected and not nexts)
        coverage(db,url,'craft','partial',len(rows),expected,'IDs/quantities incomplete; pagination tracked separately')
        coverage(db,url,'recipe_page','truncated' if missing_next or not pagination_ok else 'page_observed',
                 len(rows),expected,'Total belongs to profession, observed belongs to this page; not global completeness')
    elif kind=='vendors':
        rows=list_rows(section(main,'Recipe vendors'))
        if not rows: raise ValueError('vendor recipe seed list missing')
        for li in rows:
            for a,m in links(li,r'/item/(\d+)'):
                fact(db,doc,'vendor_listing','item',int(m[1]),label=li.text(),verification='unverified',
                     reason='Inventory seed; NPC IDs, live status and prices need item/vendor details')
        coverage(db,url,'vendors','partial',len(rows),note='Recipe subset only; all 964 vendors inventory not exposed in SSR')
    elif kind in ('quests','quest_category'):
        categories=links(main,r'/quests/([a-z-]+)')
        for a,m in categories:
            enqueue(db,urljoin(url,a.attrs['href']),'foreverdb','quest_category',priority=80)
        quests=links(main,r'/quest/(\d+)-[^/]+')
        for a,m in quests:
            enqueue(db,urljoin(url,a.attrs['href']),'foreverdb','quest',int(m[1]),70)
        expected_match=re.search(r'([\d,]+) quests with pickup',main.text())
        expected=int(expected_match[1].replace(',','')) if expected_match else None
        coverage(db,url,'quest_index','partial' if kind=='quests' or expected!=len(quests) else 'page_observed',
                 len(quests) if kind=='quest_category' else len(categories),expected)
    elif kind=='quest':
        nodes=section(main,'2. Complete the objectives')
        objectives=[n for n in nodes if n.tag=='h4']
        found=0
        for n in objectives:
            m=re.fullmatch(r'(.+?) × (\d+)(.*)',n.text())
            if not m: continue
            matches=links(n,r'/item/(\d+)')
            status='classic_reference' if 'Classic' in m[3] else 'unverified'
            # Quest existence or NPC sighting is not verification of objective counts.
            fact(db,doc,'quest_requires','quest',entity,'item',int(matches[0][1][1]) if len(matches)==1 else None,
                 label=m[1],quantity=int(m[2]),verification=status,raw=n.text())
            found+=1
        coverage(db,url,'quest_requires','partial' if found else 'unknown',found,
                 note='Objective IDs/Forever live quantities are not established by quest/NPC sightings')
    elif kind=='item':
        summary_nodes=[n for n in main.walk('p') if 'ssr-summary' in n.attrs.get('class','').split()]
        if not summary_nodes: raise ValueError('item summary missing')
        description=summary_nodes[0].text()
        status='beta_reference' if re.search(r'beta.only|beta.event|send.off',description,re.I) else 'client_catalog'
        fact(db,doc,'item_basic','item',entity,label=title,verification=status,name=title,summary=description)
        db.execute('UPDATE items SET name=COALESCE(name,?) WHERE item_id=?',(title,entity))
        # One explicit paragraph binds recipe item, teaching spell and output IDs.
        for p in main.walk('p'):
            if not p.text().startswith('Teaches '): continue
            spells=links(p,r'/spell/(\d+)');outputs=links(p,r'/item/(\d+)')
            if len(spells)==1 and len(outputs)==1 and 'which makes' in p.text():
                spell=int(spells[0][1][1]);output=int(outputs[0][1][1])
                fact(db,doc,'teaches_spell','item',entity,'craft_spell',spell,verification=status)
                fact(db,doc,'craft_output','craft_spell',spell,'item',output,verification=status)
                db.execute('''INSERT OR IGNORE INTO recipe_mappings(document_id,recipe_item_id,craft_spell_id,
                    output_item_id,verification) VALUES (?,?,?,?,?)''',(doc,entity,spell,output,status))
        rows=list_rows(section(main,'Sold by'));parsed=0
        advertised=re.search(r'Sold by (\d+)',description)
        expected=int(advertised[1]) if advertised else None
        for index,li in enumerate(rows):
            raw=li.text()
            m=re.fullmatch(r'(.+?), (.+?): ([\d\s]+[gsc](?:\s*\d+\s*[gsc])*) \((.+)\)',raw)
            if not m: continue
            price=money(m[3])
            if price is None: continue
            # Zone suffix is NPC affiliation, not a confirmed purchase restriction.
            faction=re.search(r' \((Alliance|Horde)\)$',m[2])
            zone=m[2][:faction.start()] if faction else m[2]
            verification=('classic_reference' if 'Classic' in m[4] else
                          'source_reported_live' if 'seen in WoW Forever' in m[4] else 'unverified')
            if status=='beta_reference': verification=status
            conditions={'source_label':m[4],'raw':raw}
            if faction:
                conditions['npc_side']=faction[1]
            offer(db,entity,'foreverdb',f'doc:{doc}:vendor:{index}',npc_name=m[1],zone=zone,
                  faction=None,**price,document_id=doc,source_url=url,
                  verification=verification,price_basis='displayed_unknown_bundle',
                  note='SSR displayed price. Bundle, alternate costs, stock and purchase eligibility unknown.',
                  conditions=json.dumps(conditions,ensure_ascii=False))
            parsed+=1
        coverage(db,url,'vendors','partial' if parsed else 'unknown',parsed,expected,
                 'No NPC IDs/bundle/stock/reputation/currency fields in inspected SSR; failed rows retained in cached page')
        if rows and parsed!=len(rows):
            coverage(db,url,'vendor_parse','partial',parsed,len(rows),'Unparsed/more rows, not an empty inventory')
        coverage(db,url,'item_basic','observed',1)
        coverage(db,url,'quest_requires','unsupported',note='Item SSR has no complete required-by quest list')
    else:
        raise ValueError('unsupported page kind')
    return build


def parse_wowhead_sold_by(db,job,doc,text):
    root=Tree(text).root
    containers=[n for n in root.walk() if n.attrs.get('id')=='tab-sold-by']
    if len(containers)!=1: raise ValueError('sold-by table missing')
    container=containers[0]
    rows=[n for n in container.walk('tr') if 'listview-row' in n.attrs.get('class','').split()]
    count=re.search(r'(\d+)\s*-\s*(\d+)\s*의\s*(\d+)',container.text())
    if not count or len(rows)!=int(count[2])-int(count[1])+1: raise ValueError('sold-by page count mismatch')
    for index,row in enumerate(rows):
        cells=[n for n in row.children if isinstance(n,Node) and n.tag=='td']
        if len(cells)!=7: raise ValueError('sold-by columns changed')
        npcs=links(cells[1],r'https://www\.wowhead\.com/forever/ko/npc=(\d+)/.*')
        zones=links(cells[2],r'https://www\.wowhead\.com/forever/ko/zone=(\d+)/.*')
        if len(npcs)!=1: raise ValueError('NPC ID missing')
        units={}
        for n in cells[5].walk('span'):
            for cls,key in (('moneygold','gold'),('moneysilver','silver'),('moneycopper','copper')):
                if cls in n.attrs.get('class','').split():
                    if key in units or not n.text().isdigit(): raise ValueError('money markup changed')
                    units[key]=int(n.text())
        if not units: raise ValueError('price units missing')
        g,s,c=(units.get(k,0) for k in ('gold','silver','copper'))
        if s>=100 or c>=100: raise ValueError('money units out of range')
        stock=cells[4].text()
        offer(db,job['entity_id'],'wowhead',f'doc:{doc}:vendor:{index}',npc_id=int(npcs[0][1][1]),
              npc_name=npcs[0][0].text(),zone=zones[0][0].text() if len(zones)==1 else None,
              price_copper=g*10000+s*100+c,gold=g,silver=s,copper=c,
              stock_state='unlimited_displayed' if stock=='∞' else 'limited_displayed' if stock.isdigit() else 'unknown',
              stock_quantity=int(stock) if stock.isdigit() else None,
              document_id=doc,source_url=job['url'],verification='forever_branch_unverified',
              conditions=json.dumps({'reaction_text':cells[3].text(),'zone_id':int(zones[0][1][1]) if len(zones)==1 else None},ensure_ascii=False),
              price_basis='displayed_unknown_bundle',note='Browser DOM observed. Reaction is not purchase eligibility; bundle/discount/currencies unknown.')
    coverage(db,job['url'],'vendors','partial',len(rows),int(count[3]),
             'Visible table only; full live build, bundle, discounts and purchase conditions unverified')
    coverage(db,job['url'],'vendor_page','truncated' if int(count[3])>len(rows) else 'page_observed',len(rows),int(count[3]))
    return None
