"""Join local prices to static evidence without manufacturing missing market metrics."""
import json
from common import AppError, config_path, get_meta


def known_spells(cfg):
    path=config_path(cfg,'known_recipes')
    if not path.exists(): return None
    result=set()
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line=line.split('#',1)[0].strip()
        if not line: continue
        if not line.startswith('spell:') or not line[6:].isdigit() or int(line[6:])<=0:
            raise AppError('Forever known_recipes.txt는 spell:7753 형식의 craft spell ID를 사용하세요. Blizzard recipe ID와 혼용하지 않습니다.')
        result.add(int(line[6:]))
    return result


def report(db,cfg,now):
    from analyze import gold
    escape=lambda s:str(s if s is not None else '미확인').replace('|','/').replace('\n',' ')
    rid=get_meta(db,'auctionator_local_realm_id')
    current_build=get_meta(db,'foreverdb_api_build') or get_meta(db,'foreverdb_build')
    lines=['# WoW Forever 경매장 조사 리포트','',
           '[Data from ForeverDB.net](https://foreverdb.net) · [Wowhead Forever](https://www.wowhead.com/forever/ko/database)',
           f'생성 UTC: {now.isoformat()} · 로컬 시장: {cfg["auctionator_realm_key"]} (DB realm_id={rid or "미수집"})','',
           f'현재 확인한 공급자 빌드: {current_build or "미확인"}. 다른 빌드의 근거는 갱신 전 자료이며 현재 라이브 수치로 확정하지 않습니다.',
           '이 시장 키는 SavedVariables 내부 식별자입니다. 실제 지역·서버·진영을 확인한 Blizzard 서버 ID가 아닙니다.',
           '수수료 규칙: 순이익 = floor(판매가 × 판매 개수 × 0.95) − 구매 비용. 이 조사표는 가중 중위가·현재 수량이 없어 그 식으로 수익 추천을 만들지 않습니다.',
           '조건부 소량 시험만 market_dashboard의 오늘 할 일에서 계산합니다. 마지막 최저가 자체를 기대 판매가로 쓰지 않습니다.',
           '제작·퀘스트 용도는 수요 근거이며 판매량으로 환산하지 않습니다.','']
    for title in ('제작 차익','상점템 되팔기','7일/30일 저점 매수'):
        lines += [f'## {title}','',
            '데이터 부족: Auctionator m은 마지막 최저가입니다. 가중 중위가·현재 수량·경매 수가 없어 수익 추천 0개.',
            '하루 판매 추정: 계산 불가. 반복 수입으로 같은 관측을 새 판매량으로 만들지 않습니다.','']
    n=db.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    priced=db.execute('SELECT COUNT(DISTINCT item_id) FROM prices').fetchone()[0]
    daily=db.execute('''SELECT COUNT(*),COUNT(DISTINCT d.item_id) FROM auctionator_daily d
        JOIN auctionator_imports f ON f.id=d.import_id WHERE f.realm_id=?''',
        (int(rid) if rid else -999,)).fetchone()
    lines += [f'일별 가격 범위·최대 수량 이력 {daily[0]:,}행 / {daily[1]:,}개 품목 보관. '
              '일별 최저가 범위는 화면의 7일 비교 기준에 반영하며 최대 수량을 판매량으로 환산하지 않습니다.', '']
    lines += [f'아이템 {n:,}개 보관, 가격을 한 번이라도 관측한 아이템 {priced:,}개. 미관측 아이템은 추천 제외.','',
              '## 내 최저가와 판매처 연결 (수익 추천 아님)','',
              '| 아이템 | 판매 NPC / 위치 | 사이트 표시 가격 | 내 마지막 최저가 | 스냅샷 / 관측일 | 확인 상태 |',
              '|---|---|---:|---:|---|---|']
    rows=db.execute('''WITH candidates AS (SELECT v.*,i.name,p.min_price,p.snapshot_id,p.observed_at,c.eligibility,
        ROW_NUMBER() OVER(PARTITION BY v.item_id ORDER BY
          CASE v.verification WHEN 'source_reported_live' THEN 0 WHEN 'forever_branch_unverified' THEN 1 ELSE 2 END,
          CASE WHEN v.source='foreverdb_api' AND v.currency_costs IS NOT NULL THEN 0 ELSE 1 END,
          v.price_copper,v.offer_id) choice
        FROM current_vendor_offers v JOIN items i USING(item_id)
        JOIN latest_prices p ON p.item_id=v.item_id AND p.source='auctionator' AND p.realm_id=?
        LEFT JOIN character_offer_checks c ON c.offer_id=v.offer_id AND c.character_key=?
        ) SELECT * FROM candidates WHERE choice=1 ORDER BY
        CASE verification WHEN 'source_reported_live' THEN 0 ELSE 1 END,item_id LIMIT 20''',
        (int(rid) if rid else -999,cfg.get('character_key','default'))).fetchall()
    for r in rows:
        eligibility=r['eligibility'] or 'unknown'
        costs=json.loads(r['currency_costs']) if r['currency_costs'] is not None else None
        currency='별도 비용 미확인' if costs is None else '별도 비용 없음' if not costs else json.dumps(costs,ensure_ascii=False)
        cells=[r['name'] or f"item#{r['item_id']}",f"{r['npc_name'] or 'NPC 미확인'} / {r['zone'] or '위치 미확인'} ({r['x']}, {r['y']})",
               gold(r['price_copper']) if r['price_copper'] is not None else '미확인',gold(r['min_price']),
               f"#{r['snapshot_id']} / {r['observed_at']}",
               f"{r['verification']}; 구매묶음 {r['purchase_qty'] or '미확인'}; {currency}; {r['stock_state']}; 캐릭터 {eligibility}"+
               ('; 이전 빌드 자료' if current_build and r['game_build']!=current_build else '')]
        lines.append('| '+' | '.join(map(escape,cells))+' |')
    if not rows: lines.append('| 데이터 부족 | 가격 관측과 연결된 판매처 없음 | — | — | — | — |')
    lines += ['','사이트 가격의 구매 묶음·별도 화폐·재고·평판이 미확인이면 개당 비용으로 사용하지 않습니다.',
              'NPC 판매 정보가 확인되어도 내 캐릭터의 진영·평판·재고 접근 가능 여부는 별도로 확인해야 합니다.','',
              '## 행동을 계산하기 위한 근거','']
    seen=set()
    for r in rows:
        if r['item_id'] in seen: continue
        seen.add(r['item_id'])
        count=db.execute('SELECT COUNT(*) FROM prices p JOIN snapshots s ON s.id=p.snapshot_id '
                         'WHERE p.item_id=? AND s.realm_id=?',(r['item_id'],int(rid))).fetchone()[0]
        lines.append(f"- {r['npc_name']} ({r['zone']})에서 {r['name'] or r['item_id']} "
                     f"{r['purchase_qty'] or '개수 미확인'}개 한 묶음 구매 → 보유 → "
                     f"내 경매장 등록가는 아직 산정 불가. 사이트 표시 {gold(r['price_copper']) if r['price_copper'] is not None else '미확인'}, "
                     f"내 최저가 {gold(r['min_price'])}; 가격 스냅샷 {count}개. "
                     f"근거: {r['source_url']}, 빌드 {r['game_build'] or '미상'}, 조회 {r['source_fetched_at'] or r['retrieved_at']}.")
        lines.append('  구매 묶음 b, 한 번 구매 가격 c가 확인되면 n개 구매비용 = ceil(n/b)×c + 별도 화폐 비용. '
                     '판매가 p와 판매 개수 n이 확인된 경우에만 순이익 = floor(p×n×0.95)−구매비용. 보증금 손실·재등록 비용 별도.')
    known=known_spells(cfg)
    lines += ['','## 제작 관계와 내 가격 연결 (수익 추천 아님)','',
              'API의 client_catalog는 클라이언트 제작 관계입니다. 라이브 제작 성공·거래 가능 여부와 구분합니다.',
              '| 제작 주문 | 살 재료 / 1회 필요량 | 만들 결과물 | 결과물 마지막 최저가 | 배운 제작법 | 근거 |',
              '|---|---|---|---:|---|---|']
    crafts=db.execute('''SELECT f.*,p.min_price,p.snapshot_id,p.observed_at FROM current_static_facts f
        JOIN latest_prices p ON p.item_id=f.object_id AND p.source='auctionator' AND p.realm_id=?
        WHERE f.provider='foreverdb_api' AND f.kind='craft_output' AND f.verification='client_catalog'
        AND f.quantity IS NOT NULL ORDER BY CAST(f.subject_id AS INTEGER),f.fetched_at DESC''',(int(rid) if rid else -999,)).fetchall()
    spells=set()
    for r in crafts:
        spell=int(r['subject_id'])
        if spell in spells or (known is not None and spell not in known): continue
        spells.add(spell)
        ingredients=db.execute("SELECT f.*,i.name FROM static_facts f LEFT JOIN items i ON i.item_id=f.object_id "
                               "WHERE f.document_id=? AND f.subject_id=? AND f.kind='craft_reagent'",
                               (r['document_id'],r['subject_id'])).fetchall()
        materials=', '.join(f"{v['name'] or v['label'] or v['object_id']} ×{v['quantity']}" for v in ingredients)
        cells=[f'spell:{spell}',materials or '재료 정보 없음',f"{r['label']} ×{r['quantity']}",gold(r['min_price']),
               '목록에 있음' if known is not None else '레시피 필요',
               f"시장 #{r['snapshot_id']} / {r['observed_at']}; {r['source_url']}; 빌드 {r['game_build']}; 조회 {r['fetched_at']}"]
        lines.append('| '+' | '.join(map(escape,cells))+' |')
        if len(spells)>=20: break
    if not spells: lines.append('| 데이터 부족 | 확인된 제작법과 가격 연결 없음 | — | — | — | — |')
    lines += ['','재료 구매 → 해당 주문으로 제작 → 내 시장 등록 순서입니다. '
              '중위가·거래 가능 여부·구매 조건을 확인하기 전에는 등록가와 순이익을 산정하지 않습니다. '
              'known_recipes.txt가 있으면 spell:7753 형식으로 필터합니다.']
    lines += ['','## 정적 관계 / 누락 현황','',
              '| 공급자 | 관계 | 검증 상태 | 건수 |','|---|---|---|---:|']
    for r in db.execute('SELECT provider,kind,verification,COUNT(*) FROM current_static_facts GROUP BY provider,kind,verification'):
        lines.append('| '+' | '.join(map(escape,r))+' |')
    for r in db.execute("SELECT relation_kind,status,COUNT(*) n FROM static_coverage WHERE status NOT IN ('observed','page_observed','documentation') GROUP BY relation_kind,status"):
        lines.append(f"- {r['relation_kind']}: {r['status']} {r['n']}페이지. 관계 없음이라는 뜻이 아닙니다.")
    lines += ['','Blizzard recipe ID / craft spell ID / 도안 item ID / 결과물 item ID는 별도 열입니다. '
              '이름이 같다는 이유로 ID를 연결하지 않습니다. 수량·재료 ID가 빠진 제작은 원가 계산에서 제외합니다.',
              'ForeverDB 인증 API에서 제작 ID·수량, NPC 좌표·구매 묶음·통화를 수집합니다. '
              '퀘스트 요구 아이템은 확인한 API 응답에서 제공되지 않았으므로 라이브 요구 관계로 보완할 수 없습니다.']
    from blizzard_collector import summary_lines
    lines += summary_lines(db, cfg, int(rid) if rid else None)
    return '\n'.join(lines)+'\n'
