"""Exact, bounded product/package selection for one menu."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import math
import re
from typing import Any, Mapping
import unicodedata

from core import HouseholdError


PRODUCT_PLAN_VERSION = "product-plan-v2"
MAX_REQUIREMENTS = 20
MAX_CANDIDATES_PER_REQUIREMENT = 5
MAX_PACKAGES_PER_REQUIREMENT = 100
MAX_COMBINATIONS = 10_000

_UNITS = {
    "g": ("g", Fraction(1)),
    "gram": ("g", Fraction(1)),
    "kg": ("g", Fraction(1_000)),
    "ml": ("ml", Fraction(1)),
    "cl": ("ml", Fraction(10)),
    "dl": ("ml", Fraction(100)),
    "l": ("ml", Fraction(1_000)),
    "stk": ("count", Fraction(1)),
    "stykk": ("count", Fraction(1)),
    "count": ("count", Fraction(1)),
}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _ref_sort_key(value: str | int) -> bytes:
    return canonical(value).encode("utf-8")


def _valid_product_ref(value: Any) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and value > 0
    ) or (
        isinstance(value, str) and 1 <= len(value.encode("utf-8")) <= 500
    )


def _identity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if not text or len(text.encode("utf-8")) > 300:
        return None
    return text.casefold()


def _positive_fraction(value: Any) -> Fraction | None:
    if isinstance(value, Mapping):
        try:
            result = _read_fraction(value, positive=True)
            return result if result.numerator <= 10**15 and result.denominator <= 10**12 else None
        except HouseholdError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        return None
    if not decimal.is_finite() or decimal <= 0 or not -12 <= decimal.adjusted() <= 15:
        return None
    result = Fraction(decimal)
    if result.numerator > 10**15 or result.denominator > 10**12:
        return None
    return result


def _normalized_unit(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _legacy_scalable(recipe: Mapping[str, Any], index: int, requirement: Mapping[str, Any]) -> bool:
    """Recover only an omitted flag from the same frozen scaled ingredient."""

    if "scalable" in requirement:
        return False
    ingredients = recipe.get("ingredients")
    if not isinstance(ingredients, list) or index >= len(ingredients):
        return False
    ingredient = ingredients[index]
    if not isinstance(ingredient, Mapping) or ingredient.get("scalable") is not True:
        return False
    return (
        _identity(ingredient.get("item")) == _identity(requirement.get("item"))
        and _identity(ingredient.get("item")) is not None
        and _positive_fraction(ingredient.get("quantity")) == _positive_fraction(requirement.get("quantity"))
        and _positive_fraction(ingredient.get("quantity")) is not None
        and _normalized_unit(ingredient.get("unit")) == _normalized_unit(requirement.get("unit"))
        and _normalized_unit(ingredient.get("unit")) != ""
        and ingredient.get("optional", False) is requirement.get("optional", False)
        and ingredient.get("pantry", False) is requirement.get("pantry", False)
    )


def _fraction_json(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _read_fraction(value: Any, *, positive: bool = False) -> Fraction:
    if not isinstance(value, Mapping) or set(value) != {"numerator", "denominator"}:
        raise HouseholdError("product plan fraction is invalid")
    numerator = value.get("numerator")
    denominator = value.get("denominator")
    if (
        isinstance(numerator, bool) or not isinstance(numerator, int)
        or isinstance(denominator, bool) or not isinstance(denominator, int)
        or denominator < 1 or (positive and numerator < 1) or (not positive and numerator < 0)
    ):
        raise HouseholdError("product plan fraction is invalid")
    return Fraction(numerator, denominator)


def menu_requirements(menu: Any, *, maximum: int | None = MAX_REQUIREMENTS, ingredient_decisions: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Aggregate only exact compatible recipe requirements."""

    if not isinstance(menu, Mapping):
        raise HouseholdError("product preparation needs one exact menu")
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    unresolved = []
    decisions = ingredient_decisions or []
    if not isinstance(decisions, list) or len(decisions) > 512:
        raise HouseholdError("ingredient_decisions must be a bounded list")
    by_position = {}
    for decision in decisions:
        if not isinstance(decision, Mapping) or not set(decision).issubset({"source", "action", "quantity", "unit"}):
            raise HouseholdError("ingredient decision fields are invalid")
        position = decision.get("source")
        if not isinstance(position, Mapping) or set(position) != {"collection", "recipe_index", "ingredient_index"} or position["collection"] not in {"dishes", "salads"} or any(type(position[k]) is not int or position[k] < 0 for k in ("recipe_index", "ingredient_index")):
            raise HouseholdError("ingredient decision requires an exact returned source position")
        key = canonical(position)
        if key in by_position or decision.get("action") not in {"have_all", "have_quantity", "include", "omit"}:
            raise HouseholdError("ingredient decisions must be unique explicit choices")
        if decision["action"] != "have_quantity" and ("quantity" in decision or "unit" in decision):
            raise HouseholdError("only have_quantity accepts a quantity and unit")
        by_position[key] = decision
    used = set()
    for collection in ("dishes", "salads"):
        recipes = menu.get(collection)
        if not isinstance(recipes, list):
            raise HouseholdError("menu recipes are invalid")
        for recipe_index, recipe in enumerate(recipes):
            if not isinstance(recipe, Mapping) or not isinstance(recipe.get("shopping_requirements"), list):
                raise HouseholdError("menu recipe shopping requirements are invalid")
            for ingredient_index, raw in enumerate(recipe["shopping_requirements"]):
                position = {
                    "collection": collection,
                    "recipe_index": recipe_index,
                    "ingredient_index": ingredient_index,
                }
                if not isinstance(raw, Mapping):
                    unresolved.append({**position, "reason": "invalid_requirement"})
                    continue
                item = raw.get("item")
                identity = _identity(item)
                quantity = _positive_fraction(raw.get("quantity"))
                unit = _normalized_unit(raw.get("unit"))
                conversion = _UNITS.get(unit)
                scalable = raw.get("scalable") is True or _legacy_scalable(
                    recipe, ingredient_index, raw
                )
                decision = by_position.get(canonical(position))
                action = decision.get("action") if decision else None
                if decision:
                    used.add(canonical(position))
                if action == "omit" and raw.get("optional") is not True:
                    raise HouseholdError("only an optional ingredient can be omitted")
                if action == "omit" or (action == "have_all" and (quantity is None or conversion is None or identity is None or not scalable)):
                    continue
                reason = None
                if raw.get("pantry") is True and action is None:
                    reason = "pantry_state_needs_input"
                elif raw.get("optional") is True and action is None:
                    reason = "optional_requirement_needs_input"
                elif not scalable:
                    reason = "non_scalable_quantity_unresolved"
                elif identity is None:
                    reason = "ingredient_identity_unresolved"
                elif quantity is None or conversion is None:
                    reason = "quantity_or_unit_unresolved"
                if reason is not None:
                    unresolved.append({
                        **position, "item": str(item or "")[:300],
                        "quantity": raw.get("quantity"), "unit": raw.get("unit"), "reason": reason,
                    })
                    continue
                canonical_unit, factor = conversion
                exact_quantity = quantity * factor
                gross_quantity = exact_quantity
                pantry_quantity = Fraction(0)
                if action == "have_all":
                    pantry_quantity = exact_quantity
                    exact_quantity = Fraction(0)
                if action == "have_quantity":
                    available = _positive_fraction(decision.get("quantity"))
                    available_unit = _UNITS.get(_normalized_unit(decision.get("unit")))
                    if available is None or available_unit is None or available_unit[0] != canonical_unit:
                        raise HouseholdError("pantry quantity must have an exact compatible unit")
                    pantry_quantity = min(exact_quantity, available * available_unit[1])
                    exact_quantity -= pantry_quantity
                key = (identity, canonical_unit)
                requirement = aggregated.setdefault(key, {
                    "identity": identity,
                    "item": " ".join(unicodedata.normalize("NFC", str(item)).split()),
                    "unit": canonical_unit,
                    "quantity_fraction": Fraction(0),
                    "gross_fraction": Fraction(0),
                    "pantry_fraction": Fraction(0),
                    "sources": [],
                })
                requirement["quantity_fraction"] += exact_quantity
                requirement["gross_fraction"] += gross_quantity
                requirement["pantry_fraction"] += pantry_quantity
                requirement["sources"].append(position)
    requirements = []
    for (identity, unit), value in sorted(aggregated.items(), key=lambda pair: (pair[0][0].encode("utf-8"), pair[0][1])):
        if value["quantity_fraction"] == 0:
            continue
        requirement_id = "req:" + hashlib.sha256(canonical({"identity": identity, "unit": unit}).encode()).hexdigest()[:24]
        requirements.append({
            "requirement_id": requirement_id,
            "identity": identity,
            "item": value["item"],
            "search": value["item"],
            "quantity": _fraction_json(value["quantity_fraction"]),
            "gross_quantity": _fraction_json(value["gross_fraction"]),
            "confirmed_pantry_quantity": _fraction_json(value["pantry_fraction"]),
            "unit": unit,
            "sources": value["sources"],
        })
    if set(by_position) != used:
        raise HouseholdError("ingredient decision does not name a source in this exact menu")
    if maximum is not None and len(requirements) + len(unresolved) > maximum:
        raise HouseholdError(f"product preparation supports at most {maximum} menu requirements")
    return requirements, unresolved


def normalize_approvals(value: Any, requirement_ids: set[str]) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, list) or len(value) > MAX_REQUIREMENTS:
        raise HouseholdError("candidate_approvals must be a bounded list")
    approvals = {}
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw).difference({"requirement_id", "candidate_refs", "max_excess"}):
            raise HouseholdError("candidate approval has unknown fields")
        requirement_id = raw.get("requirement_id")
        refs = raw.get("candidate_refs")
        if requirement_id not in requirement_ids:
            raise HouseholdError("candidate approval requirement_id is not in this menu")
        if requirement_id in approvals:
            raise HouseholdError("candidate approval requirement_id is duplicated")
        if (
            not isinstance(refs, list) or not 1 <= len(refs) <= MAX_CANDIDATES_PER_REQUIREMENT
            or any(not _valid_product_ref(ref) for ref in refs)
            or len(set(refs)) != len(refs)
        ):
            raise HouseholdError("candidate approval needs one to five exact product refs")
        approval: dict[str, Any] = {
            "requirement_id": requirement_id,
            "candidate_refs": sorted(refs, key=_ref_sort_key),
            "source": "current_user_exact_candidate_scope",
        }
        if raw.get("max_excess") is not None:
            maximum = _read_fraction(raw["max_excess"])
            if maximum > 100:
                raise HouseholdError("candidate approval max_excess is too large")
            approval["max_excess"] = _fraction_json(maximum)
        approvals[requirement_id] = approval
    return approvals


def _normalize_hard_product_constraints(value: Any) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or set(value).difference({
        "allergies_or_sensitivities", "avoid",
    }):
        raise HouseholdError("hard product constraints are invalid")
    normalized = {}
    for key in ("allergies_or_sensitivities", "avoid"):
        rules = value.get(key, [])
        if not isinstance(rules, list) or len(rules) > 50:
            raise HouseholdError("hard product constraints are invalid")
        cleaned = []
        for rule in rules:
            if not isinstance(rule, str):
                raise HouseholdError("hard product constraints are invalid")
            text = " ".join(unicodedata.normalize("NFC", rule).split())
            if not text or len(text.encode("utf-8")) > 300:
                raise HouseholdError("hard product constraints are invalid")
            cleaned.append(text)
        if cleaned:
            normalized[key] = sorted(set(cleaned), key=lambda item: item.encode("utf-8"))
    return normalized


def _option_costs(product: Mapping[str, Any], maximum: int) -> list[dict[str, Any] | None]:
    options = product.get("purchase_options")
    if not isinstance(options, list) or not options:
        raise HouseholdError("candidate has no purchase options")
    bundles = []
    for index, option in enumerate(options):
        if not isinstance(option, Mapping):
            raise HouseholdError("candidate purchase option is invalid")
        packages = option.get("package_count")
        fields = (option.get("merchandise_ore"), option.get("mandatory_deposit_ore"), option.get("total_payable_ore"))
        if (
            option.get("price_kind") != "exact" or option.get("eligibility") != "confirmed"
            or isinstance(packages, bool) or not isinstance(packages, int) or not 1 <= packages <= 20
            or any(isinstance(amount, bool) or not isinstance(amount, int) or amount < 0 for amount in fields)
            or fields[0] + fields[1] != fields[2]
        ):
            raise HouseholdError("candidate has an inexact or ineligible purchase option")
        bundles.append({
            "option_index": index,
            "package_count": packages,
            "merchandise_ore": fields[0],
            "mandatory_deposit_ore": fields[1],
            "total_payable_ore": fields[2],
            "offer_kind": str(option.get("offer_kind") or "regular"),
        })
    costs: list[dict[str, Any] | None] = [None] * (maximum + 1)
    costs[0] = {"merchandise_ore": 0, "mandatory_deposit_ore": 0, "total_payable_ore": 0, "bundles": []}
    promotion_limit = max(
        (
            bundle["package_count"] for bundle in bundles
            if bundle["offer_kind"] != "regular"
        ),
        default=None,
    )
    for count in range(maximum + 1):
        current = costs[count]
        if current is None:
            continue
        for bundle in bundles:
            if bundle["offer_kind"] != "regular" and any(
                existing["option_index"] == bundle["option_index"]
                for existing in current["bundles"]
            ):
                continue
            target = count + bundle["package_count"]
            if target > maximum or (
                promotion_limit is not None and target > promotion_limit
            ):
                continue
            candidate = {
                "merchandise_ore": current["merchandise_ore"] + bundle["merchandise_ore"],
                "mandatory_deposit_ore": current["mandatory_deposit_ore"] + bundle["mandatory_deposit_ore"],
                "total_payable_ore": current["total_payable_ore"] + bundle["total_payable_ore"],
                "bundles": [*current["bundles"], {
                    "option_index": bundle["option_index"],
                    "package_count": bundle["package_count"],
                    "offer_kind": bundle["offer_kind"],
                }],
            }
            rank = (
                candidate["total_payable_ore"], canonical(candidate["bundles"]),
            )
            previous = costs[target]
            previous_rank = None if previous is None else (
                previous["total_payable_ore"], canonical(previous["bundles"]),
            )
            if previous_rank is None or rank < previous_rank:
                costs[target] = candidate
    return costs


def _select_requirement(
    requirement: Mapping[str, Any], observation: Mapping[str, Any], approval: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str | None, int]:
    products = observation.get("products")
    if not isinstance(products, list):
        return None, "provider_search_invalid", 0
    by_ref = {
        product.get("product_ref"): product
        for product in products
        if isinstance(product, Mapping) and _valid_product_ref(product.get("product_ref"))
    }
    approved_refs = approval["candidate_refs"]
    missing = [ref for ref in approved_refs if ref not in by_ref]
    if missing:
        return None, "approved_candidate_scope_changed", 0
    approved = [by_ref[ref] for ref in approved_refs]
    required = _read_fraction(requirement["quantity"], positive=True)
    candidates = []
    for product in approved:
        if product.get("availability") != "available":
            return None, "candidate_availability_unresolved", 0
        package = product.get("package")
        if not isinstance(package, Mapping) or package.get("unit") != requirement.get("unit"):
            return None, "candidate_package_incompatible", 0
        try:
            package_quantity = _read_fraction(package.get("quantity"), positive=True)
        except HouseholdError:
            return None, "candidate_package_incompatible", 0
        options = product.get("purchase_options")
        if not isinstance(options, list) or not options:
            return None, "candidate_price_unresolved", 0
        if any(
            not isinstance(option, Mapping)
            or option.get("price_kind") != "exact"
            or option.get("eligibility") != "confirmed"
            or option.get("total_payable_ore") is None
            for option in options
        ):
            return None, "candidate_price_or_eligibility_unresolved", 0
        candidates.append((product, package_quantity))
    if not candidates:
        return None, "candidate_scope_empty", 0
    maximum_bundle = max(
        option["package_count"]
        for product, _quantity in candidates
        for option in product["purchase_options"]
    )
    minimum_quantity = min(quantity for _product, quantity in candidates)
    maximum_packages = math.ceil(required / minimum_quantity) + maximum_bundle - 1
    if maximum_packages > MAX_PACKAGES_PER_REQUIREMENT:
        return None, "package_limit_exceeded", len(candidates)
    costs = [
        _option_costs(product, maximum_packages)
        for product, _quantity in candidates
    ]
    maximum_excess = _read_fraction(approval["max_excess"]) if approval.get("max_excess") is not None else None
    work = 0
    best: tuple[Any, dict[str, Any]] | None = None

    def visit(index: int, quantities: list[int], covered: Fraction, merchandise: int, deposit: int, payable: int, bundles: list[Any]) -> bool:
        nonlocal work, best
        if index == len(candidates):
            work += 1
            if work > MAX_COMBINATIONS:
                return False
            if covered < required:
                return True
            excess = (covered - required) / required
            if maximum_excess is not None and excess > maximum_excess:
                return True
            selected_products = [
                {
                    "product_ref": candidates[position][0]["product_ref"],
                    "name": candidates[position][0]["name"],
                    "quantity": count,
                    "purchase_options": bundles[position],
                    "merchandise_ore": sum(
                        candidates[position][0]["purchase_options"][bundle["option_index"]]["merchandise_ore"]
                        for bundle in bundles[position]
                    ),
                    "mandatory_deposit_ore": sum(
                        candidates[position][0]["purchase_options"][bundle["option_index"]]["mandatory_deposit_ore"]
                        for bundle in bundles[position]
                    ),
                    "total_payable_ore": sum(
                        candidates[position][0]["purchase_options"][bundle["option_index"]]["total_payable_ore"]
                        for bundle in bundles[position]
                    ),
                }
                for position, count in enumerate(quantities) if count
            ]
            package_count = sum(quantities)
            stable_refs = [[item["product_ref"], item["quantity"]] for item in selected_products]
            selection = {
                "products": selected_products,
                "coverage": _fraction_json(covered),
                "required": _fraction_json(required),
                "unit": requirement["unit"],
                "excess_score": _fraction_json(excess),
                "package_count": package_count,
                "merchandise_ore": merchandise,
                "mandatory_deposit_ore": deposit,
                "total_payable_ore": payable,
                "tie_break": stable_refs,
            }
            rank = (payable, excess, package_count, canonical(stable_refs))
            if best is None or rank < best[0]:
                best = (rank, selection)
            return True
        product, package_quantity = candidates[index]
        for count, cost in enumerate(costs[index]):
            if cost is None:
                continue
            if not visit(
                index + 1, [*quantities, count], covered + package_quantity * count,
                merchandise + cost["merchandise_ore"], deposit + cost["mandatory_deposit_ore"],
                payable + cost["total_payable_ore"], [*bundles, cost["bundles"]],
            ):
                return False
        return True

    if not visit(0, [], Fraction(0), 0, 0, 0, []):
        return None, "combination_work_limit_exceeded", len(candidates)
    if best is None:
        return None, "quantity_or_excess_limit_unmet", len(candidates)
    return best[1], None, len(candidates)


def _canonical_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    observation = deepcopy(dict(value))
    products = observation.get("products")
    if isinstance(products, list) and all(
        isinstance(product, Mapping) and _valid_product_ref(product.get("product_ref"))
        for product in products
    ):
        observation["products"] = sorted(
            products, key=lambda product: _ref_sort_key(product["product_ref"])
        )
    return observation


def _without_presentation(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _without_presentation(child)
            for key, child in value.items()
            if key not in {"product_plan_digest", "observed_at", "display", "display_ore_per_unit"}
        }
    if isinstance(value, list):
        return [_without_presentation(child) for child in value]
    return value


def product_plan_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(_without_presentation(value)).encode()).hexdigest()


def _estimated_single_product(requirement, observation, approval):
    """Quantity-ready explicit selection when only the deposit is unknown."""
    refs = approval["candidate_refs"]
    if len(refs) != 1:
        return None
    matches = [p for p in observation.get("products", []) if p.get("product_ref") == refs[0]]
    if len(matches) != 1:
        return None
    product = matches[0]
    package = product.get("package")
    options = product.get("purchase_options", [])
    if product.get("availability") != "available" or not isinstance(package, Mapping) or package.get("unit") != requirement["unit"] or len(options) != 1:
        return None
    option = options[0]
    if option.get("price_kind") != "exact" or option.get("eligibility") != "confirmed" or option.get("offer_kind") != "regular" or option.get("package_count") != 1 or type(option.get("merchandise_ore")) is not int or option["merchandise_ore"] < 0:
        return None
    size = _read_fraction(package["quantity"], positive=True)
    needed = _read_fraction(requirement["quantity"], positive=True)
    count = math.ceil(needed / size)
    if not 1 <= count <= MAX_PACKAGES_PER_REQUIREMENT:
        return None
    excess = (count * size - needed) / needed
    if approval.get("max_excess") is not None and excess > _read_fraction(approval["max_excess"]):
        return None
    merchandise = count * option["merchandise_ore"]
    return {"products": [{"product_ref": refs[0], "name": product["name"], "quantity": count,
                          "merchandise_ore": merchandise, "mandatory_deposit_ore": None, "total_payable_ore": None}],
            "coverage": _fraction_json(count * size), "required": _fraction_json(needed),
            "unit": requirement["unit"], "excess_score": _fraction_json(excess), "package_count": count,
            "merchandise_ore": merchandise, "mandatory_deposit_ore": None, "total_payable_ore": None}


def build_product_plan(
    *, provider: str, binding: Mapping[str, Any], menu: Mapping[str, Any],
    observations: Mapping[str, Mapping[str, Any]], candidate_approvals: Any,
    hard_product_constraints: Any = None,
    ingredient_decisions: Any = None,
    budget_ore: int | None = None,
    price_mode: str = "exact",
) -> dict[str, Any]:
    if price_mode not in {"exact", "estimate"}:
        raise HouseholdError("price_mode must be exact or estimate")
    if budget_ore is not None and (type(budget_ore) is not int or not 1 <= budget_ore <= 100_000_000):
        raise HouseholdError("product budget_ore must be a positive integer")
    requirements, structural_unresolved = menu_requirements(menu, ingredient_decisions=ingredient_decisions)
    approvals = normalize_approvals(candidate_approvals, {item["requirement_id"] for item in requirements})
    hard_constraints = _normalize_hard_product_constraints(hard_product_constraints)
    ref_owners: dict[str | int, set[str]] = {}
    for requirement_id, approval in approvals.items():
        for reference in approval["candidate_refs"]:
            ref_owners.setdefault(reference, set()).add(requirement_id)
    reused_refs = {
        reference for reference, owners in ref_owners.items() if len(owners) > 1
    }
    planned = []
    unresolved = deepcopy(structural_unresolved)
    merchandise = deposit = payable = packages = 0
    payable_known = True
    excess = Fraction(0)
    for requirement in requirements:
        requirement_id = requirement["requirement_id"]
        observation = observations.get(requirement_id)
        item = deepcopy(requirement)
        if not isinstance(observation, Mapping):
            unresolved.append({"requirement_id": requirement_id, "item": requirement["item"], "reason": "provider_search_unavailable"})
            item["status"] = "needs_input"
            planned.append(item)
            continue
        item["observation"] = _canonical_observation(observation)
        if hard_constraints:
            unresolved.append({
                "requirement_id": requirement_id,
                "item": requirement["item"],
                "reason": "hard_product_constraints_unverified",
            })
            item["status"] = "needs_input"
            planned.append(item)
            continue
        approval = approvals.get(requirement_id)
        if approval is None:
            unresolved.append({"requirement_id": requirement_id, "item": requirement["item"], "reason": "exact_candidate_scope_needs_user_approval"})
            item["status"] = "needs_input"
            planned.append(item)
            continue
        item["candidate_approval"] = deepcopy(approval)
        if reused_refs.intersection(approval["candidate_refs"]):
            unresolved.append({
                "requirement_id": requirement_id,
                "item": requirement["item"],
                "reason": "candidate_ref_reused_across_requirements",
            })
            item["status"] = "needs_input"
            planned.append(item)
            continue
        selection, reason, eligible_count = _select_requirement(requirement, observation, approval)
        if reason == "candidate_price_or_eligibility_unresolved" and price_mode == "estimate":
            estimated = _estimated_single_product(requirement, observation, approval)
            if estimated is not None:
                selection, reason, eligible_count = estimated, None, 1
        item["eligible_candidate_count"] = eligible_count
        if reason is not None:
            unresolved.append({"requirement_id": requirement_id, "item": requirement["item"], "reason": reason})
            item["status"] = "needs_input"
        else:
            item["status"] = "selected"
            item["selection"] = selection
            selection["surplus_quantity"] = _fraction_json(_read_fraction(selection["coverage"]) - _read_fraction(selection["required"]))
            merchandise += selection["merchandise_ore"]
            if selection["total_payable_ore"] is None:
                payable_known = False
            else:
                deposit += selection["mandatory_deposit_ore"]
                payable += selection["total_payable_ore"]
            packages += selection["package_count"]
            excess += _read_fraction(selection["excess_score"])
        planned.append(item)
    status = "prepared" if (requirements or ingredient_decisions) and not unresolved else "needs_input"
    plan: dict[str, Any] = {
        "product_plan_version": PRODUCT_PLAN_VERSION,
        "provider": provider,
        "binding": deepcopy(dict(binding)),
        "hard_product_constraints": hard_constraints,
        "ingredient_decisions": deepcopy(ingredient_decisions or []),
        "budget_ore": budget_ore,
        "price_mode": price_mode,
        "cost_status": "exact_product_payable" if payable_known and status == "prepared" else "merchandise_estimate_only" if status == "prepared" else "unresolved",
        "status": status,
        "scope": {
            "search_semantics": "bounded_relevance_ranked",
            "candidate_semantics": "exact_current_user_approved_refs_per_requirement",
            "maximum_requirements": MAX_REQUIREMENTS,
            "maximum_candidates_per_requirement": MAX_CANDIDATES_PER_REQUIREMENT,
            "maximum_combinations_per_requirement": MAX_COMBINATIONS,
        },
        "requirements": planned,
        "unresolved_requirements": unresolved,
        "comparison_claim": (
            f"lowest verified total payable amount among the approved, exactly priced candidates observed for {len(requirements)} bounded {provider.upper()} searches"
            if status == "prepared" and payable_known else None
        ),
        "excluded_costs": ["delivery", "cart_level_bags", "cart_level_fees", "checkout_price_drift"],
    }
    if status == "prepared":
        plan["totals"] = {
            "merchandise_ore": merchandise,
            "mandatory_deposit_ore": deposit if payable_known else None,
            "total_payable_ore": payable if payable_known else None,
            "excess_score": _fraction_json(excess),
            "package_count": packages,
        }
        plan["budget_status"] = "not_set" if budget_ore is None else "exceeded" if merchandise + deposit > budget_ore else "unverified" if not payable_known else "within_budget"
        if plan["budget_status"] == "exceeded":
            plan["status"] = "needs_input"
            plan["unresolved_requirements"].append({"reason": "product_budget_exceeded", "budget_ore": budget_ore, "known_minimum_ore": merchandise + deposit, "total_payable_ore": payable if payable_known else None})
    plan["product_plan_digest"] = product_plan_digest(plan)
    return plan


def validate_product_plan(value: Any, supplied_digest: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HouseholdError("apply needs the complete server-returned product_plan")
    plan = deepcopy(dict(value))
    digest = plan.get("product_plan_digest")
    if (
        not isinstance(supplied_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", supplied_digest)
        or digest != supplied_digest or product_plan_digest(plan) != supplied_digest
    ):
        raise HouseholdError("product_plan payload or digest changed")
    if plan.get("product_plan_version") != PRODUCT_PLAN_VERSION or plan.get("status") != "prepared":
        raise HouseholdError("only one complete prepared product_plan can be applied")
    return plan


def cart_requirements(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    quantities: dict[str | int, dict[str, Any]] = {}
    for requirement in plan.get("requirements", []):
        selection = requirement.get("selection") if isinstance(requirement, Mapping) else None
        if not isinstance(selection, Mapping):
            raise HouseholdError("prepared product plan has an incomplete selection")
        for product in selection.get("products", []):
            if not isinstance(product, Mapping):
                raise HouseholdError("prepared product plan product is invalid")
            reference = product.get("product_ref")
            quantity = product.get("quantity")
            name = product.get("name")
            if (
                not _valid_product_ref(reference) or not isinstance(name, str) or not name
                or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1
            ):
                raise HouseholdError("prepared product plan product is invalid")
            current = quantities.setdefault(reference, {"product_id": reference, "product_name": name, "quantity": 0})
            if current["product_name"] != name:
                raise HouseholdError("prepared product plan product name conflicts")
            current["quantity"] += quantity
    return [quantities[reference] for reference in sorted(quantities, key=_ref_sort_key)]
