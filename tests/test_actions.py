"""Synthetic safety/arithmetic checks, not real market profitability evidence."""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from action_planner import plan_actions, sell_scenario, trial_math
from tests.test_static import Base
from static_store import enqueue
from sync_static import collect


NOW = datetime(2026,11,12,3,tzinfo=timezone.utc)


def item(price=10000):
    return dict(id=1,name='Synthetic Item',price=price,observed='2026-11-11T15:00:00+00:00',
                snapshot=2,snapshots=[1,2],binding=None,category='Trade Goods',subclass='Herb',craft_uses=3,
                history=[dict(price=8000,observed='2026-11-10T15:00:00+00:00'),
                         dict(price=price,observed='2026-11-11T15:00:00+00:00')])


class ActionTests(unittest.TestCase):
    def data(self):
        return dict(items=[item()],market={'faction':'horde'},build='new',crafts=[],vendors=[dict(
            id=1,item=1,side='Horde',verification='source_reported_live',build='new',costs=[],
            price=1000,qty=1,basis='per_purchase',eligibility='unknown')])

    def test_lowest_observed_reference_fee_and_stress_buy_ceiling(self):
        s=sell_scenario(item(),NOW)
        self.assertEqual(s['price'],7999)
        self.assertEqual(s['stress_price'],6399)
        c=trial_math(1000,1,s)
        self.assertEqual(c['net'],7599)
        self.assertEqual(c['stress_difference'],5079)
        self.assertEqual(c['buy_ceiling'],5065)
        self.assertEqual(c['break_even'],1053)

    def test_lowercase_and_unseparated_faction_do_not_guess(self):
        d=self.data(); d['vendors'][0]['side']='horde'
        self.assertEqual(len(plan_actions(d,{},NOW)['vendors']),1)
        d['vendors'][0]['side']='both'
        self.assertEqual(len(plan_actions(d,{},NOW)['vendors']),1)
        d['market']['faction']='Both'
        self.assertEqual(plan_actions(d,{},NOW)['vendors'],[])

    def test_unknown_price_old_observation_and_wrong_faction_excluded(self):
        for mutation in ({'price':None},{'observed':'2026-10-01T00:00:00+00:00'}):
            d=self.data();d['items'][0].update(mutation)
            self.assertEqual(plan_actions(d,{},NOW)['vendors'],[])
        d=self.data();d['vendors'][0]['side']='Alliance'
        self.assertEqual(plan_actions(d,{},NOW)['vendors'],[])

    def test_unverified_currency_binding_old_build_not_trials(self):
        for mutation in ({'costs':None},{'costs':[{'name':'Favor','count':30}],'price':0},
                         {'build':'old'},{'verification':'classic_reference'},
                         {'eligibility':'ineligible'}):
            with self.subTest(mutation=mutation):
                d=self.data();d['vendors'][0].update(mutation)
                self.assertEqual(plan_actions(d,{},NOW)['vendors'],[])
        d=self.data();d['items'][0]['binding']='1'
        self.assertEqual(plan_actions(d,{},NOW)['vendors'],[])

    def test_trial_not_sale_or_farming_rate_and_duplicate_npc_not_extra_item(self):
        d=self.data();d['vendors'].append(dict(d['vendors'][0],id=2))
        result=plan_actions(d,{},NOW)
        self.assertEqual(len(result['vendors']),1)
        self.assertIn('현재 NPC 구매 가능·재고',result['vendors'][0]['checks'])
        self.assertIsNone(result['farms'][0]['gold_per_hour'])
        self.assertIsNone(result['farms'][0]['yield_per_hour'])
        self.assertEqual(result['farms'][0]['units_for_10g'],17)
        d['items'][0].update(category='Armor',subclass='Cloth')
        self.assertEqual(plan_actions(d,{},NOW)['farms'],[])


class TargetedCollectionTests(Base):
    def test_selected_queue_never_fetches_other_pending_jobs(self):
        for iid in (1,2):
            enqueue(self.db,f'https://foreverdb.net/api/v1/items/{iid}','foreverdb_api','api_item',iid)
        self.db.commit()
        with patch('sync_static.fetch',return_value=(404,b'',None)) as fetch:
            attempted,ok=collect(self.db,self.cfg,2,self.cache,urls=['https://foreverdb.net/api/v1/items/2'])
        self.assertEqual((attempted,ok),(1,0))
        self.assertEqual(fetch.call_args[0][0]['entity_id'],2)
        self.assertEqual(self.db.execute('SELECT state FROM static_queue WHERE entity_id=1').fetchone()[0],'pending')


if __name__=='__main__':
    unittest.main()
