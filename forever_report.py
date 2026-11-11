"""Join local prices to static evidence without manufacturing missing market metrics."""
import json
from common import get_meta


def report(db,cfg,now):
    from analyze import gold
    escape=lambda s:str(s if s is not None else '미확인').replace('|','/').replace('\n',' ')
    rid=get_meta(db,'auctionator_local_realm_id')
    lines=['# WoW Forever 경매장 조사 리포트','',
           '[Data from ForeverDB.net](https://foreverdb.net) · [Wowhead Forever](https://www.wowhead.com/forever/ko/database)',
           f'생성 UTC: {now.isoformat()} · 로컬 시장: {cfg["auctionator_realm_key"]} (DB realm_id={rid or "미수집"})','',
           '이 시장 키는 SavedVariables 내부 식별자입니다. 실제 지역·서버·진영을 확인한 Blizzard 서버 ID가 아닙니다.',
           '판매가 = min(최근 가중 중위가, 현재 최저가), 순이익 = 판매가 × 판매 개수 × 0.95 − 구매 비용.',
           '제작·퀘스트 용도는 수요 근거이며 판매량으로 환산하지 않습니다.','']
    for title in ('제작 차익','상점템 되팔기','7일/30일 저점 매수'):
        lines += [f'## {title}','',
            '데이터 부족: Auctionator m은 마지막 최저가입니다. 가중 중위가·현재 수량·경매 수가 없어 수익 추천 0개.',
            '하루 판매 추정: 계산 불가. 반복 수입으로 같은 관측을 새 판매량으로 만들지 않습니다.','']
    n=db.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    priced=db.execute('SELECT COUNT(DISTINCT item_id) FROM prices').fetchone()[0]
    lines += [f'아이템 {n:,}개 보관, 가격을 한 번이라도 관측한 아이템 {priced:,}개. 미관측 아이템은 추천 제외.','',
              '## 내 최저가와 판매처 연결 (수익 추천 아님)','',
              '| 아이템 | 판매 NPC / 위치 | 사이트 표시 가격 | 내 마지막 최저가 | 스냅샷 / 관측일 | 확인 상태 |',
              '|---|---|---:|---:|---|---|']
    rows=db.execute('''SELECT v.*,i.name,p.min_price,p.snapshot_id,p.observed_at,c.eligibility
        FROM current_vendor_offers v JOIN items i USING(item_id)
        JOIN latest_prices p ON p.item_id=v.item_id AND p.source='auctionator' AND p.realm_id=?
        LEFT JOIN character_offer_checks c ON c.offer_id=v.offer_id AND c.character_key=?
        ORDER BY CASE v.verification WHEN 'source_reported_live' THEN 0 WHEN 'forever_branch_unverified' THEN 1 ELSE 2 END,
        v.item_id,v.offer_id LIMIT 20''',(int(rid) if rid else -999,cfg.get('character_key','default'))).fetchall()
    for r in rows:
        eligibility=r['eligibility'] or 'unknown'
        cells=[r['name'] or f"item#{r['item_id']}",f"{r['npc_name'] or 'NPC 미확인'} / {r['zone'] or '위치 미확인'}",
               gold(r['price_copper']) if r['price_copper'] is not None else '미확인',gold(r['min_price']),
               f"#{r['snapshot_id']} / {r['observed_at']}",
               f"{r['verification']}; 구매묶음 {r['purchase_qty'] or '미확인'}; 캐릭터 {eligibility}"]
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
        lines.append(f"- {r['npc_name']} ({r['zone']})에서 {r['name'] or r['item_id']} 구매 → 보유 → "
                     f"내 경매장 등록가는 아직 산정 불가. 사이트 표시 {gold(r['price_copper']) if r['price_copper'] is not None else '미확인'}, "
                     f"내 최저가 {gold(r['min_price'])}; 가격 스냅샷 {count}개. "
                     f"근거: {r['source_url']}, 빌드 {r['game_build'] or '미상'}, 조회 {r['source_fetched_at'] or r['retrieved_at']}.")
        lines.append('  구매 묶음 b, 한 번 구매 가격 c가 확인되면 n개 구매비용 = ceil(n/b)×c + 별도 화폐 비용. '
                     '판매가 p와 판매 개수 n이 확인된 경우에만 순이익 = floor(p×n×0.95)−구매비용. 보증금 손실·재등록 비용 별도.')
    lines += ['','## 정적 관계 / 누락 현황','',
              '| 공급자 | 관계 | 검증 상태 | 건수 |','|---|---|---|---:|']
    for r in db.execute('SELECT provider,kind,verification,COUNT(*) FROM current_static_facts GROUP BY provider,kind,verification'):
        lines.append('| '+' | '.join(map(escape,r))+' |')
    for r in db.execute("SELECT relation_kind,status,COUNT(*) n FROM static_coverage WHERE status NOT IN ('observed','page_observed','documentation') GROUP BY relation_kind,status"):
        lines.append(f"- {r['relation_kind']}: {r['status']} {r['n']}페이지. 관계 없음이라는 뜻이 아닙니다.")
    lines += ['','Blizzard recipe ID / craft spell ID / 도안 item ID / 결과물 item ID는 별도 열입니다. '
              '이름이 같다는 이유로 ID를 연결하지 않습니다. 수량·재료 ID가 빠진 제작은 원가 계산에서 제외합니다.',
              'Forever 제작 추천과 spell: 형식 배운 제작법 필터는 필요한 시세·제작 수량 확보 후 연결할 기능입니다.',
              'ForeverDB 보호 API의 정규화는 승인 키와 실제 응답 검증 후 보완이 필요합니다. 퀘스트 요구 아이템 API 필드는 현재 문서만으로 확인되지 않았습니다.']
    return '\n'.join(lines)+'\n'
