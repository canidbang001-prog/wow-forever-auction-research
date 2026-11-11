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

> **개발 중 / 실제 공개 응답 수집 검증.** 모든 아이템과 관계 수집이 끝난 상태는 아닙니다. 일부 필드는 비공개이거나 클래식 참고자료입니다. ForeverDB 보호 API는 승인 키와 실제 응답 검증이 필요합니다. Auctionator 최저가만으로 수익이나 판매량을 확정하지 않습니다.

## 무엇을 볼 수 있나요?

| 기능 | 동작 |
|---|---|
| 내 시장 기록 | 실제 SavedVariables 구조의 마지막 최저가 저장, mtime/내용 중복 제외 |
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

Python **3.10 이상**, 표준 라이브러리만 사용합니다. 전체 소스 묶음 `source.zip`에는 테스트와 실제 응답 fixture도 포함됩니다. 압축을 해제한 폴더에서 실행하세요.

```powershell
Copy-Item config.forever.example.json config.forever.json
# auctionator_path에 실제 Auctionator.lua 경로 입력
# auctionator_realm_key는 --inspect에서 확인한 키 사용
python import_auctionator.py --config config.forever.json --inspect
python import_auctionator.py --config config.forever.json
python sync_static.py --config config.forever.json --max-requests 60
python analyze.py --config config.forever.json --output reports/forever.md
```

경매장을 조회한 뒤 `/reload` 또는 로그아웃해야 파일이 저장됩니다. 경로 예: `<WoW>/_forever_/WTF/Account/<계정>/SavedVariables/Auctionator.lua`.

기본 Forever DB는 **`ah_forever.db`**이며 설정 파일 폴더 기준입니다. 리테일은 별도 `ah.db`입니다. 같은 파일에 제품을 섞으면 실행을 거절합니다. Forever의 음수 `realm_id`는 로컬 시장 번호입니다. `PvP`만으로 실제 서버·지역·진영이 확인된 것은 아닙니다.

```powershell
# 다음 묶음 수집 / 상태 확인
python sync_static.py --config config.forever.json --max-requests 60
python sync_static.py --config config.forever.json --status
# 패치 직후: 실제 확인한 빌드를 입력
python sync_static.py --config config.forever.json --patch <확인한-빌드> --max-requests 60
# 파서를 고친 뒤 실패한 작업 재개
python sync_static.py --config config.forever.json --retry-errors --max-requests 60
```

초기 큐는 경매장 아이템뿐 아니라 상점 목록·전문기술 결과물·툴팁 재료 링크를 포함합니다. ID 없는 재료도 이름과 수량을 `static_facts`에 보관하며 이름으로 ID를 추측하지 않습니다. 전문기술 페이지의 실제 다음 페이지 링크를 따라가고 표시 개수 불일치·잘림을 기록합니다. 실행 한 번은 제한된 묶음이며 전체 완료를 뜻하지 않습니다.

## 출처와 현재 제약

| 공급자 | 확인한 접근 | 한계 |
|---|---|---|
| Wowhead Forever | 판매처 표와 [문서화된 아이템 XML](https://www.wowhead.com/tooltips) | XML에 전체 관계가 없음. 현재 HTTP 403. 이전 정상 XML과 실제 브라우저 표를 별도 근거로 보존 |
| ForeverDB 공개 페이지 | 아이템·상점·전문기술·퀘스트 SSR | NPC ID·묶음·통화·평판 등 일부 누락, 제작 재료는 이름만 있는 경우가 많음 |
| [ForeverDB API](https://foreverdb.net/developers) | 공개 `/api/v1` 문서 JSON | 승인 키 필요. `--api-probe`는 원본 JSON만 보관. **보호 API의 세부 정규화는 미구현**이며 키만 넣어도 자동 완성되지 않음 |
| Blizzard 한국 API | 리테일 `dynamic-kr` / `static-kr` | Forever에 대체 사용하지 않음. [리테일 가이드](RETAIL.md) 참고 |

`.env`에 선택적으로 `FOREVERDB_API_KEY`, 리테일용 `BNET_CLIENT_ID` / `BNET_CLIENT_SECRET`을 직접 입력합니다. 기존 키 파일을 덮어쓰지 마세요. 키는 로그·DB·공개 저장소에 넣지 않습니다. ForeverDB의 한국어 API 및 퀘스트 요구 아이템 필드는 현재 문서에서 확인되지 않았습니다.

정적 응답은 로컬 `cache/static`에 저장해 7일간 재사용합니다. 실행 시 소스 빌드는 하루 주기로 확인하며 변경되면 갱신을 예약합니다. 최소 요청 간격 1.1초, 로컬 하루 9,500회 상한을 적용합니다. 429 `Retry-After`는 다음 실행에도 유지합니다. 403 이후 공급자를 중단하며 접근 정상화 확인 후 `--retry-provider wowhead`로 재개합니다. 404·파싱 실패를 관계 없음으로 저장하지 않습니다.

## 데이터를 읽는 방법

가격은 **동(copper) 정수**, `1g = 100s = 10,000c`입니다. 출력만 골드로 표시합니다. `NULL`은 미확인이며 0이나 관계 없음과 다릅니다.

상점 `price_copper`는 사이트 표시 가격입니다. `purchase_qty`가 없으면 개당 원가를 계산하지 않습니다. 금·은·동, 묶음, 재고, 별도 화폐, 평판·진영은 별도 열입니다. 판매처 출처 검증과 내 캐릭터 구매 가능 여부는 `verification` / `character_offer_checks`로 분리합니다.

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

구매 묶음·재료 ID/수량·결과 수량이 확인되어야 원가를 계산할 수 있습니다. Blizzard recipe ID, craft spell ID, 도안 item ID, 결과물 item ID는 별도 열입니다. 리테일 `known_recipes.txt`는 Blizzard recipe ID 목록입니다. Forever 제작 추천 및 `spell:` 목록 필터는 필요한 데이터가 확보된 뒤 연결할 후속 기능입니다. 보증금 손실·이동·재등록 비용은 별도입니다.

## 파일 구성과 검증

| 파일 | 역할 |
|---|---|
| `sync_static.py`, `static_parsers.py`, `static_store.py` | 수집·캐시·파싱·증거·마이그레이션 |
| `import_auctionator.py`, `savedvariables.py` | Lua/CBOR 데이터를 실행하지 않고 읽기 |
| `collect_api.py`, `sync_recipes.py` | 한국 리테일 공식 API |
| `analyze.py`, `forever_report.py` | 리테일 기회 분석 / Forever 조사표 |
| `import_vendors.py` | 여러 판매처 CSV 추가·수정 |
| `common.py`, `schema.sql`, `static_schema.sql` | 설정·SQLite·스키마 |
| `demo.py`, `tests/` | 합성 데이터와 실제 응답 fixture 검증 |

전체 소스 압축을 해제한 뒤 `python -m unittest discover -s tests -v`로 검사합니다. 실제/합성 검증을 구분한 결과는 [STATIC_VERIFICATION.md](STATIC_VERIFICATION.md)에 있습니다. 개인 설정, SavedVariables, DB, 키는 공개 배포하지 않습니다.

## OS 스케줄러

아래 경로를 실제 프로젝트/Python 경로로 바꿉니다. 작업을 자동 등록하지 않고 명령을 안내합니다.

```powershell
schtasks /Create /TN "WoW Forever Prices" /SC HOURLY /MO 1 /TR '"C:\Python313\python.exe" "C:\project\import_auctionator.py" --config "C:\project\config.forever.json"'
schtasks /Create /TN "WoW Forever Static" /SC WEEKLY /D SUN /ST 04:00 /TR '"C:\Python313\python.exe" "C:\project\sync_static.py" --config "C:\project\config.forever.json" --max-requests 300'
```

```cron
0 * * * * /usr/bin/python3 /path/project/import_auctionator.py --config /path/project/config.forever.json
0 4 * * 0 /usr/bin/python3 /path/project/sync_static.py --config /path/project/config.forever.json --max-requests 300
```

주간 실행 한 번으로 대기열 전체가 끝나지는 않습니다. 초기 수집은 제한된 묶음을 반복 실행하고 패치 직후에는 `--patch`로 갱신합니다.

---

**[Data from ForeverDB.net](https://foreverdb.net)** · [Wowhead Forever](https://www.wowhead.com/forever/ko/database)

게임 데이터의 권리는 Blizzard Entertainment 및 각 권리자에게 있습니다. 이 프로젝트는 Blizzard, Wowhead, ForeverDB의 공식 서비스가 아닙니다. 제3자 자료의 출처·검증 상태를 보존합니다.
