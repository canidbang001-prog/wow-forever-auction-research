"""Round 2 checks use synthetic prices only. They are not market evidence."""
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
import json
import os
import shutil
import subprocess
import unittest

from action_planner import (
    action_report, alchemy_rows, listing_quote, market_settings, plan_actions, process_comparison,
)
from common import AppError, set_meta
from market_dashboard import build_market, render
from profession_profile import PRIMARY
from static_store import fact
from tests.test_actions import NOW, item
from tests.test_static import Base


ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get('NODE_BINARY') or shutil.which('node')


def vendor(**overrides):
    row = dict(id=1, item=1, side='Horde', verification='source_reported_live', build='new',
               costs=[], price=1000, qty=1, basis='per_purchase', eligibility='unknown',
               npc='Synthetic NPC', zone='Synthetic Zone', x=0, y=0,
               url='https://foreverdb.net/npc/1', date='2026-11-11T00:00:00+00:00')
    row.update(overrides)
    return row


def bundle(items, vendors=None, crafts=None, faction='horde'):
    return dict(items=items, market={'faction': faction}, build='new',
                vendors=[] if vendors is None else vendors, crafts=crafts or [])


def listing(**kwargs):
    base = {'deposit_rate_pct': None, 'deposit_copper_per_item': None, 'sell_through_pct': None}
    base.update(kwargs)
    return {'listing_cost': base}


def flat(iid, price, subclass, name):
    history = [dict(price=price, observed=f'2026-11-{day}T15:00:00+00:00') for day in ('09', '10', '11')]
    return dict(id=iid, name=name, price=price, observed='2026-11-11T15:00:00+00:00',
                snapshot=3, snapshots=[1, 2, 3], binding=None, category='Trade Goods',
                subclass=subclass, craft_uses=1, history=history)


def report_of(data, actions):
    payload = dict(data, generated='2026-11-12T03:00:00+00:00', actions=actions)
    return action_report(payload)


class ListingCostTests(unittest.TestCase):
    def test_unknown_sell_through_does_not_subtract_a_guessed_loss(self):
        cfg = listing(deposit_copper_per_item=100)
        data = bundle([item()], vendors=[vendor()])
        result = plan_actions(data, cfg, NOW)
        row = result['vendors'][0]
        self.assertEqual(row['deposit'], 100)
        self.assertEqual(row['deposit_status'], 'known')
        self.assertEqual(row['ah_cut'], 320)
        self.assertEqual(row['cost'], 1000)
        self.assertIsNone(row['expected_deposit_loss'])
        self.assertEqual(row['expected_deposit_loss_status'], '데이터 부족')
        self.assertIsNone(row['official_net'])
        self.assertEqual(row['official_net_status'], '데이터 부족')
        self.assertEqual(row['net_ex_deposit'], 5079)
        self.assertEqual(row['net_ex_deposit'], row['stress_difference'])
        self.assertEqual(row['unstressed_net_ex_deposit'], 6599)
        self.assertIn('스트레스 시험이 아닙니다', row['unstressed_basis'])
        self.assertEqual(result['buy_recommendations'], [])
        self.assertFalse(result['sell_through_known'])
        text = report_of(data, result)
        self.assertIn('가정 순이익: 데이터 부족', text)
        self.assertIn('기대 보증금 손실', text)
        self.assertIn('0으로 빼지 않습니다', text)
        self.assertIn('데이터 부족', text.split('가정 순이익')[1])

    def test_known_deposit_and_sell_through_compute_expected_loss(self):
        data = bundle([item()], vendors=[vendor()])
        result = plan_actions(data, listing(deposit_copper_per_item=100, sell_through_pct=40), NOW)
        row = result['vendors'][0]
        self.assertEqual((row['deposit'], row['expected_deposit_loss'], row['official_net']), (100, 150, 4929))
        self.assertEqual(row['net_ex_deposit'], 5079)
        self.assertEqual(result['buy_recommendations'], [row])
        self.assertTrue(result['sell_through_known'])
        both = plan_actions(data, listing(deposit_copper_per_item=100, deposit_rate_pct=10, sell_through_pct=40), NOW)
        added = both['vendors'][0]
        self.assertEqual(added['deposit'], 739)
        self.assertEqual(added['expected_deposit_loss'], 1109)
        self.assertEqual(added['official_net'], 3970)
        full = plan_actions(data, listing(deposit_copper_per_item=100, sell_through_pct=100), NOW)['vendors'][0]
        self.assertEqual(full['expected_deposit_loss'], 0)
        self.assertEqual(full['official_net'], 5079)
        zero = listing_quote(6399, 1, 1000, market_settings(listing(deposit_copper_per_item=100, sell_through_pct=0)))
        self.assertIsNone(zero['expected_deposit_loss'])
        self.assertIsNone(zero['official_net'])
        self.assertEqual(zero['net_ex_deposit'], 5079)
        bare = listing_quote(6399, 1, 1000, market_settings(listing(sell_through_pct=40)))
        self.assertIsNone(bare['deposit'])
        self.assertEqual(bare['deposit_status'], '데이터 부족')
        self.assertIsNone(bare['official_net'])

    def test_non_positive_official_net_is_not_recommended(self):
        data = bundle([item()], vendors=[vendor()])
        result = plan_actions(data, listing(deposit_copper_per_item=100000, sell_through_pct=40), NOW)
        self.assertEqual(result['vendors'], [])
        self.assertEqual(result['buy_recommendations'], [])
        self.assertEqual(result['watch'][0]['status'], '순이익 없음')
        self.assertEqual(result['watch'][0]['distinct_days'], 3)


class StressComparisonTests(unittest.TestCase):
    def test_recommendation_uses_haircut_and_labels_the_unstressed_gain(self):
        items = {i: dict(item(price), id=i, history=[]) for i, price in [(1, 1001), (2, 5001), (3, 2000)]}
        craft = dict(spell=1, item=2, qty=1, cost=4002, complete=True, materials=[
            {'id': 1, 'qty': 2, 'purchase': {'cost': 2002, 'method': 'auction'}},
            {'id': 3, 'qty': 1, 'purchase': {'cost': 2000, 'method': 'auction'}}])
        row = process_comparison(craft, 1, items, NOW)
        self.assertEqual((row['raw_net'], row['other_cost'], row['processed_net'], row['gain'], row['choice']),
                         (1900, 2000, 2750, 850, 'process'))
        self.assertEqual((row['stress_raw_net'], row['stress_processed_net'], row['stress_gain'], row['stress_choice']),
                         (1520, 1800, 280, 'process'))
        self.assertEqual(row['recommendation'], '관찰 필요')
        self.assertEqual(row['raw_distinct_days'], 1)
        self.assertIn('스트레스 시험이 아닙니다', row['unstressed_basis'])
        items[2]['price'] = 3001
        sold = process_comparison(craft, 1, items, NOW)
        self.assertEqual(sold['choice'], 'sell_raw')
        self.assertEqual((sold['stress_raw_net'], sold['stress_processed_net'], sold['stress_choice']), (1520, 280, 'sell_raw'))
        flat_cut = process_comparison(craft, 1, items, NOW, haircut=0)
        self.assertEqual(flat_cut['stress_raw_net'], flat_cut['raw_net'])
        self.assertNotEqual(row['stress_raw_net'], row['raw_net'])


class LiquidityTests(unittest.TestCase):
    def test_few_days_stale_price_and_missing_price(self):
        one = item()
        one['history'] = [dict(price=10000, observed='2026-11-11T15:00:00+00:00')]
        few = plan_actions(bundle([one], vendors=[vendor()]), {}, NOW)
        self.assertEqual(few['vendors'], [])
        self.assertEqual(few['buy_recommendations'], [])
        self.assertEqual(few['watch'][0]['status'], '관찰 필요')
        self.assertEqual(few['watch'][0]['distinct_days'], 1)
        self.assertIsNotNone(few['watch'][0]['price_age_seconds'])
        old = '2026-10-01T00:00:00+00:00'
        stale_item = item()
        stale_item['observed'] = old
        stale_item['history'] = [dict(price=10000, observed=day) for day in (
            '2026-09-29T00:00:00+00:00', '2026-09-30T00:00:00+00:00', old)]
        stale = plan_actions(bundle([stale_item], vendors=[vendor()]), {}, NOW)
        self.assertEqual(stale['vendors'], [])
        self.assertEqual(stale['buy_recommendations'], [])
        self.assertEqual(stale['watch'][0]['status'], '관찰 필요')
        self.assertGreater(stale['watch'][0]['price_age_seconds'], 3 * 86400)
        self.assertGreaterEqual(stale['watch'][0]['distinct_days'], 3)
        missing = item()
        missing['price'] = None
        gone = plan_actions(bundle([missing], vendors=[vendor()]), {}, NOW)
        self.assertEqual(gone['vendors'], [])
        self.assertEqual(gone['watch'], [])
        self.assertEqual(gone['buy_recommendations'], [])

    def test_age_equal_to_the_limit_still_passes(self):
        end = NOW - timedelta(days=3)
        points = [dict(price=10000, observed=(end - timedelta(days=offset)).isoformat()) for offset in (2, 1, 0)]
        fresh = item()
        fresh['observed'] = end.isoformat()
        fresh['history'] = points
        ok = plan_actions(bundle([fresh], vendors=[vendor()]), {}, NOW)
        self.assertEqual(len(ok['vendors']), 1)
        self.assertEqual(ok['vendors'][0]['liquidity']['status'], 'ok')
        self.assertEqual(ok['vendors'][0]['sale']['distinct_days'], 3)
        late = item()
        late['observed'] = (NOW - timedelta(days=3, seconds=1)).isoformat()
        late['history'] = points
        blocked = plan_actions(bundle([late], vendors=[vendor()]), {}, NOW)
        self.assertEqual(blocked['vendors'], [])
        self.assertEqual(blocked['watch'][0]['status'], '관찰 필요')

    def test_thin_auction_ingredient_is_watch_and_missing_purchase_is_dropped(self):
        output = dict(item(10000), id=2)
        material = dict(item(1000), id=1, history=[dict(price=1000, observed='2026-11-11T15:00:00+00:00')])
        craft = dict(spell=3, item=2, qty=1, cost=1000, complete=True, profession='tailoring',
                     verification='client_catalog', build='new', name='Synthetic Bolt',
                     materials=[{'id': 1, 'qty': 1, 'purchase': {'cost': 1000, 'method': 'auction'}}],
                     url='https://foreverdb.net/spell/3')
        thin = plan_actions(bundle([material, output], crafts=[craft]), {}, NOW)
        self.assertEqual(thin['crafts'], [])
        self.assertEqual(thin['buy_recommendations'], [])
        self.assertEqual(thin['watch'][0]['status'], '관찰 필요')
        self.assertEqual(thin['watch'][0]['kind'], 'craft')
        craft['materials'][0]['purchase'] = None
        material['history'] = item()['history']
        dropped = plan_actions(bundle([material, output], crafts=[craft]), {}, NOW)
        self.assertEqual(dropped['crafts'], [])
        self.assertEqual(dropped['watch'], [])


class AlchemyTests(unittest.TestCase):
    def craft(self, materials, cost=10500):
        return dict(spell=77, name='Synthetic Potion', item=30, qty=1, cost=cost, complete=True,
                    profession='alchemy', skill=None, known=False, verification='client_catalog', build='new',
                    materials=materials, url='https://foreverdb.net/spell/77')

    def test_complete_herb_prices_compare_with_stress(self):
        herb = flat(10, 10000, 'Herb', 'Synthetic Herb')
        vial = flat(20, 500, 'Other', 'Synthetic Vial')
        potion = flat(30, 30000, 'Consumable', 'Synthetic Potion')
        craft = self.craft([
            {'id': 10, 'qty': 1, 'purchase': {'cost': 10000, 'method': 'auction'}},
            {'id': 20, 'qty': 1, 'purchase': {'cost': 500, 'method': 'auction'}}])
        result = plan_actions(bundle([herb, vial, potion], crafts=[craft]), {}, NOW)
        row = result['comparisons']['alchemy'][0]
        self.assertEqual(row['status'], 'ok')
        self.assertEqual((row['raw_net'], row['processed_net'], row['gain']), (9499, 27999, 18500))
        self.assertEqual((row['stress_raw_net'], row['stress_processed_net'], row['stress_gain']), (7599, 22299, 14700))
        self.assertEqual(row['recommendation'], 'process')
        self.assertEqual((row['raw_distinct_days'], row['output_distinct_days']), (3, 3))
        self.assertIsNotNone(row['raw_price_age_seconds'])
        self.assertIsNotNone(row['output_price_age_seconds'])

    def test_missing_price_or_subclass_is_a_gap_and_non_herbs_are_skipped(self):
        herb = flat(10, 10000, 'Herb', 'Synthetic Herb')
        herb['price'] = None
        vial = flat(20, 500, 'Other', 'Synthetic Vial')
        potion = flat(30, 30000, 'Consumable', 'Synthetic Potion')
        craft = self.craft([
            {'id': 10, 'qty': 1, 'purchase': {'cost': 10000, 'method': 'auction'}},
            {'id': 20, 'qty': 1, 'purchase': {'cost': 500, 'method': 'auction'}}])
        data = bundle([herb, vial, potion], crafts=[craft])
        gap = plan_actions(data, {}, NOW)
        row = gap['comparisons']['alchemy'][0]
        self.assertEqual(row['status'], '데이터 부족')
        self.assertEqual(row['recommendation'], '데이터 부족')
        self.assertIsNone(row['gain'])
        self.assertIsNone(row['stress_gain'])
        self.assertIsNone(row['raw_sale'])
        text = report_of(data, gap)
        self.assertIn('데이터 부족', text)
        self.assertNotIn('| None |', text)
        blank = flat(10, 10000, None, 'Synthetic Reagent')
        unknown = self.craft([{'id': 10, 'qty': 1, 'purchase': {'cost': 10000, 'method': 'auction'}}], cost=10000)
        missing = alchemy_rows(unknown, {10: blank, 30: potion}, NOW, market_settings({}))
        self.assertEqual(missing[0]['status'], '데이터 부족')
        self.assertIsNone(missing[0]['gain'])
        ore = flat(10, 10000, 'Metal & Stone', 'Synthetic Ore')
        self.assertEqual(alchemy_rows(unknown, {10: ore, 30: potion}, NOW, market_settings({})), [])
        self.assertEqual(plan_actions(bundle([ore, potion], crafts=[unknown]), {}, NOW)['comparisons']['alchemy'], [])
        self.assertEqual(plan_actions(bundle([potion], crafts=[]), {}, NOW)['comparisons']['alchemy'], [])

    def test_cloth_stress_profile_gate_and_enchant_service(self):
        cloth = flat(2589, 1001, 'Cloth', 'Synthetic Linen')
        bolt = flat(2996, 5001, 'Cloth', 'Synthetic Bolt')
        tailoring = dict(spell=8, name='Synthetic Bolt', item=2996, qty=1, cost=2002, complete=True,
                         profession='tailoring', verification='client_catalog', build='new',
                         materials=[{'id': 2589, 'qty': 2, 'purchase': {'cost': 2002, 'method': 'auction'}}],
                         url='https://foreverdb.net/spell/8')
        result = plan_actions(bundle([cloth, bolt], crafts=[tailoring]), {}, NOW)
        row = result['comparisons']['cloth'][0]
        self.assertEqual(row['stress_gain'], 2280)
        self.assertEqual(row['recommendation'], 'process')
        self.assertIn('스트레스 시험이 아닙니다', row['unstressed_basis'])
        herb = flat(10, 10000, 'Herb', 'Synthetic Herb')
        potion = flat(30, 30000, 'Consumable', 'Synthetic Potion')
        alchemy = self.craft([{'id': 10, 'qty': 1, 'purchase': {'cost': 10000, 'method': 'auction'}}], cost=10000)
        configured = {'character_professions': {'primary': {'tailoring': None, 'enchanting': None}, 'secondary': {}}}
        gated = plan_actions(bundle([herb, potion, cloth, bolt], crafts=[tailoring, alchemy]), configured, NOW)
        self.assertEqual(gated['comparisons']['alchemy'], [])
        self.assertEqual(len(gated['comparisons']['cloth']), 1)
        herbalism = {'character_professions': {'primary': {'herbalism': None}, 'secondary': {}}}
        opened = plan_actions(bundle([herb, potion], crafts=[alchemy]), herbalism, NOW)
        self.assertEqual(opened['comparisons']['alchemy'][0]['recommendation'], 'process')
        self.assertEqual(opened['crafts'], [])
        self.assertNotIn(77, [a['spell'] for a in opened['craft_pool']])
        service = dict(spell=4, item=None, qty=None, cost=None, complete=False, service=True,
                       profession='enchanting', verification='client_catalog', build='new',
                       materials=[], name='Synthetic Enchant', url='https://foreverdb.net/spell/4')
        listed = plan_actions(bundle([item()], crafts=[service]), {}, NOW)
        self.assertIn(4, listed['services'])
        self.assertTrue({'tailoring', 'enchanting', 'alchemy', 'herbalism'} <= set(PRIMARY))
        ui = (ROOT / 'profession_ui.js').read_text(encoding='utf-8')
        self.assertIn("tailoring','enchanting','alchemy'", ui)
        self.assertIn('herbalism', ui)
        for name in ('재봉술', '마법부여', '연금술', '약초채집'):
            self.assertIn(name, (ROOT / 'market_template.html').read_text(encoding='utf-8'))


class ConcentrationTests(unittest.TestCase):
    def test_position_share_stays_separate_from_the_trial_cap(self):
        data = bundle([item()], vendors=[vendor()])
        unset = plan_actions(data, {}, NOW)
        row = unset['vendors'][0]
        self.assertEqual(row['suggested_qty'], 1)
        self.assertEqual(row['capital_required'], 1000)
        self.assertIsNone(row['position_cap_copper'])
        self.assertEqual(row['cap_status'], '데이터 부족')
        self.assertIn('시험 상한', row['cap_note'])
        self.assertEqual(unset['trial_budget_copper'], 50000)
        self.assertIsNone(unset['total_budget_copper'])
        self.assertEqual(unset['buy_recommendations'], [])
        known = dict(listing(deposit_copper_per_item=100, sell_through_pct=40),
                     total_budget_copper=10000, max_position_share_pct=20)
        capped = plan_actions(data, known, NOW)
        buy = capped['buy_recommendations'][0]
        self.assertEqual(buy['position_cap_copper'], 2000)
        self.assertEqual(buy['suggested_units'], 1)
        self.assertEqual(buy['suggested_qty'], 1)
        self.assertEqual(buy['capital_required'], 1000)
        self.assertEqual(buy['official_net'], 4929)
        self.assertEqual(capped['trial_budget_copper'], 50000)
        self.assertIn('1묶음/제작 1회', buy['cap_note'])
        tight = dict(known, total_budget_copper=1000)
        blocked = plan_actions(data, tight, NOW)
        self.assertEqual(blocked['vendors'], [])
        self.assertEqual(blocked['buy_recommendations'], [])
        self.assertEqual(blocked['watch'][0]['status'], '한도 초과')


class ExampleConfigTests(unittest.TestCase):
    def test_example_defaults_stay_unknown_without_personal_facts(self):
        raw = json.loads((ROOT / 'config.forever.example.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['market_faction'], 'unknown')
        self.assertEqual(raw['character_professions'], {'primary': {}, 'secondary': {}})
        settings = market_settings(raw)
        self.assertEqual(settings['haircut_pct'], 20)
        self.assertEqual(settings['min_distinct_days'], 3)
        self.assertEqual(settings['max_price_age_days'], 3)
        self.assertEqual(settings['trial_budget_copper'], 50000)
        self.assertIsNone(settings['total_budget_copper'])
        self.assertEqual(settings['max_position_share_pct'], 20)
        self.assertIsNone(settings['deposit_rate_pct'])
        self.assertIsNone(settings['deposit_copper_per_item'])
        self.assertIsNone(settings['sell_through_pct'])
        self.assertIsNone(market_settings({})['total_budget_copper'])
        self.assertEqual(market_settings({})['trial_budget_copper'], 50000)
        with self.assertRaises(AppError):
            market_settings({'min_distinct_days': True})
        with self.assertRaises(AppError):
            market_settings({'listing_cost': []})
        with self.assertRaises(AppError):
            market_settings({'listing_cost': {'sell_through_pct': 101}})


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        found = dict(attrs).get('id')
        if found:
            self.ids.append(found)


DOM_PRELUDE = r"""
const fs = require('fs');
const payloadText = fs.readFileSync(process.argv[2], 'utf8');
const store = new Map();
function make(id) {
  const node = {
    id: id || '', textContent: '', value: '', checked: false, hidden: false, disabled: false,
    open: false, scrollTop: 0, className: '', dataset: {}, style: {}, _html: '', children: [],
    classList: {add(){}, remove(){}, toggle(){}, contains(){ return false; }},
    setAttribute(name, value) {
      this['attr_' + name] = value;
      if (String(name).startsWith('data-')) this.dataset[name.slice(5)] = value;
    },
    getAttribute(name) { return this['attr_' + name]; },
    addEventListener() {}, removeEventListener() {},
    prepend(child) { this.children.unshift(child); },
    appendChild(child) { this.children.push(child); return child; },
    focus() {}, click() { if (this.onclick) this.onclick(); }, close() {},
    showModal() { this.open = true; }, closest() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; }
  };
  Object.defineProperty(node, 'innerHTML', {
    get() { return this._html; },
    set(value) {
      this._html = String(value);
      const re = /<([A-Za-z][\w:-]*)([^>]*)>/g;
      let match;
      while ((match = re.exec(this._html))) {
        const attrs = match[2];
        const idMatch = /\sid="([^"]*)"/.exec(' ' + attrs);
        const child = make(idMatch ? idMatch[1] : '');
        const tab = /\sdata-tab="([^"]*)"/.exec(' ' + attrs);
        if (tab) child.dataset.tab = tab[1];
        const valueMatch = /\svalue="([^"]*)"/.exec(' ' + attrs);
        if (valueMatch) child.value = valueMatch[1];
        if (/(?:^|\s)checked(?:\s|=|$)/.test(attrs)) child.checked = true;
        if (idMatch && idMatch[1]) store.set(idMatch[1], child);
      }
    }
  });
  return node;
}
function el(id) {
  if (!store.has(id)) store.set(id, make(id));
  return store.get(id);
}
const document = {
  getElementById: el,
  createElement() { return make(''); },
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll(selector) {
    if (selector === '[data-tab]') return [...store.values()].filter(node => node.dataset && node.dataset.tab);
    return [];
  },
  activeElement: null
};
const localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.document = document;
globalThis.localStorage = localStorage;
el('market-data').textContent = payloadText;
"""

DOM_CHECK = r"""
const panel = document.getElementById('profession-panel').innerHTML;
for (const name of ['재봉술', '마법부여', '연금술', '약초채집']) {
  if (!panel.includes(name)) { console.error('panel missing ' + name); process.exit(1); }
}
if (!panel.includes('채집')) { console.error('herbalism gathering label missing'); process.exit(1); }
for (const id of ['prof-tailoring', 'prof-enchanting', 'prof-alchemy', 'prof-herbalism']) {
  const box = document.getElementById(id);
  if (!box) { console.error('missing ' + id); process.exit(1); }
  box.checked = true;
}
document.getElementById('prof-alchemy').onchange();
const results = document.getElementById('results').innerHTML;
for (const text of ['약초: 원물 판매 vs 연금', '천: 원물 판매 vs 재봉', '마부: 재료값과 손님에게 받을 금액', '스트레스', 'Synthetic Potion']) {
  if (!results.includes(text)) { console.error('results missing ' + text); process.exit(1); }
}
document.getElementById('prof-alchemy').checked = false;
document.getElementById('prof-herbalism').onchange();
const herbsOnly = document.getElementById('results').innerHTML;
if (!herbsOnly.includes('약초: 원물 판매 vs 연금') || !herbsOnly.includes('Synthetic Herb')) {
  console.error('herbalism-only comparison missing');
  process.exit(1);
}
switchTab('crafts');
const crafts = document.getElementById('results').innerHTML;
if (!crafts.includes('스트레스 시험 아님') || !crafts.includes('20% 스트레스 차액')) {
  console.error('craft table did not separate last-min and stress');
  process.exit(1);
}
console.log('dom-ok');
"""


class MarketHtmlSmokeTests(Base):
    @unittest.skipUnless(NODE, 'Node executable required for JavaScript smoke test')
    def test_synthetic_db_renders_market_html(self):
        self.cfg['auctionator_realm_key'] = 'PvP'
        self.cfg['market_faction'] = 'unknown'
        self.cfg['character_professions'] = {'primary': {}, 'secondary': {}}
        set_meta(self.db, 'auctionator_local_realm_id', -1)
        set_meta(self.db, 'foreverdb_api_build', 'synthetic-build')
        self.db.execute("INSERT INTO realms VALUES (-1,'[\"PvP\"]')")
        rows = (
            (91001, 'Synthetic Herb', 'Trade Goods', 'Herb', 10000),
            (91002, 'Synthetic Vial', 'Trade Goods', 'Other', 500),
            (91003, 'Synthetic Potion', 'Consumable', 'Consumable', 30000),
        )
        for iid, name, klass, sub, _price in rows:
            self.db.execute('INSERT INTO items(item_id,name,class_name,subclass_name) VALUES (?,?,?,?)',
                            (iid, name, klass, sub))
        observed = ('2026-11-09T15:00:00+00:00', '2026-11-10T15:00:00+00:00', '2026-11-11T15:00:00+00:00')
        for index, seen in enumerate(observed, start=1):
            self.db.execute("INSERT INTO snapshots VALUES (?,'auctionator',-1,?,?,NULL)",
                            (index, f'2026-11-1{index}T12:00:00+00:00', f'synthetic-{index}'))
            for iid, _name, _klass, _sub, price in rows:
                self.db.execute('INSERT INTO prices VALUES (?,?,?,NULL,NULL,NULL,?)', (index, iid, price, seen))
        self.db.execute('''INSERT INTO static_documents(id,url,provider,game_build,fetched_at,
            sha256,cache_path,http_status,verification) VALUES (1,?,?,?,?,?,?,?,?)''',
            ('https://foreverdb.net/api/v1/professions/alchemy', 'foreverdb_api', 'synthetic-build',
             '2026-11-11T12:00:00+00:00', 'synthetic', 'test', 200, 'client_catalog'))
        fact(self.db, 1, 'craft_recipe', 'spell', 9001, label='Synthetic Potion',
             verification='client_catalog', profession='alchemy', payload={
                 'reagents': [{'itemId': 91001, 'count': 1}, {'itemId': 91002, 'count': 1}],
                 'makes': {'itemId': 91003, 'count': 1}})
        fact(self.db, 1, 'craft_output', 'spell', 9001, 'item', 91003, quantity=1, verification='client_catalog')
        fact(self.db, 1, 'craft_reagent', 'spell', 9001, 'item', 91001, quantity=1, verification='client_catalog')
        fact(self.db, 1, 'craft_reagent', 'spell', 9001, 'item', 91002, quantity=1, verification='client_catalog')
        fact(self.db, 1, 'craft_recipe', 'spell', 9002, label='Synthetic Enchant',
             verification='client_catalog', profession='enchanting', payload={'reagents': [], 'makes': None})
        self.db.commit()
        data = build_market(self.db, self.cfg, NOW)
        craft = next(c for c in data['crafts'] if c['spell'] == 9001)
        self.assertEqual(craft['margin_basis'], 'last_min_price')
        self.assertEqual(craft['margin'], 18000)
        self.assertEqual(craft['stress_margin'], 12299)
        self.assertEqual(data['actions']['comparisons']['alchemy'], [])
        compared = data['action_catalog']['comparisons']['alchemy'][0]
        self.assertEqual(compared['stress_gain'], 14700)
        self.assertEqual(compared['recommendation'], 'process')
        self.assertEqual(compared['raw_distinct_days'], 3)
        trial = next(a for a in data['action_catalog']['craft_pool'] if a['spell'] == 9001)
        self.assertIsNone(trial['official_net'])
        self.assertEqual(trial['net_ex_deposit'], 12299)
        self.assertEqual(trial['expected_deposit_loss_status'], '데이터 부족')
        self.assertEqual(trial['sale']['distinct_days'], 3)
        self.assertIsNotNone(trial['sale']['price_age_seconds'])
        self.assertEqual(trial['cap_status'], '데이터 부족')
        self.assertEqual(data['actions']['buy_recommendations'], [])
        self.assertEqual(data['action_catalog']['buy_recommendations'], [])
        self.assertIn(9002, data['action_catalog']['services'])
        text = action_report(data)
        self.assertIn('데이터 부족', text)
        self.assertNotIn('config.forever.json', text)
        html = render(data)
        parser = _Tags()
        parser.feed(html)
        self.assertIn('html', parser.tags)
        self.assertIn('market-data', parser.ids)
        payload = html.split('<script id="market-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        self.assertNotIn('<', payload)
        loaded = json.loads(payload)
        self.assertEqual(loaded['market']['faction'], 'unknown')
        self.assertEqual(next(c['name'] for c in loaded['crafts'] if c['spell'] == 9001), 'Synthetic Potion')
        script = html.split('<script id="market-data" type="application/json">', 1)[1].split('</script>', 1)[1]
        body = script.split('<script>', 1)[1].rsplit('</script>', 1)[0]
        page = self.root / 'page.js'
        page.write_text(body, encoding='utf-8')
        checked = subprocess.run([NODE, '--check', str(page)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        payload_path = self.root / 'market-data.json'
        payload_path.write_text(payload, encoding='utf-8')
        runner = self.root / 'dom.js'
        runner.write_text(DOM_PRELUDE + '\n' + body + '\n' + DOM_CHECK, encoding='utf-8')
        ran = subprocess.run([NODE, str(runner), str(payload_path)], capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr + ran.stdout)
        self.assertIn('dom-ok', ran.stdout)
        self.assertTrue(html.startswith('<!doctype html>'))
        self.assertNotIn('reports/', str(self.root))


if __name__ == '__main__':
    unittest.main()
