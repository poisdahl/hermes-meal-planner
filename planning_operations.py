"""Household menu, cooking feedback, product selection and cart operations.

Application owns shared state and locks; these methods run on that same instance.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import math
import re
import secrets
import time
from typing import Any, Mapping
from core import HouseholdError, cart_summary
from meny import MAX_CART_CLICKS, MENY_CART_TIMEOUT
from recipes import RecipeError, normalize_recipe, recipe_key, scale_recipe, validate_week
from planner import MAX_CANDIDATES, MAX_HISTORY_RECORDS, PLANNER_VERSION, PlannerError, plan_week
from product_planner import MAX_CANDIDATES_PER_REQUIREMENT, MAX_REQUIREMENTS, normalize_approvals, build_product_plan, cart_requirements as prepared_cart_requirements, menu_requirements as exact_menu_requirements, validate_product_plan, product_plan_digest
from product_observations import MAX_PRODUCTS
import menu_planning as mp
import planning_feedback as pf
from planning_assessment import assess_menu, feedback_targets
import batch_planning as bp
from recipe_libraries import MAX_LIBRARY_RECIPE_KEY, RecipeLibraryError, library_recipe_key, library_recipe_key_aliases, validate_library_recipe_ref
from service_common import (
    MAX_EMAIL_HTML_BYTES,
    MAX_MENU_BYTES,
    MAX_REQUEST,
    UNRESOLVED_CHECKOUT_STATUSES,
    bounded_limit,
    canonical,
    menu_digest,
    menu_email_html,
    validate_schedule
)


class PlanningOperations:
    @staticmethod
    def _feedback_slot(state: Mapping[str, Any], value: Any, *, allow_predecessor: bool = False) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {"menu_ref", "slot_id", "recipe_key", "reference"}:
            raise HouseholdError("feedback target needs exact menu_ref, slot_id, recipe_key and reference")
        menu = state.get("menu")
        if not isinstance(menu, Mapping):
            raise HouseholdError("feedback has no active structured menu")
        if canonical(mp.menu_ref(menu)) != canonical(value["menu_ref"]):
            if not allow_predecessor or canonical(menu.get("supersedes")) != canonical(value["menu_ref"]):
                raise HouseholdError("feedback target is not the exact current menu or direct predecessor")
            source = value["menu_ref"]
            menu = state["menu_planning"]["history"].get(f'{source["menu_id"]}:{source["revision"]}')
            if not isinstance(menu, Mapping) or canonical(mp.menu_ref(menu)) != canonical(source):
                raise HouseholdError("feedback predecessor snapshot is unavailable")
        slot = mp.slot_by_id(menu, value["slot_id"])
        if value["recipe_key"] != slot["recipe_key"] or canonical(value["reference"]) != canonical(slot["reference"]):
            raise HouseholdError("feedback recipe reference does not match its exact slot")
        return deepcopy(slot)

    def _feedback(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "inspect")
        allowed = {"operation", "action", "planner_handoff", "target", "from_target", "to_target",
                   "recipe_key", "reference", "event_id", "scope", "reason", "idempotency_key", "view", "limit", "cursor", "experience"}
        if set(request).difference(allowed):
            raise HouseholdError("feedback request has unknown fields")
        snapshot = self.store.read()
        today = self._household_today(snapshot).isoformat()
        events = snapshot["planning_feedback"]
        if action == "inspect":
            view = request.get("view") or "events"
            if view not in {"events", "signals"}:
                raise HouseholdError("feedback inspect view must be events or signals")
            limit = request.get("limit", 20)
            if type(limit) is not int or not 1 <= limit <= 25:
                raise HouseholdError("feedback inspect limit must be one to 25")
            effective = pf.effective(events, today)
            digest = mp.digest({"events": events, "as_of_date": today})
            keys = [e["event_id"] for e in events] if view == "events" else sorted(effective["signals"])
            start = 0
            cursor = request.get("cursor")
            if cursor is not None:
                if not isinstance(cursor, Mapping) or set(cursor) != {"digest", "view", "after"} or cursor["digest"] != digest or cursor["view"] != view or cursor["after"] not in keys:
                    raise HouseholdError("feedback cursor changed or expired; restart inspection")
                start = keys.index(cursor["after"]) + 1
            page_keys = keys[start:start+limit]
            page = events[start:start+limit] if view == "events" else []
            recipe_keys = {c["recipe_key"] for e in page for c in e["contributions"]} if view == "events" else set(page_keys)
            return {"events": deepcopy(page), "effective": {k: deepcopy(v) for k,v in effective.items() if k not in {"events", "signals"}},
                    "signals": {k:effective["signals"][k] for k in sorted(recipe_keys) if k in effective["signals"]},
                    "signal_scope": "page_recipe_keys", "view": view,
                    "cooking_experiences": [e for e in pf.experiences(events) if e["event_id"] in page_keys],
                    "next_cursor": {"digest":digest, "view":view, "after":page_keys[-1]} if start+limit < len(keys) else None,
                    "policy": {"version": pf.POLICY_VERSION, "maximum_events": pf.MAX_EVENTS,
                               "retention_days": pf.RETENTION_DAYS, "decay_days": [30, 60], "per_recipe_cap": 6}}
        if action not in {"accept", "reject", "swap", "experience", "undo", "reset"}:
            raise HouseholdError("feedback action must be explicit: accept, reject, swap, undo or reset")
        key = request.get("idempotency_key")
        reason = request.get("reason")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise HouseholdError("feedback writes require a bounded idempotency_key")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise HouseholdError("feedback reason must be at most 500 characters")
        signature = mp.digest({k:v for k,v in request.items() if v is not None})
        existing = pf.prior(events, key, signature)
        if existing is not None:
            return {"event": existing, "idempotent": True}
        contributions = []
        targets = None
        recipe_scope = None
        binding = {}
        if action in {"accept", "reject"}:
            handoff = request.get("planner_handoff")
            if handoff is not None:
                self._verify_planner_handoff(handoff)
                if handoff["request"]["as_of_date"] != today:
                    raise HouseholdError("feedback proposal is stale; prepare a current proposal")
                if request.get("target") is not None:
                    raise HouseholdError("feedback requires one exact proposal or saved-menu target")
                binding = {k:deepcopy(handoff[k]) for k in ("planner_version", "input_digest", "selection_digest")}
                if action == "reject":
                    matches = [s for s in handoff["selection"]["slots"] if s["recipe_key"] == request.get("recipe_key") and canonical(s["reference"]) == canonical(request.get("reference"))]
                    if len(matches) != 1:
                        raise HouseholdError("proposal rejection needs its exact recipe key and reference")
                    binding["recipe"] = {"recipe_key":matches[0]["recipe_key"], "reference":deepcopy(matches[0]["reference"])}
                    contributions = [{"recipe_key":matches[0]["recipe_key"], "direction":-1}]
            else:
                if action == "accept":
                    raise HouseholdError("plan acceptance needs the complete unchanged planner_handoff")
                slot = self._feedback_slot(snapshot, request.get("target"))
                binding = {"target":deepcopy(request["target"])}
                contributions = [{"recipe_key":slot["recipe_key"], "direction":-1}]
        elif action == "experience":
            target = request.get("target")
            menus = [snapshot.get("menu"), *snapshot.get("menu_planning", {}).get("history", {}).values(), *snapshot.get("order_snapshots", {}).values()]
            if not isinstance(target, Mapping) or not any(canonical(candidate) == canonical(target) for menu in menus if isinstance(menu, Mapping) for candidate in feedback_targets(menu)):
                raise HouseholdError("experience needs an exact returned feedback target from a retained menu")
            experience = request.get("experience")
            if not isinstance(experience, Mapping) or not experience or not set(experience).issubset({"actual_active_minutes", "portion_fit", "leftover_portions"}):
                raise HouseholdError("experience needs actual_active_minutes, portion_fit or leftover_portions")
            if "actual_active_minutes" in experience and (type(experience["actual_active_minutes"]) is not int or not 0 <= experience["actual_active_minutes"] <= 1440):
                raise HouseholdError("actual_active_minutes must be an integer from zero to 1440")
            if "portion_fit" in experience and experience["portion_fit"] not in {"too_small", "right", "too_large"}:
                raise HouseholdError("portion_fit must be too_small, right or too_large")
            if "leftover_portions" in experience and (type(experience["leftover_portions"]) not in {int, float} or not math.isfinite(experience["leftover_portions"]) or not 0 <= experience["leftover_portions"] <= 100):
                raise HouseholdError("leftover_portions must be a finite number from zero to 100")
            binding = {"target": deepcopy(target), "experience": deepcopy(experience)}
            contributions = [{"recipe_key": target["recipe_key"], "direction": 0}]
        elif action == "swap":
            former = self._feedback_slot(snapshot, request.get("from_target"), allow_predecessor=True)
            latter = self._feedback_slot(snapshot, request.get("to_target"))
            current = snapshot["menu"]
            if canonical(request["from_target"]["menu_ref"]) != canonical(current.get("supersedes")) or former["slot_id"] == latter["slot_id"] or former["recipe_key"] == latter["recipe_key"] or (former["date"], former["meal_type"]) != (latter["date"], latter["meal_type"]):
                raise HouseholdError("swap feedback requires one exact replaced predecessor slot and its successor on the same date/type")
            binding = {"from_target":deepcopy(request["from_target"]), "to_target":deepcopy(request["to_target"])}
            contributions = [{"recipe_key":former["recipe_key"], "direction":-1}, {"recipe_key":latter["recipe_key"], "direction":1}]
        elif action == "undo":
            event_id = request.get("event_id")
            if not isinstance(event_id, str) or not any(e["event_id"] == event_id for e in events):
                raise HouseholdError("undo requires one exact retained event_id")
            targets = [event_id]
        else:
            scope = request.get("scope")
            if scope not in {"recipe", "all"}:
                raise HouseholdError("reset requires explicit scope recipe or all")
            if scope == "recipe":
                recipe_scope = request.get("recipe_key")
                if not isinstance(recipe_scope, str) or not any(c["recipe_key"] == recipe_scope for e in events for c in e["contributions"]):
                    raise HouseholdError("recipe reset requires one exact feedback recipe_key")
            elif request.get("recipe_key") is not None:
                raise HouseholdError("all-feedback reset cannot also target one recipe")
            targets = [e["event_id"] for e in events if e["kind"] in {"accept", "reject", "swap", "experience"} and
                       (recipe_scope is None or any(c["recipe_key"] == recipe_scope for c in e["contributions"]))]
        with self.store.locked() as state:
            if canonical(state) != canonical(snapshot):
                raise HouseholdError("feedback target changed before write; inspect again")
            event = pf.append(state["planning_feedback"], kind=action, binding=binding,
                contributions=contributions, reason=reason, key=key, signature=signature, as_of_date=today,
                targets=targets, recipe_key=recipe_scope)
            effective = pf.effective(state["planning_feedback"], today)
            affected = {c["recipe_key"] for c in contributions}
            summary = {k: v for k, v in effective.items() if k not in {"events", "signals"}}
            summary["signals"] = {k: effective["signals"][k] for k in sorted(affected) if k in effective["signals"]}
            return {"event": event, "effective": summary, "signal_scope": "event_recipe_keys"}

    def _mark_slot(self, request: Mapping[str, Any]) -> dict[str, Any]:
        with self.store.locked() as state:
            menu = state.get("menu")
            if not isinstance(menu, Mapping) or request.get("menu_id") != menu.get("menu_id") or type(request.get("expected_revision")) is not int or request["expected_revision"] != menu["revision"]:
                raise HouseholdError("cooking requires the exact current menu ID and revision")
            slot = mp.slot_by_id(menu, request.get("slot_id"))
            if request.get("recipe_key") not in {None, slot["recipe_key"]}:
                raise HouseholdError("recipe_key does not match slot_id")
            action = request["action"]
            key = request.get("idempotency_key")
            if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 200):
                raise HouseholdError("idempotency_key must be bounded text")
            signature = canonical({"menu": mp.menu_ref(menu), "slot_id": slot["slot_id"], "action": action, "actual_batch": request.get("actual_batch")})
            if key and (existing := self._usage_request(state, key, signature)):
                return existing
            owner = menu.get("slot_owners", {}).get(slot["slot_id"], menu["menu_id"])
            record = state["recipe_usage"].get(owner)
            if not isinstance(record, dict) or slot not in record.get("slots", []):
                raise HouseholdError("exact slot usage owner is unavailable")
            cooked = action == "mark_cooked"
            try:
                handled = bp.record_outcome(state, menu, slot, request)
            except HouseholdError as exc:
                return {"status":"needs_input", "reason":str(exc), "slot_id":slot["slot_id"]}
            if handled:
                result = {"menu_id":menu["menu_id"], "slot_id":slot["slot_id"], "cooked":cooked,
                          "kind":"leftover", "new_recipe_usage":False, "batch_dependencies":bp.dependency_status(state,menu)}
                if key:
                    self._store_usage_request(state,key,signature,result)
                return result
            if owner != menu["menu_id"]:
                outcomes = state["menu_planning"]["outcomes"]
                if slot["slot_id"] not in outcomes and len(outcomes) >= mp.MAX_PLANNING_MENUS:
                    raise HouseholdError("planning outcome limit reached")
                outcomes[slot["slot_id"]] = {"outcome": "cooked" if cooked else "not_cooked", "owner_menu_id": owner,
                    "recorded_in_menu_id": menu["menu_id"], "recipe_key": slot["recipe_key"]}
            else:
                for field, value in (("cooked_slot_ids", slot["slot_id"]), ("cooked_keys", slot["recipe_key"]),
                                     ("not_cooked_slot_ids", slot["slot_id"]), ("not_cooked_keys", slot["recipe_key"])):
                    values = record.setdefault(field, [])
                    wanted = cooked == field.startswith("cooked")
                    if wanted and value not in values:
                        values.append(value)
                    if not wanted and value in values:
                        values.remove(value)
            result = {"menu_id": menu["menu_id"], "slot_id": slot["slot_id"], "recipe_key": slot["recipe_key"], "cooked": cooked, "batch_dependencies": bp.dependency_status(state,menu)}
            if key:
                self._store_usage_request(state, key, signature, result)
            return result

    @staticmethod
    def _replan_state_digest(state: Mapping[str, Any]) -> str:
        return mp.digest({key: state.get(key) for key in ("menu", "profile", "recipe_usage", "menu_planning", "planning_feedback", "batch_outcomes")})

    def _prepare_replan(self, request: Mapping[str, Any]) -> dict[str, Any]:
        state = self.store.read()
        current = mp.exact_menu(state, request.get("menu_ref"))
        slots = mp.slots(current)
        today = self._household_today(state).isoformat()
        if request.get("as_of_date") not in {None, today}:
            raise HouseholdError("replan as_of_date changed; prepare again")
        dates = request.get("remaining_dates")
        if not isinstance(dates, list) or not 1 <= len(dates) <= 7 or any(not isinstance(day, str) for day in dates) or len(set(dates)) != len(dates):
            raise HouseholdError("remaining_dates must be one to seven exact distinct slot dates")
        by_date = {s["date"]: s for s in slots}
        if len(by_date) != len(slots) or any(day not in by_date or day < today for day in dates):
            raise HouseholdError("remaining_dates must name exact current slots on or after as_of_date")
        stored_locks = state["menu_planning"]["locks"].get(mp.lock_key(current), [])
        explicit = request.get("locked_slot_ids")
        if explicit is None:
            explicit = []
        if not isinstance(explicit, list) or len(explicit) > 7 or any(not isinstance(v, str) for v in explicit) or len(set(explicit)) != len(explicit):
            raise HouseholdError("locked_slot_ids must be exact distinct slot IDs")
        for value in explicit:
            mp.slot_by_id(current, value)
        locks = sorted(set(stored_locks) | set(explicit))
        historical = {s["slot_id"] for s in slots if s["date"] < today or mp.slot_outcome(state, current, s) == "cooked"}
        replacing = [s for s in slots if s["date"] in dates and s["slot_id"] not in historical and s["slot_id"] not in locks]
        if not replacing:
            return {"status": "needs_input", "reason": "no requested unlocked future slots"}
        carried = [s for s in slots if s not in replacing]
        batch = current.get("batch")
        if batch:
            component = {batch["source_slot_id"]} | {s["slot_id"] for s in slots if s.get("source_slot_id") == batch["source_slot_id"] and s["slot_id"] not in historical}
            changed = {s["slot_id"] for s in replacing}
            invalid = {s["slot_id"] for s in bp.dependency_status(state,current) if s["status"]=="needs_replan" and s["slot_id"] not in historical}
            if invalid.difference(changed):
                return {"status":"needs_input", "reason":"all invalid future leftovers must be replanned together"}
            if changed & component and set(locks) & component:
                return {"status":"needs_input", "reason":"locked batch component cannot be partially replanned"}
            if batch["source_slot_id"] in changed and any(s.get("source_slot_id")==batch["source_slot_id"] and s["slot_id"] in historical for s in slots):
                return {"status":"needs_input", "reason":"a source with historical leftovers must retain its exact context; correct the conflicting source outcome first"}
            if batch["source_slot_id"] in changed and not component <= changed:
                return {"status":"needs_input", "reason":"source replacement requires every future dependent date in the same replan"}
        planning_state = deepcopy(state)
        for slot in replacing:
            owner = current.get("slot_owners", {}).get(slot["slot_id"], current["menu_id"])
            if slot.get("kind") != "leftover":
                planning_state["menu_planning"]["retired"].setdefault(owner, []).append(slot["recipe_key"])
        planner_input = request.get("planner_input")
        if not isinstance(planner_input, Mapping):
            raise HouseholdError("replan requires bounded planner_input candidates")
        planner_input = deepcopy(dict(planner_input))
        if planner_input.get("week", current["week"]) != current["week"]:
            raise HouseholdError("replan must remain in the exact source week")
        replacement_dates = sorted(s["date"] for s in replacing)
        if planner_input.get("dates", replacement_dates) != replacement_dates:
            raise HouseholdError("planner dates must exactly match the unlocked remaining dates")
        planner_input.update({"week": current["week"], "dates": replacement_dates, "as_of_date": today, "alternatives": 1})
        effective = self._effective_planner_request(planner_input, planning_state, anchor_current_date=True)
        candidates = self._resolve_planner_candidates(effective, planning_state)
        carried_keys = {s["recipe_key"] for s in carried}
        candidates = [c for c in candidates if c["recipe_key"] not in carried_keys]
        if not candidates:
            return {"status": "needs_input", "reason": "no distinct replacement candidates"}
        effective["candidates"] = [{**c["reference"], "facts": c["supplied_facts"]} for c in candidates]
        result = self._run_planner(effective, candidates, planning_state)
        if result["status"] != "planned":
            return {"status": "needs_input", "plan": result}
        replacement = self._materialize_planner_menu(result["save_handoff"], candidates)
        # Stable IDs are scoped to this exact predecessor; only carried slots keep IDs.
        for slot in replacement["slots"]:
            slot["slot_id"] = "slot_" + mp.digest({"source": mp.menu_ref(current), "replacement": slot})[:32]
        successor = {"week": current["week"], "dishes": [], "salads": [],
                     "slots": sorted(deepcopy(carried) + replacement["slots"], key=lambda s: (s["date"], s["meal_type"])),
                     "historical_slot_ids": sorted(historical), "supersedes": mp.menu_ref(current),
                     "replan_selection": deepcopy(result["save_handoff"]),
                     "planning_scope": deepcopy(current.get("planning_scope") or (current.get("planner_selection") or {}).get("request") or {"dates": sorted({s["date"] for s in current["slots"]}), "portions": state["profile"]["meals"]["portions"]})}
        successor["slot_owners"] = {s["slot_id"]: current.get("slot_owners", {}).get(s["slot_id"], current["menu_id"]) for s in carried}
        recipes = {r["recipe_key"]: r for r in current["dishes"] + current["salads"] + replacement["dishes"]}
        successor["dishes"] = [deepcopy(recipes[key]) for key in dict.fromkeys(s["recipe_key"] for s in successor["slots"])]
        if batch and batch["source_slot_id"] not in {s["slot_id"] for s in replacing}:
            successor["batch"] = deepcopy(batch)
        successor["schedule"] = [{"day": s["date"], "meal": recipes[s["recipe_key"]]["name"], "recipe_key": s["recipe_key"], "slot_id": s["slot_id"]} for s in successor["slots"]]
        before = deepcopy(current)
        before["historical_slot_ids"] = sorted(historical)
        prepared = {"status": "prepared", "source": mp.menu_ref(current), "as_of_date": today,
                    "remaining_dates": sorted(dates), "locked_slot_ids": sorted(explicit),
                    "planner_input": planner_input, "state_digest": self._replan_state_digest(state),
                    "successor": successor, "replaced_slot_ids": sorted(s["slot_id"] for s in replacing),
                    "shopping_comparison": mp.shopping_comparison(before, successor)}
        prepared["replan_digest"] = mp.digest(prepared)
        if len(canonical(prepared).encode()) > MAX_MENU_BYTES or len(json.dumps({"ok": True, "result": {"replan": prepared}}, ensure_ascii=True).encode()) > MAX_REQUEST - 4096:
            raise HouseholdError("replan exceeds bounded response size")
        return prepared

    def _replanning(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request["action"]
        if action in {"batch_prepare", "batch_apply"}:
            return self._batch(request)
        if action == "lock":
            desired = request.get("locked")
            if not isinstance(desired, bool):
                raise HouseholdError("locked must be an explicit desired boolean")
            with self.store.locked() as state:
                current = mp.exact_menu(state, request.get("menu_ref"))
                slot = mp.slot_by_id(current, request.get("slot_id"))
                usage = state["recipe_usage"].get(current["menu_id"], {})
                if current.get("phase") == "ordered" or usage.get("status") == "ordered":
                    raise HouseholdError("ordered/historical locks are immutable; pass exact preparation locks instead")
                values = state["menu_planning"]["locks"].setdefault(mp.lock_key(current), [])
                if desired and slot["slot_id"] not in values:
                    values.append(slot["slot_id"])
                    values.sort()
                elif not desired and slot["slot_id"] in values:
                    values.remove(slot["slot_id"])
                return {"menu_ref": mp.menu_ref(current), "slot_id": slot["slot_id"], "locked": desired}
        if action == "replan_prepare":
            return {"replan": self._prepare_replan(request)}
        supplied = request.get("replan")
        if not isinstance(supplied, Mapping) or supplied.get("status") != "prepared" or supplied.get("replan_digest") != mp.digest({k: v for k, v in supplied.items() if k != "replan_digest"}):
            raise HouseholdError("replan_apply requires the complete unchanged prepared replan")
        state = self.store.read()
        applied = state["menu_planning"]["applied"].get(supplied["replan_digest"])
        if applied is not None:
            return {"menu_ref": deepcopy(applied), "idempotent": True}
        fresh = self._prepare_replan({"menu_ref": supplied["source"], "as_of_date": supplied["as_of_date"],
            "remaining_dates": supplied["remaining_dates"], "locked_slot_ids": supplied["locked_slot_ids"], "planner_input": supplied["planner_input"]})
        if canonical(fresh) != canonical(supplied):
            raise HouseholdError("replan is stale or altered; prepare again")
        with self.store.locked() as state:
            if any(state.get(k) for k in ("pending_checkout", "pending_cancellation", "order_change")):
                raise HouseholdError("reconcile pending protected operations before replan apply")
            if self._household_today(state).isoformat() != supplied["as_of_date"] or self._replan_state_digest(state) != supplied["state_digest"]:
                raise HouseholdError("replan date or state changed; prepare again")
            return self._commit_successor(state, supplied)

    def _prepare_batch(self, request):
        state = self.store.read()
        current = mp.exact_menu(state, request.get("menu_ref"))
        today = self._household_today(state).isoformat()
        if request.get("as_of_date") not in {None, today}:
            raise HouseholdError("batch preparation date changed")
        try:
            spec = bp.normalize(state, current, request.get("batch_spec"), today)
        except HouseholdError as exc:
            return {"status":"needs_input", "reason":str(exc)}
        source = mp.slot_by_id(current, spec["source_slot_id"])
        replaced = {d["replaces_slot_id"] for d in spec["leftovers"]}
        carried = [deepcopy(s) for s in current["slots"] if s["slot_id"] not in replaced]
        leftover_slots = [{"slot_id":d["slot_id"], "date":d["date"], "meal_type":d["meal_type"], "kind":"leftover",
            "source_slot_id":source["slot_id"], "portions":deepcopy(d["portions"]), "recipe_key":source["recipe_key"],
            "reference":deepcopy(source["reference"]), "snapshot_digest":source["snapshot_digest"]} for d in spec["leftovers"]]
        successor = {"week":current["week"], "slots":sorted(carried+leftover_slots,key=lambda s:(s["date"],s["meal_type"])),
            "dishes":[], "salads":[], "batch":spec, "supersedes":mp.menu_ref(current),
            "historical_slot_ids":[s["slot_id"] for s in carried if s["date"]<today or mp.slot_outcome(state,current,s)=="cooked"],
            "slot_owners":{s["slot_id"]:current.get("slot_owners",{}).get(s["slot_id"],current["menu_id"]) for s in carried}}
        successor["planning_scope"] = deepcopy(current.get("planning_scope") or (current.get("planner_selection") or {}).get("request") or {"dates": sorted({s["date"] for s in current["slots"]}), "portions": state["profile"]["meals"]["portions"]})
        by_key = {r["recipe_key"]:r for r in current["dishes"]+current["salads"]}
        successor["dishes"] = [deepcopy(by_key[key]) for key in dict.fromkeys(s["recipe_key"] for s in successor["slots"])]
        eligibility = bp.evaluate_plan(state, current, successor)
        if eligibility["status"] != "pass":
            return {"status":"needs_input", "reason":"batch arrangement does not satisfy current hard/strict constraints", "evaluation":eligibility}
        successor["schedule"] = [{"day":s["date"],"meal":by_key[s["recipe_key"]]["name"]+(" (rester)" if s.get("kind")=="leftover" else ""),
                                  "slot_id":s["slot_id"],"recipe_key":s["recipe_key"]} for s in successor["slots"]]
        prepared = {"status":"prepared", "source":mp.menu_ref(current), "state_digest":self._replan_state_digest(state),
            "as_of_date":today, "batch_spec":deepcopy(request["batch_spec"]), "successor":successor,
            "replaced_slot_ids":sorted(replaced), "locked_slot_ids":[], "planner_input":{},
            "shopping_comparison":mp.shopping_comparison(current,successor),
            "shopping_reasons":[{"slot_id":source["slot_id"],"reason":"source_scaled_once","prepared_portions":spec["prepared_portions"]}]+
                [{"slot_id":s["slot_id"],"source_slot_id":source["slot_id"],"reason":"leftover_zero_new_requirements","new_requirements":0} for s in leftover_slots],
            "confirmation_statement":bp.CONFIRMATION_STATEMENT}
        prepared["batch_digest"] = mp.digest(prepared)
        if len(canonical(prepared).encode())>MAX_MENU_BYTES or len(json.dumps({"ok":True,"result":{"batch_plan":prepared}},ensure_ascii=True).encode())>MAX_REQUEST-4096:
            raise HouseholdError("batch plan exceeds bounded response size")
        return prepared

    def _batch(self, request):
        if request["action"]=="batch_prepare":
            return {"batch_plan":self._prepare_batch(request)}
        supplied = request.get("batch_plan")
        if not isinstance(supplied,Mapping) or supplied.get("status")!="prepared" or supplied.get("batch_digest")!=mp.digest({k:v for k,v in supplied.items() if k!="batch_digest"}):
            raise HouseholdError("batch_apply requires the complete unchanged prepared batch_plan")
        confirmation = request.get("batch_confirmation")
        expected = {"batch_digest":supplied["batch_digest"],"statement":bp.CONFIRMATION_STATEMENT}
        if canonical(confirmation)!=canonical(expected):
            return {"status":"needs_input","reason":"clear current-user confirmation of the exact batch specification is required", "required_confirmation":expected}
        state = self.store.read()
        prior=state["menu_planning"]["applied"].get(supplied["batch_digest"])
        if prior is not None:
            return {"menu_ref":deepcopy(prior),"idempotent":True}
        fresh=self._prepare_batch({"menu_ref":supplied["source"],"as_of_date":supplied["as_of_date"],"batch_spec":supplied["batch_spec"]})
        if canonical(fresh)!=canonical(supplied):
            raise HouseholdError("batch source or specification is stale; prepare again")
        with self.store.locked() as state:
            if any(state.get(k) for k in ("pending_checkout","pending_cancellation","order_change")):
                raise HouseholdError("reconcile protected operations before batch apply")
            if self._household_today(state).isoformat()!=supplied["as_of_date"] or self._replan_state_digest(state)!=supplied["state_digest"]:
                raise HouseholdError("batch state/date changed before apply")
            commit=deepcopy(supplied); commit["replan_digest"]=supplied["batch_digest"]
            commit["successor"]["batch"]["confirmation"]=deepcopy(confirmation)
            return self._commit_successor(state,commit)

    def _commit_successor(self, state, supplied):
        planning = state["menu_planning"]
        if any(len(v) >= mp.MAX_PLANNING_MENUS for v in planning.values()):
            raise HouseholdError("planning history limit reached")
        current = state["menu"]
        successor = deepcopy(supplied["successor"])
        successor.update({"menu_id": "menu_" + secrets.token_hex(12), "revision": 1, "phase": "draft"})
        successor["digest"] = menu_digest(successor)
        planning["history"][mp.lock_key(current)] = deepcopy(current)
        for slot in current["slots"]:
            if slot["slot_id"] in supplied["replaced_slot_ids"] and slot.get("kind") != "leftover":
                owner = current.get("slot_owners", {}).get(slot["slot_id"], current["menu_id"])
                values = planning["retired"].setdefault(owner, [])
                if slot["recipe_key"] not in values:
                    values.append(slot["recipe_key"])
        owned = [s for s in successor["slots"] if s["slot_id"] not in successor["slot_owners"]]
        state["recipe_usage"][successor["menu_id"]] = {"week": successor["week"], "status": "planned",
            "recipe_keys": [s["recipe_key"] for s in owned if s.get("kind") != "leftover"], "slots": deepcopy(owned),
            "cooked_keys": [], "not_cooked_keys": [], "cooked_slot_ids": [], "not_cooked_slot_ids": [],
            "cooldown_overrides": deepcopy(supplied["planner_input"].get("cooldown_overrides", {})), "order_id": None}
        carried_locks = [s["slot_id"] for s in successor["slots"] if s["slot_id"] in set(planning["locks"].get(mp.lock_key(current), [])) | set(supplied["locked_slot_ids"])]
        planning["locks"][mp.lock_key(successor)] = carried_locks
        planning["applied"][supplied["replan_digest"]] = mp.menu_ref(successor)
        state["menu"] = successor
        return {"menu": deepcopy(successor), "shopping_comparison": deepcopy(supplied["shopping_comparison"])}

    def _materialize_menu(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise HouseholdError("menu must be an object")
        week = validate_week(value.get("week"))
        result: dict[str, Any] = {"week": week}
        profile_portions = self.store.read()["profile"]["meals"]["portions"]
        count = 0
        for collection in ("dishes", "salads"):
            values = value.get(collection, [])
            if not isinstance(values, list) or len(values) > 31:
                raise HouseholdError(f"menu {collection} must be a bounded list")
            materialized = []
            for raw in values:
                if not isinstance(raw, Mapping):
                    raise HouseholdError("menu recipes must be objects")
                reference = raw.get("recipe_ref")
                library_reference = raw.get("library_recipe_ref")
                if reference is not None and library_reference is not None:
                    raise RecipeLibraryError("menu recipe must use exactly one recipe reference type")
                if library_reference is not None:
                    checked = validate_library_recipe_ref(library_reference)
                    if checked["library_id"] not in self.recipe_libraries:
                        raise RecipeLibraryError("library_recipe_ref names an unconfigured recipe library")
                    if checked["library_id"] == "builtin":
                        stored = self.recipes.get(checked["recipe_id"], checked.get("version"))
                        if stored.get("status") != "active" or stored.get("revision_status", stored.get("status")) != "active":
                            raise HouseholdError("only active recipes can be added to a new menu")
                    else:
                        stored = self._external_library_get(checked)
                    for field in ("library_id", "is_favorite", "favorite_revision"):
                        stored.pop(field, None)
                    recipe = scale_recipe(stored, raw.get("portions") if raw.get("portions") is not None else profile_portions)
                    recipe["library_recipe_ref"] = deepcopy(stored["library_recipe_ref"])
                    recipe["recipe_key"] = library_recipe_key(recipe["library_recipe_ref"])
                elif isinstance(reference, Mapping):
                    stored = self.recipes.get(reference.get("id"), reference.get("revision"))
                    if stored.get("status") != "active" or stored.get("revision_status", stored.get("status")) != "active":
                        raise HouseholdError("only active recipes can be added to a new menu")
                    for field in ("library_id", "is_favorite", "favorite_revision"):
                        stored.pop(field, None)
                    recipe = scale_recipe(stored, raw.get("portions") if raw.get("portions") is not None else profile_portions)
                else:
                    candidate = deepcopy(dict(raw))
                    candidate.setdefault("portions", profile_portions)
                    if not isinstance(candidate.get("source"), Mapping) or not isinstance(candidate.get("rights"), Mapping):
                        raise HouseholdError("new menu recipes require explicit source, relationship and rights metadata")
                    recipe = scale_recipe(normalize_recipe(candidate), candidate["portions"])
                materialized.append(recipe)
                count += 1
            result[collection] = materialized
        if count < 1:
            raise HouseholdError("menu needs at least one complete recipe")
        schedule = value.get("schedule")
        if schedule is not None:
            if not isinstance(schedule, list) or len(schedule) > 31 or any(not isinstance(item, Mapping) for item in schedule):
                raise HouseholdError("menu schedule must be a bounded list of objects")
            result["schedule"] = deepcopy(schedule)
        if value.get("notes") is not None:
            if not isinstance(value["notes"], str) or len(value["notes"]) > 4_000:
                raise HouseholdError("menu notes are invalid")
            result["notes"] = value["notes"].strip()
        rendered_email = menu_email_html(result)
        if len(rendered_email.encode()) > MAX_EMAIL_HTML_BYTES:
            raise HouseholdError("menu recipes exceed the deliverable email size limit")
        if len(json.dumps({"ok": True, "result": {"html": rendered_email}}, ensure_ascii=True).encode()) > MAX_REQUEST - 4_096:
            raise HouseholdError("menu recipe email cannot fit the meal concierge response transport")
        if len(canonical(result).encode()) > MAX_MENU_BYTES:
            raise HouseholdError("menu is too large")
        response_probe = {"ok": True, "result": {"menu": result}}
        if len(json.dumps(response_probe, ensure_ascii=True).encode()) > MAX_REQUEST - 4_096:
            raise HouseholdError("menu cannot fit the meal concierge response transport")
        return result

    @staticmethod
    def _default_planner_dates(week: str, profile: Mapping[str, Any]) -> list[str]:
        match = re.fullmatch(r"(\d{4})-W(\d{2})", validate_week(week))
        monday = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        meals = profile.get("meals")
        if not isinstance(meals, Mapping):
            raise PlannerError("profile meals are invalid")
        count = meals.get("dinner_days")
        dishes = meals.get("dishes")
        batch_dishes = meals.get("batch_dishes")
        eat_days = meals.get("eat_days")
        weekdays = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 7:
            raise PlannerError("profile dinner_days must be an integer from one to seven")
        if dishes != count or batch_dishes != 0:
            raise PlannerError(
                "default deterministic planning requires one different dish per dinner day and batch_dishes=0; supply exact dates only for a current explicit count"
            )
        if not isinstance(eat_days, list) or len(eat_days) > 7:
            raise PlannerError("profile eat_days must be a bounded list")
        selected_values = {weekdays.get(str(value).casefold()) for value in eat_days}
        if None in selected_values or len(selected_values) < count:
            raise PlannerError("profile eat_days do not cover dinner_days")
        selected = sorted(selected_values)
        return [(monday + timedelta(days=offset)).isoformat() for offset in selected[:count]]

    def _effective_planner_request(
        self, value: Any, state: Mapping[str, Any], *, anchor_current_date: bool
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value).difference({
            "week", "dates", "portions", "candidates", "strict_targets",
            "cooldown_overrides", "alternatives", "as_of_date",
        }):
            raise PlannerError("planner input has unknown fields")
        week = validate_week(value.get("week"))
        supplied_as_of_date = value.get("as_of_date")
        if anchor_current_date:
            as_of_date = self._household_today(state).isoformat()
            if supplied_as_of_date is not None and supplied_as_of_date != as_of_date:
                raise PlannerError("planner as_of_date is not current in the household timezone")
        else:
            if not isinstance(supplied_as_of_date, str):
                raise PlannerError("planner as_of_date must be an ISO date")
            try:
                as_of_date = date.fromisoformat(supplied_as_of_date).isoformat()
            except ValueError as exc:
                raise PlannerError("planner as_of_date must be an ISO date") from exc
            if supplied_as_of_date != as_of_date:
                raise PlannerError("planner as_of_date must be a canonical ISO date")
        profile = state.get("profile")
        if not isinstance(profile, Mapping):
            raise PlannerError("household profile is invalid")
        meals = profile.get("meals")
        if not isinstance(meals, Mapping):
            raise PlannerError("profile meals are invalid")
        return {
            "week": week,
            "dates": deepcopy(value.get("dates")) if value.get("dates") is not None
            else self._default_planner_dates(week, profile),
            "portions": value.get("portions", meals.get("portions")),
            "candidates": deepcopy(value.get("candidates")),
            "strict_targets": deepcopy(value.get("strict_targets", [])),
            "cooldown_overrides": deepcopy(value.get("cooldown_overrides", {})),
            "alternatives": value.get("alternatives", 1),
            "as_of_date": as_of_date,
        }

    @staticmethod
    def _planner_reference(value: Any) -> tuple[dict[str, Any], str]:
        if not isinstance(value, Mapping) or set(value).difference(
            {"recipe_ref", "discovery_ref", "facts"}
        ):
            raise PlannerError("each planner candidate must contain one exact reference")
        recipe_ref = value.get("recipe_ref")
        discovery_ref = value.get("discovery_ref")
        if (recipe_ref is None) == (discovery_ref is None):
            raise PlannerError(
                "each planner candidate requires exactly one recipe_ref or discovery_ref"
            )
        if recipe_ref is not None:
            if (
                not isinstance(recipe_ref, Mapping) or set(recipe_ref) != {"id", "revision"}
                or not isinstance(recipe_ref.get("id"), str)
                or re.fullmatch(r"rec_[a-f0-9]{24}", recipe_ref["id"]) is None
                or isinstance(recipe_ref.get("revision"), bool)
                or not isinstance(recipe_ref.get("revision"), int)
                or recipe_ref["revision"] < 1
            ):
                raise PlannerError("planner recipe_ref must contain an exact id and revision")
            reference = {"recipe_ref": {"id": recipe_ref["id"], "revision": recipe_ref["revision"]}}
        else:
            if (
                not isinstance(discovery_ref, str)
                or re.fullmatch(
                    r"discovery:v1:[A-Za-z0-9_-]{16}:[A-Za-z0-9_-]{16,64}",
                    discovery_ref,
                ) is None
            ):
                raise PlannerError("planner discovery_ref must be bounded exact text")
            reference = {"discovery_ref": discovery_ref}
        return reference, canonical(reference)

    def _resolve_planner_candidates(
        self, request: Mapping[str, Any], state: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        history = state.get("recipe_usage")
        if not isinstance(history, Mapping) or len(history) > MAX_HISTORY_RECORDS:
            raise PlannerError(
                f"planner history exceeds {MAX_HISTORY_RECORDS} records"
            )
        raw_candidates = request.get("candidates")
        if not isinstance(raw_candidates, list) or not 1 <= len(raw_candidates) <= MAX_CANDIDATES:
            raise PlannerError(
                f"planner candidates must contain one to {MAX_CANDIDATES} entries"
            )
        resolved = []
        seen = set()
        for raw in raw_candidates:
            reference, reference_key = self._planner_reference(raw)
            if reference_key in seen:
                raise PlannerError("planner candidates contain a duplicate exact reference")
            seen.add(reference_key)
            if "recipe_ref" in reference:
                exact = reference["recipe_ref"]
                stored = self.recipes.get(exact["id"], exact["revision"])
                materialization_error = None
                if (
                    stored.get("status") != "active"
                    or stored.get("revision_status", stored.get("status")) != "active"
                ):
                    materialization_error = "only active built-in recipe revisions can be planned"
                key = stored["recipe_key"]
                recipe = normalize_recipe(stored)
            else:
                snapshot = self.recipes.resolve_discovery(reference["discovery_ref"])
                recipe = snapshot["recipe"]
                key = recipe_key(recipe)
                materialization_error = None
            try:
                scale_recipe(recipe, request.get("portions"))
            except RecipeError as exc:
                materialization_error = str(exc)
            supplied_facts = deepcopy(raw.get("facts", {})) if isinstance(raw, Mapping) else {}
            resolved.append({
                "reference": reference,
                "reference_key": reference_key,
                "recipe": recipe,
                "recipe_key": key,
                "dedupe_key": recipe_key(recipe),
                "content_digest": hashlib.sha256(canonical(recipe).encode()).hexdigest(),
                "usage": self._usage_summary(state, key, request["week"]),
                "facts": supplied_facts,
                "supplied_facts": supplied_facts,
                "materialization_error": materialization_error,
            })
        return resolved

    def _run_planner(
        self, request: Mapping[str, Any], resolved: list[Mapping[str, Any]],
        state: Mapping[str, Any],
    ) -> dict[str, Any]:
        history = state.get("recipe_usage")
        if not isinstance(history, Mapping) or len(history) > MAX_HISTORY_RECORDS:
            raise PlannerError(
                f"planner history exceeds {MAX_HISTORY_RECORDS} records"
            )
        feedback = pf.effective(state["planning_feedback"], request["as_of_date"])
        refreshed = []
        for candidate in resolved:
            item = deepcopy(dict(candidate))
            item["usage"] = self._usage_summary(
                state, item["recipe_key"], request["week"]
            )
            if item["recipe_key"] in feedback["signals"]:
                item["planning_feedback"] = {"weight": feedback["signals"][item["recipe_key"]],
                    "policy_version": pf.POLICY_VERSION, "events": [e for e in feedback["events"] if e["recipe_key"] == item["recipe_key"]]}
            refreshed.append(item)
        profile = state.get("profile")
        if not isinstance(profile, Mapping):
            raise PlannerError("household profile is invalid")
        return plan_week(
            request, profile=profile, candidates=refreshed, history=history, feedback=feedback
        )

    def _plan_menu(
        self, value: Any, *, state: Mapping[str, Any] | None = None,
        anchor_current_date: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        snapshot = deepcopy(dict(state)) if isinstance(state, Mapping) else self.store.read()
        request = self._effective_planner_request(
            value, snapshot, anchor_current_date=anchor_current_date
        )
        resolved = self._resolve_planner_candidates(request, snapshot)
        result = self._run_planner(request, resolved, snapshot)
        result["cooking_experiences"] = pf.experiences(snapshot["planning_feedback"], {r["recipe_key"] for r in resolved})[-36:]
        if len(json.dumps({"ok": True, "result": {"plan": result}}, ensure_ascii=True).encode()) > MAX_REQUEST - 4_096:
            raise PlannerError("planner result cannot fit the response transport")
        return result, resolved, request

    @staticmethod
    def _matching_planner_handoff(
        result: Mapping[str, Any], handoff: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        values = result.get("save_handoffs")
        if not isinstance(values, list):
            return None
        supplied_digest = handoff.get("selection_digest")
        return next((
            value for value in values
            if isinstance(value, Mapping)
            and value.get("selection_digest") == supplied_digest
            and canonical(value) == canonical(handoff)
        ), None)

    def _verify_planner_handoff(
        self, value: Any
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        if not isinstance(value, Mapping) or set(value) != {
            "planner_version", "input_digest", "selection_digest", "request", "selection"
        }:
            raise PlannerError("planner_handoff must be one complete server-returned handoff")
        if value.get("planner_version") != PLANNER_VERSION:
            raise PlannerError("planner_handoff uses an unsupported planner version")
        if any(
            re.fullmatch(r"[a-f0-9]{64}", str(value.get(field) or "")) is None
            for field in ("input_digest", "selection_digest")
        ):
            raise PlannerError("planner_handoff digests are invalid")
        result, resolved, request = self._plan_menu(
            value.get("request"), anchor_current_date=False
        )
        if result.get("status") != "planned" or self._matching_planner_handoff(result, value) is None:
            raise PlannerError("planner_handoff is stale, changed or fabricated; generate it again")
        return result, resolved, request

    def _materialize_planner_menu(
        self, handoff: Mapping[str, Any], resolved: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        selection = handoff["selection"]
        slots = selection.get("slots") if isinstance(selection, Mapping) else None
        if not isinstance(slots, list) or not slots:
            raise PlannerError("planner_handoff selection is invalid")
        by_reference = {item["reference_key"]: item for item in resolved}
        dishes = []
        schedule = []
        for slot in slots:
            if not isinstance(slot, Mapping):
                raise PlannerError("planner_handoff selection is invalid")
            candidate = by_reference.get(slot.get("reference_key"))
            if candidate is None or canonical(candidate["reference"]) != canonical(slot.get("reference")):
                raise PlannerError("planner_handoff selection reference is invalid")
            portions = slot.get("portions")
            if "recipe_ref" in candidate["reference"]:
                dishes.append({
                    "recipe_ref": deepcopy(candidate["reference"]["recipe_ref"]),
                    "portions": portions,
                })
            else:
                dishes.append(scale_recipe(candidate["recipe"], portions))
            schedule.append({
                "day": slot.get("date"),
                "meal": slot.get("name"),
                "portions": portions,
                "recipe_key": slot.get("recipe_key"),
                "reference": deepcopy(slot.get("reference")),
            })
        menu = self._materialize_menu({
            "week": handoff["request"]["week"],
            "dishes": dishes,
            "salads": [],
            "schedule": schedule,
        })
        menu["slots"] = [{
            "slot_id": "slot_" + mp.digest({"selection": handoff["selection_digest"], "date": slot["date"]})[:32],
            "date": slot["date"], "meal_type": "dinner", "recipe_key": slot["recipe_key"],
            "reference": deepcopy(slot["reference"]), "snapshot_digest": mp.digest(recipe),
        } for slot, recipe in zip(slots, menu["dishes"], strict=True)]
        menu["planner_selection"] = {
            "planner_version": handoff["planner_version"],
            "input_digest": handoff["input_digest"],
            "selection_digest": handoff["selection_digest"],
            "request": deepcopy(handoff["request"]),
            "selection": deepcopy(selection),
        }
        if len(canonical(menu).encode()) > MAX_MENU_BYTES:
            raise PlannerError("planned menu is too large")
        if len(json.dumps({"ok": True, "result": {"menu": menu}}, ensure_ascii=True).encode()) > MAX_REQUEST - 4_096:
            raise PlannerError("planned menu cannot fit the response transport")
        return menu

    @staticmethod
    def _abandon_predispatch(state: dict[str, Any], *, reason: str) -> None:
        pending = state.get("pending_checkout")
        if not pending:
            return
        if pending.get("status") != "awaiting_confirmation":
            raise HouseholdError("checkout is pending and may have been dispatched; reconcile it before changing the menu")
        occurrence = pending.get("occurrence")
        if occurrence and isinstance(state.get("occurrences", {}).get(occurrence), dict):
            state["occurrences"][occurrence]["status"] = "abandoned"
            state["occurrences"][occurrence]["reason"] = reason
        state["pending_checkout"] = None

    def _menu(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "get")
        if action == "assess":
            return {"assessment": assess_menu(self.store.read())}
        if action == "get":
            state = self.store.read()
            current = state.get("menu")
            return {"menu": deepcopy(current), "assessment": assess_menu(state), "feedback_targets": feedback_targets(current), "slot_replan_available": bool(current and current.get("slots")),
                    "batch_dependencies": bp.dependency_status(state, current) if current else [],
                    "locks": deepcopy(state["menu_planning"]["locks"].get(mp.lock_key(current), [])) if current else []}
        if action in {"lock", "replan_prepare", "replan_apply", "batch_prepare", "batch_apply"}:
            return self._replanning(request)
        if action == "plan":
            setup_gate = self._setup_gate(request)
            if setup_gate is not None:
                return setup_gate
            result, _resolved, _planner_request = self._plan_menu(
                request.get("planner_input")
            )
            return {"plan": result}
        if action == "clear":
            with self.product_plan_lock, self.store.locked() as state:
                current = state.get("menu")
                if isinstance(current, Mapping) and (current.get("menu_id") or current.get("revision") is not None):
                    supplied_menu_id = request.get("menu_id")
                    expected_revision = request.get("expected_revision")
                    if supplied_menu_id != current.get("menu_id"):
                        raise HouseholdError("menu_id does not match the current menu")
                    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision != current.get("revision"):
                        raise HouseholdError(f"menu revision conflict; current revision is {current.get('revision')}")
                self._abandon_predispatch(state, reason="menu cleared")
                if isinstance(current, Mapping):
                    mp.retire_planned_slots(state, current)
                    usage = state.setdefault("recipe_usage", {}).get(current.get("menu_id"))
                    if isinstance(usage, dict) and usage.get("status") == "planned":
                        usage["status"] = "cancelled"
                state["menu"] = None
                state["cart_plan"] = None
                return {"menu": None}
        if action == "save":
            setup_gate = self._setup_gate(request)
            if setup_gate is not None:
                return setup_gate
            baseline_menu = deepcopy(self.store.read().get("menu"))
            planner_handoff = request.get("planner_handoff")
            planner_context = None
            if planner_handoff is not None:
                if (
                    request.get("menu") is not None
                    or request.get("allow_repeat_keys") not in (None, [])
                    or request.get("override_reason") not in {None, ""}
                ):
                    raise PlannerError(
                        "planner save accepts planner_handoff without legacy menu or cooldown overrides"
                    )
                existing_planner = (
                    baseline_menu.get("planner_selection")
                    if isinstance(baseline_menu, Mapping) else None
                )
                if (
                    isinstance(existing_planner, Mapping)
                    and canonical(existing_planner) == canonical(planner_handoff)
                ):
                    return {"menu": baseline_menu, "idempotent": True}
                _planner_result, resolved, planner_request = self._verify_planner_handoff(
                    planner_handoff
                )
                menu = self._materialize_planner_menu(planner_handoff, resolved)
                planner_context = (deepcopy(planner_handoff), resolved, planner_request)
                override_map = dict(planner_request["cooldown_overrides"])
            else:
                menu = self._materialize_menu(request.get("menu"))
                repeat_keys = request.get("allow_repeat_keys", [])
                if not isinstance(repeat_keys, list) or len(repeat_keys) > 62 or not all(isinstance(key, str) and 1 <= len(key) <= MAX_LIBRARY_RECIPE_KEY for key in repeat_keys):
                    raise HouseholdError("allow_repeat_keys must be a list of recipe keys")
                override_reason = str(request.get("override_reason") or "").strip()
                if repeat_keys and not override_reason:
                    raise HouseholdError("a cooldown override reason is required")
                if len(override_reason) > 500:
                    raise HouseholdError("cooldown override reason is too long")
                override_map = {key: override_reason for key in repeat_keys}
            supplied_menu_id = str(request.get("menu_id") or "") or None
            expected_revision = request.get("expected_revision")
            def matched_override(key: str) -> str | None:
                aliases = library_recipe_key_aliases(key)
                return next((
                    reason for supplied_key, reason in override_map.items()
                    if aliases.intersection(library_recipe_key_aliases(supplied_key))
                ), None)

            digest = menu_digest(menu)
            keys = [recipe["recipe_key"] for collection in ("dishes", "salads") for recipe in menu[collection]]
            seen_keys: set[str] = set()
            duplicate_key = False
            for key in keys:
                aliases = library_recipe_key_aliases(key)
                if seen_keys.intersection(aliases):
                    duplicate_key = True
                    break
                seen_keys.update(aliases)
            if duplicate_key:
                raise HouseholdError("the same recipe cannot appear twice in one menu")
            with self.product_plan_lock, self.store.locked() as state:
                current = state.get("menu")
                if isinstance(current, Mapping) and current.get("digest") == digest:
                    return {"menu": deepcopy(current), "idempotent": True}
                if planner_context is not None:
                    original_handoff, resolved, planner_request = planner_context
                    current_result = self._run_planner(
                        planner_request, resolved, state
                    )
                    if (
                        current_result.get("status") != "planned"
                        or self._matching_planner_handoff(
                            current_result, original_handoff
                        ) is None
                    ):
                        raise PlannerError(
                            "planner_handoff became stale before save; generate it again"
                        )
                if supplied_menu_id:
                    if not isinstance(current, Mapping) or current.get("menu_id") != supplied_menu_id:
                        raise HouseholdError("menu_id does not match the current menu")
                    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or current.get("revision") != expected_revision:
                        raise HouseholdError(f"menu revision conflict; current revision is {current.get('revision')}")
                    if current.get("supersedes"):
                        raise HouseholdError("a successor preserves immutable lineage; use replan instead of revision edits")
                    current_usage = state.setdefault("recipe_usage", {}).get(supplied_menu_id)
                    if current.get("phase") == "ordered" or (isinstance(current_usage, Mapping) and current_usage.get("status") == "ordered"):
                        raise HouseholdError("an ordered menu is immutable; save a new menu instead")
                    if isinstance(current_usage, Mapping) and (
                        current_usage.get("cooked_keys")
                        or current_usage.get("not_cooked_keys")
                        or current_usage.get("cooldown_overrides")
                    ):
                        raise HouseholdError("a menu with explicit usage history is immutable; save a new menu instead")
                    menu_id = supplied_menu_id
                    revision = expected_revision + 1
                else:
                    if canonical(current) != canonical(baseline_menu):
                        raise HouseholdError("menu changed while saving; read it and try again")
                    menu_id = f"menu_{secrets.token_hex(12)}"
                    revision = 1
                self._abandon_predispatch(state, reason="menu replaced")
                blocked = []
                cooldown_state = deepcopy(state)
                if isinstance(current, Mapping):
                    mp.retire_planned_slots(cooldown_state, current)
                current_usage = state.setdefault("recipe_usage", {}).get(current.get("menu_id")) if isinstance(current, Mapping) else None
                for key in keys:
                    ignored_menu_id = (
                        current.get("menu_id")
                        if isinstance(current_usage, Mapping)
                        and current_usage.get("status") == "planned"
                        and self._matching_recipe_key(key, current_usage.get("cooked_keys")) is None
                        and not library_recipe_key_aliases(key).intersection(
                            (current_usage.get("cooldown_overrides") or {}).keys()
                        )
                        else None
                    )
                    summary = self._usage_summary(cooldown_state, key, menu["week"], ignore_menu_id=ignored_menu_id)
                    if not summary["eligible"] and matched_override(key) is None:
                        blocked.append({"recipe_key": key, "usage": summary})
                if blocked:
                    raise HouseholdError(f"recipe cooldown blocks this menu: {canonical(blocked)}")
                if isinstance(current, Mapping):
                    if current.get("menu_id") != menu_id:
                        mp.retire_planned_slots(state, current)
                    old_usage = state.setdefault("recipe_usage", {}).get(current.get("menu_id"))
                    if isinstance(old_usage, dict) and old_usage.get("status") == "planned" and current.get("menu_id") != menu_id:
                        old_usage["status"] = "cancelled"
                menu.update({"menu_id": menu_id, "revision": revision, "digest": digest, "phase": "draft"})
                state["menu"] = deepcopy(menu)
                state.setdefault("recipe_usage", {})[menu_id] = {
                    "week": menu["week"], "status": "planned", "recipe_keys": keys,
                    "cooked_keys": [], "not_cooked_keys": [],
                    "cooldown_overrides": {
                        key: matched_override(key)
                        for key in keys
                        if matched_override(key) is not None
                    },
                    "order_id": None, "updated_at": self._now().isoformat(),
                    "slots": deepcopy(menu.get("slots", [])), "cooked_slot_ids": [], "not_cooked_slot_ids": [],
                }
                return {"menu": deepcopy(menu)}
        raise HouseholdError("unknown menu action")

    def _schedule(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "show")
        with self.store.locked() as state:
            schedule = state["schedule"]
            if action == "show":
                return {"schedule": deepcopy(schedule)}
            if action == "disable":
                schedule["enabled"] = False
                schedule["auto_checkout"] = False
                return {"schedule": deepcopy(schedule), "remove_cron_job_id": schedule.get("cron_job_id")}
            if action == "set_cron_job":
                schedule["cron_job_id"] = request.get("cron_job_id")
                return {"schedule": deepcopy(schedule)}
            if action == "update":
                changes = request.get("changes")
                if not isinstance(changes, Mapping):
                    raise HouseholdError("schedule changes must be an object")
                allowed = {"enabled", "weekday", "time", "timezone", "mode", "delivery", "maximum_total", "auto_checkout"}
                if not set(changes).issubset(allowed):
                    raise HouseholdError("schedule contains unknown fields")
                if "delivery" in changes:
                    replacement = changes["delivery"]
                    if not isinstance(replacement, Mapping):
                        raise HouseholdError("schedule delivery preference is invalid")
                    replacement = deepcopy(dict(replacement))
                    replacement.setdefault("strategy", "cheapest")
                    changes = {**changes, "delivery": replacement}
                schedule.update(deepcopy(changes))
                validate_schedule(schedule, self.provider)
                if schedule.get("auto_checkout"):
                    schedule["mode"] = "auto_checkout"
                elif schedule.get("mode") == "auto_checkout":
                    schedule["mode"] = "cart_ready"
                return {
                    "schedule": deepcopy(schedule),
                    "cron": {
                        "name": f"{self.provider.upper()} ukesmeny ({state['household']})",
                        "weekday": schedule["weekday"],
                        "time": schedule["time"],
                        "timezone": schedule["timezone"],
                        "prompt": "Kjør den lagrede ukesplanen via den delte ukesmeny-skillen. Bruk forekomstnøkkel for denne lokale uken og stopp ved lagret modus.",
                    } if schedule.get("enabled") else None,
                }
        raise HouseholdError("unknown schedule action")

    def _catalog(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        kwargs = {
            "deadline": request.get("_deadline"),
            "allow_recovery": request.get("_allow_browser_recovery") is True,
        } if self.provider == "meny" else {}
        if action == "products":
            query_value = request.get("query", "")
            if not isinstance(query_value, str):
                raise HouseholdError("catalog query must be text")
            query = query_value.strip()
            return self.oda.call("product_search", {"queries": [query], "page": 1, "size": bounded_limit(request.get("limit"), default=5, maximum=MAX_PRODUCTS)}, **kwargs)
        if action == "recipes":
            query = request.get("query", "")
            if not isinstance(query, str):
                raise HouseholdError("catalog query must be text")
            return self.oda.call("recipe_search", {"query": query, "page": 1, "size": bounded_limit(request.get("limit"), default=5)}, **kwargs)
        if action == "usuals":
            return self.oda.call("likely_to_buy", {}, **kwargs)
        raise HouseholdError("unknown catalog action")

    def _product_binding(
        self, *, menu_ref: Any = None, planner_handoff: Any = None,
        require_saved_planner: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
        if (menu_ref is None) == (planner_handoff is None):
            raise HouseholdError("product preparation needs exactly one menu_ref or planner_handoff")
        if menu_ref is not None:
            if not isinstance(menu_ref, Mapping) or set(menu_ref) != {"menu_id", "revision", "digest"}:
                raise HouseholdError("menu_ref must be the exact current menu identity")
            state = self.store.read()
            menu = state.get("menu")
            if not isinstance(menu, Mapping) or canonical(self._cart_menu_ref(menu)) != canonical(menu_ref):
                raise HouseholdError("menu_ref is stale or not the active menu")
            return (
                {"kind": "saved_menu", "menu_ref": deepcopy(dict(menu_ref))},
                deepcopy(dict(menu)), deepcopy(dict(menu_ref)),
            )
        _result, resolved, _request = self._verify_planner_handoff(planner_handoff)
        menu = self._materialize_planner_menu(planner_handoff, resolved)
        saved_ref = None
        if require_saved_planner:
            current = self.store.read().get("menu")
            current_selection = current.get("planner_selection") if isinstance(current, Mapping) else None
            if not isinstance(current, Mapping) or canonical(current_selection) != canonical(planner_handoff):
                raise HouseholdError("save this exact planner selection before applying its product plan")
            saved_ref = self._cart_menu_ref(current)
        return (
            {"kind": "planner_selection", "planner_handoff": deepcopy(dict(planner_handoff))},
            menu, saved_ref,
        )

    def _product_observations(
        self, menu: Mapping[str, Any], *, deadline: float | None,
        search_cache: dict[str, dict[str, Any]] | None = None,
        ingredient_decisions: Any = None,
    ) -> dict[str, dict[str, Any]]:
        requirements, _unresolved = exact_menu_requirements(mp.shopping_menu(menu), ingredient_decisions=ingredient_decisions)
        observations = {}
        cache = search_cache if search_cache is not None else {}
        for requirement in requirements:
            query = requirement["identity"]
            if query in cache:
                observations[requirement["requirement_id"]] = deepcopy(cache[query])
                continue
            kwargs = {
                "deadline": deadline,
                "allow_recovery": False,
            } if self.provider == "meny" else {}
            observation = self.oda.call(
                "product_search",
                {"queries": [query], "page": 1, "size": MAX_CANDIDATES_PER_REQUIREMENT},
                **kwargs,
            )
            if not isinstance(observation, Mapping) or observation.get("provider") != self.provider:
                raise HouseholdError("provider product search is not normalized")
            observed_query = observation.get("query")
            if observed_query not in {None, query}:
                raise HouseholdError("provider product search query changed")
            normalized = deepcopy(dict(observation))
            normalized["query"] = query
            scope = normalized.get("scope")
            if (not isinstance(scope, Mapping) or scope.get("semantics") != "bounded_relevance_ranked"
                or scope.get("kind") != "provider_search"
                or type(scope.get("page")) is not int or scope["page"] != 1
                or type(scope.get("requested_size")) is not int
                or scope["requested_size"] != MAX_CANDIDATES_PER_REQUIREMENT):
                raise HouseholdError("provider product search scope changed")
            products = normalized.get("products")
            returned = scope.get("returned")
            if (
                not isinstance(products, list)
                or len(products) > MAX_CANDIDATES_PER_REQUIREMENT
                or isinstance(returned, bool)
                or not isinstance(returned, int)
                or returned != len(products)
            ):
                raise HouseholdError("provider product search exceeded its bounded candidate scope")
            normalized["scope"] = {
                **deepcopy(dict(scope)),
                "page": 1,
                "requested_size": MAX_CANDIDATES_PER_REQUIREMENT,
            }
            cache[query] = normalized
            observations[requirement["requirement_id"]] = deepcopy(normalized)
        return observations

    @staticmethod
    def _plan_approvals(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
        values = []
        for requirement in plan.get("requirements", []):
            approval = requirement.get("candidate_approval") if isinstance(requirement, Mapping) else None
            if isinstance(approval, Mapping):
                values.append({
                    key: deepcopy(approval[key])
                    for key in ("requirement_id", "candidate_refs", "max_excess")
                    if key in approval
                })
        return values

    @staticmethod
    def _cart_amount_ore(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return None
        ore = amount * 100
        if not amount.is_finite() or amount < 0 or ore != ore.to_integral_value():
            return None
        integer = int(ore)
        return integer if integer <= 100_000_000 else None

    def _verified_cart_product_amounts(
        self, plan: Mapping[str, Any], cart: Mapping[str, Any],
    ) -> str:
        # The current MENY cart fixture does not establish a semantic
        # product-line-total node. Its cart prices remain presentation only.
        if self.provider == "meny":
            return "unavailable_after_cart_write"
        if not isinstance(cart, Mapping):
            return "unavailable_after_cart_write"
        expected: dict[str, dict[str, int]] = {}
        for requirement in plan.get("requirements", []):
            selection = requirement.get("selection") if isinstance(requirement, Mapping) else None
            if not isinstance(selection, Mapping) or not isinstance(selection.get("products"), list):
                return "unavailable_after_cart_write"
            for product in selection["products"]:
                if not isinstance(product, Mapping):
                    return "unavailable_after_cart_write"
                try:
                    product_id = self._product_id(product.get("product_ref"))
                except HouseholdError:
                    return "unavailable_after_cart_write"
                quantity = product.get("quantity")
                payable = product.get("total_payable_ore")
                if (
                    isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1
                    or isinstance(payable, bool) or not isinstance(payable, int) or payable < 0
                ):
                    return "unavailable_after_cart_write"
                current = expected.setdefault(product_id, {"quantity": 0, "total_payable_ore": 0})
                current["quantity"] += quantity
                current["total_payable_ore"] += payable
        try:
            summary = cart_summary(cart)
        except HouseholdError:
            return "unavailable_after_cart_write"
        lines = {}
        for line in summary.get("items", []):
            if not isinstance(line, Mapping):
                return "unavailable_after_cart_write"
            try:
                product_id = self._product_id(line.get("product_id"))
            except HouseholdError:
                return "unavailable_after_cart_write"
            if product_id in lines:
                return "unavailable_after_cart_write"
            lines[product_id] = line
        for product_id, prepared in expected.items():
            line = lines.get(product_id)
            if not isinstance(line, Mapping):
                return "unavailable_after_cart_write"
            live_quantity = line.get("quantity")
            line_ore = self._cart_amount_ore(line.get("price"))
            if (
                isinstance(live_quantity, bool) or not isinstance(live_quantity, int)
                or live_quantity < 1 or line_ore is None
            ):
                return "unavailable_after_cart_write"
            if line_ore * prepared["quantity"] != prepared["total_payable_ore"] * live_quantity:
                return "changed_after_cart_write"
        return "unchanged"

    def _prepare_products(
        self, *, binding: Mapping[str, Any], menu: Mapping[str, Any],
        candidate_approvals: Any, deadline: float | None,
        ingredient_decisions: Any = None, budget_ore: int | None = None, price_mode: str = "exact",
    ) -> dict[str, Any]:
        observations = self._product_observations(menu, deadline=deadline, ingredient_decisions=ingredient_decisions)
        profile = self.store.read().get("profile")
        diet = profile.get("diet") if isinstance(profile, Mapping) else None
        hard_constraints = {
            key: deepcopy(diet.get(key, []))
            for key in ("allergies_or_sensitivities", "avoid")
            if isinstance(diet, Mapping) and diet.get(key)
        }
        return build_product_plan(
            provider=self.provider,
            binding=binding,
            menu=mp.shopping_menu(menu),
            observations=observations,
            candidate_approvals=candidate_approvals,
            hard_product_constraints=hard_constraints,
            ingredient_decisions=ingredient_decisions, budget_ore=budget_ore, price_mode=price_mode,
        )

    def _compare_menu_costs(self, request: Mapping[str, Any]) -> dict[str, Any]:
        value = request.get("planner_input")
        if not isinstance(value, Mapping):
            raise PlannerError("lowest_cost requires one canonical planner_input")
        # Explicit comparison defaults to three; the normal planner stays at one.
        value = {**value, "alternatives": value.get("alternatives", 3)}
        result, resolved, _ = self._plan_menu(value)
        comparison = {
            "mode": "lowest_cost", "status": "unavailable", "comparison_claim": None,
            "planner_version": result["planner_version"], "input_digest": result["input_digest"],
            "work_limits": {"maximum_alternatives": 3, "maximum_unique_requirements": MAX_REQUIREMENTS,
                            "maximum_unique_searches": MAX_REQUIREMENTS,
                            "maximum_candidates_per_search": MAX_CANDIDATES_PER_REQUIREMENT,
                            "maximum_product_plans": 3},
            "alternatives": [], "unavailable": [],
        }
        if result.get("status") != "planned":
            comparison["unavailable"] = deepcopy(result.get("issues", []))
            return {"plan": result, "cost_comparison": comparison}
        menus = [self._materialize_planner_menu(h, resolved) for h in result["save_handoffs"]]
        requirement_error = None
        try:
            requirements = [exact_menu_requirements(menu)[0] for menu in menus]
        except HouseholdError as exc:
            requirements = []
            requirement_error = str(exc)
        ids = {r["requirement_id"] for rows in requirements for r in rows}
        queries = {r["identity"] for rows in requirements for r in rows}
        if requirement_error or len(ids) > MAX_REQUIREMENTS or len(queries) > MAX_REQUIREMENTS:
            comparison["alternatives"] = [{"original_rank": rank, "selection_digest": h["selection_digest"],
                "non_price_selection": deepcopy(h["selection"]), "save_handoff": deepcopy(h),
                "product_plan": None, "cost_status": "comparison_work_budget_exceeded"}
                for rank, h in enumerate(result["save_handoffs"], 1)]
            comparison["selected_handoff"] = deepcopy(result["save_handoff"])
            comparison["unavailable"] = [{"reason": "comparison_work_budget_exceeded", "detail": requirement_error}]
            return {"plan": result, "cost_comparison": comparison}
        approvals = normalize_approvals(request.get("candidate_approvals"), ids)
        profile = self.store.read()["profile"]
        diet = profile.get("diet", {})
        hard = {key: deepcopy(diet[key]) for key in ("allergies_or_sensitivities", "avoid") if diet.get(key)}
        cache = {}
        # All alternatives share one canonical query per ingredient identity. Search
        # dispatch order is independent of planner rank and caller candidate order.
        for query in sorted(queries):
            synthetic = {"dishes": [{"shopping_requirements": [{"item": query, "unit": "g", "quantity": 1, "scalable": True}]}], "salads": []}
            try:
                self._product_observations(synthetic, deadline=request.get("_deadline"), search_cache=cache)
            except HouseholdError:
                comparison["unavailable"].append({"search": query, "reason": "provider_search_unavailable_or_scope_changed"})
        for rank, (handoff, menu, rows) in enumerate(zip(result["save_handoffs"], menus, requirements, strict=True), 1):
            observations = {r["requirement_id"]: cache[r["identity"]] for r in rows if r["identity"] in cache}
            selected_approvals = [{key: value for key, value in approvals[r["requirement_id"]].items() if key != "source"}
                                  for r in rows if r["requirement_id"] in approvals]
            product_plan = build_product_plan(
                provider=self.provider, binding={"kind": "planner_selection", "planner_handoff": handoff},
                menu=menu, observations=observations, candidate_approvals=selected_approvals, hard_product_constraints=hard,
            )
            comparison["alternatives"].append({
                "original_rank": rank, "selection_digest": handoff["selection_digest"],
                "non_price_selection": deepcopy(handoff["selection"]), "save_handoff": deepcopy(handoff),
                "product_plan": product_plan,
            })
            if product_plan["status"] != "prepared":
                comparison["unavailable"].append({"selection_digest": handoff["selection_digest"],
                                                   "requirements": product_plan["unresolved_requirements"]})
        alternatives = comparison["alternatives"]
        scopes = {canonical(a["product_plan"]["scope"]) for a in alternatives}
        if len(scopes) != 1:
            comparison["unavailable"].append({"reason": "inconsistent_candidate_scope"})
        if not comparison["unavailable"]:
            def cost_key(alternative):
                totals = alternative["product_plan"]["totals"]
                excess = totals["excess_score"]
                return (totals["total_payable_ore"], Fraction(excess["numerator"], excess["denominator"]),
                        totals["package_count"], alternative["original_rank"], alternative["selection_digest"])
            alternatives.sort(key=cost_key)
            comparison["status"] = "compared"
            comparison["comparison_claim"] = f"lowest verified product cost among these {len(alternatives)} exact menu alternatives and their declared provider candidate scopes"
        comparison["selected_handoff"] = deepcopy(alternatives[0]["save_handoff"])
        # Timestamps/display never carry comparison or apply authority.
        comparison["fact_digest"] = hashlib.sha256(canonical({
            "input_digest": result["input_digest"], "status": comparison["status"],
            "alternatives": [{"selection_digest": a["selection_digest"],
                              "product_plan_digest": a["product_plan"]["product_plan_digest"]} for a in alternatives],
        }).encode()).hexdigest()
        if len(json.dumps({"ok": True, "result": comparison}, ensure_ascii=True).encode()) > MAX_REQUEST - 4096:
            raise PlannerError("cost comparison cannot fit the response transport; reduce candidate scope")
        return {"cost_comparison": comparison}

    def _products(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "prepare")
        deadline = request.get("_deadline")
        if action == "lowest_cost":
            return self._compare_menu_costs(request)
        if action == "prepare":
            binding, menu, _saved_ref = self._product_binding(
                menu_ref=request.get("menu_ref"),
                planner_handoff=request.get("planner_handoff"),
            )
            plan = self._prepare_products(
                binding=binding,
                menu=menu,
                candidate_approvals=request.get("candidate_approvals"),
                ingredient_decisions=request.get("ingredient_decisions"), budget_ore=request.get("budget_ore"), price_mode=request.get("price_mode") or "exact",
                deadline=deadline,
            )
            result = {"product_plan": plan}
            previous = request.get("previous_product_plan")
            if previous is not None:
                previous = validate_product_plan(previous, previous.get("product_plan_digest") if isinstance(previous, Mapping) else None)
                old_binding = previous["binding"]
                same_selection = (
                    old_binding.get("kind") == "planner_selection"
                    and canonical(old_binding.get("planner_handoff")) == canonical(menu.get("planner_selection"))
                )
                if not same_selection and canonical(old_binding) != canonical(binding):
                    raise HouseholdError("previous product plan does not bind this exact menu selection")
                old_facts = {k: v for k, v in previous.items() if k != "binding"}
                new_facts = {k: v for k, v in plan.items() if k != "binding"}
                result["observation_drift"] = {
                    "status": "unchanged" if product_plan_digest(old_facts) == product_plan_digest(new_facts) else "changed",
                    "previous_product_plan_digest": previous["product_plan_digest"],
                    "current_product_plan_digest": plan["product_plan_digest"],
                }
            return result
        if action == "apply":
            if request.get("cart_change_requested") is not True:
                return {
                    "applied": False,
                    "reason": "a clear current user request to change the cart is required",
                }
            supplied = validate_product_plan(
                request.get("product_plan"), request.get("product_plan_digest")
            )
            binding = supplied.get("binding")
            if not isinstance(binding, Mapping):
                raise HouseholdError("prepared product plan binding is invalid")
            if binding.get("kind") == "saved_menu":
                fresh_binding, menu, expected_menu_ref = self._product_binding(
                    menu_ref=binding.get("menu_ref")
                )
            elif binding.get("kind") == "planner_selection":
                fresh_binding, menu, expected_menu_ref = self._product_binding(
                    planner_handoff=binding.get("planner_handoff"),
                    require_saved_planner=True,
                )
            else:
                raise HouseholdError("prepared product plan binding is invalid")
            fresh = self._prepare_products(
                binding=fresh_binding,
                menu=menu,
                candidate_approvals=self._plan_approvals(supplied),
                ingredient_decisions=supplied.get("ingredient_decisions"), budget_ore=supplied.get("budget_ore"), price_mode=supplied.get("price_mode") or "exact",
                deadline=deadline,
            )
            if fresh.get("product_plan_digest") != supplied.get("product_plan_digest"):
                return {
                    "applied": False,
                    "status": "needs_input",
                    "reason": "menu, candidate, availability, eligibility, offer or price facts changed",
                    "fresh_product_plan": fresh,
                }

            def final_product_prewrite_check() -> dict[str, Any]:
                try:
                    final = self._prepare_products(
                        binding=fresh_binding,
                        menu=menu,
                        candidate_approvals=self._plan_approvals(supplied),
                ingredient_decisions=supplied.get("ingredient_decisions"), budget_ore=supplied.get("budget_ore"), price_mode=supplied.get("price_mode") or "exact",
                        deadline=deadline,
                    )
                except HouseholdError:
                    return {
                        "ok": False,
                        "reason": "product facts became unavailable immediately before cart sync",
                        "fresh_product_plan": None,
                    }
                return {
                    "ok": final.get("product_plan_digest") == supplied.get("product_plan_digest"),
                    "reason": "product facts changed immediately before cart sync",
                    "fresh_product_plan": final,
                }

            if not prepared_cart_requirements(supplied):
                cart_result = self._cart_sync({"requirements": [], "_expected_menu_ref": expected_menu_ref, "_allow_empty_requirements": True}, deadline)
                if not cart_result.get("synced"):
                    return {"applied": False, **cart_result}
                summary = cart_summary(cart_result["cart"])
                live, names = self._cart_lines(summary)
                if live:
                    with self.store.locked() as state:
                        self._set_cart_needs_input(state["cart_plan"], live, names)
                        question = self._cart_question(state["cart_plan"], summary, reason="menu_fully_covered_review_existing_cart")
                    return {"applied": False, "nothing_to_buy_for_menu": True, **question}
                with self.store.locked() as state:
                    if self._cart_menu_ref(state.get("menu")) != expected_menu_ref:
                        raise HouseholdError("menu changed before recording pantry coverage")
                    state["product_plan_completion"] = {"menu_ref": deepcopy(expected_menu_ref), "nothing_to_buy": True, "product_plan_digest": supplied["product_plan_digest"]}
                return {"applied": True, "cart_changed": False, "nothing_to_buy": True, "product_plan": supplied}
            cart_result = self._cart_sync({
                "requirements": prepared_cart_requirements(supplied),
                "start_as_extra_product_ids": [],
                "_expected_menu_ref": expected_menu_ref,
                "_before_cart_write": final_product_prewrite_check,
            }, deadline)
            if cart_result.get("synced") is not True:
                return {
                    "applied": False,
                    **({"status": "needs_input"} if cart_result.get("product_plan_stale") else {}),
                    "reason": "cart reconciliation required",
                    **cart_result,
                }
            with self.store.locked() as state:
                if canonical(self._cart_menu_ref(state.get("menu"))) != canonical(expected_menu_ref):
                    raise HouseholdError("menu changed before recording the applied product plan")
                state.pop("product_plan_completion", None)
                state["cart_plan"]["product_plan_digest"] = supplied["product_plan_digest"]
                state["cart_plan"]["product_plan_summary"] = {
                    key: deepcopy(supplied.get(key)) for key in ("totals", "cost_status", "budget_status", "budget_ore", "ingredient_decisions")
                }
            price_verification = self._verified_cart_product_amounts(
                supplied, cart_result.get("cart")
            )
            postwrite_plan = None
            try:
                postwrite_plan = self._prepare_products(
                    binding=fresh_binding,
                    menu=menu,
                    candidate_approvals=self._plan_approvals(supplied),
                ingredient_decisions=supplied.get("ingredient_decisions"), budget_ore=supplied.get("budget_ore"), price_mode=supplied.get("price_mode") or "exact",
                    deadline=deadline,
                )
                if postwrite_plan.get("product_plan_digest") != supplied.get("product_plan_digest"):
                    price_verification = "changed_after_cart_write"
            except HouseholdError:
                if price_verification == "unchanged":
                    price_verification = "unavailable_after_cart_write"
            return {
                "applied": True,
                "cart": cart_result,
                "price_verification": price_verification,
                "fresh_product_plan": postwrite_plan if price_verification != "unchanged" else None,
                "price_locked": False,
                "final_price_authority": "provider checkout summary",
            }
        raise HouseholdError("unknown products action")

    @staticmethod
    def _cart_menu_ref(menu: Any) -> dict[str, Any] | None:
        if not isinstance(menu, Mapping):
            return None
        return {
            "menu_id": menu.get("menu_id"),
            "revision": menu.get("revision"),
            "digest": menu.get("digest"),
        }

    def _cart_lines(self, summary: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, str]]:
        quantities: dict[str, int] = {}
        names: dict[str, str] = {}
        for item in summary.get("items", []):
            if not isinstance(item, Mapping):
                raise HouseholdError("provider cart item is invalid")
            product_id = self._product_id(item.get("product_id"))
            quantity = item.get("quantity")
            name = str(item.get("name") or "").strip()
            if product_id in quantities or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1 or not name:
                raise HouseholdError("provider cart product identity is ambiguous")
            quantities[product_id] = quantity
            names[product_id] = name
        return quantities, names

    @staticmethod
    def _cart_digest(quantities: Mapping[str, int]) -> str:
        return hashlib.sha256(canonical(dict(sorted(quantities.items()))).encode()).hexdigest()

    def _cart_requirements(self, value: Any, *, allow_empty: bool = False) -> tuple[dict[str, int], dict[str, str]]:
        if allow_empty and value == []:
            return {}, {}
        if not isinstance(value, list) or not value:
            raise HouseholdError("cart sync needs one or more exact product requirements")
        quantities: dict[str, int] = {}
        names: dict[str, str] = {}
        for item in value:
            if not isinstance(item, Mapping):
                raise HouseholdError("cart requirements must be objects")
            product_id = self._product_id(item.get("product_id"))
            name = str(item.get("product_name") or "").strip()
            quantity = item.get("quantity")
            if not name or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise HouseholdError("cart requirements need exact product_id, name and positive integer quantity")
            if product_id in names and names[product_id] != name:
                raise HouseholdError("one product_id cannot have conflicting requirement names")
            names[product_id] = name
            quantities[product_id] = quantities.get(product_id, 0) + quantity
            if quantities[product_id] > 1_000_000:
                raise HouseholdError("cart requirement quantity is too large")
        return quantities, names

    @staticmethod
    def _cart_target(plan: Mapping[str, Any]) -> dict[str, int]:
        baseline = plan["baseline_quantities"]
        requirements = plan["required_quantities"]
        extra = set(plan["start_as_extra_product_ids"])
        target = {}
        for product_id in set(baseline) | set(requirements):
            quantity = (
                baseline.get(product_id, 0) + requirements.get(product_id, 0)
                if product_id in extra
                else max(baseline.get(product_id, 0), requirements.get(product_id, 0))
            )
            if quantity:
                target[product_id] = quantity
        return target

    @staticmethod
    def _meny_cart_batches(operations: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        batches: list[list[dict[str, Any]]] = []
        batch: list[dict[str, Any]] = []
        remaining_capacity = MAX_CART_CLICKS
        for operation in operations:
            remaining_quantity = abs(operation["quantity"])
            direction = 1 if operation["quantity"] > 0 else -1
            while remaining_quantity:
                amount = min(remaining_quantity, remaining_capacity)
                batch.append({**operation, "quantity": direction * amount})
                remaining_quantity -= amount
                remaining_capacity -= amount
                if remaining_capacity == 0:
                    batches.append(batch)
                    batch = []
                    remaining_capacity = MAX_CART_CLICKS
        if batch:
            batches.append(batch)
        return batches

    def _apply_meny_cart_batches(
        self, operations: list[dict[str, Any]], acknowledged: dict[str, int],
        *, deadline: float | None,
    ) -> dict[str, int]:
        for batch in self._meny_cart_batches(operations):
            before = self.oda.call("get_cart", {}, deadline=deadline)
            before_live, _before_names = self._cart_lines(cart_summary(before))
            if before_live != acknowledged:
                raise HouseholdError("MENY cart changed between bounded batches")
            changed_cart = self.oda.call(
                "manipulate_cart", {"operations": batch}, deadline=deadline
            )
            for operation in batch:
                product_id = str(operation["productId"])
                quantity = acknowledged.get(product_id, 0) + operation["quantity"]
                if quantity > 0:
                    acknowledged[product_id] = quantity
                else:
                    acknowledged.pop(product_id, None)
            changed_live, _changed_names = self._cart_lines(cart_summary(changed_cart))
            if changed_live != acknowledged:
                raise HouseholdError("MENY cart changed between bounded batches")
        return acknowledged

    def _cart_plan_view(self, plan: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
        live, live_names = self._cart_lines(summary)
        requirements = plan["required_quantities"]
        names = {**plan["product_names"], **live_names}
        approved = plan.get("approved_cart_digest") == self._cart_digest(live)
        items = []
        for product_id in sorted(set(live) | set(requirements) | set(plan["baseline_quantities"])):
            current = live.get(product_id, 0)
            required = requirements.get(product_id, 0)
            items.append({
                "product_id": product_id,
                "name": names.get(product_id, product_id),
                "start_quantity": plan["baseline_quantities"].get(product_id, 0),
                "required_quantity": required,
                "confirmed_added_quantity": plan["added_quantities"].get(product_id, 0),
                "live_quantity": current,
                "extra_quantity": max(current - required, 0),
                "missing_quantity": max(required - current, 0),
                "unresolved_start_quantity": bool(plan["baseline_quantities"].get(product_id, 0) and not approved),
            })
        return {
            "provider": plan["provider"],
            "menu_ref": deepcopy(plan["menu_ref"]),
            "status": plan["status"],
            "cart_digest": self._cart_digest(live),
            "approved": approved,
            "items": items,
        }

    def _new_cart_plan(
        self,
        menu_ref: Mapping[str, Any],
        live: Mapping[str, int],
        names: Mapping[str, str],
        requirements: Mapping[str, int],
        requirement_names: Mapping[str, str],
        start_as_extra: set[str],
        previous: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        retained_added = {}
        baseline = dict(live)
        status = "active"
        if isinstance(previous, Mapping) and previous.get("provider") == self.provider:
            for product_id, quantity in previous.get("added_quantities", {}).items():
                retained = min(quantity, live.get(product_id, 0))
                if retained:
                    retained_added[product_id] = retained
                    baseline[product_id] = live.get(product_id, 0) - retained
                    if baseline[product_id] == 0:
                        baseline.pop(product_id)
            status = "needs_input"
        digest = self._cart_digest(live)
        return {
            "provider": self.provider,
            "menu_ref": deepcopy(dict(menu_ref)),
            "status": status,
            "baseline_quantities": baseline,
            "required_quantities": dict(requirements),
            "added_quantities": retained_added,
            "start_as_extra_product_ids": sorted(start_as_extra),
            "product_names": {**dict(names), **dict(requirement_names)},
            "last_synced_quantities": dict(live),
            "last_synced_digest": digest,
            "approved_cart_digest": None,
            "pending_cart_digest": digest if status == "needs_input" else None,
            "updated_at": self._now().isoformat(),
        }

    def _cart_question(self, plan: Mapping[str, Any], summary: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
        return {
            "cart_reconciliation_required": True,
            "reason": reason,
            "default_suggestion": "keep_current",
            "cart_plan": self._cart_plan_view(plan, summary),
            "next": (
                "Ask one combined question for this exact cart digest. Suggest keeping the current cart, "
                "but require an explicit answer. The owner may name exact exclusions, restore missing menu products, "
                "or explicitly accept the current missing quantities."
            ),
        }

    def _set_cart_needs_input(self, plan: dict[str, Any], live: Mapping[str, int], names: Mapping[str, str]) -> None:
        digest = self._cart_digest(live)
        plan["status"] = "needs_input"
        plan["last_synced_quantities"] = dict(live)
        plan["last_synced_digest"] = digest
        plan["pending_cart_digest"] = digest
        plan["approved_cart_digest"] = None
        plan["product_names"].update(names)
        plan["updated_at"] = self._now().isoformat()

    @staticmethod
    def _retain_acknowledged_cart_additions(
        plan: dict[str, Any], before: Mapping[str, int],
        acknowledged: Mapping[str, int], live: Mapping[str, int],
    ) -> None:
        retained = {
            product_id: min(quantity, live.get(product_id, 0))
            for product_id, quantity in plan["added_quantities"].items()
            if min(quantity, live.get(product_id, 0)) > 0
        }
        for product_id in set(before) | set(acknowledged):
            confirmed = acknowledged.get(product_id, 0) - before.get(product_id, 0)
            if confirmed > 0:
                retained[product_id] = min(
                    retained.get(product_id, 0) + confirmed,
                    live.get(product_id, 0),
                )
        plan["added_quantities"] = retained

    def _cart_sync(self, request: Mapping[str, Any], deadline: float | None) -> dict[str, Any]:
        expected_menu_ref = self._require_cart_menu(request)
        with self.store.locked() as state:
            state.pop("product_plan_completion", None)
        requirements, requirement_names = self._cart_requirements(request.get("requirements"), allow_empty=request.get("_allow_empty_requirements") is True)
        extra_values = request.get("start_as_extra_product_ids") or []
        if not isinstance(extra_values, list):
            raise HouseholdError("start_as_extra_product_ids must be a list")
        start_as_extra = {self._product_id(value) for value in extra_values}
        first_cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
        first_summary = cart_summary(first_cart)
        first_live, first_names = self._cart_lines(first_summary)
        if not start_as_extra.issubset(first_live):
            raise HouseholdError("starting quantities can be extra only for exact products already in the cart")
        approved_idempotent = False
        with self.store.locked() as state:
            if state.get("pending_checkout") or state.get("order_change"):
                raise HouseholdError("finish the pending checkout or order change before syncing a menu cart")
            menu_ref = self._cart_menu_ref(state.get("menu"))
            if menu_ref is None:
                raise HouseholdError("save the active menu before syncing its cart")
            if canonical(expected_menu_ref) != canonical(menu_ref):
                raise HouseholdError("prepared product plan menu binding is stale")
            current = deepcopy(state.get("cart_plan"))
            same_plan = (
                isinstance(current, Mapping)
                and current.get("provider") == self.provider
                and canonical(current.get("menu_ref")) == canonical(menu_ref)
            )
            if not same_plan:
                current = self._new_cart_plan(
                    menu_ref, first_live, first_names, requirements, requirement_names,
                    start_as_extra, previous=current if isinstance(current, Mapping) else None,
                )
                state["cart_plan"] = deepcopy(current)
            else:
                requirements_changed = (
                    canonical(current.get("required_quantities")) != canonical(requirements)
                    or set(current.get("start_as_extra_product_ids", [])) != start_as_extra
                )
                if requirements_changed:
                    current.pop("product_plan_digest", None)
                    current.pop("product_plan_summary", None)
                current["required_quantities"] = dict(requirements)
                current["start_as_extra_product_ids"] = sorted(start_as_extra)
                current["product_names"].update(requirement_names)
                first_digest = self._cart_digest(first_live)
                if not requirements_changed and current.get("approved_cart_digest") == first_digest:
                    current["status"] = "active"
                    current["pending_cart_digest"] = None
                    current["last_synced_quantities"] = dict(first_live)
                    current["last_synced_digest"] = first_digest
                    current["product_names"].update(first_names)
                    current["updated_at"] = self._now().isoformat()
                    approved_idempotent = True
                elif current.get("last_synced_digest") != first_digest:
                    self._set_cart_needs_input(current, first_live, first_names)
                elif requirements_changed:
                    current["approved_cart_digest"] = None
                    current.pop("product_plan_digest", None)
                    current.pop("product_plan_summary", None)
                state["cart_plan"] = deepcopy(current)
        if current["status"] == "needs_input":
            return {"synced": False, **self._cart_question(current, first_summary, reason="cart_or_menu_changed_before_sync")}
        if approved_idempotent:
            return {
                "synced": True,
                "idempotent": True,
                "applied_operations": [],
                "cart": first_cart,
                "cart_plan": self._cart_plan_view(current, first_summary),
            }
        target = self._cart_target(current)
        operations = []
        for product_id in sorted(target):
            missing = target[product_id] - first_live.get(product_id, 0)
            if missing > 0:
                operations.append({
                    "productId": int(product_id) if self.provider == "oda" else product_id,
                    "quantity": missing,
                })
        mutation_error = None
        acknowledged_live = dict(first_live)
        if operations:
            before_cart_write = request.get("_before_cart_write")
            if before_cart_write is not None:
                if not callable(before_cart_write):
                    raise HouseholdError("cart prewrite check is invalid")
                guard = before_cart_write()
                if not isinstance(guard, Mapping) or guard.get("ok") is not True:
                    return {
                        "synced": False,
                        "product_plan_stale": True,
                        "reason": (
                            str(guard.get("reason"))
                            if isinstance(guard, Mapping) and guard.get("reason")
                            else "product facts changed immediately before cart sync"
                        ),
                        "fresh_product_plan": (
                            deepcopy(guard.get("fresh_product_plan"))
                            if isinstance(guard, Mapping) else None
                        ),
                    }
            prewrite_cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
            prewrite_summary = cart_summary(prewrite_cart)
            prewrite_live, prewrite_names = self._cart_lines(prewrite_summary)
            if self._cart_digest(prewrite_live) != self._cart_digest(first_live):
                with self.store.locked() as state:
                    plan = state["cart_plan"]
                    self._set_cart_needs_input(plan, prewrite_live, prewrite_names)
                    current = deepcopy(plan)
                return {"synced": False, **self._cart_question(current, prewrite_summary, reason="cart_changed_immediately_before_sync")}
            if expected_menu_ref is not None:
                with self.store.locked() as state:
                    if canonical(self._cart_menu_ref(state.get("menu"))) != canonical(expected_menu_ref):
                        return {
                            "synced": False,
                            "menu_binding_stale": True,
                            "reason": "prepared product plan menu binding changed immediately before sync",
                        }
            try:
                if self.provider == "meny":
                    self._apply_meny_cart_batches(
                        operations, acknowledged_live, deadline=deadline
                    )
                else:
                    self.oda.call("manipulate_cart", {"operations": operations})
            except HouseholdError as exc:
                mutation_error = exc
        verified_cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
        verified_summary = cart_summary(verified_cart)
        verified_live, verified_names = self._cart_lines(verified_summary)
        expected = dict(first_live)
        for operation in operations:
            product_id = str(operation["productId"])
            expected[product_id] = expected.get(product_id, 0) + operation["quantity"]
        if mutation_error is not None or verified_live != expected:
            with self.store.locked() as state:
                plan = state["cart_plan"]
                self._retain_acknowledged_cart_additions(
                    plan, first_live, acknowledged_live, verified_live
                )
                self._set_cart_needs_input(plan, verified_live, verified_names)
                current = deepcopy(plan)
            return {
                "synced": False,
                **self._cart_question(
                    current,
                    verified_summary,
                    reason="cart_write_result_uncertain" if mutation_error is not None else "cart_changed_during_sync",
                ),
            }
        with self.store.locked() as state:
            plan = state.get("cart_plan")
            if not isinstance(plan, dict) or canonical(plan.get("menu_ref")) != canonical(current.get("menu_ref")):
                raise HouseholdError("cart plan changed while syncing")
            for operation in operations:
                product_id = str(operation["productId"])
                plan["added_quantities"][product_id] = plan["added_quantities"].get(product_id, 0) + operation["quantity"]
            plan["required_quantities"] = dict(requirements)
            plan["product_names"].update({**verified_names, **requirement_names})
            plan["last_synced_quantities"] = dict(verified_live)
            plan["last_synced_digest"] = self._cart_digest(verified_live)
            plan["approved_cart_digest"] = None
            plan["pending_cart_digest"] = None
            plan["status"] = "active"
            plan["updated_at"] = self._now().isoformat()
            current = deepcopy(plan)
        return {
            "synced": True,
            "idempotent": not operations,
            "applied_operations": operations,
            "cart": verified_cart,
            "cart_plan": self._cart_plan_view(current, verified_summary),
        }

    def _cart_reconcile(self, request: Mapping[str, Any], deadline: float | None) -> dict[str, Any]:
        expected_menu_ref = self._require_cart_menu(request)
        with self.store.locked() as state:
            state.pop("product_plan_completion", None)
        decision = request.get("decision")
        if decision not in {"keep_current", "restore_missing"}:
            raise HouseholdError("cart reconciliation decision must be keep_current or restore_missing")
        supplied_digest = request.get("cart_digest")
        if not isinstance(supplied_digest, str) or re.fullmatch(r"[a-f0-9]{64}", supplied_digest) is None:
            raise HouseholdError("cart reconciliation needs the exact returned cart_digest")
        excluded_values = request.get("exclude_product_ids") or []
        accepted_missing_values = request.get("accept_missing_product_ids") or []
        if not isinstance(excluded_values, list) or not isinstance(accepted_missing_values, list):
            raise HouseholdError("cart exclusions and accepted missing products must be lists")
        excluded = {self._product_id(value) for value in excluded_values}
        accepted_missing = {self._product_id(value) for value in accepted_missing_values}
        first_cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
        first_summary = cart_summary(first_cart)
        first_live, first_names = self._cart_lines(first_summary)
        first_digest = self._cart_digest(first_live)
        with self.store.locked() as state:
            plan = deepcopy(state.get("cart_plan"))
        if not isinstance(plan, dict) or canonical(plan.get("menu_ref")) != canonical(expected_menu_ref) or plan.get("status") != "needs_input" or plan.get("pending_cart_digest") != supplied_digest:
            raise HouseholdError("cart reconciliation is not bound to the pending cart")
        if first_digest != supplied_digest:
            with self.store.locked() as state:
                current = state["cart_plan"]
                self._set_cart_needs_input(current, first_live, first_names)
                plan = deepcopy(current)
            return {"reconciled": False, **self._cart_question(plan, first_summary, reason="cart_changed_after_question")}
        requirements = plan["required_quantities"]
        unknown = (excluded | accepted_missing) - (set(first_live) | set(requirements))
        if unknown:
            raise HouseholdError("cart decisions must use exact products from the current plan or cart")
        target = dict(first_live)
        if decision == "restore_missing":
            for product_id, required in requirements.items():
                if first_live.get(product_id, 0) < required and product_id not in accepted_missing:
                    target[product_id] = required
        for product_id in excluded:
            current = target.get(product_id, 0)
            required = requirements.get(product_id, 0)
            if current <= required:
                if product_id not in accepted_missing:
                    raise HouseholdError("an exclusion cannot reduce below the menu requirement unless that missing product is explicitly accepted")
                target.pop(product_id, None)
            elif required:
                target[product_id] = required
            else:
                target.pop(product_id, None)
        operations = []
        for product_id in sorted(set(first_live) | set(target)):
            delta = target.get(product_id, 0) - first_live.get(product_id, 0)
            if delta:
                operations.append({"productId": int(product_id) if self.provider == "oda" else product_id, "quantity": delta})
        mutation_error = None
        acknowledged_live = dict(first_live)
        if operations:
            prewrite_cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
            prewrite_summary = cart_summary(prewrite_cart)
            prewrite_live, prewrite_names = self._cart_lines(prewrite_summary)
            if self._cart_digest(prewrite_live) != supplied_digest:
                with self.store.locked() as state:
                    current = state["cart_plan"]
                    self._set_cart_needs_input(current, prewrite_live, prewrite_names)
                    plan = deepcopy(current)
                return {"reconciled": False, **self._cart_question(plan, prewrite_summary, reason="cart_changed_immediately_before_decision")}
            try:
                if self.provider == "meny":
                    self._apply_meny_cart_batches(
                        operations, acknowledged_live, deadline=deadline
                    )
                else:
                    self.oda.call("manipulate_cart", {"operations": operations})
            except HouseholdError as exc:
                mutation_error = exc
        verified_cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
        verified_summary = cart_summary(verified_cart)
        verified_live, verified_names = self._cart_lines(verified_summary)
        if mutation_error is not None or verified_live != target:
            with self.store.locked() as state:
                current = state["cart_plan"]
                self._retain_acknowledged_cart_additions(
                    current, first_live, acknowledged_live, verified_live
                )
                self._set_cart_needs_input(current, verified_live, verified_names)
                plan = deepcopy(current)
            return {
                "reconciled": False,
                **self._cart_question(
                    plan,
                    verified_summary,
                    reason="cart_write_result_uncertain" if mutation_error is not None else "cart_changed_while_applying_decision",
                ),
            }
        approved_digest = self._cart_digest(verified_live)
        with self.store.locked() as state:
            current = state.get("cart_plan")
            if not isinstance(current, dict) or current.get("pending_cart_digest") != supplied_digest:
                raise HouseholdError("cart plan changed while applying the decision")
            current["added_quantities"] = {
                product_id: min(quantity, verified_live.get(product_id, 0))
                for product_id, quantity in current["added_quantities"].items()
                if min(quantity, verified_live.get(product_id, 0)) > 0
            }
            for operation in operations:
                if operation["quantity"] > 0:
                    product_id = str(operation["productId"])
                    current["added_quantities"][product_id] = current["added_quantities"].get(product_id, 0) + operation["quantity"]
            current["product_names"].update(verified_names)
            current["last_synced_quantities"] = dict(verified_live)
            current["last_synced_digest"] = approved_digest
            current["approved_cart_digest"] = approved_digest
            current["pending_cart_digest"] = None
            current["status"] = "active"
            current["updated_at"] = self._now().isoformat()
            plan = deepcopy(current)
        return {
            "reconciled": True,
            "decision": decision,
            "excluded_product_ids": sorted(excluded),
            "accepted_missing_product_ids": sorted(accepted_missing),
            "cart": verified_cart,
            "cart_plan": self._cart_plan_view(plan, verified_summary),
        }

    def _cart_checkout_gate(self, summary: Mapping[str, Any], menu: Mapping[str, Any]) -> dict[str, Any] | None:
        live, names = self._cart_lines(summary)
        digest = self._cart_digest(live)
        menu_ref = self._cart_menu_ref(menu)
        with self.store.locked() as state:
            plan = state.get("cart_plan")
            bound = (
                isinstance(plan, dict)
                and plan.get("provider") == self.provider
                and canonical(plan.get("menu_ref")) == canonical(menu_ref)
            )
            if not bound:
                previous = deepcopy(plan) if isinstance(plan, Mapping) else None
                plan = self._new_cart_plan(menu_ref or {}, live, names, {}, {}, set(), previous=previous)
                self._set_cart_needs_input(plan, live, names)
                state["cart_plan"] = plan
                result = deepcopy(plan)
                reason = "missing_or_stale_cart_plan"
            elif plan.get("approved_cart_digest") == digest:
                plan["status"] = "active"
                plan["pending_cart_digest"] = None
                plan["last_synced_quantities"] = dict(live)
                plan["last_synced_digest"] = digest
                plan["product_names"].update(names)
                plan["updated_at"] = self._now().isoformat()
                return None
            else:
                view = self._cart_plan_view(plan, summary)
                has_extra = any(item["extra_quantity"] > 0 for item in view["items"])
                has_missing = any(item["missing_quantity"] > 0 for item in view["items"])
                has_unresolved_start = any(item["unresolved_start_quantity"] for item in view["items"])
                if not has_extra and not has_missing and not has_unresolved_start:
                    plan["approved_cart_digest"] = digest
                    plan["pending_cart_digest"] = None
                    plan["status"] = "active"
                    plan["last_synced_quantities"] = dict(live)
                    plan["last_synced_digest"] = digest
                    plan["product_names"].update(names)
                    plan["updated_at"] = self._now().isoformat()
                    return None
                self._set_cart_needs_input(plan, live, names)
                result = deepcopy(plan)
                reason = "cart_requires_owner_decision"
        return self._cart_question(result, summary, reason=reason)

    def _require_cart_menu(self, request: Mapping[str, Any]) -> dict[str, Any]:
        supplied = request.get("menu_ref", request.get("_expected_menu_ref"))
        if not isinstance(supplied, Mapping) or set(supplied) != {"menu_id", "revision", "digest"}:
            raise HouseholdError("cart sync and reconcile require the exact menu_ref from the menu or question")
        current = self._cart_menu_ref(self.store.read().get("menu"))
        if current is None or canonical(current) != canonical(supplied):
            raise HouseholdError("cart menu_ref is stale; read the menu and rebuild its requirements")
        return deepcopy(dict(supplied))

    def _cart(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "get")
        if action in {"sync", "reconcile"}:
            deadline = time.monotonic() + MENY_CART_TIMEOUT if self.provider == "meny" else None
            with self._browser_operation(deadline):
                return self._cart_sync(request, deadline) if action == "sync" else self._cart_reconcile(request, deadline)
        if action == "get":
            cart = self.oda.call("get_cart", {}, deadline=request.get("_deadline"), allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.oda.call("get_cart", {})
            plan = self.store.read().get("cart_plan")
            return {**cart, **({"meal_concierge_cart_plan": self._cart_plan_view(plan, cart_summary(cart))} if isinstance(plan, Mapping) else {})}
        if action in {"change", "apply", "set", "update"}:
            deadline = time.monotonic() + MENY_CART_TIMEOUT if self.provider == "meny" else None
            operations = request.get("operations")
            if not isinstance(operations, list) or not operations:
                raise HouseholdError("cart change needs operations")
            normalized = []
            for operation in operations:
                if not isinstance(operation, Mapping):
                    raise HouseholdError("cart operations must be objects")
                item = dict(operation)
                if "product_id" in item and "productId" not in item:
                    product_id = item.pop("product_id")
                    normalized_id = self._product_id(product_id)
                    item["productId"] = int(normalized_id) if self.provider == "oda" else normalized_id
                normalized.append(item)
            with self._browser_operation(deadline):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before changing the cart")
                change = deepcopy(state.get("order_change"))
                if change and change.get("status") != "editing":
                    raise HouseholdError("the order change is still starting")
                if isinstance(state.get("menu"), Mapping) and not change:
                    raise HouseholdError("an active weekly menu must update the cart with action=sync and exact requirements")
                if self.provider == "oda" and (state.get("order_change") or {}).get("requested_delivery"):
                    raise HouseholdError("finish or abort the staged Oda delivery change before adding items")
                arguments = {"operations": normalized}
                if self.provider == "meny":
                    self.browser.verify_order_change(
                        change.get("order_id") if change else None,
                        change.get("code") if change else None,
                        deadline=deadline,
                    )
                    if change:
                        arguments["order_change_code"] = change["code"]
                    result = self.oda.call("manipulate_cart", arguments, deadline=deadline)
                    self.browser.verify_order_change(
                        change.get("order_id") if change else None,
                        change.get("code") if change else None,
                        deadline=deadline,
                    )
                else:
                    result = self.oda.call("manipulate_cart", arguments)
                with self.store.locked() as locked:
                    if canonical(locked.get("order_change")) != canonical(change):
                        raise HouseholdError("order change state changed while updating the cart")
                    if change:
                        locked["order_change"]["kind"] = "full_order" if self.provider == "meny" else "addition"
                return result
        raise HouseholdError("unknown cart action")
