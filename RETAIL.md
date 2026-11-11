# 한국 리테일 경매장 수집기와 기회 리포트

Python 3.10 이상 + 표준 라이브러리 `sqlite3`만 사용합니다. 패키지 설치, 웹 서버, 게임 실행은 필요 없습니다. 이 리테일 가이드의 네트워크 수집은 Blizzard 공식 API를 사용합니다. SavedVariables는 읽기만 합니다.

리테일 기능 안내입니다. Forever 기능과 최신 검증은 [README.md](README.md), [STATIC_VERIFICATION.md](STATIC_VERIFICATION.md)를 참고하세요. 실제 Blizzard 인증 수집은 키 미설정으로 검증하지 못했습니다.

## 빠른 실행

```powershell
# Python 3.10+의 실제 실행 경로를 사용합니다.
python --version
Copy-Item .env.example .env  # 최초 1회; 이미 있는 키 파일을 덮어쓰지 마세요.
# config.json이 없으면 config.example.json을 복사합니다.
# .env에 BNET_CLIENT_ID / BNET_CLIENT_SECRET을 직접 입력합니다.

python collect_api.py
python sync_recipes.py
python import_auctionator.py --inspect
python import_auctionator.py
python analyze.py --output reports/latest.md
```

`.env`, `config.json`, DB, 리포트는 `.gitignore`에 포함됩니다. 키와 토큰은 HTTP 헤더로만 전송하며 DB/로그에 넣지 않습니다. 프로세스 환경변수도 지원하고 `.env`보다 우선합니다. 설정 파일과 `.env`는 기본적으로 스크립트 폴더에 둡니다. `--config <경로>`로 바꾸면 설정 내부 상대경로와 `.env`는 그 설정 파일의 폴더 기준입니다. `--db <경로>`는 호출 위치 기준입니다.

현재 컴퓨터에서 검증한 Python 경로:

```powershell
$pythonExe = 'C:\Python313\python.exe'
& $pythonExe analyze.py
```

### 설정

`config.example.json`에 전체 기본값이 있습니다. `config.json`에서 필요한 값만 덮어쓸 수 있습니다.

| 키 | 의미 |
|---|---|
| `db_path` | 기본 `ah.db`. 모든 실행이 같은 DB를 사용 |
| `realm_name` | 실제 한국 리테일 서버 이름. API가 수집한 `realms.names` 중 하나와 공백 무시 일치 |
| `realm_ids` | 수집할 **연결 서버 ID** 배열. `[]`이면 한국 전체. 개별 realm ID와 다름 |
| `auctionator_path` | `_retail_/WTF/Account/<계정>/SavedVariables/Auctionator.lua` 절대경로 |
| `auctionator_realm_key` | `--inspect`에서 확인한 정확한 서버 키 |
| `auctionator_realm_id` | 해당 한국 연결 서버 ID. 이름도 교차 확인 |
| `game_flavor` | 리테일 스크립트는 `retail`; Forever는 별도 설정/DB |
| `scan_timezone_offset_hours` | Auctionator 날짜 계산에 쓰인 게임 PC 시간대. 한국은 `9` |
| `known_recipes` | 기본 `known_recipes.txt`. 한 줄에 recipe_id 하나, `#` 주석 가능 |
| `item_cache_limit` | 실행당 새 아이템 조회 수. 최대 300회로 강제 제한 |
| `analysis` | 아래 분석 임계값. 생략한 값은 예제 설정 기본값 사용 |

`known_recipes.txt`가 없으면 전체 레시피를 보고 `레시피 필요`로 표시합니다. 파일이 있지만 비어 있으면 배운 레시피가 없는 것으로 해석합니다.

## 파일과 수집 동작

| 파일 | 동작 |
|---|---|
| `collect_api.py` | OAuth → 한국 연결 서버 목록/이름 → 지역 재료 + 서버 경매 → 새 아이템 캐시 |
| `sync_recipes.py` | 전문기술 index → profession → skill-tier → recipe. 성공 후 7일간 생략. `--force`로 다시 조회 |
| `import_auctionator.py` | mtime 변경 시에만 파일 읽기, 마지막 최저가만 저장. `--inspect`는 DB 변경 없이 확인 |
| `import_vendors.py` | `item_id,vendor_price,source,note` CSV를 원자적으로 추가/수정 |
| `analyze.py` | 현재 서버 + 지역 재료 시세로 세 표를 출력. `--output`으로 Markdown 저장 |
| `common.py`, `schema.sql` | HTTP/설정/DB 공통 코드와 스키마 |
| `savedvariables.py` | 실행 기능 없는 Lua 데이터/CBOR 파서 |
| `demo.py`, `tests/` | API 키 없는 합성 검증과 회귀 테스트 |

Blizzard API는 `kr.api.blizzard.com`, `locale=ko_KR`, 경매/서버는 `dynamic-kr`, 아이템/전문기술/레시피는 `static-kr`입니다. 토큰은 `https://oauth.battle.net/token`의 `client_credentials`로 얻습니다. 429/5xx/연결 장애는 최대 3회 요청, 대기 최대 30초입니다.

경매는 `If-Modified-Since`와 DB UNIQUE 제약으로 중복을 막습니다. `Last-Modified`가 없으면 저장하지 않습니다. `snapshots.n_auctions`는 API 응답 전체 경매 수이며, 개별 `prices.n_auctions`는 유효한 즉시구매가가 있는 경매 수입니다. buyout 없는 서버 경매/수량 0/가격 0은 가격 집계에서 제외합니다. 부분 서버 실패 시 성공한 스냅샷은 보존하며 실행은 실패 상태로 기록합니다.

아이템 캐시는 구매가/판매가/분류/귀속 정보와 한국어 이름을 받습니다. 메타데이터 실패는 가격 수집을 취소하지 않으며 다음 실행에 재시도합니다. 레시피는 모든 API 조회 성공 후 한 트랜잭션에서 교체하여 중도 실패 시 기존 목록을 보존합니다. 제작 수량 범위는 최솟값을 쓰며, 수량 미제공·진영별 결과물·단일 결과물 없는 레시피는 저장하되 분석에서 제외합니다.

### Auctionator 실제 구조

설치된 Auctionator v345의 실제 SavedVariables와 다음 애드온 소스를 읽고 구현했습니다:

- `Source/Variables/Main.lua`: DB 버전 8, realm 버전 2, CBOR 직렬화
- `Source/Database/Mixin.lua`: `m` 마지막 최저 단가, `h` 일별 최고 최저가, `l` 일별 최저 최저가, `a` 일별 **최대** 수량
- `Source/Constants/Main.lua`: 날짜는 게임 PC 현지시각 `2020-01-01 00:00`부터의 일수

실제 파일의 realm 값은 Lua 문자열 안의 CBOR 바이트입니다. 비직렬화된 v8/realm v2 테이블도 애드온 소스에 근거하여 지원합니다. Lua를 실행하지 않고 필요한 변수만 파싱합니다. 임의 코드, 미확인 버전/구조, 잘린 파일, 읽는 도중 변경된 파일은 거부합니다. 순수 숫자 item 키만 받고 변형/아이템 레벨/펫 키는 합치지 않습니다.

`m`만 `min_price`에 넣습니다. **중위가·현재 수량·현재 경매 수는 없으므로 NULL**입니다. `observed_at`에는 `h`의 가장 최근 날짜 시작시각을 UTC로 기록합니다. 이는 일 단위 관측일이고 정확한 스캔 시각은 아닙니다. 오래된 가격을 파일 mtime만으로 새 시세로 만들지 않습니다. mtime 변경 후에도 가격 내용이 같으면 정렬된 행의 SHA-256으로 중복을 막습니다.

공식 API의 수량 가중 중위가/수량 통계와 의미가 달라서 Auctionator 가격은 조회용으로 보존하고 **추천 계산에는 사용하지 않습니다**. 두 소스를 섞어 회전율을 만들지 않습니다. 실제 `_retail_` 파일과 한국 서버 매핑을 설정해야 임포트할 수 있습니다.

### 상점 CSV

```powershell
python import_vendors.py vendor_items.csv
```

UTF-8 또는 UTF-8 BOM, 헤더는 `vendor_items.example.csv`를 사용합니다. 단가는 **1개당 동 정수**이고 출처는 필수입니다. `source`에 직접 확인한 NPC/공개 DB URL 등을 넣고, `note`에 위치·재고 제한·평판 요구·묶음 수량과 할인 여부를 적습니다. 수동 입력은 `csv:` 접두어로 출처를 보존하고 API 갱신이 덮어쓰지 않습니다. `vendor_price=0`으로 저장하면 해당 후보를 분석에서 비활성화합니다.

자동 후보는 `items.purchase_price>0`이며 제작 재료로 등장하거나 아이템 분류 5(재료)/7(직업용품)/12(퀘스트)인 아이템입니다. `api:purchase_price` 출처와 확인 필요 메모를 붙입니다. API 가격만으로 NPC 위치·실재고·현재 판매 여부·묶음 단가를 보장할 수 없습니다. 자동 후보도 항상 주의 표시를 하며 실제 확인한 단가는 CSV로 보정합니다. 공개 DB의 NPC 정보를 추측하여 채워 넣지 않았습니다.

## 분석 규칙

- 시장: `realm_name`으로 찾은 연결 서버와 한국 공통 재료(0). 같은 ID가 양쪽에 있으면 지역 재료를 우선합니다. 소스는 공식 API만 사용합니다.
- 현재: **시장별 최신 스냅샷**에 실제 존재하는 아이템만 사용합니다. 사라진 매물에 과거 가격을 붙이지 않습니다. API `Last-Modified`로부터 기본 3시간이 지나면 추천하지 않습니다.
- 스냅샷 중위가: 가격을 수량만큼 가중한 **하위 중위값**. 짝수이고 중앙 두 값이 다르면 낮은 쪽을 택합니다. 서버 단가는 `buyout // quantity`로 소수 동을 버립니다.
- 최근 중위가: 최근 7일의 유효한 스냅샷 가중 중위가들에 대한 하위 중위값. 각 스냅샷의 가중치는 같습니다.
- 판매 단가: `min(최근 중위가, 현재 최저가)`. 판매 순수입은 `판매 단가 × 결과 수량 × 95 // 100`.
- 제작: 재료별 `min(현재 경매장 최저가, 양수 상점 단가)` × 필요 수량을 합산합니다. 경매장 경로는 관측 수량이 필요 수량 이상이어야 합니다. 모든 재료의 구매처와 스냅샷/출처를 표시합니다.
- 제작 원가에는 숙련도/품질/선택 재료/개인 보너스가 들어 있지 않습니다. item_id 단위로만 묶기 때문에 보너스·아이템 레벨·품질이 섞일 수 있습니다. 최저가에 필요한 수량을 전부 살 수 있는지도 확인해야 합니다.
- 상점 되팔기: 1개 기준. 제작 차익/상점 되팔기 모두 기본 순이익 ≥1골드, 이익률 ≥10%인 후보만 표에 표시합니다. 이익률=`순이익/비용`.
- 회전: 같은 시장/아이템의 연속 관측에서 `max(이전 수량 - 다음 수량, 0)` 합계 ÷ 유효 관측 시간 ×24. 기본 6시간 넘는 간격은 제외합니다. 실제 판매와 취소/만료를 구별할 수 없고 재입고로 판매가 가려질 수 있습니다.
- 데이터 충분성: 최근 7일 최소 6개 스냅샷, 24시간 관측 범위, 최소 5개 유효 수량 비교 구간과 합계 24시간이 필요합니다. 원가 현재 가격이 없거나 필수 통계가 없으면 제외합니다.
- 주의: 현재 수량 <20, 경매 수 <5, 최근 중위가의 변동계수(`표준편차/평균`) ≥0.4, 추정 회전 <1개/일, 상점 정보 미확인 등을 표시합니다.
- 정렬: `양수 순이익 × min(1, 하루 판매 추정/결과 수량) / (1+주의 항목 수)` 내림차순. 느린 물건은 하위로 갑니다. 유형별 상위 20개.
- 저점: **현재 관측을 제외한** 과거 7일/30일 중위가 대비 현재 최저가가 25% 이상 저렴한 후보입니다. 각 기간은 최소 6개 표본, 기간 길이 70% 이상의 관측 범위, 기간 일수 70% 이상의 서로 다른 관측일이 필요합니다. 충분한 기간만 근거에 표시합니다. 할인율×`log(1+하루 회전)`을 위험 수로 할인해 정렬합니다.
- 저점의 즉시 판매가는 항상 구매가 이하이므로 수수료 후 손익은 음수입니다. 과거 시세로 복귀한다고 가정하여 가짜 이익을 만들지 않고 **저점 관찰 목록**으로 출력합니다.
- 모든 추천에 정확한 스냅샷 ID 목록/개수, UTC 기간, 현재 최저가, 기준 중위가, 수량 비교 구간/시간, 변동계수를 붙입니다. 누적 데이터가 부족하면 `데이터 부족`과 제외 이유를 표시합니다.

## DB 사전

기본 DB는 이 프로젝트 폴더의 `ah.db`. SQLite WAL 모드, 외래키 활성화, 스키마는 `schema.sql`입니다. **가격은 모두 1개당 동(copper) 정수, 1골드=10,000동**. 보고서의 제작 비용/판매가/순이익만 1회 제작 묶음 기준입니다. 모든 ISO 시각은 UTC `+00:00`입니다. NULL은 정보 없음이며 0과 다릅니다.

| 테이블/뷰 | 컬럼과 의미 |
|---|---|
| `snapshots` | `id` PK; `source` api/auctionator; `realm_id` 연결 서버 ID, 0=지역 재료; `fetched_at` 수집 완료 시각; `last_modified` API 원문 HTTP 헤더 또는 Auctionator `sha256:` 지문; `n_auctions` API 응답 경매 수, Auctionator NULL. UNIQUE(source,realm_id,last_modified) |
| `prices` | PK(`snapshot_id`,`item_id`); `min_price` 최저 단가; `median_price` 수량 가중 하위 중위가; `quantity` 유효 즉시구매 매물 총수량; `n_auctions` 유효 경매 레코드 수; `observed_at` API 스냅샷 시각/Auctionator 마지막 관측일. Auctionator는 중위가/수량/경매 수 NULL |
| `items` | `item_id` PK; `name` 한국어; `purchase_price` 공식 상점 구매가; `sell_price` NPC에게 팔 때 가격; `class_id`,`class_name`,`subclass_id`,`subclass_name` 분류; `binding` API 귀속 type; `updated_at` 캐시 성공 시각; `cache_error` 마지막 실패 시각. 신규 아이템은 item_id만 있는 행부터 생성 |
| `realms` | `realm_id` PK; `names` 연결된 서버 이름들의 **JSON 배열 문자열**. 0은 한국 공통 재료 |
| `recipes` | `recipe_id` PK; `name`,`profession`,`skill_tier` 한국어 이름; `crafted_item_id` 단일 제작품, 미확인 NULL; `crafted_qty` 보수적 제작 수량, 미확인 NULL; `note` 범위/분석 제외 이유 |
| `reagents` | PK(`recipe_id`,`item_id`); `qty` 1회 제작에 필요한 수량 |
| `vendor_items` | 현재 `offer_id` PK, `offer_key` UNIQUE, 여러 NPC 가능. 리테일 `vendor_price`는 기존 단가 후보. 전체 열은 DATABASE.md 참조 |
| `runs` | `id` PK; `started_at`,`finished_at`; `ok` 성공=1, 실패=0, 실행 중=NULL; `note` 스크립트 이름과 실패 종류. 민감한 예외 본문은 기록하지 않음 |
| `meta` | `key` PK, `value`; data_mode=live/fixture, recipes_synced_at, 파일별 Auctionator mtime. 프로그램 내부 상태 |
| `latest_prices` | 소스/시장별 최신 **전체 스냅샷**의 가격만 JOIN한 뷰. `snapshot_id,source,realm_id,fetched_at,last_modified,item_id,name,min_price,median_price,quantity,n_auctions,observed_at`. 이름 미확인은 `item#ID` |

외래키: snapshots→realms, prices→snapshots/items, recipes→items, reagents→recipes/items, vendor_items→items. 스냅샷 삭제 시 가격 행도 삭제되지만 자동 보관기간 삭제 기능은 없습니다. `sell_price`는 상점 매입가로, 상점에서 구매할 원가에 사용하면 안 됩니다.

### 다른 AI/SQL 클라이언트용 예시

SQLite CLI가 있으면 `sqlite3 ah.db "SQL"`. Python 3.12 이상은 별도 설치 없이 `python -X utf8 -m sqlite3 ah.db "SQL"`도 가능합니다. 후자는 한 번에 SQL 문장 하나씩 실행합니다. `-X utf8`은 Windows 콘솔의 한글 출력을 보존합니다.

```sql
SELECT 'snapshots',COUNT(*) FROM snapshots
UNION ALL SELECT 'prices',COUNT(*) FROM prices
UNION ALL SELECT 'items',COUNT(*) FROM items
UNION ALL SELECT 'recipes',COUNT(*) FROM recipes;

SELECT realm_id,names FROM realms;

SELECT source,realm_id,item_id,name,min_price/10000.0 AS min_gold,
       median_price/10000.0 AS median_gold,quantity,observed_at
FROM latest_prices ORDER BY fetched_at DESC,item_id LIMIT 10;

-- 특정 아이템의 시계열. API와 Auctionator 통계를 섞지 않습니다.
SELECT s.id,s.realm_id,p.observed_at,p.min_price,p.median_price,p.quantity
FROM prices p JOIN snapshots s ON s.id=p.snapshot_id
WHERE s.source='api' AND s.realm_id=0 AND p.item_id=190311
ORDER BY p.observed_at DESC LIMIT 168;

SELECT i.item_id,i.name,v.vendor_price,v.source,v.note
FROM vendor_items v JOIN items i USING(item_id) ORDER BY i.item_id;

SELECT * FROM runs ORDER BY id DESC LIMIT 10;
PRAGMA integrity_check;
PRAGMA foreign_key_check;
```

## OS 스케줄러

수집 1시간마다, 레시피 주 1회, SavedVariables 임포트/분석 1시간마다 등록하는 예시입니다. 아래 명령은 안내이며 자동 등록하지 않았습니다. Auctionator 작업은 실제 리테일 파일과 서버를 설정한 뒤 등록하세요.

### Windows 작업 스케줄러 (PowerShell)

```powershell
$projectDir = 'C:\project'
$pythonExe = 'C:\Python313\python.exe'
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours 1)
$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '04:20'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
$apiAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$projectDir\collect_api.py`"" -WorkingDirectory $projectDir
$recipeAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$projectDir\sync_recipes.py`"" -WorkingDirectory $projectDir
$importAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$projectDir\import_auctionator.py`"" -WorkingDirectory $projectDir
$reportAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "`"$projectDir\analyze.py`" --output `"$projectDir\reports\latest.md`"" -WorkingDirectory $projectDir
Register-ScheduledTask -TaskName 'WoW AH Hourly' -Action $apiAction,$reportAction -Trigger $hourly -Settings $settings
Register-ScheduledTask -TaskName 'WoW AH Auctionator' -Action $importAction -Trigger $hourly -Settings $settings
Register-ScheduledTask -TaskName 'WoW AH Recipes' -Action $recipeAction -Trigger $weekly -Settings $settings
```

현재 사용자의 로그인 세션에서 실행하는 예입니다. 작업 스케줄러 UI에서 필요에 따라 로그온하지 않은 상태 실행 조건을 설정하세요. `.env`를 읽으므로 명령행에 비밀키를 넣지 않습니다.

### Linux/macOS cron

`/absolute/path/wow-ah`와 Python 경로를 설치 위치로 바꿉니다. 아래 예시는 수집이 끝나면 분석하며, 같은 수집 작업의 중복 실행은 Linux의 `flock`으로 막습니다.

```cron
0 * * * * cd /absolute/path/wow-ah && flock -n /tmp/wow-ah-collect.lock sh -c '/usr/bin/python3 collect_api.py && /usr/bin/python3 analyze.py --output reports/latest.md' >> collect.log 2>&1
10 * * * * cd /absolute/path/wow-ah && /usr/bin/python3 import_auctionator.py >> auctionator.log 2>&1
20 4 * * 0 cd /absolute/path/wow-ah && /usr/bin/python3 sync_recipes.py >> recipes.log 2>&1
```

macOS에는 `flock`이 기본 제공되지 않으므로 `flock -n /tmp/wow-ah-collect.lock` 부분을 빼거나 launchd의 중복 방지 설정을 사용합니다. 로그/트리거 시간대는 OS 설정을 따르고 DB 시각은 항상 UTC입니다.

## 오프라인 검증

```powershell
python -m unittest discover -s tests -v
python demo.py --dir demo
python -m sqlite3 demo/demo.db "SELECT source,COUNT(*) FROM snapshots GROUP BY source;"
python analyze.py --config demo/config.json --db demo/demo.db
```

`demo.py`는 **합성 시세** 30일 이력과 응답 파일을 만든 뒤 네 실행 스크립트 및 CSV 임포트를 실제 자식 프로세스로 실행합니다. DB에 fixture 표식을 붙이고 실데이터와 혼합을 거부합니다. 기존 demo DB를 덮어쓰지 않으므로 재검증은 `--dir demo/second`처럼 새 폴더를 사용하세요. 스냅샷 신선도 기준은 실제 실행 시각이어서 오래된 demo를 다시 분석하면 정상적으로 데이터 부족이 나옵니다.

실거래 자동화, 게임 클라이언트 제어, 메모리 읽기, 자동 주문 기능은 없습니다.
