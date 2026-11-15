"""Optional names/search: offline fixtures, no operational state or network."""
import unittest

from tests import test_api_observer as fixtures


class SearchTests(unittest.TestCase):
    setUp = fixtures.ObserverTests.setUp
    tearDown = fixtures.ObserverTests.tearDown
    snapshot = fixtures.ObserverTests.snapshot
    observe = fixtures.ObserverTests.observe

    def ranked_market(self, count=501):
        self.snapshot(113, [fixtures.listing(i, 100, ident=i) for i in range(1, count + 1)])
        self.snapshot(114, [fixtures.listing(i, 100 + count + 1 - i, ident=i)
                            for i in range(1, count + 1)])

    def test_query_selects_rank40_and_rank501_before_output_limit(self):
        self.ranked_market()
        names = {40: dict(name_ko='마흔 번째', state='named'),
                 501: dict(name_ko='오백한 번째', state='named')}
        for query, item_id in [('마흔', 40), ('오백한', 501), ('501', 501)]:
            with self.subTest(query=query):
                data = self.observe(item_names=names, query=query, limit=30)
                self.assertEqual([c['item_id'] for c in data['comparisons']], [item_id])
                self.assertEqual(data['candidate_count'], 501)
                self.assertEqual(data['returned_count'], 1)
                self.assertEqual(data['selection'], dict(eligible_count=501, match_count=1,
                                 shown_count=1, named_count=1, unnamed_count=0, query=query))

    def test_official_name_query_uses_nfkc_and_casefold(self):
        self.ranked_market(3)
        names = {1: dict(name_ko='ＡＢＣ Straße 가루', state='named')}
        for query in ('abc', 'STRASSE', '가루', '  Ａbc  '):
            with self.subTest(query=query):
                data = self.observe(item_names=names, query=query)
                self.assertEqual([c['item_id'] for c in data['comparisons']], [1])

    def test_query_must_be_text_of_at_most64_characters(self):
        from common import AppError
        for query in (True, 7, [], '가' * 65, ' ' * 65):
            with self.subTest(query=query), self.assertRaises(AppError):
                self.observe(query=query)
        self.assertEqual(self.observe(query='가' * 64)['returned_count'], 0)

    def test_feature_reports_explicit_name_status_without_changing_default_shape(self):
        self.ranked_market(2)
        default = self.observe()
        self.assertNotIn('selection', default)
        self.assertNotIn('names_status', default)
        self.assertNotIn('name_status', default['comparisons'][0])
        missing = self.observe(query='1')
        self.assertEqual(missing['names_status'], dict(status='missing', reason='cache_not_supplied'))
        provided = self.observe(item_names={})
        self.assertEqual(provided['names_status'], dict(status='provided', reason=None))
        self.assertEqual(provided['selection'], dict(eligible_count=2, match_count=2,
                         shown_count=2, named_count=0, unnamed_count=2, query=None))

    def test_digit_query_is_exact_positive_id_never_name_or_price(self):
        self.ranked_market(41)
        names = {1: dict(name_ko='40 가루', state='named'),
                 4: dict(name_ko='가루', state='named')}
        for query, expected in [('4', [4]), ('004', [4]), ('４０', [40]),
                                ('0', []), ('40 가루', [1]), ('item#4', []),
                                ('0.0100g', []), ('-4', [])]:
            with self.subTest(query=query):
                self.assertEqual([c['item_id'] for c in self.observe(
                    item_names=names, query=query)['comparisons']], expected)

    def test_absent_cache_never_searches_item_table_or_fallback_names(self):
        self.ranked_market(2)
        self.db.execute('UPDATE items SET name=? WHERE item_id=1', ('가루',))
        for item_names in (None, {}):
            for query in ('가루', 'item#1'):
                with self.subTest(item_names=item_names, query=query):
                    self.assertEqual(self.observe(item_names=item_names, query=query)['comparisons'], [])
            self.assertEqual(self.observe(item_names=item_names, query='1')['returned_count'], 1)
        self.assertEqual(self.observe()['comparisons'][0]['name'], '가루')

    def test_counts_apply_after_eligibility_before_query_then_limit(self):
        self.ranked_market(41)
        names = {i: dict(name_ko='가루', state='named') for i in range(1, 36)}
        data = self.observe(item_names=names, query='가루', limit=30)
        self.assertEqual(data['selection'], dict(eligible_count=41, match_count=35,
                         shown_count=30, named_count=30, unnamed_count=0, query='가루'))
        unfiltered = self.observe(item_names=names, limit=40)
        self.assertEqual(unfiltered['selection']['named_count'], 35)
        self.assertEqual(unfiltered['selection']['unnamed_count'], 5)
        self.assertEqual((data['candidate_count'], data['returned_count']), (41, 30))
        self.assertEqual(self.observe(item_names={}, query='  ')['selection']['match_count'], 41)

    def test_enrichment_is_pure_display_only_with_identical_quotes_and_evidence(self):
        import copy
        from api_market_search import select_observations
        for ago in (180, 120, 60):
            self.snapshot(113, [fixtures.listing(7, 200), fixtures.listing(8, 150, ident=2)], ago=ago)
            self.snapshot(114, [fixtures.listing(7, 100), fixtures.listing(8, 150, ident=2),
                                fixtures.listing(9, 1, kind='auction')], ago=ago)
        baseline = self.observe()
        names = {7: dict(name_ko='평온초', state='named', item_class_id=999),
                 8: dict(name_ko='평온초', state='named'), 9: dict(name_ko='평온초', state='named')}
        saved = copy.deepcopy((baseline, names))
        rows, selection = select_observations(baseline['comparisons'], item_names=names, query='평온')
        enriched = self.observe(item_names=names, query='평온')
        self.assertEqual(enriched['comparisons'], rows)
        self.assertEqual(selection['eligible_count'], 1)
        self.assertEqual(rows[0]['name_status'], 'named')
        self.assertEqual({k: v for k, v in rows[0].items() if k not in ('name', 'name_ko', 'name_status')},
                         {k: v for k, v in baseline['comparisons'][0].items() if k != 'name'})
        for field in ('houses', 'pairs', 'thresholds', 'warnings'):
            self.assertEqual(enriched[field], baseline[field])
        self.assertEqual((baseline, names), saved)
        self.assertEqual(self.observe(item_names={7: dict(name_ko='위장', state='unresolved')},
                                      query='위장')['comparisons'], [])

    def test_old_renderer_stays_script_free_even_in_feature_mode(self):
        from api_market_observer import render
        self.ranked_market(2)
        html = render(self.observe(item_names={1: dict(name_ko='</script><script>alert(1)</script>',
                                                      state='named')}))
        self.assertNotIn('<script', html.lower())
        self.assertIn('&lt;script&gt;', html)

    def test_searchable_renderer_escapes_hostile_name_and_keeps_script_fixed(self):
        from html import escape
        import re
        import api_market_search as search
        self.assertTrue(hasattr(search, 'render_searchable_html'), 'standalone searchable renderer is missing')
        self.ranked_market(2)
        hostile = '</script><script>alert("owned")</script> " onfocus="alert(2) &'
        data = self.observe(item_names={1: dict(name_ko=hostile, state='named')})
        html = search.render_searchable_html(data)
        safe_html = search.render_searchable_html(self.observe(item_names={}))
        scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts, re.findall(r'<script>(.*?)</script>', safe_html, re.S))
        self.assertNotIn(hostile, html)
        self.assertIn(escape(hostile, quote=True), html)
        self.assertIn('data-official-name="' + escape(hostile, quote=True) + '"', html)
        self.assertIn('data-item-id="1"', html)
        self.assertIn('0.0100g → 0.0102g', html)
        self.assertNotRegex(html.lower(), r'<(?:script|link|img|iframe)[^>]+(?:src|href)\s*=')
        self.assertNotRegex(scripts[0], r'\b(?:fetch|eval|XMLHttpRequest)\b|innerHTML|https?://')

    def exercise_document(self, html, actions):
        from html.parser import HTMLParser
        import json
        import re
        import shutil
        import subprocess

        class Articles(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows = []

            def handle_starttag(self, tag, attrs):
                if tag == 'article':
                    values = dict(attrs)
                    self.rows.append(dict(dataset=dict(itemId=values['data-item-id'],
                                         officialName=values['data-official-name'])))

        parsed = Articles()
        parsed.feed(html)
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Node is required to exercise the actual inline script')
        program = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
for (const id of ['market-search','visible-count','search-empty','show-more']) {
  elements[id] = {value:'', hidden:true, disabled:true, textContent:'', events:{},
    addEventListener(name, fn) { this.events[name] = fn; }};
}
const document = {getElementById: id => elements[id], querySelectorAll: () => data.rows};
vm.runInNewContext(data.script, {document});
const states = [];
function state() { states.push({ids: data.rows.filter(r => !r.hidden).map(r => r.dataset.itemId),
  visible: elements['visible-count'].textContent, empty: elements['search-empty'].hidden,
  more: elements['show-more'].hidden}); }
state();
for (const action of data.actions) {
  if (action === null) elements['show-more'].events.click();
  else { elements['market-search'].value = action; elements['market-search'].events.input(); }
  state();
}
console.log(JSON.stringify(states));
"""
        payload = dict(script=re.search(r'<script>(.*?)</script>', html, re.S).group(1),
                       rows=parsed.rows, actions=actions)
        result = subprocess.run([node, '-e', program], input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_document_initial30_show_more_and_clearing_restores30(self):
        from api_market_search import render_searchable_html
        self.ranked_market(41)
        html = render_searchable_html(self.observe(item_names={}, limit=40))
        self.assertIn('id="show-more"', html)
        self.assertIn('최대 500개', html)
        self.assertIn('DB 전체 검색이 아닙니다', html)
        self.assertIn('<label for="market-search">', html)
        self.assertIn('id="loaded-count">40<', html)
        self.assertIn('id="eligible-count">41<', html)
        self.assertIn('<noscript>', html)
        self.assertEqual(html.count('<article '), 40, 'No-JS readers must retain all server-rendered prices')
        initial, expanded, reset = self.exercise_document(html, [None, ''])
        self.assertEqual((len(initial['ids']), initial['visible'], initial['more']), (30, '30', False))
        self.assertEqual((len(expanded['ids']), expanded['visible'], expanded['more']), (40, '40', True))
        self.assertEqual((len(reset['ids']), reset['visible'], reset['more']), (30, '30', False))

    def test_searchable_prose_counts_loaded_rows_not_currently_visible_rows(self):
        from api_market_observer import render
        from api_market_search import render_searchable_html
        self.ranked_market(360)
        data = self.observe(item_names={}, limit=500)
        legacy = render(data)
        legacy_prose = '<p>관측 후보 360개 중 360개 표시.'
        self.assertIn(legacy_prose, legacy)
        html = render_searchable_html(data)
        self.assertFalse(legacy_prose in html, 'Searchable prose must not claim every loaded row is visible')
        self.assertIn('문서에 360개 로드', html)
        self.assertIn('id="loaded-count">360<', html)
        self.assertEqual(html.count('<article '), 360)
        initial = self.exercise_document(html, [])[0]
        self.assertEqual((len(initial['ids']), initial['visible']), (30, '30'))
        self.assertEqual(render(data), legacy, 'The canonical script-free renderer must stay unchanged')

    def test_document_search_shows_all_loaded_matches_not_price_or_fallback(self):
        from api_market_search import render_searchable_html
        self.ranked_market(41)
        names = {i: dict(name_ko='ＡＢＣ Straße 가루', state='named') for i in range(1, 41)}
        html = render_searchable_html(self.observe(item_names=names, limit=41))
        actions = ['가루', '４０', '4', 'item#41', '0.0100g', 'STRASSE', '가루', '0', '']
        states = self.exercise_document(html, actions)[1:]
        self.assertEqual(len(states[0]['ids']), 40, 'Search must reveal every loaded match beyond first30')
        self.assertEqual(states[1]['ids'], ['40'])
        self.assertEqual(states[2]['ids'], ['4'])
        for index in (3, 4, 7):
            self.assertEqual(states[index]['ids'], [])
            self.assertFalse(states[index]['empty'])
        self.assertEqual(len(states[5]['ids']), 40)
        self.assertEqual(len(states[6]['ids']), 40)
        self.assertEqual(len(states[-1]['ids']), 30)

    def run_cli(self, extra, cache_document=None):
        import contextlib
        import io
        import json
        from pathlib import Path
        import sqlite3
        import tempfile
        from unittest.mock import patch
        import api_market_observer as observer

        scratch = 'C:/Users/home/AppData/Local/hermes/cache/scratch'
        self.db.commit()
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            base = Path(temp)
            db_path, cfg_path, cache_path = base / 'fixture.sqlite3', base / 'config.json', base / 'cache.json'
            target = sqlite3.connect(db_path)
            try:
                self.db.backup(target)
            finally:
                target.close()
            cfg_path.write_text(json.dumps(dict(self.cfg, db_path=str(db_path), game_flavor='forever')),
                                encoding='utf-8')
            if cache_document is not None:
                cache_path.write_text(json.dumps(cache_document), encoding='utf-8')
            build = observer.build_observations
            replacements = {'CACHE': str(cache_path), 'OUTPUT': str(base / 'report.html')}
            command = ['--config', str(cfg_path)] + [replacements.get(part, part) for part in extra]
            stream = io.StringIO()
            with patch.object(observer, 'build_observations', side_effect=lambda *a, **kw:
                              build(*a, **dict(kw, now=fixtures.NOW))), contextlib.redirect_stdout(stream):
                observer.main(command)
            if '--output' in command:
                return Path(command[command.index('--output') + 1]).read_text(encoding='utf-8')
            return json.loads(stream.getvalue())

    def test_cli_query_without_cache_is_explicitly_missing_and_id_still_works(self):
        self.ranked_market(41)
        data = self.run_cli(['--query', '40'])
        self.assertEqual([c['item_id'] for c in data['comparisons']], [40])
        self.assertEqual(data['names_status'], dict(status='missing', reason='cache_not_supplied'))

    def test_cli_loads_real_validated_cache_before_rank501_query(self):
        self.ranked_market(501)
        stamp = '2026-11-14T00:00:00+00:00'
        prefix = 'https://kr.api.blizzard.com/data/wow/item/'
        document = dict(schema_version=1, provenance=dict(game_flavor='forever',
                        namespace='static-forever-kr', locale='ko_KR', region='kr',
                        source_url_prefix=prefix, fetched_at=stamp), items={'501': dict(
                        item_id=501, state='named', name_ko='평온초', namespace='static-forever-kr',
                        locale='ko_KR', fetched_at=stamp,
                        source_url=prefix + '501?namespace=static-forever-kr&locale=ko_KR')})
        data = self.run_cli(['--cache', 'CACHE', '--query', '평온'], document)
        self.assertEqual([c['item_id'] for c in data['comparisons']], [501])
        self.assertEqual(data['comparisons'][0]['name'], '평온초')
        self.assertEqual(data['names_status']['status'], 'ok')
        self.assertEqual(data['names_status']['named_count'], 1)
        self.assertNotIn('items', data['names_status'])

    def test_cli_feature_html_uses_searchable_renderer_only_when_requested(self):
        self.ranked_market(41)
        html = self.run_cli(['--query', '40', '--format', 'html', '--output', 'OUTPUT'])
        self.assertIn('<script>', html)
        self.assertIn('data-item-id="40"', html)
        default_html = self.run_cli(['--format', 'html', '--output', 'OUTPUT'])
        self.assertNotIn('<script', default_html.lower())

    def test_mobile390_controls_and_long_names_do_not_overflow(self):
        from html import escape, unescape
        import json
        from pathlib import Path
        import re
        import shutil
        import subprocess
        import tempfile
        from api_market_search import render_searchable_html

        chrome = shutil.which('chrome') or 'C:/Program Files/Google/Chrome/Application/chrome.exe'
        if not Path(chrome).is_file():
            self.skipTest('Local Chromium is not installed')
        self.ranked_market(41)
        html = render_searchable_html(self.observe(item_names={1: dict(name_ko='X' * 200, state='named')}, limit=40))
        probe = r"""<script>
const input = document.getElementById('market-search');
const control = input.getBoundingClientRect();
document.querySelector('article details').open = true;
parent.document.body.dataset.probe = JSON.stringify({width:innerWidth,
  scroll:document.documentElement.scrollWidth, left:control.left, right:control.right,
  visible:document.querySelectorAll('article:not([hidden])').length,
  resources:performance.getEntriesByType('resource').length,
  label:!!document.querySelector('label[for="market-search"]'), disabled:input.disabled});
</script>"""
        with tempfile.TemporaryDirectory(dir='C:/Users/home/AppData/Local/hermes/cache/scratch') as temp:
            base = Path(temp)
            page = base / 'mobile.html'
            page.write_text('<!doctype html><body><iframe style="width:390px;height:844px;border:0" srcdoc="' +
                            escape(html.replace('</html>', probe + '</html>'), quote=True) + '"></iframe>', encoding='utf-8')
            result = subprocess.run([chrome, '--headless=new', '--disable-gpu', '--disable-background-networking',
                                     '--disable-component-update', '--no-first-run', '--no-default-browser-check',
                                     '--host-resolver-rules=MAP * ~NOTFOUND', '--user-data-dir=' + str(base / 'profile'),
                                     '--dump-dom', page.as_uri()], capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            match = re.search(r'data-probe="([^"]+)"', result.stdout)
            self.assertIsNotNone(match, 'Browser probe did not execute')
            measured = json.loads(unescape(match.group(1)))
        self.assertEqual(measured['width'], 390)
        self.assertLessEqual(measured['scroll'], 390, measured)
        self.assertGreaterEqual(measured['left'], 0)
        self.assertLessEqual(measured['right'], 390)
        self.assertEqual(measured['visible'], 30)
        self.assertEqual(measured['resources'], 0)
        self.assertTrue(measured['label'])
        self.assertFalse(measured['disabled'])

    def test_cli_output_cannot_replace_cache_input(self):
        from common import AppError
        self.ranked_market(2)
        with self.assertRaises(AppError):
            self.run_cli(['--cache', 'CACHE', '--output', 'CACHE'], {})

    def test_document_unicode_casefold_and_decimal_ids_match_server_selection(self):
        from api_market_search import render_searchable_html
        self.ranked_market(4)
        names = {1: dict(name_ko='ᾳ', state='named')}
        queries = ['αι', '٤', '４', '٠']
        html = render_searchable_html(self.observe(item_names=names))
        states = self.exercise_document(html, queries)[1:]
        for query, state in zip(queries, states):
            with self.subTest(query=query):
                expected = [str(c['item_id']) for c in self.observe(item_names=names, query=query)['comparisons']]
                self.assertEqual(state['ids'], expected)

    def test_pure_selector_preserves_output_limit_bounds(self):
        from api_market_search import select_observations
        from common import AppError
        for limit in (0, 501, True, 1.5, '30'):
            with self.subTest(limit=limit), self.assertRaises(AppError):
                select_observations([], item_names={}, limit=limit)

    def test_default_cli_does_not_import_optional_name_or_search_modules(self):
        import builtins
        from unittest.mock import patch
        self.ranked_market(2)
        original = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name in ('forever_item_cache', 'update_forever_item_cache', 'api_market_search'):
                self.fail('Default CLI imported optional module: ' + name)
            return original(name, *args, **kwargs)

        with patch.object(builtins, '__import__', side_effect=guarded):
            data = self.run_cli([])
        self.assertNotIn('selection', data)
        self.assertNotIn('names_status', data)

    def test_cli_missing_cache_preserves_status_and_id_query_but_no_name_matches(self):
        self.ranked_market(41)
        data = self.run_cli(['--cache', 'CACHE', '--query', '40'])
        self.assertEqual(data['names_status']['status'], 'missing')
        self.assertEqual(data['comparisons'][0]['item_id'], 40)
        self.assertEqual(data['comparisons'][0]['name_status'], 'unresolved')
        self.assertEqual(self.run_cli(['--cache', 'CACHE', '--query', '가루'])['returned_count'], 0)

    def test_document_empty_results_and_identifier_attributes_are_escaped(self):
        import copy
        from html import escape
        from api_market_search import render_searchable_html
        self.ranked_market(2)
        empty = render_searchable_html(self.observe(item_names={}, query='missing'))
        initial = self.exercise_document(empty, [])[0]
        self.assertEqual(initial['visible'], '0')
        self.assertFalse(initial['empty'])
        data = copy.deepcopy(self.observe(item_names={}))
        hostile = '" onfocus="alert(1)<svg>'
        data['comparisons'][0]['item_id'] = hostile
        html = render_searchable_html(data)
        self.assertIn('data-item-id="' + escape(hostile, quote=True) + '"', html)
        self.assertNotIn('<svg>', html)
