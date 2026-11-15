"""Standalone no-agent addon runner. Copy under either cron script basename.

Run the metadata mode in a dedicated child: it must not patch HTTP globals in
an agent or collector process. Existing collection and its lock are untouched.
Only sanitized local state is printed; child output/errors are never relayed.
Profile ancestors and the separately reviewed runtime snapshot are trusted.
"""
import json
import os
from pathlib import Path
import subprocess
import sys


def run_job(mode, *, home=None, launcher=subprocess.run):
    if mode not in ('report', 'metadata'):
        return dict(state='blocked_configuration', returncode=2)
    home = Path(home or os.environ.get('HERMES_HOME') or Path.home()/'AppData/Local/hermes').resolve()
    data = home/'data/wow-forever-auction'
    data.mkdir(parents=True, exist_ok=True)
    with (data/f'{mode}-job.lock').open('a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return dict(state='skipped_overlap', returncode=0)
        try:
            return _run_job(mode, home=home, launcher=launcher)
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run_job(mode, *, home=None, launcher=subprocess.run):
    home = Path(home or os.environ.get('HERMES_HOME') or Path.home()/'AppData/Local/hermes').resolve()
    data = home / 'data/wow-forever-auction'
    try:
        if mode not in ('report', 'metadata'):
            raise ValueError('mode')
        settings = json.loads((data/'report-runtime.json').read_text(encoding='utf-8'))
        source = Path(settings['source_root']).resolve()
        houses = settings['house_ids']
        chat = settings['chat_id']
        if (settings.get('schema_version') != 1 or not source.is_relative_to((data/'report-runtime').resolve())
                or not isinstance(houses, list) or not houses
                or any(type(value) is not int or value <= 0 for value in houses)
                or type(chat) is not int or chat <= 0
                or any((parent/'.git').exists() for parent in (source, *source.parents))):
            raise ValueError('settings')
        module = 'api_report_service.py' if mode == 'report' else 'forever_item_updater.py'
        script = (source/module).resolve()
        if script.parent != source or not script.is_file():
            raise ValueError('source')
        output = data/'reports/hourly'
        command = [sys.executable, str(script), '--config', str(data/'config.forever.json'),
                   '--db', str(data/'auction.sqlite3'), '--cache', str(data/'item-names.json')]
        if mode == 'report':
            command += ['--output-root', str(output), '--houses', ','.join(map(str,houses)),
                        '--send', '--chat-id', str(chat), '--telegram-env', str(home/'.env')]
        else:
            command += ['--state', str(data/'item-names-state.json'),
                        '--env', str(data/'secrets/blizzard.env'),
                        '--max-requests', '40', '--budget-seconds', '90']
            if (output/'report-latest.json').is_file():
                command += ['--candidates-report', str(output/'report-latest.json')]
    except (OSError, ValueError, TypeError, KeyError):
        return dict(state='blocked_configuration', returncode=2)
    try:
        child = launcher(command, cwd=source, capture_output=True, text=True,
                         encoding='utf-8', errors='replace', timeout=150)
    except subprocess.TimeoutExpired:
        return dict(state='timeout', returncode=124)
    except OSError:
        return dict(state='execution_error', returncode=1)
    return dict(state='success' if child.returncode == 0 else 'failed', returncode=child.returncode)


def main():
    mode = 'metadata' if Path(__file__).stem == 'wow_forever_metadata' else 'report'
    try:
        result = run_job(mode)
    except Exception:
        result = dict(state='runner_error', returncode=1)
    print(json.dumps(result))
    return result['returncode']


if __name__ == '__main__':
    raise SystemExit(main())
