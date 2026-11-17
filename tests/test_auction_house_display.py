"""User display aliases never identify markets or change price observations."""
import copy
from html import escape
from pathlib import Path
import sqlite3
import unittest

from api_market_observer import build_observations, render
from api_market_search import render_searchable_html
from api_report_summary import notification_fingerprint, render_summary
from blizzard_collector import upsert_houses
from common import AppError, set_meta
from tests.test_api_observer import NOW, listing
from tests import test_api_observer as observer_fixture


class AuctionHouseDisplayTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        root = Path(__file__).resolve().parents[1]
        self.db.executescript((root / 'schema.sql').read_text(encoding='utf-8'))
        set_meta(self.db, 'game_flavor', 'forever')
        set_meta(self.db, 'data_mode', 'fixture')
        self.cfg = dict(blizzard_forever=dict(house_id=None, market_confirmed=False))

    def tearDown(self):
        self.db.close()

    def report(self, names=('Horde', 'Blackwater'), houses=(113, 114), snapshots=True):
        upsert_houses(self.db, 1, [dict(id=ident, name=name, side='horde')
                                  for ident, name in zip(houses, names)], NOW.isoformat())
        if snapshots:
            # Both comparison directions must use the same market labels.
            for ident, prices in zip(houses, ((100, 300), (200, 150))):
                observer_fixture.ObserverTests.snapshot(
                    self, ident, [listing(7, prices[0]), listing(8, prices[1], ident=2)])
        return build_observations(self.db, self.cfg, now=NOW, house_ids=list(houses))

    def test_known_aliases_win_over_api_and_conflicting_display_names_in_all_renderers(self):
        for names in (('Horde', 'Horde'), ('Horde', 'Blackwater'),
                      ('충돌한 호드 이름', '충돌한 중립 이름')):
            with self.subTest(names=names):
                self.db.execute('DELETE FROM blizzard_snapshots')
                report = self.report(names)
                for house in report['houses']:
                    house['display_name'] = '이전 표시 이름'
                before = copy.deepcopy(report)
                for renderer in (render, render_searchable_html, render_summary):
                    with self.subTest(renderer=renderer.__name__):
                        text = renderer(report)
                        self.assertIn('호드 경매장 (#113)', text)
                        self.assertIn('중립 경매장 (#114)', text)
                        for old_name in (*names, '이전 표시 이름'):
                            self.assertNotIn(old_name, text)
                        self.assertNotIn('시장 #113', text)
                        self.assertNotIn('시장 #114', text)
                        self.assertNotIn(' · #113 → #114', text)
                        if renderer is not render_summary:
                            self.assertIn('호드 경매장 (#113) → 중립 경매장 (#114)', text)
                            self.assertIn('중립 경매장 (#114) → 호드 경매장 (#113)', text)
                        else:
                            self.assertEqual(text.count('호드 경매장 (#113)'), 3)
                            self.assertEqual(text.count('중립 경매장 (#114)'), 3)
                self.assertEqual(report, before)
                self.assertIsNone(self.cfg['blizzard_forever']['house_id'])
                self.assertFalse(self.cfg['blizzard_forever']['market_confirmed'])

    def test_unknown_house_keeps_existing_escaped_html_and_numeric_summary_fallbacks(self):
        raw_name = '<img src=x onerror="alert(1)">'
        report = self.report(('Horde', raw_name), (113, 115))
        for renderer in (render, render_searchable_html):
            text = renderer(report)
            self.assertIn(f'#115 {escape(raw_name, quote=True)}', text)
            self.assertNotIn(raw_name, text)
            self.assertIn('호드 경매장 (#113) → #115', text)
        self.assertIn('시장 #115', render_summary(report))
        self.assertEqual(report['houses'][1]['name'], raw_name)
        self.assertEqual(report['houses'][1]['house_id'], 115)

    def test_blocked_house_diagnostics_still_lead_with_aliases(self):
        report = self.report(snapshots=False)
        for renderer in (render, render_searchable_html, render_summary):
            text = renderer(report)
            self.assertIn('호드 경매장 (#113)', text)
            self.assertIn('중립 경매장 (#114)', text)
            self.assertIn('no_snapshot', text)
            self.assertNotIn('Blackwater', text)

    def test_display_names_do_not_change_numeric_report_or_notification_identity(self):
        report = self.report()
        changed = copy.deepcopy(report)
        for house in changed['houses']:
            house['name'] = '충돌한 API 이름'
            house['display_name'] = '충돌한 표시 이름'
        self.assertEqual(notification_fingerprint(report), notification_fingerprint(changed))
        for renderer in (render, render_searchable_html, render_summary):
            self.assertEqual(renderer(report), renderer(changed))
        self.assertEqual([h['house_id'] for h in report['houses']], [113, 114])
        self.assertEqual([h['name'] for h in report['houses']], ['Horde', 'Blackwater'])
        self.assertEqual([(c['item_id'], c['gap_copper']) for c in report['comparisons']],
                         [(8, 150), (7, 100)])

    def test_missing_requested_houses_use_aliases_in_the_report_error(self):
        for houses, labels in (([113, 114], ['호드 경매장 (#113)', '중립 경매장 (#114)']),
                               ([115], ['설정한 경매장 ID가 색인에 없습니다: 115'])):
            with self.subTest(houses=houses):
                with self.assertRaises(AppError) as caught:
                    build_observations(self.db, self.cfg, now=NOW, house_ids=houses)
                for label in labels:
                    self.assertIn(label, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
