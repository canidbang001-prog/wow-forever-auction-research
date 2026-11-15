"""Offline Forever KR item names. This module never imports an API client.

JSON v1: {schema_version: 1, provenance: {game_flavor, namespace, region,
locale, source_url_prefix, fetched_at}, items: {canonical_id: entry}}.
load_item_cache(path, now=None) returns a non-raising status envelope with
integer-keyed, validated entries. Original official display names are retained.

Envelope keys: status (ok/missing/unreadable/invalid), reason (None or a fixed
code), items, named_count, rejected_count, old, provenance. Invalid file-level
provenance rejects the file; invalid records are omitted individually. Entries
require item_id, state (named/not_found), namespace, locale, source_url and an
aware fetched_at. named additionally requires name_ko; not_found forbids it.
Optional class_id/subclass_id are display-only integers, including zero.
old means file or entry metadata is older than 30 days, not unusable auction
data; there is no three-hour name cutoff. now is an aware datetime or None.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import unicodedata

PROVENANCE = dict(game_flavor='forever', namespace='static-forever-kr',
                  region='kr', locale='ko_KR',
                  source_url_prefix='https://kr.api.blizzard.com/data/wow/item/')
ENTRY_FIELDS = ('item_id', 'state', 'name_ko', 'namespace', 'locale', 'game_flavor',
                'region', 'source_url', 'fetched_at', 'class_id', 'subclass_id')


def _empty(status, reason):
    return dict(status=status, reason=reason, items={}, named_count=0,
                rejected_count=0, old=False, provenance=None)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('naive timestamp')
    return stamp.astimezone(timezone.utc)


def _valid_entry(key, entry, now):
    if not re.fullmatch(r'[1-9][0-9]*', key) or not isinstance(entry, dict):
        return False
    item_id = entry.get('item_id')
    if type(item_id) is not int or item_id <= 0 or str(item_id) != key:
        return False
    if any(entry.get(k) != PROVENANCE[k] for k in ('namespace', 'locale')):
        return False
    if any(k in entry and entry[k] != PROVENANCE[k] for k in ('game_flavor', 'region')):
        return False
    expected = f'{PROVENANCE["source_url_prefix"]}{key}?namespace=static-forever-kr&locale=ko_KR'
    if entry.get('source_url') != expected or entry.get('state') not in ('named', 'not_found'):
        return False
    if entry['state'] == 'not_found' and 'name_ko' in entry:
        return False
    if entry['state'] == 'named':
        name = entry.get('name_ko')
        if (not isinstance(name, str) or not 1 <= len(name) <= 128 or not name.strip()
                or re.fullmatch(r'item#[0-9]+', name.strip(), re.IGNORECASE)
                or any(unicodedata.category(c) in ('Cc', 'Cs')
                       or 0x202a <= ord(c) <= 0x202e or 0x2066 <= ord(c) <= 0x2069
                       for c in name)):
            return False
    for field in ('class_id', 'subclass_id'):
        if field in entry and (type(entry[field]) is not int or entry[field] < 0):
            return False
    try:
        return _timestamp(entry.get('fetched_at')) <= now + timedelta(minutes=15)
    except (ValueError, TypeError, OverflowError):
        return False


def load_item_cache(path, now=None):
    now = datetime.now(timezone.utc) if now is None else now
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
    except FileNotFoundError:
        return _empty('missing', 'cache_missing')
    except (UnicodeError, ValueError, RecursionError):
        return _empty('invalid', 'cache_invalid_json')
    except OSError:
        return _empty('unreadable', 'cache_unreadable')
    if (not isinstance(data, dict) or type(data.get('schema_version')) is not int
            or data['schema_version'] != 1 or not isinstance(data.get('items'), dict)
            or not isinstance(data.get('provenance'), dict)):
        return _empty('invalid', 'cache_invalid_schema')
    provenance = data['provenance']
    try:
        if (any(provenance.get(key) != value for key, value in PROVENANCE.items())
                or _timestamp(provenance.get('fetched_at')) > now + timedelta(minutes=15)):
            return _empty('invalid', 'cache_invalid_provenance')
    except (ValueError, TypeError, OverflowError):
        return _empty('invalid', 'cache_invalid_provenance')
    items = {int(key): {field: value[field] for field in ENTRY_FIELDS if field in value}
             for key, value in data['items'].items() if _valid_entry(key, value, now)}
    provenance = {key: provenance[key] for key in (*PROVENANCE, 'fetched_at')}
    return dict(status='ok', reason=None, items=items,
                named_count=sum(value['state'] == 'named' for value in items.values()),
                rejected_count=len(data['items']) - len(items),
                old=any(now - _timestamp(value['fetched_at']) > timedelta(days=30)
                        for value in [provenance, *items.values()]), provenance=provenance)
