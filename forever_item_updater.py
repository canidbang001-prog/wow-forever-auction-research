"""Separate official Forever KR metadata updater; never imported by reports.

update_cache(cfg, *, db_path, cache_path, state_path, config_path=None,
             env_path=None, candidates_report=None, max_requests=40,
             budget_seconds=90, transport=None, now=None, monotonic=time.monotonic)
Transport injection: callable(item_id, remaining_seconds) -> official item dict.
Raise urllib.error.HTTPError to simulate an HTTP response, or another exception
for a sanitized transport failure. Supplying a transport bypasses credential
loading by the updater.

cfg requires base_dir and game_flavor='forever'; the existing catalog DB must
also have meta.game_flavor='forever'. Only positive IDs from items are read via
SQLite mode=ro, without common.connect, migration or auction-data writes.
config_path should identify the selected configuration when supplied through
the Python API. Cache/state must be different, outside every Git checkout and
not alias any input or SQLite sidecar, including existing hardlinks. Canonical
ancestors must be trusted/stable; this is not an adversarial-filesystem sandbox.

Results contain status, requests (attempted item lookups, not OAuth HTTP count),
and updated_count (persisted named records); completed runs also include
not_found_count and cooldown_until. Failures may include a fixed reason code.
No response bodies, headers, credentials or error URLs leave this API. Recent
names are reused for 30 days; 404 negatives for seven days. Uncached candidate
report comparisons/item_id entries precede the remaining numeric catalog.
401/403 stop with a one-hour cooldown; 429 honors numeric/date Retry-After,
bounded to 60..86400 seconds (invalid/missing: 900). Invalid state fails closed.

CLI: python forever_item_updater.py [--config JSON] [--db SQLITE] [--env FILE]
     --cache JSON --state JSON [--candidates-report JSON]
     [--max-requests 40] [--budget-seconds 90]
JSON stdout only; exit 0 for ok/cooldown/budget_exhausted, 1 for failures.
The default real adapter is for a dedicated updater process, not threads.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from forever_item_cache import PROVENANCE, _timestamp, _unique_object, _valid_entry, load_item_cache


class _ConfigurationError(Exception):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class OfficialItemTransport:
    """Single-process adapter around ForeverAuctions; one HTTP try, no backoff.

    OAuth and item requests share the caller's remaining deadline. The HTTP
    hook/socket cap are scoped and restored; do not use this adapter in threads.
    client_factory/opener exist for offline tests, never to supply chat secrets.
    """

    def __init__(self, cfg, env_path=None, *, client_factory=None, opener=None,
                 monotonic=time.monotonic):
        self.cfg, self.env_path = deepcopy(cfg), env_path
        self.client_factory = client_factory
        self.opener = opener or build_opener(_NoRedirect()).open
        self.monotonic, self.client = monotonic, None

    def _request(self, url, headers, data=None):
        import common
        if url not in ('https://oauth.battle.net/token', self.item_url):
            raise OSError('unexpected metadata endpoint')
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError('metadata budget exhausted')
        timeout = min(5, remaining)
        body_deadline = time.monotonic() + remaining
        previous = common.HTTP_SOCKET_TIMEOUT
        try:
            common.HTTP_SOCKET_TIMEOUT = timeout
            with self.opener(Request(url, data=data, headers=common._with_user_agent(headers)),
                             timeout=timeout) as response:
                raw = common._read_body(response, body_deadline)
                return json.loads(raw), response.headers.get('Last-Modified')
        except HTTPError as exc:
            if url == 'https://oauth.battle.net/token' and exc.code == 404:
                exc.close()
                raise OSError('OAuth endpoint unavailable') from None
            raise
        finally:
            common.HTTP_SOCKET_TIMEOUT = previous

    def __call__(self, item_id, remaining_seconds):
        import blizzard_collector
        self.deadline = self.monotonic() + remaining_seconds
        self.item_url = _base_entry(item_id, datetime.now(timezone.utc))['source_url']
        if self.client is None:
            factory = self.client_factory or blizzard_collector.ForeverAuctions
            try:
                self.client = factory(self.cfg, env_path=self.env_path)
            except Exception:
                raise _ConfigurationError('metadata credentials/configuration unavailable') from None
            self.client.settings = dict(self.client.settings, namespace='static-forever-kr',
                                        locale='ko_KR', region='kr')
            # No unbudgeted sleeps in the collector client's pacing hook.
            self.client.request_interval = 0
        previous = blizzard_collector.http_json
        try:
            blizzard_collector.http_json = self._request
            body, _ = self.client.get(f'/data/wow/item/{item_id}')
            return body
        finally:
            blizzard_collector.http_json = previous


def _result(status, requests=0, updated_count=0, **fields):
    return dict(status=status, requests=requests, updated_count=updated_count, **fields)


def _base_entry(item_id, now):
    return dict(item_id=item_id, namespace='static-forever-kr', locale='ko_KR',
                fetched_at=now.isoformat(),
                source_url=f'{PROVENANCE["source_url_prefix"]}{item_id}?namespace=static-forever-kr&locale=ko_KR')


def _retry_after_seconds(value, now):
    text = str(value).strip()
    if re.fullmatch(r'[0-9]+', text):
        seconds = int(text) if len(text) <= 8 else 86400
    else:
        try:
            stamp = parsedate_to_datetime(text)
            if stamp.tzinfo is None:
                return 900
            seconds = (stamp - now).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 900
    return min(86400, max(60, seconds))


def _validate_paths(cfg, db_path, cache_path, state_path, config_path, env_path, candidates_report):
    """Resolve aliases under trusted, stable ancestors before any input reads.

    Existing hardlinks are checked by identity as well as spelling. Outputs may
    not live inside any Git checkout (including worktrees with a .git file).
    """
    lexical = [Path(cache_path).absolute(), Path(state_path).absolute()]
    outputs = [path.resolve() for path in lexical]
    base = Path(cfg['base_dir']).resolve()
    inputs = [Path(db_path).resolve(), Path(config_path).resolve() if config_path else base / 'config.json',
              base / '.env']
    if env_path:
        inputs.append(Path(env_path).resolve())
    if candidates_report:
        inputs.append(Path(candidates_report).resolve())
    protected = inputs + [Path(str(path) + suffix) for path in inputs
                          for suffix in ('-wal', '-shm', '-journal')]
    for index, output in enumerate(outputs):
        if output.exists() and not output.is_file():
            raise ValueError('invalid output path')
        ancestors = (*lexical[index].parents, *output.parents)
        if any((parent / '.git').exists() for parent in ancestors):
            raise ValueError('repository output')
        for other in protected + outputs[:index]:
            if output == other or (output.exists() and other.exists() and output.samefile(other)):
                raise ValueError('input/output collision')
    return outputs


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def update_cache(cfg, *, db_path, cache_path, state_path, config_path=None,
                 env_path=None, candidates_report=None, max_requests=40,
                 budget_seconds=90, transport=None, now=None, monotonic=time.monotonic):
    if (type(max_requests) is not int or max_requests < 0
            or type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds)
            or budget_seconds < 0):
        return _result('configuration_error', reason='invalid_limits')
    started = monotonic()
    deadline = started + budget_seconds
    now = datetime.now(timezone.utc) if now is None else now
    try:
        cache_path, state_path = _validate_paths(cfg, db_path, cache_path, state_path,
                                                config_path, env_path, candidates_report)
    except (OSError, ValueError, TypeError, KeyError):
        return _result('invalid_paths')
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
        if (not isinstance(state, dict) or type(state.get('schema_version')) is not int
                or state['schema_version'] != 1 or 'cooldown_until' not in state
                or _timestamp(state.get('updated_at')) > now + timedelta(minutes=15)):
            return _result('state_error', reason='state_invalid')
        if state['cooldown_until'] is not None:
            until = _timestamp(state['cooldown_until'])
            if until > _timestamp(state['updated_at']) + timedelta(days=1):
                return _result('state_error', reason='state_invalid')
            if until > now:
                return _result('cooldown', cooldown_until=until.isoformat())
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return _result('state_error', reason='state_unavailable')
    try:
        if cfg.get('game_flavor') != 'forever':
            return _result('db_error', reason='forever_catalog_required')
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True,
                                     timeout=min(5, max(0, deadline - monotonic())))) as db:
            flavor = db.execute("SELECT value FROM meta WHERE key='game_flavor'").fetchone()
            if flavor != ('forever',):
                return _result('db_error', reason='forever_catalog_required')
            ids = [row[0] for row in db.execute(
                "SELECT item_id FROM items WHERE typeof(item_id)='integer' AND item_id>0 ORDER BY item_id")]
    except (OSError, sqlite3.Error, ValueError):
        return _result('db_error', reason='catalog_unavailable')
    loaded = load_item_cache(cache_path, now=now)
    if loaded['status'] == 'unreadable':
        return _result('cache_error', reason='cache_unreadable')
    items = {str(key): value for key, value in loaded['items'].items()}
    due = set()
    for item_id in ids:
        previous = items.get(str(item_id))
        if previous is None:
            due.add(item_id)
        else:
            age = now - _timestamp(previous['fetched_at'])
            if ((previous['state'] == 'named' and age > timedelta(days=30))
                    or (previous['state'] == 'not_found' and age >= timedelta(days=7))):
                due.add(item_id)
    candidates = set()
    if candidates_report:
        try:
            report = json.loads(Path(candidates_report).read_text(encoding='utf-8'))
            if not isinstance(report, dict) or not isinstance(report.get('comparisons'), list):
                return _result('candidates_error', reason='candidates_invalid')
            catalog = set(ids)
            candidates = {row['item_id'] for row in report['comparisons']
                          if isinstance(row, dict) and type(row.get('item_id')) is int
                          and row['item_id'] in catalog and str(row['item_id']) not in items}
        except (OSError, ValueError, TypeError, RecursionError):
            return _result('candidates_error', reason='candidates_unavailable')
    ordered = sorted(candidates & due) + [item_id for item_id in ids if item_id not in candidates
                                         and item_id in due]
    requests = updated_count = not_found_count = 0
    status, cooldown_until = 'ok', None
    if transport is None:
        transport = OfficialItemTransport(cfg, env_path=env_path, monotonic=monotonic)
    for item_id in ordered[:max_requests]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            status = 'budget_exhausted'
            break
        requests += 1
        try:
            body = transport(item_id, remaining)
        except _ConfigurationError:
            status = 'configuration_error'
            break
        except HTTPError as exc:
            code = exc.code
            retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
            exc.close()
            response_at = now + timedelta(seconds=max(0, monotonic() - started))
            if code == 404:
                items[str(item_id)] = dict(_base_entry(item_id, now), state='not_found')
                not_found_count += 1
                continue
            status = f'http_{code}'
            if code in (401, 403, 429):
                wait = 3600
                if code == 429:
                    wait = _retry_after_seconds(retry_after, response_at)
                cooldown_until = (response_at + timedelta(seconds=wait)).isoformat()
            break
        except TimeoutError:
            status = 'budget_exhausted' if monotonic() >= deadline else 'transport_error'
            break
        except Exception:
            status = 'transport_error'
            break
        if (not isinstance(body, dict) or type(body.get('id')) is not int or body['id'] != item_id
                or any(key in body and body[key] != PROVENANCE[key] for key in ('namespace', 'locale'))):
            continue
        name = body.get('name')
        if isinstance(name, dict):
            name = name.get('ko_KR')  # No English or guessed fallback.
        value = dict(_base_entry(item_id, now), state='named', name_ko=name)
        for source, target in (('item_class', 'class_id'), ('item_subclass', 'subclass_id')):
            if source in body:
                value[target] = body[source].get('id') if isinstance(body[source], dict) else None
        if _valid_entry(str(item_id), value, now):
            items[str(item_id)] = value
            updated_count += 1
    try:
        completed_at = now + timedelta(seconds=max(0, monotonic() - started))
        # Persist cooldown first, even when the subsequent cache replacement fails.
        _atomic_json(state_path, dict(schema_version=1, last_status=status,
            updated_at=completed_at.isoformat(), cooldown_until=cooldown_until))
        _atomic_json(cache_path, dict(schema_version=1,
            provenance=dict(PROVENANCE, fetched_at=now.isoformat()), items=items))
    except (OSError, ValueError, TypeError):
        return _result('write_error', requests, reason='metadata_write_failed')
    return _result(status, requests, updated_count, not_found_count=not_found_count,
                   cooldown_until=cooldown_until)


def build_parser():
    from common import parser
    result = parser('Forever KR 공식 아이템 이름 캐시 갱신 (경매 DB 읽기 전용)')
    result.add_argument('--env', help='Blizzard 자격증명 파일 경로 (값은 출력하지 않음)')
    result.add_argument('--cache', required=True, help='Git 외부 공개 메타데이터 JSON')
    result.add_argument('--state', required=True, help='Git 외부 쿨다운 상태 JSON')
    result.add_argument('--candidates-report', help='comparisons/item_id 우선순위 JSON')
    result.add_argument('--max-requests', type=int, default=40, help='최대 아이템 조회 시도 수 (기본 40)')
    result.add_argument('--budget-seconds', type=float, default=90, help='OAuth 포함 요청 시간 예산 (기본 90초)')
    return result


def main(argv=None):
    from common import ROOT, config_path as resolve_config_path, load_config
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        configured_db = (cfg.get('blizzard_forever') or {}).get('db_path')
        db = Path(args.db).resolve() if args.db else (
            Path(configured_db) if configured_db else resolve_config_path(cfg, 'db_path'))
        if not db.is_absolute():
            db = Path(cfg['base_dir']) / db
        result = update_cache(cfg, db_path=db, cache_path=args.cache, state_path=args.state,
                              config_path=Path(args.config).resolve() if args.config else ROOT / 'config.json',
                              env_path=args.env, candidates_report=args.candidates_report,
                              max_requests=args.max_requests, budget_seconds=args.budget_seconds)
    except Exception:
        result = _result('configuration_error', reason='configuration_unavailable')
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0 if result['status'] in ('ok', 'cooldown', 'budget_exhausted') else 1


if __name__ == '__main__':
    raise SystemExit(main())
