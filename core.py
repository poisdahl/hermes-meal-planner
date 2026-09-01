"""Small, private-state core for one Hermes meal-planning household."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, Iterator, Mapping


class HouseholdError(RuntimeError):
    pass


class CheckoutPreconditionError(HouseholdError):
    """Checkout stopped before the final provider control was dispatched."""


class CancellationPreconditionError(HouseholdError):
    """Cancellation stopped before the final provider control was dispatched."""


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
}


DEFAULT_SCHEDULE: dict[str, Any] = {
    "enabled": False,
    "weekday": "Thursday",
    "time": "15:00",
    "timezone": "Europe/Oslo",
    "mode": "draft",
    "delivery": {"weekday": "Saturday", "preferred_end": "15:00", "latest_end": "18:00"},
    "maximum_total": None,
    "auto_checkout": False,
    "cron_job_id": None,
}


def initial_state(config: Mapping[str, Any]) -> dict[str, Any]:
    profile = deepcopy(DEFAULT_PROFILE)
    _merge(profile, config.get("profile_overrides", {}))
    return {
        "version": 1,
        "household": str(config["household"]),
        "provider": str(config.get("provider") or "oda").casefold(),
        "profile": profile,
        "favorites": [],
        "recurring_items": [],
        "schedule": deepcopy(DEFAULT_SCHEDULE),
        "email_recipient": None,
        "menu": None,
        "pending_checkout": None,
        "pending_cancellation": None,
        "order_change": None,
        "email_jobs": [],
        "occurrences": {},
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


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, path)


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
            before = json.dumps(state, ensure_ascii=False, sort_keys=True)
            yield state
            after = json.dumps(state, ensure_ascii=False, sort_keys=True)
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
        "household": state["household"],
        "integration": dict(integration),
        "auto_checkout": state["schedule"]["auto_checkout"],
        "schedule": deepcopy(state["schedule"]),
        "email_recipient": mask_email(state.get("email_recipient")),
        "favorites": len(state["favorites"]),
        "recurring_items": len(state["recurring_items"]),
        "menu_phase": (state.get("menu") or {}).get("phase"),
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
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity < 1 or not float(quantity).is_integer():
            raise HouseholdError("Oda cart quantity is invalid")
        lines.append({
            "product_id": str(product.get("id") or product.get("product_id") or ""),
            "name": str(product.get("name") or product.get("product_name") or ""),
            "quantity": int(quantity),
            "price": item.get("totalGrossAmount", item.get("price", product.get("price"))),
        })
    total = cart.get("totalGrossAmount", cart.get("subtotal", cart.get("total")))
    try:
        numeric_total = float(str(total).replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise HouseholdError("Oda cart total is unavailable") from exc
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
    return {
        "items": lines,
        "count": cart.get("productQuantityCount", cart.get("count")),
        "total": numeric_total,
        "delivery": delivery,
    }
