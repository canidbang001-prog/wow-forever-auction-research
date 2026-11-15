"""Explicit orchestration: finish offline artifacts before any optional send."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from common import AppError, ROOT, load_config, main_guard, parser
from api_report_delivery import TelegramSender, deliver_summary
from api_report_refresh import atomic_write_text, refresh_report, validate_report_paths


def configured_telegram_sender(chat_id, *, env_path=None, environ=None):
    """Opt-in only. Read one configured key; never load it into process globals."""
    environment = os.environ if environ is None else environ
    token = environment.get('TELEGRAM_BOT_TOKEN')
    try:
        if not token and env_path is not None:
            path = Path(env_path)
            if path.stat().st_size > 262144:
                return None
            matches = []
            with path.open('r', encoding='utf-8-sig') as handle:
                for line in handle:
                    if line.lstrip().startswith('#'):
                        continue
                    key, separator, value = line.partition('=')
                    if separator and key.strip() == 'TELEGRAM_BOT_TOKEN':
                        value = value.strip()
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('\"', "'"):
                            value = value[1:-1]
                        matches.append(value)
            if len(matches) != 1:
                return None
            token = matches[0]
        if not token:
            return None
        return TelegramSender(token, chat_id)
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def run_report_service(db_path, cfg, output_root, *, config_path=None, cache_path=None,
                       now=None, sender=None, protected_paths=(), **observation_options):
    """Real Telegram requires live origin; injected fake senders support offline QA."""
    now = now or datetime.now(timezone.utc)
    protected = [Path(path).resolve() for path in
                 (db_path, config_path, cache_path, *protected_paths) if path is not None]
    protected.extend(Path(str(Path(db_path).resolve()) + suffix) for suffix in ('-wal', '-shm', '-journal'))
    root, _ = validate_report_paths(output_root, protected)
    result = refresh_report(db_path, cfg, root, config_path=config_path, cache_path=cache_path,
                            now=now, **observation_options)
    if isinstance(sender, TelegramSender) and result['report'].get('mode') != 'live':
        delivery = {'status': 'nonlive_blocked'}
    else:
        delivery = deliver_summary(root, result['fingerprint'], result['summary'],
                                   now=now, sender=sender, protected_paths=protected)
    result['delivery'] = delivery
    result['status']['delivery'] = delivery
    try:
        atomic_write_text(Path(result['paths']['status']),
                          json.dumps(result['status'], ensure_ascii=False, indent=2, allow_nan=False))
    except OSError:
        result['status_write_failed'] = True
    return result


def main(argv=None):
    p = parser(__doc__)
    p.add_argument('--output-root', required=True)
    p.add_argument('--cache')
    p.add_argument('--houses')
    p.add_argument('--limit', type=int, default=500)
    p.add_argument('--window-hours', type=float, default=72)
    p.add_argument('--max-age-hours', type=float, default=3)
    p.add_argument('--max-skew-minutes', type=float, default=15)
    p.add_argument('--min-pairs', type=int, default=3)
    p.add_argument('--send', action='store_true', help='명시적으로 텔레그램 전송 허용')
    p.add_argument('--chat-id', help='명시적인 숫자 Telegram 대상 ID')
    p.add_argument('--telegram-env', help='기존 Telegram 설정 env 파일; --send일 때만 읽음')
    args = p.parse_args(argv)
    if not args.db:
        raise AppError('--db로 기존 관측 DB를 명시해야 합니다.')
    if args.send and (not args.chat_id or not re.fullmatch(r'-?[1-9][0-9]*', args.chat_id)):
        raise AppError('--send에는 명시적인 숫자 --chat-id가 필요합니다.')
    houses = None
    if args.houses is not None:
        if not re.fullmatch(r'[1-9]\d*(,[1-9]\d*)*', args.houses):
            raise AppError('--houses는 113,114와 같은 양의 정수 목록입니다.')
        houses = [int(value) for value in args.houses.split(',')]
    protected = [args.telegram_env] if args.telegram_env else []
    # Validate outputs before reading a credential source or generating any files.
    validate_report_paths(args.output_root, [args.db, args.config, args.cache, *protected])
    sender = configured_telegram_sender(args.chat_id, env_path=args.telegram_env) if args.send else None
    cfg = load_config(args.config)
    result = run_report_service(args.db, cfg, args.output_root, config_path=args.config or ROOT / 'config.json',
                               cache_path=args.cache, sender=sender, protected_paths=protected,
                               house_ids=houses, limit=args.limit, window_hours=args.window_hours,
                               max_age_hours=args.max_age_hours, max_skew_minutes=args.max_skew_minutes,
                               min_pairs=args.min_pairs)
    print(json.dumps(dict(report_status='ok', delivery=result['delivery'], paths=result['paths'],
                          status_write_failed=result.get('status_write_failed', False)), ensure_ascii=False))


if __name__ == '__main__':
    main_guard(main)
