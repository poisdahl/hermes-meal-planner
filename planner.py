"""Deterministic, bounded whole-week menu selection."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
from itertools import permutations
import json
import math
import re
from typing import Any, Mapping
import unicodedata

from core import HouseholdError


PLANNER_VERSION = "weekly-menu-v1"
MAX_CANDIDATES = 12
MAX_DAYS = 7
MAX_ALTERNATIVES = 3
MAX_EXPLORED_STATES = 250_000
MAX_HISTORY_RECORDS = 2_000
MAX_FACT_TOKEN = 80

SUPPORTED_STRICT_TARGETS = {
    "active_minutes",
    "minimum_fish_portions",
    "minimum_legume_dinners",
    "minimum_wholegrain_or_potato_dinners",
    "minimum_vegetable_types",
}
DIETARY_FACETS = {"fish", "legume", "wholegrain_or_potato", "vegetable"}
PERISHABILITY = {"fresh", "shelf_stable", "unknown"}

FISH_TERMS = {
    "ansjos", "fisk", "hyse", "kveite", "laks", "makrell", "ørret", "sardiner",
    "sei", "sild", "torsk", "tunfisk",
}
LEGUME_TERMS = {
    "bønne", "bønner", "erte", "erter", "kikerter", "linse", "linser", "soyabønner",
}
WHOLEGRAIN_OR_POTATO_TERMS = {
    "bygg", "fullkorn", "fullkornsris", "grov pasta", "havre", "potet", "poteter",
    "quinoa", "rug",
}
VEGETABLE_TERMS = {
    "agurk", "aubergine", "blomkål", "brokkoli", "gulrot", "grønnkål", "kål",
    "løk", "paprika", "pastinakk", "purre", "selleri", "spinat", "squash", "tomat",
}


class PlannerError(HouseholdError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _text(value: Any) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))
    ).strip().casefold()


def _bounded_token(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PlannerError(f"{field} must be text")
    result = _text(value)
    if not result or len(result) > MAX_FACT_TOKEN or any(
        0xD800 <= ord(character) <= 0xDFFF for character in result
    ):
        raise PlannerError(f"{field} must be bounded text")
    return result


def _source(value: Any, field: str, allowed: set[str]) -> str:
    if not isinstance(value, Mapping) or set(value).difference({"source", *allowed}):
        raise PlannerError(f"{field} has unknown fields")
    if value.get("source") != "explicit":
        raise PlannerError(f"{field}.source must be explicit")
    return "explicit"


def _normalize_token_list(
    value: Any, field: str, *, allowed: set[str] | None = None, maximum: int = 50
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PlannerError(f"{field} must be a bounded list")
    result = sorted({_bounded_token(item, field) for item in value})
    if allowed is not None and any(item not in allowed for item in result):
        raise PlannerError(f"{field} contains an unsupported value")
    return result


def normalize_candidate_facts(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or set(value).difference(
        {"active_minutes", "dietary_facets", "variety_facets", "perishability"}
    ):
        raise PlannerError("candidate facts have unknown fields")
    result: dict[str, Any] = {}
    if "active_minutes" in value:
        raw = value["active_minutes"]
        _source(raw, "facts.active_minutes", {"value"})
        minutes = raw.get("value")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1_440:
            raise PlannerError("facts.active_minutes.value must be an integer from zero to 1440")
        result["active_minutes"] = {"source": "explicit", "value": minutes}
    if "dietary_facets" in value:
        raw = value["dietary_facets"]
        _source(raw, "facts.dietary_facets", {"values", "complete", "vegetable_types"})
        if not isinstance(raw.get("complete"), bool):
            raise PlannerError("facts.dietary_facets.complete must be true or false")
        result["dietary_facets"] = {
            "source": "explicit",
            "values": _normalize_token_list(
                raw.get("values"), "facts.dietary_facets.values", allowed=DIETARY_FACETS
            ),
            "complete": raw["complete"],
            "vegetable_types": _normalize_token_list(
                raw.get("vegetable_types", []),
                "facts.dietary_facets.vegetable_types",
                maximum=50,
            ),
        }
    if "variety_facets" in value:
        raw = value["variety_facets"]
        _source(raw, "facts.variety_facets", {"values"})
        result["variety_facets"] = {
            "source": "explicit",
            "values": _normalize_token_list(
                raw.get("values"), "facts.variety_facets.values", maximum=20
            ),
        }
    if "perishability" in value:
        raw = value["perishability"]
        _source(raw, "facts.perishability", {"value"})
        perishability = raw.get("value")
        if perishability not in PERISHABILITY:
            raise PlannerError(
                "facts.perishability.value must be fresh, shelf_stable or unknown"
            )
        result["perishability"] = {"source": "explicit", "value": perishability}
    return result


def _ingredient_identities(recipe: Mapping[str, Any]) -> list[tuple[str, str | None, bool]]:
    result = []
    values = recipe.get("ingredients")
    if not isinstance(values, list):
        return result
    for value in values:
        if not isinstance(value, Mapping):
            continue
        item = _text(value.get("item"))
        if not item:
            continue
        unit = _text(value.get("unit")) or None
        result.append((item, unit, bool(value.get("pantry") or value.get("optional"))))
    return result


def _contains_term(identity: str, terms: set[str]) -> bool:
    words = set(re.findall(r"[^\W\d_]+", identity, flags=re.UNICODE))
    return identity in terms or bool(words.intersection(terms)) or any(
        " " in term and term in identity for term in terms
    )


def _derived_dietary(recipe: Mapping[str, Any]) -> dict[str, Any]:
    facets: set[str] = set()
    vegetables: set[str] = set()
    for identity, _unit, excluded in _ingredient_identities(recipe):
        if excluded:
            continue
        if _contains_term(identity, FISH_TERMS):
            facets.add("fish")
        if _contains_term(identity, LEGUME_TERMS):
            facets.add("legume")
        if _contains_term(identity, WHOLEGRAIN_OR_POTATO_TERMS):
            facets.add("wholegrain_or_potato")
        if _contains_term(identity, VEGETABLE_TERMS):
            facets.add("vegetable")
            vegetables.add(identity)
    return {
        "source": f"derived:{PLANNER_VERSION}:ingredient-facets",
        "values": sorted(facets),
        "complete": False,
        "vegetable_types": sorted(vegetables),
    }


def _derived_active_minutes(recipe: Mapping[str, Any]) -> dict[str, Any]:
    times = recipe.get("times")
    if isinstance(times, Mapping):
        value = times.get("active_minutes")
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_440:
            return {"source": "structured_recipe", "value": value}
    return {"source": "unknown", "value": None}


def _derived_variety(recipe: Mapping[str, Any]) -> dict[str, Any]:
    identities = [
        identity for identity, _unit, excluded in _ingredient_identities(recipe) if not excluded
    ]
    return {
        "source": f"derived:{PLANNER_VERSION}:first-ingredient",
        "values": identities[:1],
    }


def _effective_facts(recipe: Mapping[str, Any], supplied: Any) -> dict[str, Any]:
    facts = normalize_candidate_facts(supplied)
    return {
        # V1 has no server-owned allergen evidence source. Caller assertions
        # cannot turn an unknown hard constraint into a pass.
        "safety": {
            "source": "unknown", "allergies_or_sensitivities": {}, "avoid": {},
        },
        "active_minutes": facts.get("active_minutes", _derived_active_minutes(recipe)),
        "dietary_facets": facts.get("dietary_facets", _derived_dietary(recipe)),
        "variety_facets": facts.get("variety_facets", _derived_variety(recipe)),
        "perishability": facts.get(
            "perishability", {"source": "unknown", "value": "unknown"}
        ),
    }


def _profile_rules(profile: Mapping[str, Any], field: str) -> list[str]:
    diet = profile.get("diet")
    values = diet.get(field, []) if isinstance(diet, Mapping) else []
    if not isinstance(values, list) or len(values) > 50:
        raise PlannerError(f"profile diet.{field} must be a bounded list")
    return sorted({_bounded_token(item, f"profile diet.{field}") for item in values})


def _hard_evaluation(
    candidate: Mapping[str, Any], profile: Mapping[str, Any], overrides: Mapping[str, str]
) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    status = "pass"
    error = candidate.get("materialization_error")
    if error:
        status = "fail"
        reasons.append({"code": "not_materializable", "status": "fail", "detail": str(error)[:500]})
    safety = candidate["facts"]["safety"]
    for field in ("allergies_or_sensitivities", "avoid"):
        supplied = safety.get(field) if isinstance(safety, Mapping) else {}
        for rule in _profile_rules(profile, field):
            value = supplied.get(rule, "unknown") if isinstance(supplied, Mapping) else "unknown"
            reason_status = "pass" if value == "free" else "fail" if value == "contains" else "unknown"
            reasons.append({
                "code": f"safety:{field}",
                "status": reason_status,
                "detail": {"rule": rule, "evidence": safety.get("source", "unknown")},
            })
            if reason_status == "fail":
                status = "fail"
            elif reason_status == "unknown" and status == "pass":
                status = "unknown"
    usage = candidate.get("usage")
    eligible = bool(usage.get("eligible")) if isinstance(usage, Mapping) else False
    key = str(candidate["recipe_key"])
    if eligible:
        reasons.append({"code": "cooldown", "status": "pass", "detail": "eligible"})
    elif key in overrides:
        reasons.append({
            "code": "cooldown_override", "status": "pass",
            "detail": {"recipe_key": key, "reason": overrides[key]},
        })
    else:
        status = "fail"
        reasons.append({
            "code": "cooldown", "status": "fail",
            "detail": deepcopy(usage.get("blocked_by", [])) if isinstance(usage, Mapping) else "unknown",
        })
    return {"status": status, "reasons": reasons}


def _positive_int(profile: Mapping[str, Any], field: str, default: int) -> int:
    diet = profile.get("diet")
    value = diet.get(field, default) if isinstance(diet, Mapping) else default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlannerError(f"profile diet.{field} must be a non-negative integer")
    return value


def _active_window(profile: Mapping[str, Any]) -> tuple[int, int, int]:
    meals = profile.get("meals")
    if not isinstance(meals, Mapping):
        raise PlannerError("profile meals are invalid")
    target = meals.get("target_active_minutes", [0, 1_440])
    maximum = meals.get("maximum_active_minutes", 1_440)
    if (
        not isinstance(target, list) or len(target) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in target)
        or not 0 <= target[0] <= target[1] <= 1_440
        or isinstance(maximum, bool) or not isinstance(maximum, int)
        or not target[1] <= maximum <= 1_440
    ):
        raise PlannerError("profile active-minute targets are invalid")
    return target[0], target[1], maximum


def _strict_evaluation(
    selected: tuple[Mapping[str, Any], ...], strict_targets: list[str], profile: Mapping[str, Any]
) -> dict[str, Any]:
    results = []
    overall = "pass"
    dietary = [candidate["facts"]["dietary_facets"] for candidate in selected]
    incomplete = any(not facts["complete"] for facts in dietary)
    for target in strict_targets:
        if target == "active_minutes":
            low, high, maximum = _active_window(profile)
            values = [candidate["facts"]["active_minutes"]["value"] for candidate in selected]
            if any(value is None for value in values):
                result = {"target": target, "status": "unknown", "detail": "active minutes are missing"}
            elif any(not low <= value <= high for value in values):
                result = {
                    "target": target, "status": "fail",
                    "detail": {"target_range": [low, high], "soft_maximum": maximum, "values": values},
                }
            else:
                result = {
                    "target": target, "status": "pass",
                    "detail": {"target_range": [low, high], "values": values},
                }
        elif target == "minimum_vegetable_types":
            wanted = _positive_int(profile, target, 0)
            values = sorted({item for facts in dietary for item in facts["vegetable_types"]})
            if len(values) >= wanted:
                result = {"target": target, "status": "pass", "detail": {"minimum": wanted, "observed": values}}
            elif incomplete:
                result = {"target": target, "status": "unknown", "detail": {"minimum": wanted, "observed": values}}
            else:
                result = {"target": target, "status": "fail", "detail": {"minimum": wanted, "observed": values}}
        else:
            facet = {
                "minimum_fish_portions": "fish",
                "minimum_legume_dinners": "legume",
                "minimum_wholegrain_or_potato_dinners": "wholegrain_or_potato",
            }[target]
            wanted = _positive_int(profile, target, 0)
            observed = sum(facet in facts["values"] for facts in dietary)
            if observed >= wanted:
                result = {"target": target, "status": "pass", "detail": {"minimum": wanted, "observed": observed}}
            elif wanted > len(selected):
                result = {"target": target, "status": "fail", "detail": {"minimum": wanted, "observed": observed, "maximum_possible": len(selected)}}
            elif incomplete:
                result = {"target": target, "status": "unknown", "detail": {"minimum": wanted, "observed": observed}}
            else:
                result = {"target": target, "status": "fail", "detail": {"minimum": wanted, "observed": observed}}
        results.append(result)
        if result["status"] == "fail":
            overall = "fail"
        elif result["status"] == "unknown" and overall == "pass":
            overall = "unknown"
    return {"status": overall, "results": results}


def _reason(code: str, weight: int, detail: Any) -> dict[str, Any]:
    return {"code": code, "weight": int(weight), "detail": detail}


def _slot_reasons(
    candidate: Mapping[str, Any], day: str, index: int, count: int, profile: Mapping[str, Any]
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    facets = candidate["facts"]["dietary_facets"]["values"]
    weights = {"fish": 9, "legume": 8, "wholegrain_or_potato": 5, "vegetable": 3}
    for facet in sorted(facets):
        reasons.append(_reason(f"dietary:{facet}", weights[facet], "positive structured evidence"))
    active = candidate["facts"]["active_minutes"]
    low, high, maximum = _active_window(profile)
    value = active["value"]
    weekday = date.fromisoformat(day).weekday() < 5
    if value is None:
        reasons.append(_reason("active_minutes:unknown", 0, "not scored"))
    elif low <= value <= high:
        reasons.append(_reason(
            "active_minutes:weekday_target" if weekday else "active_minutes:target",
            8, {"value": value, "range": [low, high], "weekday": weekday},
        ))
    elif value <= maximum:
        reasons.append(_reason(
            "active_minutes:weekday_outside_target" if weekday else "active_minutes:weekend_capacity",
            -2 if weekday else 2,
            {"value": value, "range": [low, high], "weekday": weekday},
        ))
    else:
        reasons.append(_reason("active_minutes:above_maximum", -12, {"value": value, "maximum": maximum}))
    perishable = candidate["facts"]["perishability"]["value"]
    if perishable == "fresh":
        reasons.append(_reason("perishability:fresh_early", (count - index - 1) * 2, {"day": day}))
    elif perishable == "shelf_stable":
        reasons.append(_reason("perishability:shelf_stable_late", index, {"day": day}))
    else:
        reasons.append(_reason("perishability:unknown", 0, "not scored"))
    usage = candidate.get("usage") if isinstance(candidate.get("usage"), Mapping) else {}
    if not any(usage.get(field) for field in ("last_planned_week", "last_ordered_week", "last_cooked_week")):
        reasons.append(_reason("recency:no_recorded_use", 5, "no matching recorded use"))
    else:
        weeks_since = None
        recorded_weeks = []
        for field in ("last_cooked_week", "last_ordered_week", "last_planned_week"):
            recorded_week = usage.get(field)
            if not isinstance(recorded_week, str) or re.fullmatch(r"\d{4}-W\d{2}", recorded_week) is None:
                continue
            try:
                monday = date.fromisocalendar(int(recorded_week[:4]), int(recorded_week[6:]), 1)
            except ValueError:
                continue
            recorded_weeks.append((monday, recorded_week))
        latest = max(recorded_weeks, default=None)
        last_week = latest[1] if latest is not None else None
        if latest is not None:
            last_monday = latest[0]
            weeks_since = max(0, (date.fromisoformat(day) - last_monday).days // 7)
        cooldown = usage.get("cooldown_weeks")
        weight = (
            min(5, max(0, weeks_since - cooldown))
            if isinstance(weeks_since, int) and isinstance(cooldown, int) else 0
        )
        reasons.append(_reason("recency:recorded_use", weight, {
            "last_week": last_week, "weeks_since": weeks_since, "cooldown_weeks": cooldown,
        }))
    return reasons


def _plan_reasons(
    selected: tuple[Mapping[str, Any], ...], profile: Mapping[str, Any]
) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    variety = [
        value for candidate in selected for value in candidate["facts"]["variety_facets"]["values"]
    ]
    distinct = len(set(variety))
    duplicates = max(0, len(variety) - distinct)
    reasons.append(_reason("variety:distinct_facets", distinct * 3, {"distinct": distinct}))
    if duplicates:
        reasons.append(_reason("variety:monotony", -10 * duplicates, {"duplicates": duplicates}))

    ingredient_counts: dict[tuple[str, str], int] = {}
    for candidate in selected:
        identities = {
            (identity, unit)
            for identity, unit, excluded in _ingredient_identities(candidate["recipe"])
            if not excluded and unit is not None
        }
        for identity, unit in identities:
            ingredient_counts[(identity, unit)] = ingredient_counts.get((identity, unit), 0) + 1
    reusable = sorted(
        {f"{identity}|{unit}": count for (identity, unit), count in ingredient_counts.items() if count > 1}.items()
    )
    reuse_weight = min(16, sum(min(count - 1, 2) * 4 for _key, count in reusable))
    reasons.append(_reason("ingredients:exact_reuse", reuse_weight, dict(reusable)))
    monotony = min(24, sum(max(0, count - 2) * 6 for _key, count in reusable))
    if monotony:
        reasons.append(_reason("ingredients:monotony", -monotony, dict(reusable)))

    dietary = [candidate["facts"]["dietary_facets"] for candidate in selected]
    for target, facet in (
        ("minimum_fish_portions", "fish"),
        ("minimum_legume_dinners", "legume"),
        ("minimum_wholegrain_or_potato_dinners", "wholegrain_or_potato"),
    ):
        wanted = _positive_int(profile, target, 0)
        observed = sum(facet in facts["values"] for facts in dietary)
        met = observed >= wanted
        reasons.append(_reason(
            f"weekly_target:{target}", 10 if met else -min(10, (wanted - observed) * 3),
            {"minimum": wanted, "positive_evidence": observed, "met_by_positive_evidence": met},
        ))
    wanted_vegetables = _positive_int(profile, "minimum_vegetable_types", 0)
    vegetables = sorted({item for facts in dietary for item in facts["vegetable_types"]})
    reasons.append(_reason(
        "weekly_target:minimum_vegetable_types",
        10 if len(vegetables) >= wanted_vegetables else -min(10, wanted_vegetables - len(vegetables)),
        {"minimum": wanted_vegetables, "positive_evidence": vegetables,
         "met_by_positive_evidence": len(vegetables) >= wanted_vegetables},
    ))
    return reasons


def _selection(
    selected: tuple[Mapping[str, Any], ...], dates: list[str], profile: Mapping[str, Any],
    input_digest: str, scope: list[dict[str, Any]], strict: Mapping[str, Any], portions: int,
) -> dict[str, Any]:
    slots = []
    for index, (candidate, day) in enumerate(zip(selected, dates, strict=True)):
        reasons = _slot_reasons(candidate, day, index, len(dates), profile)
        slots.append({
            "date": day,
            "reference": deepcopy(candidate["reference"]),
            "reference_key": candidate["reference_key"],
            "recipe_key": candidate["recipe_key"],
            "content_digest": candidate["content_digest"],
            "name": str(candidate["recipe"].get("name") or "")[:300],
            "portions": portions,
            "hard_constraints": deepcopy(candidate["hard_constraints"]),
            "reason_contributions": reasons,
            "score": sum(reason["weight"] for reason in reasons),
        })
    plan_reasons = _plan_reasons(selected, profile)
    relaxations = {
        item
        for candidate in selected
        for item in (
            ["active_minutes"] if candidate["facts"]["active_minutes"]["value"] is None else []
        ) + (
            ["perishability"] if candidate["facts"]["perishability"]["value"] == "unknown" else []
        ) + (
            ["dietary_completeness"] if not candidate["facts"]["dietary_facets"]["complete"] else []
        )
    }
    diet = profile.get("diet") if isinstance(profile.get("diet"), Mapping) else {}
    cuisine = profile.get("cuisine") if isinstance(profile.get("cuisine"), Mapping) else {}
    if any(diet.get(field) for field in ("patterns", "fish_grams_per_person", "plate", "nutrition")):
        relaxations.add("nutrition_detail")
    if diet.get("leafy_green_days"):
        relaxations.add("leafy_green_days")
    if any(cuisine.get(field) for field in ("base_style", "wanted", "flavours", "quality")):
        relaxations.add("cuisine_and_format_preferences")
    payload = {
        "candidate_scope": scope,
        "slots": slots,
        "strict_targets": deepcopy(strict),
        "soft_relaxations": sorted(relaxations),
        "plan_reason_contributions": plan_reasons,
        "total_score": sum(slot["score"] for slot in slots)
        + sum(reason["weight"] for reason in plan_reasons),
        "tie_break": [slot["reference_key"] for slot in slots],
    }
    payload["selection_digest"] = digest({
        "planner_version": PLANNER_VERSION,
        "input_digest": input_digest,
        "selection": payload,
    })
    return payload


def _validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value).difference({
        "week", "dates", "portions", "candidates", "strict_targets",
        "cooldown_overrides", "alternatives", "as_of_date",
    }):
        raise PlannerError("planner input has unknown fields")
    week = str(value.get("week") or "")
    if re.fullmatch(r"\d{4}-W\d{2}", week) is None:
        raise PlannerError("planner week must use YYYY-Www")
    try:
        week_year, week_number = int(week[:4]), int(week[6:])
        date.fromisocalendar(week_year, week_number, 1)
    except ValueError as exc:
        raise PlannerError("planner week is invalid") from exc
    raw_dates = value.get("dates")
    if not isinstance(raw_dates, list) or not 1 <= len(raw_dates) <= MAX_DAYS:
        raise PlannerError(f"planner dates must contain one to {MAX_DAYS} dates")
    dates = []
    for raw in raw_dates:
        if not isinstance(raw, str):
            raise PlannerError("planner dates must be ISO dates")
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise PlannerError("planner dates must be ISO dates") from exc
        iso = parsed.isocalendar()
        if (iso.year, iso.week) != (week_year, week_number):
            raise PlannerError("every planner date must belong to planner week")
        dates.append(parsed.isoformat())
    dates = sorted(dates)
    if len(set(dates)) != len(dates):
        raise PlannerError("planner dates must be unique")
    portions = value.get("portions")
    if isinstance(portions, bool) or not isinstance(portions, int) or not 1 <= portions <= 100:
        raise PlannerError("planner portions must be an integer from one to 100")
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise PlannerError(f"planner candidates must contain one to {MAX_CANDIDATES} entries")
    strict = value.get("strict_targets", [])
    if not isinstance(strict, list) or len(strict) > len(SUPPORTED_STRICT_TARGETS):
        raise PlannerError("strict_targets must be a bounded list")
    strict = sorted(set(strict))
    if any(not isinstance(item, str) or item not in SUPPORTED_STRICT_TARGETS for item in strict):
        raise PlannerError("strict_targets contains an unsupported target")
    raw_overrides = value.get("cooldown_overrides", {})
    if not isinstance(raw_overrides, Mapping) or len(raw_overrides) > MAX_CANDIDATES:
        raise PlannerError("cooldown_overrides must be a bounded object")
    overrides = {}
    for key, reason in raw_overrides.items():
        if not isinstance(key, str) or not 1 <= len(key) <= 1_024:
            raise PlannerError("cooldown override recipe keys are invalid")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise PlannerError("cooldown override reasons must be bounded text")
        overrides[key] = reason.strip()
    alternatives = value.get("alternatives", 1)
    if isinstance(alternatives, bool) or not isinstance(alternatives, int) or not 1 <= alternatives <= MAX_ALTERNATIVES:
        raise PlannerError(f"alternatives must be from one to {MAX_ALTERNATIVES}")
    as_of_date = value.get("as_of_date")
    if not isinstance(as_of_date, str):
        raise PlannerError("as_of_date must be an ISO date")
    try:
        as_of_date = date.fromisoformat(as_of_date).isoformat()
    except ValueError as exc:
        raise PlannerError("as_of_date must be an ISO date") from exc
    return {
        "planner_version": PLANNER_VERSION,
        "week": week,
        "dates": dates,
        "portions": portions,
        "candidates": deepcopy(candidates),
        "strict_targets": strict,
        "cooldown_overrides": dict(sorted(overrides.items())),
        "alternatives": alternatives,
        "as_of_date": as_of_date,
    }


def plan_week(
    request: Any, *, profile: Mapping[str, Any], candidates: list[Mapping[str, Any]],
    history: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a byte-stable ranking for already-resolved exact candidates."""
    checked = _validate_request(request)
    if not isinstance(history, Mapping) or len(history) > MAX_HISTORY_RECORDS:
        raise PlannerError(f"planner history exceeds {MAX_HISTORY_RECORDS} records")
    if len(candidates) != len(checked["candidates"]):
        raise PlannerError("resolved planner candidates do not match planner input")
    prepared = []
    reference_keys = set()
    for candidate in candidates:
        required = {
            "reference", "reference_key", "recipe", "recipe_key", "dedupe_key", "content_digest",
            "usage", "facts", "materialization_error",
        }
        if not isinstance(candidate, Mapping) or not required.issubset(candidate):
            raise PlannerError("resolved planner candidate is invalid")
        reference_key = str(candidate["reference_key"])
        if reference_key in reference_keys:
            raise PlannerError("planner candidates contain a duplicate exact reference")
        reference_keys.add(reference_key)
        item = deepcopy(dict(candidate))
        usage = item.get("usage")
        if isinstance(usage, Mapping):
            item["usage"] = deepcopy(dict(usage))
            blocked_by = item["usage"].get("blocked_by")
            if isinstance(blocked_by, list):
                item["usage"]["blocked_by"] = sorted(blocked_by, key=canonical)
        item["facts"] = _effective_facts(item["recipe"], item.get("facts"))
        item["hard_constraints"] = _hard_evaluation(
            item, profile, checked["cooldown_overrides"]
        )
        prepared.append(item)
    identity_owners: dict[tuple[str, str], str] = {}
    status_priority = {"pass": 0, "unknown": 1, "fail": 2}
    for item in sorted(prepared, key=lambda candidate: (
        status_priority[candidate["hard_constraints"]["status"]],
        candidate["reference_key"],
    )):
        identities = (
            ("recipe", str(item["dedupe_key"])),
            ("content", str(item["content_digest"])),
        )
        owner = next((identity_owners[key] for key in identities if key in identity_owners), None)
        if owner is not None:
            item["hard_constraints"]["status"] = "fail"
            item["hard_constraints"]["reasons"].append({
                "code": "duplicate_recipe_identity",
                "status": "fail",
                "detail": {"same_as_reference_key": owner},
            })
            continue
        for key in identities:
            identity_owners[key] = item["reference_key"]
    prepared.sort(key=lambda item: item["reference_key"])
    unknown_override_keys = set(checked["cooldown_overrides"]).difference(
        item["recipe_key"] for item in prepared
    )
    if unknown_override_keys:
        raise PlannerError("cooldown override does not name an exact candidate recipe key")
    unnecessary_override_keys = {
        item["recipe_key"] for item in prepared
        if item["recipe_key"] in checked["cooldown_overrides"]
        and isinstance(item.get("usage"), Mapping)
        and item["usage"].get("eligible") is True
    }
    if unnecessary_override_keys:
        raise PlannerError("cooldown override is valid only for a currently blocked candidate")

    public_request = {
        key: deepcopy(value) for key, value in checked.items() if key != "planner_version"
    }
    public_request["candidates"] = [
        {
            **deepcopy(item["reference"]),
            "facts": normalize_candidate_facts(item.get("supplied_facts")),
        }
        for item in prepared
    ]

    evaluations = [{
        "reference": deepcopy(item["reference"]),
        "reference_key": item["reference_key"],
        "recipe_key": item["recipe_key"],
        "dedupe_key": item["dedupe_key"],
        "content_digest": item["content_digest"],
        "facts": deepcopy(item["facts"]),
        "usage": deepcopy(item["usage"]),
        "hard_constraints": deepcopy(item["hard_constraints"]),
    } for item in prepared]
    canonical_input = {
        "planner_version": PLANNER_VERSION,
        "request": public_request,
        "profile": deepcopy(dict(profile)),
        "history": {
            str(key): deepcopy(value)
            for key, value in sorted(history.items(), key=lambda item: str(item[0]))
        },
        "candidates": evaluations,
    }
    input_digest = digest(canonical_input)
    base_result = {
        "planner_version": PLANNER_VERSION,
        "request": public_request,
        "input_digest": input_digest,
        "canonical_input": canonical_input,
        "candidate_evaluations": evaluations,
        "work_limits": {
            "maximum_candidates": MAX_CANDIDATES,
            "maximum_days": MAX_DAYS,
            "maximum_alternatives": MAX_ALTERNATIVES,
            "maximum_explored_states": MAX_EXPLORED_STATES,
            "maximum_history_records": MAX_HISTORY_RECORDS,
        },
    }
    eligible = [item for item in prepared if item["hard_constraints"]["status"] == "pass"]
    unknown = [item for item in prepared if item["hard_constraints"]["status"] == "unknown"]
    count = len(checked["dates"])
    if len(eligible) < count:
        status = "needs_input" if len(eligible) + len(unknown) >= count else "no_plan"
        return {
            **base_result,
            "status": status,
            "issues": [{
                "code": "insufficient_hard_constraint_candidates",
                "required": count,
                "eligible": len(eligible),
                "unknown": [item["reference_key"] for item in unknown],
            }],
            "selections": [],
        }
    explored_states = math.perm(len(eligible), count)
    if explored_states > MAX_EXPLORED_STATES:
        raise PlannerError(
            f"planner work limit exceeded: {explored_states} states exceeds {MAX_EXPLORED_STATES}"
        )
    scope = [{
        "reference": deepcopy(item["reference"]),
        "reference_key": item["reference_key"],
        "content_digest": item["content_digest"],
    } for item in prepared]
    ranked: list[dict[str, Any]] = []
    strict_unknowns: dict[str, dict[str, Any]] = {}
    strict_failures = 0
    for selected in permutations(eligible, count):
        if len({item["recipe_key"] for item in selected}) != count:
            continue
        if len({item["dedupe_key"] for item in selected}) != count:
            continue
        if len({item["content_digest"] for item in selected}) != count:
            continue
        strict = _strict_evaluation(selected, checked["strict_targets"], profile)
        if strict["status"] == "unknown":
            for item in strict["results"]:
                if item["status"] == "unknown":
                    strict_unknowns.setdefault(canonical(item), item)
            continue
        if strict["status"] == "fail":
            strict_failures += 1
            continue
        selection = _selection(
            selected, checked["dates"], profile, input_digest, scope, strict,
            checked["portions"],
        )
        ranked.append(selection)
        ranked.sort(key=lambda item: (-item["total_score"], item["tie_break"], item["selection_digest"]))
        del ranked[checked["alternatives"]:]
    if not ranked:
        if strict_unknowns:
            issues = sorted(strict_unknowns.values(), key=canonical)
            status = "needs_input"
        else:
            issues = [{"code": "strict_targets_infeasible", "evaluated": strict_failures}]
            status = "no_plan"
        return {**base_result, "status": status, "issues": issues, "selections": []}
    handoffs = [{
        "planner_version": PLANNER_VERSION,
        "input_digest": input_digest,
        "selection_digest": selection["selection_digest"],
        "request": deepcopy(public_request),
        "selection": deepcopy(selection),
    } for selection in ranked]
    return {
        **base_result,
        "status": "planned",
        "explored_states": explored_states,
        "selection": deepcopy(ranked[0]),
        "selection_digest": ranked[0]["selection_digest"],
        "selections": ranked,
        "save_handoff": deepcopy(handoffs[0]),
        "save_handoffs": handoffs,
    }
