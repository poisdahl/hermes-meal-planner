"""Read-only menu coverage and household workflow, derived from existing state."""
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re
import unicodedata


def _text(value):
    return " ".join(unicodedata.normalize("NFC", str(value)).casefold().split())


def feedback_targets(menu):
    if not isinstance(menu, Mapping):
        return []
    reference = {k: menu[k] for k in ("menu_id", "revision", "digest")}
    if menu.get("slots"):
        return [{"menu_ref": reference, "slot_id": slot["slot_id"], "recipe_key": slot["recipe_key"], "reference": deepcopy(slot["reference"])} for slot in menu["slots"]]
    return [{"menu_ref": reference, "recipe_key": recipe["recipe_key"],
             "reference": deepcopy(recipe.get("library_recipe_ref") or recipe.get("recipe_ref") or {"recipe_key": recipe["recipe_key"]})}
            for recipe in [*menu.get("dishes", []), *menu.get("salads", [])]]


def assess_menu(state):
    menu = state.get("menu")
    if not isinstance(menu, Mapping):
        return {"ready": False, "status": "missing", "issues": [{"code": "menu_missing"}]}
    profile = state["profile"]
    meals = profile["meals"]
    selection = menu.get("planner_selection") or {}
    requested = menu.get("planning_scope") or selection.get("request") or {}
    expected_days = len(requested["dates"]) if requested.get("dates") else meals["dinner_days"]
    expected_portions = requested.get("portions") or meals["portions"]
    dishes = menu.get("dishes", [])
    recipes = [*dishes, *menu.get("salads", [])]
    slots = menu.get("slots") or []
    covered_days = len({slot.get("date") for slot in slots if slot.get("meal_type") == "dinner"}) if slots else None
    issues = []
    if slots and covered_days != expected_days:
        issues.append({"code": "dinner_day_coverage", "expected": expected_days, "actual": covered_days})
    if not slots and len(dishes) != meals["dishes"]:
        issues.append({"code": "dish_coverage", "expected": meals["dishes"], "actual": len(dishes)})
    if not slots:
        issues.append({"code": "meal_dates_unverified", "detail": "Legacy recipe lists do not establish which dates are covered."})
    unknown_quantities = []
    conflicts = []
    diet = profile["diet"]
    rules = [*diet["allergies_or_sensitivities"], *diet["avoid"]]
    for recipe in recipes:
        key = recipe.get("recipe_key")
        if not slots and recipe.get("portions") != expected_portions:
            issues.append({"code": "portion_mismatch", "recipe_key": key, "expected": expected_portions, "actual": recipe.get("portions")})
        for index, ingredient in enumerate(recipe.get("ingredients", [])):
            if ingredient.get("quantity") is None or not ingredient.get("unit"):
                unknown_quantities.append({"recipe_key": key, "ingredient_index": index, "item": ingredient.get("item"), "pantry": ingredient.get("pantry", False)})
            identity = _text(ingredient.get("item", ""))
            for rule in rules:
                if re.search(r"(?<!\w)" + re.escape(_text(rule)) + r"(?!\w)", identity):
                    conflicts.append({"recipe_key": key, "item": ingredient.get("item"), "rule": rule})
    if conflicts:
        issues.append({"code": "explicit_ingredient_conflict", "conflicts": conflicts[:25], "total": len(conflicts)})
    if rules:
        issues.append({"code": "diet_safety_unverified", "detail": "No authoritative allergy or ingredient-safety evidence is connected."})
    result = {"menu_ref": {k: menu[k] for k in ("menu_id", "revision", "digest")},
              "ready": not issues, "status": "ready" if not issues else "needs_input",
              "dinner_days": {"expected": expected_days, "verified": covered_days},
              "portions": expected_portions, "issues": issues[:25], "issue_count": len(issues),
              "ingredients_needing_quantity_or_pantry_decision": unknown_quantities[:25],
              "ingredients_needing_decision_count": len(unknown_quantities),
              "scope": "Menu coverage and explicit conflicts only; nutrition and allergy safety are not certified."}
    result["assessment_digest"] = hashlib.sha256(json.dumps({"menu": result["menu_ref"], "profile": profile, "assessment": result}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def workflow_status(state):
    assessment = assess_menu(state)
    menu = state.get("menu") or {}
    cart = state.get("cart_plan") or {}
    current_ref = {k: menu.get(k) for k in ("menu_id", "revision", "digest")}
    bound_cart = cart if cart.get("menu_ref") == current_ref else {}
    pending = state.get("pending_checkout") or {}
    cancellation = state.get("pending_cancellation") or {}
    change = state.get("order_change") or {}
    order_id = menu.get("order_id")
    jobs = [j for j in state.get("email_jobs", []) if j.get("order_id") == order_id and j.get("provider") == state["provider"]] if order_id else []
    next_action = {"operation": "menu", "action": "plan", "reason": "Prepare a menu."}
    if pending:
        action = "reconcile" if pending.get("status") in {"clicking", "uncertain", "awaiting_user_payment"} else "confirm"
        next_action = {"operation": "checkout", "action": action, "reason": "Approve the existing Vipps request on the phone, then reconcile." if pending.get("status") == "awaiting_user_payment" else "Continue the exact existing checkout attempt; confirmation policy still applies."}
    elif cancellation:
        next_action = {"operation": "orders", "action": "cancel_reconcile" if cancellation.get("status") in {"clicking", "uncertain"} else "cancel_confirm", "reason": "Finish the existing cancellation."}
    elif change:
        next_action = {"operation": "orders", "action": "get", "reason": "Review the exact order being changed before continuing."}
    elif menu.get("phase") == "ordered" and not jobs and state.get("email_recipient"):
        next_action = {"operation": "email", "action": "schedule", "reason": "Schedule recipes for the exact confirmed order and delivery date."}
    elif menu.get("phase") == "ordered" and any(j.get("status") in {"claimed", "sending", "uncertain"} for j in jobs):
        next_action = {"operation": "email", "action": "status", "reason": "Resolve the existing email dispatch before sending again."}
    elif menu.get("phase") == "ordered":
        next_action = {"operation": "email", "action": "automation_plan", "reason": "Apply any pending recipe-email scheduling changes."} if any(j.get("status") == "pending" and j.get("automation_protocol") != 4 for j in jobs) else {"operation": "feedback", "action": "experience", "reason": "Record cooking experience only when the household reports it."}
    elif menu:
        completion = state.get("product_plan_completion") or {}
        if completion.get("menu_ref") == current_ref and completion.get("nothing_to_buy"):
            next_action = {"operation": "menu", "action": "get", "reason": "All ingredient needs were explicitly covered at home; no grocery purchase is required. Continue cooking and record reported experience."}
        elif not assessment["ready"]:
            next_action = {"operation": "menu", "action": "assess", "reason": "Resolve the listed menu coverage or ingredient issues."}
        elif bound_cart.get("status") == "needs_input":
            next_action = {"operation": "cart", "action": "get", "reason": "Read the current cart and resolve its exact reconciliation question."}
        elif not bound_cart.get("product_plan_digest"):
            next_action = {"operation": "products", "action": "prepare", "reason": "Map ingredient needs to observed packages and resolve pantry assumptions."}
        elif not state.get("delivery_selection"):
            next_action = {"operation": "delivery", "action": "list", "reason": "Choose a delivery window."}
        else:
            next_action = {"operation": "checkout", "action": "prepare", "reason": "Read the final provider total and prepare checkout."}
    return {"menu": assessment, "cart_status": bound_cart.get("status", "not_prepared"),
            "product_plan": deepcopy(bound_cart.get("product_plan_summary")),
            "delivery_status": "observed_requires_revalidation" if state.get("delivery_selection") else "not_observed",
            "checkout_status": pending.get("status", "confirmed" if order_id else "not_started"),
            "email_status": [j.get("status") for j in jobs], "next_action": next_action}
