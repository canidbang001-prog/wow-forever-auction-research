<div align="center">

# WoW Forever Auction Research

**내 경매장 가격에, 출처가 남는 아이템 정보를 더합니다.**

Python 3 · SQLite · 한국어 리포트 · 읽기 전용 수집

[빠른 시작](#빠른-시작) · [DB 명세](DATABASE.md) · [검증 기록](STATIC_VERIFICATION.md)

### [Data from ForeverDB.net](https://foreverdb.net)

정적 자료: [ForeverDB](https://foreverdb.net) · [Wowhead Forever](https://www.wowhead.com/forever/ko/database)

</div>

---

개인용 경매장 조사 도구입니다. Auctionator가 저장한 내 가격과 공개 아이템·상점·제작·퀘스트 자료를 SQLite에 모아, 근거를 다시 확인할 수 있는 한국어 리포트를 만듭니다. 별도 Python 패키지는 필요 없습니다. 게임 클라이언트 조작, 메모리 읽기, 자동 거래 기능은 없습니다.

> **실제 인증 API 연결 검증.** ForeverDB 아이템 목록, 전문기술 재료·결과물, NPC 판매 목록을 수집합니다. 공급자 목록 완주와 게임의 모든 관계 확인은 다릅니다. 퀘스트 요구 아이템·일부 구매 조건은 API에서 제공되지 않으며, Auctionator 최저가만으로 수익이나 판매량을 확정하지 않습니다.

## 한국 PvP 시장 화면

`reports/market.html`에서 실제 가격을 검색하고 제작 원가·NPC 판매처·가정 마진을 비교할 수 있습니다. 아이템을 누르면 가격 기록과 구매 조건이 열립니다. 가격이 없는 품목도 목록에 보관합니다. 판매량과 7일/30일 가중 중위가는 현재 파일에서 제공되지 않아 미확인으로 표시합니다.

```powershell
.\refresh_market.ps1
# 또는
python market_dashboard.py --config config.forever.json --output reports/market.html
```

[시장 화면 사용법과 계산 기준](MARKET.md)을 참고하세요. 이 HTML은 실제 개인 시장 자료를 포함하므로 공개 소스 묶음에 넣지 않습니다. 정적 API 키와 경매 시세는 서로 다른 데이터입니다.

화면 상단의 반영 범위에서 최신 가격·누적 가격·일별 이력 건수를 확인할 수 있습니다. 주·보조 전문기술을 체크하고 숙련을 입력하면 내 전문기술 비교표, 조건부 제작 후보, 파밍 목표가 즉시 바뀝니다. 차액·이익률·필요 자금으로 정렬하고 생선·천의 원물/가공 경로, 직접 마부 서비스 견적도 비교합니다. 선택은 이 브라우저에 저장되며 시간당 효율은 실제 시간·획득량·판매 결과가 필요합니다.

## 무엇을 볼 수 있나요?

| 기능 | 동작 |
|---|---|
| 내 시장 기록 | 마지막 최저가와 모든 일별 가격 범위·최대 수량을 별도 저장, mtime/내용 중복 제외 |
| 정적 정보 | 아이템·판매처·제작·퀘스트 관계와 원본 응답 보관 |
| 판매처 비교 | 같은 품목의 여러 NPC·가격·조건을 별도 행으로 유지 |
| 확인 상태 | 클라이언트 자료 / 라이브 목격 / 클래식 참고 / 미확인 구분 |
| 다시 실행 | 캐시·대기열·파싱 실패·표시 상한을 기록해 중단 지점부터 재개 |
| 보수적 분석 | 근거가 부족하면 **데이터 부족**으로 표시, 매수 추천 제외 |

```text
내 Auctionator.lua ─ 가격 관측 ─┐
                                ├─ SQLite ─ 한국어 조사 리포트
ForeverDB / Wowhead ─ 정적 근거 ─┘           출처 · 빌드 · 조회일 · 미확인 필드
```

## 빠른 시작

Python **3.10 이상**, 표준 라이브러리만 사용합니다. 테스트는 저장소의 `tests/`에 있습니다. `python3 -m unittest discover -s tests`로 바로 실행합니다. `source.zip`은 저장소보다 낡은 스냅샷입니다. 다시 만드는 스크립트는 없고, 압축을 풀어 현재 소스나 `tests/`를 대체하지 않습니다.

```powershell
Copy-Item config.forever.example.json config.forever.json
# auctionator_path에 실제 Auctionator.lua 경로 입력
# auctionator_realm_key는 --inspect에서 확인한 키 사용
python import_auctionator.py --config config.forever.json --inspect
python import_auctionator.py --config config.forever.json
# .env에 FOREVERDB_API_KEY를 입력한 뒤 인증 API로 수집
python sync_static.py --config config.forever.json --provider foreverdb_api --catalog-only --max-requests 1100
python analyze.py --config config.forever.json --output reports/forever.md
```

경매장을 조회한 뒤 `/reload` 또는 로그아웃해야 파일이 저장됩니다. 경로 예: `<WoW>/_forever_/WTF/Account/<계정>/SavedVariables/Auctionator.lua`.

기본 Forever DB는 **`ah_forever.db`**이며 설정 파일 폴더 기준입니다. 리테일은 별도 `ah.db`입니다. 같은 파일에 제품을 섞으면 실행을 거절합니다. Forever의 음수 `realm_id`는 로컬 시장 번호입니다. `PvP`만으로 실제 서버·지역·진영이 확인된 것은 아닙니다.

```powershell
# 다음 묶음 수집 / 상태 확인
python sync_static.py --config config.forever.json --provider foreverdb_api --catalog-only --max-requests 100
python sync_static.py --config config.forever.json --status
# 패치 직후: 실제 확인한 빌드를 입력
python sync_static.py --config config.forever.json --provider foreverdb_api --catalog-only --patch <확인한-빌드> --max-requests 1100
# 파서를 고친 뒤 실패한 작업 재개
python sync_static.py --config config.forever.json --retry-errors --max-requests 60
# 파서 수정 후 이미 받은 API 원본을 재해석 (네트워크 요청 없음)
python sync_static.py --config config.forever.json --reparse-cache --max-requests 0
```

초기 큐는 경매장 아이템뿐 아니라 아이템 전체 목록·목격된 NPC 목록·제작 재료·결과물·도안을 포함합니다. `--catalog-only`는 API 전체 목록, 전문기술, NPC별 전체 판매 목록, 연관 퀘스트를 읽습니다. 아이템별 상세 응답은 별도 대기열에 보관하며 이 옵션을 빼면 이어서 수집합니다. 목록의 기본정보에는 판매가·귀속 상세가 없을 수 있습니다. 키가 없으면 공개 SSR/XML 수집을 사용할 수 있습니다.

API는 `limit=100`과 실제 `meta.next`를 따라갑니다. offset·필터·전체 수·행 수·중복 ID를 검사하고, `--status`에서 연속된 페이지와 빌드·전체 수가 일치해야 목록을 complete로 표시합니다. 완주해도 그것은 **공급자가 제공한 목록의 범위**입니다. 한 행짜리 탐색(probe)은 별도 partial 근거입니다. ID 없는 SSR 재료는 이름으로 연결하지 않습니다.

## 출처와 현재 제약

| 공급자 | 확인한 접근 | 한계 |
|---|---|---|
| Wowhead Forever | 판매처 표와 [문서화된 아이템 XML](https://www.wowhead.com/tooltips) | XML에 전체 관계가 없음. 현재 HTTP 403. 이전 정상 XML과 실제 브라우저 표를 별도 근거로 보존 |
| ForeverDB 공개 페이지 | 아이템·상점·전문기술·퀘스트 SSR | NPC ID·묶음·통화·평판 등 일부 누락, 제작 재료는 이름만 있는 경우가 많음 |
| [ForeverDB API](https://foreverdb.net/developers) | 실제 키로 items/vendors/professions/quests/meta HTTP 200 확인 | `forever_api.py`가 실제 필드를 정규화. NPC 좌표·묶음·별도 화폐·재고·할인 평판, 제작 ID·수량 보관. 퀘스트 요구 품목은 미제공 |
| Blizzard 한국 리테일 API | `dynamic-kr` / `static-kr` | `collect_api.py` 전용. Forever 경매장 대신 쓰지 않음. [리테일 가이드](RETAIL.md) |
| Blizzard WoW Forever KR | `dynamic-forever-kr`, 룰셋 1, `ko_KR` | `blizzard_collector.py`. 기본 수집은 색인의 호드와 검은바다. 얼라이언스는 설정 전에는 제외 |

`.env`에 `FOREVERDB_API_KEY`, Blizzard용 `BNET_CLIENT_ID` / `BNET_CLIENT_SECRET`을 입력합니다. 같은 변수 이름을 리테일 수집과 Forever 경매장 수집이 쓰며, 값은 로그·DB·예제 설정에 남기지 않습니다. `fdb_` 키는 ForeverDB용이며 Blizzard OAuth 키가 아닙니다. 기존 키 파일을 덮어쓰지 마세요. 키는 로그·DB·공개 저장소에 넣지 않습니다. 인증 요청은 ForeverDB API에만 보내고 HTTP 리다이렉트에는 전달하지 않습니다. API 캐시의 `meta.client`는 개발자 식별자이므로 공개 fixture에서는 제거합니다. 원본 캐시는 공개하지 않습니다.

ForeverDB의 한국어 API는 문서화되어 있지 않습니다. 기존 한국어 이름이 있으면 보존하고 없으면 API 영문명을 사용합니다. 퀘스트 4161의 실제 응답에는 시작·완료 NPC와 연결 퀘스트가 있지만 **요구 아이템 필드가 없습니다**. 제작 도안을 주는 퀘스트는 `recipe_quest_source`이고 퀘스트 요구 아이템이 아닙니다.

정적 응답은 로컬 `cache/static`에 저장해 7일간 재사용합니다. 소스 빌드는 하루 주기로 확인하며, 다른 API 응답에서 수집 도중 새 빌드를 발견해도 이전 빌드 페이지 갱신을 예약합니다. 같은 빌드에서 목록 전체 수가 달라져도 앞쪽 페이지를 재확인합니다. 서로 다른 빌드/전체 수를 합쳐 목록 완주로 표시하지 않습니다. 캐시 재해석은 최초 조회일을 유지합니다. 최소 요청 간격 1.1초, 로컬 하루 9,500회 상한을 적용합니다. 429 `Retry-After`는 다음 실행에도 유지합니다. 401/403 이후 공급자를 중단하며 접근 정상화 확인 후 `--retry-provider foreverdb_api` 또는 `wowhead`로 재개합니다. 404·파싱 실패는 관계 없음이 아니며 이전 성공 자료를 보존합니다.

## 데이터를 읽는 방법

가격은 **동(copper) 정수**, `1g = 100s = 10,000c`입니다. 출력만 골드로 표시합니다. `NULL`은 미확인이며 0이나 관계 없음과 다릅니다.

상점 `price_copper`는 사이트 표시 가격입니다. `purchase_qty`가 없으면 개당 원가를 계산하지 않습니다. 금·은·동, 묶음, 재고, 별도 화폐, 평판·진영은 별도 열입니다. 판매처 출처 검증과 내 캐릭터 구매 가능 여부는 `verification` / `character_offer_checks`로 분리합니다.

NPC의 side/faction은 소속 정보로 `conditions.npc_side`에 저장하며 구매 제한 `faction`으로 단정하지 않습니다. `reputationFaction`은 할인 평판이고 구매 요구 평판이 아닙니다. NPC가 목격되어도 해당 판매 행의 `seenInForever`가 없으면 라이브 판매 확인으로 올리지 않습니다. `costs` 누락은 NULL, 실제 빈 배열은 []로 구별합니다. 클라이언트 자료·추측 품목·베타 이벤트·이전 빌드 자료는 별도 상태로 보존합니다.

Forever에서 `known_recipes.txt`는 `spell:7753`처럼 제작 주문 ID를 한 줄에 하나씩 적습니다. 파일이 없으면 전체 조사표에 “레시피 필요”를 표시합니다. Blizzard recipe ID, 도안 item ID, craft spell ID, 결과물 item ID를 서로 대입하지 않습니다. 제작 수량이 min/max 범위이면 고정 수량을 만들지 않습니다.

`static_documents`는 URL·공급자·게임 제품·빌드·조회일·원본 SHA-256·캐시 경로를 저장합니다. 각 사실·관계는 이 문서를 참조합니다. `current_static_facts`, `current_vendor_offers`는 마지막 성공한 자료만 보여줍니다. 갱신 실패 시 이전 자료와 조회일을 보존합니다. 상세 열·예제 쿼리는 [DATABASE.md](DATABASE.md)에 있습니다.

```sql
SELECT provider,kind,verification,COUNT(*) FROM current_static_facts
GROUP BY provider,kind,verification;

SELECT v.item_id,i.name,v.npc_name,v.zone,v.price_copper,v.purchase_qty,
       v.verification,p.min_price,p.snapshot_id,p.observed_at
FROM current_vendor_offers v JOIN items i USING(item_id)
JOIN latest_prices p USING(item_id)
WHERE p.source='auctionator' AND p.realm_id=-1;

SELECT relation_kind,status,expected,observed,note FROM static_coverage;
SELECT state,COUNT(*) FROM static_queue GROUP BY state;
```

CSV 보강: `python import_vendors.py --config config.forever.json vendor_offers.example.csv`. 예제는 헤더만 있습니다. 판매처마다 고유 `offer_key`를 부여하고 수정할 때 같은 키를 씁니다. 수동 값은 `manual_unverified`로 저장합니다.

## 분석 규칙

리테일 중위가는 수량 가중 하위 중위값입니다. 판매가는 현재 최저가와 최근 7일 스냅샷 중위값들의 하위 중위값 중 낮은 값, 순이익은 수수료 5% 차감 후 비용을 뺀 값입니다. 수량 감소는 취소·만료와 구분할 수 없는 회전 추정치입니다. 관측 기간·스냅샷 수·변동성 기준은 `config.example.json`에 있습니다. 각 종류 상위 20개, 사용한 스냅샷 ID와 개수를 표시합니다.

현재 Forever 입력인 Auctionator `m`은 마지막 최저가이며 `a`는 하루 최대 수량입니다. **가중 중위가·현재 수량·경매 수가 없으므로 Forever 리포트는 조사표와 데이터 부족 사유를 보여주며 수익 추천을 생성하지 않습니다.** 제작·퀘스트 용도는 수요 근거이며 판매량으로 환산하지 않습니다.

구매 묶음·재료 ID/수량·결과 수량이 확인되어야 원가를 계산할 수 있습니다. Blizzard recipe ID, craft spell ID, 도안 item ID, 결과물 item ID는 별도 열입니다. 리테일 `known_recipes.txt`는 Blizzard recipe ID 목록, Forever 조사표는 `spell:` 목록으로 필터합니다. 수익 추천은 필요한 시세 통계가 추가로 필요합니다. 오늘 할 일의 공식 순이익은 보증금 조건과 체결 확률이 모두 있을 때만 기대 보증금 손실을 뺍니다. 설정이 null이면 데이터 부족이며 0으로 빼지 않습니다. 사용자가 `listing_cost.sell_through_pct`를 적으면 그 값이 우선입니다. 비어 있고, 경매장 번호를 하나 지정했으며, 그 경매장의 블리자드 이력이 기준을 넘을 때만 소멸 추정(판매 또는 취소, 확정 판매 아님)을 채웁니다. 기준 미만이면 그대로 데이터 부족입니다. 이 식은 Forever 보증금 공식이 아닙니다. 중립 경매장 수수료도 따로 만들지 않습니다. 이동·학습 비용은 계속 별도입니다. 기본값과 키 이름은 `config.forever.example.json`과 [MARKET.md](MARKET.md)에 있습니다.

## 파일 구성과 검증

| 파일 | 역할 |
|---|---|
| `sync_static.py`, `forever_api.py`, `static_parsers.py`, `static_store.py` | 수집·인증 API·캐시·파싱·증거·마이그레이션 |
| `import_auctionator.py`, `savedvariables.py` | Lua/CBOR 데이터를 실행하지 않고 읽기 |
| `collect_api.py`, `sync_recipes.py` | 한국 리테일 공식 API (`dynamic-kr`) |
| `blizzard_collector.py` | WoW Forever KR 공식 경매장 스냅샷 (`dynamic-forever-kr`) |
| `analyze.py`, `forever_report.py` | 리테일 기회 분석 / Forever 조사표 |
| `import_vendors.py` | 여러 판매처 CSV 추가·수정 |
| `common.py`, `schema.sql`, `static_schema.sql` | 설정·SQLite·스키마 |
| `demo.py`, `tests/` | 합성 데이터와 실제 응답 fixture 검증 |

`python -m unittest discover -s tests -v`로 검사합니다. 실제/합성 검증을 구분한 결과는 [STATIC_VERIFICATION.md](STATIC_VERIFICATION.md)에 있습니다. 개인 설정, SavedVariables, DB, 키는 공개 배포하지 않습니다.

## OS 스케줄러

아래 경로를 실제 프로젝트/Python 경로로 바꿉니다. 작업을 자동 등록하지 않고 명령을 안내합니다.

```powershell
schtasks /Create /TN "WoW Forever Prices" /SC HOURLY /MO 1 /TR '"C:\Python313\python.exe" "C:\project\import_auctionator.py" --config "C:\project\config.forever.json"'
schtasks /Create /TN "WoW Forever Static" /SC WEEKLY /D SUN /ST 04:00 /TR '"C:\Python313\python.exe" "C:\project\sync_static.py" --config "C:\project\config.forever.json" --provider foreverdb_api --catalog-only --max-requests 1100'
```

```cron
0 * * * * /usr/bin/python3 /path/project/import_auctionator.py --config /path/project/config.forever.json
*/10 * * * * /usr/bin/python3 /path/project/blizzard_collector.py --config /path/project/config.forever.json --env /path/outside/bnet.env --db /path/outside/forever-ah.db
0 4 * * 0 /usr/bin/python3 /path/project/sync_static.py --config /path/project/config.forever.json --provider foreverdb_api --catalog-only --max-requests 1100
```

주간 실행 한 번으로 대기열 전체가 끝나지는 않습니다. 초기 수집은 제한된 묶음을 반복 실행하고 패치 직후에는 `--patch`로 갱신합니다. 블리자드 수집은 10분마다(`*/10`) 확인합니다. 요청에 `If-Modified-Since`를 보내고, `Last-Modified`가 이전 스냅샷과 같거나 304이면 가격 행은 다시 넣지 않고 확인한 사실만 남깁니다. 라이브 DB와 `.env`는 저장소 밖에 둡니다.

## 블리자드 Forever 경매장 수집

게임 안 검색 없이 공식 경매장 시각별 가격을 남기는 경로입니다. 사용자 시장은 한국, PvP, 호드입니다. 어느 호드 경매장 번호가 내 서버인지는 응답만으로 알 수 없으므로 기본값은 번호를 고정하지 않습니다.

1. [Battle.net 개발자 포털](https://develop.battle.net/)에서 클라이언트(Client)를 만듭니다. Client ID와 Client Secret을 받습니다. 이 값은 OAuth `client_credentials`용이며 ForeverDB `fdb_` 키가 아닙니다. 포털 화면에만 두고 저장소·채팅·리포트에 붙이지 않습니다.
2. 저장소 밖의 env 파일에 이름만 적습니다. 예제의 값은 비워 둡니다.

```
BNET_CLIENT_ID=
BNET_CLIENT_SECRET=
```

3. `config.forever.example.json`을 복사해 `blizzard_forever`를 확인합니다. `ruleset` 1, `namespace` `dynamic-forever-kr`, `locale` `ko_KR`, `region` `kr`이 기본입니다. `house_ids`가 빈 배열이면 색인에서 이름이 호드 또는 검은바다인 경매장을 모두 수집합니다. 118번 이후라도 이름에 맞으면 포함합니다. 얼라이언스는 `include_alliance`를 true로 하거나 `house_ids` / `--houses`에 번호를 적기 전에는 매물을 받지 않습니다. 색인에 없는 번호는 만들지 않습니다. `house_id` 하나, 또는 `house_ids`가 정확히 하나일 때만 그 경매장의 소멸 추정을 오늘 할 일에 넣습니다. 여러 경매장이면 비교 결과를 돈 계산에 자동으로 넣지 않습니다.
4. 라이브 DB는 저장소 밖에 둡니다. `blizzard_forever.db_path`가 null이면 프로젝트 `db_path`를 쓰므로, 박스에서 돌릴 때는 `--db`로 저장소 밖 파일을 지정합니다.

```bash
python3 blizzard_collector.py --config config.forever.json --env /path/outside/bnet.env --db /path/outside/forever-ah.db
```

토큰은 `POST https://oauth.battle.net/token` (`grant_type=client_credentials`)입니다. 색인은 `GET https://kr.api.blizzard.com/data/wow/ruleset/1/auctions/index?namespace=dynamic-forever-kr&locale=ko_KR` 입니다. 경매장마다 비재료 `/data/wow/ruleset/1/auctions/{id}` 와 재료 `/data/wow/ruleset/1/auctions/{id}/commodities` 를 받습니다. `auctions` 필드가 없거나 null이면 빈 경매장으로 저장하고 중단하지 않습니다. 429·5xx는 최대 3회, 대기 30초 한도, 요청 시간 제한 60초입니다. 로그에는 경매장 번호, 건수, Last-Modified, 생략, 오류 종류만 남기고 토큰·비밀·Authorization 헤더는 남기지 않습니다.

재료 단가는 응답의 `unit_price`입니다. 나누지 않습니다. 그 외 매물의 단가는 `floor(buyout / quantity)`입니다. 올림하지 않습니다. 즉시구매가 없거나 입찰만 있거나 정수가 아니면 그 매물은 집계와 소멸 추적에서 뺍니다. 스냅샷마다 아이템별로 최저 단가, 수량 가중 하위 중위, 하위 25%, 수량 합, 단가가 있는 매물 수를 남깁니다.

`fetched_at`은 UTC입니다. API `Last-Modified`가 있으면 같이 저장합니다. 경매·재료 양쪽이 그 경매장의 마지막 스냅샷과 같으면 스냅샷은 생략하고 확인 행만 남깁니다. 헤더가 없으면 생략하지 않습니다.

내 Auctionator 수입이 있으면 `--match`가 같은 아이템 최저가의 하위 중앙 상대오차(기저점)로 가까운 경매장 번호를 추정합니다. 네트워크를 쓰지 않습니다. 겹치는 아이템이 5개 미만이거나 Auctionator 가격이 없으면 번호를 고르지 않고 데이터 부족이라고 말합니다. 결과는 추정이며 서버 확정이 아닙니다.

```bash
python3 blizzard_collector.py --config config.forever.json --db /path/outside/forever-ah.db --match
```

네트워크 없는 샘플은 별도 DB가 필요합니다.

```bash
python3 blizzard_collector.py --config config.forever.json --db /tmp/forever-fixture.db --fixture tests/fixtures/blizzard_forever.json
```

소멸 추정·시간대·경매장 비교의 기준은 [MARKET.md](MARKET.md), 테이블은 [DATABASE.md](DATABASE.md)에 있습니다. Auctionator 최저가는 그대로 두고 공식 스냅샷은 옆에 보여 줍니다.

---

**[Data from ForeverDB.net](https://foreverdb.net)** · [Wowhead Forever](https://www.wowhead.com/forever/ko/database)

게임 데이터의 권리는 Blizzard Entertainment 및 각 권리자에게 있습니다. 이 프로젝트는 Blizzard, Wowhead, ForeverDB의 공식 서비스가 아닙니다. 제3자 자료의 출처·검증 상태를 보존합니다.
