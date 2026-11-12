"""Synthetic profile/opportunity-cost tests; not evidence of market profits."""
from copy import deepcopy
import unittest

from common import AppError
from profession_profile import character_profile, access, allowed
from action_planner import plan_actions, process_comparison
from tests.test_actions import item, NOW


CONFIG = {'character_professions': {'primary': {'tailoring': None, 'enchanting': None},
          'secondary': {'cooking': None, 'first-aid': None, 'fishing': None}}}


class ProfessionTests(unittest.TestCase):
    def test_two_primary_and_three_secondary_unknown_is_not_zero(self):
        profile = character_profile(CONFIG)
        self.assertEqual(len(profile['secondary']), 3)
        craft = dict(profession='tailoring', skill=300, known=False, required_level=60)
        self.assertEqual(access(craft, profile), 'requirements_check')
        self.assertTrue(allowed(craft, profile))
        self.assertFalse(allowed(dict(craft, profession='engineering'), profile))
        cfg = deepcopy(CONFIG); cfg['character_professions']['primary']['alchemy'] = None
        with self.assertRaises(AppError):
            character_profile(cfg)

    def test_known_low_skill_and_level_exclude_but_blank_does_not(self):
        cfg = deepcopy(CONFIG); cfg['character_professions']['primary']['tailoring'] = 100
        c = dict(profession='tailoring', skill=101, known=True, required_level=10)
        self.assertEqual(access(c, character_profile(cfg)), 'skill_too_low')
        c['skill'] = 100
        self.assertEqual(access(c, character_profile(cfg)), 'listed')
        cfg['character_level'] = 9
        self.assertEqual(access(c, character_profile(cfg)), 'level_too_low')

    def comparison_fixture(self):
        items = {i: dict(item(price), id=i, history=[]) for i, price in [(1, 1001), (2, 5001), (3, 2000)]}
        craft = dict(spell=1, item=2, qty=1, cost=4002, complete=True, materials=[
            {'id': 1, 'qty': 2, 'purchase': {'cost': 2002, 'method': 'auction'}},
            {'id': 3, 'qty': 1, 'purchase': {'cost': 2000, 'method': 'auction'}}])
        return craft, items

    def test_owned_materials_have_sale_opportunity_cost(self):
        craft, items = self.comparison_fixture()
        r = process_comparison(craft, 1, items, NOW)
        self.assertEqual((r['raw_net'], r['other_cost'], r['processed_net'], r['gain']),
                         (1900, 2000, 2750, 850))
        self.assertEqual(r['choice'], 'process')
        items[2]['price'] = 3001
        self.assertEqual(process_comparison(craft, 1, items, NOW)['choice'], 'sell_raw')

    def test_missing_or_stale_extra_ingredient_not_free(self):
        craft, items = self.comparison_fixture()
        items[1]['price'] = None
        self.assertIsNone(process_comparison(craft, 1, items, NOW))
        craft, items = self.comparison_fixture()
        items[3]['observed'] = '2026-01-01T00:00:00+00:00'
        self.assertIsNone(process_comparison(craft, 1, items, NOW))

    def test_secondary_candidates_survive_general_top20_and_catalog(self):
        data = dict(items=[dict(item(100000), history=[])], market={'faction': 'horde'},
                    build='new', vendors=[], crafts=[])
        for spell in range(23):
            profession = 'tailoring' if spell < 21 else 'cooking' if spell == 21 else 'engineering'
            data['crafts'].append(dict(spell=spell, item=1, qty=1, cost=1000+spell,
                complete=True, profession=profession, skill=200, known=False,
                verification='client_catalog', build='new', materials=[
                    {'id': 1, 'qty': 1, 'purchase': {'cost': 1000+spell, 'method': 'auction'}}]))
        result = plan_actions(data, CONFIG, NOW)
        self.assertEqual(len(result['crafts']), 20)
        self.assertEqual(len(result['by_profession']['cooking']), 1)
        self.assertNotIn('engineering', result['by_profession'])
        catalog = plan_actions(data, {}, NOW)
        self.assertEqual(len(catalog['craft_pool']), 23)
        self.assertIn('engineering', catalog['by_profession'])

    def test_fishing_and_gathering_targets_follow_profile(self):
        data = dict(items=[dict(item(), id=6289), dict(item(), id=2)],
                    market={'faction': 'horde'}, build='new', vendors=[], crafts=[])
        self.assertEqual([r['item'] for r in plan_actions(data, CONFIG, NOW)['farms']], [6289])


if __name__ == '__main__':
    unittest.main()
