from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from core import StateStore, HouseholdError
from service import Application
import menu_planning as mp
import planning_feedback as pf
import test_meal_concierge_planner as fixtures


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WeeklyPlannerTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.app = self.fixture.app; self.store = self.fixture.store
        self.clock = mock.patch.object(Application, '_household_today', return_value=date(2026,9,7))
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.candidates = self.fixture.save_candidates(3)
        self.input = self.fixture.request(self.candidates, alternatives=3)

    def event(self, action, **kwargs):
        return self.app.handle({'operation':'feedback','action':action, **kwargs})

    def plan(self):
        return self.fixture.plan(self.input)

    def target(self, menu, index=0):
        slot = menu['slots'][index]
        return {'menu_ref':mp.menu_ref(menu), 'slot_id':slot['slot_id'], 'recipe_key':slot['recipe_key'], 'reference':slot['reference']}

    def test_acceptance_exact_idempotency_no_positive_signal_and_save(self):
        plan = self.plan(); handoff = plan['save_handoff']
        before = self.store.read()
        self.assertEqual(self.event('inspect')['events'], [])
        event = self.event('accept', planner_handoff=handoff, idempotency_key='accept')['event']
        self.assertEqual(event, self.event('accept', planner_handoff=handoff, idempotency_key='accept')['event'])
        self.assertEqual(self.event('inspect')['signals'], {})
        self.assertEqual(self.plan()['input_digest'], plan['input_digest'])
        self.app.handle({'operation':'menu','action':'save','planner_handoff':handoff})
        self.assertEqual(self.store.read()['profile'], before['profile'])
        with self.assertRaises(HouseholdError): self.event('accept', planner_handoff=handoff, reason='changed', idempotency_key='accept')
        self.assertEqual(self.fixture.provider.calls, [])

    def test_proposal_rejection_binding_rank_reason_and_stale_payload(self):
        plan = self.plan(); handoff = plan['save_handoff']; slot = handoff['selection']['slots'][0]
        bad = deepcopy(handoff); bad['selection']['slots'][0]['name']='fabricated'
        before = self.store.path.read_bytes()
        with self.assertRaises(HouseholdError): self.event('reject',planner_handoff=bad,recipe_key=slot['recipe_key'],reference=slot['reference'],idempotency_key='bad')
        self.assertEqual(before,self.store.path.read_bytes())
        self.event('reject',planner_handoff=handoff,recipe_key=slot['recipe_key'],reference=slot['reference'],idempotency_key='reject')
        fresh = self.plan()
        self.assertNotEqual(plan['input_digest'],fresh['input_digest'])
        self.assertNotEqual(fresh['selection']['slots'][0]['recipe_key'],slot['recipe_key'])
        rejected = next(s for s in fresh['selections'] if s['slots'][0]['recipe_key']==slot['recipe_key'])
        reasons = rejected['slots'][0]['reason_contributions']
        self.assertEqual(next(r['weight'] for r in reasons if r['code']=='feedback:explicit-v1'),-2)
        self.assertEqual(rejected['total_score'],sum(r['weight'] for r in reasons)+sum(r['weight'] for r in rejected['plan_reason_contributions']))
        with self.assertRaises(HouseholdError): self.event('accept',planner_handoff=handoff,idempotency_key='stale')

    def test_saved_rejection_caps_undo_reset_and_restart(self):
        menu = self.app.handle({'operation':'menu','action':'save','planner_handoff':self.plan()['save_handoff']})['menu']
        target = self.target(menu); before = self.store.read()
        for i in range(8): self.event('reject',target=target,idempotency_key=f'reject-{i}')
        self.assertEqual(self.event('inspect')['signals'][target['recipe_key']],-6)
        reset = self.event('reset',scope='recipe',recipe_key=target['recipe_key'],idempotency_key='reset')['event']
        self.assertEqual(self.event('inspect')['signals'],{})
        undo = self.event('undo',event_id=reset['event_id'],idempotency_key='undo-reset')['event']
        self.assertEqual(self.event('inspect')['signals'][target['recipe_key']],-6)
        self.event('undo',event_id=undo['event_id'],idempotency_key='redo-reset')
        self.assertEqual(self.event('inspect')['signals'],{})
        after = self.store.read()
        for field in ('profile','recipe_usage','menu','order_snapshots'):
            self.assertEqual(before[field],after[field])
        restarted = Application(StateStore(self.store.directory, fixtures.CONFIG), fixtures.NoProviderCalls(), object())
        self.assertEqual(restarted.handle({'operation':'feedback','action':'inspect'}),self.event('inspect'))

    def test_atomic_swap_exact_successor_and_scoped_reset(self):
        menu = self.app.handle({'operation':'menu','action':'save','planner_handoff':self.plan()['save_handoff']})['menu']
        remaining = [c for c in self.candidates if c['recipe_ref'] != menu['slots'][0]['reference']['recipe_ref']]
        prepared = self.app.handle({'operation':'menu','action':'replan_prepare','menu_ref':mp.menu_ref(menu),
            'remaining_dates':['2026-09-07'],'planner_input':{'candidates':remaining}})['replan']
        successor = self.app.handle({'operation':'menu','action':'replan_apply','replan':prepared})['menu']
        former=self.target(menu); latter=self.target(successor)
        before=self.store.path.read_bytes(); bad=deepcopy(latter); bad['slot_id']='0'
        with self.assertRaises(HouseholdError): self.event('swap',from_target=former,to_target=bad,idempotency_key='bad-swap')
        self.assertEqual(before,self.store.path.read_bytes())
        result=self.event('swap',from_target=former,to_target=latter,idempotency_key='swap')
        self.assertEqual(len(result['event']['contributions']),2)
        self.assertEqual(result['effective']['signals'],{former['recipe_key']:-2,latter['recipe_key']:2})
        self.event('reset',scope='recipe',recipe_key=former['recipe_key'],idempotency_key='reset-former')
        self.assertEqual(self.event('inspect')['signals'],{latter['recipe_key']:2})
        self.event('reset',scope='all',idempotency_key='reset-all')
        self.assertEqual(self.event('inspect')['signals'],{})

    def test_no_implicit_feedback_ambiguity_and_hard_constraints(self):
        plan=self.plan()
        menu=self.app.handle({'operation':'menu','action':'save','planner_handoff':plan['save_handoff']})['menu']
        for action in ('mark_cooked','mark_not_cooked'):
            self.app.handle({'operation':'recipes','action':action,'menu_id':menu['menu_id'],'expected_revision':menu['revision'],'slot_id':menu['slots'][0]['slot_id']})
        self.assertEqual(self.event('inspect')['events'],[])
        for action,kwargs in [('accept',{}),('reject',{'target':{'name':'Recipe 0'}}),('swap',{}),('reset',{})]:
            before=self.store.path.read_bytes()
            with self.assertRaises(HouseholdError): self.event(action,idempotency_key='ambiguous',**kwargs)
            self.assertEqual(before,self.store.path.read_bytes())
        with self.store.locked() as state:
            state['profile']['diet']['allergies_or_sensitivities']=['milk']
        self.assertNotEqual(self.plan()['status'],'planned')
        self.assertEqual(self.fixture.provider.calls,[])

    def test_inspection_pages_fit_actual_wire_and_stale_cursor_fails(self):
        handoff = self.plan()['save_handoff']
        for i in range(260):
            self.event('accept',planner_handoff=handoff,reason='😀'*500,idempotency_key=str(i)+'😀'*190)
        seen = []
        request = {'operation':'feedback','action':'inspect','limit':25}
        first_cursor = None
        while True:
            result = self.fixture.socket_call(request)
            self.assertTrue(result['ok'])
            self.assertLess(len(json.dumps(result,ensure_ascii=True).encode()),2*1024*1024)
            page=result['result']; seen.extend(e['event_id'] for e in page['events'])
            cursor=page['next_cursor']
            if first_cursor is None: first_cursor=cursor
            if cursor is None: break
            request['cursor']=cursor
        self.assertEqual(len(seen),260)
        self.assertEqual(len(set(seen)),260)
        self.event('accept',planner_handoff=handoff,idempotency_key='next')
        with self.assertRaises(HouseholdError): self.event('inspect',cursor=first_cursor)
        self.assertEqual(self.event('inspect',view='signals')['signals'],{})

    def test_decay_bound_date_and_compaction_dependency_integrity(self):
        events=[]
        def add(kind, day, key, targets=None):
            return pf.append(events,kind=kind,binding={},contributions=[{'recipe_key':'exact','direction':-1}] if kind=='reject' else [],
                reason=None,key=key,signature=key,as_of_date=day,targets=targets)
        original=add('reject','2026-01-01','one')
        for day,wanted in [('2026-01-30',-2),('2026-01-31',-1),('2026-03-02',0)]:
            self.assertEqual(pf.effective(events,day)['signals'].get('exact',0),wanted)
        undo=add('undo','2026-06-30','undo',[original['event_id']])
        # Original expired when correction is added, but its fresh dependency pins it.
        self.assertEqual(len(events),2)
        self.assertEqual(pf.effective(events,'2026-07-01')['signals'],{})
        self.assertEqual(len(pf.compact(events,'2026-07-01')),2)
        self.assertEqual(pf.compact(events,'2027-01-01'),[])
        self.assertEqual(events[0]['event_id'],original['event_id'])
        self.assertEqual(events[1]['targets'],[original['event_id']])


class FeedbackMigrationTests(unittest.TestCase):
    def test_atomic_private_v9_backup_and_newer_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            store=StateStore(directory,fixtures.CONFIG); old=store.read()
            old['version']=9; old.pop('planning_feedback'); old.pop('batch_outcomes'); store.path.write_text(json.dumps(old))
            before=store.path.read_bytes()
            with mock.patch('core._atomic_json',side_effect=OSError('fixture')):
                with self.assertRaises(OSError): StateStore(directory,fixtures.CONFIG)
            self.assertEqual(before,store.path.read_bytes())
            current=StateStore(directory,fixtures.CONFIG)
            backup=Path(directory)/'state-v9.backup.json'
            self.assertEqual(json.loads(backup.read_text()),old)
            self.assertEqual(backup.stat().st_mode&0o777,0o600)
            self.assertEqual(current.read()['version'],12)
            self.assertEqual(current.read()['planning_feedback'],[])
            raw=backup.read_bytes(); StateStore(directory,fixtures.CONFIG); self.assertEqual(raw,backup.read_bytes())
            old['version']=99; store.path.write_text(json.dumps(old))
            with self.assertRaisesRegex(HouseholdError,'newer'): StateStore(directory,fixtures.CONFIG)
