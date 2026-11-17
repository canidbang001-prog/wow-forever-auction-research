# API-only 경매장 관측

`api_market_observer.py`는 기존 시간당 Blizzard Forever API 수집 DB만 읽어 시장별 시세와 시장 간 **등록 호가 차이**를 보여줍니다. Auctionator 파일·API 자격증명·추가 패키지·네트워크 호출이 필요하지 않습니다. 기존 제작/상점 매매 행동 계획기와 별개이며 `market_confirmed`를 우회하거나 바꾸지 않습니다.

보고서의 사용자 지정 별칭은 **호드 경매장 (#113)**, **중립 경매장 (#114)**입니다.
`auction_house_display.py`의 고정 별칭을 HTML·검색 HTML·텔레그램 요약의 시장 제목,
양방향 비교 행과 경매장 누락 오류에 공통 적용하며, API/캐시 이름이 덮어쓰지 않습니다.
이는 공식 진영/중립 매핑을 확인했다는 뜻이 아닙니다. 원본 JSON의 ID·API 이름,
미확인 시장 설정, 가격·정렬·알림 지문과 그 밖의 경매장 표시 fallback은 유지합니다.
별도 운영 스냅샷을 패키징할 때 새 표시 모듈도 포함해야 합니다.

## 실행

Python 표준 라이브러리만 사용합니다. `--config`에는 수집기가 사용하는 기존 설정을 지정합니다. DB가 없으면 오류를 내며 DB 생성·마이그레이션을 하지 않습니다.

```bash
# 기본: JSON을 stdout으로 출력
python api_market_observer.py --config /path/to/config.forever.json

# 두 시장만 관찰하고 JSON 파일 저장
python api_market_observer.py --config /path/to/config.forever.json --houses 113,114 --output reports/api-market.json

# 휴대 가능한 단일 HTML. 인터넷 연결·서버 실행 없이 브라우저로 열 수 있음
python api_market_observer.py --config /path/to/config.forever.json --houses 113,114 --format html --output reports/api-market.html

# 별도 DB 경로 명시
python api_market_observer.py --config /path/to/config.forever.json --db /path/to/auction.sqlite3 --format html
```

Windows에서도 `C:/Users/...` 형식을 사용할 수 있습니다. `--format` 기본값은 `json`, HTML의 기본 출력은 `reports/api-market.html`입니다. 출력 확장자가 형식을 바꾸지는 않습니다.

기본 시장 범위는 설정의 `blizzard_forever.house_ids`, 없으면 DB에 기록된 호드·검은바다입니다. `include_alliance=true`이면 얼라이언스도 포함합니다. 명시적 `--houses`는 같은 ruleset의 알려진 ID만 허용하며, 개인 시장 확인이나 실제 접근 가능성을 의미하지 않습니다. 113·114 같은 ID를 중립/진영 전용으로 단정하지 않습니다.

## 출력과 제한

- `houses`: API ID별 원본 이름·진영 표기, 최신 스냅샷, 수집 시각과 commodity 원본 갱신 시각, 상태·차단 사유, 관측 범위, 현재 commodity 호가. `fetched_epoch`는 검증된 수집 시각이며 손상된 경우 `null`입니다. `latest`는 최신 원본 메타데이터를 유지하되 JSON으로 표현할 수 없는 비유한 실수는 `{"type":"non_finite_float","value":"inf"}` 같은 명시적 태그로 남깁니다. 수집 시각·Last-Modified 등 원본 메타데이터 필드에 잘못 저장된 BLOB도 `type="bytes"`와 원본의 문자열 표현을 담은 태그로 보존합니다.
- `pairs`: 시장 쌍의 원본 갱신 시각차와 수집 시각차, 비교 가능 여부. 시각이 안 맞으면 가격 차이를 만들지 않습니다.
- `comparisons`: 같은 상품의 현재 낮은 시장 → 높은 등록 참고 시장, 개당 최저가, **그 최저가에 나온 수량**과 전체 수량, 원시 호가차, 방향별 과거 근거.
- `observation`: 현재 비교 가능한 호가 차이. `repeated_gap`: 독립된 정합 갱신 쌍에서 현재 방향이 기본 3번 이상 말미에 반복되고 마지막 근거가 현재 스냅샷과 같음. 어느 것도 매수/매도 권고가 아닙니다.
- `history_readiness`: 설정의 `min_distinct_days`(기본 3일)에 대한 별도 상태. 기본 한국시간 날짜이며, 3일이 됐다고 수익이나 충분한 거래 수요가 보장되는 것은 아닙니다.
- 이력 첫 값→마지막 값 변화와 **스냅샷 최저가들의 하위 중앙값**은 실제 체결가·거래량 가중값·24시간 수익률이 아닙니다.

관측 매개변수:

```text
--window-hours 72       이력 시간 창 (실제 epoch 기준, 보간 없음)
--max-age-hours 3       원본 갱신·수집의 최대 나이
--max-skew-minutes 15   비교할 원본 갱신·수집 시각의 최대 차이
--min-pairs 3           repeated_gap 라벨의 최소 말미 반복 수 (2 이상)
--limit 30             원시 개당 호가차 기준 표시 수 (1~500)
```

하우스별로 수집 시각이 시간 창 안인 모든 스냅샷 헤더·시각 메타데이터를 읽되, 이력 호가 집계는 최근 저장 스냅샷 최대 168개로 제한하며 잘린 경우 표시합니다. 동일 commodity Last-Modified가 반복돼도 독립 갱신으로 세지 않습니다. 집계 범위 안에서 같은 Last-Modified의 호가가 다르면 그 갱신을 충돌로 제외합니다. 이 일치 검사는 원본 갱신 시각이 이력 창보다 오래됐어도 수행하며, 최신 원본의 충돌은 현재 비교를 `lm_conflict`로 차단합니다. 일치하는 오래된 원본도 이력 창 밖이면 표본·독립 갱신 수에는 넣지 않습니다. 잘려 나간 헤더와 남은 헤더에 같은 Last-Modified가 있으면 전체 호가의 일치 여부를 검증할 수 없으므로 해당 갱신을 이력에서 보수적으로 제외하고 `coverage.lm_history_truncated`에 원본 epoch를 표시합니다. 이는 확인된 `lm_conflict`가 아니며, 최신 원본이 여기에 해당하면 `lm_history_truncated` 사유로 현재 비교도 차단합니다. 최신 스냅샷·호가는 그대로 표시하고 과거 깨끗한 가격으로 대체하지 않습니다. 최신 원본 시간이 뒤로 갔거나 시각이 손상·미래·시간대 미상인 경우도 과거 가격으로 몰래 대체하지 않습니다. 오래된 원본은 보수적으로 `aged_source_data`로 표시하며, 수집 실패라고 단정하지 않습니다.

상품별 `history`에는 적격 근거 표본 중 마지막 128개만 반환합니다. 관측 수·연속 방향·날짜 수와 첫 값·마지막 값·중앙값 등의 통계 및 추세는 반환된 128개만이 아니라 모든 적격 표본으로 계산합니다. 이 표시 제한은 하우스별 168개 스냅샷 호가 집계 제한과 별개입니다.

비교는 **`kind='commodity'`만 다시 집계**합니다. 일반 경매 스택의 나눗셈·장비 옵션이 섞인 `blizzard_item_prices` 최저가를 상품 가격으로 사용하지 않습니다. 최저가 수량은 최저 가격 버킷의 수량 합이지 전체 재고가 아닙니다. 스냅샷 간 같은 매물은 판매 건수로 중복 계산하지 않습니다.

**수수료·보증금·운송·시장 접근·판매확률·실제 체결은 미확인입니다.** 비싼 시장의 등록가는 그 가격에 살 사람이 있다는 증거가 아닙니다. 소멸한 매물은 취소·만료일 수 있습니다. 실제 거래 속도나 확정 이익을 추론하지 않습니다.

## 안전·검증

CLI는 SQLite URI `mode=ro`와 짧은 읽기 트랜잭션으로 일관된 뷰를 읽고, 연결을 닫은 뒤 보고서를 씁니다. 설정·DB·SQLite 보조 파일 및 그 하드링크를 출력으로 덮어쓰는 경로는 거부합니다. 출력 검증은 `Path.resolve()`로 확인한 canonical 경로를 반환하며, 이후 디렉터리 생성·임시 파일 작성·원자 교체·정리는 이 경로만 사용합니다. 따라서 분석 도중 원래 지정한 경로의 symlink나 Windows 디렉터리 junction이 다른 디렉터리로 재지정돼도 출력을 따라 보내지 않습니다. 보고서는 검증된 출력과 같은 디렉터리의 임시 파일에 완전히 쓴 뒤 핸들을 닫고 원자적으로 출력 경로를 교체합니다. 분석 도중 출력 파일 자리에 입력의 하드링크가 생겨도 입력 파일을 잘라 쓰지 않으며, 쓰기·교체 오류 시 기존 보고서를 유지하고 임시 파일을 정리합니다.

**경로 보호의 신뢰 경계:** 검증된 canonical 출력 경로의 부모와 모든 상위 디렉터리(실행 중 생성할 디렉터리 포함)는 검증부터 임시 파일 정리까지 신뢰 가능하고 변경되지 않아야 합니다. 디렉터리 핸들로 그 identity를 고정하지 않으므로, 공격자가 canonical 상위 디렉터리 자체를 이름 변경·교체하거나 junction/symlink로 바꿀 수 있는 환경까지 보호하지는 않습니다. 원래 입력한 alias의 재지정 방지와 이 전제는 별개입니다. 출력은 접근 권한으로 보호된 디렉터리를 사용해야 합니다.

DB에서 읽은 같은 ruleset의 경매장 ID와 집계한 commodity 상품 ID는 양의 정수 타입인지 검증하며 문자열·실수 등을 정수로 변환해 보정하지 않습니다. HTML은 표시하는 상품·경매장·스냅샷 ID를 포함해 외부 문자열을 escape하며 스크립트·원격 리소스가 없습니다. 수집 코드·운영 설정·예약은 변경하지 않습니다.

하우스·ruleset별 최신 헤더는 시각 필드가 아니라 안정적인 저장 순서인 `id DESC`로 먼저 선택한 뒤 검증합니다. 손상된 최신 `fetched_epoch`가 이전 정상 값보다 작아도 최신 헤더·호가를 유지하고 비교를 차단하며 과거 정상 스냅샷으로 대체하지 않습니다. 이 선택은 이력 시간 창·호가 집계 제한·원본 시간 회귀 검사·시장 쌍 정합 방식을 바꾸지 않습니다. 수집 epoch의 숫자 타입·유한성·시각 변환 가능 범위와 시간대가 있는 ISO 수집 시각의 일치를 검증한 뒤에만 나이·미래 여부·시장 간 수집 시각차를 계산합니다. 손상된 최신 수집 시각은 `invalid_fetch_time`으로 그 시장을 차단하고 해당 쌍의 `fetch_skew_seconds`를 `null`로 남깁니다. 다른 정상 시장의 보고는 유지하며 HTML에서 미확인·변환 불가능한 시각은 `미확인`으로 표시합니다. 원본의 비유한 숫자를 JSON의 NaN/Infinity 토큰이나 임의의 정상 시각으로 보정하지 않습니다.

```bash
python -m unittest tests.test_api_observer -v
python -m unittest discover -s tests
```

새 테스트는 합성 임시 SQLite만 사용합니다. 실제 DB로 테스트 데이터를 넣지 않습니다.

## 추가 기능: 한글 이름·검색

기본 호출과 기존 `render(data)`는 그대로입니다. `--cache` 또는 `--query`를 명시하면 공식 Forever KR 이름을 표시 정보로만 덧붙이고, **전체 적격 관측에서 검색한 뒤** 상위 N개를 선택합니다. 500위 밖의 상품도 CLI 검색에서 찾을 수 있지만, 가격 정합 조건을 통과하지 못한 상품을 검색으로 되살리지는 않습니다.

```bash
python api_market_observer.py --config /runtime/config.forever.json --db /runtime/auction.sqlite3 --cache /runtime/item-names.json --query 리넨 --limit 30
python api_market_observer.py --config /runtime/config.forever.json --db /runtime/auction.sqlite3 --query 2589
```

숫자 전체 입력은 정확한 양의 아이템 ID이며, 나머지는 공식 이름의 NFKC·casefold 부분 검색입니다. 입력은 최대 64자입니다. 캐시가 없으면 ID 검색은 되지만 이름 검색은 일치하지 않습니다. `selection`에는 검색 전 `eligible_count`, 검색 후·제한 전 `match_count`, 반환한 `shown_count` 및 반환 행의 이름 적용 수가 따로 있습니다.

`forever_item_cache.py`는 오프라인 검증기입니다. JSON v1의 provenance는 `game_flavor=forever`, `namespace=static-forever-kr`, `region=kr`, `locale=ko_KR`, 공식 아이템 URL 접두사여야 합니다. 키와 양의 정수 `item_id`가 일치해야 하며 각 항목의 URL·namespace·locale·시각도 검증합니다. 잘못된 파일은 이름을 전혀 적용하지 않고, 잘못된 개별 항목만 제외할 수도 있습니다. 제어 문자·bidi 재정렬 문자는 거부하고 이름·ID를 HTML escape합니다. 이름·분류는 가격·수량·정렬·근거·거래 조건을 바꾸지 않습니다. 30일이 지난 정적 이름은 `old` 표시와 함께 계속 쓸 수 있습니다.

## 별도 보고서와 전송

```bash
# 네트워크·자격증명 없이 새 보고서 디렉터리만 갱신
python api_report_refresh.py --config /runtime/config.forever.json --db /runtime/auction.sqlite3 --cache /runtime/item-names.json --houses 113,114 --output-root /runtime/reports/hourly

# 명시적으로 전송을 허용한 별도 조정 진입점
python api_report_service.py --config /runtime/config.forever.json --db /runtime/auction.sqlite3 --cache /runtime/item-names.json --houses 113,114 --output-root /runtime/reports/hourly --send --chat-id YOUR_NUMERIC_CHAT_ID --telegram-env /runtime/hermes.env
```

새 디렉터리의 `report-latest.json`, `report-latest.html`, `report-latest-summary.txt`, `report-status.json`을 각각 같은 디렉터리의 닫힌 임시 파일에서 원자 교체합니다. 파일별 원자성이지 여러 산출물을 묶은 트랜잭션은 아닙니다. 기존 canonical 보고서 경로는 사용하지 않습니다. 입력·SQLite 보조 파일·다른 산출물과 겹치는 경로와 Git 내부 출력은 거부하며 앞서 설명한 canonical 상위 디렉터리 신뢰 경계가 적용됩니다.

새 HTML만 고정 inline JavaScript로 문서 안의 최대 500개 후보를 검색합니다. DB 전체 검색이 아니며 처음 30개 표시, 전체 보기, 검색 초기화, 표시/로드/적격 수 구분을 제공합니다. 이름을 JS에 삽입하지 않고 외부 리소스·네트워크 호출·`innerHTML`을 쓰지 않습니다. JavaScript가 없어도 가격과 근거 전체를 읽을 수 있습니다.

요약은 상위 30개의 가격 관측 중 최대 8개, 최대 3,500자입니다. **거래 권고가 아니며**, `repeated_gap`는 ‘반복 관측’입니다. `mode=fixture`는 `[합성 테스트 데이터]`, `live`는 `[실제 수집 데이터]`, 누락·미확인 출처는 `[출처 미확인 데이터]`로 구분합니다. 서비스는 실제 `TelegramSender`에 대해 `mode=live`만 전송하고 나머지는 `nonlive_blocked`로 기록하며 전송 ledger를 건드리지 않습니다. 명시적으로 주입한 가짜 sender만 비실수집 데이터의 오프라인 전송 테스트에 사용할 수 있습니다. 텔레그램은 서식 해석 없는 일반 텍스트로 보냅니다. `--send`가 없으면 자격증명도 읽지 않습니다. 전송 허용 시 기존 환경 또는 명시한 env 파일의 `TELEGRAM_BOT_TOKEN` 하나만 사용하고 값·오류 URL·원격 응답은 저장하지 않습니다.

알림 fingerprint v2는 데이터 출처 `mode`와 상위 30개의 가격·최저가 수량·전체 수량·등록 수·방향·반복 관측, 시장/규칙/정합 조건으로 만듭니다. 같은 가격이어도 fixture와 live는 서로 다른 지문이며, v1 지문과도 구분합니다. 같은 mode 안에서 생성 시각·스냅샷 ID·표본 수 증가·이름 보강·31위 이하 변화는 알림을 만들지 않습니다. `delivery-ledger.json`과 OS 잠금으로 전송 판단부터 성공 기록까지 직렬화합니다. 같은 fingerprint는 재전송하지 않고, 다른 관측도 성공 후 기본 6시간 동안 억제합니다. 명확한 실패는 30분 지수 backoff, 타임아웃·연결 유실·중단된 전송은 결과 미상으로 6시간 억제하며 성공 처리하지 않습니다. HTTP의 정확히 한 번 전달은 보장하지 않습니다. 손상된 ledger는 초기화 후 재전송하지 않고 차단합니다.

보고서를 먼저 완성한 뒤 전송하므로 전송 미설정·실패에도 HTML은 남고 성공한 보고서 생성의 종료 코드는 0입니다. 진단과 전송 상태는 로컬 status에 기록합니다. 보고서 생성 실패 시 전송하지 않습니다. API의 `sent`는 의도한 대상·본문·양의 message ID를 Telegram 응답에서 확인했다는 뜻이지 사용자의 열람 확인이 아닙니다.

## 이름 수집은 별도 프로세스

```bash
python forever_item_updater.py --config /runtime/config.forever.json --db /runtime/auction.sqlite3 --env /runtime/secrets/blizzard.env --cache /runtime/item-names.json --state /runtime/item-names-state.json --candidates-report /runtime/reports/hourly/report-latest.json --max-requests 40 --budget-seconds 90
```

업데이터만 기존 Blizzard 클라이언트를 사용하며 설정을 복사해 `static-forever-kr/ko_KR`로 조회합니다. OAuth를 포함한 시간 예산, 실제 아이템 조회 시도 상한, payload ID 일치, 공개 필드 allowlist를 적용합니다. 후보의 미등록 이름을 먼저 조회하고 나머지 catalog를 숫자 순서로 채웁니다. 404는 7일 negative cache이며 401/403/429는 즉시 중단·지속 cooldown합니다. 캐시는 거래 DB 밖, Git 밖에 둡니다. HTTP hook을 제한하고 복원하므로 **스레드가 아닌 전용 프로세스로 실행**합니다.

자동 운영은 기존 수집 및 별도 오후 분석 예약을 수정하지 않고, 시간별 보고와 더 느린 이름 갱신의 두 no-agent 작업을 별도로 연결합니다. 두 작업은 20분 이상 엇갈리게 배치하고 수집기를 호출하지 않습니다. 이 문서는 연결 방법을 설명하며 실제 활성화 여부는 운영 스케줄과 실행 이력에서 따로 확인해야 합니다.
