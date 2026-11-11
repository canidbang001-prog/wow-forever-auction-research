# SQLite 데이터 사전

[Data from ForeverDB.net](https://foreverdb.net) · [Wowhead Forever](https://www.wowhead.com/forever/ko/database)

Python 3 표준 `sqlite3`로 읽습니다. 기본 파일은 리테일 `ah.db`, Forever `ah_forever.db`입니다. 상대경로는 설정 파일 폴더 기준, `--db`는 호출 위치 기준입니다. 모든 `*_at`은 ISO UTC, 가격은 정수 동(1골드=10,000동), 수량은 아이템 개수입니다. NULL은 미확인이며 0이 아닙니다. 텍스트 JSON은 `json.loads`로 해석합니다.

## 가격과 기존 리테일 자료

| 테이블 | 열 설명 |
|---|---|
| `meta` | `key` PK, `value` 텍스트. `data_mode=live/fixture`, `game_flavor=retail/forever`, 빌드·임포트 mtime·재시도 시간을 보관 |
| `runs` | `id` PK, `started_at`, `finished_at`, `ok` 1/0/NULL, `note` 실행 종류. 정적 run 성공은 묶음 실행 성공이며 전체 큐 완료가 아님 |
| `realms` | `realm_id` PK, `names` JSON 문자열 배열. 0은 한국 리테일 공통 재료, 양수는 Blizzard 연결 서버, 음수는 Forever 로컬 시장 |
| `items` | `item_id` PK, `name`, `purchase_price` 공급자의 기본 구매가 후보, `sell_price` NPC에 파는 가격, `class_id/class_name`, `subclass_id/subclass_name`, `binding`, `updated_at` Blizzard 캐시 조회일, `cache_error` 최근 실패. 정적 자료 provenance는 `static_facts` 참조 |
| `snapshots` | `id` PK, `source` api/auctionator, `realm_id` FK, `fetched_at` 수집시각, `last_modified` API 헤더 또는 Auctionator 내용 SHA-256, `n_auctions` 전체 경매 수. UNIQUE(source,realm_id,last_modified) |
| `prices` | PK(snapshot_id,item_id), 두 열 FK. `min_price`, `median_price` 수량 가중 하위 중위가, `quantity`, `n_auctions`, `observed_at` 실제 가격 관측시각. Auctionator의 마지막 3개 통계는 NULL, 관측일은 로컬 일자 단위 |
| `recipes` | `recipe_id` Blizzard ID PK, `name`, `profession`, `skill_tier`, `crafted_item_id` FK, `crafted_qty`, `note`. Forever spell ID를 여기에 넣지 않음 |
| `reagents` | PK(recipe_id,item_id), 두 열 FK, `qty` 제작 1회 필요량 |

`latest_prices`는 source/realm별 **마지막 스냅샷에 존재하는 아이템만** 반환합니다. 열: snapshot_id,source,realm_id,fetched_at,last_modified,item_id,name,min_price,median_price,quantity,n_auctions,observed_at. 사라진 품목을 예전 스냅샷으로 보충하지 않습니다.

## 정적 자료

| 테이블 | 열 설명 |
|---|---|
| `static_documents` | `id` PK, `url`, `provider`, `game_version` 제품(forever), `game_build` 알려진 빌드/NULL, `fetched_at`, `sha256`, `cache_path`, `http_status` HTTP 상태(0=브라우저 DOM 관측), `verification` 수집 방법, `active` 마지막 성공본 여부. UNIQUE(url,sha256) |
| `static_queue` | `url` PK, `provider`, `kind` 페이지 형식, `entity_id` 해당 item/quest ID, `priority` 작은 값 우선, `state`, `attempts`, `checked_at`, `retry_at`, `error` 비밀 없는 상태 요약, `document_id` FK |
| `static_coverage` | PK(url,relation_kind), `url` 큐 FK, `status`, `expected` 사이트 표시 전체 수/NULL, `observed` 현재 페이지에서 읽은 수, `note`, `checked_at`. profession total을 페이지마다 반복하므로 expected 합계 금지 |
| `static_facts` | `id` PK, `document_id` FK, `kind`, `subject_type`, `subject_id` 문자열, `object_type`, `object_id` nullable ID, `label` 원문 이름, `quantity` 확인된 개수/NULL, `verification`, `details` JSON 추가 사실/미확인 이유, `fact_key` 내용 해시. UNIQUE(document_id,fact_key) |
| `item_seeds` | PK(item_id,reason,source_url), `item_id` FK, `reason` 최초 수집 경로, `source_url`, `discovered_at`. 가격 없는 아이템도 유지 |
| `recipe_mappings` | `id` PK, `document_id` FK, `recipe_item_id`, `craft_spell_id`, `output_item_id`, `blizzard_recipe_id`, `verification`, `output_qty`. 각각 다른 ID 공간. NULL Blizzard ID를 spell ID로 대체하지 않음 |
| `character_offer_checks` | PK(offer_id,character_key), `offer_id` FK, `character_key` 사용자 로컬 식별자, `eligibility` unknown/eligible/ineligible, `checked_at`, `note`. 출처의 판매처 검증과 별개 |

`kind`: item_basic(기본정보), vendor_listing(상점 목록의 품목), teaches_spell(도안→주문), craft_output(주문/익명 제작행→결과물), craft_reagent(→재료), quest_requires(퀘스트→요구 품목). `subject_type=recipe_row`의 문자열은 출처 페이지+제작 이름이며 공식 레시피 ID가 아닙니다. `object_id=NULL`인 관계는 이름만 확인되었으므로 ID 결합/원가 계산에 사용하지 않습니다.

`current_static_facts`는 사실의 모든 열 + source_url,provider,game_version,game_build,fetched_at을 반환하며 active 문서만 포함합니다. 소스별 중복 관측을 통합하지 않습니다. 같은 매핑을 두 공급자가 읽은 것은 2개 근거이지 2개 제작법이 아닙니다.

## 상점의 여러 판매 조건

`vendor_items`는 기존 1품목1행 테이블을 자동 마이그레이션한 테이블입니다. 이전 행은 legacy:item_id 키로 보존됩니다.

| 열 | 의미 |
|---|---|
| `offer_id`, `offer_key` | 정수 PK / 수동 갱신에도 유지할 UNIQUE 키 |
| `item_id` | items FK, 동일 품목 여러 행 허용 |
| `source`, `source_url`, `document_id` | 공급자 또는 csv:출처 / 원본 링크 / 정적 문서 FK |
| `npc_id`, `npc_name`, `zone` | 출처에서 확인한 NPC ID·이름·지역, 없으면 NULL |
| `map_id`, `x`, `y` | 지도 ID 및 백분율 좌표. 현재 공개 SSR 파서는 좌표를 제공하지 않음 |
| `price_copper` | 표시 가격을 동 정수로 정규화. 별도 화폐 환산값 아님 |
| `gold`, `silver`, `copper` | 표시 금·은·동 구성. 파서가 단위를 확인한 경우만 저장 |
| `purchase_qty`, `price_basis` | 1회 구매 개수 / per_purchase, displayed_unknown_bundle, legacy_unit_candidate 등. 알 수 없는 묶음을 1로 가정하지 않음 |
| `vendor_price` | 이전 리테일 단가 후보 호환용. Forever 사이트 표시는 이 열에 쓰지 않음 |
| `stock_state`, `stock_quantity` | unknown / unlimited_displayed / limited_displayed. 표의 ∞는 현재 실제 재고 보장이 아님 |
| `currency_costs` | 별도 통화·교환 아이템 JSON 배열. NULL=미확인, []=별도 비용 없음을 직접 확인 |
| `reputation_required` | 구매 요구 평판 |
| `reputation_discount_faction` | 가격 할인을 적용하는 평판. 구매 요구와 다름 |
| `faction` | 출처의 진영 조건/NULL. NPC 반응 A/H를 구매 가능 여부로 바꾸지 않음 |
| `conditions`, `note` | 원문 조건 JSON / 사람이 읽는 추가 설명 |
| `verification` | 아래 출처 검증 상태 |
| `active` | 수동/출처 행 활성 여부 |
| `retrieved_at`, `game_version`, `game_build_override` | CSV 등 문서 없는 관측의 조회일·제품·명시적 빌드. 문서가 있으면 문서 provenance 우선 |

`current_vendor_offers`는 vendor_items 모든 열에 game_build,source_fetched_at을 더합니다. 활성 행 및 활성 문서만 포함합니다. 삭제된 판매처는 새 문서가 성공적으로 파싱될 때 이전 문서 비활성화를 통해 최신 뷰에서 제외되며 원본 이력은 유지됩니다.

검증 상태는 `client_catalog`(클라이언트에 존재), `source_reported_live`(사이트가 라이브 목격이라고 표기), `forever_branch_unverified`(Wowhead Forever 분기이나 라이브 빌드 보장 없음), `classic_reference`, `beta_reference`, `unresolved`, `unverified`, `manual_unverified`입니다. `source_reported_live`도 내 캐릭터 구매 가능을 뜻하지 않습니다.

큐 state는 pending/done/running/retry/blocked/missing/parse_error입니다. coverage status의 unknown/partial/unsupported/truncated/parse_error는 관계 없음이 아닙니다. `page_observed`도 한 페이지 확인이며 전체 관계를 검증했다는 의미가 아닙니다. API schema_review_required는 원본 보관만 되었음을 뜻합니다.

## 안전한 조회 예

```sql
SELECT COUNT(*) FROM snapshots;
SELECT * FROM latest_prices ORDER BY item_id LIMIT 5;
SELECT provider,kind,verification,COUNT(*) FROM current_static_facts
GROUP BY provider,kind,verification;
SELECT item_id,npc_id,npc_name,zone,price_copper,purchase_qty,stock_state,
       currency_costs,verification,source_url,game_build,source_fetched_at
FROM current_vendor_offers WHERE item_id=6328;
SELECT recipe_item_id,craft_spell_id,output_item_id,blizzard_recipe_id,output_qty,verification
FROM recipe_mappings;
SELECT label,quantity,object_id,verification,source_url FROM current_static_facts
WHERE kind='quest_requires';
SELECT COUNT(*) FROM items i WHERE NOT EXISTS(SELECT 1 FROM prices p WHERE p.item_id=i.item_id);
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

sqlite3 CLI가 없으면 `python -m sqlite3 ah_forever.db "SELECT COUNT(*) FROM prices;"`를 사용합니다. 테스트 DB는 `meta.data_mode=fixture`, 실제 수집 DB는 live이며 서로 혼합할 수 없습니다. 캐시 원본은 수집 증거이고 외부 명령/실행할 코드가 아닙니다.
