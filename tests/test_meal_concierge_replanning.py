from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core import StateStore, HouseholdError
import menu_planning as mp
from service import Application
import test_meal_concierge_planner as fixtures
CONFIG = fixtures.CONFIG


class ReplanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WeeklyPlannerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app
        self.store = self.fixture.store
        self.clock = mock.patch.object(Application, '_household_today', return_value=date(2026, 9, 7))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.candidates = self.fixture.save_candidates(5)
        plan = self.fixture.plan(self.fixture.request(self.candidates[:3], dates=['2026-09-07', '2026-09-08', '2026-09-09']))
        self.menu = self.app.handle({'operation':'menu', 'action':'save', 'planner_handoff':plan['save_handoff']})['menu']

    def prepare(self, menu=None, **changes):
        menu = menu or self.menu
        return self.app.handle({'operation':'menu', 'action':'replan_prepare', 'menu_ref':mp.menu_ref(menu),
            'remaining_dates':['2026-09-08', '2026-09-09'],
            'planner_input':self.fixture.request(self.candidates[3:]), **changes})['replan']

    def cook(self, slot, menu=None, **changes):
        menu = menu or self.menu
        return self.app.handle({'operation':'recipes', 'action':'mark_cooked', 'menu_id':menu['menu_id'],
            'expected_revision':menu['revision'], 'slot_id':slot['slot_id'], **changes})

    def apply(self, replan):
        return self.app.handle({'operation':'menu', 'action':'replan_apply', 'replan':replan})

    def test_exact_locks_cooked_history_successor_shopping_and_idempotency(self):
        first, locked, replaced = self.menu['slots']
        self.cook(first)
        lock_request = {'operation':'menu', 'action':'lock', 'menu_ref':mp.menu_ref(self.menu), 'slot_id':locked['slot_id'], 'locked':True}
        self.assertEqual(self.app.handle(lock_request), self.app.handle(lock_request))
        before = self.store.read()
        prepared = self.prepare(planner_input={'candidates':self.candidates[3:]})
        self.assertEqual(prepared, self.prepare(planner_input={'candidates':self.candidates[3:]}))
        self.assertEqual(before, self.store.read())
        result = self.apply(prepared)
        successor = result['menu']
        self.assertEqual(successor['slots'][:2], self.menu['slots'][:2])
        self.assertNotEqual(successor['slots'][2]['slot_id'], replaced['slot_id'])
        self.assertEqual(successor['supersedes'], mp.menu_ref(self.menu))
        after = self.store.read()
        self.assertEqual(after['recipe_usage'][self.menu['menu_id']], before['recipe_usage'][self.menu['menu_id']])
        self.assertEqual(after['menu_planning']['history'][mp.lock_key(self.menu)], self.menu)
        shopping = mp.shopping_menu(successor)
        self.assertEqual(len(shopping['dishes']), 2)
        self.assertTrue(self.apply(prepared)['idempotent'])
        self.assertEqual(after, self.store.read())
        # Carried future cooking is routed to its original usage owner exactly once.
        self.cook(locked, successor)
        state = self.store.read()
        self.assertEqual(state['recipe_usage'][self.menu['menu_id']], before['recipe_usage'][self.menu['menu_id']])
        self.assertEqual(state['menu_planning']['outcomes'][locked['slot_id']]['outcome'], 'cooked')
        self.assertNotIn(locked['recipe_key'], state['recipe_usage'][successor['menu_id']]['recipe_keys'])
        self.assertEqual(self.fixture.provider.calls, [])

    def test_stale_date_revision_locks_and_payload_do_not_write(self):
        prepared = self.prepare(planner_input={'candidates':self.candidates[3:]})
        before = self.store.path.read_bytes()
        with mock.patch.object(Application, '_household_today', return_value=date(2026, 9, 8)):
            with self.assertRaises(HouseholdError): self.apply(prepared)
        self.assertEqual(before, self.store.path.read_bytes())
        bad = deepcopy(prepared)
        bad['successor']['slots'][0]['date'] = '2026-09-10'
        bad['replan_digest'] = mp.digest({k:v for k,v in bad.items() if k != 'replan_digest'})
        with self.assertRaises(HouseholdError): self.apply(bad)
        self.assertEqual(before, self.store.path.read_bytes())
        self.app.handle({'operation':'menu', 'action':'lock', 'menu_ref':mp.menu_ref(self.menu), 'slot_id':self.menu['slots'][1]['slot_id'], 'locked':True})
        with self.assertRaises(HouseholdError): self.apply(prepared)

    def test_today_inclusion_exclusion_and_impossible_replan(self):
        prepared = self.prepare(planner_input={'candidates':self.candidates[3:]})
        self.assertEqual(prepared['successor']['slots'][0], self.menu['slots'][0])
        changed = self.prepare(remaining_dates=['2026-09-07'], planner_input={'candidates':self.candidates[3:]})
        self.assertNotEqual(changed['successor']['slots'][0]['slot_id'], self.menu['slots'][0]['slot_id'])
        self.assertEqual(changed['successor']['slots'][1:], self.menu['slots'][1:])
        impossible = self.prepare(planner_input={'candidates':self.candidates[3:4]})
        self.assertEqual(impossible['status'], 'needs_input')
        self.assertNotIn('successor', impossible)
        with self.assertRaises(HouseholdError): self.prepare(remaining_dates=['Monday'])
        with self.assertRaises(HouseholdError): self.cook(self.menu['slots'][0], expected_revision=99)

    def test_pending_protected_operations_block_and_ordered_snapshots_preserved(self):
        with self.store.locked() as state:
            state['menu']['phase'] = 'ordered'
            state['recipe_usage'][self.menu['menu_id']]['status'] = 'ordered'
            state['order_snapshots']['fixture'] = deepcopy(state['menu'])
        self.menu = self.store.read()['menu']
        before_snapshot = deepcopy(self.store.read()['order_snapshots'])
        with self.assertRaises(HouseholdError):
            self.app.handle({'operation':'menu','action':'lock','menu_ref':mp.menu_ref(self.menu),'slot_id':self.menu['slots'][0]['slot_id'],'locked':True})
        prepared = self.prepare(planner_input={'candidates':self.candidates[3:]}, locked_slot_ids=[self.menu['slots'][0]['slot_id']])
        for field in ('pending_checkout','pending_cancellation','order_change'):
            with self.store.locked() as state: state[field] = {'status':'uncertain'}
            before = self.store.path.read_bytes()
            with self.assertRaises(HouseholdError): self.apply(prepared)
            self.assertEqual(before, self.store.path.read_bytes())
            with self.store.locked() as state: state[field] = None
        self.apply(prepared)
        self.assertEqual(before_snapshot, self.store.read()['order_snapshots'])
        summary = self.app._usage_summary(self.store.read(), self.menu['slots'][1]['recipe_key'], self.menu['week'])
        self.assertEqual(summary['last_ordered_week'], self.menu['week'])
        self.assertFalse(summary['eligible'])
        self.assertEqual(self.fixture.provider.calls, [])

    def test_lineage_retires_removed_future_and_keeps_cooked_once(self):
        self.cook(self.menu['slots'][0])
        prepared = self.prepare(planner_input={'candidates':self.candidates[3:]})
        successor = self.apply(prepared)['menu']
        cooked = self.menu['slots'][0]['recipe_key']
        removed = self.menu['slots'][1]['recipe_key']
        self.assertEqual(len(self.app._usage_summary(self.store.read(), cooked, self.menu['week'])['blocked_by']), 1)
        self.assertTrue(self.app._usage_summary(self.store.read(), removed, self.menu['week'])['eligible'])
        next_plan = self.prepare(menu=successor, remaining_dates=['2026-09-09'], planner_input={'candidates':self.candidates[1:3]})
        second = self.apply(next_plan)['menu']
        self.assertEqual(second['slots'][:2], successor['slots'][:2])
        self.assertEqual(len(self.app._usage_summary(self.store.read(), cooked, self.menu['week'])['blocked_by']), 1)
        self.assertEqual(len(mp.shopping_menu(second)['dishes']), 2)
        with self.assertRaisesRegex(HouseholdError, 'lineage'):
            self.app.handle({'operation':'menu','action':'save','menu_id':second['menu_id'],'expected_revision':second['revision'],
                'menu':{'week':second['week'],'dishes':[{'recipe_ref':self.candidates[1]['recipe_ref']} ]}})

    def test_clear_and_new_save_retire_carried_planned_owners(self):
        prepared = self.prepare(remaining_dates=['2026-09-09'], planner_input={'candidates':self.candidates[3:]})
        successor = self.apply(prepared)['menu']
        carried = self.menu['slots'][1]['recipe_key']
        predecessor = deepcopy(self.store.read()['recipe_usage'][self.menu['menu_id']])
        self.app.handle({'operation':'menu','action':'clear','menu_id':successor['menu_id'],'expected_revision':successor['revision']})
        self.assertTrue(self.app._usage_summary(self.store.read(), carried, self.menu['week'])['eligible'])
        self.assertEqual(predecessor, self.store.read()['recipe_usage'][self.menu['menu_id']])
        # Recreate fixture state with the successor and exercise ordinary replacement.
        with self.store.locked() as state:
            state['menu'] = deepcopy(successor)
            state['menu_planning']['retired'] = {}
        self.app.handle({'operation':'menu','action':'save', 'menu':{'week':self.menu['week'],
            'dishes':[{'recipe_ref':self.candidates[4]['recipe_ref']}]}})
        self.assertTrue(self.app._usage_summary(self.store.read(), carried, self.menu['week'])['eligible'])
        self.assertEqual(predecessor, self.store.read()['recipe_usage'][self.menu['menu_id']])

    def test_structural_comparison_has_no_provider_search_limit(self):
        rows = [{'item':f'item{i}','quantity':1,'unit':'g','scalable':True} for i in range(24)]
        menu = {'dishes':[{'shopping_requirements':rows}], 'salads':[]}
        self.assertEqual(len(mp.shopping_comparison(menu, menu)['unchanged']), 24)

    def test_legacy_and_unresolved_shopping(self):
        with self.store.locked() as state: state['menu'].pop('slots')
        self.assertFalse(self.app.handle({'operation':'menu','action':'get'})['slot_replan_available'])
        with self.assertRaisesRegex(HouseholdError, 'legacy'): self.prepare()
        menu = {'dishes':[{'shopping_requirements':[{'item':'salt','unit':'pinch','quantity':1,'scalable':False}]}], 'salads':[]}
        comparison = mp.shopping_comparison(menu, menu)
        self.assertTrue(comparison['unresolved']['before'])


class ReplanMigrationTests(unittest.TestCase):
    def test_v8_and_v7_private_backups_atomic_idempotent_and_unknown_newer(self):
        for version in (7,8):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                store = StateStore(directory, CONFIG)
                legacy = store.read()
                legacy['version'] = version
                legacy.pop('menu_planning')
                legacy['menu'] = {'schedule':[{'day':'Monday','meal':'unknown'}]}
                store.path.write_text(json.dumps(legacy))
                migrated = StateStore(directory, CONFIG)
                self.assertEqual(migrated.read()['version'], 9)
                self.assertEqual(migrated.read()['menu'], legacy['menu'])
                backup = Path(directory)/f'state-v{version}.backup.json'
                self.assertEqual(json.loads(backup.read_text()), legacy)
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                before = backup.read_bytes()
                StateStore(directory, CONFIG)
                self.assertEqual(before, backup.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory, CONFIG)
            legacy = store.read(); legacy['version']=8; legacy.pop('menu_planning')
            store.path.write_text(json.dumps(legacy)); before = store.path.read_bytes()
            with mock.patch('core._atomic_json', side_effect=OSError('fixture backup failure')):
                with self.assertRaises(OSError): StateStore(directory, CONFIG)
            self.assertEqual(before, store.path.read_bytes())
            legacy['version']=99; store.path.write_text(json.dumps(legacy))
            with self.assertRaisesRegex(HouseholdError, 'newer'): StateStore(directory, CONFIG)
