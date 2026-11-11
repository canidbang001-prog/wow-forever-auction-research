# 실제 수집 및 검증 기록 — 2026-11-12

[Data from ForeverDB.net](https://foreverdb.net) · [Wowhead Forever](https://www.wowhead.com/forever/ko/database)

## 실제 응답과 합성 검증 구분

- 실제 ForeverDB 승인 키로 HTTP 200 응답을 받아 정규화했습니다. Blizzard OAuth 키와는 별개입니다.
- 실제 인증 API: items/vendors/professions/quests/meta. 원본은 로컬 cache/static, 공개 fixture는 meta.client를 제거한 사본입니다. 원본·사본 SHA-256과 조회일을 함께 기록했습니다.
- 실제 Auctionator DB v8 / realm v2 / PvP: 최신 파일을 다시 읽어 스냅샷 #3에 3,414개 가격을 저장했습니다. min_price만 제공되고 median_price/quantity/n_auctions는 NULL입니다.
- 실제 Wowhead: 앞서 성공한 6328 XML과 브라우저 판매처 9행을 유지합니다. 후속 HTTP 403을 우회하지 않았습니다.
- 테스트 44개 통과: 기존 리테일 22, 실제 공개 응답 6, 합성 정적 경계조건 5, 실제 인증 API 응답 4, 합성 API 안전성/리포트 조건 7.
- 합성 demo/authenticated-api-update는 이번 변경 후 별도 DB에서 수집·레시피·Auctionator·CSV·분석을 모두 다시 실행했습니다(483 snapshots / 965 prices). 실제 시세로 사용하지 않습니다.
- collect_api.py/sync_recipes.py의 실제 Blizzard 실행은 별도 BNET 키 미설정으로 종료했습니다. 이번 키로 Blizzard 데이터 검증에 성공했다고 주장하지 않습니다.

수집 도중 공급자가 1.60.1.70862 → 1.60.1.70888로 갱신되었습니다. 아이템 목록의 이전 빌드 38페이지를 재수집했고, 새 메타 응답으로 나머지 이전 빌드 페이지의 갱신 예약을 실제 검증했습니다. 최신 빌드 아이템 11,100개 이후 페이지는 HTTP 502로 재시도 대기입니다. 판매처 676곳/12개 전문기술은 이전 빌드 수집 이력이며 최신 빌드 완료로 주장하지 않습니다.

## SQLite 행 수

| 테이블/뷰 | 행 수 |
|---|---:|
| items | 14669 |
| snapshots | 3 |
| prices | 9741 |
| static_documents | 1036 |
| static_facts | 42474 |
| recipe_mappings | 2525 |
| current_vendor_offers | 11512 |
| item_seeds | 38173 |
| static_api_pages | 152 |

## API 목록 범위

| 목록 | 읽은 행 / 표시 전체 | 자료 빌드 | 상태 |
|---|---:|---|---|
| https://foreverdb.net/api/v1/items?limit=100 | 11100 / 23634 | 1.60.1.70888 | partial |
| https://foreverdb.net/api/v1/professions/alchemy?limit=100 | 196 / 196 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/blacksmithing?limit=100 | 506 / 506 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/cooking?limit=100 | 132 / 132 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/enchanting?limit=100 | 262 / 262 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/engineering?limit=100 | 275 / 275 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/first-aid?limit=100 | 32 / 32 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/fishing?limit=100 | 3 / 3 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/herbalism?limit=100 | 3 / 3 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/leatherworking?limit=100 | 592 / 592 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/mining?limit=100 | 18 / 18 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/skinning?limit=100 | 3 / 3 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/professions/tailoring?limit=100 | 471 / 471 | 1.60.1.70862 | complete |
| https://foreverdb.net/api/v1/vendors?limit=100&seen=true | 577 / 577 | 1.60.1.70862 | complete |
- api_vendor: {"pending": 676}
- api_quest: {"done": 74, "missing": 3, "pending": 1, "retry": 1}
- api_item: {"done": 3, "pending": 14665}
- 위 state는 현재 작업 대기 상태입니다. 새 빌드 갱신 예약으로 pending이어도 이전 성공 문서가 보존되어 있습니다.
- API 오류 URL: [{"url": "https://foreverdb.net/api/v1/quests/127", "error": "HTTP 502", "retry_at": "2026-11-11T17:27:07+00:00"}, {"url": "https://foreverdb.net/api/v1/quests/8877", "error": "HTTP 404", "retry_at": "2026-11-11T17:21:22+00:00"}, {"url": "https://foreverdb.net/api/v1/quests/8882", "error": "HTTP 404", "retry_at": "2026-11-11T17:21:25+00:00"}, {"url": "https://foreverdb.net/api/v1/quests/8878", "error": "HTTP 404", "retry_at": "2026-11-11T17:21:23+00:00"}, {"url": "https://foreverdb.net/api/v1/items?limit=100&offset=11100", "error": "HTTP 502", "retry_at": "2026-11-11T17:26:51+00:00"}]

complete는 공급자가 표시한 목록의 연속 페이지를 읽었다는 뜻입니다. 게임의 모든 관계를 확인했다는 뜻이 아닙니다. 한 행 probe는 별도 partial 근거로 남습니다. 아이템별 상세 API 작업은 기본 목록과 별도입니다.

## 공급자·관계·검증 상태별 관측 수

| 공급자 | 관계 | 검증 상태 | 건수 |
|---|---|---|---:|
| foreverdb | craft_output | client_catalog | 2506 |
| foreverdb | craft_reagent | unresolved | 8360 |
| foreverdb | item_basic | client_catalog | 24 |
| foreverdb | quest_requires | classic_reference | 1 |
| foreverdb | teaches_spell | client_catalog | 18 |
| foreverdb | vendor_listing | unverified | 715 |
| foreverdb_api | craft_output | client_catalog | 2172 |
| foreverdb_api | craft_output | speculative | 139 |
| foreverdb_api | craft_reagent | client_catalog | 7874 |
| foreverdb_api | craft_reagent | speculative | 543 |
| foreverdb_api | craft_recipe | client_catalog | 2351 |
| foreverdb_api | craft_recipe | speculative | 143 |
| foreverdb_api | item_basic | client_catalog | 11059 |
| foreverdb_api | item_basic | older_build_reference | 3 |
| foreverdb_api | item_basic | speculative | 43 |
| foreverdb_api | quest_basic | source_reported_live | 25 |
| foreverdb_api | quest_basic | unresolved | 50 |
| foreverdb_api | recipe_quest_source | client_catalog | 76 |
| foreverdb_api | recipe_quest_source | unresolved | 4 |
| foreverdb_api | teaches_spell | client_catalog | 1773 |
| foreverdb_api | teaches_spell | hidden_client_reference | 109 |
| foreverdb_api | teaches_spell | speculative | 104 |
| foreverdb_api | vendor_npc | source_reported_live | 578 |
| wowhead | craft_output | forever_branch_unverified | 1 |
| wowhead | craft_reagent | forever_branch_unverified | 1 |
| wowhead | item_basic | forever_branch_unverified | 1 |
| wowhead | teaches_spell | forever_branch_unverified | 1 |
| foreverdb | vendor_offer | classic_reference | 32 |
| foreverdb | vendor_offer | source_reported_live | 34 |
| foreverdb_api | vendor_offer | classic_reference | 2148 |
| foreverdb_api | vendor_offer | not_seen_on_list | 14 |
| foreverdb_api | vendor_offer | source_reported_live | 9275 |
| wowhead | vendor_offer | forever_branch_unverified | 9 |

같은 관계가 여러 URL에서 관측될 수 있으므로 위 행 수를 고유 제작법·아이템 수나 판매량으로 해석하지 않습니다.

## SQLite 실제 최근 가격 샘플

```sql
SELECT snapshot_id,item_id,min_price,median_price,quantity,observed_at FROM latest_prices ORDER BY item_id LIMIT 5;
```
```text
(3, 117, 4, None, None, '2026-11-11T15:00:00+00:00')
(3, 118, 6, None, None, '2026-11-11T15:00:00+00:00')
(3, 159, 16, None, None, '2026-11-11T15:00:00+00:00')
(3, 414, 22, None, None, '2026-11-11T15:00:00+00:00')
(3, 422, 36, None, None, '2026-11-11T15:00:00+00:00')
```

## 실제 판매처·제작 샘플

```text
(6328, 66, 'Tharynn Bouden', 'Elwynn Forest', 400, 1, '[]', 'source_reported_live')
(6328, 1684, 'Khara Deepwater', 'Loch Modan', 400, 1, '[]', 'source_reported_live')
(6328, 3027, 'Naal Mistrunner', 'Thunder Bluff', 400, 1, '[]', 'source_reported_live')
(6328, 7753, 4592, None, 1, 'client_catalog')
('7753', 6289, 1, 'client_catalog')
```

- 6328 도안 → craft spell 7753 → 결과물 4592 ×1, 재료 6289 ×1은 실제 API의 명시적 연결입니다. Blizzard recipe ID는 NULL입니다.
- 주문 3931의 Coarse Dynamite는 실제 API makes가 count=2, min=1, max=3입니다. 고정 결과 수량은 NULL로 보존합니다. 실제 응답에서 추출한 원문은 api_variable_recipe.excerpt.json으로 구별했습니다.
- Merchant's Favor 등의 추가 비용은 currency_costs에 원문 배열로 보존합니다. price=0이어도 무료 구매로 해석하지 않습니다.
- NPC side는 소속, reputationFaction은 할인 평판입니다. 구매 요구 평판과 캐릭터 구매 가능 여부는 미확인입니다.
- 퀘스트 4161의 인증 응답에는 starts/ends/chain만 있고 요구 아이템이 없습니다. 기존 Small Spider Leg ×7은 Classic reference로 유지합니다.
- recipeItemsHidden은 숨겨진 도안 ID 참조입니다. hidden_client_reference/speculative로 구분하며 획득 가능 도안으로 추천하지 않습니다.

## analyze.py 실제 출력 일부

```text
데이터 부족: Auctionator m은 마지막 최저가입니다. 가중 중위가·현재 수량·경매 수가 없어 수익 추천 0개.
하루 판매 추정: 계산 불가. 반복 수입으로 같은 관측을 새 판매량으로 만들지 않습니다.
```

판매처/묶음/통화/내 최저가 조사표 및 재료→결과물 조사표를 생성했습니다. known_recipes.txt는 spell: 형식으로 필터합니다. 가격 관측 없는 아이템은 계속 보관하지만 수익 추천 대상이 아닙니다.

## 무결성과 남은 제약

- PRAGMA integrity_check: ok
- PRAGMA foreign_key_check: []
- 실제 API 파싱/HTTP 실패 작업: 5
- API 한국어 명칭, 라이브 퀘스트 요구 품목, 완전한 구매 요구 조건은 확보되지 않았습니다.
- 일반 아이템 목록은 기본정보입니다. 귀속·NPC 판매가 등 개별 상세 응답이 필요한 필드는 상세 큐에서 추가 수집합니다.
- 정적 수집 연결과 조회 리포트를 검증했으며, Forever 수익 추천에 필요한 중위가·현재 수량 데이터는 여전히 부족합니다.
