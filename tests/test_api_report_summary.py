"""Pure summary/fingerprint contracts: synthetic observations, no runtime data."""
import copy
import importlib.util
import unittest


def report_fixture(count=32):
    rows = []
    for ident in range(1, count + 1):
        rows.append(dict(item_id=ident, name=f'item#{ident}', lower_quote_house=113,
                         higher_quote_house=114,
                         source_quote=dict(min_unit_price=100, qty_at_min=2, total_qty=3, listing_count=2),
                         target_quote=dict(min_unit_price=10000-ident, qty_at_min=4, total_qty=5, listing_count=3),
                         gap_copper=9900-ident, status='repeated_gap',
                         evidence={'paired_observations': 3}, history=[],
                         source_snapshot_id=1, reference_snapshot_id=2))
    return dict(schema_version=1, generated_at='2026-11-15T04:00:00+00:00', mode='fixture',
                game_flavor='forever', region='kr', ruleset=1, timezone_offset_hours=9,
                thresholds=dict(window_hours=72, min_pairs=3, max_age_hours=3, max_skew_minutes=15),
                houses=[dict(house_id=113, status='available', reasons=[], source_epoch=1794715140),
                        dict(house_id=114, status='available', reasons=[], source_epoch=1794715140)],
                pairs=[dict(house_ids=[113,114], status='available', reasons=[])],
                candidate_count=count, returned_count=count, comparisons=rows,
                names_status=dict(status='missing', old=False))


class SummaryTests(unittest.TestCase):
    def test_fingerprint_tracks_price_not_display_or_ingestion_churn(self):
        self.assertIsNotNone(importlib.util.find_spec('api_report_summary'),
                             'pure summary/fingerprint module is missing')
        from api_report_summary import notification_fingerprint
        for mode in ('fixture', 'live', 'unknown'):
            with self.subTest(mode=mode):
                data = report_fixture()
                data['mode'] = mode
                before = copy.deepcopy(data)
                original = notification_fingerprint(data)
                changed = copy.deepcopy(data)
                changed['generated_at'] = '2026-11-15T05:00:00+00:00'
                changed['comparisons'][0].update(name='공식 이름', name_ko='공식 이름',
                                                 source_snapshot_id=900, reference_snapshot_id=901)
                changed['comparisons'][0]['evidence']['paired_observations'] = 999
                changed['comparisons'][0]['history'] = [{'new': 'evidence'}]
                changed['houses'][0]['source_epoch'] = 1800000000
                changed['names_status'] = dict(status='ok', old=True)
                changed['comparisons'][30]['source_quote']['min_unit_price'] += 1
                self.assertEqual(original, notification_fingerprint(changed))
                changed['comparisons'][0]['source_quote']['min_unit_price'] += 1
                self.assertNotEqual(original, notification_fingerprint(changed))
                self.assertEqual(data, before)
                self.assertRegex(original, r'^[0-9a-f]{64}$')


    def test_summary_labels_origin_without_treating_unknown_as_live(self):
        from api_report_summary import render_summary
        cases = [('fixture', '[합성 테스트 데이터]'), ('live', '[실제 수집 데이터]'),
                 ('unknown', '[출처 미확인 데이터]'), (None, '[출처 미확인 데이터]'),
                 ('unrecognized', '[출처 미확인 데이터]')]
        summaries = {}
        for mode, label in cases:
            with self.subTest(mode=mode):
                data = report_fixture()
                data['mode'] = mode
                text = render_summary(data)
                self.assertTrue(text.startswith(label), text)
                if mode != 'live':
                    self.assertNotIn('[실제 수집 데이터]', text)
                summaries[mode] = text
        missing = report_fixture()
        del missing['mode']
        self.assertTrue(render_summary(missing).startswith('[출처 미확인 데이터]'))
        self.assertNotEqual(summaries['fixture'], summaries['live'])


    def test_plain_summary_is_bounded_and_labels_observation_not_trade(self):
        import api_report_summary as module
        self.assertTrue(hasattr(module, 'render_summary'), 'plain summary renderer is missing')
        data = report_fixture()
        data['comparisons'][0]['name_ko'] = '[공식 이름](https://example.invalid)'
        original = copy.deepcopy(data)
        text = module.render_summary(data)
        self.assertIn('거래 권고 아님', text)
        self.assertIn('반복 관측', text)
        self.assertIn('최저가 수량', text)
        self.assertIn('#113', text)
        self.assertIn('#114', text)
        self.assertIn('item#8', text)
        self.assertNotIn('item#9', text)
        self.assertIn('[공식 이름](https://example.invalid)', text)
        self.assertNotIn('매수', text)
        self.assertNotIn('매도', text)
        self.assertNotIn('이익', text)
        self.assertLessEqual(len(text), 3500)
        self.assertEqual(data, original)


    def test_summary_bound_also_applies_to_large_market_headers(self):
        from api_report_summary import render_summary
        data = report_fixture()
        data['houses'] = data['houses'] * 100
        self.assertLessEqual(len(render_summary(data)), 3500)


if __name__ == '__main__':
    unittest.main()
