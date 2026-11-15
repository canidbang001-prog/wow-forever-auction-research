"""Isolated, explicitly invoked report delivery; stdlib only.

No report, updater, credential client, configuration, or environment is imported.
A caller must separately opt in to ``deliver_summary`` and supply a sender.

The caller owns a local state root OUTSIDE every Git working tree and must pass
all input DB/config/cache paths in ``protected_paths`` (include input directories
where possible). SQLite ``-wal``, ``-shm``, and ``-journal`` siblings are protected
automatically. Roots cannot overlap protected paths; output symlink/junction
escapes and same-file/hardlink aliases are rejected before any output is opened.
Canonical ancestors and protected inputs must be trusted and remain stable for
the duration of the call: these checks are not an adversarial filesystem sandbox.

The persistent OS lock is never unlinked, even after a crash. A process crash
cannot prove whether Telegram accepted a message: expired in-progress attempts
therefore become uncertain, with suppression measured from the original attempt.
This bounds duplicates/spam; it cannot provide exactly-once delivery over HTTP.
Only fixed local status names, timestamps, fingerprints, counters, and a validated
message ID are returned/persisted. Summary text, credentials, URLs, remote error
bodies, and exception text are never logged or saved.
"""
from contextlib import contextmanager
import errno
import json
import os
import re
from pathlib import Path
import tempfile
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from datetime import datetime, timezone


class _LockBusy(Exception):
    pass


@contextmanager
def _exclusive_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+b') as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise _LockBusy() from None
            raise
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramSender:
    """Opt-in plain-text Bot API transport; credentials stay in memory.

    ``token`` is a Bot API token; ``chat_id`` must be a nonzero integer or its
    exact decimal spelling (not an @username). Invalid configuration raises only
    ``ValueError('invalid_telegram_configuration')``. The default object repr
    does not reveal the token. Do not serialize or introspect sender attributes.

    An injected opener is a callable ``opener(Request, timeout=15)`` returning a
    context-managed response with ``read(size)`` and optionally ``status``.
    The default HTTPS opener rejects redirects. No retries are performed here.
    Call results are ``{'status': 'success', 'message_id': positive_exact_int}``,
    ``{'status': 'failure'}``, or ``{'status': 'uncertain'}``. Success requires
    Telegram's exact true boolean, intended numeric chat, and exact text echo.
    """

    def __init__(self, token, chat_id, *, opener=None):
        if (not isinstance(token, str)
                or re.fullmatch(r'[1-9][0-9]*:[A-Za-z0-9_-]+', token) is None
                or not ((type(chat_id) is int and chat_id != 0)
                        or (isinstance(chat_id, str)
                            and re.fullmatch(r'-?[1-9][0-9]*', chat_id) is not None))):
            raise ValueError('invalid_telegram_configuration')
        try:
            intended_chat = int(chat_id)
        except (ValueError, TypeError, OverflowError):
            raise ValueError('invalid_telegram_configuration') from None
        self._token = token
        self._chat_id = intended_chat
        self._opener = opener if opener is not None else build_opener(_NoRedirect()).open

    def __call__(self, summary) -> dict:
        if not isinstance(summary, str) or not 1 <= len(summary) <= 3500:
            return {'status': 'failure'}
        try:
            summary.encode('utf-8')
        except UnicodeError:
            return {'status': 'failure'}
        payload = {'chat_id': self._chat_id, 'text': summary, 'entities': [],
                   'disable_notification': False, 'protect_content': False,
                   'link_preview_options': {'is_disabled': True}}
        request = Request('https://api.telegram.org/bot' + self._token + '/sendMessage',
                          data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with self._opener(request, timeout=15) as response:
                http_status = getattr(response, 'status', 200)
                if http_status != 200:
                    return {'status': 'failure' if type(http_status) is int
                            and 400 <= http_status <= 599 else 'uncertain'}
                raw = response.read(65537)
            if len(raw) > 65536:
                return {'status': 'uncertain'}
            result = json.loads(raw, object_pairs_hook=_unique_object)
        except HTTPError as error:
            try:
                error.close()
            except Exception:
                pass
            return {'status': 'failure' if type(error.code) is int
                    and 400 <= error.code <= 599 else 'uncertain'}
        except Exception:
            return {'status': 'uncertain'}
        if type(result) is dict and result.get('ok') is False:
            return {'status': 'failure'}
        if type(result) is not dict or result.get('ok') is not True:
            return {'status': 'uncertain'}
        message = result.get('result')
        if type(message) is not dict:
            return {'status': 'uncertain'}
        message_id, chat = message.get('message_id'), message.get('chat')
        if (type(message_id) is not int or message_id <= 0 or type(chat) is not dict
                or type(chat.get('id')) is not int or chat['id'] != self._chat_id
                or message.get('text') != summary):
            return {'status': 'uncertain'}
        return {'status': 'success', 'message_id': message_id}


def _empty_ledger():
    return {
        'schema_version': 1,
        'last_acked_fingerprint': None, 'last_acked_at': None,
        'last_attempt_fingerprint': None, 'last_attempt_at': None,
        'last_attempt_outcome': None,
        'uncertain_fingerprint': None, 'uncertain_at': None,
        'consecutive_failures': 0, 'backoff_until': None,
    }


def _fingerprint(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def _timestamp(value, now):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('timestamp')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed) or parsed > now:
        raise ValueError('timestamp')
    return parsed


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _read_ledger(path, now):
    try:
        with path.open('r', encoding='utf-8') as stream:
            raw = stream.read(16385)
    except FileNotFoundError:
        return _empty_ledger()
    if len(raw) > 16384:
        raise ValueError('size')
    ledger = json.loads(raw, object_pairs_hook=_unique_object)
    if type(ledger) is not dict or ledger.keys() != _empty_ledger().keys():
        raise ValueError('schema')
    if type(ledger['schema_version']) is not int or ledger['schema_version'] != 1:
        raise ValueError('version')
    times = {}
    for prefix in ('last_acked', 'last_attempt', 'uncertain'):
        fp, stamp = ledger[prefix + '_fingerprint'], ledger[prefix + '_at']
        if (fp is None) != (stamp is None) or (fp is not None and not _fingerprint(fp)):
            raise ValueError('pair')
        times[prefix] = None if stamp is None else _timestamp(stamp, now)
    outcome = ledger['last_attempt_outcome']
    if outcome not in (None, 'success', 'failure', 'uncertain', 'in_progress'):
        raise ValueError('outcome')
    if (outcome is None) != (times['last_attempt'] is None):
        raise ValueError('attempt')
    count, until = ledger['consecutive_failures'], ledger['backoff_until']
    if type(count) is not int or not 0 <= count <= 1000000 or (count == 0) != (until is None):
        raise ValueError('failures')
    if until is not None:
        # A future backoff is valid; timestamps identifying events are never future.
        backoff = _timestamp(until, datetime.max.replace(tzinfo=timezone.utc))
        if times['last_attempt'] is None or (outcome == 'failure'
                                               and backoff < times['last_attempt']):
            raise ValueError('backoff')
    for prefix in ('last_acked', 'uncertain'):
        if times[prefix] is not None and (times['last_attempt'] is None
                                         or times[prefix] > times['last_attempt']):
            raise ValueError('order')
    if outcome == 'success' and (ledger['last_acked_fingerprint'] != ledger['last_attempt_fingerprint']
                                 or times['last_acked'] != times['last_attempt']):
        raise ValueError('ack')
    if outcome == 'success' and (count != 0 or times['uncertain'] is not None):
        raise ValueError('success')
    if outcome == 'failure' and count == 0:
        raise ValueError('failure')
    if outcome == 'uncertain' and (ledger['uncertain_fingerprint'] != ledger['last_attempt_fingerprint']
                                   or times['uncertain'] != times['last_attempt']):
        raise ValueError('uncertain')
    return ledger


def _write_ledger(path, ledger):
    fd, name = tempfile.mkstemp(prefix='.delivery-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(ledger, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _prepare_paths(root, protected_paths):
    # Canonical ancestors must be trusted and stable: this is not a sandbox
    # against an adversary concurrently replacing directories or symlinks.
    root = Path(root).resolve()
    if any((ancestor / '.git').exists() for ancestor in (root, *root.parents)):
        raise ValueError('git')
    if isinstance(protected_paths, (str, bytes, os.PathLike)):
        raise ValueError('protected_paths')
    protected = []
    for supplied in protected_paths:
        item = Path(supplied).resolve()
        protected.extend([item, *(Path(str(item) + suffix).resolve()
                                  for suffix in ('-wal', '-shm', '-journal'))])
    if any(root == item or root in item.parents or item in root.parents for item in protected):
        raise ValueError('protected')
    ledger = (root / 'delivery-ledger.json').resolve()
    lock = (root / 'delivery.lock').resolve()
    for target in (ledger, lock):
        if target.parent != root or target == root:
            raise ValueError('escape')
        # Reject every multiply-linked output, including aliases to descendants
        # of protected directories, without traversing or reading input trees.
        if target.exists() and target.stat().st_nlink > 1:
            raise ValueError('hardlink')
        for item in protected:
            if target == item or (target.exists() and item.exists() and target.samefile(item)):
                raise ValueError('alias')
    if ledger == lock or (ledger.exists() and lock.exists() and ledger.samefile(lock)):
        raise ValueError('alias')
    root.mkdir(parents=True, exist_ok=True)
    return ledger, lock


def deliver_summary(root, fingerprint, summary, *, now=None, sender=None,
                    protected_paths=(), cooldown_seconds=21600,
                    backoff_seconds=1800, in_progress_seconds=300) -> dict:
    """Deliver a 1..3500-character summary under an isolated durable ledger.

    ``fingerprint`` must be exactly 64 lowercase hexadecimal characters. ``now``
    defaults to aware UTC, accepts aware datetimes and normalizes them to UTC.
    ``sender(summary)`` must return a dict with status success/failure/uncertain;
    success additionally requires an exact positive int ``message_id``. Other
    results and exceptions are uncertain. ``sender=None`` performs no state I/O
    and returns telegram_unconfigured without attempting or acknowledging.

    Defaults: acknowledgement and uncertainty cooldown 21600 seconds, explicit
    failure backoff 1800 seconds doubling across fingerprints up to 21600 seconds,
    and crashed in-progress grace 300 seconds. Failure counters reset on success.
    Timing overrides are nonnegative exact ints (in-progress must be positive).
    The exclusive nonblocking OS lock spans validation, decision, durable attempt,
    the sender call, and final persistence. Missing ledgers initialize; malformed,
    inconsistent, unreadable, or future-dated event state fails closed unchanged.

    Return statuses: sent (with only message_id), telegram_unconfigured,
    skip_unchanged, skip_cooldown, skip_backoff, skip_uncertain, skip_in_progress,
    delivery_locked, invalid_input, path_invalid, ledger_invalid, or
    ledger_write_failed (with attempted: bool). On final-write failure the durable
    in-progress attempt remains; attempted=True is NOT an acknowledgement.
    """
    now = now if now is not None else datetime.now(timezone.utc)
    if (not _fingerprint(fingerprint)
            or not isinstance(summary, str) or not 1 <= len(summary) <= 3500
            or not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None
            or any(type(v) is not int or v < 0 for v in (cooldown_seconds, backoff_seconds))
            or type(in_progress_seconds) is not int or in_progress_seconds <= 0):
        return {'status': 'invalid_input'}
    now = now.astimezone(timezone.utc)
    if sender is None:
        return {'status': 'telegram_unconfigured'}
    try:
        path, lock_path = _prepare_paths(root, protected_paths)
    except (OSError, ValueError, TypeError, RuntimeError):
        return {'status': 'path_invalid'}
    try:
        with _exclusive_lock(lock_path):
            return _deliver_locked(path, fingerprint, summary, now, sender,
                                   cooldown_seconds, backoff_seconds, in_progress_seconds)
    except _LockBusy:
        return {'status': 'delivery_locked'}
    except OSError:
        return {'status': 'ledger_write_failed', 'attempted': False}


def _deliver_locked(path, fingerprint, summary, now, sender,
                    cooldown_seconds, backoff_seconds, in_progress_seconds):
    try:
        ledger = _read_ledger(path, now)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return {'status': 'ledger_invalid'}
    if ledger['last_acked_fingerprint'] == fingerprint:
        return {'status': 'skip_unchanged'}
    if ledger['last_attempt_outcome'] == 'in_progress':
        elapsed = (now - datetime.fromisoformat(ledger['last_attempt_at'])).total_seconds()
        if elapsed < in_progress_seconds:
            return {'status': 'skip_in_progress'}
        ledger.update(last_attempt_outcome='uncertain',
                      uncertain_fingerprint=ledger['last_attempt_fingerprint'],
                      uncertain_at=ledger['last_attempt_at'])
        _write_ledger(path, ledger)
    if ledger['uncertain_at'] is not None:
        elapsed = (now - datetime.fromisoformat(ledger['uncertain_at'])).total_seconds()
        if elapsed < cooldown_seconds:
            return {'status': 'skip_uncertain'}
    if ledger['last_acked_at'] is not None:
        elapsed = (now - datetime.fromisoformat(ledger['last_acked_at'])).total_seconds()
        if elapsed < cooldown_seconds:
            return {'status': 'skip_cooldown'}
    if ledger['backoff_until'] is not None and now < datetime.fromisoformat(ledger['backoff_until']):
        return {'status': 'skip_backoff'}
    ledger.update(last_attempt_fingerprint=fingerprint, last_attempt_at=now.isoformat(),
                  last_attempt_outcome='in_progress')
    try:
        _write_ledger(path, ledger)
    except OSError:
        return {'status': 'ledger_write_failed', 'attempted': False}
    try:
        outcome = sender(summary)
    except Exception:
        outcome = None
    if not isinstance(outcome, dict):
        outcome = {}
    status = outcome.get('status')
    message_id = outcome.get('message_id')
    if status != 'failure' and not (status == 'success' and type(message_id) is int and message_id > 0):
        ledger.update(last_attempt_outcome='uncertain', uncertain_fingerprint=fingerprint,
                      uncertain_at=now.isoformat())
        try:
            _write_ledger(path, ledger)
        except OSError:
            return {'status': 'ledger_write_failed', 'attempted': True}
        return {'status': 'uncertain'}
    if status == 'failure':
        failures = ledger['consecutive_failures'] + 1
        delay = min(21600, backoff_seconds * (2 ** min(failures - 1, 16)))
        ledger.update(last_attempt_outcome='failure', consecutive_failures=failures,
                      backoff_until=datetime.fromtimestamp(now.timestamp() + delay,
                                                           timezone.utc).isoformat())
        try:
            _write_ledger(path, ledger)
        except OSError:
            return {'status': 'ledger_write_failed', 'attempted': True}
        return {'status': 'failed'}
    ledger.update(last_acked_fingerprint=fingerprint, last_acked_at=now.isoformat(),
                  last_attempt_fingerprint=fingerprint, last_attempt_at=now.isoformat(),
                  last_attempt_outcome='success', uncertain_fingerprint=None,
                  uncertain_at=None, consecutive_failures=0, backoff_until=None)
    try:
        _write_ledger(path, ledger)
    except OSError:
        return {'status': 'ledger_write_failed', 'attempted': True}
    return {'status': 'sent', 'message_id': outcome['message_id']}
