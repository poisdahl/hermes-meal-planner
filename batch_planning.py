"""Explicit current-plan batch dependencies and exact portion accounting."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from core import HouseholdError
import menu_planning as mp

CONFIRMATION_STATEMENT = "I confirm these exact batch portions, suitability and storage facts for this plan."


def fraction(value, *, zero=False):
    try:
        if isinstance(value, dict) and set(value) == {'numerator','denominator'}:
            n, d = value['numerator'], value['denominator']
            if type(n) is not int or type(d) is not int or not 0 < d <= 1_000_000 or abs(n) > 1_000_000_000:
                raise ValueError()
            result = Fraction(n,d)
        elif type(value) in {str,int} and len(str(value)) <= 30:
            decimal = Decimal(str(value))
            if not decimal.is_finite() or decimal < 0 or decimal > 1000 or not -30 <= decimal.as_tuple().exponent <= 30:
                raise ValueError()
            result = Fraction(decimal)
        else:
            raise ValueError()
        if result < 0 or (not zero and result == 0) or result > 1000 or result.denominator > 1_000_000:
            raise ValueError()
        return result
    except (ValueError, InvalidOperation, OverflowError, ZeroDivisionError):
        raise HouseholdError('portions require an exact bounded decimal string, integer or rational') from None


def rational(value):
    return {'numerator':value.numerator,'denominator':value.denominator}


def normalize(state, menu, value, today):
    expected={'source_slot_id','source_snapshot_digest','prepared_portions','consumed_at_source','suitability','storage','leftovers'}
    if not isinstance(value,dict) or set(value) != expected:
        raise HouseholdError('batch specification needs exact source, portions, suitability, storage and dependent slots')
    if menu.get('batch'):
        raise HouseholdError('replan the existing batch dependency before creating another specification')
    source=mp.slot_by_id(menu,value['source_slot_id'])
    if value['source_snapshot_digest'] != source['snapshot_digest'] or source.get('kind') == 'leftover':
        raise HouseholdError('batch source must be one exact fresh recipe snapshot')
    if source['date'] < today or mp.slot_outcome(state,menu,source) is not None:
        raise HouseholdError('batch source must be an unrecorded current/future meal')
    recipe=next(r for r in menu['dishes']+menu['salads'] if r['recipe_key']==source['recipe_key'])
    prepared=fraction(value['prepared_portions']); consumed=fraction(value['consumed_at_source'])
    if consumed != fraction(str(recipe['portions'])) or prepared <= consumed:
        raise HouseholdError('source consumption must match its exact meal portions, with explicit extra preparation')
    suitability=value['suitability']; storage=value['storage']
    if suitability != {'source':'current_user','value':'suitable'}:
        raise HouseholdError('structured current-user batch suitability is missing; prose/boolean inference is unsupported')
    if not isinstance(storage,dict) or set(storage).difference({'source','method','max_interval_days','use_by_date'}) or storage.get('source')!='current_user' or storage.get('method') not in {'refrigerated','frozen'}:
        raise HouseholdError('explicit structured refrigerated/frozen storage facts are required')
    days=storage.get('max_interval_days'); use_by=storage.get('use_by_date')
    if days is None and use_by is None:
        raise HouseholdError('an explicit maximum interval or use-by date is required')
    last=date.max
    if days is not None:
        if type(days) is not int or not 1 <= days <= 365:
            raise HouseholdError('maximum interval must be an explicit bounded day count')
        last=date.fromisoformat(source['date'])+timedelta(days=days)
    if use_by is not None:
        try:
            parsed=date.fromisoformat(use_by)
            if parsed.isoformat()!=use_by: raise ValueError()
        except (ValueError,TypeError):
            raise HouseholdError('use-by must be an exact ISO date') from None
        last=min(last,parsed)
    raw=value['leftovers']
    if not isinstance(raw,list) or not 1 <= len(raw) <= 6:
        raise HouseholdError('batch needs one to six exact dependent slots')
    dependents=[]; seen=set(); available=prepared-consumed
    for item in raw:
        if not isinstance(item,dict) or set(item)!={'slot_id','portions'}:
            raise HouseholdError('each leftover needs an exact target slot_id and portions')
        target=mp.slot_by_id(menu,item['slot_id'])
        if target['slot_id'] in seen or target['slot_id']==source['slot_id']:
            raise HouseholdError('leftover target is duplicated or equals its source')
        seen.add(target['slot_id'])
        if not source['date'] < target['date'] <= last.isoformat() or mp.slot_outcome(state,menu,target) is not None:
            raise HouseholdError('leftovers must be after their source, within the exact interval, and unrecorded')
        portions=fraction(item['portions']); available-=portions
        if available < 0:
            raise HouseholdError('leftover portions exceed exact source remainder')
        dependents.append({'replaces_slot_id':target['slot_id'],'date':target['date'],'meal_type':target['meal_type'],'portions':rational(portions)})
    locks=set(state['menu_planning']['locks'].get(mp.lock_key(menu),[]))
    if locks & (seen|{source['slot_id']}):
        raise HouseholdError('batch component intersects explicit locks; unlock the component first')
    spec={'source_slot_id':source['slot_id'],'source_snapshot_digest':source['snapshot_digest'],
          'prepared_portions':rational(prepared),'consumed_at_source':rational(consumed),
          'unallocated_portions':rational(available),'suitability':deepcopy(suitability),'storage':deepcopy(storage),
          'leftovers':sorted(dependents,key=lambda d:(d['date'],d['replaces_slot_id']))}
    spec['spec_digest']=mp.digest({'menu_ref':mp.menu_ref(menu),'spec':spec})
    for dependent in spec['leftovers']:
        dependent['slot_id']='slot_'+mp.digest({'spec':spec['spec_digest'],'target':dependent})[:32]
    return spec


def shopping(menu):
    result=deepcopy(menu)
    batch=menu.get('batch')
    if not batch: return result
    source=mp.slot_by_id(menu,batch['source_slot_id'])
    if source['slot_id'] in menu.get('historical_slot_ids',[]):
        return result
    factor=fraction(batch['prepared_portions'])/fraction(batch['consumed_at_source'])
    for recipe in result['dishes']+result['salads']:
        if recipe['recipe_key']!=source['recipe_key']: continue
        for requirement in recipe['shopping_requirements']:
            if requirement.get('scalable') is True:
                # Rational quantities are local shopping metadata, not recipe documents.
                try:
                    amount=Fraction(str(requirement['quantity']))*factor
                    requirement['quantity']=rational(amount)
                except (ValueError, ZeroDivisionError):
                    requirement['scalable']=False
        recipe['batch_prepared_portions']=deepcopy(batch['prepared_portions'])
    return result


def dependency_status(state,menu):
    batch=menu.get('batch')
    if not batch: return []
    actual=state['batch_outcomes']['sources'].get(batch['source_slot_id'])
    statuses=[]
    for slot in menu['slots']:
        if slot.get('source_slot_id')!=batch['source_slot_id']: continue
        recorded=state['batch_outcomes']['leftovers'].get(slot['slot_id'])
        status=recorded['outcome'] if recorded else ('needs_replan' if actual and (actual['outcome']!='cooked' or not actual['matches_plan']) else 'confirmed_source' if actual else 'planned_not_confirmed')
        statuses.append({'slot_id':slot['slot_id'],'source_slot_id':batch['source_slot_id'],'status':status,'new_shopping_requirements':0})
    return statuses


def record_outcome(state,menu,slot,request):
    batch=menu.get('batch')
    if not batch:
        if slot.get('kind')=='leftover':
            raise HouseholdError('historical leftover source context is unavailable; no outcome changed')
        return False
    source_id=batch['source_slot_id']; cooked=request['action']=='mark_cooked'
    data=state['batch_outcomes']
    if slot['slot_id']==source_id:
        if len(data['sources'])>=2000 and source_id not in data['sources']:
            raise HouseholdError('batch outcome limit reached')
        actual=request.get('actual_batch')
        if cooked:
            if not isinstance(actual,dict) or set(actual)!={'prepared_portions','consumed_at_source'}:
                raise HouseholdError('source cooking needs explicit actual prepared/consumed portions')
            prepared=fraction(actual['prepared_portions']); consumed=fraction(actual['consumed_at_source'],zero=True)
            if consumed>prepared: raise HouseholdError('actual consumed portions exceed preparation')
            remaining=prepared-consumed
            matches=prepared==fraction(batch['prepared_portions']) and consumed==fraction(batch['consumed_at_source'])
            data['sources'][source_id]={'outcome':'cooked','prepared_portions':rational(prepared),'consumed_at_source':rational(consumed),
                'confirmed_remaining':rational(remaining),'matches_plan':matches}
        else:
            data['sources'][source_id]={'outcome':'not_cooked','matches_plan':False}
        return False
    if slot.get('source_slot_id')==source_id:
        if len(data['leftovers'])>=2000 and slot['slot_id'] not in data['leftovers']:
            raise HouseholdError('batch outcome limit reached')
        actual=data['sources'].get(source_id)
        if cooked:
            if not actual or actual['outcome']!='cooked' or not actual['matches_plan']:
                raise HouseholdError('leftover needs an explicitly cooked matching source; replan after a mismatch')
            used=sum((fraction(value['portions']) for key,value in data['leftovers'].items()
                      if key!=slot['slot_id'] and value.get('source_slot_id')==source_id and value['outcome']=='cooked'),Fraction(0))
            if used+fraction(slot['portions'])>fraction(actual['confirmed_remaining'],zero=True):
                raise HouseholdError('leftover exceeds confirmed remaining portions')
        data['leftovers'][slot['slot_id']]={'source_slot_id':source_id,'outcome':'cooked' if cooked else 'not_cooked','portions':deepcopy(slot['portions'])}
        return True
    return False


def evaluate_plan(state, original, successor):
    from planner import _effective_facts, _hard_evaluation, _strict_evaluation
    handoff=original.get('planner_selection') or original.get('replan_selection')
    if not handoff:
        return {'status':'unknown','reason':'an exact structured planner selection is required'}
    facts={mp.canonical({k:v for k,v in c.items() if k!='facts'}):c.get('facts',{}) for c in handoff['request']['candidates']}
    recipes={r['recipe_key']:r for r in successor['dishes']}
    candidates=[]; hard=[]
    for slot in successor['slots']:
        recipe=recipes[slot['recipe_key']]
        candidate={'recipe':recipe,'recipe_key':slot['recipe_key'],'usage':{'eligible':True},'materialization_error':None,
                   'facts':_effective_facts(recipe,facts.get(mp.canonical(slot['reference']),{}))}
        check=_hard_evaluation(candidate,state['profile'],{})
        hard.append(check); candidates.append(candidate)
    strict=_strict_evaluation(tuple(candidates),handoff['request'].get('strict_targets',[]),state['profile'])
    return {'status':'pass' if all(c['status']=='pass' for c in hard) and strict['status']=='pass' else 'unknown',
            'hard':hard,'strict':strict}
