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
| `static_api_pages` | `url` PK/큐 FK, `document_id` FK, `collection` offset을 뺀 정규화 URL, `page_offset`, `total`, `observed`, `ids` 실제 페이지 ID JSON 배열. 연속 offset·전체 수·빌드로 목록 완주 검사. 한 행 probe와 100행 본수집은 별도 collection |
| `static_facts` | `id` PK, `document_id` FK, `kind`, `subject_type`, `subject_id` 문자열, `object_type`, `object_id` nullable ID, `label` 원문 이름, `quantity` 확인된 개수/NULL, `verification`, `details` JSON 추가 사실/미확인 이유, `fact_key` 내용 해시. UNIQUE(document_id,fact_key) |
| `item_seeds` | PK(item_id,reason,source_url), `item_id` FK, `reason` 최초 수집 경로, `source_url`, `discovered_at`. 가격 없는 아이템도 유지 |
| `recipe_mappings` | `id` PK, `document_id` FK, `recipe_item_id`, `craft_spell_id`, `output_item_id`, `blizzard_recipe_id`, `verification`, `output_qty`. 각각 다른 ID 공간. NULL Blizzard ID를 spell ID로 대체하지 않음 |
| `character_offer_checks` | PK(offer_id,character_key), `offer_id` FK, `character_key` 사용자 로컬 식별자, `eligibility` unknown/eligible/ineligible, `checked_at`, `note`. 출처의 판매처 검증과 별개 |

`kind`: item_basic(기본정보), vendor_listing(상점 목록의 품목), vendor_npc(목록에 나온 NPC), craft_recipe(제작 주문의 원문·전문기술), teaches_spell(도안→주문), craft_output(주문/익명 제작행→결과물), craft_reagent(→재료), quest_basic(퀘스트 기본정보), recipe_quest_source(제작법을 얻는 퀘스트), quest_requires(퀘스트→요구 품목). 퀘스트 보상과 요구 품목은 다른 관계입니다. `subject_type=recipe_row`는 SSR 페이지+제작 이름이며 공식 레시피 ID가 아닙니다. API 제작 관계는 `subject_type=spell`입니다. `object_id=NULL`인 관계는 이름만 확인되었으므로 ID 결합/원가 계산에 사용하지 않습니다.

`details.payload`는 API의 실제 엔티티/관계 원문입니다. `item_basic`은 betaEvent/speculation/speculationReasons/dataFromBuild/liveClient 등 제공된 필드를 그대로 보존합니다. `craft_recipe`에는 tools, needsNearby, skill, learnedFrom 등의 원문이 남습니다. craft_output의 min/max는 payload에 남고 랜덤 수량의 quantity/output_qty는 NULL입니다. API 원본 envelope의 `meta.client`는 로컬 캐시에만 있으며 사실 테이블/공개 fixture에 복사하지 않습니다.

`learnedFrom.recipeItemsHidden`에서 실제 확인한 정수 아이템 ID도 초기 목록과 명시적 teaches_spell 매핑으로 보관합니다. `hidden_client_reference`는 클라이언트의 참조만 있고 상세·구매/획득 가능 여부가 숨겨진 상태입니다. 이를 라이브에서 얻을 수 있는 도안으로 취급하지 않습니다. 과거 호환 필드 recipeItemsNotInClient를 중복 합산하지 않습니다.

`current_static_facts`는 사실의 모든 열 + source_url,provider,game_version,game_build,fetched_at을 반환하며 active 문서만 포함합니다. 소스별 중복 관측을 통합하지 않습니다. 같은 매핑을 두 공급자가 읽은 것은 2개 근거이지 2개 제작법이 아닙니다.

## 상점의 여러 판매 조건

`vendor_items`는 기존 1품목1행 테이블을 자동 마이그레이션한 테이블입니다. 이전 행은 legacy:item_id 키로 보존됩니다.

| 열 | 의미 |
|---|---|
| `offer_id`, `offer_key` | 정수 PK / 수동 갱신에도 유지할 UNIQUE 키 |
| `item_id` | items FK, 동일 품목 여러 행 허용 |
| `source`, `source_url`, `document_id` | 공급자 또는 csv:출처 / 원본 링크 / 정적 문서 FK |
| `npc_id`, `npc_name`, `zone` | 출처에서 확인한 NPC ID·이름·지역, 없으면 NULL |
| `map_id`, `x`, `y` | API에서 확인한 UiMap ID 및 해당 지역 지도 백분율 좌표. SSR은 누락 가능 |
| `price_copper` | 표시 가격을 동 정수로 정규화. 별도 화폐 환산값 아님 |
| `gold`, `silver`, `copper` | 표시 금·은·동 구성. 파서가 단위를 확인한 경우만 저장 |
| `purchase_qty`, `price_basis` | 1회 구매 개수 / per_purchase, displayed_unknown_bundle, legacy_unit_candidate 등. 알 수 없는 묶음을 1로 가정하지 않음 |
| `vendor_price` | 이전 리테일 단가 후보 호환용. Forever 사이트 표시는 이 열에 쓰지 않음 |
| `stock_state`, `stock_quantity` | unknown / unlimited_displayed / limited_displayed. 표의 ∞는 현재 실제 재고 보장이 아님 |
| `currency_costs` | 별도 통화·교환 아이템 JSON 배열. NULL=미확인, []=별도 비용 없음을 직접 확인 |
| `reputation_required` | 구매 요구 평판 |
| `reputation_discount_faction` | 가격 할인을 적용하는 평판. 구매 요구와 다름 |
| `faction` | 명시된 구매 진영 조건/NULL. NPC의 side/faction·반응 A/H를 구매 가능 여부로 바꾸지 않음 |
| `conditions`, `note` | API는 npc_side(소속), offer(가격·통화·재고 원문), npc_seen, source를 보관. 사람이 읽는 추가 설명은 note |
| `verification` | 아래 출처 검증 상태 |
| `active` | 수동/출처 행 활성 여부 |
| `retrieved_at`, `game_version`, `game_build_override` | CSV 등 문서 없는 관측의 조회일·제품·명시적 빌드. 문서가 있으면 문서 provenance 우선 |

`current_vendor_offers`는 vendor_items 모든 열에 game_build,source_fetched_at을 더합니다. 활성 행 및 활성 문서만 포함합니다. 삭제된 판매처는 새 문서가 성공적으로 파싱될 때 이전 문서 비활성화를 통해 최신 뷰에서 제외되며 원본 이력은 유지됩니다.

검증 상태는 `client_catalog`(클라이언트에 존재), `source_reported_live`(해당 판매 행을 사이트가 라이브 목격이라고 표기), `forever_branch_unverified`(Wowhead Forever 분기이나 라이브 빌드 보장 없음), `classic_reference`, `beta_reference`, `speculative`, `older_build_reference`, `not_seen_on_list`(목격한 판매 목록에 없음), `unresolved`, `unverified`, `manual_unverified`입니다. `source_reported_live`도 내 캐릭터 구매 가능을 뜻하지 않습니다. NPC 전체의 목격만으로 개별 판매 행을 확인 처리하지 않습니다.

조회일은 HTTP 응답을 받은 시각이며 NPC를 게임에서 목격한 시각이 아닙니다. API 판매 행에 실제 목격일·구매 요구 평판·완전한 진영 제한이 없으면 NULL/원문 보존으로 남깁니다. `static_documents.game_build`는 응답 메타의 자료 빌드이며 개별 판매처를 그 빌드에서 직접 재검증했다는 뜻이 아닙니다.

큐 state는 pending/done/running/retry/blocked/missing/parse_error입니다. coverage status의 unknown/partial/unsupported/truncated/parse_error는 관계 없음이 아닙니다. `page_observed`도 한 페이지 확인이며 전체 관계를 검증했다는 의미가 아닙니다. API schema_review_required는 미연결 탐색 원본만 보관했음을 뜻합니다. 인증 API 정규화가 성공하면 해당 probe 상태를 제거합니다. `--reparse-cache`는 원본의 fetched_at을 유지합니다.

## 안전한 조회 예

```sql
SELECT COUNT(*) FROM snapshots;
SELECT * FROM latest_prices ORDER BY item_id LIMIT 5;
SELECT provider,kind,verification,COUNT(*) FROM current_static_facts
GROUP BY provider,kind,verification;
SELECT item_id,npc_id,npc_name,zone,price_copper,purchase_qty,stock_state,
       currency_costs,verification,source_url,game_build,source_fetched_at
FROM current_vendor_offers WHERE item_id=6328;
SELECT r.recipe_item_id,r.craft_spell_id,r.output_item_id,r.blizzard_recipe_id,r.output_qty,r.verification
FROM recipe_mappings r JOIN static_documents d ON d.id=r.document_id WHERE d.active=1;
SELECT label,quantity,object_id,verification,source_url FROM current_static_facts
WHERE kind='quest_requires';
SELECT COUNT(*) FROM items i WHERE NOT EXISTS(SELECT 1 FROM prices p WHERE p.item_id=i.item_id);
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

sqlite3 CLI가 없으면 `python -m sqlite3 ah_forever.db "SELECT COUNT(*) FROM prices;"`를 사용합니다. 테스트 DB는 `meta.data_mode=fixture`, 실제 수집 DB는 live이며 서로 혼합할 수 없습니다. 캐시 원본은 수집 증거이고 외부 명령/실행할 코드가 아닙니다.

## 로컬 시장 화면 (market_dashboard.py)

`config.character_professions.primary/secondary`는 기본 기술 선택과 숙련 정수 또는 `null`을 담습니다. UI 비교 선택은 별도의 localStorage이며 SQLite의 캐릭터 검증 사실을 변경하지 않습니다. `profession_profile.py`와 `action_planner.py`에서 만드는 기술별 후보·가공 비교는 조회 시 계산한 결과이고 별도 실매출 테이블이 아닙니다. `skill.orangeSource=classic`은 참고값으로만 보존하며 클라이언트 근거가 있는 숙련과 구분합니다. API `requiredLevel`은 제작자 학습 레벨인지 문서화되지 않아 캐릭터 필터로 쓰지 않습니다.

DB를 SQLite `mode=ro`로 읽어 `reports/market.html`을 생성합니다. 별도 시세 테이블이나 합성 관측을 만들지 않습니다. `latest_prices`와 해당 `source=auctionator`, 로컬 `realm_id`의 과거 `prices`를 사용합니다. 화면의 `history`는 `(observed_at,min_price)`가 동일한 연속 관측을 합친 표시용 자료입니다. 원본 스냅샷은 보존합니다.

제작 관계는 동일 `document_id`와 craft spell ID로 결합합니다. `character_offer_checks.eligibility`가 미확인인 상점가는 자동 제작 원가에 반영하지 않습니다. 화면의 가정 차액은 DB의 실현 수익 또는 추천 결과가 아니며 가격/수량 가정을 사용한 계산입니다. 상세 기준은 [MARKET.md](MARKET.md)를 참조하세요.


## Auctionator 일별 이력과 수입 근거

`prices`와 `snapshots`의 의미는 그대로 유지합니다. `m`이 같고 과거 `l/h/a`만 바뀌면 새 가격 스냅샷을 만들지 않습니다.

| 테이블 | 열 / 의미 |
|---|---|
| `auctionator_imports` | `id` PK; `realm_id` 로컬 시장; `snapshot_id` 마지막 가격 내용에 대응하는 기존/새 스냅샷; `fetched_at` 파일 해석 UTC; `file_mtime_ns` 원본 파일 mtime; `content_sha256` 선택 시장 전체 내용 해시; `parser_version`; `latest_count`, `daily_count`, `skipped_count`. UNIQUE(realm_id,content_sha256,parser_version) |
| `auctionator_daily` | PK(import_id,item_id,scan_day); `observed_at` 설정 시간대의 관측일 00:00을 UTC로 변환; `low_price` 하루 관측한 최저가들 중 최솟값; `high_price` 그 최댓값; `max_quantity` 하루 최대 관측 수량. 가격은 동 정수; 미제공 값은 NULL |

실제 설치된 Auctionator `Source/Database/Mixin.lua`의 `GetPriceHistory`를 확인했습니다. 일별 low는 `l[day]`가 없으면 `h[day]`, high는 `h[day]`, 수량은 `a[day]`입니다. `a`는 판매량·현재 매물량이 아니며 수량 감소를 체결량으로 쓰지 않습니다. 표의 범위도 전체 매물의 최소/최대가가 아니라 그날 여러 번 기록한 **최저가의 범위**입니다.

업그레이드 최초 실행은 파서 버전 표시가 없으면 mtime이 같아도 1회 재해석합니다. 다음부터 mtime이 같으면 파일 읽기를 생략합니다. 같은 내용은 파일 근거도 중복 제외하며, 과거 이력만 변경되면 가격 스냅샷은 그대로 두고 새 파일 근거를 저장합니다. 실패한 파싱은 기존 자료를 삭제하지 않습니다. 가격이 없는 수량 전용 날짜도 NULL 가격으로 보관합니다. 다른 시장·변형 ID를 기본 item ID로 합치지 않습니다.

```sql
-- 시장별, 품목별, 날짜별 마지막으로 파일에서 확인한 일별 기록
WITH ranked AS (
 SELECT d.*,f.realm_id,f.fetched_at,
        ROW_NUMBER() OVER(PARTITION BY f.realm_id,d.item_id,d.scan_day
                          ORDER BY f.fetched_at DESC,f.id DESC) AS rank
 FROM auctionator_daily d JOIN auctionator_imports f ON f.id=d.import_id
)
SELECT item_id,observed_at,low_price,high_price,max_quantity,import_id
FROM ranked WHERE rank=1 AND realm_id=-1 ORDER BY item_id,scan_day;
```

`market_history.py`는 이 기록을 전부 화면에 포함하고 최근 7일/30일 내 실제 관측 범위와 관측일 수를 표시합니다. 기간 전체가 관측됐다는 뜻은 아닙니다. 조건부 시험 판매 계산의 기준은 최근 7일의 m 기록과 일별 low 중 최솟값입니다. 가중 중위가·실제 판매가로 승격하지 않습니다.
