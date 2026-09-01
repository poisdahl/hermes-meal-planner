#!/usr/bin/env python3
"""One small meal-planning service per household."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import html
import json
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

from oda_browser import OdaBrowser, delivery_signature as oda_delivery_signature
from core import (
    CancellationPreconditionError,
    CheckoutPreconditionError,
    HouseholdError,
    StateStore,
    cart_summary,
    due_recurring,
    mask_email,
    masked_status,
    put_item,
    remove_item,
)
from oda import OdaClient
from meny import MENY_CART_TIMEOUT, MENY_ORDER_TIMEOUT, MENY_READ_TIMEOUT, MenyClient, normalize_product_ref


MAX_REQUEST = 2 * 1024 * 1024
CANCELLATION_OPERATION_TIMEOUT = 105
MENY_CHECKOUT_OPERATION_TIMEOUT = 600
MENY_VIPPS_EXPIRY_BUFFER = timedelta(minutes=11)
UNRESOLVED_CHECKOUT_STATUSES = {"clicking", "uncertain", "awaiting_user_payment"}
SCHEDULE_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def peer_uid(connection: socket.socket) -> int:
    """Return the effective UID at the other end of a local Unix socket."""
    linux_option = getattr(socket, "SO_PEERCRED", None)
    if linux_option is not None:
        credentials = connection.getsockopt(socket.SOL_SOCKET, linux_option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return uid
    darwin_option = getattr(socket, "LOCAL_PEERCRED", None)
    if darwin_option is not None:
        credentials = connection.getsockopt(getattr(socket, "SOL_LOCAL", 0), darwin_option, 256)
        version, uid = struct.unpack_from("@II", credentials)
        if version != 0:
            raise PermissionError("unsupported local peer credential version")
        return uid
    raise RuntimeError("local peer credentials are unavailable")


def now() -> datetime:
    return datetime.now(timezone.utc)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def expired_awaiting_confirmation(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("status") != "awaiting_confirmation":
        return False
    try:
        expires_at = datetime.fromisoformat(str(value.get("expires_at") or ""))
    except ValueError:
        return False
    return expires_at.tzinfo is not None and now() >= expires_at


def scheduled_occurrence(schedule: Mapping[str, Any], current: datetime) -> str:
    weekday = SCHEDULE_WEEKDAYS.get(str(schedule.get("weekday") or "").casefold())
    clock = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", str(schedule.get("time") or ""))
    try:
        zone = ZoneInfo(str(schedule.get("timezone") or ""))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HouseholdError("schedule timezone is invalid") from exc
    if weekday is None or clock is None:
        raise HouseholdError("schedule weekday or time is invalid")
    local = current.astimezone(zone)
    due = local.replace(hour=int(clock[1]), minute=int(clock[2]), second=0, microsecond=0)
    if local.weekday() != weekday or local < due or local >= due + timedelta(minutes=30):
        raise HouseholdError("auto-checkout is not in its configured 30-minute schedule window")
    iso = due.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def meny_login_lost(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if "MENY login is required" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def menu_email_period(menu: Mapping[str, Any]) -> str:
    week = menu.get("week")
    if not isinstance(week, str) or not re.fullmatch(r"\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])", week):
        raise HouseholdError("email menu needs a valid ISO week")
    return week


def menu_email_html(menu: Mapping[str, Any], *, test: bool = False) -> str:
    escape = lambda value: html.escape(str(value or ""))

    def ingredients(values: Any) -> str:
        parts = []
        for value in values if isinstance(values, list) else []:
            if isinstance(value, Mapping):
                amount = str(value.get("amount") or "").strip()
                item = str(value.get("item") or value.get("name") or "").strip()
                text = " ".join(part for part in (amount, item) if part)
            else:
                text = str(value).strip()
            if text:
                parts.append(f"<li>{escape(text)}</li>")
        return "".join(parts)

    def steps(values: Any) -> str:
        if not isinstance(values, list):
            return ""
        return "".join(f"<li>{escape(value)}</li>" for value in values if str(value).strip())

    week = menu_email_period(menu)
    title = f"Ukesmeny og oppskrifter – {week}"
    parts = [
        "<!doctype html><html lang=\"no\"><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{escape(title)}</title>",
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;line-height:1.55;max-width:820px;margin:24px auto;padding:0 16px;color:#202124}h1,h2{color:#173f35}.recipe{border-top:2px solid #d7e4df;padding-top:12px;margin-top:28px}li{margin:6px 0}.test{background:#fff4ce;border:1px solid #e0b400;border-radius:6px;padding:10px}</style>",
        "</head><body>",
        f'<p class="test"><strong>TEST:</strong> Denne testmailen endrer ikke den planlagte utsendingen.</p>' if test else "",
        f"<h1>{escape(title)}</h1>",
    ]
    schedule = menu.get("schedule")
    if isinstance(schedule, list) and schedule:
        parts.append("<h2>Ukeplan</h2><ul>")
        for item in schedule:
            if isinstance(item, Mapping):
                day = escape(item.get("day"))
                meal = escape(item.get("meal") or item.get("action"))
                portions = escape(item.get("portions"))
                suffix = f" ({portions} porsjoner)" if portions else ""
                parts.append(f"<li><strong>{day}</strong>: {meal}{suffix}</li>")
        parts.append("</ul>")
    for heading, recipes in (("Middager", menu.get("dishes")), ("Salater", menu.get("salads"))):
        if not isinstance(recipes, list) or not recipes:
            continue
        parts.append(f"<h2>{heading}</h2>")
        for recipe in recipes:
            if not isinstance(recipe, Mapping):
                continue
            parts.extend([
                '<section class="recipe">',
                f"<h2>{escape(recipe.get('name'))}</h2>",
                f"<p>{escape(recipe.get('portions'))} porsjoner</p>" if recipe.get("portions") else "",
                "<h3>Ingredienser</h3><ul>",
                ingredients(recipe.get("ingredients")),
                "</ul><h3>Fremgangsmåte</h3><ol>",
                steps(recipe.get("steps")),
                "</ol>",
                f"<p><strong>Lagring:</strong> {escape(recipe.get('storage'))}</p>" if recipe.get("storage") else "",
                f"<p><strong>Oppvarming:</strong> {escape(recipe.get('reheating'))}</p>" if recipe.get("reheating") else "",
                "</section>",
            ])
    parts.append("</body></html>")
    return "".join(parts)


def delivery_matches(preference: Mapping[str, Any], delivery: Mapping[str, Any] | None) -> bool:
    if not delivery or not delivery.get("display"):
        return False
    display = str(delivery["display"]).casefold()
    weekday = str(preference.get("weekday") or "").casefold()
    translations = {
        "monday": "mandag", "tuesday": "tirsdag", "wednesday": "onsdag",
        "thursday": "torsdag", "friday": "fredag", "saturday": "lørdag",
        "sunday": "søndag",
    }
    if weekday and weekday not in display and translations.get(weekday) not in display:
        return False
    latest = str(preference.get("latest_end") or "")
    times = re.findall(r"\b([01]\d|2[0-3]):([0-5]\d)\b", display)
    if latest and times and f"{times[-1][0]}:{times[-1][1]}" > latest:
        return False
    return True


def order_matches_checkout(order: Mapping[str, Any], summary: Mapping[str, Any]) -> bool:
    products = order.get("products")
    if not isinstance(products, list):
        return False
    try:
        observed = cart_summary({
            "items": products,
            "totalGrossAmount": order.get("grossAmount"),
            "deliverySlot": {"name": order.get("deliverySlotDisplay")},
        })
    except HouseholdError:
        return False

    def items(value: Mapping[str, Any]) -> list[tuple[str, int]] | None:
        result = []
        for item in value.get("items", []):
            product_id = str(item.get("product_id") or "")
            quantity = item.get("quantity")
            if not product_id or not isinstance(quantity, int):
                return None
            result.append((product_id, quantity))
        return sorted(result)

    def delivery_signature(display: str, iso_date: str = "") -> tuple[tuple[int, int], tuple[str, ...]] | None:
        date_keys: set[tuple[int, int]] = set()
        if iso_date:
            try:
                parsed = date.fromisoformat(iso_date)
            except ValueError:
                return None
            date_keys.add((parsed.month, parsed.day))
        for embedded in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", display):
            try:
                parsed = date.fromisoformat(embedded)
            except ValueError:
                return None
            date_keys.add((parsed.month, parsed.day))
        months = {
            "jan": 1, "januar": 1,
            "feb": 2, "februar": 2,
            "mar": 3, "mars": 3,
            "apr": 4, "april": 4,
            "mai": 5,
            "jun": 6, "juni": 6,
            "jul": 7, "juli": 7,
            "aug": 8, "august": 8,
            "sep": 9, "sept": 9, "september": 9,
            "okt": 10, "oktober": 10,
            "nov": 11, "november": 11,
            "des": 12, "desember": 12,
        }
        for match in re.finditer(r"\b(\d{1,2})\.\s*([A-Za-zÆØÅæøå]+)", display):
            month = months.get(match.group(2).casefold())
            if not month:
                return None
            try:
                date(2000, month, int(match.group(1)))
            except ValueError:
                return None
            date_keys.add((month, int(match.group(1))))
        if len(date_keys) != 1:
            return None
        date_key = next(iter(date_keys))
        explicit_times = tuple(re.findall(r"\b(?:[01]\d|2[0-3]):[0-5]\d(?=$|\s|[-–,])", display))
        compact = re.search(
            r"\bmellom\s+(?:kl\.?\s*)?([01]?\d|2[0-3])(?:[:.]([0-5]\d))?(?![:.]\d)\s+og\s+([01]?\d|2[0-3])(?:[:.]([0-5]\d))?(?![:.]\d)\b",
            display,
            re.IGNORECASE,
        )
        compact_times = None
        if compact:
            compact_times = (
                f"{int(compact.group(1)):02d}:{compact.group(2) or '00'}",
                f"{int(compact.group(3)):02d}:{compact.group(4) or '00'}",
            )
        if compact_times and explicit_times and explicit_times != compact_times:
            return None
        times = compact_times or explicit_times
        return (date_key, times) if date_key and len(times) == 2 else None

    summary_delivery = summary.get("delivery")
    expected_delivery = summary_delivery.get("display") if isinstance(summary_delivery, Mapping) else None
    observed_delivery = order.get("deliverySlotDisplay")
    observed_date = order.get("deliveryDate")
    if not isinstance(expected_delivery, str) or not isinstance(observed_delivery, str):
        return False
    if observed_date is not None and not isinstance(observed_date, str):
        return False
    expected_signature = delivery_signature(expected_delivery)
    observed_signature = delivery_signature(observed_delivery, observed_date or "")
    return (
        items(observed) is not None
        and items(observed) == items(summary)
        and int(round(observed["total"] * 100)) == int(round(float(summary.get("total")) * 100))
        and expected_signature is not None
        and observed_signature == expected_signature
    )


def meny_order_matches_checkout(order: Mapping[str, Any], summary: Mapping[str, Any]) -> bool:
    def delivery_signature(value: Any) -> tuple[tuple[int, int], tuple[str, str]] | None:
        if not isinstance(value, str):
            return None
        months = {
            "jan": 1, "januar": 1, "feb": 2, "februar": 2, "mar": 3, "mars": 3,
            "apr": 4, "april": 4, "mai": 5, "jun": 6, "juni": 6, "jul": 7,
            "juli": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
            "okt": 10, "oktober": 10, "nov": 11, "november": 11, "des": 12, "desember": 12,
        }
        dates = set()
        date_matches = list(re.finditer(r"\b(\d{1,2})\.\s*([A-Za-zÆØÅæøå]+)", value))
        for match in date_matches:
            month = months.get(match.group(2).casefold().rstrip("."))
            if not month:
                return None
            try:
                date(2000, month, int(match.group(1)))
            except ValueError:
                return None
            dates.add((month, int(match.group(1))))
        times = re.findall(r"\b(?:[01]\d|2[0-3]):[0-5]\d(?=$|\s|[-–,])", value)
        return (next(iter(dates)), (times[0], times[1])) if len(dates) == 1 and len(times) == 2 else None

    try:
        total_matches = int(round(float(order.get("grossAmount")) * 100)) == int(round(float(summary.get("total")) * 100))
    except (TypeError, ValueError, OverflowError):
        return False
    def lines(value: Any, *, key: str, require_product_id: bool = False) -> dict[str, int] | None:
        if not isinstance(value, list) or not value:
            return None
        result: dict[str, int] = {}
        identities: dict[str, str] = {}
        for item in value:
            if not isinstance(item, Mapping):
                return None
            identity = re.sub(r"\s+", " ", str(item.get(key) or item.get("name") or "")).strip().casefold()
            quantity = item.get("quantity")
            if not identity or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                return None
            if require_product_id:
                product_id = str(item.get("product_id") or "")
                if not product_id or (identity in identities and identities[identity] != product_id):
                    return None
                identities[identity] = product_id
            result[identity] = result.get(identity, 0) + quantity
        return result

    expected_lines = lines(summary.get("order_lines"), key="identity", require_product_id=True)
    observed_lines = lines(order.get("products"), key="identity")
    count = order.get("productQuantityCount")
    delivery = summary.get("delivery")
    return (
        expected_lines is not None
        and observed_lines == expected_lines
        and not isinstance(count, bool)
        and isinstance(count, int)
        and count == summary.get("count")
        and total_matches
        and isinstance(delivery, Mapping)
        and delivery_signature(order.get("deliverySlotDisplay")) is not None
        and delivery_signature(order.get("deliverySlotDisplay")) == delivery_signature(delivery.get("display"))
        and summary.get("payment") == "vipps"
    )


def oda_order_matches_addition(before: Mapping[str, Any], after: Mapping[str, Any], additions: Mapping[str, Any]) -> bool:
    def quantities(order: Mapping[str, Any]) -> dict[str, int] | None:
        products = order.get("products")
        if not isinstance(products, list):
            return None
        try:
            summary = cart_summary({"items": products, "total": order.get("grossAmount", 0)})
        except HouseholdError:
            return None
        result: dict[str, int] = {}
        for item in summary["items"]:
            product_id = item["product_id"]
            if not product_id:
                return None
            result[product_id] = result.get(product_id, 0) + item["quantity"]
        return result

    expected = quantities(before)
    observed = quantities(after)
    if expected is None or observed is None:
        return False
    for item in additions.get("items", []):
        if not isinstance(item, Mapping) or not item.get("product_id") or not isinstance(item.get("quantity"), int):
            return False
        product_id = str(item["product_id"])
        expected[product_id] = expected.get(product_id, 0) + item["quantity"]
    try:
        expected_total = float(before.get("grossAmount")) + float(additions.get("total"))
        total_matches = int(round(expected_total * 100)) == int(round(float(after.get("grossAmount")) * 100))
    except (TypeError, ValueError, OverflowError):
        return False
    return observed == expected and total_matches


class Application:
    def __init__(self, store: StateStore, provider_client: Any, browser: OdaBrowser):
        self.store = store
        self.oda = provider_client
        self.provider = str(store.config.get("provider") or "oda").casefold()
        self.confirmation_policy = str(store.config.get("confirmation_policy") or "fresh").casefold()
        self.browser = provider_client if self.provider == "meny" else browser
        self.browser_lock = threading.Lock()
        self.email_automation_profile = str(store.config.get("email_automation_profile") or "").strip()
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
    def _browser_operation(self, deadline: float | None = None):
        if deadline is None:
            acquired = self.browser_lock.acquire()
        else:
            remaining = deadline - time.monotonic()
            acquired = remaining > 0 and self.browser_lock.acquire(timeout=remaining)
        if not acquired:
            raise HouseholdError("provider browser deadline reached")
        try:
            yield
        finally:
            self.browser_lock.release()

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            result = self._handle(request)
        except HouseholdError as exc:
            if self.provider == "meny" and meny_login_lost(exc):
                self.integration = {
                    "status": "awaiting_login",
                    "provider": "meny",
                    "message": str(exc),
                }
            raise
        if self.provider == "meny" and request.get("operation") in {"catalog", "cart", "delivery", "orders", "checkout"}:
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
        if operation == "health":
            return {"ok": True, "integration": self.integration}
        if operation == "status":
            if self.provider == "meny" and self.integration.get("status") != "ready":
                deadline = time.monotonic() + MENY_READ_TIMEOUT
                with self._browser_operation(deadline):
                    state = self.store.read()
                    pending_status = (state.get("pending_checkout") or {}).get("status")
                    if pending_status not in UNRESOLVED_CHECKOUT_STATUSES:
                        safe = not state.get("pending_checkout") and not state.get("pending_cancellation") and not state.get("order_change")
                        self._refresh_integration(deadline, allow_recovery=safe)
            return {
                **masked_status(self.store.read(), self.integration),
                "confirmation_policy": self.confirmation_policy,
            }
        if operation == "profile":
            return self._profile(request)
        if operation == "favorites":
            return self._items(request, "favorites")
        if operation == "recurring":
            return self._recurring(request)
        if operation == "menu":
            return self._menu(request)
        if operation == "schedule":
            return self._schedule(request)
        action = request.get("action")
        meny_read = self.provider == "meny" and (
            operation == "catalog"
            or (operation == "cart" and action in {None, "get"})
            or (operation == "delivery" and action in {None, "list"})
            or (operation == "orders" and action in {None, "list", "get"})
            or (operation == "email" and action == "due")
        )
        if meny_read:
            timeout = MENY_ORDER_TIMEOUT if operation in {"delivery", "orders"} else MENY_READ_TIMEOUT
            deadline = time.monotonic() + timeout
            with self._browser_operation(deadline):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending MENY checkout before using another browser operation")
                safe = not state.get("pending_checkout") and not state.get("pending_cancellation") and not state.get("order_change")
                guarded = {**request, "_deadline": deadline, "_allow_browser_recovery": safe}
                if operation == "catalog":
                    return self._catalog(guarded)
                if operation == "cart":
                    return self._cart(guarded)
                if operation == "delivery":
                    return self._delivery(guarded)
                if operation == "orders":
                    return self._orders(guarded)
                return self._email(guarded)
        if self.provider == "meny" and operation in {"catalog", "cart", "delivery", "orders"}:
            pending_status = (self.store.read().get("pending_checkout") or {}).get("status")
            if pending_status in UNRESOLVED_CHECKOUT_STATUSES:
                raise HouseholdError("reconcile the pending MENY checkout before using another browser operation")
        if operation == "catalog":
            return self._catalog(request)
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
            return {"profile": self.store.update_profile(request.get("changes", {}))}
        if action == "reset":
            paths = request.get("paths")
            if paths is not None and (not isinstance(paths, list) or not all(isinstance(item, str) for item in paths)):
                raise HouseholdError("reset paths must be strings")
            return {"profile": self.store.reset_profile(paths)}
        if action == "set_email":
            email = str(request.get("email") or "").strip()
            if not email or "@" not in email or len(email) > 254:
                raise HouseholdError("email address is invalid")
            with self.store.locked() as state:
                state["email_recipient"] = email
            return {"email_recipient": mask_email(email)}
        raise HouseholdError("unknown profile action")

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
                when = date.fromisoformat(str(request.get("date") or date.today().isoformat()))
            except ValueError as exc:
                raise HouseholdError("due date is invalid") from exc
            items = self.store.read()["recurring_items"]
            for item in items:
                self._product_id(item.get("product_id") if isinstance(item, Mapping) else None)
            return {"date": when.isoformat(), "due": [item for item in items if due_recurring(item, when)]}
        return self._items({**request, "action": action}, "recurring_items")

    def _menu(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "get")
        with self.store.locked() as state:
            if action == "get":
                return {"menu": deepcopy(state.get("menu"))}
            if action == "clear":
                if expired_awaiting_confirmation(state.get("pending_checkout")):
                    state["pending_checkout"] = None
                elif state.get("pending_checkout"):
                    raise HouseholdError("checkout is pending; the menu was not cleared")
                state["menu"] = None
                return {"menu": None}
            if action == "save":
                menu = request.get("menu")
                if not isinstance(menu, Mapping) or not isinstance(menu.get("week"), str):
                    raise HouseholdError("menu needs an ISO week")
                menu_email_period(menu)
                recipes = list(menu.get("dishes", [])) + list(menu.get("salads", []))
                if not recipes or any(not isinstance(item, Mapping) or not item.get("name") or not item.get("ingredients") or not item.get("steps") for item in recipes):
                    raise HouseholdError("menu needs complete recipes with ingredients and steps")
                state["menu"] = deepcopy(dict(menu))
                state["menu"].setdefault("phase", "draft")
                return {"menu": deepcopy(state["menu"])}
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
                schedule.update(deepcopy(changes))
                if schedule.get("mode") not in {"draft", "cart_ready", "auto_checkout"}:
                    raise HouseholdError("schedule mode is invalid")
                if schedule.get("auto_checkout"):
                    if self.provider != "oda":
                        raise HouseholdError("MENY supports cart_ready scheduling; checkout continues manually in the browser")
                    if not isinstance(schedule.get("maximum_total"), (int, float)) or not schedule.get("delivery"):
                        raise HouseholdError("auto-checkout requires maximum total and delivery preference")
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
            query = str(request.get("query") or "").strip()
            return self.oda.call("product_search", {"queries": [query], "page": 1, "size": int(request.get("limit", 5))}, **kwargs)
        if action == "recipes":
            return self.oda.call("recipe_search", {"query": str(request.get("query") or ""), "page": 1, "size": int(request.get("limit", 5))}, **kwargs)
        if action == "usuals":
            return self.oda.call("likely_to_buy", {}, **kwargs)
        raise HouseholdError("unknown catalog action")

    def _cart(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "get")
        if action == "get":
            return self.oda.call("get_cart", {}, deadline=request.get("_deadline"), allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.oda.call("get_cart", {})
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
                    item["productId"] = int(product_id) if self.provider == "oda" else str(product_id)
                normalized.append(item)
            with self._browser_operation(deadline):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before changing the cart")
                change = deepcopy(state.get("order_change"))
                if change and change.get("status") != "editing":
                    raise HouseholdError("the order change is still starting")
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

    @staticmethod
    def _find_delivery_slot(value: Any, slot_id: Any) -> dict[str, Any] | None:
        if isinstance(value, Mapping):
            candidate_id = value.get("id", value.get("slot_id", value.get("deliverySlotId")))
            if candidate_id is not None and str(candidate_id) == str(slot_id):
                display = value.get("name", value.get("display", value.get("description")))
                if isinstance(display, str) and display.strip():
                    return {"slot_id": candidate_id, "display": display.strip()}
            for child in value.values():
                if found := Application._find_delivery_slot(child, slot_id):
                    return found
        elif isinstance(value, list):
            for child in value:
                if found := Application._find_delivery_slot(child, slot_id):
                    return found
        return None

    def _delivery(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "list")
        if action == "list":
            arguments: dict[str, Any] = {}
            if request.get("address_id") is not None:
                arguments["delivery_address_id"] = request["address_id"]
            dates = request.get("dates")
            if dates is None:
                return self.oda.call("get_delivery_slots", arguments, deadline=request.get("_deadline"), allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.oda.call("get_delivery_slots", arguments)
            if not isinstance(dates, list) or not dates or len(dates) > 7 or not all(isinstance(item, str) for item in dates):
                raise HouseholdError("delivery dates must be one to seven ISO dates")
            return {"dates": [{"date": item, "slots": self.oda.call("get_delivery_slots", {**arguments, "delivery_date": item}, deadline=request.get("_deadline"), allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.oda.call("get_delivery_slots", {**arguments, "delivery_date": item})} for item in dates]}
        if action == "select":
            arguments = {"delivery_slot_id": request.get("slot_id")}
            if request.get("address_id") is not None:
                arguments["delivery_address_id"] = request["address_id"]
            if request.get("unattended") is not None:
                arguments["is_unattended_delivery"] = bool(request["unattended"])
            deadline = time.monotonic() + MENY_ORDER_TIMEOUT if self.provider == "meny" else None
            with self._browser_operation(deadline):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before changing delivery")
                change = deepcopy(state.get("order_change"))
                if change and change.get("status") != "editing":
                    raise HouseholdError("the order change is still starting")
                if self.provider == "oda" and change:
                    cart = cart_summary(self.oda.call("get_cart", {}))
                    if cart["items"]:
                        raise HouseholdError("an Oda delivery-window change must be prepared without staged item additions")
                    available = self.oda.call("get_delivery_slots", {})
                    selected = self._find_delivery_slot(available, request.get("slot_id"))
                    if not selected:
                        raise HouseholdError("the requested Oda delivery slot is no longer available")
                    with self.store.locked() as locked:
                        if canonical(locked.get("order_change")) != canonical(change):
                            raise HouseholdError("order change state changed while selecting delivery")
                        locked["order_change"]["kind"] = "delivery"
                        locked["order_change"]["requested_delivery"] = selected
                    return {"provider": "oda", "selected": selected, "staged_for_order": change["order_id"], "next": "Prepare checkout, review the exact new window and any payment difference, then ask for confirmation."}
                if self.provider == "meny":
                    self.browser.verify_order_change(
                        change.get("order_id") if change else None,
                        change.get("code") if change else None,
                        deadline=deadline,
                    )
                    result = self.oda.call("select_delivery_slot", arguments, deadline=deadline)
                    self.browser.verify_order_change(
                        change.get("order_id") if change else None,
                        change.get("code") if change else None,
                        deadline=deadline,
                    )
                    if change:
                        with self.store.locked() as locked:
                            if canonical(locked.get("order_change")) != canonical(change):
                                raise HouseholdError("order change state changed while selecting delivery")
                            locked["order_change"]["kind"] = "full_order"
                    return result
                return self.oda.call("select_delivery_slot", arguments)
        raise HouseholdError("unknown delivery action")

    def _orders(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "list")
        cancellation_deadline = time.monotonic() + CANCELLATION_OPERATION_TIMEOUT if action in {"cancel_prepare", "cancel_confirm", "cancel_reconcile", "cancel_submit"} else None
        if action == "list":
            return self.oda.call("get_orders", {"page": 1, "size": int(request.get("limit", 10))}, deadline=request.get("_deadline"), allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.oda.call("get_orders", {"page": 1, "size": int(request.get("limit", 10))})
        order_id = str(request.get("order_id") or "")
        if action == "get":
            deadline = request.get("_deadline") if self.provider == "meny" else None
            return {
                "order": self.oda.call("get_order", {"order_number": order_id}, deadline=deadline, allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.oda.call("get_order", {"order_number": order_id}),
                "tracking": self.oda.call("order_tracking", {"order_number": order_id}, deadline=deadline, allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.oda.call("order_tracking", {"order_number": order_id}),
            }
        if action == "change_begin":
            if not order_id:
                raise HouseholdError("order_id is required for an order change")
            deadline = time.monotonic() + MENY_ORDER_TIMEOUT if self.provider == "meny" else None
            reservation = {
                "provider": self.provider,
                "order_id": order_id,
                "status": "starting",
                "token": secrets.token_urlsafe(18),
                "started_at": now().isoformat(),
            }
            with self.store.locked() as state:
                if state.get("order_change"):
                    raise HouseholdError("another order change is active; abort it before changing a different order")
                if state.get("pending_checkout") or state.get("pending_cancellation"):
                    raise HouseholdError("finish the pending protected operation before changing an order")
            with self._browser_operation(deadline):
                with self.store.locked() as state:
                    active = deepcopy(state.get("order_change"))
                    if active:
                        raise HouseholdError("another order change is active; abort it before changing a different order")
                    if state.get("pending_checkout") or state.get("pending_cancellation"):
                        raise HouseholdError("finish the pending protected operation before changing an order")
                    state["order_change"] = deepcopy(reservation)
            try:
                current = self._orders({"action": "get", "order_id": order_id, "_deadline": deadline})
                status = str((current.get("tracking") or {}).get("status") or "").casefold()
                if self.provider == "oda" and status != "paid_and_modifiable":
                    raise HouseholdError("Oda order is not currently modifiable")
                if self.provider == "meny":
                    with self._browser_operation(deadline):
                        started = self.browser.begin_order_change(order_id, deadline=deadline)
                    code = str(started.get("code") or "").strip()
                    if not code:
                        raise HouseholdError("MENY order change identity is unavailable")
                else:
                    cart = cart_summary(self.oda.call("get_cart", {}))
                    if cart["items"]:
                        raise HouseholdError("empty the Oda cart before starting an addition to an existing order")
                    started = {"provider": "oda", "order_id": order_id, "editing": True}
                    code = ""
                change = {
                    "provider": self.provider,
                    "order_id": order_id,
                    "before": current,
                    "status": "editing",
                    "started_at": reservation["started_at"],
                    **({"code": code} if code else {}),
                }
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) != canonical(reservation):
                        raise HouseholdError("order change state changed while starting")
                    state["order_change"] = change
            except Exception:
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) == canonical(reservation):
                        state["order_change"] = None
                raise
            return {**started, **change, "next": "Change the cart or delivery, then prepare checkout for this exact order."}
        if action == "change_abort":
            with self.store.locked() as state:
                change = deepcopy(state.get("order_change"))
                if state.get("pending_checkout"):
                    raise HouseholdError("finish or reconcile the prepared checkout before aborting the order change")
            if not change or (order_id and order_id != change.get("order_id")):
                raise HouseholdError("no matching order change is active")
            if change.get("status") == "starting":
                try:
                    started_at = datetime.fromisoformat(str(change.get("started_at") or ""))
                except (TypeError, ValueError) as exc:
                    raise HouseholdError("the starting order change cannot be recovered safely") from exc
                try:
                    still_starting = now() < started_at + timedelta(minutes=5)
                except TypeError as exc:
                    raise HouseholdError("the starting order change cannot be recovered safely") from exc
                if still_starting:
                    raise HouseholdError("the order change is still starting")
            deadline = time.monotonic() + MENY_ORDER_TIMEOUT if self.provider == "meny" else None
            with self._browser_operation(deadline):
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) != canonical(change):
                        raise HouseholdError("order change state changed before aborting")
                if change.get("status") == "starting":
                    if self.provider == "meny":
                        current = self._orders({"action": "get", "order_id": change["order_id"], "_deadline": deadline})
                        code = str((current.get("order") or {}).get("code") or "")
                        try:
                            self.browser.verify_order_change(change["order_id"], code, deadline=deadline)
                        except HouseholdError:
                            self.browser.verify_order_change(None, None, deadline=deadline)
                            result = {"provider": "meny", "order_id": change["order_id"], "aborted": True, "recovered": True}
                        else:
                            result = self.browser.abort_order_change(change["order_id"], code, deadline=deadline)
                    else:
                        result = {"provider": "oda", "order_id": change["order_id"], "aborted": True, "recovered": True}
                elif self.provider == "meny":
                    result = self.browser.abort_order_change(change["order_id"], change.get("code"), deadline=deadline)
                else:
                    if cart_summary(self.oda.call("get_cart", {}))["items"]:
                        raise HouseholdError("remove the staged Oda additions before aborting the order change")
                    result = {"provider": "oda", "order_id": change["order_id"], "aborted": True}
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) != canonical(change):
                        raise HouseholdError("order change state changed while aborting")
                    state["order_change"] = None
            return result
        if action == "cancel_prepare":
            if not order_id:
                raise HouseholdError("order_id is required for cancellation")
            with self.store.locked() as state:
                baseline = deepcopy(state.get("pending_cancellation"))
                if baseline and baseline.get("status") in {"clicking", "uncertain"}:
                    raise HouseholdError("reconcile the pending cancellation before preparing another")
                if state.get("order_change"):
                    raise HouseholdError("finish or abort the active order change before cancellation")
            current = self._orders({"action": "get", "order_id": order_id, "_deadline": cancellation_deadline})
            with self._browser_operation(cancellation_deadline):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before preparing cancellation")
                if state.get("order_change"):
                    raise HouseholdError("finish or abort the active order change before cancellation")
                browser = self.browser.review_cancellation(order_id, current["order"], deadline=cancellation_deadline)
                if browser.get("available") is not True:
                    return browser
                confirmation_id = secrets.token_urlsafe(18)
                with self.store.locked() as state:
                    if canonical(state.get("pending_cancellation")) != canonical(baseline):
                        raise HouseholdError("cancellation state changed while preparing the summary")
                    state["pending_cancellation"] = {"order_id": order_id, "confirmation_id": confirmation_id, "before": current, "browser": browser, "expires_at": (now() + timedelta(minutes=30)).isoformat(), "status": "awaiting_confirmation"}
                return {
                    "available": True,
                    "confirmation_id": confirmation_id,
                    "confirmation_policy": self.confirmation_policy,
                    "confirmation_required": self.confirmation_policy == "fresh",
                    "order": current["order"],
                    "tracking": current["tracking"],
                    "consequence": browser.get("consequence"),
                    "next": (
                        "Ask once for explicit confirmation of this exact order, then pass this confirmation_id unchanged."
                        if self.confirmation_policy == "fresh"
                        else "Standing authorization is configured. If the current request explicitly asks to cancel this order, call cancel_confirm now with this confirmation_id; do not ask again."
                    ),
                }
        if action == "cancel_confirm":
            return self._cancel(
                action,
                cancellation_deadline,
                order_id=order_id,
                confirmation_id=str(request.get("confirmation_id") or ""),
            )
        if action == "cancel_submit":
            if self.confirmation_policy != "standing":
                raise HouseholdError("standing authorization is not configured; prepare cancellation and ask for confirmation")
            prepared = self._orders({"action": "cancel_prepare", "order_id": order_id})
            if prepared.get("available") is not True:
                return prepared
            result = self._cancel(
                "cancel_confirm",
                cancellation_deadline,
                order_id=order_id,
                confirmation_id=str(prepared.get("confirmation_id") or ""),
            )
            return {**result, "authorized_summary": {"order": prepared.get("order"), "tracking": prepared.get("tracking"), "consequence": prepared.get("consequence")}}
        if action == "cancel_reconcile":
            return self._cancel(action, cancellation_deadline)
        raise HouseholdError("unknown order action")

    def _cancel(
        self,
        action: str,
        deadline: float | None = None,
        *,
        order_id: str = "",
        confirmation_id: str = "",
    ) -> dict[str, Any]:
        if action == "cancel_reconcile":
            return self._cancel_reconcile(deadline)
        with self.store.locked() as state:
            pending = deepcopy(state.get("pending_cancellation"))
        if not pending:
            raise HouseholdError("no order cancellation is pending")
        if order_id != pending.get("order_id") or confirmation_id != pending.get("confirmation_id"):
            raise HouseholdError("cancellation confirmation does not match the prepared order")
        if now() >= datetime.fromisoformat(pending["expires_at"]):
            raise HouseholdError("cancellation confirmation expired")
        current = self._orders({"action": "get", "order_id": order_id, "_deadline": deadline})
        if pending["status"] != "awaiting_confirmation" or canonical(current) != canonical(pending["before"]):
            raise HouseholdError("the order changed; ask for a new cancellation confirmation")
        with self._browser_operation(deadline):
            with self.store.locked() as state:
                current_pending = state.get("pending_cancellation")
                if not current_pending or current_pending.get("status") != "awaiting_confirmation" or canonical(current_pending) != canonical(pending):
                    raise HouseholdError("no fresh cancellation confirmation is pending")
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before cancelling an order")
                state["pending_cancellation"]["status"] = "clicking"
            pending["status"] = "clicking"

            def before_click() -> None:
                if now() >= datetime.fromisoformat(pending["expires_at"]):
                    raise CancellationPreconditionError("cancellation confirmation expired before the final click")
                with self.store.locked() as state:
                    current_pending = state.get("pending_cancellation")
                    if not current_pending or current_pending.get("status") != "clicking":
                        raise CancellationPreconditionError("cancellation confirmation changed before the final click")
                    expected = {**pending, "status": "clicking"}
                    if canonical(current_pending) != canonical(expected):
                        raise CancellationPreconditionError("cancellation confirmation changed before the final click")
            try:
                self.browser.submit_cancellation(
                    order_id,
                    current["order"],
                    pending["browser"],
                    before_click,
                    deadline=deadline,
                )
                tracking = self.oda.call("order_tracking", {"order_number": order_id}, deadline=deadline) if self.provider == "meny" else self.oda.call("order_tracking", {"order_number": order_id})
                current = {"order": current["order"], "tracking": tracking}
            except CancellationPreconditionError:
                with self.store.locked() as state:
                    current_pending = state.get("pending_cancellation")
                    if current_pending and current_pending.get("status") == "clicking" and current_pending.get("browser") == pending.get("browser"):
                        state["pending_cancellation"] = None
                raise
            except HouseholdError:
                with self.store.locked() as state:
                    current_pending = state.get("pending_cancellation")
                    if current_pending and current_pending.get("status") == "clicking" and current_pending.get("browser") == pending.get("browser"):
                        state["pending_cancellation"]["status"] = "uncertain"
                raise
            cancelled = str(tracking.get("status") or "").casefold() in {"cancelled", "canceled"}
            with self.store.locked() as state:
                if canonical(state.get("pending_cancellation")) != canonical(pending):
                    raise HouseholdError("cancellation state changed while reconciling the order")
                if cancelled:
                    state["pending_cancellation"] = None
                    for job in state["email_jobs"]:
                        if job.get("order_id") == order_id and job.get("status") == "pending":
                            job["status"] = "cancelled"
                else:
                    state["pending_cancellation"]["status"] = "uncertain"
        return {"cancelled": cancelled, "tracking": current["tracking"], "retry_allowed": False}

    def _cancel_reconcile(self, deadline: float | None = None) -> dict[str, Any]:
        with self._browser_operation(deadline):
            with self.store.locked() as state:
                pending = deepcopy(state.get("pending_cancellation"))
            if not pending:
                raise HouseholdError("no order cancellation is pending")
            if pending.get("status") not in {"clicking", "uncertain"}:
                raise HouseholdError("cancellation has not reached reconciliation")
            order_id = pending["order_id"]
            current = self._orders({"action": "get", "order_id": order_id, "_deadline": deadline})
            cancelled = str((current.get("tracking") or {}).get("status") or "").casefold() in {"cancelled", "canceled"}
            with self.store.locked() as state:
                if canonical(state.get("pending_cancellation")) != canonical(pending):
                    raise HouseholdError("cancellation state changed while reconciling the order")
                if cancelled:
                    state["pending_cancellation"] = None
                    for job in state["email_jobs"]:
                        if job.get("order_id") == order_id and job.get("status") == "pending":
                            job["status"] = "cancelled"
                else:
                    state["pending_cancellation"]["status"] = "uncertain"
            return {"cancelled": cancelled, "tracking": current["tracking"], "retry_allowed": False}

    def _checkout(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "prepare")
        deadline = time.monotonic() + (MENY_CHECKOUT_OPERATION_TIMEOUT if self.provider == "meny" else 240)
        if action == "prepare":
            return self._checkout_prepare(deadline)
        if action == "confirm":
            return self._checkout_confirm(deadline, str(request.get("confirmation_id") or ""))
        if action == "submit":
            if self.confirmation_policy != "standing":
                raise HouseholdError("standing authorization is not configured; prepare checkout and ask for confirmation")
            with self.store.locked() as state:
                pending = deepcopy(state.get("pending_checkout"))
            if pending and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending):
                prepared = {
                    "confirmation_id": pending["confirmation_id"],
                    "summary": deepcopy(pending["summary"]),
                    "order_change": deepcopy(pending.get("order_change")),
                }
            else:
                prepared = self._checkout_prepare(deadline)
            result = self._checkout_confirm(deadline, prepared["confirmation_id"])
            return {**result, "authorized_summary": prepared["summary"], "order_change": prepared.get("order_change")}
        if action == "reconcile":
            return self._checkout_reconcile(deadline)
        if action == "auto":
            occurrence = str(request.get("occurrence") or "")
            with self.store.locked() as state:
                schedule = deepcopy(state["schedule"])
                if not schedule.get("enabled") or not schedule.get("auto_checkout"):
                    raise HouseholdError("auto-checkout is off")
                if not isinstance(schedule.get("cron_job_id"), str) or not schedule["cron_job_id"].strip():
                    raise HouseholdError("auto-checkout is not linked to its configured cron job")
                expected_occurrence = scheduled_occurrence(schedule, now())
                if state.get("order_change"):
                    raise HouseholdError("scheduled checkout cannot submit an interactive order change")
                if occurrence != expected_occurrence:
                    raise HouseholdError("scheduled occurrence does not match the currently due local week")
                if occurrence in state["occurrences"]:
                    raise HouseholdError("this scheduled occurrence was already handled")
                state["occurrences"][occurrence] = {"status": "started", "at": now().isoformat()}
            try:
                prepared = self._checkout_prepare(deadline)
            except HouseholdError:
                with self.store.locked() as state:
                    state["occurrences"][occurrence]["status"] = "needs_input"
                raise
            total = prepared["summary"]["total"]
            if not isinstance(total, (int, float)) or total > schedule["maximum_total"]:
                with self.store.locked() as state:
                    state["occurrences"][occurrence]["status"] = "needs_input"
                return {"completed": False, "reason": "total exceeds maximum", "summary": prepared["summary"]}
            if not delivery_matches(schedule["delivery"], prepared["summary"].get("delivery")):
                with self.store.locked() as state:
                    state["occurrences"][occurrence]["status"] = "needs_input"
                return {"completed": False, "reason": "delivery does not match preference", "summary": prepared["summary"]}
            if self.confirmation_policy == "standing":
                try:
                    result = self._checkout_confirm(deadline, prepared["confirmation_id"])
                except HouseholdError:
                    with self.store.locked() as state:
                        state["occurrences"][occurrence]["status"] = "needs_input"
                    raise
                with self.store.locked() as state:
                    state["occurrences"][occurrence]["status"] = "completed" if result.get("confirmed") is True else "needs_input"
                return {**result, "completed": result.get("confirmed") is True, "authorized_summary": prepared["summary"]}
            with self.store.locked() as state:
                state["occurrences"][occurrence]["status"] = "awaiting_confirmation"
            return {
                "completed": False,
                "confirmed": False,
                "awaiting_confirmation": True,
                "confirmation_id": prepared["confirmation_id"],
                "summary": prepared["summary"],
                "next": "Show this exact guarded scheduled summary and require a fresh explicit user confirmation before checkout confirm.",
            }
        raise HouseholdError("unknown checkout action")

    def _checkout_prepare(self, deadline: float | None = None) -> dict[str, Any]:
        with self.store.locked() as state:
            baseline = deepcopy(state.get("pending_checkout"))
            if baseline and baseline.get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                raise HouseholdError("reconcile the pending checkout before preparing another")
            order_change = deepcopy(state.get("order_change"))
            if expired_awaiting_confirmation(state.get("pending_cancellation")):
                state["pending_cancellation"] = None
            pending_cancellation = deepcopy(state.get("pending_cancellation"))
        allow_recovery = self.provider == "meny" and not order_change and not pending_cancellation
        if order_change:
            if order_change.get("provider") != self.provider or order_change.get("status") != "editing":
                raise HouseholdError("the order change is not ready for checkout; abort or recover it first")
        if order_change and self.provider == "oda":
            fresh_target = self._orders({"action": "get", "order_id": order_change["order_id"]})
            if canonical(fresh_target) != canonical(order_change["before"]):
                raise HouseholdError("the target Oda order changed; begin the order change again")
        cart = self.oda.call("get_cart", {}, deadline=deadline, allow_recovery=allow_recovery) if self.provider == "meny" else self.oda.call("get_cart", {})
        summary = cart_summary(cart)
        delivery_change = bool(order_change and order_change.get("requested_delivery"))
        if self.provider == "oda" and not delivery_change:
            delivery = summary.get("delivery")
            if not isinstance(delivery, Mapping) or not delivery.get("display"):
                raise HouseholdError("select a delivery slot before checkout")
            address = delivery.get("address")
            if not isinstance(address, str) or not address.strip():
                raise HouseholdError("select a delivery address before checkout")
            summary["delivery"]["address"] = unicodedata.normalize("NFC", " ".join(address.split()))
        before = self.oda.call("get_orders", {"page": 1, "size": 20}, deadline=deadline, allow_recovery=allow_recovery) if self.provider == "meny" else self.oda.call("get_orders", {"page": 1, "size": 20})
        with self._browser_operation(deadline):
            state = self.store.read()
            if (state.get("pending_cancellation") or {}).get("status") in {"clicking", "uncertain"}:
                raise HouseholdError("reconcile the pending cancellation before checkout")
            if delivery_change and self.provider == "oda":
                review = self.browser.review_delivery_change(
                    order_change["order_id"],
                    order_change["before"]["order"],
                    order_change["requested_delivery"],
                    deadline=deadline,
                )
                reviewed_summary = review.get("summary")
                if not isinstance(reviewed_summary, Mapping):
                    raise HouseholdError("Oda delivery change returned no verified summary")
                summary = deepcopy(dict(reviewed_summary))
            elif order_change and self.provider == "oda":
                review = self.browser.review_order_change(
                    cart,
                    order_change["order_id"],
                    order_change["before"]["order"],
                    deadline=deadline,
                )
            else:
                review = self.browser.review_checkout(
                    cart,
                    order_change=order_change if self.provider == "meny" else None,
                    deadline=deadline,
                    allow_recovery=allow_recovery,
                ) if self.provider == "meny" else self.browser.review_checkout(cart, deadline=deadline)
            if self.provider == "meny":
                reviewed_summary = review.get("summary")
                if not isinstance(reviewed_summary, Mapping):
                    raise HouseholdError("MENY checkout returned no verified summary")
                summary = deepcopy(dict(reviewed_summary))
                refreshed_cart = self.oda.call(
                    "get_cart",
                    {},
                    deadline=deadline,
                    allow_recovery=allow_recovery,
                )
                refreshed_summary = cart_summary(refreshed_cart)
                reviewed_lines = sorted(
                    (str(item["product_id"]), int(item["quantity"]))
                    for item in summary["items"]
                )
                refreshed_lines = sorted(
                    (str(item["product_id"]), int(item["quantity"]))
                    for item in refreshed_summary["items"]
                )
                if refreshed_summary["count"] != summary["count"] or refreshed_lines != reviewed_lines:
                    raise HouseholdError("MENY cart items changed after the delivery reservation")
                cart = refreshed_cart
            payment_display = None
            if self.provider == "oda":
                payment_display = str((review.get("summary") or {}).get("payment") or review.get("payment_display") or "")
                if re.fullmatch(r"•••• \d{4}", payment_display) is None:
                    raise HouseholdError("Oda checkout returned no verified masked payment identity")
            confirmation_id = secrets.token_urlsafe(18)
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) != canonical(baseline):
                    raise HouseholdError("checkout state changed while preparing the summary")
                if canonical(state.get("order_change")) != canonical(order_change):
                    raise HouseholdError("order change state changed while preparing the summary")
                state["pending_checkout"] = {
                    "status": "awaiting_confirmation",
                    "confirmation_id": confirmation_id,
                    "cart": cart,
                    "summary": summary,
                    "orders_before": before,
                    "browser_review": review,
                    "expires_at": (now() + timedelta(minutes=20)).isoformat(),
                    "menu": deepcopy(state.get("menu")),
                    "order_change": order_change,
                }
        return {
            "confirmation_id": confirmation_id,
            "confirmation_policy": self.confirmation_policy,
            "confirmation_required": self.confirmation_policy == "fresh",
            "summary": {
                **deepcopy(summary),
                **({"payment": payment_display} if self.provider == "oda" else {}),
            },
            "order_change": {"order_id": order_change["order_id"], "kind": order_change.get("kind")} if order_change else None,
            "next": (
                "Ask once for an explicit final confirmation of this unchanged cart, total, delivery and target order, then pass this confirmation_id unchanged."
                if self.confirmation_policy == "fresh"
                else "Standing authorization is configured. If the current request explicitly asks to order, pay or check out, call checkout confirm now with this confirmation_id; do not ask again."
            ),
        }

    def _checkout_confirm(self, deadline: float | None = None, confirmation_id: str = "") -> dict[str, Any]:
        with self.store.locked() as state:
            pending = deepcopy(state.get("pending_checkout"))
        if not pending or pending["status"] != "awaiting_confirmation":
            raise HouseholdError("no fresh checkout confirmation is pending")
        if confirmation_id != pending.get("confirmation_id"):
            raise HouseholdError("checkout confirmation does not match the prepared summary")
        if now() >= datetime.fromisoformat(pending["expires_at"]):
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) == canonical(pending):
                    state["pending_checkout"] = None
            raise HouseholdError("checkout confirmation expired")
        cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
        pending_change = pending.get("order_change") or {}
        expected_cart = cart_summary(pending["cart"]) if self.provider == "meny" or pending_change.get("requested_delivery") else pending["summary"]
        if canonical(cart_summary(cart)) != canonical(expected_cart):
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) == canonical(pending):
                    state["pending_checkout"] = None
            raise HouseholdError("cart or delivery changed; show a new summary")
        with self.store.locked() as state:
            if canonical(state.get("order_change")) != canonical(pending.get("order_change")):
                raise HouseholdError("order change changed; show a new summary")
        try:
            with self._browser_operation(deadline):
                with self.store.locked() as state:
                    current_pending = state.get("pending_checkout")
                    if not current_pending or current_pending.get("status") != "awaiting_confirmation" or canonical(current_pending) != canonical(pending):
                        raise HouseholdError("no fresh checkout confirmation is pending")
                    if (state.get("pending_cancellation") or {}).get("status") in {"clicking", "uncertain"}:
                        raise HouseholdError("reconcile the pending cancellation before checkout")
                    if canonical(state.get("order_change")) != canonical(pending.get("order_change")):
                        raise HouseholdError("order change changed; show a new summary")
                    state["pending_checkout"]["status"] = "clicking"

                def before_click() -> None:
                    if self.provider == "oda":
                        fresh = self.oda.call("get_cart", {})
                        expected = cart_summary(pending["cart"]) if pending_change.get("requested_delivery") else pending["summary"]
                        if canonical(cart_summary(fresh)) != canonical(expected):
                            raise CheckoutPreconditionError("cart or delivery changed before the final click")
                    if pending_change:
                        with self.store.locked() as state:
                            if canonical(state.get("order_change")) != canonical(pending_change):
                                raise CheckoutPreconditionError("order change changed before the final click")
                        if self.provider == "oda":
                            fresh_target = self._orders({"action": "get", "order_id": pending_change["order_id"], "_deadline": deadline})
                            if canonical(fresh_target) != canonical(pending_change["before"]):
                                raise CheckoutPreconditionError("the target order changed before the final click")
                    with self.store.locked() as state:
                        current_pending = state.get("pending_checkout")
                        expected = {**pending, "status": "clicking"}
                        if not current_pending or canonical(current_pending) != canonical(expected):
                            raise CheckoutPreconditionError("checkout confirmation changed before the final click")
                    if now() >= datetime.fromisoformat(pending["expires_at"]):
                        raise CheckoutPreconditionError("checkout confirmation expired before the final click")

                if pending_change.get("requested_delivery") and self.provider == "oda":
                    change = pending["order_change"]
                    submit_result = self.browser.submit_delivery_change(
                        change["order_id"],
                        change["before"]["order"],
                        change["requested_delivery"],
                        pending["browser_review"],
                        before_click,
                        deadline=deadline,
                    )
                elif pending.get("order_change") and self.provider == "oda":
                    change = pending["order_change"]
                    submit_result = self.browser.submit_order_change(
                        cart,
                        change["order_id"],
                        change["before"]["order"],
                        pending["browser_review"],
                        before_click,
                        deadline=deadline,
                    )
                else:
                    submit_result = self.browser.submit_checkout(
                        cart,
                        pending["browser_review"],
                        before_click,
                        order_change=pending_change or None,
                        deadline=deadline,
                    ) if self.provider == "meny" else self.browser.submit_checkout(
                        cart,
                        pending["browser_review"],
                        before_click,
                        deadline=deadline,
                    )
                if self.provider == "meny" and isinstance(submit_result, Mapping) and submit_result.get("awaiting_user_payment") is True:
                    requested_at = now()
                    with self.store.locked() as state:
                        current_pending = state.get("pending_checkout")
                        if not current_pending or current_pending.get("status") != "clicking" or current_pending.get("browser_review") != pending.get("browser_review"):
                            raise HouseholdError("checkout state changed after the Vipps request")
                        state["pending_checkout"]["status"] = "awaiting_user_payment"
                        state["pending_checkout"]["payment_requested_at"] = requested_at.isoformat()
                        state["pending_checkout"]["payment_expires_at"] = (requested_at + MENY_VIPPS_EXPIRY_BUFFER).isoformat()
                    return {
                        "confirmed": False,
                        "awaiting_user_payment": True,
                        "payment": "vipps",
                        "retry_allowed": False,
                        "next": "Approve this exact MENY payment in Vipps, then call checkout reconcile. Do not submit again.",
                    }
                return self._checkout_reconcile_unlocked(deadline)
        except CheckoutPreconditionError:
            with self.store.locked() as state:
                current_pending = state.get("pending_checkout")
                if current_pending and current_pending.get("status") == "clicking" and current_pending.get("browser_review") == pending.get("browser_review"):
                    state["pending_checkout"] = None
            raise
        except HouseholdError:
            with self.store.locked() as state:
                current_pending = state.get("pending_checkout")
                if current_pending and current_pending.get("status") == "clicking" and current_pending.get("browser_review") == pending.get("browser_review"):
                    state["pending_checkout"]["status"] = "uncertain"
            raise

    def _checkout_reconcile(self, deadline: float | None = None) -> dict[str, Any]:
        with self._browser_operation(deadline):
            return self._checkout_reconcile_unlocked(deadline)

    def _checkout_reconcile_unlocked(self, deadline: float | None = None) -> dict[str, Any]:
        with self.store.locked() as state:
            pending = deepcopy(state.get("pending_checkout"))
        if not pending:
            raise HouseholdError("no checkout attempt is pending")
        if pending.get("status") not in {"clicking", "uncertain", "awaiting_user_payment"}:
            raise HouseholdError("checkout has not reached reconciliation")
        if pending.get("order_change"):
            return self._order_change_reconcile(pending, deadline)
        payment_not_dispatched = (
            self.provider == "meny"
            and pending.get("status") == "uncertain"
            and self.browser.checkout_payment_not_dispatched(pending["browser_review"], deadline=deadline)
        )
        confirmation_order_id = self.browser.checkout_confirmation_order_id(deadline=deadline) if self.provider == "meny" else None
        after = self.oda.call("get_orders", {"page": 1, "size": 20}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_orders", {"page": 1, "size": 20})
        before_ids = {str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "") for item in pending["orders_before"].get("orders", []) if isinstance(item, Mapping)}
        candidates = [item for item in after.get("orders", []) if isinstance(item, Mapping) and str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "") not in before_ids]
        order = None
        tracking = None
        candidate_id = ""
        details_id = ""
        tracking_id = ""
        if self.provider == "meny" and confirmation_order_id:
            candidates = [item for item in candidates if str(item.get("orderNumber") or item.get("order_number") or item.get("id") or "") == confirmation_order_id]
        if len(candidates) == 1:
            candidate_id = str(candidates[0].get("orderNumber") or candidates[0].get("order_number") or candidates[0].get("id") or "")
            details = self.oda.call("get_order", {"order_number": candidate_id}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_order", {"order_number": candidate_id})
            tracking = self.oda.call("order_tracking", {"order_number": candidate_id}, deadline=deadline) if self.provider == "meny" else self.oda.call("order_tracking", {"order_number": candidate_id})
            details_id = str(details.get("orderNumber") or details.get("order_number") or details.get("id") or "")
            tracking_id = str(tracking.get("orderNumber") or tracking.get("order_number") or tracking.get("order_id") or tracking.get("id") or "")
            order = {**candidates[0], **details}
        tracking_status = str((tracking or {}).get("status") or "").casefold()
        if self.provider == "meny":
            confirmed = (
                order is not None
                and confirmation_order_id == candidate_id
                and candidate_id == details_id == tracking_id
                and tracking_status in {"confirmed", "delivered"}
                and meny_order_matches_checkout(order, pending["summary"])
            )
        else:
            fulfillable = {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}
            confirmed = order is not None and candidate_id and candidate_id == details_id == tracking_id and tracking_status in fulfillable and order_matches_checkout(order, pending["summary"])
        expired_unpaid = False
        candidate_matches = order is not None and meny_order_matches_checkout(order, pending["summary"])
        undispatched_retryable = payment_not_dispatched and confirmation_order_id is None and not candidates
        if self.provider == "meny" and pending.get("status") == "awaiting_user_payment" and not confirmed and confirmation_order_id is None and len(candidates) <= 1 and not candidate_matches:
            expiry = pending.get("payment_expires_at") or pending.get("expires_at")
            try:
                expires_at = datetime.fromisoformat(str(expiry or ""))
            except ValueError:
                expires_at = None
            expired_unpaid = expires_at is not None and expires_at.tzinfo is not None and now() >= expires_at
        with self.store.locked() as state:
            if canonical(state.get("pending_checkout")) != canonical(pending):
                raise HouseholdError("checkout state changed while reconciling the order")
            if confirmed:
                order_id = str(order.get("orderNumber") or order.get("order_number") or order.get("id"))
                state["pending_checkout"] = None
                if pending.get("menu"):
                    state["menu"] = pending["menu"]
                    state["menu"]["phase"] = "ordered"
                    state["menu"]["order_id"] = order_id
            elif expired_unpaid or undispatched_retryable:
                state["pending_checkout"] = None
            else:
                state["pending_checkout"]["status"] = "uncertain"
        return {
            "confirmed": confirmed,
            "expired": expired_unpaid,
            "order": order if confirmed else None,
            "tracking": tracking if confirmed else None,
            "retry_allowed": expired_unpaid or undispatched_retryable,
            **({"payment_dispatched": False} if undispatched_retryable else {}),
        }

    def _order_change_reconcile(self, pending: Mapping[str, Any], deadline: float | None = None) -> dict[str, Any]:
        change = pending["order_change"]
        order_id = change["order_id"]
        confirmation_order_id = self.browser.checkout_confirmation_order_id(deadline=deadline) if self.provider == "meny" else order_id
        current = self._orders({"action": "get", "order_id": order_id, "_deadline": deadline})
        current_id = str((current.get("order") or {}).get("orderNumber") or (current.get("order") or {}).get("order_number") or "")
        tracking_id = str((current.get("tracking") or {}).get("order_id") or (current.get("tracking") or {}).get("orderNumber") or "")
        status = str((current.get("tracking") or {}).get("status") or "").casefold()
        if self.provider == "meny":
            matched = confirmation_order_id == order_id and meny_order_matches_checkout(current["order"], pending["summary"])
            fulfillable = status in {"confirmed", "delivered"}
        elif change.get("requested_delivery"):
            expected_delivery = str(change["requested_delivery"].get("display") or "")
            actual_delivery = str(current["order"].get("deliverySlotDisplay") or current["order"].get("deliveryDate") or "")
            matched = oda_delivery_signature(expected_delivery) is not None and oda_delivery_signature(expected_delivery) == oda_delivery_signature(actual_delivery)
            fulfillable = status in {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}
        else:
            matched = oda_order_matches_addition(change["before"]["order"], current["order"], pending["summary"])
            fulfillable = status in {"paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered"}
        confirmed = current_id == tracking_id == order_id and matched and fulfillable
        with self.store.locked() as state:
            if canonical(state.get("pending_checkout")) != canonical(pending):
                raise HouseholdError("checkout state changed while reconciling the order change")
            if confirmed:
                state["pending_checkout"] = None
                state["order_change"] = None
                if pending.get("menu"):
                    state["menu"] = pending["menu"]
                    state["menu"]["phase"] = "ordered"
                    state["menu"]["order_id"] = order_id
            else:
                state["pending_checkout"]["status"] = "uncertain"
        return {"confirmed": confirmed, "changed_existing_order": confirmed, "order": current["order"] if confirmed else None, "tracking": current["tracking"] if confirmed else None, "retry_allowed": False}

    def _email(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "status")
        if action == "status":
            state = self.store.read()
            return {"jobs": [{**job, "recipient": mask_email(state.get("email_recipient"))} for job in state["email_jobs"]]}
        if action == "schedule":
            order_id = str(request.get("order_id") or "")
            delivery_date = str(request.get("delivery_date") or "")
            state = self.store.read()
            if not state.get("menu") or state["menu"].get("order_id") != order_id or not state.get("email_recipient"):
                raise HouseholdError("confirmed order, exact menu and email recipient are required")
            with self.store.locked() as locked:
                existing = [job for job in locked["email_jobs"] if job.get("order_id") == order_id]
                if not existing:
                    locked["email_jobs"].append({"order_id": order_id, "delivery_date": delivery_date, "status": "pending", "sent_at": None})
            return {
                "scheduled": True,
                "delivery_date": delivery_date,
                "recipient": mask_email(state["email_recipient"]),
                "cron_prompt": (
                    f"På {delivery_date}: kall meal_planner_email action=due for ordre {order_id}. "
                    "Hvis send=true, send nøyaktig returnert recipient, subject og HTML én gang med eksisterende "
                    "e-postverktøy. Bare etter vellykket sending: kall meal_planner_email action=mark_sent "
                    f"for samme ordre {order_id}. Ikke marker ved sendefeil."
                ),
            }
        if action == "test":
            order_id = str(request.get("order_id") or "")
            state = self.store.read()
            jobs = [job for job in state["email_jobs"] if job.get("order_id") == order_id and job.get("status") == "pending"]
            menu = state.get("menu")
            recipient = state.get("email_recipient")
            if len(jobs) != 1 or not isinstance(menu, Mapping) or menu.get("order_id") != order_id or not isinstance(recipient, str) or not recipient.strip():
                raise HouseholdError("pending email, exact menu and recipient are required for a test")
            period = menu_email_period(menu)
            result = {
                "send": True,
                "test": True,
                "recipient": recipient,
                "subject": f"TEST – Ukesmeny og oppskrifter – {period}",
                "html": menu_email_html(menu, test=True),
                "order_id": order_id,
                "mark_sent_after_success": False,
                "next": "Send this test once; do not call mark_sent.",
            }
            if self.email_automation_profile:
                result["automation_environment"] = {"HERMES_WORKSPACE_AUTOMATION_PROFILE": self.email_automation_profile}
            return result
        if action == "due":
            order_id = str(request.get("order_id") or "")
            current = self._orders({"action": "get", "order_id": order_id, "_deadline": request.get("_deadline")})
            tracking = str((current.get("tracking") or {}).get("status") or "").casefold()
            with self.store.locked() as state:
                jobs = [job for job in state["email_jobs"] if job.get("order_id") == order_id and job.get("status") == "pending"]
                if not jobs:
                    return {"send": False, "reason": "no pending email"}
                menu = state.get("menu")
                recipient = state.get("email_recipient")
                if len(jobs) != 1 or not isinstance(menu, Mapping) or menu.get("order_id") != order_id or not isinstance(recipient, str) or not recipient.strip():
                    return {"send": False, "reason": "pending email is not bound to one exact menu and recipient"}
                if tracking in {"cancelled", "canceled"}:
                    jobs[0]["status"] = "cancelled"
                    return {"send": False, "reason": "order cancelled"}
                delivery = str((current.get("order") or {}).get("deliveryDate") or (current.get("order") or {}).get("delivery_date") or jobs[0]["delivery_date"])
                if delivery != date.today().isoformat():
                    jobs[0]["delivery_date"] = delivery
                    return {"send": False, "reason": "delivery moved", "delivery_date": delivery}
                period = menu_email_period(menu)
                result = {
                    "send": True,
                    "recipient": recipient,
                    "subject": f"Ukesmeny og oppskrifter – {period}",
                    "html": menu_email_html(menu),
                    "order_id": order_id,
                    "mark_sent_after_success": True,
                    "next": "After this non-test email is sent successfully, call mark_sent for this exact order_id. Do not mark before success.",
                }
                if self.email_automation_profile:
                    result["automation_environment"] = {
                        "HERMES_WORKSPACE_AUTOMATION_PROFILE": self.email_automation_profile,
                    }
                return result
        if action == "mark_sent":
            order_id = str(request.get("order_id") or "")
            with self.store.locked() as state:
                jobs = [job for job in state["email_jobs"] if job.get("order_id") == order_id and job.get("status") == "pending"]
                if len(jobs) != 1:
                    raise HouseholdError("email is not pending")
                jobs[0]["status"] = "sent"
                jobs[0]["sent_at"] = now().isoformat()
            return {"sent": True}
        raise HouseholdError("unknown email action")


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
                request = json.loads(data.split(b"\n", 1)[0])
                if not isinstance(request, dict):
                    raise HouseholdError("request must be an object")
                response = {"ok": True, "result": self.app.handle(request)}
            except (HouseholdError, json.JSONDecodeError) as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                connection.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode())
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
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--state", type=Path, required=True)
    result.add_argument("--tokens", type=Path)
    result.add_argument("--socket", type=Path, default=Path("/tmp/meal-planner.sock"))
    result.add_argument("--socket-group", type=int, default=os.getgid())
    result.add_argument("--agent-uid", type=int, default=os.getuid())
    result.add_argument("--browser-binary", type=Path, default=Path("agent-browser"))
    result.add_argument("--browser-executable", type=Path, default=Path(os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "/usr/bin/chromium")))
    result.add_argument("--browser-profile", type=Path, default=Path.home() / ".meal-planner-browser" / "profile")
    result.add_argument("--browser-home", type=Path, default=Path.home() / ".meal-planner-browser")
    result.add_argument("--browser-socket-directory", type=Path, default=Path("/tmp/meal-planner-browser"))
    result.add_argument("--browser-cdp")
    result.add_argument("--browser-uid", type=int, default=os.getuid())
    result.add_argument("--browser-gid", type=int, default=os.getgid())
    return result


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
    app = Application(
        StateStore(args.state, settings),
        provider_client,
        OdaBrowser(**browser_arguments),
    )
    Server(args.socket, args.socket_group, args.agent_uid, app).run()


if __name__ == "__main__":
    main()
