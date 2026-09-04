"""Exact stable meal slots and structural shopping comparisons (no providers)."""
from copy import deepcopy
import hashlib
import json
from typing import Any, Mapping

from core import HouseholdError
from product_planner import menu_requirements

MAX_PLANNING_MENUS = 2000


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def initial_planning():
    return {"locks": {}, "history": {}, "retired": {}, "applied": {}, "outcomes": {}}


def menu_ref(menu):
    return {key: menu[key] for key in ("menu_id", "revision", "digest")}


def exact_menu(state, supplied):
    current = state.get("menu")
    if not isinstance(current, Mapping) or not isinstance(supplied, Mapping) or canonical(menu_ref(current)) != canonical(supplied):
        raise HouseholdError("replan requires the exact current menu ID, revision and digest")
    return current


def slots(menu):
    values = menu.get("slots")
    if not isinstance(values, list) or not values:
        raise HouseholdError("legacy schedule has no exact slots; create a new structured plan")
    return values


def slot_by_id(menu, slot_id):
    matches = [s for s in slots(menu) if s["slot_id"] == slot_id]
    if len(matches) != 1:
        raise HouseholdError("slot_id does not identify one exact current meal")
    return matches[0]


def lock_key(menu):
    return f'{menu["menu_id"]}:{menu["revision"]}'


def slot_outcome(state, menu, slot):
    outcome = state.get("menu_planning", {}).get("outcomes", {}).get(slot["slot_id"])
    if outcome is not None:
        return outcome["outcome"]
    owner = menu.get("slot_owners", {}).get(slot["slot_id"], menu["menu_id"])
    record = state.get("recipe_usage", {}).get(owner, {})
    if slot["slot_id"] in record.get("cooked_slot_ids", []):
        return "cooked"
    if slot["slot_id"] in record.get("not_cooked_slot_ids", []):
        return "not_cooked"
    return None


def shopping_menu(menu, historical_ids=None):
    result = deepcopy(menu)
    historical = set(menu.get("historical_slot_ids", []) if historical_ids is None else historical_ids)
    keys = {s["recipe_key"] for s in menu.get("slots", []) if s["slot_id"] in historical}
    for collection in ("dishes", "salads"):
        result[collection] = [r for r in result[collection] if r.get("recipe_key") not in keys]
    return result


def shopping_comparison(before, after):
    old, old_unresolved = menu_requirements(shopping_menu(before), maximum=None)
    new, new_unresolved = menu_requirements(shopping_menu(after), maximum=None)
    old = {r["requirement_id"]: {k: v for k, v in r.items() if k != "sources"} for r in old}
    new = {r["requirement_id"]: {k: v for k, v in r.items() if k != "sources"} for r in new}
    same = sorted(k for k in old.keys() & new.keys() if old[k] == new[k])
    return {"kind": "structural_recipe_requirements", "unchanged": [new[k] for k in same],
            "removed": [old[k] for k in sorted(old) if k not in same],
            "added": [new[k] for k in sorted(new) if k not in same],
            "unresolved": {"before": old_unresolved, "after": new_unresolved},
            "cart_action": "separate_explicit_sync_or_order_reconciliation_required"}


def retire_planned_slots(state, menu):
    """Cancel only active planned ownership; historical records stay untouched."""
    for slot in menu.get("slots", []):
        if slot_outcome(state, menu, slot) == "cooked":
            continue
        owner = menu.get("slot_owners", {}).get(slot["slot_id"], menu["menu_id"])
        if state.get("recipe_usage", {}).get(owner, {}).get("status") != "planned":
            continue
        retired = state["menu_planning"]["retired"]
        if owner not in retired and len(retired) >= MAX_PLANNING_MENUS:
            raise HouseholdError("planning retirement limit reached")
        values = retired.setdefault(owner, [])
        if slot["recipe_key"] not in values:
            values.append(slot["recipe_key"])
