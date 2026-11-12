"""Read Auctionator SavedVariables; only import when file mtime changes."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from common import (AppError, connect, config_path, get_meta, load_config, main_guard,
                    parser, positive_int, run, set_meta, snapshot, utcnow)
from savedvariables import price_database

DAILY_PARSER_VERSION = 1


def daily_rows(data, offset):
    """Installed Auctionator Database/Mixin.lua GetPriceHistory: l or h, h, a.

    h is the highest observed minimum; a is daily MAX availability, never sales.
    Keep quantity-only days with NULL prices instead of manufacturing a quote.
    """
    epoch = datetime(2020, 1, 1, tzinfo=timezone(timedelta(hours=offset)))
    rows = []
    for key, item in data.items():
        if not re.fullmatch(r"[1-9]\d*", str(key)) or not isinstance(item, dict):
            continue
        maps = []
        for field in ('l', 'h', 'a'):
            value = item.get(field, {})
            if value == []:
                value = {}
            if not isinstance(value, dict):
                raise AppError('Auctionator 일별 이력 형식이 변경되었습니다. 저장을 중단합니다.')
            if any(not re.fullmatch(r'\d+', str(day)) for day in value):
                raise AppError('Auctionator 관측일 형식을 확인할 수 없습니다.')
            maps.append({int(day): v for day, v in value.items()})
        lows, highs, amounts = maps
        for day in sorted(set(lows) | set(highs) | set(amounts)):
            high, quantity = highs.get(day), amounts.get(day)
            low = lows.get(day, high)
            if (any(v is not None and not positive_int(v) for v in (low, high))
                    or quantity is not None and (type(quantity) is not int or quantity < 0)
                    or low is not None and high is not None and low > high):
                raise AppError('Auctionator 일별 가격/수량 값이 올바르지 않습니다.')
            try:
                observed = (epoch + timedelta(days=day)).astimezone(timezone.utc).isoformat(timespec='seconds')
            except (OverflowError, ValueError):
                raise AppError('Auctionator 관측일 범위 오류') from None
            rows.append((int(key), day, observed, low, high, quantity))
    return sorted(rows)


def store_daily(db, realm_id, snapshot_id, data, rows, daily, stat, skipped):
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    cursor = db.execute('''INSERT OR IGNORE INTO auctionator_imports
        (realm_id,snapshot_id,fetched_at,file_mtime_ns,content_sha256,parser_version,
         latest_count,daily_count,skipped_count) VALUES (?,?,?,?,?,?,?,?,?)''',
        (realm_id, snapshot_id, utcnow(), str(stat.st_mtime_ns), digest, DAILY_PARSER_VERSION,
         len(rows), len(daily), skipped))
    if not cursor.rowcount:
        return None
    import_id = cursor.lastrowid
    db.executemany('INSERT OR IGNORE INTO items(item_id) VALUES (?)', ((r[0],) for r in daily))
    db.executemany('INSERT INTO auctionator_daily VALUES (?,?,?,?,?,?,?)',
                   ((import_id, *r) for r in daily))
    return import_id


def latest_rows(data, offset):
    # Source/Constants/Main.lua: SCAN_DAY_0 = local 2020-01-01 00:00.
    epoch = datetime(2020,1,1,tzinfo=timezone(timedelta(hours=offset)))
    rows, skipped = [], 0
    for key, item in data.items():
        if key == "version":
            continue
        # Variant/level/pet keys cannot safely be collapsed to a retail item ID.
        if not re.fullmatch(r"[1-9]\d*", str(key)) or not isinstance(item, dict):
            skipped += 1
            continue
        price = item.get("m")
        history = item.get("h")
        if not positive_int(price) or not isinstance(history, dict) or not history:
            skipped += 1
            continue
        days = [int(day) for day in history if re.fullmatch(r"\d+", str(day))]
        if not days:
            skipped += 1
            continue
        observed = (epoch + timedelta(days=max(days))).astimezone(timezone.utc).isoformat(timespec="seconds")
        # m = last minimum, a = daily MAX quantity. Neither is a median or current quantity.
        rows.append((int(key), price, None, None, None, observed))
    return sorted(rows), skipped


def read_file(path):
    before = path.stat()
    if before.st_size > 256 * 1024 * 1024:
        raise AppError("SavedVariables 파일이 256 MiB 한도를 넘었습니다.")
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise AppError("파일 쓰기 중입니다. /reload 또는 로그아웃 후 다시 시도하세요.")
    return raw, after


def check_retail(path):
    check_flavor(path, "retail")


def check_flavor(path, flavor):
    label = {"retail": "리테일", "forever": "Forever"}[flavor]
    marker = {"retail": "_retail_", "forever": "_forever_"}[flavor]
    token = {"retail": "wow", "forever": "wowf"}[flavor]
    for parent in path.parents:
        flavor_file = parent / ".flavor.info"
        if flavor_file.exists():
            lines = flavor_file.read_text(encoding="utf-8").splitlines()
            if not lines or lines[-1].strip() != token:
                raise AppError("설치 제품과 game_flavor가 다릅니다. 다른 제품 DB에 합칠 수 없습니다.")
            return
        if parent.name == marker:
            return
    raise AppError(f"{label} 설치 경로를 확인할 수 없습니다. {marker} 원본 경로를 설정하세요.")


def main():
    p = parser(__doc__)
    p.add_argument("--path", help="Auctionator.lua 경로 덮어쓰기")
    p.add_argument("--inspect", action="store_true", help="시장과 무관하게 구조/최근 가격만 확인; DB 변경 없음")
    p.add_argument("--sample", action="store_true", help="합성 검증 데이터; 별도 --db 필수")
    args = p.parse_args()
    cfg = load_config(args.config)
    if not args.path and not cfg["auctionator_path"]:
        raise AppError("config.json의 auctionator_path에 실제 파일 경로를 지정하세요.")
    path = Path(args.path).resolve() if args.path else config_path(cfg, "auctionator_path")
    if args.inspect:
        raw, stat = read_file(path)
        data = price_database(raw)
        print(f"Auctionator DB v8 / 파일 {stat.st_size} bytes / mtime_ns={stat.st_mtime_ns}")
        for key, values in data.items():
            if key == "__dbversion":
                continue
            rows, skipped = latest_rows(values, cfg["scan_timezone_offset_hours"])
            print(f"서버 키={key!r}, realm 버전=2, 최신 가격 {len(rows)}개, 제외 {skipped}개")
            for row in rows[:5]:
                print(f"  item_id={row[0]} 최저가={row[1]}동 관측일={row[-1]} 중위가/현재 수량=미제공")
        return
    if args.sample and not args.db:
        raise AppError("--sample에는 별도 --db가 필요합니다.")
    if cfg["game_flavor"] not in ("retail", "forever"):
        raise AppError("지원 제품은 retail/forever입니다.")
    check_flavor(path, cfg["game_flavor"])
    db = connect(cfg, args.db, "fixture" if args.sample else "live")
    with run(db, "import_auctionator"):
        key = cfg["auctionator_realm_key"]
        realm_id = cfg["auctionator_realm_id"]
        if cfg["game_flavor"] == "forever" and key:
            # Negative IDs are local SavedVariables markets, never Blizzard realm IDs.
            market_key = "local_market:" + hashlib.sha256((str(path)+":"+key).encode()).hexdigest()
            saved = get_meta(db, market_key)
            realm_id = int(saved) if saved else min(-1, db.execute("SELECT COALESCE(MIN(realm_id),0)-1 FROM realms").fetchone()[0])
            set_meta(db, market_key, realm_id)
            set_meta(db, "auctionator_local_realm_id", realm_id)
            db.execute("INSERT OR IGNORE INTO realms VALUES (?,?)", (realm_id, json.dumps([key])))
        if not key or (cfg["game_flavor"] == "retail" and not positive_int(realm_id)):
            raise AppError("auctionator_realm_key와 한국 connected-realm ID를 설정하세요.")
        realm = db.execute("SELECT names FROM realms WHERE realm_id=?", (realm_id,)).fetchone()
        if not realm:
            raise AppError("collect_api.py로 서버 이름/ID 목록을 먼저 수집하세요.")
        normalized = lambda value: re.sub(r"\s+", "", value).casefold()
        if normalized(key) not in [normalized(n) for n in json.loads(realm[0])]:
            raise AppError("Auctionator 서버 키와 connected-realm 이름이 일치하지 않습니다.")
        state_key = "auctionator:" + hashlib.sha256(str(path).encode()).hexdigest() + f":{realm_id}:{key}"
        stat = path.stat()
        if (get_meta(db, state_key) == str(stat.st_mtime_ns)
                and get_meta(db, state_key+':daily_version') == str(DAILY_PARSER_VERSION)):
            print("Auctionator: mtime 동일, 파일 읽기 생략")
            return
        raw, stat = read_file(path)
        data = price_database(raw)
        if key not in data:
            raise AppError("설정된 서버 키가 SavedVariables에 없습니다. --inspect로 확인하세요.")
        rows, skipped = latest_rows(data[key], cfg["scan_timezone_offset_hours"])
        daily = daily_rows(data[key], cfg['scan_timezone_offset_hours'])
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        with db:
            sid = snapshot(db, "auctionator", realm_id, utcnow(), "sha256:" + digest, rows)
            evidence_snapshot = sid or db.execute('''SELECT id FROM snapshots WHERE source='auctionator'
                AND realm_id=? AND last_modified=?''', (realm_id, 'sha256:'+digest)).fetchone()[0]
            imported = store_daily(db, realm_id, evidence_snapshot, data[key], rows, daily, stat, skipped)
            set_meta(db, state_key, stat.st_mtime_ns)
            set_meta(db, state_key+':daily_version', DAILY_PARSER_VERSION)
        print(f"Auctionator: {len(rows)}개 가격, {skipped}개 제외, " +
              (f"스냅샷 #{sid}" if sid else "가격 내용 동일, 중복 제외"))
        print(f"일별 이력: {len(daily)}개 · " + (f"파일 근거 #{imported}" if imported else '내용 동일, 중복 제외'))


if __name__ == "__main__":
    main_guard(main)
