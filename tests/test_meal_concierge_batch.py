from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core import HouseholdError,StateStore
from service import Application
import menu_planning as mp
import batch_planning as bp
from product_planner import menu_requirements
import test_meal_concierge_planner as fixtures


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.WeeklyPlannerTests(); self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.app=self.fixture.app; self.store=self.fixture.store
        patch=mock.patch.object(Application,'_household_today',return_value=date(2026,9,7)); patch.start(); self.addCleanup(patch.stop)
        self.candidates=self.fixture.save_candidates(4)
        plan=self.fixture.plan(self.fixture.request(self.candidates[:3],dates=['2026-09-07','2026-09-08','2026-09-09']))
        self.menu=self.app.handle({'operation':'menu','action':'save','planner_handoff':plan['save_handoff']})['menu']
        source=self.menu['slots'][0]
        self.spec={'source_slot_id':source['slot_id'],'source_snapshot_digest':source['snapshot_digest'],
            'prepared_portions':'5','consumed_at_source':'2','suitability':{'source':'current_user','value':'suitable'},
            'storage':{'source':'current_user','method':'refrigerated','max_interval_days':2},
            'leftovers':[{'slot_id':s['slot_id'],'portions':'1.5'} for s in self.menu['slots'][1:]]}

    def prepare(self,spec=None):
        return self.app.handle({'operation':'menu','action':'batch_prepare','menu_ref':mp.menu_ref(self.menu),'batch_spec':spec or self.spec})['batch_plan']

    def apply(self,prepared,confirmation=None):
        return self.app.handle({'operation':'menu','action':'batch_apply','batch_plan':prepared,
            'batch_confirmation':confirmation if confirmation is not None else {'batch_digest':prepared['batch_digest'],'statement':bp.CONFIRMATION_STATEMENT}})

    def cook(self,menu,slot,**changes):
        return self.app.handle({'operation':'recipes','action':'mark_cooked','menu_id':menu['menu_id'],
            'expected_revision':menu['revision'],'slot_id':slot['slot_id'],**changes})

    def test_exact_portions_confirmation_shopping_and_no_duplicate_usage(self):
        before=self.store.read(); prepared=self.prepare()
        self.assertEqual(prepared,self.prepare()); self.assertEqual(before,self.store.read())
        self.assertEqual(self.apply(prepared,True)['status'],'needs_input'); self.assertEqual(before,self.store.read())
        menu=self.apply(prepared)['menu']; self.assertTrue(self.apply(prepared)['idempotent'])
        self.assertEqual(len(menu['dishes']),1); self.assertEqual(len(menu['slots']),3)
        self.assertEqual(menu['slots'][0],self.menu['slots'][0])
        requirements,unresolved=menu_requirements(mp.shopping_menu(menu))
        self.assertEqual(unresolved,[]); self.assertEqual(requirements[0]['quantity'],{'numerator':500,'denominator':1})
        self.assertEqual([r['new_requirements'] for r in prepared['shopping_reasons'][1:]],[0,0])
        self.assertEqual(self.store.read()['recipe_usage'][menu['menu_id']]['recipe_keys'],[])
        self.assertEqual(self.store.read()['profile']['meals']['batch_dishes'],0)
        self.assertEqual(self.store.read()['menu_planning']['history'][mp.lock_key(self.menu)],self.menu)
        self.assertEqual(self.fixture.provider.calls,[])

    def test_huge_decimal_exponents_fail_before_fraction_construction(self):
        for value in ('1e1000000000','1e-1000000000','0e1000000000'):
            with mock.patch('batch_planning.Fraction',side_effect=AssertionError('must not construct')):
                with self.assertRaises(HouseholdError): bp.fraction(value)
        self.assertEqual(bp.fraction('0.5000000000000000'),bp.fraction({'numerator':1,'denominator':2}))

    def test_unknown_conflicting_overconsumed_intervals_and_locks_need_input(self):
        bads=[]
        for field,value in [('suitability',True),('storage',{'method':'fridge'}),('prepared_portions','4'),('prepared_portions','-1'),('consumed_at_source','3')]:
            bad=deepcopy(self.spec); bad[field]=value; bads.append(bad)
        bad=deepcopy(self.spec); bad['leftovers'].append(deepcopy(bad['leftovers'][0])); bads.append(bad)
        bad=deepcopy(self.spec); bad['storage']['max_interval_days']=1; bads.append(bad)
        bad=deepcopy(self.spec); bad['leftovers'][0]['slot_id']=bad['source_slot_id']; bads.append(bad)
        for bad in bads:
            before=self.store.path.read_bytes(); self.assertEqual(self.prepare(bad)['status'],'needs_input'); self.assertEqual(before,self.store.path.read_bytes())
        self.app.handle({'operation':'menu','action':'lock','menu_ref':mp.menu_ref(self.menu),'slot_id':self.menu['slots'][0]['slot_id'],'locked':True})
        self.assertEqual(self.prepare()['status'],'needs_input')

    def test_actual_source_and_multiple_leftovers_are_required_exact_and_idempotent(self):
        menu=self.apply(self.prepare())['menu']; source,*leftovers=menu['slots']
        before=self.store.path.read_bytes()
        self.assertEqual(self.cook(menu,leftovers[0])["status"],"needs_input")
        self.assertEqual(self.cook(menu,source)["status"],"needs_input")
        self.assertEqual(before,self.store.path.read_bytes())
        result=self.cook(menu,source,actual_batch={'prepared_portions':'5','consumed_at_source':'2'})
        self.assertEqual({s['status'] for s in result['batch_dependencies']},{'confirmed_source'})
        prior=deepcopy(self.store.read()['recipe_usage'][self.menu['menu_id']])
        for index,slot in enumerate(leftovers):
            result=self.cook(menu,slot,idempotency_key=f'left-{index}')
            self.assertFalse(result['new_recipe_usage'])
            self.assertEqual(result,self.cook(menu,slot,idempotency_key=f'left-{index}'))
        self.assertEqual(self.store.read()['recipe_usage'][self.menu['menu_id']],prior)
        self.assertEqual(len(self.app._usage_summary(self.store.read(),source['recipe_key'],menu['week'])['blocked_by']),1)
        self.assertEqual(len(self.store.read()['batch_outcomes']['leftovers']),2)

    def test_source_not_cooked_or_actual_mismatch_invalidates_future_dependencies(self):
        menu=self.apply(self.prepare())['menu']; source,leftover,*_=menu['slots']
        snapshot=deepcopy(menu)
        for changes in ({'action':'mark_not_cooked'},{'actual_batch':{'prepared_portions':'4','consumed_at_source':'2'}}):
            result=self.cook(menu,source,**changes)
            self.assertEqual({s['status'] for s in result['batch_dependencies']},{'needs_replan'})
            self.assertEqual(self.cook(menu,leftover)["status"],"needs_input")
            self.assertEqual(snapshot,self.store.read()['menu'])

    def test_replan_preserves_valid_graph_and_rejects_partial_source_or_lock(self):
        menu=self.apply(self.prepare())['menu']; source,leftover,*_=menu['slots']
        request={'operation':'menu','action':'replan_prepare','menu_ref':mp.menu_ref(menu),
                 'remaining_dates':[source['date']],'planner_input':{'candidates':self.candidates}}
        self.assertEqual(self.app.handle(request)['replan']['status'],'needs_input')
        request['remaining_dates']=['2026-09-09']; request['planner_input']={'candidates':self.candidates[3:]}
        self.app.handle({'operation':'menu','action':'lock','menu_ref':mp.menu_ref(menu),'slot_id':source['slot_id'],'locked':True})
        self.assertEqual(self.app.handle(request)['replan']['status'],'needs_input')
        self.app.handle({'operation':'menu','action':'lock','menu_ref':mp.menu_ref(menu),'slot_id':source['slot_id'],'locked':False})
        self.cook(menu,source,actual_batch={'prepared_portions':'5','consumed_at_source':'2'})
        prepared=self.app.handle(request)['replan']; successor=self.app.handle({'operation':'menu','action':'replan_apply','replan':prepared})['menu']
        self.assertEqual(successor['slots'][:2],menu['slots'][:2]); self.assertEqual(successor['batch'],menu['batch'])
        self.assertEqual(len(mp.shopping_menu(successor)['dishes']),1)
        self.cook(successor,leftover)
        self.assertEqual(len(self.app._usage_summary(self.store.read(),source['recipe_key'],menu['week'])['blocked_by']),1)
        self.assertEqual(self.fixture.provider.calls,[])

    def test_past_invalid_leftovers_do_not_block_remaining_week(self):
        menu=self.apply(self.prepare())['menu']; source=menu['slots'][0]
        self.cook(menu,source,action='mark_not_cooked')
        with mock.patch.object(Application,'_household_today',return_value=date(2026,9,9)):
            prepared=self.app.handle({'operation':'menu','action':'replan_prepare','menu_ref':mp.menu_ref(menu),
                'remaining_dates':['2026-09-09'],'planner_input':{'candidates':self.candidates[3:]}})['replan']
            self.assertEqual(prepared['status'],'prepared')
            successor=self.app.handle({'operation':'menu','action':'replan_apply','replan':prepared})['menu']
        self.assertEqual(successor['slots'][:2],menu['slots'][:2])
        self.assertEqual(len(mp.shopping_menu(successor)['dishes']),1)

    def test_historical_leftover_context_cannot_be_removed_with_conflicting_source(self):
        menu=self.apply(self.prepare())['menu']; source,leftover,_=menu['slots']
        self.cook(menu,source,actual_batch={'prepared_portions':'5','consumed_at_source':'2'})
        self.cook(menu,leftover)
        self.cook(menu,source,action='mark_not_cooked')
        prepared=self.app.handle({'operation':'menu','action':'replan_prepare','menu_ref':mp.menu_ref(menu),
            'remaining_dates':['2026-09-07','2026-09-09'],'planner_input':{'candidates':self.candidates}})['replan']
        self.assertEqual(prepared['status'],'needs_input')
        correction=self.cook(menu,leftover,action='mark_not_cooked')
        self.assertFalse(correction['cooked'])
        self.assertEqual(mp.slot_outcome(self.store.read(),menu,leftover),'not_cooked')

    def test_source_replacement_removes_all_future_dependencies_and_stale_never_writes(self):
        prepared=self.prepare(); stale=deepcopy(prepared); stale['successor']['batch']['prepared_portions']={'numerator':9,'denominator':1}
        stale['batch_digest']=mp.digest({k:v for k,v in stale.items() if k!='batch_digest'})
        before=self.store.path.read_bytes()
        with self.assertRaises(HouseholdError): self.apply(stale)
        self.assertEqual(before,self.store.path.read_bytes())
        menu=self.apply(prepared)['menu']
        repl=self.app.handle({'operation':'menu','action':'replan_prepare','menu_ref':mp.menu_ref(menu),
            'remaining_dates':[s['date'] for s in menu['slots']], 'planner_input':{'candidates':self.candidates}})['replan']
        successor=self.app.handle({'operation':'menu','action':'replan_apply','replan':repl})['menu']
        self.assertNotIn('batch',successor)
        self.assertTrue(all('source_slot_id' not in s for s in successor['slots']))
        self.assertEqual(len(successor['dishes']),3)


class BatchMigrationTests(unittest.TestCase):
    def test_private_atomic_v10_backup_defaults_and_newer_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            store=StateStore(directory,fixtures.CONFIG); old=store.read(); old['version']=10; old.pop('batch_outcomes')
            store.path.write_text(json.dumps(old)); before=store.path.read_bytes()
            with mock.patch('core._atomic_json',side_effect=OSError('fixture')):
                with self.assertRaises(OSError): StateStore(directory,fixtures.CONFIG)
            self.assertEqual(before,store.path.read_bytes())
            current=StateStore(directory,fixtures.CONFIG)
            backup=Path(directory)/'state-v10.backup.json'
            self.assertEqual(json.loads(backup.read_text()),old); self.assertEqual(backup.stat().st_mode&0o777,0o600)
            self.assertEqual(current.read()['version'],12); self.assertEqual(current.read()['profile']['meals']['batch_dishes'],0)
            raw=backup.read_bytes(); StateStore(directory,fixtures.CONFIG); self.assertEqual(raw,backup.read_bytes())
            old['version']=99; store.path.write_text(json.dumps(old))
            with self.assertRaisesRegex(HouseholdError,'newer'): StateStore(directory,fixtures.CONFIG)
