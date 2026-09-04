"""Small, private-state core for one Hermes meal-planning household."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
from typing import Any, Callable, Iterator, Mapping
from zoneinfo import ZoneInfo


class HouseholdError(RuntimeError):
    pass


class CheckoutPreconditionError(HouseholdError):
    """Checkout stopped before the final provider control was dispatched."""


class CancellationPreconditionError(HouseholdError):
    """Cancellation stopped before the final provider control was dispatched."""


STATE_VERSION = 12

RECIPE_SOURCE_IDS = ("internal", "oda", "meny", "themealdb", "wikibooks")
DEFAULT_RECIPE_SOURCES = {source: True for source in RECIPE_SOURCE_IDS}


DEFAULT_PROFILE: dict[str, Any] = {
    "meals": {
        "dinner_days": 7,
        "people": 2,
        "guest_meals": 0,
        "portions": 2,
        "dishes": 7,
        "batch_dishes": 0,
        "salads": 0,
        "target_active_minutes": [15, 45],
        "maximum_active_minutes": 60,
        "dinner_time": "18:00",
        "cook_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "eat_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "leftovers": "Plan one different dinner for each dinner day; leftovers are optional lunches, not repeated dinners.",
        "storage": ["fridge"],
    },
    "cuisine": {
        "base_style": "Varied weekday cooking",
        "variation": "Use different main ingredients, formats and cuisines across the seven dinners.",
        "wanted": [],
        "flavours": [],
        "quality": "Complete, practical recipes with clear ingredients and steps.",
    },
    "diet": {
        "patterns": ["Norwegian dietary guidelines"],
        "allergies_or_sensitivities": [],
        "avoid": [],
        "prioritise": ["vegetables", "whole grains", "fish", "legumes"],
        "fish_grams_per_person": [300, 450],
        "minimum_fish_portions": 2,
        "leafy_green_days": [],
        "minimum_legume_dinners": 1,
        "minimum_wholegrain_or_potato_dinners": 2,
        "minimum_vegetable_types": 5,
        "plate": {"vegetables": 0.5, "protein": 0.25, "wholegrain_or_potato": 0.25},
        "nutrition": "Prefer balanced everyday meals and sensible portions.",
        "legumes": "Use cooked legumes.",
        "exceptions": [],
    },
    "products": {
        "priority": ["diet fit", "practical need", "price per amount", "quality"],
        "prefer_value_brands": [],
        "organic": "when requested or equally suitable",
        "local": "when requested or equally suitable",
        "brands": [],
        "offers": "use when compatible with preferences, shelf life and real need",
        "shelf_life": "buy larger packs only when later use or freezing is realistic",
        "processing": "prefer less processed when otherwise suitable",
    },
    "pantry": {
        "assume": ["salt", "pepper", "cooking oil"],
        "confirm_if_stale": True,
        "breakfast_context": [],
        "notes": [],
    },
    "recipes": {
        "repeat_cooldown_weeks": 6,
        "sources": deepcopy(DEFAULT_RECIPE_SOURCES),
    },
}


DEFAULT_SCHEDULE: dict[str, Any] = {
    "enabled": False,
    "weekday": "Thursday",
    "time": "15:00",
    "timezone": "Europe/Oslo",
    "mode": "draft",
    "delivery": {
        "weekday": "Saturday",
        "preferred_end": "15:00",
        "latest_end": "18:00",
        "strategy": "cheapest",
    },
    "maximum_total": None,
    "auto_checkout": False,
    "cron_job_id": None,
}


def valid_email_address(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= 254 and re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
        value,
    ) is not None


def initial_state(config: Mapping[str, Any]) -> dict[str, Any]:
    profile = deepcopy(DEFAULT_PROFILE)
    _merge(profile, config.get("profile_overrides", {}))
    validate_profile(profile)
    return {
        "version": STATE_VERSION,
        "household": str(config["household"]),
        "provider": str(config.get("provider") or "oda").casefold(),
        "profile": profile,
        "product_favorites": [],
        "recurring_items": [],
        "schedule": deepcopy(DEFAULT_SCHEDULE),
        "email_recipient": None,
        "menu": None,
        "cart_plan": None,
        "setup": {
            "version": 1,
            "status": "needs_review",
            "reviewed_at": None,
            "noninteractive_defaults_applied_at": None,
        },
        "pending_checkout": None,
        "delivery_selection": None,
        "pending_cancellation": None,
        "order_change": None,
        "email_jobs": [],
        "occurrences": {},
        "batch_outcomes": {"sources": {}, "leftovers": {}},
        "planning_feedback": [],
        "menu_planning": {"locks": {}, "history": {}, "retired": {}, "applied": {}, "outcomes": {}},
        "recipe_usage": {},
        "recipe_usage_requests": {},
        "order_snapshots": {},
        "order_snapshot_times": {},
        "order_snapshot_providers": {},
        "protected_results": {},
        "protected_requests": {},
    }


def _merge(target: dict[str, Any], changes: Mapping[str, Any]) -> None:
    if not isinstance(changes, Mapping):
        raise HouseholdError("changes must be an object")
    for key, value in changes.items():
        if key not in target:
            raise HouseholdError(f"unknown profile field: {key}")
        if isinstance(target[key], dict):
            if not isinstance(value, Mapping):
                raise HouseholdError(f"profile field {key} must be an object")
            _merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def validate_profile(profile: Mapping[str, Any]) -> None:
    """Validate editable household values at the write boundary."""
    def check(value: Any, default: Any, path: str) -> None:
        if isinstance(default, dict):
            if not isinstance(value, Mapping) or set(value) != set(default):
                raise HouseholdError(f"profile {path} has invalid fields")
            for key, child in default.items():
                check(value[key], child, f"{path}.{key}".strip("."))
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise HouseholdError(f"profile {path} must be true or false")
        elif isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise HouseholdError(f"profile {path} must be a non-negative finite number")
            if isinstance(default, int) and not isinstance(value, int):
                raise HouseholdError(f"profile {path} must be an integer")
        elif isinstance(default, str):
            if not isinstance(value, str) or len(value) > 4000:
                raise HouseholdError(f"profile {path} must be bounded text")
        elif isinstance(default, list):
            if not isinstance(value, list) or len(value) > 100:
                raise HouseholdError(f"profile {path} must be a bounded list")
            if path == "diet.leafy_green_days" and all(type(x) is int for x in value):
                if len(value) != len(set(value)) or any(not 1 <= x <= 7 for x in value):
                    raise HouseholdError("profile leafy_green_days must contain distinct day numbers from 1 to 7")
                return
            numeric = path in {"meals.target_active_minutes", "diet.fish_grams_per_person"}
            if numeric:
                if len(value) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x < 0 for x in value) or value[0] > value[1]:
                    raise HouseholdError(f"profile {path} must be an ordered pair of non-negative numbers")
            elif any(not isinstance(x, str) or not x.strip() or len(x) > 500 for x in value):
                raise HouseholdError(f"profile {path} must contain bounded non-empty text")
    check(profile, DEFAULT_PROFILE, "")
    meals = profile["meals"]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in meals["target_active_minutes"]) or meals["target_active_minutes"][1] > meals["maximum_active_minutes"]:
        raise HouseholdError("profile active-time targets must be integers within maximum_active_minutes")
    for field, minimum, maximum in (("people", 1, 100), ("portions", 1, 100), ("dinner_days", 0, 7), ("dishes", 0, 31), ("batch_dishes", 0, 31), ("salads", 0, 31), ("guest_meals", 0, 31), ("maximum_active_minutes", 1, 1440)):
        value = meals[field]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise HouseholdError(f"profile meals.{field} must be an integer from {minimum} to {maximum}")
    days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    for field in ("cook_days", "eat_days"):
        values = [x.casefold() for x in meals[field]]
        if len(values) != len(set(values)) or not set(values).issubset(days):
            raise HouseholdError(f"profile meals.{field} must contain distinct weekdays")
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", meals["dinner_time"]) is None:
        raise HouseholdError("profile dinner_time must use HH:MM")
    if any(not 0 <= value <= 1 for value in profile["diet"]["plate"].values()):
        raise HouseholdError("profile plate fractions must be between zero and one")


def recurring_schedule(value: Any, today: date) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not set(value).issubset({"every", "unit", "anchor"}):
        raise HouseholdError("recurring item schedule is invalid")
    result = deepcopy(dict(value))
    unit = result.get("unit")
    if result.get("anchor") is None:
        result["anchor"] = today.strftime("%G-W%V") if unit == "weeks" else today.strftime("%Y-%m")
    try:
        due_recurring({"schedule": result}, today)
    except (TypeError, ValueError) as exc:
        raise HouseholdError("recurring item schedule is invalid") from exc
    return result


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    try:
        encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HouseholdError("household state contains an invalid JSON value") from exc
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise OSError("state write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _normalized_identity(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _legacy_recipe_key(recipe: Mapping[str, Any]) -> str:
    source = recipe.get("source") if isinstance(recipe.get("source"), Mapping) else {}
    if source.get("publisher") and source.get("external_id"):
        return f"source:{_normalized_identity(source['publisher'])}:{_normalized_identity(source['external_id'])}"
    if source.get("url"):
        return f"source-url:{source['url']}"
    ingredients = recipe.get("ingredients") if isinstance(recipe.get("ingredients"), list) else []
    identity = {
        "name": _normalized_identity(recipe.get("name")),
        "ingredients": sorted(
            _normalized_identity(item.get("item") or item.get("name") if isinstance(item, Mapping) else item)
            for item in ingredients
        ),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"content:{hashlib.sha256(encoded).hexdigest()}"


def _menu_digest(menu: Mapping[str, Any]) -> str:
    content = deepcopy(dict(menu))
    for key in ("menu_id", "revision", "digest", "phase", "order_id"):
        content.pop(key, None)
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _upgrade_legacy_menu(menu: dict[str, Any]) -> None:
    for collection in ("dishes", "salads"):
        recipes = menu.get(collection)
        if not isinstance(recipes, list):
            continue
        for recipe in recipes:
            if not isinstance(recipe, dict):
                continue
            source_defaults = {
                "kind": "unknown", "publisher": None, "title": None, "author": None,
                "url": None, "external_id": None, "relationship": "unknown",
            }
            source = recipe.setdefault("source", {})
            if not isinstance(source, dict):
                source = recipe["source"] = {}
            for key, value in source_defaults.items():
                source.setdefault(key, value)
            rights_defaults = {"storage": "full", "license": None, "license_url": None, "credit": None}
            rights = recipe.setdefault("rights", {})
            if not isinstance(rights, dict):
                rights = recipe["rights"] = {}
            for key, value in rights_defaults.items():
                rights.setdefault(key, value)
            recipe.setdefault("recipe_key", _legacy_recipe_key(recipe))
    digest = _menu_digest(menu)
    menu.setdefault("menu_id", f"menu_legacy_{digest[:16]}")
    menu.setdefault("revision", 1)
    menu["digest"] = digest
    menu.setdefault("phase", "draft")


def _add_legacy_usage(state: dict[str, Any], menu: Mapping[str, Any]) -> None:
    keys = [
        recipe.get("recipe_key")
        for collection in ("dishes", "salads")
        for recipe in (menu.get(collection) if isinstance(menu.get(collection), list) else [])
        if isinstance(recipe, Mapping) and recipe.get("recipe_key")
    ]
    state["recipe_usage"].setdefault(menu["menu_id"], {
        "week": menu.get("week"),
        "status": "ordered" if menu.get("phase") == "ordered" else "planned",
        "recipe_keys": keys,
        "cooked_keys": [],
        "not_cooked_keys": [],
        "cooldown_overrides": {},
        "order_id": menu.get("order_id"),
    })


def _validate_product_items(value: Any, field: str) -> None:
    if not isinstance(value, list):
        raise HouseholdError(f"household {field} must be a list")
    for item in value:
        if not isinstance(item, Mapping):
            raise HouseholdError(f"household {field} item must be an object")
        item_key(item)
        product_name = item.get("product_name")
        quantity = item.get("quantity", 1)
        if not isinstance(product_name, str) or not product_name.strip() or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise HouseholdError(f"household {field} item requires a product name and positive integer quantity")


DELIVERY_SLOT_KEYS = {
    "slot_ref", "provider_slot_id", "start_at", "end_at",
    "price_ore", "price_kind", "selected",
}
RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?"
    r"(?:Z|[+-](?:(?:0\d|1[0-3]):[0-5]\d|14:00))$"
)


def validate_delivery_slot(value: Any) -> dict[str, Any]:
    """Return one exact, bounded provider-neutral delivery slot."""

    if not isinstance(value, Mapping) or set(value) != DELIVERY_SLOT_KEYS:
        raise HouseholdError("delivery slot does not match the normalized contract")
    slot = dict(value)
    reference = slot.get("slot_ref")
    if not isinstance(reference, str) or not reference or len(reference.encode("utf-8")) > 500:
        raise HouseholdError("delivery slot reference is invalid")
    provider_id = slot.get("provider_slot_id")
    if provider_id is not None and (
        isinstance(provider_id, bool)
        or not isinstance(provider_id, (str, int))
        or len(str(provider_id).encode("utf-8")) > 500
    ):
        raise HouseholdError("delivery provider slot id is invalid")
    timestamps = []
    for field in ("start_at", "end_at"):
        raw = slot.get(field)
        if (
            not isinstance(raw, str)
            or len(raw) > 64
            or RFC3339_TIMESTAMP.fullmatch(raw) is None
        ):
            raise HouseholdError("delivery slot timestamp is invalid")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HouseholdError("delivery slot timestamp is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise HouseholdError("delivery slot timestamp must include an offset")
        timestamps.append(parsed)
    if timestamps[1] <= timestamps[0]:
        raise HouseholdError("delivery slot must end after it starts")
    kind = slot.get("price_kind")
    price = slot.get("price_ore")
    if kind not in {"exact", "from", "unavailable"}:
        raise HouseholdError("delivery slot price kind is invalid")
    if kind == "unavailable":
        if price is not None:
            raise HouseholdError("unavailable delivery price must be null")
    elif isinstance(price, bool) or not isinstance(price, int) or price < 0:
        raise HouseholdError("delivery slot price must be non-negative integer ore")
    if not isinstance(slot.get("selected"), bool):
        raise HouseholdError("delivery slot selected state is invalid")
    return slot


def oslo_local_timestamp(day: date, clock: str) -> str:
    """Create an unambiguous Europe/Oslo RFC3339 timestamp."""

    if not isinstance(clock, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock) is None:
        raise HouseholdError("delivery slot local time is invalid")
    hour, minute = (int(part) for part in clock.split(":"))
    naive = datetime(day.year, day.month, day.day, hour, minute)
    zone = ZoneInfo("Europe/Oslo")
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        if candidate.astimezone(ZoneInfo("UTC")).astimezone(zone).replace(tzinfo=None) == naive:
            candidates.append(candidate)
    offsets = {candidate.utcoffset() for candidate in candidates}
    if len(offsets) != 1:
        raise HouseholdError("delivery slot local time is impossible or ambiguous")
    return candidates[0].isoformat()


def delivery_price_display(slot: Mapping[str, Any]) -> str:
    normalized = validate_delivery_slot(slot)
    if normalized["price_kind"] == "unavailable":
        return "pris ikke tilgjengelig"
    amount = normalized["price_ore"]
    whole, remainder = divmod(amount, 100)
    rendered = f"{whole} kr" if remainder == 0 else f"{whole},{remainder:02d} kr"
    return f"fra {rendered}" if normalized["price_kind"] == "from" else rendered


def delivery_candidate_digest(slots: list[Mapping[str, Any]]) -> str:
    normalized = [validate_delivery_slot(slot) for slot in slots]
    if len({slot["slot_ref"] for slot in normalized}) != len(normalized):
        raise HouseholdError("delivery slot references are not unique")
    candidates = sorted(
        ({**slot, "selected": False} for slot in normalized),
        key=lambda slot: slot["slot_ref"].encode("utf-8"),
    )
    encoded = json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def cheapest_delivery_slot(
    slots: list[Mapping[str, Any]],
    *,
    preferred_end: str | None,
    timezone_name: str = "Europe/Oslo",
) -> dict[str, Any]:
    normalized = [validate_delivery_slot(slot) for slot in slots]
    if not normalized:
        raise HouseholdError("no eligible delivery slots are available")
    if any(slot["price_kind"] != "exact" for slot in normalized):
        raise HouseholdError("eligible delivery prices are not all exact")
    preferred_minutes = None
    if preferred_end is not None:
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", preferred_end) is None:
            raise HouseholdError("preferred delivery end is invalid")
        preferred_minutes = int(preferred_end[:2]) * 60 + int(preferred_end[3:])

    zone = ZoneInfo(timezone_name)

    def rank(slot: Mapping[str, Any]) -> tuple[Any, ...]:
        end = datetime.fromisoformat(str(slot["end_at"]).replace("Z", "+00:00")).astimezone(zone)
        start = datetime.fromisoformat(str(slot["start_at"]).replace("Z", "+00:00")).astimezone(zone)
        end_minutes = end.hour * 60 + end.minute
        distance = abs(end_minutes - preferred_minutes) if preferred_minutes is not None else 0
        return (slot["price_ore"], distance, start, slot["slot_ref"].encode("utf-8"))

    return deepcopy(min(normalized, key=rank))


def _validate_delivery_selection(value: Any) -> None:
    if value is None:
        return
    required = {"provider", "scope", "origin", "slot", "candidate_digest", "observed_at"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise HouseholdError("household delivery selection observation is invalid")
    if value.get("provider") not in {"oda", "meny"} or value.get("origin") not in {"explicit", "cheapest"}:
        raise HouseholdError("household delivery selection observation is invalid")
    scope = value.get("scope")
    if not isinstance(scope, Mapping) or set(scope) != {"cart_id", "order_id", "occurrence"}:
        raise HouseholdError("household delivery selection scope is invalid")
    for identity in scope.values():
        if identity is not None and (not isinstance(identity, str) or not identity or len(identity) > 128):
            raise HouseholdError("household delivery selection scope is invalid")
    validate_delivery_slot(value.get("slot"))
    digest = value.get("candidate_digest")
    if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None):
        raise HouseholdError("household delivery candidate digest is invalid")
    observed = value.get("observed_at")
    if not isinstance(observed, str) or len(observed) > 64:
        raise HouseholdError("household delivery selection timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HouseholdError("household delivery selection timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HouseholdError("household delivery selection timestamp is invalid")


def _migrate_state(
    state: dict[str, Any],
    config: Mapping[str, Any],
    before_v6: Callable[[Mapping[str, Any]], None] | None = None,
    before_v7: Callable[[Mapping[str, Any]], None] | None = None,
    before_v8: Callable[[Mapping[str, Any]], None] | None = None,
    before_v9: Callable[[Mapping[str, Any]], None] | None = None,
    before_v10: Callable[[Mapping[str, Any]], None] | None = None,
    before_v11: Callable[[Mapping[str, Any]], None] | None = None,
    before_v12: Callable[[Mapping[str, Any]], None] | None = None,
) -> None:
    version = state.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HouseholdError("household state version is invalid")
    if version > 12:
        raise HouseholdError("household state is newer than this meal concierge")
    if version >= 6 and "favorites" in state:
        raise HouseholdError("household state contains the retired favorites key")
    if version == 1:
        profile = state.get("profile")
        if not isinstance(profile, dict):
            raise HouseholdError("household profile is invalid")
        profile.setdefault("recipes", deepcopy(DEFAULT_PROFILE["recipes"]))
        state.setdefault("recipe_usage", {})
        state.setdefault("recipe_usage_requests", {})
        state.setdefault("order_snapshots", {})
        state.setdefault("order_snapshot_times", {})
        state.setdefault("protected_results", {})
        state.setdefault("protected_requests", {})
        if not isinstance(state["recipe_usage"], dict) or not isinstance(state["recipe_usage_requests"], dict) or not isinstance(state["order_snapshots"], dict) or not isinstance(state["order_snapshot_times"], dict):
            raise HouseholdError("household recipe lifecycle state is invalid")
        menu = state.get("menu")
        if isinstance(menu, dict):
            _upgrade_legacy_menu(menu)
            _add_legacy_usage(state, menu)
            if menu.get("order_id"):
                state["order_snapshots"].setdefault(str(menu["order_id"]), deepcopy(menu))
        pending = state.get("pending_checkout")
        if isinstance(pending, dict) and isinstance(pending.get("menu"), dict):
            pending_menu = pending["menu"]
            _upgrade_legacy_menu(pending_menu)
            if isinstance(menu, Mapping) and pending_menu.get("digest") == menu.get("digest"):
                pending["menu"] = pending_menu = deepcopy(menu)
            _add_legacy_usage(state, pending_menu)
            pending.setdefault("menu_ref", {
                "menu_id": pending_menu["menu_id"],
                "revision": pending_menu["revision"],
                "digest": pending_menu["digest"],
            })
        recipient = state.get("email_recipient")
        for job in state.get("email_jobs", []):
            if not isinstance(job, dict):
                continue
            snapshot = state["order_snapshots"].get(str(job.get("order_id") or ""))
            if snapshot:
                job.setdefault("menu_snapshot", deepcopy(snapshot))
            if isinstance(recipient, str) and recipient:
                job.setdefault("recipient_snapshot", recipient)
        state["version"] = 2
    if state["version"] == 2:
        bound_provider = str(state.get("provider") or config.get("provider") or "").casefold()
        email_jobs = state.get("email_jobs")
        order_snapshots = state.get("order_snapshots")
        if not isinstance(email_jobs, list) or not isinstance(order_snapshots, dict):
            raise HouseholdError("household recipe lifecycle state is invalid")
        for job in email_jobs:
            if isinstance(job, dict):
                job.setdefault("provider", bound_provider)
        snapshot_providers = state.setdefault("order_snapshot_providers", {})
        if not isinstance(snapshot_providers, dict):
            raise HouseholdError("household order snapshot providers are invalid")
        for order_id, snapshot in order_snapshots.items():
            matching_providers = {
                job.get("provider")
                for job in email_jobs
                if isinstance(job, dict)
                and job.get("order_id") == order_id
                and isinstance(job.get("provider"), str)
                and job.get("provider") in {"oda", "meny"}
                and job.get("menu_snapshot") == snapshot
            }
            snapshot_providers.setdefault(
                order_id, next(iter(matching_providers)) if len(matching_providers) == 1 else bound_provider,
            )
        state["version"] = 3
    if state["version"] == 3:
        state.setdefault("cart_plan", None)
        state["version"] = 4
    if state["version"] == 4:
        profile = state.get("profile")
        recipes = profile.get("recipes") if isinstance(profile, dict) else None
        configured = config.get("profile_overrides") if isinstance(config.get("profile_overrides"), Mapping) else {}
        configured_recipes = configured.get("recipes") if isinstance(configured.get("recipes"), Mapping) else {}
        configured_sources = configured_recipes.get("sources") if isinstance(configured_recipes.get("sources"), Mapping) else {}
        if isinstance(recipes, dict) and "sources" not in recipes:
            recipes["sources"] = {**deepcopy(DEFAULT_RECIPE_SOURCES), **deepcopy(dict(configured_sources))}
        state.setdefault("setup", {
            "version": 1,
            "status": "needs_review",
            "reviewed_at": None,
            "noninteractive_defaults_applied_at": None,
        })
        state["version"] = 5
    if version < 5:
        state.setdefault("favorites", [])
    if state["version"] == 5:
        if before_v6 is not None:
            before_v6(state)
        old_items = state.get("favorites")
        _validate_product_items(old_items, "favorites")
        if "product_favorites" in state:
            new_items = state["product_favorites"]
            _validate_product_items(new_items, "product_favorites")
            old_encoded = json.dumps(old_items, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            new_encoded = json.dumps(new_items, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if old_encoded != new_encoded:
                raise HouseholdError("household v5 favorites conflict with product_favorites")
        state["product_favorites"] = deepcopy(old_items)
        del state["favorites"]
        state["version"] = 6
    if state["version"] == 6:
        if version < 6 and "schedule" not in state:
            state["schedule"] = deepcopy(DEFAULT_SCHEDULE)
        schedule = state.get("schedule")
        if version < 6 and isinstance(schedule, dict) and "delivery" not in schedule:
            schedule["delivery"] = deepcopy(DEFAULT_SCHEDULE["delivery"])
        delivery = schedule.get("delivery") if isinstance(schedule, Mapping) else None
        if not isinstance(delivery, dict):
            raise HouseholdError("household v6 delivery preference is invalid")
        if version < 6:
            delivery.pop("strategy", None)
            state.pop("delivery_selection", None)
        if "strategy" in delivery and delivery.get("strategy") != "keep_selected":
            raise HouseholdError("household v6 delivery strategy conflicts with the migration")
        if state.get("delivery_selection") is not None:
            raise HouseholdError("household v6 delivery selection conflicts with the migration")
        if before_v7 is not None:
            before_v7(state)
        delivery["strategy"] = "keep_selected"
        state["delivery_selection"] = None
        state["version"] = 7
    if state["version"] == 7:
        if before_v8 is not None:
            before_v8(state)
        for job in state.get("email_jobs", []):
            if not isinstance(job, dict):
                continue
            automation_key = job.get("automation_key")
            if isinstance(automation_key, str) and re.fullmatch(r"meal-planner-email-[a-f0-9]{16}", automation_key):
                job["automation_key"] = "meal-concierge-email-" + automation_key.rsplit("-", 1)[-1]
                job["automation_protocol"] = 0
        state["version"] = 8
    if state["version"] == 8:
        if "menu_planning" in state:
            raise HouseholdError("household v8 planning metadata conflicts with migration")
        if before_v9 is not None:
            before_v9(state)
        state["menu_planning"] = {"locks": {}, "history": {}, "retired": {}, "applied": {}, "outcomes": {}}
        state["version"] = 9
    if state["version"] == 9:
        if "planning_feedback" in state:
            raise HouseholdError("household v9 feedback conflicts with migration")
        if before_v10 is not None:
            before_v10(state)
        state["planning_feedback"] = []
        state["version"] = 10
    if state["version"] == 10:
        if "batch_outcomes" in state:
            raise HouseholdError("household v10 batch outcomes conflict with migration")
        if before_v11 is not None:
            before_v11(state)
        state["batch_outcomes"] = {"sources": {}, "leftovers": {}}
        state["version"] = 11
    if state["version"] == 11:
        if before_v12 is not None:
            before_v12(state)
        zone = ZoneInfo(str(state.get("schedule", {}).get("timezone") or "Europe/Oslo"))
        today = datetime.now(zone).date()
        for item in state.get("recurring_items", []):
            item["schedule"] = recurring_schedule(item.get("schedule"), today)
        pending = state.get("pending_checkout")
        if isinstance(pending, dict) and pending.get("status") == "awaiting_confirmation" and pending.get("occurrence") and "automatic_checkout" not in pending and state.get("schedule", {}).get("mode") == "cart_ready":
            pending["automatic_checkout"] = False
        state["version"] = 12
    batch = state.get("batch_outcomes")
    if not isinstance(batch, dict) or set(batch) != {"sources", "leftovers"} or any(not isinstance(v,dict) or len(v)>2000 for v in batch.values()):
        raise HouseholdError("household batch outcomes are invalid")
    if not isinstance(state.get("planning_feedback"), list) or len(state["planning_feedback"]) > 500:
        raise HouseholdError("household planning feedback is invalid")
    planning = state.get("menu_planning")
    if not isinstance(planning, dict) or set(planning) != {"locks", "history", "retired", "applied", "outcomes"} or any(not isinstance(v, dict) or len(v) > 2000 for v in planning.values()):
        raise HouseholdError("household planning metadata is invalid")
    _validate_product_items(state.get("product_favorites"), "product_favorites")
    state.setdefault("recipe_usage", {})
    state.setdefault("recipe_usage_requests", {})
    state.setdefault("order_snapshots", {})
    state.setdefault("order_snapshot_times", {})
    state.setdefault("order_snapshot_providers", {})
    state.setdefault("protected_results", {})
    state.setdefault("protected_requests", {})
    state.setdefault("cart_plan", None)
    state.setdefault("delivery_selection", None)
    state.setdefault("setup", {
        "version": 1,
        "status": "needs_review",
        "reviewed_at": None,
        "noninteractive_defaults_applied_at": None,
    })
    recipient = state.get("email_recipient")
    if recipient is not None and not valid_email_address(recipient):
        state["email_recipient"] = None
    for job in state.get("email_jobs", []):
        if not isinstance(job, dict):
            continue
        job_provider = job.get("provider")
        if not isinstance(job_provider, str) or job_provider not in {"oda", "meny"}:
            job["status"] = "invalid"
        if not isinstance(job.get("order_id"), str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", job["order_id"]) is None:
            job["status"] = "invalid"
        if not valid_email_address(job.get("recipient_snapshot")):
            job.pop("recipient_snapshot", None)
            if job.get("status") in {"pending", "claimed", "sending"}:
                job["status"] = "invalid"
        delivery_date = job.get("delivery_date")
        try:
            canonical_date = date.fromisoformat(delivery_date).isoformat() if isinstance(delivery_date, str) else None
        except ValueError:
            canonical_date = None
        if canonical_date != delivery_date:
            job["status"] = "invalid"
    profile = state.get("profile")
    if not isinstance(profile, dict):
        raise HouseholdError("household profile is invalid")
    profile.setdefault("recipes", deepcopy(DEFAULT_PROFILE["recipes"]))
    recipe_profile = profile.get("recipes")
    if not isinstance(recipe_profile, dict):
        raise HouseholdError("household recipe profile is invalid")
    cooldown = recipe_profile.get("repeat_cooldown_weeks")
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or not 0 <= cooldown <= 260:
        raise HouseholdError("repeat cooldown must be an integer from zero to 260 weeks")
    sources = recipe_profile.setdefault("sources", deepcopy(DEFAULT_RECIPE_SOURCES))
    if (
        not isinstance(sources, dict)
        or set(sources) != set(RECIPE_SOURCE_IDS)
        or any(not isinstance(sources[source], bool) for source in RECIPE_SOURCE_IDS)
    ):
        raise HouseholdError("recipe sources must name the five supported sources as true or false")
    setup = state.get("setup")
    if not isinstance(setup, dict) or setup.get("version") != 1 or setup.get("status") not in {"needs_review", "complete"}:
        raise HouseholdError("household first-run setup state is invalid")
    for field in ("reviewed_at", "noninteractive_defaults_applied_at"):
        if setup.get(field) is not None and (not isinstance(setup[field], str) or len(setup[field]) > 100):
            raise HouseholdError("household first-run setup timestamp is invalid")
    if not isinstance(state["recipe_usage"], dict) or not isinstance(state["recipe_usage_requests"], dict) or not isinstance(state["order_snapshots"], dict) or not isinstance(state["order_snapshot_times"], dict) or not isinstance(state["order_snapshot_providers"], dict) or not isinstance(state["protected_results"], dict) or not isinstance(state["protected_requests"], dict):
        raise HouseholdError("household recipe lifecycle state is invalid")
    if any(
        not isinstance(provider, str) or provider not in {"oda", "meny"}
        for provider in state["order_snapshot_providers"].values()
    ):
        raise HouseholdError("household order snapshot providers are invalid")
    schedule = state.get("schedule")
    delivery = schedule.get("delivery") if isinstance(schedule, Mapping) else None
    if (
        not isinstance(delivery, Mapping)
        or not set(delivery).issubset({"weekday", "preferred_end", "latest_end", "strategy"})
        or delivery.get("strategy") not in {"keep_selected", "cheapest"}
    ):
        raise HouseholdError("household delivery preference is invalid")
    _validate_delivery_selection(state.get("delivery_selection"))
    cart_plan = state.get("cart_plan")
    if cart_plan is not None:
        if not isinstance(cart_plan, dict) or cart_plan.get("provider") not in {"oda", "meny"}:
            raise HouseholdError("household cart plan is invalid")
        if cart_plan.get("status") not in {"active", "needs_input", "ordered"}:
            raise HouseholdError("household cart plan status is invalid")
        if not isinstance(cart_plan.get("menu_ref"), dict):
            raise HouseholdError("household cart plan menu is invalid")
        mappings = (
            cart_plan.get("product_names"), cart_plan.get("baseline_quantities"),
            cart_plan.get("required_quantities"), cart_plan.get("added_quantities"),
            cart_plan.get("last_synced_quantities"),
        )
        if not all(isinstance(value, dict) for value in mappings):
            raise HouseholdError("household cart plan quantities are invalid")
        names, *quantity_maps = mappings
        for product_id in set().union(*(value.keys() for value in mappings)):
            if item_key({"product_id": product_id}) != product_id:
                raise HouseholdError("household cart plan product identity is invalid")
            if not isinstance(names.get(product_id), str) or not names[product_id].strip():
                raise HouseholdError("household cart plan product name is invalid")
            for values in quantity_maps:
                quantity = values.get(product_id, 0)
                if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0 or quantity > 1_000_000:
                    raise HouseholdError("household cart plan quantity is invalid")
        extra_ids = cart_plan.get("start_as_extra_product_ids")
        if not isinstance(extra_ids, list) or not set(extra_ids).issubset(cart_plan["baseline_quantities"]):
            raise HouseholdError("household cart plan starting-quantity mode is invalid")
        for key in ("last_synced_digest", "approved_cart_digest", "pending_cart_digest"):
            digest = cart_plan.get(key)
            if digest is not None and (not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None):
                raise HouseholdError("household cart plan digest is invalid")


class StateStore:
    def __init__(self, directory: Path | str, config: Mapping[str, Any]):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"
        self.config = dict(config)
        if not self.path.exists():
            _atomic_json(self.path, initial_state(self.config))
        configured_provider = str(self.config.get("provider") or "oda").casefold()
        with self.locked() as state:
            source_version = state.get("version", 1)
            if (
                not isinstance(source_version, bool)
                and isinstance(source_version, int)
                and source_version in {1, 2, 3, 4}
            ):
                backup = self.directory / f"state-v{source_version}.backup.json"
                if not backup.exists():
                    _atomic_json(backup, state)
            def backup_v5(value: Mapping[str, Any]) -> None:
                backup = self.directory / "state-v5.backup.json"
                if not backup.exists():
                    _atomic_json(backup, value)

            def backup_v6(value: Mapping[str, Any]) -> None:
                if source_version != 6:
                    return
                backup = self.directory / "state-v6.backup.json"
                if not backup.exists():
                    _atomic_json(backup, value)

            def backup_v7(value: Mapping[str, Any]) -> None:
                if source_version != 7:
                    return
                backup = self.directory / "state-v7.backup.json"
                if not backup.exists():
                    _atomic_json(backup, value)

            def backup_v8(value: Mapping[str, Any]) -> None:
                if source_version != 8:
                    return
                backup = self.directory / "state-v8.backup.json"
                if not backup.exists():
                    _atomic_json(backup, value)

            def backup_v9(value: Mapping[str, Any]) -> None:
                if source_version != 9:
                    return
                backup = self.directory / "state-v9.backup.json"
                if not backup.exists():
                    _atomic_json(backup, value)

            def backup_v10(value: Mapping[str, Any]) -> None:
                if source_version != 10:
                    return
                backup = self.directory / "state-v10.backup.json"
                if not backup.exists():
                    _atomic_json(backup, value)

            def backup_v11(value: Mapping[str, Any]) -> None:
                if source_version == 11:
                    backup = self.directory / "state-v11.backup.json"
                    if not backup.exists():
                        _atomic_json(backup, value)

            _migrate_state(
                state,
                self.config,
                before_v6=backup_v5,
                before_v7=backup_v6,
                before_v8=backup_v7,
                before_v9=backup_v8,
                before_v10=backup_v9,
                before_v11=backup_v10,
                before_v12=backup_v11,
            )
            state_household = state.get("household")
            configured_household = str(self.config["household"])
            if state_household is None:
                state["household"] = configured_household
            elif state_household != configured_household:
                raise HouseholdError(
                    f"household state belongs to {state_household}; use a separate state directory for {configured_household}"
                )
            state_provider = state.get("provider")
            if state_provider is None:
                state["provider"] = configured_provider
            elif state_provider != configured_provider:
                raise HouseholdError(
                    f"household state belongs to provider {state_provider}; use a separate state directory for {configured_provider}"
                )

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                state = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HouseholdError("household state is unreadable") from exc
            try:
                before = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise HouseholdError("household state contains an invalid JSON value") from exc
            yield state
            try:
                after = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise HouseholdError("household state contains an invalid JSON value") from exc
            if before != after:
                _atomic_json(self.path, state)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def read(self) -> dict[str, Any]:
        with self.locked() as state:
            return deepcopy(state)

    def update_profile(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        with self.locked() as state:
            _merge(state["profile"], changes)
            validate_profile(state["profile"])
            return deepcopy(state["profile"])

    def reset_profile(self, paths: list[str] | None = None) -> dict[str, Any]:
        defaults = initial_state(self.config)["profile"]
        with self.locked() as state:
            if not paths:
                state["profile"] = defaults
            else:
                for path in paths:
                    parts = path.split(".")
                    source: Any = defaults
                    target: Any = state["profile"]
                    for part in parts[:-1]:
                        if not isinstance(source, dict) or part not in source or not isinstance(target, dict) or part not in target:
                            raise HouseholdError(f"unknown profile field: {path}")
                        source, target = source[part], target[part]
                    if not isinstance(source, dict) or parts[-1] not in source or not isinstance(target, dict):
                        raise HouseholdError(f"unknown profile field: {path}")
                    target[parts[-1]] = deepcopy(source[parts[-1]])
            validate_profile(state["profile"])
            return deepcopy(state["profile"])


def mask_email(value: str | None) -> str | None:
    if not value or "@" not in value:
        return None
    local, domain = value.rsplit("@", 1)
    return f"{local[:1]}***@{domain[:1]}***.{domain.rsplit('.', 1)[-1]}"


def item_key(item: Mapping[str, Any]) -> str:
    product_id = str(item.get("product_id") or "").strip()
    is_meny_path = (
        len(product_id) <= 512
        and re.fullmatch(r"/varer/(?!kampanjer/)[A-Za-z0-9._~%/-]+-\d{4,14}", product_id) is not None
        and ".." not in product_id
        and "//" not in product_id
    )
    if not product_id.isdigit() and not is_meny_path:
        raise HouseholdError("product_id must be an Oda number or MENY product path")
    return product_id


def put_item(items: list[dict[str, Any]], value: Mapping[str, Any]) -> list[dict[str, Any]]:
    product_id = item_key(value)
    product_name = str(value.get("product_name") or "").strip()
    quantity = value.get("quantity", 1)
    if not product_name or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise HouseholdError("product name and positive integer quantity are required")
    normalized = {"product_id": product_id, "product_name": product_name, "quantity": quantity}
    if value.get("product_url"):
        normalized["product_url"] = str(value["product_url"])
    for optional in ("label", "schedule"):
        if optional in value:
            normalized[optional] = deepcopy(value[optional])
    return sorted([entry for entry in items if item_key(entry) != product_id] + [normalized], key=item_key)


def remove_item(items: list[dict[str, Any]], product_id: str) -> list[dict[str, Any]]:
    wanted = item_key({"product_id": product_id})
    return [entry for entry in items if item_key(entry) != wanted]


def due_recurring(item: Mapping[str, Any], when: date) -> bool:
    schedule = item.get("schedule")
    if not isinstance(schedule, Mapping):
        raise HouseholdError("recurring item schedule is missing")
    every = schedule.get("every", 1)
    unit = schedule.get("unit")
    if isinstance(every, bool) or not isinstance(every, int) or every < 1 or unit not in {"weeks", "months"}:
        raise HouseholdError("recurring interval is invalid")
    anchor = schedule.get("anchor")
    if unit == "weeks":
        if anchor is None:
            anchor_date = when - timedelta(days=when.weekday())
        else:
            match = re.fullmatch(r"(\d{4})-W(\d{2})", str(anchor))
            if not match:
                raise HouseholdError("weekly anchor must use YYYY-Www")
            anchor_date = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
        current = when - timedelta(days=when.weekday())
        return (current - anchor_date).days >= 0 and ((current - anchor_date).days // 7) % every == 0
    if anchor is None:
        anchor_year, anchor_month = when.year, when.month
    else:
        match = re.fullmatch(r"(\d{4})-(\d{2})", str(anchor))
        if not match or not 1 <= int(match.group(2)) <= 12:
            raise HouseholdError("monthly anchor must use YYYY-MM")
        anchor_year, anchor_month = int(match.group(1)), int(match.group(2))
    delta = (when.year - anchor_year) * 12 + when.month - anchor_month
    return delta >= 0 and delta % every == 0


def masked_status(state: Mapping[str, Any], integration: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "state_version": state["version"],
        "household": state["household"],
        "integration": dict(integration),
        "auto_checkout": state["schedule"]["auto_checkout"],
        "schedule": deepcopy(state["schedule"]),
        "email_recipient": mask_email(state.get("email_recipient")),
        "product_favorites_count": len(state["product_favorites"]),
        "recurring_items": len(state["recurring_items"]),
        "menu_phase": (state.get("menu") or {}).get("phase"),
        "menu_id": (state.get("menu") or {}).get("menu_id"),
        "cart_plan_status": (state.get("cart_plan") or {}).get("status"),
        "pending_checkout_status": (state.get("pending_checkout") or {}).get("status"),
        "pending_cancellation_status": (state.get("pending_cancellation") or {}).get("status"),
        "order_change_status": (state.get("order_change") or {}).get("status"),
        "configuration_status": (state.get("setup") or {}).get("status"),
        "recipe_sources": deepcopy((state.get("profile") or {}).get("recipes", {}).get("sources", {})),
    }


def cart_summary(cart: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the live camelCase Oda cart without making its schema local authority."""
    raw_lines: list[Any] = []
    groups = cart.get("groups")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, Mapping) or not isinstance(group.get("items"), list):
                raise HouseholdError("Oda cart group is invalid")
            raw_lines.extend(group["items"])
    elif isinstance(cart.get("items"), list):
        raw_lines = list(cart["items"])
    else:
        raise HouseholdError("Oda cart items are unavailable")

    lines = []
    for item in raw_lines:
        if not isinstance(item, Mapping):
            raise HouseholdError("Oda cart line is invalid")
        product = item.get("product") if isinstance(item.get("product"), Mapping) else item
        quantity = item.get("quantity", 1)
        try:
            numeric_quantity = float(quantity)
        except (TypeError, ValueError, OverflowError):
            numeric_quantity = math.nan
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(numeric_quantity)
            or numeric_quantity < 1
            or numeric_quantity > 1_000_000
            or not numeric_quantity.is_integer()
        ):
            raise HouseholdError("Oda cart quantity is invalid")
        lines.append({
            "product_id": str(product.get("id") or product.get("product_id") or ""),
            "name": str(product.get("name") or product.get("product_name") or ""),
            "quantity": int(numeric_quantity),
            "price": item.get("totalGrossAmount", item.get("price", product.get("price"))),
        })
    total = cart.get("totalGrossAmount", cart.get("subtotal", cart.get("total")))
    try:
        numeric_total = float(str(total).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise HouseholdError("Oda cart total is unavailable") from exc
    if not math.isfinite(numeric_total) or numeric_total < 0:
        raise HouseholdError("Oda cart total is unavailable")
    slot = cart.get("deliverySlot", cart.get("delivery"))
    if isinstance(slot, Mapping):
        delivery = {
            "slot_id": slot.get("id", slot.get("slot_id")),
            "display": slot.get("name", slot.get("display")),
            "address": cart.get("deliveryAddress"),
            "unattended": cart.get("isUnattendedDelivery"),
        }
    else:
        delivery = None
    result = {
        "items": lines,
        "count": cart.get("productQuantityCount", cart.get("count")),
        "total": numeric_total,
        "delivery": delivery,
    }
    raw_amounts = cart.get("amounts")
    if raw_amounts is not None:
        amount_keys = {
            "product_subtotal", "delivery_price", "discounts",
            "deposits", "bags", "other_fees", "provider_total",
        }
        if not isinstance(raw_amounts, Mapping) or set(raw_amounts) != amount_keys:
            raise HouseholdError("provider checkout amounts are invalid")
        amounts: dict[str, Any] = {}
        for key in amount_keys:
            amount = raw_amounts.get(key)
            if key == "other_fees":
                if amount is None:
                    amounts[key] = None
                    continue
                if not isinstance(amount, Mapping):
                    raise HouseholdError("provider checkout amounts are invalid")
                if not amount or len(amount) > 20:
                    raise HouseholdError("provider checkout amounts are invalid")
                named_fees: dict[str, float] = {}
                for name, value in amount.items():
                    if (
                        not isinstance(name, str)
                        or not name.strip()
                        or len(name.encode("utf-8")) > 200
                        or isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or float(value) < 0
                    ):
                        raise HouseholdError("provider checkout amounts are invalid")
                    normalized_name = " ".join(name.split())
                    if normalized_name in named_fees:
                        raise HouseholdError("provider checkout amounts are invalid")
                    named_fees[normalized_name] = float(value)
                amounts[key] = named_fees
            elif amount is None:
                amounts[key] = None
            elif isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(float(amount)):
                raise HouseholdError("provider checkout amounts are invalid")
            else:
                numeric_amount = float(amount)
                if (key == "discounts" and numeric_amount > 0) or (
                    key != "discounts" and numeric_amount < 0
                ):
                    raise HouseholdError("provider checkout amounts are invalid")
                amounts[key] = numeric_amount
        if amounts["provider_total"] != numeric_total:
            raise HouseholdError("provider checkout total is inconsistent")
        result["amounts"] = amounts
    elif "totalGrossAmount" in cart:
        result["amounts"] = {
            "product_subtotal": None,
            "delivery_price": None,
            "discounts": None,
            "deposits": None,
            "bags": None,
            "other_fees": None,
            "provider_total": numeric_total,
        }
    return result
