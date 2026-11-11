# 실제 수집 및 검증 기록 — 2026-11-11

[Data from ForeverDB.net](https://foreverdb.net) · [Wowhead Forever](https://www.wowhead.com/forever/ko/database)

## 실행 구분

- 실제: Auctionator DB v8, realm v2, PvP 시장의 마지막 가격 3,046개를 ah_forever.db에 저장. 재실행 시 mtime 동일로 중복 생략.
- 실제 HTTP: ForeverDB 공개 문서/SSR 86개 응답을 정규화. 전문기술 57페이지와 아이템 24페이지 포함. 보호 API 6개 작업은 키 없음으로 요청하지 않음. 앞선 API meta 단독 탐색에서 실제 401을 확인함.
- 실제 보존 응답: Wowhead 6328 XML은 2026-11-11 11:50 UTC에 받은 HTTP 200 원본을 재사용. 후속 HTTP는 403으로 중단. 새로운 성공 요청으로 계산하지 않음.
- 실제 브라우저 DOM: Wowhead 6328 판매처 표 9행. NPC ID, 위치, 금/은/동 및 무한 재고 표기를 저장. 브라우저 관측은 HTTP 성공 상태로 표현하지 않음.
- 합성: demo/static-update-check/demo.db에서 collect_api.py, sync_recipes.py, import_auctionator.py, import_vendors.py, analyze.py를 모두 실행. 483 snapshots, 965 prices, 4 items, 1 recipe, 2 reagents. 실제 데이터와 별도 DB.
- 실제 Blizzard 수집 실행: collect_api.py와 sync_recipes.py는 키 미설정 오류로 종료. 실제 API 가격/레시피 검증 성공이라고 주장하지 않음.
- 테스트: 33개 통과. 기존 리테일 22개 + 실제 응답 fixture 6개 + 합성 경계조건 5개. 실제 자료 fixture는 tests/fixtures/real에 URL·조회일과 함께 저장.

## SQLite 행 수

| 테이블/뷰 | 행 수 |
|---|---:|
| items | 5293 |
| snapshots | 1 |
| prices | 3046 |
| static_documents | 88 |
| static_facts | 11628 |
| recipe_mappings | 19 |
| current_vendor_offers | 75 |
| item_seeds | 7108 |

## 공급자·관계별 저장 건수

```text
Data from ForeverDB.net (https://foreverdb.net)
정적 정보 = 사실/관계 관측 건수 (고유 아이템 수와 다름)
foreverdb / craft_output / client_catalog: 2506
foreverdb / craft_reagent / unresolved: 8360
foreverdb / item_basic / client_catalog: 24
foreverdb / quest_requires / classic_reference: 1
foreverdb / teaches_spell / client_catalog: 18
foreverdb / vendor_listing / unverified: 715
wowhead / craft_output / forever_branch_unverified: 1
wowhead / craft_reagent / forever_branch_unverified: 1
wowhead / item_basic / forever_branch_unverified: 1
wowhead / teaches_spell / forever_branch_unverified: 1
foreverdb / vendor_offer / classic_reference: 32
foreverdb / vendor_offer / source_reported_live: 34
wowhead / vendor_offer / forever_branch_unverified: 9
큐 blocked: 5295
큐 done: 88
큐 pending: 5345
범위 api / documentation: 1페이지
범위 api_probe / blocked: 6페이지
범위 craft / partial: 58페이지
범위 item_basic / observed: 25페이지
범위 item_xml / blocked: 18페이지
범위 profession_index / observed: 1페이지
범위 quest_index / partial: 1페이지
범위 quest_requires / partial: 1페이지
범위 quest_requires / unsupported: 25페이지
범위 recipe_page / page_observed: 57페이지
범위 vendor_page / page_observed: 1페이지
범위 vendors / partial: 25페이지
범위 vendors / unknown: 1페이지
범위 vendors / unsupported: 1페이지
```

건수는 관계 관측 수이며 고유 제작법 수나 판매량이 아닙니다. 대부분 재료 행의 ID는 아직 미확인입니다. 모든 큐가 끝난 것이 아닙니다.

## 실제 시세 샘플 (sqlite3 조회)

```sql
SELECT snapshot_id,item_id,min_price,median_price,quantity,observed_at FROM latest_prices ORDER BY item_id LIMIT 5;
```
```text
(1, 117, 7, None, None, '2026-11-10T15:00:00+00:00')
(1, 118, 6, None, None, '2026-11-10T15:00:00+00:00')
(1, 159, 17, None, None, '2026-11-10T15:00:00+00:00')
(1, 414, 26, None, None, '2026-11-10T15:00:00+00:00')
(1, 422, 34, None, None, '2026-11-10T15:00:00+00:00')
```

## 판매처·제작·퀘스트 샘플

```text
('foreverdb', 6328, None, 'Nyalah Brightfire', 'Zephras Isle', 400, None, 'source_reported_live')
('foreverdb', 6328, None, 'Tharynn Bouden <Trade Supplies>', 'Elwynn Forest', 400, None, 'source_reported_live')
('foreverdb', 6328, None, 'Tansy Puddlefizz <Fishing Supplier>', 'Ironforge', 400, None, 'source_reported_live')
('foreverdb', 6328, None, 'Khara Deepwater <Fishing Supplies>', 'Loch Modan', 400, None, 'source_reported_live')
('foreverdb', 6328, None, 'Nyoma <Cooking Supplies>', 'Teldrassil', 400, None, 'source_reported_live')
('foreverdb', 6328, None, 'Harn Longcast <Fishing Supplies>', 'Mulgore', 400, None, 'source_reported_live')
('foreverdb', 6328, None, 'Killian Sanatha <Fisherman>', 'Silverpine Forest', 400, None, 'source_reported_live')
('foreverdb', 6328, None, 'Naal Mistrunner <Cooking Supplier>', 'Thunder Bluff', 400, None, 'source_reported_live')
('foreverdb', 6328, None, 'Lizbeth Cromwell <Fishing Supplier>', 'Undercity', 400, None, 'source_reported_live')
('wowhead', 6328, 66, '타린 바우던', '엘윈 숲', 400, None, 'forever_branch_unverified')
('wowhead', 6328, 1684, '카라 딥워터', '모단 호수', 400, None, 'forever_branch_unverified')
('wowhead', 6328, 3027, '나알 미스트러너', '썬더 블러프', 380, None, 'forever_branch_unverified')
(6328, 7753, 4592, None, None, 'client_catalog')
(6328, 7753, 4592, None, None, 'forever_branch_unverified')
('4161', None, 'Small Spider Leg', 7, 'classic_reference')
```

6328 도안 → 7753 제작 주문 → 4592 결과물은 실제 페이지/툴팁의 명시적 연결입니다. Blizzard recipe ID와 결과 수량은 미확인이므로 NULL입니다. 퀘스트 4161의 작은 거미 다리 7개는 Classic reference이며 Forever 라이브 수량으로 채택하지 않았습니다.

## analyze.py 실제 출력 일부

```text
데이터 부족: Auctionator m은 마지막 최저가입니다. 가중 중위가·현재 수량·경매 수가 없어 수익 추천 0개.
하루 판매 추정: 계산 불가. 반복 수입으로 같은 관측을 새 판매량으로 만들지 않습니다.
```

상점·내 가격 조사표는 생성하지만 구매묶음·구매조건·판매가 검증 전에는 순이익 숫자를 추천으로 표시하지 않습니다.

## 무결성과 남은 제약

- PRAGMA integrity_check: ok
- PRAGMA foreign_key_check: []
- Wowhead 목록은 23,566개 중 1,000개만 표시되는 것을 브라우저에서 확인. 전체 목록을 수집했다고 기록하지 않음.
- ForeverDB 공개 API 문서는 확보했지만 보호 응답의 필드 정규화는 승인 키와 응답 검증 후 구현 필요.
- 재료 ID, 결과 수량, 완전한 상점 조건, 라이브 퀘스트 요구 관계 중 일부는 아직 미해결.
- 수집기/조사표를 실행 검증했지만 최초 명세의 전체 정적 관계와 Forever 수익 추천 기능 완료를 뜻하지 않음.
