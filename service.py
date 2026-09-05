#!/usr/bin/env python3
"""One small meal-planning service per household."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import fcntl
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import threading
import time
from typing import Any, Mapping
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from oda_browser import OdaBrowser, OdaCheckoutMismatchError, delivery_signature as oda_delivery_signature
from core import (
    CancellationPreconditionError,
    CheckoutPreconditionError,
    HouseholdError,
    StateStore,
    cart_summary,
    cheapest_delivery_slot,
    delivery_candidate_digest,
    delivery_price_display,
    due_recurring,
    mask_email,
    masked_status,
    put_item,
    remove_item,
    recurring_schedule,
    validate_profile,
    validate_delivery_slot,
    valid_email_address,
)
from oda import (
    OdaClient,
    oda_cart_delivery_matches_slot,
    oda_cart_delivery_window,
    oda_delivery_slot_date,
)
from meny import MAX_CART_CLICKS, MENY_CART_TIMEOUT, MENY_ORDER_TIMEOUT, MENY_READ_TIMEOUT, MenyClient, MenyOrderChangeDispatchError, meny_checkout_reviews_match, normalize_product_ref
from recipes import RecipeError, RecipeStore, normalize_recipe, normalize_source_url, recipe_key, scale_recipe, validate_week
from planner import (
    MAX_CANDIDATES, MAX_HISTORY_RECORDS, PLANNER_VERSION, PlannerError, plan_week,
)
from product_planner import (
    MAX_CANDIDATES_PER_REQUIREMENT, MAX_REQUIREMENTS, normalize_approvals,
    build_product_plan,
    cart_requirements as prepared_cart_requirements,
    menu_requirements as exact_menu_requirements,
    validate_product_plan, product_plan_digest,
)
from product_observations import MAX_PRODUCTS
import menu_planning as mp
import planning_feedback as pf
from planning_assessment import assess_menu, workflow_status, feedback_targets
import batch_planning as bp
from recipe_libraries import (
    CAPABILITY_NAMES,
    MAX_LIBRARY_RECIPE_KEY,
    RecipeLibraryAdapter,
    RecipeLibraryDefiniteError,
    RecipeLibraryError,
    RecipeLibraryExternalMissingError,
    RecipeLibraryFavoriteConflictError,
    RecipeLibraryLabelConflictError,
    RecipeLibraryUncertainError,
    RecipeLibraryUpdateConflictError,
    library_recipe_key,
    library_recipe_key_aliases,
    load_library_secret,
    load_optional_adapter,
    normalize_library_configuration,
    normalize_label_name,
    validate_library_id,
    validate_library_label_ref,
    validate_library_recipe_ref,
    verified_capabilities,
    secret_path,
)
from recipe_sources import (
    SOURCE_IDS,
    TheMealDBSource,
    WikibooksSource,
    provider_recipe_candidates,
    validate_source_settings,
)


from service_common import (
    CANCELLATION_OPERATION_TIMEOUT,
    EMAIL_AUTOMATION_PROTOCOL,
    EMAIL_CLAIM_LEASE,
    LIBRARY_SEARCH_CURSOR_PREFIX,
    MAX_EMAIL_HTML_BYTES,
    MAX_EXTERNAL_FAVORITE_SEARCH_PAGES,
    MAX_MENU_BYTES,
    MAX_REQUEST,
    MENY_CHECKOUT_OPERATION_TIMEOUT,
    MENY_VIPPS_EXPIRY_BUFFER,
    SCHEDULE_OCCURRENCE_LEASE,
    SCHEDULE_WEEKDAYS,
    UNRESOLVED_CHECKOUT_STATUSES,
    bounded_limit,
    canonical,
    checkout_intent_signature,
    delivery_matches,
    email_automation_ack,
    email_automation_key,
    email_automation_prompt,
    email_job_provider,
    expired_awaiting_confirmation,
    menu_digest,
    menu_email_html,
    menu_email_period,
    meny_login_lost,
    meny_order_matches_checkout,
    money_cents,
    now,
    oda_order_address_identity,
    oda_order_delivery_identity,
    oda_order_matches_addition,
    oda_order_quantities,
    order_matches_checkout,
    peer_uid,
    require_provider_identity,
    safe_order_id,
    scheduled_occurrence,
    strict_json_loads,
    validate_request_value,
    validate_schedule
)
from recipe_operations import RecipeOperations
from planning_operations import PlanningOperations
from order_operations import OrderOperations
from email_operations import EmailOperations


class Application(RecipeOperations, PlanningOperations, OrderOperations, EmailOperations):
    def _now(self) -> datetime:
        return now()

    def __init__(
        self, store: StateStore, provider_client: Any, browser: OdaBrowser,
        *, email_provider_clients: Mapping[str, Any] | None = None,
        external_recipe_sources: Mapping[str, Any] | None = None,
        recipe_library_adapters: Mapping[str, RecipeLibraryAdapter] | None = None,
    ):
        self.store = store
        self.recipes = RecipeStore(store.directory / "recipes.sqlite3", str(store.config["household"]))
        self._recipe_operations_recovered = False
        library_configuration = normalize_library_configuration(store.config)
        self.recipe_libraries = {
            item["library_id"]: item for item in library_configuration["recipe_libraries"]
        }
        self.primary_recipe_library_id = library_configuration["primary_recipe_library_id"]
        self.recipe_library_adapters = dict(recipe_library_adapters or {})
        self.recipe_favorite_locks: dict[tuple[str, str], threading.Lock] = {}
        self.recipe_favorite_locks_guard = threading.Lock()
        self.recipe_label_locks: dict[tuple[str, str], threading.Lock] = {}
        self.recipe_label_locks_guard = threading.Lock()
        self.recipe_lifecycle_locks: dict[tuple[str, str], threading.Lock] = {}
        self.recipe_lifecycle_locks_guard = threading.Lock()
        self.recipe_planner_lock = threading.RLock()
        self.recipe_planner_lock_path = store.directory / "recipe-planner.lock"
        self.product_plan_lock = threading.RLock()
        self.oda = provider_client
        self.provider = str(store.config.get("provider") or "oda").casefold()
        self.email_provider_clients = {**dict(email_provider_clients or {}), self.provider: provider_client}
        self.confirmation_policy = str(store.config.get("confirmation_policy") or "fresh").casefold()
        self.browser = provider_client if self.provider == "meny" else browser
        self.browser_lock = threading.Lock()
        self.email_automation_profile = str(store.config.get("email_automation_profile") or "").strip()
        self.external_recipe_sources = dict(external_recipe_sources or {
            "themealdb": TheMealDBSource(api_key=os.environ.get("THEMEALDB_API_KEY", "1")),
            "wikibooks": WikibooksSource(),
        })
        self.integration: dict[str, Any]
        if self.provider == "meny":
            # MENY readiness can require a full browser navigation. Keep the
            # local RPC socket available during service startup and perform
            # that bounded probe on the first status or provider operation.
            self.integration = {
                "status": "unavailable",
                "provider": "meny",
                "message": "MENY status has not been checked since service start",
            }
        else:
            self._refresh_integration()

    def _household_today(self, state: Mapping[str, Any] | None = None) -> date:
        current = state or self.store.read()
        timezone_name = str((current.get("schedule") or {}).get("timezone") or "Europe/Oslo")
        try:
            return now().astimezone(ZoneInfo(timezone_name)).date()
        except ZoneInfoNotFoundError as exc:
            raise HouseholdError("schedule timezone is invalid") from exc

    @staticmethod
    def _store_protected_result(
        state: dict[str, Any], confirmation_id: str, kind: str, result: Mapping[str, Any],
        *, target_id: str | None = None, intent_signature: str | None = None,
    ) -> None:
        records = state.setdefault("protected_results", {})
        records[confirmation_id] = {
            "kind": kind, "target_id": target_id, "intent_signature": intent_signature,
            "result": deepcopy(dict(result)), "completed_at": now().isoformat(),
        }
        state[f"last_{kind}_confirmation_id"] = confirmation_id
        for request in state.setdefault("protected_requests", {}).values():
            if isinstance(request, dict) and request.get("kind") == kind and request.get("confirmation_id") == confirmation_id:
                request["result"] = deepcopy(dict(result))
                request["completed_at"] = now().isoformat()

    @staticmethod
    def _read_protected_result(state: Mapping[str, Any], confirmation_id: str, kind: str) -> dict[str, Any] | None:
        record = state.get("protected_results", {}).get(confirmation_id) if isinstance(state.get("protected_results"), Mapping) else None
        if not isinstance(record, Mapping) or record.get("kind") != kind or not isinstance(record.get("result"), Mapping):
            return None
        return {**deepcopy(dict(record["result"])), "idempotent": True}

    @staticmethod
    def _idempotency_key(value: Any, kind: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is None:
            raise HouseholdError(f"{kind} idempotency_key is required and must be bounded safe text")
        return value

    @staticmethod
    def _protected_request(state: dict[str, Any], kind: str, key: str, *, target_id: str | None = None) -> dict[str, Any] | None:
        records = state.setdefault("protected_requests", {})
        identity = f"{kind}:{key}"
        existing = records.get(identity)
        if existing is not None:
            if not isinstance(existing, dict) or existing.get("kind") != kind or existing.get("target_id") != target_id:
                raise HouseholdError("idempotency_key was already used for a different protected request")
            return existing
        return None

    @staticmethod
    def _bind_protected_request(state: dict[str, Any], kind: str, key: str, confirmation_id: str, *, target_id: str | None = None) -> None:
        records = state.setdefault("protected_requests", {})
        records[f"{kind}:{key}"] = {
            "kind": kind, "target_id": target_id, "confirmation_id": confirmation_id,
            "created_at": now().isoformat(),
        }

    def _refresh_integration(self, deadline: float | None = None, *, allow_recovery: bool = False) -> None:
        try:
            if self.provider == "meny" and (deadline is not None or allow_recovery):
                probe = self.oda.probe(deadline=deadline, allow_recovery=allow_recovery)
            else:
                probe = self.oda.probe()
            self.integration = {
                "status": "ready",
                "provider": self.provider,
                "protocol_version": probe.get("protocol_version"),
                "server": probe.get("server"),
                "tool_count": probe.get("tool_count"),
            }
        except HouseholdError as exc:
            status = "awaiting_login" if "login" in str(exc).casefold() else "unavailable"
            self.integration = {"status": status, "provider": self.provider, "message": str(exc)}

    @contextmanager
    def _browser_operation(self, deadline: float | None = None, *, allow_pending_cart: bool = False):
        if deadline is None:
            acquired = self.browser_lock.acquire()
        else:
            remaining = deadline - time.monotonic()
            acquired = remaining > 0 and self.browser_lock.acquire(timeout=remaining)
        if not acquired:
            raise HouseholdError("provider browser deadline reached")
        try:
            if not allow_pending_cart and self.store.read().get("pending_cart_change"):
                raise HouseholdError("reconcile_change before using the provider; a cart write is uncertain")
            yield
        finally:
            self.browser_lock.release()

    @contextmanager
    def _recipe_planner_operation(self):
        with self.recipe_planner_lock:
            descriptor = os.open(
                self.recipe_planner_lock_path, os.O_RDWR | os.O_CREAT, 0o600
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise HouseholdError("request must be an object")
        validate_request_value(request)
        operation = request.get("operation")
        action = request.get("action")
        if not isinstance(operation, str) or not 1 <= len(operation) <= 40:
            raise HouseholdError("operation must be bounded text")
        if action is not None and (not isinstance(action, str) or not 1 <= len(action) <= 40):
            raise HouseholdError("action must be bounded text")
        try:
            if operation == "products" and action == "lowest_cost":
                with self._recipe_planner_operation(), self.product_plan_lock:
                    result = self._handle(request)
            elif operation == "products":
                with self.product_plan_lock:
                    result = self._handle(request)
            elif operation == "cart" and action != "get":
                with self.product_plan_lock:
                    result = self._handle(request)
            elif (
                operation == "profile" and action in {"update", "reset"}
            ) or (
                operation == "setup" and action == "apply"
            ):
                with self.product_plan_lock:
                    result = self._handle(request)
            elif operation == "menu" and action in {"lock", "replan_prepare", "replan_apply", "batch_prepare", "batch_apply"}:
                with self._recipe_planner_operation(), self.product_plan_lock:
                    result = self._handle(request)
            elif operation == "menu" and action in {"save", "clear"}:
                if action == "save" and request.get("planner_handoff") is not None:
                    with self._recipe_planner_operation():
                        result = self._handle(request)
                else:
                    result = self._handle(request)
            elif operation in {"recipes", "feedback", "migration"} or (
                operation == "menu" and (
                    action == "plan"
                )
            ):
                with self._recipe_planner_operation():
                    result = self._handle(request)
            else:
                result = self._handle(request)
        except HouseholdError as exc:
            alternate_oda_email = (
                request.get("operation") == "email" and request.get("provider") == "oda"
            )
            if self.provider == "meny" and not alternate_oda_email and meny_login_lost(exc):
                self.integration = {
                    "status": "awaiting_login",
                    "provider": "meny",
                    "message": str(exc),
                }
            raise
        if self.provider == "meny" and request.get("operation") in {"catalog", "products", "cart", "delivery", "orders", "checkout"}:
            self.integration = {
                "status": "ready",
                "provider": "meny",
                "protocol_version": "browser-v1",
                "server": {"name": "MENY website"},
                "tool_count": 11,
            }
        return result

    def _handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request.get("operation")
        if self.store.read().get("pending_cart_change") and (
            (operation == "cart" and request.get("action", "get") not in {"get", "reconcile_change"})
            or (operation == "products" and request.get("action") == "apply")
            or operation == "checkout"
            or (operation == "orders" and request.get("action") not in {None, "list", "get"})
        ):
            raise HouseholdError("reconcile_change before continuing; the previous cart write is uncertain")
        if operation == "health":
            return {"ok": True, "integration": self.integration}
        if operation == "status":
            state = self.store.read()
            if self.provider == "meny" and self.integration.get("status") != "ready" and not state.get("pending_cart_change"):
                deadline = time.monotonic() + MENY_READ_TIMEOUT
                with self._browser_operation(deadline):
                    pending_status = (state.get("pending_checkout") or {}).get("status")
                    if pending_status not in UNRESOLVED_CHECKOUT_STATUSES:
                        safe = not state.get("pending_checkout") and not state.get("pending_cancellation") and not state.get("order_change") and not state.get("pending_cart_change")
                        self._refresh_integration(deadline, allow_recovery=safe)
            return {
                **masked_status(self.store.read(), self.integration),
                "confirmation_policy": self.confirmation_policy,
                "workflow": workflow_status(self.store.read()),
            }
        if operation == "setup":
            return self._setup(request)
        if operation == "profile":
            return self._profile(request)
        if operation == "product_favorites":
            return self._items(request, "product_favorites")
        if operation == "recurring":
            return self._recurring(request)
        if operation == "migration":
            from recipe_migration import Migration
            return Migration(self).handle(request)
        if operation == "recipes":
            return self._recipes(request)
        if operation == "menu":
            return self._menu(request)
        if operation == "feedback":
            return self._feedback(request)
        if operation == "schedule":
            return self._schedule(request)
        action = request.get("action")
        email_due_requires_meny = (
            operation == "email" and action in {"due", "reconcile"}
            and (request.get("provider") is None or request.get("provider") == "meny")
        )
        meny_read = self.provider == "meny" and (
            operation == "catalog"
            or operation == "products"
            or (operation == "cart" and action in {None, "get"})
            or (operation == "delivery" and action in {None, "list"})
            or (operation == "orders" and action in {None, "list", "get"})
            or email_due_requires_meny
        )
        if meny_read:
            timeout = MENY_ORDER_TIMEOUT if operation in {"products", "delivery", "orders"} else MENY_READ_TIMEOUT
            deadline = time.monotonic() + timeout
            with self._browser_operation(deadline, allow_pending_cart=operation == "cart" and action in {None, "get"}):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending MENY checkout before using another browser operation")
                safe = not state.get("pending_checkout") and not state.get("pending_cancellation") and not state.get("order_change") and not state.get("pending_cart_change")
                guarded = {**request, "_deadline": deadline, "_allow_browser_recovery": safe}
                if operation == "catalog":
                    return self._catalog(guarded)
                if operation == "products":
                    return self._products(guarded)
                if operation == "cart":
                    return self._cart(guarded)
                if operation == "delivery":
                    return self._delivery(guarded)
                if operation == "orders":
                    return self._orders(guarded)
                return self._email(guarded)
        if self.provider == "meny" and operation in {"catalog", "products", "cart", "delivery", "orders"}:
            pending_status = (self.store.read().get("pending_checkout") or {}).get("status")
            if pending_status in UNRESOLVED_CHECKOUT_STATUSES:
                raise HouseholdError("reconcile the pending MENY checkout before using another browser operation")
        if operation == "catalog":
            return self._catalog(request)
        if operation == "products":
            return self._products(request)
        if operation == "cart":
            return self._cart(request)
        if operation == "delivery":
            return self._delivery(request)
        if operation == "orders":
            return self._orders(request)
        if operation == "checkout":
            return self._checkout(request)
        if operation == "email":
            return self._email(request)
        raise HouseholdError("unknown household operation")

    def _profile(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "show")
        if action == "show":
            state = self.store.read()
            return {"profile": state["profile"], "email_recipient": mask_email(state.get("email_recipient"))}
        if action == "update":
            changes = request.get("changes", {})
            recipe_changes = changes.get("recipes") if isinstance(changes, Mapping) else None
            if isinstance(recipe_changes, Mapping) and "repeat_cooldown_weeks" in recipe_changes:
                cooldown = recipe_changes["repeat_cooldown_weeks"]
                if isinstance(cooldown, bool) or not isinstance(cooldown, int) or not 0 <= cooldown <= 260:
                    raise HouseholdError("repeat cooldown must be an integer from zero to 260 weeks")
            if isinstance(recipe_changes, Mapping) and "sources" in recipe_changes:
                current = self.store.read()["profile"]["recipes"]["sources"]
                source_changes = recipe_changes["sources"]
                if not isinstance(source_changes, Mapping) or not set(source_changes).issubset(SOURCE_IDS):
                    raise HouseholdError("recipe source changes contain unknown sources")
                validate_source_settings({**current, **dict(source_changes)})
            return {"profile": self.store.update_profile(changes)}
        if action == "reset":
            paths = request.get("paths")
            if paths is not None and (not isinstance(paths, list) or not all(isinstance(item, str) for item in paths)):
                raise HouseholdError("reset paths must be strings")
            return {"profile": self.store.reset_profile(paths)}
        if action == "set_email":
            supplied_email = request.get("email")
            email = supplied_email.strip() if isinstance(supplied_email, str) else ""
            if not valid_email_address(email):
                raise HouseholdError("email address is invalid")
            with self.store.locked() as state:
                state["email_recipient"] = email
            return {"email_recipient": mask_email(email)}
        raise HouseholdError("unknown profile action")

    def _setup_summary(self, state: Mapping[str, Any]) -> dict[str, Any]:
        profile = state["profile"]
        meals = profile["meals"]
        return {
            "household": state["household"],
            "provider": self.provider,
            "people": meals["people"],
            "portions": meals["portions"],
            "diet": deepcopy(profile["diet"]),
            "confirmation_policy": self.confirmation_policy,
            "weekly_menu": {
                key: deepcopy(meals[key])
                for key in ("dinner_days", "dishes", "batch_dishes", "salads", "cook_days", "eat_days")
            },
            "recipe_sources": deepcopy(profile["recipes"]["sources"]),
            "primary_recipe_library_id": self.primary_recipe_library_id,
            "optional_recipe_libraries": "Configure external recipe libraries only with the local interactive recipe_library_setup.py helper.",
        }

    def _setup_question(self, state: Mapping[str, Any]) -> dict[str, Any]:
        required = (state.get("setup") or {}).get("status") != "complete"
        return {
            "configuration_required": required,
            "configuration_status": (state.get("setup") or {}).get("status"),
            "current": self._setup_summary(state),
            "question": "Keep all current/default Meal Concierge settings? Answer once, or provide only the values you want to change." if required else None,
            "next": "Call meal_concierge_setup action=apply with keep_current=true, or keep_current=false and only the requested changes." if required else "Use action=rerun to review this configuration again.",
        }

    def _setup_gate(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        interactive = request.get("interactive") is True
        with self.store.locked() as state:
            setup = state["setup"]
            if setup["status"] == "complete":
                return None
            if not interactive:
                setup["noninteractive_defaults_applied_at"] = setup.get("noninteractive_defaults_applied_at") or now().isoformat()
                return None
            return self._setup_question(state)

    def _setup(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "show")
        if action == "show":
            state = self.store.read()
            return {"setup": deepcopy(state["setup"]), **self._setup_question(state)}
        if action == "rerun":
            with self.store.locked() as state:
                state["setup"]["status"] = "needs_review"
                state["setup"]["reviewed_at"] = None
                return {"setup": deepcopy(state["setup"]), **self._setup_question(state)}
        if action != "apply":
            raise HouseholdError("unknown setup action")
        keep_current = request.get("keep_current")
        changes = request.get("changes") or {}
        if not isinstance(keep_current, bool) or not isinstance(changes, Mapping):
            raise HouseholdError("setup apply needs keep_current true or false and an optional changes object")
        if keep_current and changes:
            raise HouseholdError("keep_current cannot be combined with setup changes")
        allowed = {"provider", "confirmation_policy", "people", "portions", "diet", "weekly_menu", "recipe_sources"}
        if not set(changes).issubset(allowed):
            raise HouseholdError("setup changes contain unknown fields")
        requested_provider = str(changes.get("provider") or self.provider).casefold()
        requested_policy = str(changes.get("confirmation_policy") or self.confirmation_policy).casefold()
        if requested_provider != self.provider:
            raise HouseholdError("changing provider requires a separate provider-bound state directory and service restart")
        if requested_policy != self.confirmation_policy:
            raise HouseholdError("changing confirmation_policy requires updating the private config and restarting the service")
        with self.store.locked() as state:
            before = self._setup_summary(state)
            profile = state["profile"]
            meals = profile["meals"]
            for field in ("people", "portions"):
                if field in changes:
                    value = changes[field]
                    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
                        raise HouseholdError(f"setup {field} must be an integer from one to 100")
                    meals[field] = value
            if "diet" in changes:
                diet = changes["diet"]
                if not isinstance(diet, Mapping) or not set(diet).issubset(profile["diet"]):
                    raise HouseholdError("setup diet contains unknown fields")
                profile["diet"].update(deepcopy(dict(diet)))
            if "weekly_menu" in changes:
                weekly = changes["weekly_menu"]
                editable = {"dinner_days", "dishes", "batch_dishes", "salads", "cook_days", "eat_days"}
                if not isinstance(weekly, Mapping) or not set(weekly).issubset(editable):
                    raise HouseholdError("setup weekly_menu contains unknown fields")
                meals.update(deepcopy(dict(weekly)))
            if "recipe_sources" in changes:
                source_changes = changes["recipe_sources"]
                if not isinstance(source_changes, Mapping) or not set(source_changes).issubset(SOURCE_IDS):
                    raise HouseholdError("setup recipe_sources contains unknown sources")
                profile["recipes"]["sources"] = validate_source_settings({
                    **profile["recipes"]["sources"], **dict(source_changes),
                })
            validate_profile(profile)
            setup = state["setup"]
            current = self._setup_summary(state)
            if setup["status"] == "complete" and canonical(current) == canonical(before):
                return {"configured": True, "idempotent": True, "setup": deepcopy(setup), "current": current}
            setup["status"] = "complete"
            setup["reviewed_at"] = now().isoformat()
            return {"configured": True, "setup": deepcopy(setup), "current": current}

    def _items(self, request: Mapping[str, Any], key: str) -> dict[str, Any]:
        action = request.get("action", "list")
        with self.store.locked() as state:
            if action == "list":
                items = deepcopy(state[key])
                for item in items:
                    self._product_id(item.get("product_id") if isinstance(item, Mapping) else None)
                return {key: items}
            if action == "add":
                item = request.get("item", {})
                if not isinstance(item, Mapping):
                    raise HouseholdError(f"{key} item must be an object")
                item = deepcopy(dict(item))
                item["product_id"] = self._product_id(item.get("product_id"))
                if key == "recurring_items":
                    schedule = deepcopy(item.get("schedule"))
                    previous = next((v for v in state[key] if v["product_id"] == item["product_id"]), None)
                    old_schedule = previous.get("schedule", {}) if previous else {}
                    if isinstance(schedule, dict) and schedule.get("anchor") is None and all(schedule.get(k, 1 if k == "every" else None) == old_schedule.get(k, 1 if k == "every" else None) for k in ("unit", "every")) and old_schedule.get("anchor"):
                        schedule["anchor"] = old_schedule["anchor"]
                    item["schedule"] = recurring_schedule(schedule, self._household_today(state))
                state[key] = put_item(state[key], item)
            elif action == "remove":
                state[key] = remove_item(state[key], self._product_id(request.get("product_id")))
            else:
                raise HouseholdError(f"unknown {key} action")
            return {key: deepcopy(state[key])}

    def _product_id(self, value: Any) -> str:
        if self.provider == "meny":
            return normalize_product_ref(value)
        product_id = str(value or "").strip()
        if not product_id.isdigit() or int(product_id) < 1:
            raise HouseholdError("saved product_id does not match the configured Oda provider")
        return product_id

    def _recurring(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "list")
        if action == "due":
            try:
                when = date.fromisoformat(str(request.get("date") or self._household_today().isoformat()))
            except ValueError as exc:
                raise HouseholdError("due date is invalid") from exc
            items = self.store.read()["recurring_items"]
            for item in items:
                self._product_id(item.get("product_id") if isinstance(item, Mapping) else None)
            return {"date": when.isoformat(), "due": [item for item in items if due_recurring(item, when)]}
        return self._items({**request, "action": action}, "recurring_items")


class Server:
    def __init__(self, path: Path, group: int, allowed_uid: int, app: Application):
        self.path = path
        self.group = group
        self.allowed_uid = allowed_uid
        self.app = app

    def run(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            os.chown(self.path, -1, self.group)
            os.chmod(self.path, 0o660)
            listener.listen(16)
            while True:
                connection, _ = listener.accept()
                threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            uid = peer_uid(connection)
            if uid not in {0, self.allowed_uid}:
                return
            data = b""
            while b"\n" not in data and len(data) <= MAX_REQUEST:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                data += chunk
            try:
                line, separator, _remainder = data.partition(b"\n")
                if not separator or len(line) > MAX_REQUEST:
                    raise HouseholdError("request exceeds the meal concierge size limit")
                request = strict_json_loads(line)
                if not isinstance(request, dict):
                    raise HouseholdError("request must be an object")
                response = {"ok": True, "result": self.app.handle(request)}
            except (HouseholdError, TypeError, ValueError, OverflowError, UnicodeError, RecursionError) as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                encoded = (json.dumps(response, ensure_ascii=True, allow_nan=False) + "\n").encode()
                if len(encoded) > MAX_REQUEST:
                    encoded = (json.dumps({"ok": False, "error": "response exceeds the meal concierge size limit"}) + "\n").encode()
            except (TypeError, ValueError, UnicodeError):
                encoded = (json.dumps({"ok": False, "error": "response contains an invalid JSON value"}) + "\n").encode()
            try:
                connection.sendall(encoded)
            except (BrokenPipeError, ConnectionResetError):
                return


def config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("invalid household config") from exc
    if not isinstance(value, dict) or not value.get("household"):
        raise SystemExit("invalid household config")
    provider = str(value.get("provider") or "oda").casefold()
    if provider not in {"oda", "meny"}:
        raise SystemExit("provider must be oda or meny")
    value["provider"] = provider
    confirmation_policy = str(value.get("confirmation_policy") or "fresh").casefold()
    if confirmation_policy not in {"fresh", "standing"}:
        raise SystemExit("confirmation_policy must be fresh or standing")
    value["confirmation_policy"] = confirmation_policy
    vipps_phone_number = value.get("vipps_phone_number")
    if vipps_phone_number is not None and (
        not isinstance(vipps_phone_number, str)
        or re.fullmatch(r"[0-9]{8}", vipps_phone_number) is None
    ):
        raise SystemExit("vipps_phone_number must be an 8-digit Norwegian mobile number")
    profile = value.get("email_automation_profile")
    if profile is not None and (not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", profile)):
        raise SystemExit("invalid email automation profile")
    try:
        value.update(normalize_library_configuration(value))
    except RecipeLibraryError as exc:
        raise SystemExit(str(exc)) from exc
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--state", type=Path, required=True)
    result.add_argument("--tokens", type=Path)
    result.add_argument("--socket", type=Path, default=Path("/tmp/meal-concierge.sock"))
    result.add_argument("--socket-group", type=int, default=os.getgid())
    result.add_argument("--agent-uid", type=int, default=os.getuid())
    result.add_argument("--browser-binary", type=Path, default=Path("agent-browser"))
    result.add_argument("--browser-executable", type=Path, default=Path(os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "/usr/bin/chromium")))
    result.add_argument("--browser-profile", type=Path, default=Path.home() / ".meal-concierge-browser" / "profile")
    result.add_argument("--browser-home", type=Path, default=Path.home() / ".meal-concierge-browser")
    result.add_argument("--browser-socket-directory", type=Path, default=Path("/tmp/meal-concierge-browser"))
    result.add_argument("--browser-cdp")
    result.add_argument("--browser-uid", type=int, default=os.getuid())
    result.add_argument("--browser-gid", type=int, default=os.getgid())
    return result


def load_library_secret_for_state(state_directory: Path, library_id: str) -> dict[str, Any]:
    """Resolve both the standard HOME/state and the state-root Compose layouts."""
    state = Path(state_directory)
    homes = [state, state.parent]
    matches = [home for home in homes if secret_path(home, library_id).exists()]
    if len(matches) > 1:
        raise RecipeLibraryError("recipe library credential location is ambiguous")
    if matches:
        return load_library_secret(matches[0], library_id)
    conventional = state.parent if state.name == "state" else state
    return load_library_secret(conventional, library_id)


def main() -> None:
    args = parser().parse_args()
    settings = config(args.config)
    browser_arguments = {
        "instance": str(settings.get("instance") or "household"),
        "binary": args.browser_binary,
        "executable": args.browser_executable,
        "profile": args.browser_profile,
        "home": args.browser_home,
        "socket_directory": args.browser_socket_directory,
        "uid": args.browser_uid,
        "gid": args.browser_gid,
    }
    if settings["provider"] == "oda":
        if args.tokens is None:
            raise SystemExit("--tokens is required for provider oda")
        provider_client = OdaClient(args.tokens)
    else:
        provider_client = MenyClient(
            **browser_arguments,
            cdp=args.browser_cdp,
            vipps_phone_number=settings.get("vipps_phone_number"),
        )
    email_provider_clients: dict[str, Any] = {}
    if settings["provider"] == "meny" and args.tokens is not None and args.tokens.is_dir():
        email_provider_clients["oda"] = OdaClient(args.tokens)
    recipe_library_adapters: dict[str, RecipeLibraryAdapter] = {}
    for connection in settings["recipe_libraries"]:
        library_id = connection["library_id"]
        if library_id == "builtin":
            continue
        try:
            credential = load_library_secret_for_state(args.state, library_id)
            recipe_library_adapters[library_id] = load_optional_adapter(connection, credential)
        except RecipeLibraryError:
            # An optional connection is reported unavailable by the recipes tool;
            # it must not block the built-in bank or grocery/order paths.
            continue
    app = Application(
        StateStore(args.state, settings),
        provider_client,
        OdaBrowser(**browser_arguments),
        email_provider_clients=email_provider_clients,
        recipe_library_adapters=recipe_library_adapters,
    )
    Server(args.socket, args.socket_group, args.agent_uid, app).run()


if __name__ == "__main__":
    main()
