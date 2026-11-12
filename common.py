"""Shared SQLite/config/HTTP helpers. Python standard library only."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent


class AppError(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise AppError("시간에는 UTC 오프셋이 필요합니다.")
    return dt.astimezone(timezone.utc)


def load_config(path=None):
    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    p = Path(path).resolve() if path else ROOT / "config.json"
    if p.exists():
        custom = json.loads(p.read_text(encoding="utf-8-sig"))
        analysis = dict(cfg["analysis"], **custom.pop("analysis", {}))
        cfg.update(custom)
        cfg["analysis"] = analysis
    elif path:
        raise AppError("설정 파일이 없습니다.")
    cfg["base_dir"] = p.parent
    return cfg


def config_path(cfg, key):
    p = Path(cfg[key])
    return p if p.is_absolute() else cfg["base_dir"] / p


def parser(description, fixture=False):
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--config", help="JSON 설정 파일")
    p.add_argument("--db", help="DB 경로 덮어쓰기")
    if fixture:
        p.add_argument("--fixture", help="오프라인 API 응답 JSON; --db 필수")
    return p


def connect(cfg, override=None, mode="live"):
    p = Path(override).resolve() if override else config_path(cfg, "db_path")
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    from static_store import migrate
    migrate(db)
    flavor = cfg.get("game_flavor", "retail")
    previous_flavor = get_meta(db, "game_flavor")
    if previous_flavor and previous_flavor != flavor:
        db.close()
        raise AppError("리테일/Forever 데이터 혼합 금지: 다른 DB를 사용하세요.")
    if not previous_flavor and flavor != "retail" and db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]:
        db.close()
        raise AppError("기존 시장 DB의 제품을 확인할 수 없습니다. 새 Forever DB를 지정하세요.")
    set_meta(db, "game_flavor", flavor)
    db.execute("INSERT OR IGNORE INTO realms VALUES (0,?)", ('["한국 공통 재료"]',))
    old = get_meta(db, "data_mode")
    if old and mode is not None and old != mode:
        db.close()
        raise AppError("실데이터/샘플 데이터 혼합 금지: 별도 --db를 지정하세요.")
    if not old and mode is not None:
        set_meta(db, "data_mode", mode)
    db.commit()
    return db


def get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(db, key, value):
    db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, str(value)))


@contextmanager
def run(db, name):
    run_id = db.execute("INSERT INTO runs(started_at,note) VALUES (?,?)", (utcnow(), name)).lastrowid
    db.commit()
    try:
        yield
    except Exception as exc:
        db.rollback()
        # Never persist HTTP bodies, URLs, credentials, or arbitrary exception text.
        db.execute("UPDATE runs SET finished_at=?,ok=0,note=? WHERE id=?",
                   (utcnow(), name + ": " + type(exc).__name__, run_id))
        db.commit()
        raise
    else:
        db.execute("UPDATE runs SET finished_at=?,ok=1 WHERE id=?", (utcnow(), run_id))
        db.commit()


def main_guard(fn):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        fn()
    except AppError as exc:
        print("오류: " + str(exc), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print("오류: " + type(exc).__name__ + " — 설정/입력 형식을 확인하세요.", file=sys.stderr)
        sys.exit(1)


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() in ("BNET_CLIENT_ID", "BNET_CLIENT_SECRET", "FOREVERDB_API_KEY"):
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def http_json(url, headers, data=None):
    """JSON request with the Battle.net retry policy. Errors omit credentials and bodies."""
    for attempt in range(3):
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=60) as response:
                return json.load(response), response.headers.get("Last-Modified")
        except HTTPError as exc:
            code = exc.code
            retry = exc.headers.get("Retry-After", "")
            exc.close()
            if code == 304:
                return None, None
            if code == 429 or code >= 500:
                if attempt < 2:
                    time.sleep(min(30, int(retry)) if retry.isdigit() else 2 ** attempt)
                    continue
            raise AppError(f"Blizzard HTTP {code} (인증 정보/응답 본문 생략)") from None
        except (URLError, TimeoutError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise AppError("Blizzard 네트워크 연결 실패") from None
    raise AppError("Blizzard 요청 실패")


class Blizzard:
    def __init__(self, cfg, fixture=None):
        if cfg.get("game_flavor", "retail") != "retail":
            raise AppError("Blizzard dynamic-kr/static-kr 수집기는 리테일 전용입니다. Forever는 sync_static.py를 사용하세요.")
        self.fixture = json.loads(Path(fixture).read_text(encoding="utf-8")) if fixture else None
        self.token = None
        self.expires = 0
        if self.fixture is None:
            load_env(cfg["base_dir"] / ".env")
            self.client_id = os.environ.get("BNET_CLIENT_ID", "")
            self.secret = os.environ.get("BNET_CLIENT_SECRET", "")
            if not self.client_id or not self.secret:
                raise AppError(".env에 BNET_CLIENT_ID / BNET_CLIENT_SECRET을 설정하세요. 값은 로그에 남기지 않습니다.")

    def request(self, url, headers, data=None):
        return http_json(url, headers, data)

    def get(self, path, static=False, modified=None):
        if self.fixture is not None:
            if path not in self.fixture:
                raise AppError("샘플 API 응답에 경로가 없습니다: " + path)
            entry = self.fixture[path]
            if modified and entry.get("last_modified") == modified:
                return None, None
            return entry["body"], entry.get("last_modified")
        if not self.token or time.time() >= self.expires:
            basic = base64.b64encode((self.client_id + ":" + self.secret).encode()).decode()
            body, _ = self.request("https://oauth.battle.net/token",
                                   {"Authorization": "Basic " + basic,
                                    "Content-Type": "application/x-www-form-urlencoded"},
                                   b"grant_type=client_credentials")
            self.token = body["access_token"]
            self.expires = time.time() + int(body["expires_in"]) - 60
        headers = {"Authorization": "Bearer " + self.token}
        if modified:
            headers["If-Modified-Since"] = modified
        query = urlencode({"namespace": "static-kr" if static else "dynamic-kr", "locale": "ko_KR"})
        return self.request("https://kr.api.blizzard.com" + path + "?" + query, headers)


def positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def ensure_items(db, ids):
    db.executemany("INSERT OR IGNORE INTO items(item_id) VALUES (?)", ((i,) for i in set(ids)))


def cache_items(db, api, limit):
    ids = [r[0] for r in db.execute(
        "SELECT item_id FROM items WHERE updated_at IS NULL ORDER BY cache_error IS NOT NULL,cache_error,item_id LIMIT ?",
        (min(300, max(0, int(limit))),))]
    failures = 0
    for item_id in ids:
        try:
            item, _ = api.get(f"/data/wow/item/{item_id}", static=True)
        except AppError:
            failures += 1
            db.execute("UPDATE items SET cache_error=? WHERE item_id=?", (utcnow(), item_id))
            continue
        cls, sub = item.get("item_class", {}), item.get("item_subclass", {})
        db.execute("""UPDATE items SET name=?,purchase_price=?,sell_price=?,class_id=?,class_name=?,
                   subclass_id=?,subclass_name=?,binding=?,updated_at=?,cache_error=NULL WHERE item_id=?""",
                   (item.get("name"), item.get("purchase_price"), item.get("sell_price"), cls.get("id"),
                    cls.get("name"), sub.get("id"), sub.get("name"), item.get("preview_item", {}).get(
                        "binding", {}).get("type"), utcnow(), item_id))
    refresh_vendors(db)
    db.commit()
    print(f"아이템 캐시: {len(ids)-failures}개 갱신, {failures}개 보류 (최대 300회)")


def refresh_vendors(db):
    db.execute("""INSERT INTO vendor_items(offer_key,item_id,vendor_price,source,note)
        SELECT 'legacy:'||item_id,item_id,purchase_price,'api:purchase_price',
          'NPC/실재고/평판/묶음 단가 확인 필요; API 가격만으로 추린 후보'
        FROM items WHERE purchase_price>0 AND (class_id IN (5,7,12) OR EXISTS (
          SELECT 1 FROM reagents r WHERE r.item_id=items.item_id))
        ON CONFLICT(offer_key) DO UPDATE SET vendor_price=excluded.vendor_price
        WHERE vendor_items.source='api:purchase_price'""")


def weighted_median(levels):
    """Lower weighted median in integer copper (no floating-point conversion)."""
    total = sum(q for _, q in levels)
    if total <= 0:
        raise AppError("수량 가중 중위값에 양수 수량이 필요합니다.")
    threshold, seen = (total + 1) // 2, 0
    for price, qty in sorted(levels):
        seen += qty
        if seen >= threshold:
            return price


def snapshot(db, source, realm_id, fetched_at, modified, rows, auctions=None):
    """rows: item_id,min,median,quantity,n_auctions,observed_at. Atomic with caller."""
    ensure_items(db, (r[0] for r in rows))
    cursor = db.execute("""INSERT OR IGNORE INTO snapshots
        (source,realm_id,fetched_at,last_modified,n_auctions) VALUES (?,?,?,?,?)""",
        (source, realm_id, fetched_at, modified, auctions))
    if cursor.rowcount == 0:
        return None
    db.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)",
                   ((cursor.lastrowid, *row) for row in rows))
    return cursor.lastrowid


def header_time(value):
    if not value:
        raise AppError("Last-Modified 헤더 누락: 스냅샷을 저장하지 않았습니다.")
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        raise AppError("Last-Modified 헤더 형식 오류") from None
