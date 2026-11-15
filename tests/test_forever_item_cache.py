"""Offline synthetic fixtures only; no credentials or network."""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

NOW = datetime(2026, 11, 15, 0, 0, tzinfo=timezone.utc)
PREFIX = 'https://kr.api.blizzard.com/data/wow/item/'


def entry(item_id=2447, **changes):
    value = dict(item_id=item_id, state='named', name_ko=' 평온초 ',
                 namespace='static-forever-kr', locale='ko_KR',
                 source_url=f'{PREFIX}{item_id}?namespace=static-forever-kr&locale=ko_KR',
                 fetched_at=NOW.isoformat())
    value.update(changes)
    return value


def document(items=None, **changes):
    value = dict(schema_version=1, provenance=dict(
        game_flavor='forever', namespace='static-forever-kr', region='kr',
        locale='ko_KR', source_url_prefix=PREFIX, fetched_at=NOW.isoformat()),
        items={'2447': entry()} if items is None else items)
    value.update(changes)
    return value


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'names.json'

    def load(self, value=None):
        if value is not None:
            self.path.write_text(json.dumps(value), encoding='utf-8')
        from forever_item_cache import load_item_cache
        try:
            return load_item_cache(self.path, now=NOW)
        except Exception as exc:
            self.fail(f'cache reader must not raise: {type(exc).__name__}')

    def test_file_failures_are_explicit_empty_sanitized_envelopes(self):
        for status, raw, error in (
                ('missing', None, None), ('invalid', '{broken', None),
                ('invalid', '\udcff', None), ('unreadable', '{}', PermissionError('SECRET /private'))):
            with self.subTest(status=status, raw=repr(raw)):
                self.path.unlink(missing_ok=True)
                if raw is not None:
                    self.path.write_bytes(raw.encode('utf-8', errors='surrogatepass'))
                if error:
                    with patch.object(Path, 'read_text', side_effect=error):
                        result = self.load()
                else:
                    result = self.load()
                self.assertEqual(result['status'], status)
                self.assertEqual(result['items'], {})
                self.assertEqual(result['named_count'], 0)
                self.assertTrue(result['reason'])
                self.assertNotIn('SECRET', json.dumps(result))

    def test_bad_file_provenance_rejects_every_record(self):
        invalid = [[], None, document(schema_version=True), document(schema_version=2),
                   document(items=[]), document(provenance={})]
        for key, wrong in [('namespace', 'static-kr'), ('locale', 'en_US'),
                           ('game_flavor', 'retail'), ('region', 'us'),
                           ('source_url_prefix', 'https://evil/'),
                           ('fetched_at', '2026-11-15T00:00:00'),
                           ('fetched_at', (NOW + timedelta(minutes=16)).isoformat())]:
            value = document()
            value['provenance'][key] = wrong
            invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                # None is JSON null here, not the helper's no-write sentinel.
                self.path.write_text(json.dumps(value), encoding='utf-8')
                result = self.load()
                self.assertEqual(result['status'], 'invalid')
                self.assertEqual(result['items'], {})
                self.assertEqual(result['named_count'], 0)

    def test_bad_record_identity_or_provenance_rejects_only_that_record(self):
        changes = [dict(item_id=True), dict(item_id=1.0), dict(item_id='2447'),
                   dict(item_id=0), dict(item_id=9), dict(namespace='static-kr'),
                   dict(namespace=None), dict(locale='en_US'), dict(locale=None),
                   dict(game_flavor='retail'), dict(region='us'),
                   dict(source_url=PREFIX + '9?namespace=static-forever-kr&locale=ko_KR'),
                   dict(source_url=PREFIX + '2447?locale=ko_KR&namespace=static-forever-kr'),
                   dict(fetched_at='yesterday'), dict(fetched_at=NOW.replace(tzinfo=None).isoformat()),
                   dict(fetched_at=(NOW + timedelta(minutes=16)).isoformat()), dict(state='error')]
        bad_records = [('2447', entry(**value)) for value in changes]
        bad_records += [(key, entry()) for key in ['02447', '+2447', '2447.0', '0', '-1', 'foo', '２４４７']]
        bad_records += [('2447', None), ('2447', [])]
        for key, bad in bad_records:
            with self.subTest(key=key, bad=bad):
                result = self.load(document({key: bad, '765': entry(765)}))
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(set(result['items']), {765})
                self.assertEqual(result['rejected_count'], 1)

    def test_unsafe_names_reject_record_without_inventing_fallback(self):
        names = [None, 4, '', ' ' * 3, '가' * 129, 'item#2447', 'ITEM#123',
                 '평온\n초', '평온\x00초', '평온\x7f초', '평온\ud800초']
        names += ['평온' + chr(code) + '초' for code in [0x202a, 0x202b, 0x202c, 0x202d, 0x202e,
                                                      0x2066, 0x2067, 0x2068, 0x2069]]
        for name in names:
            with self.subTest(name=repr(name)):
                result = self.load(document({'2447': entry(name_ko=name)}))
                self.assertEqual(result['items'], {})
                self.assertEqual(result['rejected_count'], 1)

    def test_display_only_class_ids_allow_zero_but_not_bool_or_coercion(self):
        valid = entry(class_id=0, subclass_id=0)
        self.assertEqual(self.load(document({'2447': valid}))['items'][2447]['class_id'], 0)
        for field in ('class_id', 'subclass_id'):
            for value in (True, False, -1, '0', 0.0, None):
                with self.subTest(field=field, value=value):
                    result = self.load(document({'2447': entry(**{field: value})}))
                    self.assertEqual(result['items'], {})
                    self.assertEqual(result['rejected_count'], 1)

    def test_negative_records_have_no_name_and_are_not_counted_as_named(self):
        negative = entry(state='not_found')
        del negative['name_ko']
        result = self.load(document({'2447': negative}))
        self.assertEqual(result['items'][2447]['state'], 'not_found')
        self.assertEqual(result['named_count'], 0)
        for name in ('가짜', None, ''):
            with self.subTest(name=name):
                result = self.load(document({'2447': dict(negative, name_ko=name)}))
                self.assertEqual(result['items'], {})

    def test_names_older_than_thirty_days_remain_usable_and_flagged(self):
        for age, old in [(timedelta(hours=4), False), (timedelta(days=30), False),
                         (timedelta(days=30, seconds=1), True), (timedelta(days=365), True)]:
            with self.subTest(age=age):
                value = document({'2447': entry(fetched_at=(NOW - age).isoformat())})
                result = self.load(value)
                self.assertEqual(result['old'], old)
                self.assertEqual(result['named_count'], 1)
                value = document()
                value['provenance']['fetched_at'] = (NOW - age).isoformat()
                self.assertEqual(self.load(value)['old'], old)

    def test_only_public_fields_leave_reader(self):
        value = document({'2447': entry(token='SECRET', headers={'Authorization': 'SECRET'})})
        value['provenance']['client_secret'] = 'SECRET'
        self.assertNotIn('SECRET', json.dumps(self.load(value)))

    def test_duplicate_keys_and_excessive_nesting_are_invalid_not_ambiguous(self):
        normal = json.dumps(document())
        duplicate_version = normal.replace('"schema_version": 1', '"schema_version": 2, "schema_version": 1')
        for raw in (duplicate_version, '[' * 20000 + '0' + ']' * 20000):
            with self.subTest(kind=raw[:60]):
                self.path.write_text(raw)
                result = self.load()
                self.assertEqual(result['status'], 'invalid')
                self.assertEqual(result['items'], {})

    def test_valid_cache_preserves_official_string_and_integer_keys(self):
        self.assertIsNotNone(importlib.util.find_spec('forever_item_cache'),
                             'offline cache reader is not implemented')
        result = self.load(document())
        self.assertEqual(result['status'], 'ok')
        self.assertIsNone(result['reason'])
        self.assertEqual(result['items'][2447]['name_ko'], ' 평온초 ')
        self.assertEqual((result['named_count'], result['rejected_count'], result['old']), (1, 0, False))
        self.assertEqual(result['provenance']['namespace'], 'static-forever-kr')
