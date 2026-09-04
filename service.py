#!/usr/bin/env python3
"""One small meal-planning service per household."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
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
    validate_delivery_slot,
    valid_email_address,
)
from oda import (
    OdaClient,
    oda_cart_delivery_matches_slot,
    oda_cart_delivery_window,
    oda_delivery_slot_date,
)
from meny import MENY_CART_TIMEOUT, MENY_ORDER_TIMEOUT, MENY_READ_TIMEOUT, MenyClient, MenyOrderChangeDispatchError, meny_checkout_reviews_match, normalize_product_ref
from recipes import RecipeError, RecipeStore, normalize_recipe, normalize_source_url, recipe_key, scale_recipe, validate_week
from planner import (
    MAX_CANDIDATES, MAX_HISTORY_RECORDS, PLANNER_VERSION, PlannerError, plan_week,
)
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


MAX_REQUEST = 2 * 1024 * 1024
MAX_EMAIL_HTML_BYTES = MAX_REQUEST // 2
MAX_MENU_BYTES = MAX_REQUEST // 2
CANCELLATION_OPERATION_TIMEOUT = 105
MENY_CHECKOUT_OPERATION_TIMEOUT = 600
MENY_VIPPS_EXPIRY_BUFFER = timedelta(minutes=11)
SCHEDULE_OCCURRENCE_LEASE = timedelta(minutes=5)
MAX_EXTERNAL_FAVORITE_SEARCH_PAGES = 10
LIBRARY_SEARCH_CURSOR_PREFIX = "library-search:v1:"
EMAIL_CLAIM_LEASE = timedelta(minutes=5)
EMAIL_AUTOMATION_PROTOCOL = 4
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


def strict_json_loads(value: str | bytes) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {constant}")

    def parse_float(number: str) -> float:
        parsed = float(number)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number is not allowed")
        return parsed

    return json.loads(value, parse_constant=reject_constant, parse_float=parse_float)


def validate_request_value(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise HouseholdError("request nesting is too deep")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                raise HouseholdError("request contains invalid Unicode")
            validate_request_value(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_request_value(child, depth + 1)
    elif isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise HouseholdError("request contains invalid Unicode")
    elif isinstance(value, float) and not math.isfinite(value):
        raise HouseholdError("request numbers must be finite")
    elif not isinstance(value, (int, float, bool, type(None))):
        raise HouseholdError("request contains an unsupported value")


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
    try:
        return validate_week(menu.get("week"))
    except RecipeError as exc:
        raise HouseholdError("email menu needs a valid ISO week") from exc


def safe_order_id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) is None:
        raise HouseholdError("order_id must be a bounded safe provider identifier")
    return value


def bounded_limit(value: Any, *, default: int, maximum: int = 100) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise HouseholdError(f"limit must be an integer from one to {maximum}")
    return value


def require_provider_identity(value: Any, expected_order_id: str, *, tracking: bool = False) -> None:
    if not isinstance(value, Mapping):
        raise HouseholdError("provider returned no exact order identity")
    primary = ("order_id", "orderNumber", "order_number") if tracking else ("orderNumber", "order_number")
    candidates = [str(value.get(key)) for key in primary if value.get(key) is not None and value.get(key) != ""]
    if not candidates and value.get("id") is not None and value.get("id") != "":
        candidates = [str(value["id"])]
    if not candidates or any(candidate != expected_order_id for candidate in candidates):
        raise HouseholdError("provider response does not match the requested order")
    for candidate in candidates:
        safe_order_id(candidate)


def email_job_provider(value: Mapping[str, Any]) -> str | None:
    provider = value.get("provider")
    return provider if isinstance(provider, str) and provider in {"oda", "meny"} else None


def email_automation_key(provider: str, order_id: str) -> str:
    if provider not in {"oda", "meny"}:
        raise HouseholdError("email provider is invalid")
    order_id = safe_order_id(order_id)
    return f"meal-concierge-email-{hashlib.sha256(f'{provider}:{order_id}'.encode()).hexdigest()[:16]}"


def email_automation_prompt(provider: str, order_id: str, delivery_date: str, automation_key: str) -> str:
    if provider not in {"oda", "meny"}:
        raise HouseholdError("email provider is invalid")
    order_id = safe_order_id(order_id)
    try:
        if date.fromisoformat(delivery_date).isoformat() != delivery_date:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise HouseholdError("delivery_date must be a canonical ISO date") from exc
    if re.fullmatch(r"meal-concierge-email-[a-f0-9]{16}", automation_key) is None:
        raise HouseholdError("email automation key is invalid")
    return (
        f"Opprett eller oppdater den ene automatiseringen {automation_key}. På {delivery_date}: "
        f"kall meal_concierge_email action=due provider={provider} for ordre {order_id}. "
        f"Hvis claim=true, kall begin_send provider={provider} med returnert claim_token rett før senderen. Bare hvis dispatch=true, "
        "send nøyaktig returnert recipient, subject og HTML én gang. Etter vellykket sending: kall mark_sent "
        f"provider={provider} for samme ordre {order_id} med returnert claim_token. Ved en uttrykkelig definitiv sendefeil "
        f"kan samme token frigis med action=release provider={provider}; ikke frigi ved timeout eller usikkert resultat."
    )


def email_automation_ack(provider: str, order_id: str, delivery_date: str, automation_key: str) -> dict[str, Any]:
    prompt = email_automation_prompt(provider, order_id, delivery_date, automation_key)
    return {
        "action": "ack_automation", "order_id": order_id, "delivery_date": delivery_date,
        "provider": provider,
        "automation_key": automation_key, "automation_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "protocol": EMAIL_AUTOMATION_PROTOCOL,
    }


def menu_digest(menu: Mapping[str, Any]) -> str:
    value = deepcopy(dict(menu))
    for key in ("menu_id", "revision", "digest", "phase", "order_id"):
        value.pop(key, None)
    import hashlib
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def menu_email_html(menu: Mapping[str, Any], *, test: bool = False) -> str:
    escape = lambda value: html.escape(str(value or ""))

    def ingredients(values: Any) -> str:
        parts = []
        for value in values if isinstance(values, list) else []:
            if isinstance(value, Mapping):
                amount = str(value.get("amount") or "").strip()
                item = str(value.get("item") or value.get("name") or "").strip()
                raw = str(value.get("raw") or "").strip()
                text = " ".join(part for part in (amount, item) if part) if amount else raw or item
            else:
                text = str(value).strip()
            if text:
                parts.append(f"<li>{escape(text)}</li>")
        return "".join(parts)

    def steps(values: Any) -> str:
        if not isinstance(values, list):
            return ""
        return "".join(f"<li>{escape(value)}</li>" for value in values if str(value).strip())

    def source(value: Mapping[str, Any]) -> str:
        metadata = value.get("source") if isinstance(value.get("source"), Mapping) else {}
        publisher = str(metadata.get("publisher") or metadata.get("kind") or "Kilde ikke registrert").strip()
        source_title = str(metadata.get("title") or value.get("name") or "").strip()
        relationship = str(metadata.get("relationship") or "unknown").casefold()
        labels = {
            "adapted": "Tilpasset",
            "inspired_by": "Inspirert av",
            "generated": "Generert av Hermes",
            "user_supplied": "Familiens egen oppskrift",
            "original": "Kilde",
            "unknown": "Kilde",
        }
        label = labels.get(relationship, "Kilde")
        try:
            url = normalize_source_url(metadata.get("url"))
        except RecipeError:
            url = None
        text = " – ".join(part for part in (publisher, source_title if source_title.casefold() != publisher.casefold() else "") if part)
        if relationship == "generated":
            text = "Hermes for denne ukemenyen"
        elif relationship == "user_supplied" and publisher.casefold() in {"unknown", "user", "bruker"}:
            text = "Familiens egen oppskrift"
        rendered = f'<a href="{escape(url)}">{escape(text)}</a>' if url else escape(text)
        rights = value.get("rights") if isinstance(value.get("rights"), Mapping) else {}
        snapshot = value.get("external_snapshot") if isinstance(value.get("external_snapshot"), Mapping) else {}
        credit = rights.get("credit")
        license_name = str(rights.get("license") or "").strip()
        try:
            license_url = normalize_source_url(rights.get("license_url"))
        except RecipeError:
            license_url = None
        try:
            permanent_url = normalize_source_url(snapshot.get("permanent_url"))
        except RecipeError:
            permanent_url = None
        details = []
        if credit:
            details.append(escape(credit))
        if license_name:
            details.append(
                f'<a href="{escape(license_url)}">{escape(license_name)}</a>'
                if license_url else escape(license_name)
            )
        if permanent_url:
            details.append(f'<a href="{escape(permanent_url)}">Frosset kilderevisjon</a>')
        if snapshot.get("changes"):
            details.append(f"Endringer: {escape(snapshot['changes'])}")
        suffix = f". {' · '.join(details)}" if details else ""
        return f"<p><strong>{escape(label)}:</strong> {rendered}{suffix}</p>"

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
                source(recipe),
                f"<p>{escape(recipe.get('portions'))} porsjoner</p>" if recipe.get("portions") else "",
                "<h3>Ingredienser</h3><ul>" if (recipe.get("rights") or {}).get("storage") != "link_only" else "",
                ingredients(recipe.get("ingredients")) if (recipe.get("rights") or {}).get("storage") != "link_only" else "",
                "</ul><h3>Fremgangsmåte</h3><ol>" if (recipe.get("rights") or {}).get("storage") != "link_only" else "",
                steps(recipe.get("steps")) if (recipe.get("rights") or {}).get("storage") != "link_only" else "",
                "</ol>" if (recipe.get("rights") or {}).get("storage") != "link_only" else "",
                f"<p>{escape(recipe.get('notes'))}</p>" if recipe.get("notes") else "",
                f"<p><strong>Lagring:</strong> {escape(recipe.get('storage'))}</p>" if recipe.get("storage") else "",
                f"<p><strong>Oppvarming:</strong> {escape(recipe.get('reheating'))}</p>" if recipe.get("reheating") else "",
                "</section>",
            ])
    parts.append("</body></html>")
    return "".join(parts)


def delivery_matches(
    preference: Mapping[str, Any],
    delivery: Mapping[str, Any] | None,
    *,
    timezone_name: str = "Europe/Oslo",
) -> bool:
    if not delivery:
        return False
    candidate = delivery.get("slot") if isinstance(delivery.get("slot"), Mapping) else delivery
    try:
        slot = validate_delivery_slot(candidate)
        zone = ZoneInfo(timezone_name)
        start_at = datetime.fromisoformat(slot["start_at"].replace("Z", "+00:00")).astimezone(zone)
        end_at = datetime.fromisoformat(slot["end_at"].replace("Z", "+00:00")).astimezone(zone)
    except (HouseholdError, ValueError, ZoneInfoNotFoundError):
        return False
    weekday = str(preference.get("weekday") or "").casefold()
    if weekday and start_at.weekday() != SCHEDULE_WEEKDAYS.get(weekday):
        return False
    latest = str(preference.get("latest_end") or "")
    if latest and end_at.strftime("%H:%M") > latest:
        return False
    return True


def validate_schedule(schedule: Mapping[str, Any], provider: str) -> float | None:
    if not isinstance(schedule.get("enabled"), bool) or not isinstance(schedule.get("auto_checkout"), bool):
        raise HouseholdError("schedule enabled and auto_checkout must be true or false")
    if str(schedule.get("mode") or "") not in {"draft", "cart_ready", "auto_checkout"}:
        raise HouseholdError("schedule mode is invalid")
    weekday = str(schedule.get("weekday") or "").casefold()
    if weekday not in SCHEDULE_WEEKDAYS:
        raise HouseholdError("schedule weekday is invalid")
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(schedule.get("time") or "")) is None:
        raise HouseholdError("schedule time is invalid")
    try:
        ZoneInfo(str(schedule.get("timezone") or ""))
    except ZoneInfoNotFoundError as exc:
        raise HouseholdError("schedule timezone is invalid") from exc
    maximum = schedule.get("maximum_total")
    maximum_value = None
    if maximum is not None:
        try:
            maximum_value = float(maximum)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HouseholdError("schedule maximum total must be a positive finite number") from exc
        if isinstance(maximum, bool) or not isinstance(maximum, (int, float)) or not math.isfinite(maximum_value) or maximum_value <= 0:
            raise HouseholdError("schedule maximum total must be a positive finite number")
    delivery = schedule.get("delivery")
    if not isinstance(delivery, Mapping) or not set(delivery).issubset({"weekday", "preferred_end", "latest_end", "strategy"}):
        raise HouseholdError("schedule delivery preference is invalid")
    if delivery.get("strategy") not in {"keep_selected", "cheapest"}:
        raise HouseholdError("schedule delivery strategy is invalid")
    delivery_weekday = str(delivery.get("weekday") or "").casefold()
    if delivery_weekday and delivery_weekday not in SCHEDULE_WEEKDAYS:
        raise HouseholdError("schedule delivery weekday is invalid")
    for key in ("preferred_end", "latest_end"):
        if delivery.get(key) is not None and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(delivery[key])) is None:
            raise HouseholdError(f"schedule delivery {key} is invalid")
    if schedule.get("auto_checkout"):
        if provider != "oda":
            raise HouseholdError("MENY supports cart_ready scheduling; checkout continues manually in the browser")
        if maximum is None or not (delivery_weekday or delivery.get("latest_end")):
            raise HouseholdError("auto-checkout requires maximum total and a delivery weekday or latest end")
    return maximum_value


def money_cents(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    scaled = number * 100
    if not math.isfinite(scaled):
        return None
    return int(round(scaled))


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
    expected_address = summary_delivery.get("address") if isinstance(summary_delivery, Mapping) else None
    observed_delivery = order.get("deliverySlotDisplay")
    observed_date = order.get("deliveryDate")
    observed_address = order.get("deliveryAddress", order.get("delivery_address"))
    if isinstance(observed_address, Mapping):
        observed_address = observed_address.get("address") or observed_address.get("display")
    if not isinstance(expected_delivery, str) or not isinstance(observed_delivery, str):
        return False
    if not isinstance(expected_address, str) or not expected_address.strip() or not isinstance(observed_address, str) or not observed_address.strip():
        return False
    if observed_date is not None and not isinstance(observed_date, str):
        return False
    expected_signature = delivery_signature(expected_delivery)
    observed_signature = delivery_signature(observed_delivery, observed_date or "")
    observed_total = money_cents(observed.get("total"))
    expected_total = money_cents(summary.get("total"))
    return (
        items(observed) is not None
        and items(observed) == items(summary)
        and observed_total is not None
        and observed_total == expected_total
        and expected_signature is not None
        and observed_signature == expected_signature
        and re.sub(r"\s+", " ", unicodedata.normalize("NFC", observed_address)).strip().casefold()
        == re.sub(r"\s+", " ", unicodedata.normalize("NFC", expected_address)).strip().casefold()
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


def oda_order_quantities(order: Mapping[str, Any]) -> dict[str, int] | None:
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


def oda_order_delivery_identity(order: Mapping[str, Any]) -> tuple[tuple[str, Any], ...] | None:
    identity: list[tuple[str, Any]] = []
    for key in ("deliveryDate", "delivery_date"):
        if key in order:
            value = order.get(key)
            if not isinstance(value, str):
                return None
            try:
                canonical_date = date.fromisoformat(value).isoformat()
            except ValueError:
                return None
            if canonical_date != value:
                return None
            identity.append(("date", canonical_date))
            break
    for key in ("deliverySlotDisplay", "delivery_slot_display"):
        if key in order:
            signature = oda_delivery_signature(str(order.get(key) or ""))
            if signature is None:
                return None
            identity.append(("slot", signature))
            break
    for key in ("deliveryAddressId", "delivery_address_id", "addressId", "address_id"):
        if key in order:
            value = order.get(key)
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                return None
            identity.append(("address", str(value)))
            break
    kinds = {kind for kind, _value in identity}
    return tuple(identity) if {"date", "slot"}.issubset(kinds) else None


def oda_order_address_identity(order: Mapping[str, Any]) -> str | None:
    for key in ("deliveryAddressId", "delivery_address_id", "addressId", "address_id"):
        if key not in order:
            continue
        value = order.get(key)
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            return None
        normalized = str(value).strip()
        return normalized or None
    return None


def oda_order_matches_addition(before: Mapping[str, Any], after: Mapping[str, Any], additions: Mapping[str, Any]) -> bool:

    expected = oda_order_quantities(before)
    observed = oda_order_quantities(after)
    if expected is None or observed is None:
        return False
    before_delivery = oda_order_delivery_identity(before)
    after_delivery = oda_order_delivery_identity(after)
    if before_delivery is None or before_delivery != after_delivery:
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


def checkout_intent_signature(summary: Mapping[str, Any]) -> str | None:
    items = summary.get("items")
    if not isinstance(items, list):
        return None
    lines = []
    for item in items:
        if not isinstance(item, Mapping):
            return None
        product_id = str(item.get("product_id") or "")
        quantity = item.get("quantity")
        if not product_id or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            return None
        lines.append((product_id, quantity))
    total = money_cents(summary.get("total"))
    if total is None:
        return None
    delivery = summary.get("delivery") if isinstance(summary.get("delivery"), Mapping) else {}
    identity = {
        "items": sorted(lines), "total_cents": total,
        "delivery": str(delivery.get("display") or ""), "address": str(delivery.get("address") or ""),
    }
    return hashlib.sha256(canonical(identity).encode()).hexdigest()


class Application:
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
            if operation == "recipes" or (
                operation == "menu" and (
                    action == "plan"
                    or (action == "save" and request.get("planner_handoff") is not None)
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
            state = self.store.read()
            if self.provider == "meny" and self.integration.get("status") != "ready":
                deadline = time.monotonic() + MENY_READ_TIMEOUT
                with self._browser_operation(deadline):
                    pending_status = (state.get("pending_checkout") or {}).get("status")
                    if pending_status not in UNRESOLVED_CHECKOUT_STATUSES:
                        safe = not state.get("pending_checkout") and not state.get("pending_cancellation") and not state.get("order_change")
                        self._refresh_integration(deadline, allow_recovery=safe)
            return {
                **masked_status(self.store.read(), self.integration),
                "confirmation_policy": self.confirmation_policy,
            }
        if operation == "setup":
            return self._setup(request)
        if operation == "profile":
            return self._profile(request)
        if operation == "product_favorites":
            return self._items(request, "product_favorites")
        if operation == "recurring":
            return self._recurring(request)
        if operation == "recipes":
            return self._recipes(request)
        if operation == "menu":
            return self._menu(request)
        if operation == "schedule":
            return self._schedule(request)
        action = request.get("action")
        email_due_requires_meny = (
            operation == "email" and action == "due"
            and (request.get("provider") is None or request.get("provider") == "meny")
        )
        meny_read = self.provider == "meny" and (
            operation == "catalog"
            or (operation == "cart" and action in {None, "get"})
            or (operation == "delivery" and action in {None, "list"})
            or (operation == "orders" and action in {None, "list", "get"})
            or email_due_requires_meny
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

    @staticmethod
    def _week_index(week: str) -> int:
        match = re.fullmatch(r"(\d{4})-W(\d{2})", validate_week(week))
        return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1).toordinal() // 7

    @staticmethod
    def _matching_recipe_key(key: str, values: Any) -> str | None:
        if not isinstance(values, list):
            return None
        aliases = library_recipe_key_aliases(key)
        return next(
            (
                value for value in values
                if isinstance(value, str) and aliases.intersection(library_recipe_key_aliases(value))
            ),
            None,
        )

    @staticmethod
    def _canonical_usage_key(key: str) -> str:
        aliases = library_recipe_key_aliases(key)
        return next((value for value in aliases if value.startswith("library:builtin:")), key)

    def _usage_summary(
        self,
        state: Mapping[str, Any],
        key: str,
        week: str,
        *,
        ignore_menu_id: str | None = None,
    ) -> dict[str, Any]:
        cooldown = (state.get("profile") or {}).get("recipes", {}).get("repeat_cooldown_weeks", 6)
        if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0 or cooldown > 260:
            raise HouseholdError("repeat cooldown must be an integer from zero to 260 weeks")
        target = self._week_index(week)
        identity_keys = library_recipe_key_aliases(key)
        last_planned = last_ordered = last_cooked = None
        blockers = []
        for menu_id, record in (state.get("recipe_usage") or {}).items():
            if menu_id == ignore_menu_id or not isinstance(record, Mapping) or not identity_keys.intersection(record.get("recipe_keys", [])):
                continue
            record_week = record.get("week")
            try:
                index = self._week_index(str(record_week))
            except (HouseholdError, RecipeError):
                continue
            status = record.get("status")
            previous = record.get("previous_status")
            if status in {"planned", "ordered"}:
                last_planned = max(filter(None, (last_planned, record_week)), default=record_week)
            if status == "ordered" or previous == "ordered":
                last_ordered = max(filter(None, (last_ordered, record_week)), default=record_week)
            cooked = bool(identity_keys.intersection(record.get("cooked_keys", [])))
            not_cooked = bool(identity_keys.intersection(record.get("not_cooked_keys", [])))
            if cooked:
                last_cooked = max(filter(None, (last_cooked, record_week)), default=record_week)
            active = (status in {"planned", "ordered"} and not not_cooked) or cooked
            distance = target - index
            if active and cooldown and -cooldown < distance < cooldown:
                match = re.fullmatch(r"(\d{4})-W(\d{2})", str(record_week))
                try:
                    eligible_date = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1) + timedelta(weeks=cooldown)
                except OverflowError:
                    next_eligible_week = None
                else:
                    eligible_iso = eligible_date.isocalendar()
                    next_eligible_week = f"{eligible_iso.year}-W{eligible_iso.week:02d}"
                blockers.append({
                    "menu_id": menu_id, "week": record_week, "status": "cooked" if cooked else status,
                    "next_eligible_week": next_eligible_week,
                })
        next_eligible = max((item["next_eligible_week"] for item in blockers if item["next_eligible_week"]), default=None)
        return {
            "last_planned_week": last_planned,
            "last_ordered_week": last_ordered,
            "last_cooked_week": last_cooked,
            "next_eligible_week": next_eligible,
            "eligible": not blockers,
            "blocked_by": blockers,
            "cooldown_weeks": cooldown,
        }

    @staticmethod
    def _usage_request(state: dict[str, Any], key: str, digest: str) -> dict[str, Any] | None:
        requests = state.setdefault("recipe_usage_requests", {})
        existing = requests.get(key)
        if existing and existing.get("digest") != digest:
            raise HouseholdError("usage idempotency key was already used with different content")
        return deepcopy(existing.get("result")) if existing else None

    @staticmethod
    def _store_usage_request(state: dict[str, Any], key: str, digest: str, result: Mapping[str, Any]) -> None:
        requests = state.setdefault("recipe_usage_requests", {})
        requests[key] = {"digest": digest, "result": deepcopy(dict(result)), "at": now().isoformat()}
        while len(requests) > 200:
            requests.pop(next(iter(requests)))

    def _internal_recipe_candidates(
        self, query: str, limit: int, state: Mapping[str, Any], week: str,
    ) -> list[dict[str, Any]]:
        results = []
        for row in self.recipes.search(query, limit=limit, include_archived=False):
            recipe = self.recipes.get(row["id"], row["revision"])
            for field in ("library_id", "is_favorite", "favorite_revision"):
                recipe.pop(field, None)
            recipe["usage"] = self._usage_summary(state, recipe["recipe_key"], week)
            if recipe["usage"]["eligible"]:
                results.append(recipe)
        return results

    def _provider_recipe_candidates(self, provider: str, query: str, limit: int) -> list[dict[str, Any]]:
        if not query:
            return []
        client = self.oda if provider == self.provider else self.email_provider_clients.get(provider)
        if client is None:
            raise HouseholdError(f"{provider.upper()} recipe source has no configured provider session")
        arguments = {"query": query, "page": 1, "size": limit}
        if provider == "meny":
            response = client.call(
                "recipe_search", arguments,
                deadline=time.monotonic() + 10,
                allow_recovery=True,
            )
        else:
            response = client.call("recipe_search", arguments, deadline=time.monotonic() + 10)
        return provider_recipe_candidates(provider, response, limit)

    @staticmethod
    def _discovery_identities(recipe: Mapping[str, Any]) -> set[str]:
        source = recipe.get("source") if isinstance(recipe.get("source"), Mapping) else {}
        publisher = " ".join(str(source.get("publisher") or source.get("kind") or "").casefold().split())
        external_id = " ".join(str(source.get("external_id") or "").casefold().split())
        identities = set()
        if publisher and external_id:
            identities.add(f"source:{publisher}:{external_id}")
        url = str(source.get("url") or "")
        if url:
            identities.add(f"url:{url}")
        ingredients = recipe.get("ingredients") if isinstance(recipe.get("ingredients"), list) else []
        name = " ".join(str(recipe.get("name") or "").casefold().split())
        normalized_ingredients = [
                " ".join(str(item.get("item") if isinstance(item, Mapping) else item).casefold().split())
                for item in ingredients
        ]
        if name and normalized_ingredients:
            exact = {"name": name, "ingredients": normalized_ingredients}
            identities.add("content:" + hashlib.sha256(canonical(exact).encode()).hexdigest())
        return identities or {"fallback:" + hashlib.sha256(canonical(recipe).encode()).hexdigest()}

    def _discover_recipes(self, request: Mapping[str, Any]) -> dict[str, Any]:
        gate = self._setup_gate(request)
        if gate is not None:
            return gate
        query_value = request.get("query", "")
        if not isinstance(query_value, str):
            raise HouseholdError("recipe discovery query must be text")
        query = " ".join(query_value.split())
        if len(query) > 200:
            raise HouseholdError("recipe discovery query is too long")
        total_limit = bounded_limit(request.get("limit"), default=10)
        state = self.store.read()
        week = validate_week(request.get("week") or self._household_today(state).strftime("%G-W%V"))
        sources = validate_source_settings(state["profile"]["recipes"]["sources"])
        enabled = [source for source in SOURCE_IDS if sources[source]]
        if not enabled:
            raise HouseholdError("no recipe sources are enabled")
        per_source = min(5, max(1, math.ceil(total_limit / len(enabled))))
        busy = bool(state.get("pending_checkout") or state.get("pending_cancellation") or state.get("order_change"))
        tasks: dict[str, Any] = {}
        for source in enabled:
            if source == "internal":
                tasks[source] = lambda q=query, n=per_source: self._internal_recipe_candidates(q, n, state, week)
            elif source in {"themealdb", "wikibooks"}:
                adapter = self.external_recipe_sources.get(source)
                if adapter is not None:
                    tasks[source] = lambda adapter=adapter, q=query, n=per_source: adapter.search(q, n)
            elif not busy:
                tasks[source] = lambda source=source, q=query, n=per_source: self._provider_recipe_candidates(source, q, n)
        executor = ThreadPoolExecutor(max_workers=min(5, max(1, len(tasks))))
        futures = {executor.submit(call): source for source, call in tasks.items()}
        done, pending = wait(futures, timeout=12)
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        by_source: dict[str, list[dict[str, Any]]] = {source: [] for source in SOURCE_IDS}
        statuses = []
        for source in SOURCE_IDS:
            if not sources[source]:
                statuses.append({"source": source, "enabled": False, "status": "disabled", "count": 0})
                continue
            future = next((candidate for candidate, name in futures.items() if name == source), None)
            if future is None:
                reason = "provider operation is pending" if source in {"oda", "meny"} and busy else "source session is unavailable"
                statuses.append({"source": source, "enabled": True, "status": "unavailable", "count": 0, "reason": reason})
                continue
            if future not in done:
                statuses.append({"source": source, "enabled": True, "status": "timeout", "count": 0})
                continue
            try:
                values = future.result()
                if not isinstance(values, list):
                    raise HouseholdError("recipe source result is invalid")
                by_source[source] = [deepcopy(value) for value in values[:per_source] if isinstance(value, Mapping)]
                status = "ready" if by_source[source] else "empty"
                statuses.append({"source": source, "enabled": True, "status": status, "count": len(by_source[source])})
            except (HouseholdError, OSError, ValueError, TypeError, RecursionError):
                statuses.append({"source": source, "enabled": True, "status": "unavailable", "count": 0})
        results = []
        seen = set()
        for index in range(per_source):
            for source in SOURCE_IDS:
                values = by_source[source]
                if index >= len(values):
                    continue
                recipe = values[index]
                identities = self._discovery_identities(recipe)
                if identities & seen:
                    continue
                seen.update(identities)
                if source == "internal":
                    recipe["discovery_source"] = source
                    recipe["recipe_ref"] = {"id": recipe["id"], "revision": recipe["revision"]}
                    recipe["already_saved"] = "builtin"
                else:
                    persisted = self.recipes.persist_discovery(recipe)
                    recipe = persisted.pop("recipe")
                    recipe["discovery_source"] = source
                    recipe.update(persisted)
                results.append(recipe)
                if len(results) == total_limit:
                    break
            if len(results) == total_limit:
                break
        if not results:
            raise HouseholdError("no enabled recipe source returned a usable candidate")
        return {
            "week": week,
            "query": query,
            "recipes": results,
            "sources": statuses,
            "balanced_limit_per_source": per_source,
        }

    def _library_capabilities(self, library_id: str) -> dict[str, Any]:
        connection = self.recipe_libraries[library_id]
        if library_id == "builtin":
            return {
                "provider": "builtin",
                "server_version": "4",
                "read_only": False,
                **{
                    name: name in {
                        "search", "get", "create_from_discovery", "favorite_read",
                        "favorite_write_desired_state", "favorite_conditional_write",
                    }
                    for name in CAPABILITY_NAMES
                },
            }
        adapter = self.recipe_library_adapters.get(library_id)
        if adapter is None:
            raise RecipeLibraryError("optional recipe library adapter is not installed")
        try:
            return verified_capabilities(adapter, connection)
        except RecipeLibraryError:
            raise
        except Exception as exc:
            raise RecipeLibraryError("recipe library capability probe is unavailable") from exc

    @staticmethod
    def _library_needs_auth(exc: Exception) -> bool:
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, RecipeLibraryError) and str(current).endswith(
                "needs_auth"
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _provider_text(value: Any, field: str, maximum: int, *, required: bool = False) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise RecipeLibraryError(f"{field} must be text")
        result = unicodedata.normalize("NFC", value).strip()
        if required and not result:
            raise RecipeLibraryError(f"{field} is required")
        if len(result) > maximum or any(0xD800 <= ord(character) <= 0xDFFF for character in result):
            raise RecipeLibraryError(f"{field} is invalid")
        return result or None

    @staticmethod
    def _external_favorite_revision(value: Any) -> int | str:
        if isinstance(value, bool):
            raise RecipeLibraryError("provider favorite revision is invalid")
        if isinstance(value, int):
            if value < 0:
                raise RecipeLibraryError("provider favorite revision is invalid")
            return value
        if isinstance(value, str):
            checked = Application._provider_text(
                value, "provider favorite revision", 300, required=True
            )
            if checked != value:
                raise RecipeLibraryError("provider favorite revision must be exact text")
            return checked
        raise RecipeLibraryError("provider favorite revision is invalid")

    @classmethod
    def _decode_library_search_cursor(
        cls, value: str | None, library_id: str, requested_limit: int
    ) -> tuple[str | None, int, int]:
        if value is None:
            return None, requested_limit, 0
        if not value.startswith(LIBRARY_SEARCH_CURSOR_PREFIX):
            return (
                cls._provider_text(
                    value, "recipe library provider cursor", 500, required=True
                ),
                requested_limit,
                0,
            )
        encoded = value.removeprefix(LIBRARY_SEARCH_CURSOR_PREFIX)
        try:
            padding = "=" * (-len(encoded) % 4)
            decoded = base64.b64decode(
                encoded + padding, altchars=b"-_", validate=True
            )
            payload = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise RecipeLibraryError("recipe library cursor is invalid") from exc
        if not isinstance(payload, Mapping) or set(payload) != {
            "v", "l", "c", "n", "s"
        }:
            raise RecipeLibraryError("recipe library cursor is invalid")
        provider_limit = payload["n"]
        skip = payload["s"]
        if (
            payload["v"] != 1
            or payload["l"] != library_id
            or isinstance(provider_limit, bool)
            or not isinstance(provider_limit, int)
            or not 1 <= provider_limit <= 50
            or isinstance(skip, bool)
            or not isinstance(skip, int)
            or not 0 <= skip <= provider_limit
        ):
            raise RecipeLibraryError("recipe library cursor is invalid")
        provider_cursor = payload["c"]
        if provider_cursor is not None:
            provider_cursor = cls._provider_text(
                provider_cursor,
                "recipe library provider cursor",
                500,
                required=True,
            )
        if provider_cursor is None and skip == 0:
            raise RecipeLibraryError("recipe library cursor is invalid")
        return provider_cursor, provider_limit, skip

    @staticmethod
    def _encode_library_search_cursor(
        library_id: str,
        provider_cursor: str | None,
        provider_limit: int,
        skip: int = 0,
    ) -> str | None:
        if provider_cursor is None and skip == 0:
            return None
        payload = json.dumps(
            {
                "v": 1,
                "l": library_id,
                "c": provider_cursor,
                "n": provider_limit,
                "s": skip,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        result = f"{LIBRARY_SEARCH_CURSOR_PREFIX}{encoded}"
        if len(result) > 1_024:
            raise RecipeLibraryError("recipe library cursor is too large")
        return result

    def _normalize_library_search_item(
        self, value: Any, library_id: str, *, favorite_read: bool = False
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RecipeLibraryError("recipe library search returned an invalid item")
        reference = validate_library_recipe_ref(value.get("library_recipe_ref"))
        if reference["library_id"] != library_id:
            raise RecipeLibraryError("recipe library search returned the wrong library identity")
        result: dict[str, Any] = {
            "name": self._provider_text(value.get("name"), "recipe library result name", 300, required=True),
            "library_id": reference["library_id"],
            "library_recipe_ref": reference,
            "recipe_key": library_recipe_key(reference),
        }
        if value.get("provider_slug") is not None:
            result["provider_slug"] = self._provider_text(
                value.get("provider_slug"), "recipe library result provider_slug", 300,
                required=True,
            )
        tags = value.get("tags")
        if tags is not None:
            if not isinstance(tags, list) or len(tags) > 50:
                raise RecipeLibraryError("recipe library result tags are invalid")
            result["tags"] = [
                self._provider_text(tag, "recipe library result tag", 80, required=True)
                for tag in tags
            ]
        source = value.get("source")
        if source is not None:
            if not isinstance(source, Mapping):
                raise RecipeLibraryError("recipe library result source is invalid")
            normalized_source = {
                key: self._provider_text(source.get(key), f"recipe library result source.{key}", maximum)
                for key, maximum in (
                    ("kind", 40), ("publisher", 200), ("title", 300),
                    ("author", 200), ("relationship", 40),
                )
                if source.get(key) is not None
            }
            if source.get("url") is not None:
                normalized_source["url"] = normalize_source_url(source.get("url"))
            result["source"] = normalized_source
        if favorite_read:
            if not isinstance(value.get("is_favorite"), bool):
                raise RecipeLibraryError(
                    "recipe library result favorite state is invalid"
                )
            result["is_favorite"] = value["is_favorite"]
            if value.get("favorite_revision") is not None:
                result["favorite_revision"] = self._external_favorite_revision(
                    value["favorite_revision"]
                )
        return result

    def _normalize_external_favorite(
        self, value: Any, expected_reference: Mapping[str, str]
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or not isinstance(
            value.get("is_favorite"), bool
        ):
            raise RecipeLibraryError("recipe library favorite read returned invalid data")
        reference = validate_library_recipe_ref(value.get("library_recipe_ref"))
        if (
            reference["library_id"] != expected_reference["library_id"]
            or reference["recipe_id"] != expected_reference["recipe_id"]
            or value.get("library_id") != expected_reference["library_id"]
        ):
            raise RecipeLibraryError(
                "recipe library favorite read returned the wrong identity"
            )
        result = {
            "library_id": reference["library_id"],
            "library_recipe_ref": reference,
            "is_favorite": value["is_favorite"],
        }
        if value.get("favorite_revision") is not None:
            result["favorite_revision"] = self._external_favorite_revision(
                value["favorite_revision"]
            )
        return result

    def _recipe_favorite_lock(
        self, reference: Mapping[str, str]
    ) -> threading.Lock:
        key = (reference["library_id"], reference["recipe_id"])
        with self.recipe_favorite_locks_guard:
            return self.recipe_favorite_locks.setdefault(key, threading.Lock())

    def _recipe_lifecycle_lock(
        self, reference: Mapping[str, str]
    ) -> threading.Lock:
        key = (reference["library_id"], reference["recipe_id"])
        with self.recipe_lifecycle_locks_guard:
            return self.recipe_lifecycle_locks.setdefault(key, threading.Lock())

    def _recover_recipe_library_operations(self) -> None:
        if not self._recipe_operations_recovered:
            self.recipes.recover_library_operations()
            self._recipe_operations_recovered = True

    @staticmethod
    def _recipe_lifecycle_digest(recipe: Mapping[str, Any]) -> str:
        return hashlib.sha256(canonical(normalize_recipe(recipe)).encode()).hexdigest()

    def _recipe_library_context(
        self, library_id: str, adapter: RecipeLibraryAdapter
    ) -> tuple[str, str]:
        try:
            value = adapter.authenticated_principal()
        except Exception as exc:
            if self._library_needs_auth(exc):
                raise RecipeLibraryError("recipe library needs_auth") from None
            raise RecipeLibraryError(
                "recipe library authenticated principal is unavailable"
            ) from exc
        principal = self._provider_text(
            value, "recipe library authenticated principal", 300, required=True
        ) or ""
        connection = self.recipe_libraries[library_id]
        binding = hashlib.sha256(canonical({
            "provider": connection.get("provider"),
            "base_url": connection.get("base_url"),
            "principal": principal,
        }).encode()).hexdigest()
        return principal, binding

    def _read_external_lifecycle(
        self,
        adapter: RecipeLibraryAdapter,
        reference: Mapping[str, str],
        *,
        archive_state: bool = False,
        enforce_version: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str], bool | None]:
        expected = validate_library_recipe_ref(reference)
        try:
            raw = adapter.get({
                "library_id": expected["library_id"],
                "recipe_id": expected["recipe_id"],
            })
        except RecipeLibraryExternalMissingError:
            raise
        except Exception as exc:
            if self._library_needs_auth(exc):
                raise RecipeLibraryError("recipe library needs_auth") from None
            raise RecipeLibraryError("recipe library exact recipe read is unavailable") from exc
        if not isinstance(raw, Mapping):
            raise RecipeLibraryError("recipe library exact recipe read returned invalid data")
        returned = validate_library_recipe_ref(raw.get("library_recipe_ref"))
        if (
            returned["library_id"] != expected["library_id"]
            or returned["recipe_id"] != expected["recipe_id"]
            or "version" not in returned
        ):
            raise RecipeLibraryError("recipe library exact recipe read returned the wrong identity")
        if (
            enforce_version
            and expected.get("version") is not None
            and returned["version"] != expected["version"]
        ):
            raise RecipeLibraryUpdateConflictError(
                "the external recipe changed after its exact reference was read"
            )
        recipe = normalize_recipe(raw)
        archived = None
        if archive_state:
            try:
                state = adapter.get_archive_state(deepcopy(returned))
            except RecipeLibraryExternalMissingError:
                raise
            except Exception as exc:
                if self._library_needs_auth(exc):
                    raise RecipeLibraryError("recipe library needs_auth") from None
                raise RecipeLibraryError("recipe library archive state is unavailable") from exc
            if not isinstance(state, Mapping) or not isinstance(
                state.get("archived"), bool
            ):
                raise RecipeLibraryError("recipe library archive state is invalid")
            state_reference = validate_library_recipe_ref(
                state.get("library_recipe_ref")
            )
            if state_reference != returned:
                raise RecipeLibraryError(
                    "recipe library archive state returned a changed identity"
                )
            archived = state["archived"]
        return recipe, returned, archived

    @staticmethod
    def _outbound_lifecycle_operation(
        operation: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            key: deepcopy(operation[key])
            for key in (
                "operation_id", "kind", "action", "library_id",
                "target_recipe_id", "request_digest", "idempotency_key",
                "snapshot_digest", "provider_binding", "provider_principal",
                "current_archived", "requested_archived",
                "dispatched_at", "created_at", "updated_at",
            )
            if key in operation
        }

    @staticmethod
    def _lifecycle_operation_response(
        operation: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = {
            "library_id": operation.get("library_id"),
            "status": operation.get("status"),
            "operation_id": operation.get("operation_id"),
            "action": operation.get("action"),
            "library_recipe_ref": deepcopy(operation.get("library_recipe_ref")),
        }
        if operation.get("confirmation_id") is not None:
            result["confirmation_id"] = operation["confirmation_id"]
        if operation.get("expires_at") is not None:
            result["expires_at"] = operation["expires_at"]
        if operation.get("name") is not None:
            result["name"] = operation["name"]
        if operation.get("current_archived") is not None:
            result["current_archived"] = operation["current_archived"]
            result["requested_archived"] = operation["requested_archived"]
        if operation.get("result") is not None:
            result.update(deepcopy(operation["result"]))
        if operation.get("error_code") is not None:
            result["error_code"] = operation["error_code"]
        if operation.get("error") is not None:
            result["error"] = operation["error"]
        if (
            operation.get("kind") in {"archive", "delete"}
            and operation.get("idempotency_key") is None
            and operation.get("status") == "pending"
        ):
            result["awaiting_confirmation"] = True
        if operation.get("kind") == "delete":
            result["permanent"] = True
            result["warning"] = (
                "confirmation permanently removes this exact external provider "
                "recipe; frozen local menu, checkout, order and email snapshots remain"
            )
            result["retained_snapshots"] = (
                "active menus, pending checkouts, confirmed orders and recipe emails"
            )
        elif operation.get("kind") == "archive":
            result["reversible"] = True
        return result

    def _external_library_get(self, reference: Mapping[str, str]) -> dict[str, Any]:
        expected_reference = deepcopy(dict(reference))
        library_id = expected_reference["library_id"]
        try:
            capabilities = self._library_capabilities(library_id)
        except RecipeLibraryError as exc:
            if self._library_needs_auth(exc):
                raise RecipeLibraryError("recipe library needs_auth") from None
            raise
        if not capabilities["get"]:
            raise RecipeLibraryError("recipe library get is unsupported or read-only")
        adapter = self.recipe_library_adapters[library_id]
        try:
            raw = adapter.get(deepcopy(expected_reference))
        except Exception as exc:
            if self._library_needs_auth(exc):
                raise RecipeLibraryError("recipe library needs_auth") from None
            raise RecipeLibraryError("recipe library get is unavailable") from exc
        if not isinstance(raw, Mapping):
            raise RecipeLibraryError("recipe library get returned invalid data")
        returned = validate_library_recipe_ref(raw.get("library_recipe_ref"))
        if (
            returned["library_id"] != library_id
            or returned["recipe_id"] != expected_reference["recipe_id"]
            or (
                expected_reference.get("version") is not None
                and returned.get("version") != expected_reference["version"]
            )
        ):
            raise RecipeLibraryError("recipe library get returned a missing or stale identity")
        recipe = normalize_recipe(raw)
        recipe["library_id"] = returned["library_id"]
        recipe["library_recipe_ref"] = returned
        recipe["recipe_key"] = library_recipe_key(returned)
        if capabilities["favorite_read"]:
            if not isinstance(raw.get("is_favorite"), bool):
                raise RecipeLibraryError(
                    "recipe library get returned invalid favorite state"
                )
            recipe["is_favorite"] = raw["is_favorite"]
            if raw.get("favorite_revision") is not None:
                recipe["favorite_revision"] = self._external_favorite_revision(
                    raw["favorite_revision"]
                )
        if raw.get("provider_slug") is not None:
            recipe["provider_slug"] = self._provider_text(
                raw.get("provider_slug"), "recipe library result provider_slug", 300,
                required=True,
            )
        return recipe

    @staticmethod
    def _outbound_library_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        if snapshot.get("rights", {}).get("storage") != "link_only":
            return deepcopy(dict(snapshot))
        return {
            key: deepcopy(snapshot[key])
            for key in ("schema_version", "name", "language", "tags", "source", "rights", "external_snapshot")
            if key in snapshot
        }

    @staticmethod
    def _outbound_library_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(operation[key])
            for key in (
                "operation_id", "kind", "library_id", "target_recipe_id",
                "request_digest", "idempotency_key", "requested_status", "status",
                "source_identity", "snapshot_digest", "dispatched_at", "created_at", "updated_at",
            )
            if key in operation
        }

    def _validated_library_create_result(
        self,
        value: Any,
        snapshot: Mapping[str, Any],
        library_id: str,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        if not isinstance(value, Mapping) or not isinstance(value.get("recipe"), Mapping):
            raise RecipeLibraryError("recipe library create did not provide a semantic readback")
        reference = validate_library_recipe_ref(value.get("library_recipe_ref"))
        if reference["library_id"] != library_id:
            raise RecipeLibraryError("recipe library create returned the wrong library identity")
        returned = normalize_recipe(value["recipe"])
        if canonical(returned.get("source")) != canonical(snapshot.get("source")) or canonical(returned.get("rights")) != canonical(snapshot.get("rights")):
            raise RecipeLibraryError("recipe library create did not preserve attribution and storage rights")
        returned["library_recipe_ref"] = reference
        returned["recipe_key"] = library_recipe_key(reference)
        return reference, returned

    @staticmethod
    def _library_operation_response(operation: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            "saved": operation.get("status") == "confirmed",
            "library_id": operation.get("library_id"),
            "status": operation.get("status"),
            "operation_id": operation.get("operation_id"),
        }
        if operation.get("library_recipe_ref") is not None:
            result["library_recipe_ref"] = deepcopy(operation["library_recipe_ref"])
        if operation.get("error_code") is not None:
            result["error_code"] = operation["error_code"]
        if operation.get("error") is not None:
            result["error"] = operation["error"]
        return result

    @staticmethod
    def _favorite_operation_response(operation: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            "library_id": operation.get("library_id"),
            "status": operation.get("status"),
            "operation_id": operation.get("operation_id"),
            "library_recipe_ref": deepcopy(operation.get("library_recipe_ref")),
            "requested_is_favorite": operation.get("requested_is_favorite"),
        }
        if operation.get("result") is not None:
            result.update(deepcopy(operation["result"]))
        if operation.get("error_code") is not None:
            result["error_code"] = operation["error_code"]
        if operation.get("error") is not None:
            result["error"] = operation["error"]
        return result

    def _read_external_favorite(
        self,
        adapter: RecipeLibraryAdapter,
        reference: Mapping[str, str],
    ) -> dict[str, Any]:
        raw = adapter.get_favorite(deepcopy(dict(reference)))
        return self._normalize_external_favorite(raw, reference)

    def _finish_external_favorite_missing(
        self, operation: Mapping[str, Any]
    ) -> dict[str, Any]:
        failed = self.recipes.finish_library_favorite(
            operation["operation_id"],
            "failed",
            error_code="external_missing",
            error="the exact external recipe is missing",
        )
        return self._favorite_operation_response(failed)

    def _reconcile_external_favorite(
        self,
        operation: Mapping[str, Any],
        adapter: RecipeLibraryAdapter,
        capabilities: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not capabilities["favorite_reconcile"]:
            return self._favorite_operation_response(operation)
        try:
            current = self._read_external_favorite(
                adapter, operation["library_recipe_ref"]
            )
        except RecipeLibraryExternalMissingError:
            return self._finish_external_favorite_missing(operation)
        except Exception as exc:
            response = self._favorite_operation_response(operation)
            if self._library_needs_auth(exc):
                response.update({
                    "error_code": "needs_auth",
                    "error": "recipe library needs_auth before favorite reconciliation",
                })
            return response
        if current["is_favorite"] != operation["requested_is_favorite"]:
            return self._favorite_operation_response(operation)
        current["reconciled"] = True
        confirmed = self.recipes.finish_library_favorite(
            operation["operation_id"], "confirmed", result=current
        )
        return self._favorite_operation_response(confirmed)

    def _set_external_favorite(
        self,
        reference: Mapping[str, str],
        is_favorite: Any,
        *,
        expected_favorite_revision: Any,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        library_id = reference["library_id"]
        if library_id not in self.recipe_libraries:
            raise RecipeLibraryError(
                "library_recipe_ref names an unconfigured recipe library"
            )
        with self._recipe_favorite_lock(reference):
            if not self._recipe_operations_recovered:
                self.recipes.recover_library_operations()
                self._recipe_operations_recovered = True
            operation = self.recipes.begin_library_favorite(
                reference,
                is_favorite,
                expected_favorite_revision=expected_favorite_revision,
                idempotency_key=idempotency_key,
            )
            if operation["status"] in {"confirmed", "failed"}:
                return self._favorite_operation_response(operation)
            adapter = self.recipe_library_adapters.get(library_id)
            if adapter is None:
                response = self._favorite_operation_response(operation)
                response.update({
                    "error_code": "adapter_unavailable",
                    "error": "optional recipe library is unavailable before dispatch",
                })
                return response
            try:
                capabilities = self._library_capabilities(library_id)
            except Exception as exc:
                response = self._favorite_operation_response(operation)
                response.update({
                    "error_code": (
                        "needs_auth"
                        if self._library_needs_auth(exc)
                        else "adapter_unavailable"
                    ),
                    "error": (
                        "recipe library needs_auth before favorite dispatch or reconciliation"
                        if self._library_needs_auth(exc)
                        else "optional recipe library is unavailable before favorite dispatch or reconciliation"
                    ),
                })
                return response
            if operation["status"] == "uncertain":
                return self._reconcile_external_favorite(
                    operation, adapter, capabilities
                )
            if not (
                capabilities["favorite_read"]
                and capabilities["favorite_write_desired_state"]
            ):
                failed = self.recipes.finish_library_favorite(
                    operation["operation_id"],
                    "failed",
                    error_code="unsupported",
                    error="this recipe library does not support native favorite mutation",
                )
                return self._favorite_operation_response(failed)
            if (
                operation.get("expected_favorite_revision") is not None
                and not capabilities["favorite_conditional_write"]
            ):
                failed = self.recipes.finish_library_favorite(
                    operation["operation_id"],
                    "failed",
                    error_code="conditional_unsupported",
                    error="this recipe library does not support conditional favorite mutation",
                )
                return self._favorite_operation_response(failed)
            try:
                current = self._read_external_favorite(adapter, reference)
            except RecipeLibraryExternalMissingError:
                return self._finish_external_favorite_missing(operation)
            except Exception as exc:
                response = self._favorite_operation_response(operation)
                response.update({
                    "error_code": (
                        "needs_auth"
                        if self._library_needs_auth(exc)
                        else "read_unavailable"
                    ),
                    "error": (
                        "recipe library needs_auth before favorite dispatch"
                        if self._library_needs_auth(exc)
                        else "native favorite state is unavailable before dispatch"
                    ),
                })
                return response
            if operation.get("expected_favorite_revision") is not None:
                if "favorite_revision" not in current:
                    failed = self.recipes.finish_library_favorite(
                        operation["operation_id"],
                        "failed",
                        error_code="conditional_unavailable",
                        error="provider did not return its advertised favorite revision",
                    )
                    return self._favorite_operation_response(failed)
                if (
                    current["favorite_revision"]
                    != operation["expected_favorite_revision"]
                ):
                    failed = self.recipes.finish_library_favorite(
                        operation["operation_id"],
                        "failed",
                        error_code="favorite_conflict",
                        error="favorite revision conflict",
                    )
                    return self._favorite_operation_response(failed)
            if current["is_favorite"] == operation["requested_is_favorite"]:
                current["idempotent"] = True
                confirmed = self.recipes.finish_library_favorite(
                    operation["operation_id"], "confirmed", result=current
                )
                return self._favorite_operation_response(confirmed)
            claimed = self.recipes.claim_library_dispatch(operation["operation_id"])
            if not claimed.get("claimed"):
                return self._favorite_operation_response(claimed)
            try:
                adapter.set_favorite(
                    deepcopy(dict(reference)),
                    operation["requested_is_favorite"],
                    expected_favorite_revision=operation.get(
                        "expected_favorite_revision"
                    ),
                )
            except RecipeLibraryFavoriteConflictError:
                failed = self.recipes.finish_library_favorite(
                    operation["operation_id"],
                    "failed",
                    error_code="favorite_conflict",
                    error="favorite revision conflict",
                )
                return self._favorite_operation_response(failed)
            except RecipeLibraryExternalMissingError:
                return self._finish_external_favorite_missing(claimed)
            except RecipeLibraryDefiniteError as exc:
                failed = self.recipes.finish_library_favorite(
                    operation["operation_id"],
                    "failed",
                    error_code=(
                        "needs_auth"
                        if self._library_needs_auth(exc)
                        else "provider_rejected"
                    ),
                    error=(
                        "recipe library favorite mutation needs_auth"
                        if self._library_needs_auth(exc)
                        else "recipe library definitely rejected favorite mutation"
                    ),
                )
                return self._favorite_operation_response(failed)
            except Exception:
                uncertain = self.recipes.finish_library_favorite(
                    operation["operation_id"],
                    "uncertain",
                    error_code="provider_uncertain",
                    error="recipe library favorite mutation may have been dispatched; do not retry",
                )
                return self._reconcile_external_favorite(
                    uncertain, adapter, capabilities
                )
            try:
                confirmed_state = self._read_external_favorite(adapter, reference)
            except RecipeLibraryExternalMissingError:
                return self._finish_external_favorite_missing(claimed)
            except Exception:
                uncertain = self.recipes.finish_library_favorite(
                    operation["operation_id"],
                    "uncertain",
                    error_code="provider_uncertain",
                    error="favorite write was sent but native readback is unavailable",
                )
                return self._favorite_operation_response(uncertain)
            if confirmed_state["is_favorite"] != operation["requested_is_favorite"]:
                uncertain = self.recipes.finish_library_favorite(
                    operation["operation_id"],
                    "uncertain",
                    error_code="provider_uncertain",
                    error="favorite write was sent but native readback did not confirm it",
                )
                return self._favorite_operation_response(uncertain)
            confirmed = self.recipes.finish_library_favorite(
                operation["operation_id"], "confirmed", result=confirmed_state
            )
            return self._favorite_operation_response(confirmed)

    def _normalize_external_label(
        self, value: Any, library_id: str
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) - {
            "library_id",
            "library_label_ref",
            "name",
            "normalized_name",
        }:
            raise RecipeLibraryError("recipe library label returned invalid data")
        reference = validate_library_label_ref(value.get("library_label_ref"))
        name, normalized_name = normalize_label_name(value.get("name"))
        if (
            reference["library_id"] != library_id
            or value.get("library_id") != library_id
            or value.get("normalized_name") != normalized_name
        ):
            raise RecipeLibraryError("recipe library label returned the wrong identity")
        return {
            "library_id": library_id,
            "library_label_ref": reference,
            "name": name,
            "normalized_name": normalized_name,
        }

    def _read_external_labels(
        self, adapter: RecipeLibraryAdapter, library_id: str
    ) -> list[dict[str, Any]]:
        raw = adapter.list_labels()
        if not isinstance(raw, list) or len(raw) > 1_000:
            raise RecipeLibraryError("recipe library label list returned invalid data")
        labels = [self._normalize_external_label(item, library_id) for item in raw]
        ids = [item["library_label_ref"]["label_id"] for item in labels]
        if len(ids) != len(set(ids)):
            raise RecipeLibraryError("recipe library label list returned duplicate identities")
        return labels

    def _read_external_recipe_labels(
        self,
        adapter: RecipeLibraryAdapter,
        reference: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        raw = adapter.get_recipe_labels(deepcopy(dict(reference)))
        if not isinstance(raw, list) or len(raw) > 1_000:
            raise RecipeLibraryError("recipe library recipe labels returned invalid data")
        labels = [
            self._normalize_external_label(item, reference["library_id"])
            for item in raw
        ]
        ids = [item["library_label_ref"]["label_id"] for item in labels]
        if len(ids) != len(set(ids)):
            raise RecipeLibraryError(
                "recipe library recipe labels returned duplicate identities"
            )
        return labels

    def _recipe_label_lock(self, library_id: str, target: str) -> threading.Lock:
        key = (library_id, target)
        with self.recipe_label_locks_guard:
            return self.recipe_label_locks.setdefault(key, threading.Lock())

    @staticmethod
    def _label_operation_response(operation: Mapping[str, Any]) -> dict[str, Any]:
        result = {
            "library_id": operation.get("library_id"),
            "status": operation.get("status"),
            "operation_id": operation.get("operation_id"),
            "action": operation.get("action"),
        }
        if operation.get("library_recipe_ref") is not None:
            result["library_recipe_ref"] = deepcopy(operation["library_recipe_ref"])
        if operation.get("library_label_ref") is not None:
            result["library_label_ref"] = deepcopy(operation["library_label_ref"])
        if operation.get("result") is not None:
            result.update(deepcopy(operation["result"]))
        if operation.get("error_code") is not None:
            result["error_code"] = operation["error_code"]
        if operation.get("error") is not None:
            result["error"] = operation["error"]
        return result

    @staticmethod
    def _label_result(
        label: Mapping[str, Any],
        *,
        recipe_ref: Mapping[str, str] | None = None,
        present: bool | None = None,
        idempotent: bool = False,
        reconciled: bool = False,
    ) -> dict[str, Any]:
        result = deepcopy(dict(label))
        if recipe_ref is not None:
            result["library_recipe_ref"] = deepcopy(dict(recipe_ref))
        if present is not None:
            result["present"] = present
        if idempotent:
            result["idempotent"] = True
        if reconciled:
            result["reconciled"] = True
        return result

    def _reconcile_external_label_change(
        self,
        operation: Mapping[str, Any],
        adapter: RecipeLibraryAdapter,
        label: Mapping[str, Any],
    ) -> dict[str, Any]:
        try:
            attached = self._read_external_recipe_labels(
                adapter, operation["library_recipe_ref"]
            )
        except RecipeLibraryExternalMissingError:
            failed = self.recipes.finish_library_label(
                operation["operation_id"],
                "failed",
                error_code="external_missing",
                error="the exact external recipe is missing",
            )
            return self._label_operation_response(failed)
        except Exception as exc:
            response = self._label_operation_response(operation)
            response["error"] = (
                "recipe library needs_auth before label reconciliation"
                if self._library_needs_auth(exc)
                else "native label state is unavailable for reconciliation"
            )
            return response
        present = any(
            item["library_label_ref"]["label_id"]
            == label["library_label_ref"]["label_id"]
            for item in attached
        )
        desired = operation["action"] == "apply"
        if present != desired:
            return self._label_operation_response(operation)
        confirmed = self.recipes.finish_library_label(
            operation["operation_id"],
            "confirmed",
            result=self._label_result(
                label,
                recipe_ref=operation["library_recipe_ref"],
                present=desired,
                reconciled=True,
            ),
        )
        return self._label_operation_response(confirmed)

    def _set_external_label(
        self,
        recipe_reference: Any,
        label_reference: Any,
        present: Any,
        *,
        expected_label_revision: Any,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        recipe_ref = validate_library_recipe_ref(recipe_reference)
        label_ref = validate_library_label_ref(label_reference)
        library_id = recipe_ref["library_id"]
        if (
            library_id == "builtin"
            or label_ref["library_id"] != library_id
            or library_id not in self.recipe_libraries
        ):
            raise RecipeLibraryError(
                "label operation requires exact refs from one configured external library"
            )
        with self._recipe_label_lock(library_id, recipe_ref["recipe_id"]):
            if not self._recipe_operations_recovered:
                self.recipes.recover_library_operations()
                self._recipe_operations_recovered = True
            operation = self.recipes.begin_library_label_change(
                recipe_ref,
                label_ref,
                present,
                expected_label_revision=expected_label_revision,
                idempotency_key=idempotency_key,
            )
            if operation["status"] in {"confirmed", "failed"}:
                return self._label_operation_response(operation)
            adapter = self.recipe_library_adapters.get(library_id)
            if adapter is None:
                if operation["status"] == "uncertain":
                    response = self._label_operation_response(operation)
                    response.update({
                        "error_code": "adapter_unavailable",
                        "error": "optional recipe library is unavailable for label reconciliation",
                    })
                    return response
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="unsupported",
                    error="optional recipe library adapter is not installed",
                )
                return self._label_operation_response(failed)
            try:
                capabilities = self._library_capabilities(library_id)
            except Exception as exc:
                if operation["status"] == "uncertain":
                    response = self._label_operation_response(operation)
                    response.update({
                        "error_code": "needs_auth" if self._library_needs_auth(exc) else "unavailable",
                        "error": (
                            "recipe library needs_auth before label reconciliation"
                            if self._library_needs_auth(exc)
                            else "optional recipe library is unavailable for label reconciliation"
                        ),
                    })
                    return response
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="needs_auth" if self._library_needs_auth(exc) else "unavailable",
                    error=(
                        "recipe library needs_auth before label dispatch"
                        if self._library_needs_auth(exc)
                        else "optional recipe library is unavailable before label dispatch"
                    ),
                )
                return self._label_operation_response(failed)
            if operation["status"] == "uncertain":
                if not capabilities["label_reconcile"]:
                    return self._label_operation_response(operation)
                try:
                    matches = [
                        item
                        for item in self._read_external_labels(adapter, library_id)
                        if item["library_label_ref"]["label_id"] == label_ref["label_id"]
                    ]
                    if len(matches) != 1:
                        return self._label_operation_response(operation)
                    return self._reconcile_external_label_change(
                        operation, adapter, matches[0]
                    )
                except Exception as exc:
                    response = self._label_operation_response(operation)
                    if self._library_needs_auth(exc):
                        response.update({
                            "error_code": "needs_auth",
                            "error": "recipe library needs_auth before label reconciliation",
                        })
                    return response
            capability = "label_apply_existing" if present is True else "label_remove"
            if not capabilities["label_read"] or not capabilities[capability]:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="unsupported",
                    error="this recipe library does not support native desired-state label mutation",
                )
                return self._label_operation_response(failed)
            if (
                operation.get("expected_label_revision") is not None
                and not capabilities["label_conditional_write"]
            ):
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="unsupported_conditional",
                    error="this recipe library does not support conditional label mutation",
                )
                return self._label_operation_response(failed)
            try:
                library_labels = self._read_external_labels(adapter, library_id)
                matches = [
                    item
                    for item in library_labels
                    if item["library_label_ref"]["label_id"] == label_ref["label_id"]
                ]
                if len(matches) != 1:
                    raise RecipeLibraryExternalMissingError(
                        "the exact provider label is missing"
                    )
                label = matches[0]
                attached = self._read_external_recipe_labels(adapter, recipe_ref)
            except RecipeLibraryExternalMissingError:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="external_missing",
                    error="the exact external recipe or label is missing",
                )
                return self._label_operation_response(failed)
            except Exception as exc:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="needs_auth" if self._library_needs_auth(exc) else "unavailable",
                    error=(
                        "recipe library needs_auth before label dispatch"
                        if self._library_needs_auth(exc)
                        else "native label state is unavailable before dispatch"
                    ),
                )
                return self._label_operation_response(failed)
            current = next(
                (
                    item
                    for item in attached
                    if item["library_label_ref"]["label_id"] == label_ref["label_id"]
                ),
                None,
            )
            if operation.get("expected_label_revision") is not None:
                if (
                    current is None
                    or current["library_label_ref"].get("version")
                    != operation["expected_label_revision"]
                ):
                    failed = self.recipes.finish_library_label(
                        operation["operation_id"],
                        "failed",
                        error_code="label_conflict",
                        error="label revision conflict",
                    )
                    return self._label_operation_response(failed)
            if (current is not None) == (present is True):
                confirmed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "confirmed",
                    result=self._label_result(
                        current or label,
                        recipe_ref=recipe_ref,
                        present=present is True,
                        idempotent=True,
                    ),
                )
                return self._label_operation_response(confirmed)
            claimed = self.recipes.claim_library_dispatch(operation["operation_id"])
            if not claimed.get("claimed"):
                return self._label_operation_response(claimed)
            try:
                adapter.set_label(
                    deepcopy(recipe_ref),
                    deepcopy(label_ref),
                    present is True,
                    expected_label_revision=operation.get("expected_label_revision"),
                )
            except RecipeLibraryLabelConflictError:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="label_conflict",
                    error="label revision conflict",
                )
                return self._label_operation_response(failed)
            except RecipeLibraryExternalMissingError:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="external_missing",
                    error="the exact external recipe or label is missing",
                )
                return self._label_operation_response(failed)
            except RecipeLibraryDefiniteError as exc:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="needs_auth" if self._library_needs_auth(exc) else "provider_rejected",
                    error=(
                        "recipe library label mutation needs_auth"
                        if self._library_needs_auth(exc)
                        else "recipe library definitely rejected label mutation"
                    ),
                )
                return self._label_operation_response(failed)
            except Exception:
                uncertain = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "uncertain",
                    error_code="provider_uncertain",
                    error="recipe library label mutation may have been dispatched; do not retry",
                )
                if capabilities["label_reconcile"]:
                    return self._reconcile_external_label_change(
                        uncertain, adapter, label
                    )
                return self._label_operation_response(uncertain)
            try:
                attached = self._read_external_recipe_labels(adapter, recipe_ref)
            except Exception:
                uncertain = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "uncertain",
                    error_code="provider_uncertain",
                    error="label write was sent but native readback is unavailable",
                )
                return self._label_operation_response(uncertain)
            desired = present is True
            confirmed_present = any(
                item["library_label_ref"]["label_id"] == label_ref["label_id"]
                for item in attached
            )
            if confirmed_present != desired:
                uncertain = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "uncertain",
                    error_code="provider_uncertain",
                    error="label write was sent but native readback did not confirm it",
                )
                return self._label_operation_response(uncertain)
            confirmed_label = next(
                (
                    item
                    for item in attached
                    if item["library_label_ref"]["label_id"] == label_ref["label_id"]
                ),
                label,
            )
            confirmed = self.recipes.finish_library_label(
                operation["operation_id"],
                "confirmed",
                result=self._label_result(
                    confirmed_label,
                    recipe_ref=recipe_ref,
                    present=desired,
                ),
            )
            return self._label_operation_response(confirmed)

    def _create_external_label(
        self, library_id: Any, name: Any, *, idempotency_key: Any
    ) -> dict[str, Any]:
        library = validate_library_id(library_id, allow_builtin=False)
        if library not in self.recipe_libraries:
            raise RecipeLibraryError(
                "library_id must name one exact configured external recipe library"
            )
        display, normalized_name = normalize_label_name(name)
        with self._recipe_label_lock(library, f"create:{normalized_name}"):
            if not self._recipe_operations_recovered:
                self.recipes.recover_library_operations()
                self._recipe_operations_recovered = True
            operation = self.recipes.begin_library_label_create(
                library,
                display,
                idempotency_key=idempotency_key,
            )
            if operation["status"] in {"confirmed", "failed"}:
                return self._label_operation_response(operation)
            adapter = self.recipe_library_adapters.get(library)
            if adapter is None:
                if operation["status"] == "uncertain":
                    response = self._label_operation_response(operation)
                    response.update({
                        "error_code": "adapter_unavailable",
                        "error": "optional recipe library is unavailable for label reconciliation",
                    })
                    return response
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="unsupported",
                    error="optional recipe library adapter is not installed",
                )
                return self._label_operation_response(failed)
            try:
                capabilities = self._library_capabilities(library)
            except Exception as exc:
                if operation["status"] == "uncertain":
                    response = self._label_operation_response(operation)
                    response.update({
                        "error_code": "needs_auth" if self._library_needs_auth(exc) else "unavailable",
                        "error": (
                            "recipe library needs_auth before label reconciliation"
                            if self._library_needs_auth(exc)
                            else "optional recipe library is unavailable for label reconciliation"
                        ),
                    })
                    return response
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="needs_auth" if self._library_needs_auth(exc) else "unavailable",
                    error=(
                        "recipe library needs_auth before label creation"
                        if self._library_needs_auth(exc)
                        else "optional recipe library is unavailable before label creation"
                    ),
                )
                return self._label_operation_response(failed)
            if operation["status"] == "uncertain":
                if not capabilities["label_reconcile"]:
                    return self._label_operation_response(operation)
                try:
                    raw = adapter.reconcile_label_create(
                        display, self._outbound_library_operation(operation)
                    )
                    if raw is None:
                        return self._label_operation_response(operation)
                    label = self._normalize_external_label(raw, library)
                    if label["normalized_name"] != normalized_name:
                        return self._label_operation_response(operation)
                    confirmed = self.recipes.finish_library_label(
                        operation["operation_id"],
                        "confirmed",
                        result=self._label_result(label, reconciled=True),
                    )
                    return self._label_operation_response(confirmed)
                except Exception as exc:
                    response = self._label_operation_response(operation)
                    if self._library_needs_auth(exc):
                        response.update({
                            "error_code": "needs_auth",
                            "error": "recipe library needs_auth before label reconciliation",
                        })
                    return response
            if not capabilities["label_read"] or not capabilities["label_create"]:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="unsupported",
                    error="this recipe library does not support explicit native label creation",
                )
                return self._label_operation_response(failed)
            try:
                labels = self._read_external_labels(adapter, library)
            except Exception as exc:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="needs_auth" if self._library_needs_auth(exc) else "unavailable",
                    error=(
                        "recipe library needs_auth before label creation"
                        if self._library_needs_auth(exc)
                        else "label identities are unavailable before creation"
                    ),
                )
                return self._label_operation_response(failed)
            if any(item["normalized_name"] == normalized_name for item in labels):
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="label_name_conflict",
                    error="an equal normalized label name already exists; use its exact label ID",
                )
                return self._label_operation_response(failed)
            claimed = self.recipes.claim_library_dispatch(operation["operation_id"])
            if not claimed.get("claimed"):
                return self._label_operation_response(claimed)
            try:
                raw = adapter.create_label(display, idempotency_key=operation["idempotency_key"])
                label = self._normalize_external_label(raw, library)
                if label["normalized_name"] != normalized_name:
                    raise RecipeLibraryUncertainError(
                        "provider returned a different label identity"
                    )
            except RecipeLibraryDefiniteError as exc:
                failed = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "failed",
                    error_code="needs_auth" if self._library_needs_auth(exc) else "provider_rejected",
                    error=(
                        "recipe library label creation needs_auth"
                        if self._library_needs_auth(exc)
                        else "recipe library definitely rejected label creation"
                    ),
                )
                return self._label_operation_response(failed)
            except Exception:
                uncertain = self.recipes.finish_library_label(
                    operation["operation_id"],
                    "uncertain",
                    error_code="provider_uncertain",
                    error="recipe library label creation may have been dispatched; do not retry",
                )
                if capabilities["label_reconcile"]:
                    try:
                        raw = adapter.reconcile_label_create(
                            display, self._outbound_library_operation(uncertain)
                        )
                        if raw is not None:
                            label = self._normalize_external_label(raw, library)
                            if label["normalized_name"] == normalized_name:
                                confirmed = self.recipes.finish_library_label(
                                    operation["operation_id"],
                                    "confirmed",
                                    result=self._label_result(label, reconciled=True),
                                )
                                return self._label_operation_response(confirmed)
                    except Exception:
                        pass
                return self._label_operation_response(uncertain)
            confirmed = self.recipes.finish_library_label(
                operation["operation_id"], "confirmed", result=label
            )
            return self._label_operation_response(confirmed)

    def _save_discovery_to_library(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not self._recipe_operations_recovered:
            self.recipes.recover_library_operations()
            self._recipe_operations_recovered = True
        discovery_ref = request.get("discovery_ref")
        explicit_target = request.get("library_id")
        if explicit_target is None:
            bound = self.recipes.bound_library_for_discovery(
                discovery_ref, idempotency_key=request.get("idempotency_key")
            )
            target = bound or self.primary_recipe_library_id
        else:
            target = validate_library_id(explicit_target)
        if target not in self.recipe_libraries:
            raise RecipeLibraryError("library_id must name one exact configured recipe library")
        operation = self.recipes.begin_library_create(
            discovery_ref,
            target,
            status=str(request.get("status") or "active"),
            idempotency_key=request.get("idempotency_key"),
        )
        if operation["status"] in {"confirmed", "failed"}:
            return self._library_operation_response(operation)
        connection = self.recipe_libraries[target]
        adapter = self.recipe_library_adapters.get(target)
        if target != "builtin":
            if operation["status"] != "uncertain" and connection["read_only"]:
                failed = self.recipes.finish_library_create(
                    operation["operation_id"], "failed",
                    error_code="unsupported", error="recipe library create is unsupported or read-only",
                )
                return self._library_operation_response(failed)
            try:
                capabilities = self._library_capabilities(target)
            except RecipeLibraryError as exc:
                if operation["status"] == "uncertain":
                    response = self._library_operation_response(operation)
                    if self._library_needs_auth(exc):
                        response.update({
                            "error_code": "needs_auth",
                            "error": "recipe library needs_auth before reconciliation",
                        })
                    return response
                pending = dict(operation)
                pending.update({
                    "error_code": (
                        "needs_auth"
                        if self._library_needs_auth(exc)
                        else "adapter_unavailable"
                    ),
                    "error": (
                        "recipe library needs_auth before dispatch"
                        if self._library_needs_auth(exc)
                        else "optional recipe library is unavailable before dispatch"
                    ),
                })
                return self._library_operation_response(pending)
            if operation["status"] != "uncertain" and (
                not capabilities["create_from_discovery"]
            ):
                failed = self.recipes.finish_library_create(
                    operation["operation_id"], "failed",
                    error_code="unsupported", error="recipe library create is unsupported or read-only",
                )
                return self._library_operation_response(failed)
        if operation["status"] == "uncertain":
            if target == "builtin":
                pass
            elif not capabilities["reconcile_create"]:
                return self._library_operation_response(operation)
            else:
                current = self.recipes.library_operation_snapshot(operation["operation_id"])
                try:
                    reconciled = adapter.reconcile_create(
                        self._outbound_library_snapshot(current["snapshot"]),
                        self._outbound_library_operation(current),
                    )
                    if reconciled is None:
                        return self._library_operation_response(current)
                    reference, recipe = self._validated_library_create_result(
                        reconciled, current["snapshot"], target
                    )
                    confirmed = self.recipes.finish_library_create(
                        operation["operation_id"], "confirmed", library_recipe_ref=reference
                    )
                    response = self._library_operation_response(confirmed)
                    response["recipe"] = recipe
                    return response
                except Exception as exc:
                    response = self._library_operation_response(current)
                    if self._library_needs_auth(exc):
                        response.update({
                            "error_code": "needs_auth",
                            "error": "recipe library needs_auth before reconciliation",
                        })
                    return response
        resume_builtin = operation["status"] == "uncertain" and target == "builtin"
        if not resume_builtin:
            claimed = self.recipes.claim_library_dispatch(operation["operation_id"])
            if not claimed["claimed"]:
                return self._library_operation_response(claimed)
        current = self.recipes.library_operation_snapshot(operation["operation_id"])
        if target == "builtin":
            try:
                saved = self.recipes.save_discovery(
                    discovery_ref,
                    status=current["requested_status"],
                    idempotency_key=request.get("idempotency_key"),
                )
                conflict = saved.pop("conflict", None)
                if conflict is not None:
                    failed = self.recipes.finish_library_create(
                        operation["operation_id"], "failed",
                        error_code="source_conflict", error="source identity has different content",
                    )
                    response = self._library_operation_response(failed)
                    response["recipe"] = saved
                    response["conflict"] = conflict
                    return response
                reference = saved["library_recipe_ref"]
            except RecipeError:
                failed = self.recipes.finish_library_create(
                    operation["operation_id"], "failed",
                    error_code="builtin_rejected", error="built-in recipe save was rejected",
                )
                return self._library_operation_response(failed)
            try:
                confirmed = self.recipes.finish_library_create(
                    operation["operation_id"], "confirmed", library_recipe_ref=reference
                )
                response = self._library_operation_response(confirmed)
                response["recipe"] = saved
                return response
            except RecipeError:
                uncertain = self.recipes.finish_library_create(
                    operation["operation_id"], "uncertain",
                    error_code="builtin_uncertain", error="built-in recipe save needs reconciliation",
                )
                return self._library_operation_response(uncertain)
        try:
            created = adapter.create_from_snapshot(
                self._outbound_library_snapshot(current["snapshot"]),
                self._outbound_library_operation(current),
            )
            reference, recipe = self._validated_library_create_result(
                created, current["snapshot"], target
            )
            confirmed = self.recipes.finish_library_create(
                operation["operation_id"], "confirmed", library_recipe_ref=reference
            )
            response = self._library_operation_response(confirmed)
            response["recipe"] = recipe
            return response
        except RecipeLibraryDefiniteError as exc:
            if self._library_needs_auth(exc):
                pending = self.recipes.defer_library_create_for_auth(
                    operation["operation_id"]
                )
                return self._library_operation_response(pending)
            failed = self.recipes.finish_library_create(
                operation["operation_id"], "failed",
                error_code="provider_rejected",
                error="recipe library definitely rejected the create",
            )
            return self._library_operation_response(failed)
        except Exception as exc:
            uncertain = self.recipes.finish_library_create(
                operation["operation_id"], "uncertain",
                error_code="provider_uncertain", error="recipe library create may have been dispatched; do not retry",
            )
            response = self._library_operation_response(uncertain)
            if self._library_needs_auth(exc):
                response.update({
                    "error_code": "needs_auth",
                    "error": "recipe library create may have been dispatched; needs_auth before reconciliation",
                })
            return response

    def _prepare_external_lifecycle(
        self, request: Mapping[str, Any], kind: str
    ) -> dict[str, Any]:
        if any(
            request.get(field) is not None
            for field in (
                "library_id", "recipe_id", "confirmation_id", "idempotency_key",
                "recipe", "expected_revision", "status",
            )
        ):
            raise RecipeLibraryError(
                f"{kind}_prepare accepts only one exact library_recipe_ref"
            )
        reference = validate_library_recipe_ref(request.get("library_recipe_ref"))
        library_id = reference["library_id"]
        if library_id == "builtin" or library_id not in self.recipe_libraries:
            raise RecipeLibraryError(
                f"{kind}_prepare requires one configured external library_recipe_ref"
            )
        capabilities = self._library_capabilities(library_id)
        capability = "delete" if kind == "delete" else "archive_desired_state"
        reconciliation = "reconcile_delete" if kind == "delete" else "reconcile_archive"
        if not capabilities[capability] or not capabilities[reconciliation]:
            raise RecipeLibraryError(
                f"this recipe library does not support safely reconcilable {kind}"
            )
        requested_archived = request.get("archived")
        if kind == "archive" and not isinstance(requested_archived, bool):
            raise RecipeLibraryError("archive_prepare requires archived=true or false")
        if kind == "delete" and requested_archived is not None:
            raise RecipeLibraryError("delete_prepare does not accept archived")
        adapter = self.recipe_library_adapters[library_id]
        with self._recipe_lifecycle_lock(reference):
            self._recover_recipe_library_operations()
            try:
                provider_principal, provider_binding = (
                    self._recipe_library_context(library_id, adapter)
                )
                recipe, returned, current_archived = self._read_external_lifecycle(
                    adapter,
                    reference,
                    archive_state=kind == "archive",
                    enforce_version="version" in reference,
                )
            except RecipeLibraryExternalMissingError:
                raise RecipeLibraryError(
                    "the exact external recipe is missing; no lifecycle action was prepared"
                ) from None
            operation = self.recipes.prepare_library_lifecycle(
                kind,
                returned,
                recipe["name"],
                self._recipe_lifecycle_digest(recipe),
                provider_binding=provider_binding,
                provider_principal=provider_principal,
                current_archived=current_archived,
                requested_archived=requested_archived,
            )
            return self._lifecycle_operation_response(operation)

    def _reconcile_external_update(
        self,
        operation: Mapping[str, Any],
        adapter: RecipeLibraryAdapter,
    ) -> dict[str, Any]:
        try:
            current, returned, _archived = self._read_external_lifecycle(
                adapter, operation["library_recipe_ref"]
            )
        except RecipeLibraryExternalMissingError:
            failed = self.recipes.finish_library_lifecycle(
                operation["operation_id"], "failed",
                error_code="external_missing",
                error="the exact external recipe is missing",
            )
            return self._lifecycle_operation_response(failed)
        except Exception as exc:
            response = self._lifecycle_operation_response(operation)
            if self._library_needs_auth(exc):
                response.update({
                    "error_code": "needs_auth",
                    "error": "recipe library needs_auth before update reconciliation",
                })
            return response
        if canonical(current) != canonical(operation["replacement"]):
            return self._lifecycle_operation_response(operation)
        confirmed = self.recipes.finish_library_lifecycle(
            operation["operation_id"], "confirmed",
            result={"library_recipe_ref": returned, "updated": True},
        )
        return self._lifecycle_operation_response(confirmed)

    def _update_external_recipe(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if request.get("library_id") is not None or request.get("recipe_id") is not None:
            raise RecipeLibraryError(
                "external update requires only one exact library_recipe_ref identity"
            )
        reference = validate_library_recipe_ref(request.get("library_recipe_ref"))
        library_id = reference["library_id"]
        if library_id == "builtin" or "version" not in reference:
            raise RecipeLibraryError(
                "external update requires one configured versioned library_recipe_ref"
            )
        replacement = normalize_recipe(request.get("recipe"))
        request_digest = hashlib.sha256(canonical({
            "kind": "conditional_update",
            "library_recipe_ref": reference,
            "replacement": replacement,
        }).encode()).hexdigest()
        existing = self.recipes.library_operation_for_idempotency(
            request.get("idempotency_key")
        )
        if existing is not None:
            if (
                existing.get("kind") != "conditional_update"
                or existing.get("library_id") != library_id
                or existing.get("target_recipe_id") != reference["recipe_id"]
                or existing.get("request_digest") != request_digest
            ):
                raise RecipeError(
                    "idempotency key was already used for another library operation"
                )
            if existing["status"] in {"confirmed", "failed"}:
                return self._lifecycle_operation_response(existing)
        if library_id not in self.recipe_libraries:
            raise RecipeLibraryError(
                "external update requires one configured versioned library_recipe_ref"
            )
        adapter = self.recipe_library_adapters.get(library_id)
        if adapter is None:
            raise RecipeLibraryError("optional recipe library is unavailable")
        with self._recipe_lifecycle_lock(reference):
            self._recover_recipe_library_operations()
            if existing is not None:
                existing = self.recipes.library_operation_snapshot(
                    existing["operation_id"]
                )
            try:
                provider_principal, provider_binding = (
                    self._recipe_library_context(library_id, adapter)
                )
            except Exception as exc:
                if existing is None:
                    raise
                response = self._lifecycle_operation_response(existing)
                response.update({
                    "error_code": (
                        "needs_auth" if self._library_needs_auth(exc) else "unavailable"
                    ),
                    "error": (
                        "recipe library needs_auth before update reconciliation"
                        if self._library_needs_auth(exc)
                        else "recipe library provider context is unavailable"
                    ),
                })
                return response
            if existing is not None and (
                existing["provider_principal"] != provider_principal
                or existing["provider_binding"] != provider_binding
            ):
                if existing["status"] == "pending" and existing.get(
                    "dispatched_at"
                ) is None:
                    existing = self.recipes.finish_library_lifecycle(
                        existing["operation_id"], "failed",
                        error_code="provider_context_changed",
                        error="recipe library provider context changed before dispatch",
                    )
                response = self._lifecycle_operation_response(existing)
                response.update({
                    "error_code": "provider_context_changed",
                    "error": "recipe library provider context changed; original outcome must be resolved there",
                })
                return response
            if existing is not None and existing.get("dispatched_at") is not None:
                if existing["status"] == "pending":
                    existing = self.recipes.finish_library_lifecycle(
                        existing["operation_id"], "uncertain",
                        error_code="uncertain",
                        error="conditional update may be in flight; reconcile before retry",
                    )
                return self._reconcile_external_update(existing, adapter)
            if existing is not None and existing["status"] == "uncertain":
                return self._reconcile_external_update(existing, adapter)
            capabilities = self._library_capabilities(library_id)
            if not capabilities["conditional_update"]:
                if existing is not None:
                    failed = self.recipes.finish_library_lifecycle(
                        existing["operation_id"], "failed",
                        error_code="unsupported",
                        error="recipe library no longer supports conditional update",
                    )
                    return self._lifecycle_operation_response(failed)
                raise RecipeLibraryError(
                    "this recipe library has no provider-enforced conditional update"
                )
            try:
                current, returned, _archived = self._read_external_lifecycle(
                    adapter, reference
                )
            except RecipeLibraryExternalMissingError:
                raise RecipeLibraryError("the exact external recipe is missing") from None
            operation = self.recipes.begin_library_conditional_update(
                reference,
                replacement,
                self._recipe_lifecycle_digest(current),
                provider_binding=provider_binding,
                provider_principal=provider_principal,
                idempotency_key=request.get("idempotency_key"),
            )
            if operation["status"] in {"confirmed", "failed"}:
                return self._lifecycle_operation_response(operation)
            if (
                operation["provider_principal"] != provider_principal
                or operation["provider_binding"] != provider_binding
            ):
                if operation["status"] == "pending" and operation.get(
                    "dispatched_at"
                ) is None:
                    operation = self.recipes.finish_library_lifecycle(
                        operation["operation_id"], "failed",
                        error_code="provider_context_changed",
                        error="recipe library provider context changed before dispatch",
                    )
                return self._lifecycle_operation_response(operation)
            if operation.get("dispatched_at") is not None:
                if operation["status"] == "pending":
                    operation = self.recipes.finish_library_lifecycle(
                        operation["operation_id"], "uncertain",
                        error_code="uncertain",
                        error="conditional update may be in flight; reconcile before retry",
                    )
                return self._reconcile_external_update(operation, adapter)
            if operation["status"] == "uncertain":
                return self._reconcile_external_update(operation, adapter)
            if (
                returned.get("version") != reference["version"]
                or self._recipe_lifecycle_digest(current)
                != operation["snapshot_digest"]
            ):
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code="conflict",
                    error="the external recipe changed before conditional update",
                )
                response = self._lifecycle_operation_response(failed)
                response["current_library_recipe_ref"] = returned
                return response
            if (
                canonical(current.get("source"))
                != canonical(replacement.get("source"))
                or canonical(current.get("rights"))
                != canonical(replacement.get("rights"))
            ):
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code="attribution_conflict",
                    error="conditional update must preserve source, rights and attribution",
                )
                return self._lifecycle_operation_response(failed)
            claimed = self.recipes.claim_library_dispatch(operation["operation_id"])
            if not claimed.get("claimed"):
                if claimed["status"] == "pending" and claimed.get(
                    "dispatched_at"
                ) is not None:
                    claimed = self.recipes.finish_library_lifecycle(
                        claimed["operation_id"], "uncertain",
                        error_code="uncertain",
                        error="conditional update may be in flight; reconcile before retry",
                    )
                if claimed["status"] == "uncertain":
                    return self._reconcile_external_update(claimed, adapter)
                return self._lifecycle_operation_response(claimed)
            outbound = self._outbound_lifecycle_operation(claimed)
            try:
                raw = adapter.update_recipe(
                    deepcopy(reference), deepcopy(replacement), outbound
                )
                if not isinstance(raw, Mapping) or not isinstance(
                    raw.get("recipe"), Mapping
                ):
                    raise RecipeLibraryError(
                        "recipe library update did not provide semantic readback"
                    )
                result_reference = validate_library_recipe_ref(
                    raw.get("library_recipe_ref")
                )
                result_recipe = normalize_recipe(raw["recipe"])
                if (
                    result_reference["library_id"] != library_id
                    or result_reference["recipe_id"] != reference["recipe_id"]
                    or "version" not in result_reference
                    or canonical(result_recipe) != canonical(replacement)
                ):
                    raise RecipeLibraryError(
                        "recipe library update readback changed identity or content"
                    )
                confirmed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "confirmed",
                    result={
                        "library_recipe_ref": result_reference,
                        "updated": True,
                    },
                )
                return self._lifecycle_operation_response(confirmed)
            except RecipeLibraryUpdateConflictError:
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code="conflict",
                    error="the provider rejected a stale conditional update",
                )
                response = self._lifecycle_operation_response(failed)
                try:
                    _current, current_reference, _archived = (
                        self._read_external_lifecycle(adapter, reference)
                    )
                    response["current_library_recipe_ref"] = current_reference
                except Exception:
                    pass
                return response
            except RecipeLibraryExternalMissingError:
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code="external_missing",
                    error="the exact external recipe is missing",
                )
                return self._lifecycle_operation_response(failed)
            except RecipeLibraryDefiniteError as exc:
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code=(
                        "needs_auth" if self._library_needs_auth(exc)
                        else "provider_rejected"
                    ),
                    error=(
                        "recipe library needs_auth"
                        if self._library_needs_auth(exc)
                        else "recipe library rejected conditional update"
                    ),
                )
                return self._lifecycle_operation_response(failed)
            except Exception:
                uncertain = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "uncertain",
                    error_code="uncertain",
                    error="conditional update may have been dispatched; reconcile before retry",
                )
                return self._lifecycle_operation_response(uncertain)

    def _reconcile_external_lifecycle(
        self,
        operation: Mapping[str, Any],
        adapter: RecipeLibraryAdapter,
        capabilities: Mapping[str, Any],
    ) -> dict[str, Any]:
        kind = operation["kind"]
        capability = "reconcile_delete" if kind == "delete" else "reconcile_archive"
        if not capabilities[capability]:
            return self._lifecycle_operation_response(operation)
        outbound = self._outbound_lifecycle_operation(operation)
        try:
            if kind == "delete":
                absent = adapter.reconcile_delete(
                    deepcopy(operation["library_recipe_ref"]), outbound
                )
                if absent is not True:
                    return self._lifecycle_operation_response(operation)
                result = {
                    "library_recipe_ref": operation["library_recipe_ref"],
                    "deleted": True,
                }
            else:
                raw = adapter.reconcile_archive(
                    deepcopy(operation["library_recipe_ref"]),
                    operation["requested_archived"],
                    outbound,
                )
                if not isinstance(raw, Mapping) or raw.get("archived") is not operation[
                    "requested_archived"
                ]:
                    return self._lifecycle_operation_response(operation)
                returned = validate_library_recipe_ref(
                    raw.get("library_recipe_ref")
                )
                if (
                    returned["library_id"] != operation["library_id"]
                    or returned["recipe_id"] != operation["target_recipe_id"]
                    or "version" not in returned
                ):
                    return self._lifecycle_operation_response(operation)
                result = {
                    "library_recipe_ref": returned,
                    "archived": operation["requested_archived"],
                }
            confirmed = self.recipes.finish_library_lifecycle(
                operation["operation_id"], "confirmed", result=result
            )
            return self._lifecycle_operation_response(confirmed)
        except Exception as exc:
            response = self._lifecycle_operation_response(operation)
            if self._library_needs_auth(exc):
                response.update({
                    "error_code": "needs_auth",
                    "error": f"recipe library needs_auth before {kind} reconciliation",
                })
            return response

    def _confirm_external_lifecycle(
        self, request: Mapping[str, Any], kind: str
    ) -> dict[str, Any]:
        if any(
            request.get(field) is not None
            for field in (
                "library_id", "recipe_id", "library_recipe_ref", "archived",
                "recipe", "expected_revision", "status",
            )
        ):
            raise RecipeLibraryError(
                f"{kind}_confirm accepts only confirmation_id and idempotency_key"
            )
        initial = self.recipes.library_operation_snapshot(
            request.get("confirmation_id")
        )
        if initial.get("kind") != kind:
            raise RecipeLibraryError(
                f"{kind}_confirm requires a matching {kind}_prepare confirmation"
            )
        if initial["status"] in {"confirmed", "failed"}:
            terminal = self.recipes.confirm_library_lifecycle(
                initial["confirmation_id"],
                idempotency_key=request.get("idempotency_key"),
            )
            return self._lifecycle_operation_response(terminal)
        reference = initial["library_recipe_ref"]
        library_id = reference["library_id"]
        if library_id not in self.recipe_libraries:
            raise RecipeLibraryError(
                "lifecycle confirmation names an unconfigured recipe library"
            )
        with self._recipe_lifecycle_lock(reference):
            self._recover_recipe_library_operations()
            operation = self.recipes.confirm_library_lifecycle(
                initial["confirmation_id"],
                idempotency_key=request.get("idempotency_key"),
            )
            if operation["status"] in {"confirmed", "failed"}:
                return self._lifecycle_operation_response(operation)
            adapter = self.recipe_library_adapters.get(library_id)
            if adapter is None:
                response = self._lifecycle_operation_response(operation)
                response.update({
                    "error_code": "adapter_unavailable",
                    "error": "optional recipe library is unavailable before dispatch",
                })
                return response
            try:
                capabilities = self._library_capabilities(library_id)
            except Exception as exc:
                response = self._lifecycle_operation_response(operation)
                response.update({
                    "error_code": (
                        "needs_auth" if self._library_needs_auth(exc) else "unavailable"
                    ),
                    "error": (
                        "recipe library needs_auth before lifecycle dispatch"
                        if self._library_needs_auth(exc)
                        else "recipe library capability probe is unavailable"
                    ),
                })
                return response
            try:
                provider_principal, provider_binding = (
                    self._recipe_library_context(library_id, adapter)
                )
            except Exception as exc:
                response = self._lifecycle_operation_response(operation)
                response.update({
                    "error_code": (
                        "needs_auth" if self._library_needs_auth(exc) else "unavailable"
                    ),
                    "error": (
                        "recipe library needs_auth before lifecycle reconciliation"
                        if self._library_needs_auth(exc)
                        else "recipe library provider context is unavailable"
                    ),
                })
                return response
            if (
                operation["provider_principal"] != provider_principal
                or operation["provider_binding"] != provider_binding
            ):
                if operation["status"] == "pending" and operation.get(
                    "dispatched_at"
                ) is None:
                    operation = self.recipes.finish_library_lifecycle(
                        operation["operation_id"], "failed",
                        error_code="provider_context_changed",
                        error="recipe library provider context changed before dispatch",
                    )
                response = self._lifecycle_operation_response(operation)
                response.update({
                    "error_code": "provider_context_changed",
                    "error": "recipe library provider context changed; original outcome must be resolved there",
                })
                return response
            if operation.get("dispatched_at") is not None:
                if operation["status"] == "pending":
                    operation = self.recipes.finish_library_lifecycle(
                        operation["operation_id"], "uncertain",
                        error_code="uncertain",
                        error=f"{kind} may be in flight; reconcile before retry",
                    )
                return self._reconcile_external_lifecycle(
                    operation, adapter, capabilities
                )
            if operation["status"] == "uncertain":
                return self._reconcile_external_lifecycle(
                    operation, adapter, capabilities
                )
            capability = "delete" if kind == "delete" else "archive_desired_state"
            reconciliation = (
                "reconcile_delete" if kind == "delete" else "reconcile_archive"
            )
            if not capabilities[capability] or not capabilities[reconciliation]:
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code="unsupported",
                    error=f"recipe library no longer supports safely reconcilable {kind}",
                )
                return self._lifecycle_operation_response(failed)
            try:
                current, returned, current_archived = self._read_external_lifecycle(
                    adapter,
                    operation["library_recipe_ref"],
                    archive_state=kind == "archive",
                )
            except RecipeLibraryExternalMissingError:
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code="external_missing",
                    error="the exact external recipe disappeared before dispatch",
                )
                return self._lifecycle_operation_response(failed)
            except Exception as exc:
                response = self._lifecycle_operation_response(operation)
                if self._library_needs_auth(exc):
                    response.update({
                        "error_code": "needs_auth",
                        "error": "recipe library needs_auth before lifecycle dispatch",
                    })
                return response
            if (
                returned != operation["library_recipe_ref"]
                or self._recipe_lifecycle_digest(current)
                != operation["snapshot_digest"]
                or (
                    kind == "archive"
                    and current_archived is not operation["current_archived"]
                )
            ):
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code="conflict",
                    error="the external recipe changed after lifecycle prepare",
                )
                return self._lifecycle_operation_response(failed)
            if kind == "archive" and current_archived is operation["requested_archived"]:
                confirmed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "confirmed",
                    result={
                        "library_recipe_ref": returned,
                        "archived": current_archived,
                    },
                )
                return self._lifecycle_operation_response(confirmed)
            claimed = self.recipes.claim_library_dispatch(operation["operation_id"])
            if not claimed.get("claimed"):
                if claimed["status"] == "pending" and claimed.get(
                    "dispatched_at"
                ) is not None:
                    claimed = self.recipes.finish_library_lifecycle(
                        claimed["operation_id"], "uncertain",
                        error_code="uncertain",
                        error=f"{kind} may be in flight; reconcile before retry",
                    )
                if claimed["status"] == "uncertain":
                    return self._reconcile_external_lifecycle(
                        claimed, adapter, capabilities
                    )
                return self._lifecycle_operation_response(claimed)
            outbound = self._outbound_lifecycle_operation(claimed)
            mutation_returned = False
            try:
                if kind == "delete":
                    adapter.delete_recipe(deepcopy(returned), outbound)
                    mutation_returned = True
                    if adapter.reconcile_delete(deepcopy(returned), outbound) is not True:
                        raise RecipeLibraryUncertainError(
                            "recipe deletion has not reached authoritative absence"
                        )
                    result = {
                        "library_recipe_ref": returned,
                        "deleted": True,
                    }
                else:
                    raw = adapter.set_archive_state(
                        deepcopy(returned),
                        operation["requested_archived"],
                        outbound,
                    )
                    mutation_returned = True
                    if not isinstance(raw, Mapping) or raw.get("archived") is not operation[
                        "requested_archived"
                    ]:
                        raise RecipeLibraryError(
                            "recipe library archive response is incompatible"
                        )
                    result_reference = validate_library_recipe_ref(
                        raw.get("library_recipe_ref")
                    )
                    if (
                        result_reference["library_id"] != library_id
                        or result_reference["recipe_id"] != reference["recipe_id"]
                        or "version" not in result_reference
                    ):
                        raise RecipeLibraryError(
                            "recipe library archive response changed identity"
                        )
                    observed = adapter.reconcile_archive(
                        deepcopy(result_reference),
                        operation["requested_archived"],
                        outbound,
                    )
                    if (
                        not isinstance(observed, Mapping)
                        or observed.get("archived")
                        is not operation["requested_archived"]
                    ):
                        raise RecipeLibraryUncertainError(
                            "recipe archive desired state is not authoritative"
                        )
                    observed_reference = validate_library_recipe_ref(
                        observed.get("library_recipe_ref")
                    )
                    if (
                        observed_reference["library_id"] != library_id
                        or observed_reference["recipe_id"] != reference["recipe_id"]
                        or "version" not in observed_reference
                    ):
                        raise RecipeLibraryUncertainError(
                            "recipe archive reconciliation changed identity"
                        )
                    result = {
                        "library_recipe_ref": observed_reference,
                        "archived": operation["requested_archived"],
                    }
                confirmed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "confirmed", result=result
                )
                return self._lifecycle_operation_response(confirmed)
            except RecipeLibraryExternalMissingError:
                if mutation_returned:
                    uncertain = self.recipes.finish_library_lifecycle(
                        operation["operation_id"], "uncertain",
                        error_code="uncertain",
                        error=f"{kind} may have succeeded; authoritative readback failed",
                    )
                    return self._lifecycle_operation_response(uncertain)
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code="external_missing",
                    error="the exact external recipe is missing",
                )
                return self._lifecycle_operation_response(failed)
            except RecipeLibraryDefiniteError as exc:
                if mutation_returned:
                    uncertain = self.recipes.finish_library_lifecycle(
                        operation["operation_id"], "uncertain",
                        error_code="uncertain",
                        error=f"{kind} may have succeeded; authoritative readback failed",
                    )
                    response = self._lifecycle_operation_response(uncertain)
                    if self._library_needs_auth(exc):
                        response.update({
                            "error_code": "needs_auth",
                            "error": f"recipe library needs_auth before {kind} reconciliation",
                        })
                    return response
                failed = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "failed",
                    error_code=(
                        "needs_auth" if self._library_needs_auth(exc)
                        else "provider_rejected"
                    ),
                    error=(
                        "recipe library needs_auth"
                        if self._library_needs_auth(exc)
                        else f"recipe library rejected {kind}"
                    ),
                )
                return self._lifecycle_operation_response(failed)
            except Exception as exc:
                uncertain = self.recipes.finish_library_lifecycle(
                    operation["operation_id"], "uncertain",
                    error_code="uncertain",
                    error=f"{kind} may have been dispatched; reconcile before retry",
                )
                response = self._lifecycle_operation_response(uncertain)
                if self._library_needs_auth(exc):
                    response.update({
                        "error_code": "needs_auth",
                        "error": f"recipe library needs_auth before {kind} reconciliation",
                    })
                return response

    def _recipes(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "search")
        if action == "discover":
            return self._discover_recipes(request)
        if action == "libraries":
            libraries = []
            for library_id, connection in self.recipe_libraries.items():
                item = {
                    key: deepcopy(connection[key])
                    for key in ("library_id", "provider", "display_name", "read_only")
                    if key in connection
                }
                item["primary"] = library_id == self.primary_recipe_library_id
                try:
                    item["capabilities"] = self._library_capabilities(library_id)
                    item["status"] = "available"
                except Exception as exc:
                    item["capabilities"] = None
                    item["status"] = (
                        "needs_auth"
                        if self._library_needs_auth(exc)
                        else "unavailable"
                    )
                libraries.append(item)
            return {"primary_recipe_library_id": self.primary_recipe_library_id, "recipe_libraries": libraries}
        if action == "search":
            requested_ids = request.get("library_ids")
            if requested_ids is not None:
                if request.get("library_id") is not None or not isinstance(requested_ids, list) or not 1 <= len(requested_ids) <= 20:
                    raise RecipeLibraryError("cross-library search requires one to 20 exact library_ids and no library_id")
                library_ids = [validate_library_id(item) for item in requested_ids]
                if len(library_ids) != len(set(library_ids)):
                    raise RecipeLibraryError("cross-library search library_ids must be unique")
            else:
                selected = self.primary_recipe_library_id if request.get("library_id") is None else request.get("library_id")
                library_ids = [validate_library_id(selected)]
            if any(item not in self.recipe_libraries for item in library_ids):
                raise RecipeLibraryError("library_id must name one exact configured recipe library")
            favorites_only = request.get("favorites_only", False)
            if not isinstance(favorites_only, bool):
                raise RecipeLibraryError("favorites_only must be true or false")
            if requested_ids is None and library_ids == ["builtin"] and request.get("cursor") is not None:
                raise RecipeLibraryError("built-in recipe search has no continuation cursor")
            if requested_ids is not None or library_ids != ["builtin"]:
                query = self._provider_text(request.get("query", ""), "recipe library query", 200) or ""
                filters = request.get("filters")
                if filters is None:
                    filters = {}
                if not isinstance(filters, Mapping) or len(filters) > 20:
                    raise RecipeLibraryError("recipe library filters must be a bounded object")
                try:
                    encoded_filters = json.dumps(
                        filters, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                    ).encode("utf-8")
                except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                    raise RecipeLibraryError("recipe library filters must be JSON data") from exc
                if len(encoded_filters) > 16 * 1024:
                    raise RecipeLibraryError("recipe library filters are too large")
                raw_cursor = request.get("cursor")
                cursor_by_library: dict[str, str | None] = {}
                if requested_ids is not None:
                    if raw_cursor is not None:
                        if not isinstance(raw_cursor, Mapping) or len(raw_cursor) > len(library_ids):
                            raise RecipeLibraryError("cross-library cursor must map exact library_ids to cursors")
                        if any(key not in library_ids for key in raw_cursor):
                            raise RecipeLibraryError("cross-library cursor names an unselected library_id")
                        for key, value in raw_cursor.items():
                            cursor_by_library[key] = (
                                None if value is None else
                                self._provider_text(value, "recipe library cursor", 1_024, required=True)
                            )
                else:
                    if isinstance(raw_cursor, Mapping):
                        raise RecipeLibraryError("single-library cursor must be exact text")
                    cursor_by_library[library_ids[0]] = (
                        None if raw_cursor is None else
                        self._provider_text(raw_cursor, "recipe library cursor", 1_024, required=True)
                    )
                if cursor_by_library.get("builtin") is not None:
                    raise RecipeLibraryError("built-in recipe search has no continuation cursor")
                limit = bounded_limit(request.get("limit"), default=10, maximum=50)
                combined = []
                cursors: dict[str, str | None] = {}
                errors: dict[str, str] = {}
                for library_id in library_ids:
                    try:
                        if library_id == "builtin":
                            state = self.store.read()
                            week = validate_week(request.get("week") or self._household_today(state).strftime("%G-W%V"))
                            include_ineligible = request.get("include_ineligible") is True
                            offset = 0
                            page_limit = limit
                            accepted = []
                            while True:
                                rows = self.recipes.search(
                                    query, limit=page_limit, offset=offset,
                                    include_archived=request.get("include_archived") is True,
                                    favorites_only=favorites_only,
                                )
                                offset += len(rows)
                                for row in rows:
                                    summary = self._usage_summary(
                                        state, row["recipe_key"], week
                                    )
                                    if not include_ineligible and not summary["eligible"]:
                                        continue
                                    item = {
                                        key: deepcopy(row.get(key))
                                        for key in (
                                            "id", "revision", "status", "name", "language", "tags",
                                            "source", "rights", "portions", "library_id", "is_favorite",
                                            "favorite_revision",
                                        )
                                    }
                                    item["library_recipe_ref"] = deepcopy(row["library_recipe_ref"])
                                    item["recipe_key"] = library_recipe_key(row["library_recipe_ref"])
                                    item["usage"] = summary
                                    accepted.append(item)
                                    if len(accepted) == limit:
                                        break
                                if (
                                    len(accepted) == limit
                                    or len(rows) < page_limit
                                    or include_ineligible
                                ):
                                    break
                                page_limit = 50
                            combined.extend(accepted)
                            cursors[library_id] = None
                            continue
                        capabilities = self._library_capabilities(library_id)
                        if not capabilities["search"]:
                            raise RecipeLibraryError("recipe library search is unsupported")
                        if favorites_only and not capabilities["favorite_read"]:
                            raise RecipeLibraryError(
                                "favorites_only is unsupported for this recipe library"
                            )
                        provider_filters = dict(filters)
                        if favorites_only:
                            provider_filters["favorites_only"] = True
                        state = self.store.read()
                        week = validate_week(
                            request.get("week")
                            or self._household_today(state).strftime("%G-W%V")
                        )
                        provider_cursor, provider_limit, provider_skip = (
                            self._decode_library_search_cursor(
                                cursor_by_library.get(library_id), library_id, limit
                            )
                        )
                        seen_positions = set()
                        accepted = []
                        result_cursor = None
                        completed = False
                        page_budget = (
                            MAX_EXTERNAL_FAVORITE_SEARCH_PAGES
                            if favorites_only else 1
                        )
                        for _page_number in range(page_budget):
                            position = (provider_cursor, provider_skip)
                            if position in seen_positions:
                                raise RecipeLibraryError(
                                    "recipe library search returned a repeated cursor"
                                )
                            seen_positions.add(position)
                            page_cursor = provider_cursor
                            page = self.recipe_library_adapters[library_id].search(
                                query, provider_filters, page_cursor, provider_limit
                            )
                            if (
                                not isinstance(page, Mapping)
                                or not isinstance(page.get("recipes"), list)
                                or len(page["recipes"]) > provider_limit
                                or provider_skip > len(page["recipes"])
                            ):
                                raise RecipeLibraryError(
                                    "recipe library search returned invalid data"
                                )
                            next_cursor = page.get("cursor")
                            if next_cursor is not None:
                                next_cursor = self._provider_text(
                                    next_cursor,
                                    "recipe library provider cursor",
                                    500,
                                    required=True,
                                )
                                if next_cursor == page_cursor:
                                    raise RecipeLibraryError(
                                        "recipe library search returned a repeated cursor"
                                    )
                            for raw_index in range(provider_skip, len(page["recipes"])):
                                raw_item = page["recipes"][raw_index]
                                item = self._normalize_library_search_item(
                                    raw_item,
                                    library_id,
                                    favorite_read=capabilities["favorite_read"],
                                )
                                item["usage"] = self._usage_summary(
                                    state, item["recipe_key"], week
                                )
                                if (
                                    request.get("include_ineligible") is not True
                                    and not item["usage"]["eligible"]
                                ):
                                    continue
                                accepted.append(item)
                                if len(accepted) == limit:
                                    consumed = raw_index + 1
                                    result_cursor = self._encode_library_search_cursor(
                                        library_id,
                                        page_cursor if consumed < len(page["recipes"]) else next_cursor,
                                        provider_limit,
                                        consumed if consumed < len(page["recipes"]) else 0,
                                    )
                                    completed = True
                                    break
                            if completed:
                                break
                            if next_cursor is None:
                                completed = True
                                result_cursor = None
                                break
                            provider_cursor = next_cursor
                            provider_skip = 0
                        if not completed:
                            result_cursor = self._encode_library_search_cursor(
                                library_id, provider_cursor, provider_limit
                            )
                        cursors[library_id] = result_cursor
                        combined.extend(accepted)
                    except Exception as exc:
                        if len(library_ids) == 1:
                            if self._library_needs_auth(exc):
                                raise RecipeLibraryError(
                                    "recipe library needs_auth"
                                ) from None
                            raise RecipeLibraryError("recipe library search is unavailable")
                        errors[library_id] = (
                            "recipe library needs_auth"
                            if self._library_needs_auth(exc)
                            else "recipe library search is unavailable"
                        )
                result = {"recipes": combined, "library_ids": library_ids, "cursors": cursors}
                if errors:
                    result["errors"] = errors
                return result
            state = self.store.read()
            week = validate_week(request.get("week") or self._household_today(state).strftime("%G-W%V"))
            results = []
            requested_limit = request.get("limit", 10)
            include_ineligible = request.get("include_ineligible") is True
            offset = 0
            page_limit = requested_limit
            while True:
                rows = self.recipes.search(
                    request.get("query", ""), limit=page_limit,
                    include_archived=request.get("include_archived") is True,
                    favorites_only=favorites_only, offset=offset,
                )
                offset += len(rows)
                for row in rows:
                    summary = self._usage_summary(state, row["recipe_key"], week)
                    value = {
                        key: deepcopy(row.get(key))
                        for key in (
                            "id", "revision", "status", "name", "language", "tags", "source", "rights",
                            "portions", "created_at", "updated_at", "created_via", "content_fingerprint", "recipe_key",
                            "library_id", "is_favorite", "favorite_revision",
                        )
                    }
                    value["library_recipe_ref"] = deepcopy(row["library_recipe_ref"])
                    value["recipe_key"] = library_recipe_key(row["library_recipe_ref"])
                    value["usage"] = summary
                    if include_ineligible or summary["eligible"]:
                        results.append(value)
                        if len(results) == requested_limit:
                            break
                if len(results) == requested_limit or len(rows) < page_limit or include_ineligible:
                    break
                page_limit = 50
            return {"week": week, "library_id": "builtin", "recipes": results}
        if action == "get":
            supplied_reference = request.get("library_recipe_ref")
            if supplied_reference is None:
                library_id = self.primary_recipe_library_id if request.get("library_id") is None else request.get("library_id")
                library_id = validate_library_id(library_id)
                if library_id != "builtin":
                    raise RecipeLibraryError("external recipe get requires one exact library_recipe_ref")
                recipe = self.recipes.get(request.get("recipe_id"), request.get("revision"))
                recipe["recipe_key"] = library_recipe_key(recipe["library_recipe_ref"])
            else:
                reference = validate_library_recipe_ref(supplied_reference)
                if request.get("library_id") is not None and request["library_id"] != reference["library_id"]:
                    raise RecipeLibraryError("library_recipe_ref does not match library_id")
                if reference["library_id"] not in self.recipe_libraries:
                    raise RecipeLibraryError("library_recipe_ref names an unconfigured recipe library")
                if reference["library_id"] == "builtin":
                    recipe = self.recipes.get(reference["recipe_id"], reference.get("version"))
                    recipe["recipe_key"] = library_recipe_key(recipe["library_recipe_ref"])
                else:
                    recipe = self._external_library_get(reference)
            result = scale_recipe(recipe, request.get("portions")) if recipe["rights"]["storage"] == "full" else recipe
            if result.get("library_recipe_ref") is not None:
                result["recipe_key"] = library_recipe_key(result["library_recipe_ref"])
            if request.get("week"):
                result["usage"] = self._usage_summary(self.store.read(), result["recipe_key"], validate_week(request["week"]))
            return {"recipe": result}
        if action == "list_labels":
            if request.get("library_id") is None:
                raise RecipeLibraryError(
                    "list_labels requires one exact external library_id"
                )
            library_id = validate_library_id(
                request.get("library_id"), allow_builtin=False
            )
            if library_id not in self.recipe_libraries:
                raise RecipeLibraryError(
                    "library_id must name one exact configured external recipe library"
                )
            capabilities = self._library_capabilities(library_id)
            if not capabilities["label_read"]:
                raise RecipeLibraryError(
                    "this recipe library does not support native label reads"
                )
            labels = self._read_external_labels(
                self.recipe_library_adapters[library_id], library_id
            )
            return {"library_id": library_id, "labels": labels}
        if action == "get_labels":
            reference = validate_library_recipe_ref(
                request.get("library_recipe_ref")
            )
            if request.get("library_id") is not None:
                raise RecipeLibraryError(
                    "get_labels requires only one exact library_recipe_ref identity"
                )
            if (
                reference["library_id"] == "builtin"
                or reference["library_id"] not in self.recipe_libraries
            ):
                raise RecipeLibraryError(
                    "get_labels requires one configured external library_recipe_ref"
                )
            capabilities = self._library_capabilities(reference["library_id"])
            if not capabilities["label_read"]:
                raise RecipeLibraryError(
                    "this recipe library does not support native label reads"
                )
            labels = self._read_external_recipe_labels(
                self.recipe_library_adapters[reference["library_id"]], reference
            )
            return {
                "library_id": reference["library_id"],
                "library_recipe_ref": reference,
                "labels": labels,
            }
        if action == "set_label":
            if request.get("library_id") is not None:
                raise RecipeLibraryError(
                    "set_label requires exact recipe and label refs, not library_id"
                )
            return self._set_external_label(
                request.get("library_recipe_ref"),
                request.get("library_label_ref"),
                request.get("present"),
                expected_label_revision=request.get("expected_label_revision"),
                idempotency_key=request.get("idempotency_key"),
            )
        if action == "create_label":
            return self._create_external_label(
                request.get("library_id"),
                request.get("label_name"),
                idempotency_key=request.get("idempotency_key"),
            )
        if action == "set_favorite":
            reference = validate_library_recipe_ref(request.get("library_recipe_ref"))
            if request.get("recipe_id") is not None:
                raise RecipeLibraryError("set_favorite requires only one exact library_recipe_ref identity")
            if request.get("library_id") is not None and request["library_id"] != reference["library_id"]:
                raise RecipeLibraryError("library_recipe_ref does not match library_id")
            if reference["library_id"] == "builtin":
                return self.recipes.set_favorite(
                    reference,
                    request.get("is_favorite"),
                    expected_favorite_revision=request.get("expected_favorite_revision"),
                    idempotency_key=request.get("idempotency_key"),
                )
            return self._set_external_favorite(
                reference,
                request.get("is_favorite"),
                expected_favorite_revision=request.get(
                    "expected_favorite_revision"
                ),
                idempotency_key=request.get("idempotency_key"),
            )
        if action == "resolve":
            return self.recipes.resolve_discovery(request.get("discovery_ref"))
        if action == "save":
            has_recipe = request.get("recipe") is not None
            has_ref = request.get("discovery_ref") is not None
            if has_recipe == has_ref:
                raise HouseholdError("recipes save requires exactly one of recipe or discovery_ref")
            key = request.get("idempotency_key")
            status = str(request.get("status") or "active")
            if has_ref:
                return self._save_discovery_to_library(request)
            target = self.primary_recipe_library_id if request.get("library_id") is None else request.get("library_id")
            if target != "builtin":
                raise RecipeLibraryError("external recipe create requires an exact discovery_ref")
            value = normalize_recipe(request.get("recipe"))
            return {
                "saved": True,
                "library_id": "builtin",
                "recipe": self.recipes.save(value, status=status, idempotency_key=key),
            }
        if action == "archive_prepare":
            return self._prepare_external_lifecycle(request, "archive")
        if action == "delete_prepare":
            return self._prepare_external_lifecycle(request, "delete")
        if action == "archive_confirm":
            return self._confirm_external_lifecycle(request, "archive")
        if action == "delete_confirm":
            return self._confirm_external_lifecycle(request, "delete")
        if action == "update":
            if request.get("library_recipe_ref") is not None:
                return self._update_external_recipe(request)
            if request.get("library_id") not in {None, "builtin"}:
                raise RecipeLibraryError(
                    "external update requires one exact library_recipe_ref"
                )
            value = normalize_recipe(request.get("recipe"))
            recipe_id = str(request.get("recipe_id") or "")
            expected = request.get("expected_revision")
            key = request.get("idempotency_key")
            return {"recipe": self.recipes.update(recipe_id, expected, value, status=request.get("status"), idempotency_key=key)}
        if action == "archive":
            if request.get("library_id") not in {None, "builtin"}:
                raise RecipeLibraryError("external recipe lifecycle is not implemented")
            recipe_id = str(request.get("recipe_id") or "")
            expected = request.get("expected_revision")
            key = request.get("idempotency_key")
            return {"recipe": self.recipes.archive(recipe_id, expected, idempotency_key=key)}
        if action in {"mark_cooked", "mark_not_cooked"}:
            week = validate_week(request.get("week"))
            recipe_identity = str(request.get("recipe_key") or "")
            if request.get("recipe_id"):
                stored_identity = self.recipes.get(request.get("recipe_id"))["recipe_key"]
                if recipe_identity and recipe_identity not in library_recipe_key_aliases(stored_identity):
                    raise HouseholdError("recipe_id and recipe_key refer to different recipes")
                recipe_identity = recipe_identity or stored_identity
            if not recipe_identity or len(recipe_identity) > MAX_LIBRARY_RECIPE_KEY:
                raise HouseholdError("recipe_key or recipe_id is required")
            menu_id = str(request.get("menu_id") or "") or None
            supplied_request_key = request.get("idempotency_key")
            if supplied_request_key is not None and (not isinstance(supplied_request_key, str) or not 1 <= len(supplied_request_key.strip()) <= 200):
                raise HouseholdError("idempotency_key must be one to 200 characters")
            request_key = supplied_request_key.strip() if supplied_request_key else None
            digest = canonical({
                "action": action,
                "menu_id": menu_id,
                "recipe_key": self._canonical_usage_key(recipe_identity),
                "week": week,
            })
            with self.store.locked() as state:
                if request_key:
                    if existing := self._usage_request(state, request_key, digest):
                        return existing
                records = state.setdefault("recipe_usage", {})
                if menu_id:
                    record = records.get(menu_id)
                    matched = (
                        self._matching_recipe_key(recipe_identity, record.get("recipe_keys"))
                        if isinstance(record, dict) and record.get("week") == week else None
                    )
                    if matched is None:
                        raise HouseholdError("menu usage record does not contain this recipe and week")
                    recipe_identity = matched
                else:
                    candidates = [
                        (candidate_id, value, self._matching_recipe_key(recipe_identity, value.get("recipe_keys")))
                        for candidate_id, value in records.items()
                        if isinstance(value, dict)
                        and value.get("week") == week
                        and self._matching_recipe_key(recipe_identity, value.get("recipe_keys")) is not None
                        and value.get("status") in {"planned", "ordered", "manual"}
                    ]
                    if len(candidates) > 1:
                        raise HouseholdError("multiple menu usage records match; menu_id is required")
                    if candidates:
                        menu_id, record, recipe_identity = candidates[0]
                    elif action == "mark_cooked":
                        menu_id = f"manual_{secrets.token_hex(10)}"
                        record = records[menu_id] = {"week": week, "status": "manual", "recipe_keys": [recipe_identity], "cooked_keys": [], "not_cooked_keys": [], "cooldown_overrides": {}, "order_id": None}
                    else:
                        raise HouseholdError("mark_not_cooked requires a matching planned or ordered menu")
                cooked = record.setdefault("cooked_keys", [])
                not_cooked = record.setdefault("not_cooked_keys", [])
                if action == "mark_cooked":
                    if recipe_identity not in cooked:
                        cooked.append(recipe_identity)
                    not_cooked[:] = [key for key in not_cooked if key != recipe_identity]
                else:
                    if recipe_identity not in not_cooked:
                        not_cooked.append(recipe_identity)
                    cooked[:] = [key for key in cooked if key != recipe_identity]
                result = {"menu_id": menu_id, "recipe_key": recipe_identity, "week": week, "cooked": action == "mark_cooked", "usage": self._usage_summary(state, recipe_identity, week)}
                if request_key:
                    self._store_usage_request(state, request_key, digest, result)
                return result
        raise HouseholdError("unknown recipe action")

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
                    recipe = scale_recipe(stored, raw.get("portions"))
                    recipe["library_recipe_ref"] = deepcopy(stored["library_recipe_ref"])
                    recipe["recipe_key"] = library_recipe_key(recipe["library_recipe_ref"])
                elif isinstance(reference, Mapping):
                    stored = self.recipes.get(reference.get("id"), reference.get("revision"))
                    if stored.get("status") != "active" or stored.get("revision_status", stored.get("status")) != "active":
                        raise HouseholdError("only active recipes can be added to a new menu")
                    for field in ("library_id", "is_favorite", "favorite_revision"):
                        stored.pop(field, None)
                    recipe = scale_recipe(stored, raw.get("portions"))
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
        refreshed = []
        for candidate in resolved:
            item = deepcopy(dict(candidate))
            item["usage"] = self._usage_summary(
                state, item["recipe_key"], request["week"]
            )
            refreshed.append(item)
        profile = state.get("profile")
        if not isinstance(profile, Mapping):
            raise PlannerError("household profile is invalid")
        return plan_week(
            request, profile=profile, candidates=refreshed, history=history
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
        if action == "get":
            return {"menu": deepcopy(self.store.read().get("menu"))}
        if action == "plan":
            setup_gate = self._setup_gate(request)
            if setup_gate is not None:
                return setup_gate
            result, _resolved, _planner_request = self._plan_menu(
                request.get("planner_input")
            )
            return {"plan": result}
        if action == "clear":
            with self.store.locked() as state:
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
            with self.store.locked() as state:
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
                    summary = self._usage_summary(state, key, menu["week"], ignore_menu_id=ignored_menu_id)
                    if not summary["eligible"] and matched_override(key) is None:
                        blocked.append({"recipe_key": key, "usage": summary})
                if blocked:
                    raise HouseholdError(f"recipe cooldown blocks this menu: {canonical(blocked)}")
                if isinstance(current, Mapping):
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
                    "order_id": None, "updated_at": now().isoformat(),
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
            return self.oda.call("product_search", {"queries": [query], "page": 1, "size": bounded_limit(request.get("limit"), default=5)}, **kwargs)
        if action == "recipes":
            query = request.get("query", "")
            if not isinstance(query, str):
                raise HouseholdError("catalog query must be text")
            return self.oda.call("recipe_search", {"query": query, "page": 1, "size": bounded_limit(request.get("limit"), default=5)}, **kwargs)
        if action == "usuals":
            return self.oda.call("likely_to_buy", {}, **kwargs)
        raise HouseholdError("unknown catalog action")

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

    def _cart_requirements(self, value: Any) -> tuple[dict[str, int], dict[str, str]]:
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
            "updated_at": now().isoformat(),
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
        plan["updated_at"] = now().isoformat()

    def _cart_sync(self, request: Mapping[str, Any], deadline: float | None) -> dict[str, Any]:
        requirements, requirement_names = self._cart_requirements(request.get("requirements"))
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
                    current["updated_at"] = now().isoformat()
                    approved_idempotent = True
                elif current.get("last_synced_digest") != first_digest:
                    self._set_cart_needs_input(current, first_live, first_names)
                elif requirements_changed:
                    current["approved_cart_digest"] = None
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
        if operations:
            prewrite_cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
            prewrite_summary = cart_summary(prewrite_cart)
            prewrite_live, prewrite_names = self._cart_lines(prewrite_summary)
            if self._cart_digest(prewrite_live) != self._cart_digest(first_live):
                with self.store.locked() as state:
                    plan = state["cart_plan"]
                    self._set_cart_needs_input(plan, prewrite_live, prewrite_names)
                    current = deepcopy(plan)
                return {"synced": False, **self._cart_question(current, prewrite_summary, reason="cart_changed_immediately_before_sync")}
            try:
                self.oda.call("manipulate_cart", {"operations": operations}, deadline=deadline) if self.provider == "meny" else self.oda.call("manipulate_cart", {"operations": operations})
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
            plan["updated_at"] = now().isoformat()
            current = deepcopy(plan)
        return {
            "synced": True,
            "idempotent": not operations,
            "applied_operations": operations,
            "cart": verified_cart,
            "cart_plan": self._cart_plan_view(current, verified_summary),
        }

    def _cart_reconcile(self, request: Mapping[str, Any], deadline: float | None) -> dict[str, Any]:
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
        if not isinstance(plan, dict) or plan.get("status") != "needs_input" or plan.get("pending_cart_digest") != supplied_digest:
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
                self.oda.call("manipulate_cart", {"operations": operations}, deadline=deadline) if self.provider == "meny" else self.oda.call("manipulate_cart", {"operations": operations})
            except HouseholdError as exc:
                mutation_error = exc
        verified_cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {})
        verified_summary = cart_summary(verified_cart)
        verified_live, verified_names = self._cart_lines(verified_summary)
        if mutation_error is not None or verified_live != target:
            with self.store.locked() as state:
                current = state["cart_plan"]
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
            current["updated_at"] = now().isoformat()
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
                plan["updated_at"] = now().isoformat()
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
                    plan["updated_at"] = now().isoformat()
                    return None
                self._set_cart_needs_input(plan, live, names)
                result = deepcopy(plan)
                reason = "cart_requires_owner_decision"
        return self._cart_question(result, summary, reason=reason)

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

    @staticmethod
    def _delivery_scope(state: Mapping[str, Any], cart: Mapping[str, Any] | None = None) -> dict[str, str | None]:
        raw_cart_id = None
        if isinstance(cart, Mapping):
            raw_cart_id = cart.get("id", cart.get("cartId", cart.get("cart_id")))
        order_change = state.get("order_change")
        pending = state.get("pending_checkout")
        return {
            "cart_id": str(raw_cart_id)[:128] if raw_cart_id not in {None, ""} else None,
            "order_id": str(order_change.get("order_id"))[:128] if isinstance(order_change, Mapping) and order_change.get("order_id") else None,
            "occurrence": str(pending.get("occurrence"))[:128] if isinstance(pending, Mapping) and pending.get("occurrence") else None,
        }

    def _record_delivery_selection(
        self,
        slot: Mapping[str, Any],
        *,
        origin: str,
        candidate_digest: str | None,
        baseline: Mapping[str, Any],
        cart: Mapping[str, Any] | None = None,
        occurrence: str | None = None,
    ) -> None:
        normalized = validate_delivery_slot(slot)
        if origin not in {"explicit", "cheapest"}:
            raise HouseholdError("delivery selection origin is invalid")
        if candidate_digest is not None and (
            not isinstance(candidate_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", candidate_digest) is None
        ):
            raise HouseholdError("delivery candidate digest is invalid")
        observation = {
            "provider": self.provider,
            "scope": {
                **self._delivery_scope(baseline, cart),
                **({"occurrence": occurrence} if occurrence else {}),
            },
            "origin": origin,
            "slot": normalized,
            "candidate_digest": candidate_digest,
            "observed_at": now().isoformat(),
        }
        with self.store.locked() as state:
            if canonical(state.get("order_change")) != canonical(baseline.get("order_change")):
                raise HouseholdError("order change state changed while recording delivery")
            state["delivery_selection"] = observation

    def _delivery_observation_applies(
        self,
        observation: Any,
        state: Mapping[str, Any],
        *,
        cart: Mapping[str, Any] | None,
        occurrence: str | None,
    ) -> bool:
        if not isinstance(observation, Mapping) or observation.get("provider") != self.provider:
            return False
        scope = observation.get("scope")
        if not isinstance(scope, Mapping):
            return False
        current = self._delivery_scope(state, cart)
        if scope.get("cart_id") != current["cart_id"] or scope.get("order_id") != current["order_id"]:
            return False
        return scope.get("occurrence") in {None, occurrence}

    @staticmethod
    def _same_delivery_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        keys = ("slot_ref", "provider_slot_id", "start_at", "end_at")
        return all(left.get(key) == right.get(key) for key in keys)

    @staticmethod
    def _delivery_slot_date(slot: Mapping[str, Any]) -> str:
        normalized = validate_delivery_slot(slot)
        return datetime.fromisoformat(
            normalized["start_at"].replace("Z", "+00:00")
        ).astimezone(ZoneInfo("Europe/Oslo")).date().isoformat()

    def _normalized_provider_slots(
        self,
        dates: list[str] | None = None,
        *,
        deadline: float | None = None,
        address_id: Any = None,
        allow_recovery: bool = False,
        display_metadata: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        calls = dates or [None]
        slots: list[dict[str, Any]] = []
        for delivery_date in calls:
            arguments = {"delivery_date": delivery_date} if delivery_date else {}
            if address_id is not None:
                arguments["delivery_address_id"] = address_id
            if self.provider == "meny":
                result = self.oda.call(
                    "get_delivery_slots", arguments, deadline=deadline, allow_recovery=allow_recovery,
                )
            else:
                result = self.oda.call("get_delivery_slots", arguments, deadline=deadline)
            raw_slots = result.get("slots") if isinstance(result, Mapping) else None
            if not isinstance(raw_slots, list):
                raise HouseholdError(f"{self.provider.upper()} delivery slots are not normalized")
            normalized_call = [validate_delivery_slot(slot) for slot in raw_slots]
            if display_metadata is not None:
                raw_display = result.get("display") if isinstance(result, Mapping) else None
                if self.provider == "meny" and (
                    not isinstance(raw_display, Mapping)
                    or set(raw_display) != {slot["slot_ref"] for slot in normalized_call}
                ):
                    raise HouseholdError("MENY delivery display metadata is unavailable")
                if isinstance(raw_display, Mapping):
                    for slot in normalized_call:
                        reference = slot["slot_ref"]
                        label = raw_display.get(reference)
                        if (
                            not isinstance(label, str)
                            or not label.strip()
                            or len(label.encode("utf-8")) > 500
                        ):
                            raise HouseholdError("provider delivery display metadata is invalid")
                        normalized_label = " ".join(label.split())
                        previous = display_metadata.get(reference)
                        if previous is not None and previous != normalized_label:
                            raise HouseholdError("provider returned conflicting delivery display metadata")
                        display_metadata[reference] = normalized_label
            slots.extend(normalized_call)
        unique: dict[str, dict[str, Any]] = {}
        for slot in slots:
            reference = slot["slot_ref"]
            if reference in unique and canonical(unique[reference]) != canonical(slot):
                raise HouseholdError("provider returned conflicting delivery slot references")
            unique[reference] = slot
        return list(unique.values())

    @staticmethod
    def _scheduled_delivery_dates(schedule: Mapping[str, Any], instant: datetime) -> list[str]:
        preference = schedule["delivery"]
        local_day = instant.astimezone(ZoneInfo(str(schedule["timezone"]))).date()
        weekday = str(preference.get("weekday") or "").casefold()
        if weekday:
            offset = (SCHEDULE_WEEKDAYS[weekday] - local_day.weekday()) % 7
            return [(local_day + timedelta(days=offset)).isoformat()]
        return [(local_day + timedelta(days=offset)).isoformat() for offset in range(7)]

    def _scheduled_delivery_choice(
        self,
        schedule: Mapping[str, Any],
        *,
        occurrence: str,
        deadline: float | None,
    ) -> dict[str, Any]:
        preference = schedule["delivery"]
        state = self.store.read()
        scope_cart = (
            self.oda.call("get_cart", {}, deadline=deadline)
            if self.provider == "meny"
            else self.oda.call("get_cart", {}, deadline=deadline)
        )
        observation = state.get("delivery_selection")
        applicable = self._delivery_observation_applies(
            observation, state, cart=scope_cart, occurrence=occurrence,
        )
        cart_delivery = cart_summary(scope_cart).get("delivery") if self.provider == "oda" else None
        current_dates = None
        if applicable and isinstance(observation.get("slot"), Mapping):
            current_dates = [self._delivery_slot_date(observation["slot"])]
        elif isinstance(cart_delivery, Mapping):
            oda_today = now().astimezone(ZoneInfo("Europe/Oslo")).date()
            current_dates = [oda_cart_delivery_window(cart_delivery, today=oda_today)["date"]]
        current_slots = self._normalized_provider_slots(current_dates, deadline=deadline)
        selected = [slot for slot in current_slots if slot["selected"]]
        if len(selected) > 1:
            raise HouseholdError("provider-selected delivery is ambiguous")
        selected_slot = selected[0] if selected else None
        if self.provider == "oda":
            if isinstance(cart_delivery, Mapping):
                if (
                    selected_slot is None
                    or not oda_cart_delivery_matches_slot(cart_delivery, selected_slot)
                ):
                    raise HouseholdError("Oda cart and selected delivery listing disagree")
            elif selected_slot is not None:
                raise HouseholdError("Oda cart and selected delivery listing disagree")
        if selected_slot is not None and not applicable:
            return {
                "ready": True,
                "origin": "external",
                "selected": selected_slot,
                "price_display": delivery_price_display(selected_slot),
            }
        if applicable:
            observed_slot = observation.get("slot")
            if (
                selected_slot is None
                or not isinstance(observed_slot, Mapping)
                or not self._same_delivery_identity(
                    selected_slot, validate_delivery_slot(observed_slot),
                )
            ):
                raise HouseholdError("provider delivery and local selection provenance disagree")
            if observation.get("origin") == "explicit" or preference["strategy"] == "keep_selected":
                self._record_delivery_selection(
                    selected_slot,
                    origin=str(observation["origin"]),
                    candidate_digest=observation.get("candidate_digest"),
                    baseline=state,
                    cart=scope_cart,
                    occurrence=occurrence,
                )
                return {
                    "ready": True,
                    "origin": observation["origin"],
                    "selected": selected_slot,
                    "price_display": delivery_price_display(selected_slot),
                }
        elif preference["strategy"] == "keep_selected":
            return {"ready": False, "reason": "select a delivery slot", "candidates": []}

        dates = self._scheduled_delivery_dates(schedule, now())
        candidates = [
            slot for slot in self._normalized_provider_slots(dates, deadline=deadline)
            if delivery_matches(
                preference, slot, timezone_name=str(schedule["timezone"]),
            )
        ]
        rendered = [
            {"slot": slot, "price_display": delivery_price_display(slot)}
            for slot in candidates
        ]
        if not candidates:
            return {"ready": False, "reason": "no delivery slot satisfies the hard constraints", "candidates": []}
        if any(slot["price_kind"] != "exact" for slot in candidates):
            return {
                "ready": False,
                "reason": "eligible delivery prices are not all exact",
                "candidates": rendered,
            }
        digest = delivery_candidate_digest(candidates)
        winner = cheapest_delivery_slot(
            candidates,
            preferred_end=preference.get("preferred_end"),
            timezone_name=str(schedule["timezone"]),
        )
        if selected_slot is not None and self._same_delivery_identity(selected_slot, winner) and selected_slot["price_ore"] == winner["price_ore"]:
            self._record_delivery_selection(
                selected_slot,
                origin="cheapest",
                candidate_digest=digest,
                baseline=state,
                cart=scope_cart,
                occurrence=occurrence,
            )
            return {
                "ready": True,
                "origin": "cheapest",
                "selected": selected_slot,
                "candidate_digest": digest,
                "price_display": delivery_price_display(selected_slot),
            }
        selection_error = None
        try:
            result = self._delivery({
                "action": "select",
                "slot_ref": winner["slot_ref"],
                "_deadline": deadline,
                "_origin": "cheapest",
                "_candidate_digest": digest,
                "_occurrence": occurrence,
                "_defer_record": True,
            })
        except HouseholdError as exc:
            selection_error = exc
            result = None
        verified = result.get("selected") if isinstance(result, Mapping) else None
        fresh_slots = self._normalized_provider_slots(dates, deadline=deadline)
        fresh_selected = [slot for slot in fresh_slots if slot["selected"]]
        fresh_candidates = [
            slot for slot in fresh_slots
            if delivery_matches(
                preference, slot, timezone_name=str(schedule["timezone"]),
            )
        ]
        if (
            len(fresh_selected) != 1
            or (selection_error is None and not isinstance(verified, Mapping))
            or (
                isinstance(verified, Mapping)
                and not self._same_delivery_identity(
                    fresh_selected[0], validate_delivery_slot(verified),
                )
            )
            or not fresh_candidates
            or any(slot["price_kind"] != "exact" for slot in fresh_candidates)
        ):
            raise HouseholdError(
                "automatic delivery selection failed and fresh provider state is not the exact winner"
                if selection_error is not None
                else "automatic delivery selection changed; inspect the provider selection"
            ) from selection_error
        fresh_digest = delivery_candidate_digest(fresh_candidates)
        fresh_winner = cheapest_delivery_slot(
            fresh_candidates,
            preferred_end=preference.get("preferred_end"),
            timezone_name=str(schedule["timezone"]),
        )
        if fresh_digest != digest or canonical(fresh_selected[0]) != canonical(fresh_winner):
            raise HouseholdError(
                "automatic delivery selection failed and fresh candidates do not prove the winner"
                if selection_error is not None
                else "automatic delivery candidates changed; inspect the provider selection"
            ) from selection_error
        verified = fresh_selected[0]
        fresh_scope_cart = (
            self.oda.call("get_cart", {}, deadline=deadline)
            if self.provider == "meny"
            else self.oda.call("get_cart", {}, deadline=deadline)
        )
        if self.provider == "oda":
            cart_delivery = cart_summary(fresh_scope_cart).get("delivery")
            if (
                not isinstance(cart_delivery, Mapping)
                or not oda_cart_delivery_matches_slot(cart_delivery, verified)
            ):
                raise HouseholdError(
                    "automatic delivery selection is uncertain; provider cart and slots disagree"
                ) from selection_error
        self._record_delivery_selection(
            verified,
            origin="cheapest",
            candidate_digest=fresh_digest,
            baseline=self.store.read(),
            cart=fresh_scope_cart,
            occurrence=occurrence,
        )
        return {
            "ready": True,
            "origin": "cheapest",
            "selected": validate_delivery_slot(verified),
            "candidate_digest": digest,
            "price_display": delivery_price_display(verified),
        }

    def _current_delivery_choice(
        self,
        *,
        occurrence: str | None,
        deadline: float | None,
        allow_recovery: bool = False,
        expected_slot: Mapping[str, Any] | None = None,
        scope_cart: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.store.read()
        if scope_cart is None:
            scope_cart = (
                self.oda.call(
                    "get_cart", {}, deadline=deadline, allow_recovery=allow_recovery,
                )
                if self.provider == "meny"
                else self.oda.call("get_cart", {}, deadline=deadline)
            )
        observation = state.get("delivery_selection")
        applicable = self._delivery_observation_applies(
            observation, state, cart=scope_cart, occurrence=occurrence,
        )
        target = expected_slot
        if target is None and applicable and isinstance(observation.get("slot"), Mapping):
            target = observation["slot"]
        cart_delivery = cart_summary(scope_cart).get("delivery") if self.provider == "oda" else None
        dates = [self._delivery_slot_date(target)] if isinstance(target, Mapping) else None
        if dates is None and isinstance(cart_delivery, Mapping):
            oda_today = now().astimezone(ZoneInfo("Europe/Oslo")).date()
            dates = [oda_cart_delivery_window(cart_delivery, today=oda_today)["date"]]
        selected = [
            slot for slot in self._normalized_provider_slots(
                dates, deadline=deadline, allow_recovery=allow_recovery,
            )
            if slot["selected"]
        ]
        if self.provider == "oda":
            if isinstance(cart_delivery, Mapping):
                if (
                    len(selected) != 1
                    or not oda_cart_delivery_matches_slot(cart_delivery, selected[0])
                ):
                    raise HouseholdError("Oda cart and selected delivery listing disagree")
            elif selected:
                raise HouseholdError("Oda cart and selected delivery listing disagree")
        if len(selected) != 1:
            raise HouseholdError("select one unambiguous provider delivery slot before checkout")
        slot = selected[0]
        if applicable:
            observed_slot = observation.get("slot")
            if (
                not isinstance(observed_slot, Mapping)
                or not self._same_delivery_identity(
                    slot, validate_delivery_slot(observed_slot),
                )
            ):
                raise HouseholdError("provider delivery and local selection provenance disagree")
            origin = str(observation["origin"])
            digest = observation.get("candidate_digest")
            self._record_delivery_selection(
                slot,
                origin=origin,
                candidate_digest=digest,
                baseline=state,
                cart=scope_cart,
                occurrence=occurrence,
            )
        else:
            origin = "external"
            digest = None
        return {
            "ready": True,
            "origin": origin,
            "selected": slot,
            "candidate_digest": digest,
            "price_display": delivery_price_display(slot),
        }

    @staticmethod
    def _bind_delivery_summary(summary: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
        result = deepcopy(dict(summary))
        slot = validate_delivery_slot(binding["selected"])
        existing = result.get("delivery")
        delivery = deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
        delivery.update({
            "slot": slot,
            "price_display": str(binding["price_display"]),
            "candidate_digest": binding.get("candidate_digest"),
            "selection_origin": binding["origin"],
        })
        result["delivery"] = delivery
        amounts = result.get("amounts")
        if slot["price_kind"] == "exact" and isinstance(amounts, Mapping):
            amounts = deepcopy(dict(amounts))
            exact_price = slot["price_ore"] / 100
            supplied = amounts.get("delivery_price")
            if supplied is not None and supplied != exact_price:
                raise HouseholdError("provider checkout delivery price disagrees with the selected slot")
            amounts["delivery_price"] = exact_price
            result["amounts"] = amounts
        return result

    def _unchanged_delivery_binding(
        self,
        binding: Mapping[str, Any],
        *,
        occurrence: str | None,
        deadline: float | None,
        allow_recovery: bool = False,
        scope_cart: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        expected = binding.get("selected", binding.get("slot"))
        if not isinstance(expected, Mapping):
            raise HouseholdError("checkout delivery binding is invalid")
        expected_slot = validate_delivery_slot(expected)
        expected_origin = str(binding.get("origin", binding.get("selection_origin")) or "")
        current = self._current_delivery_choice(
            occurrence=occurrence,
            deadline=deadline,
            allow_recovery=allow_recovery,
            expected_slot=expected_slot,
            scope_cart=scope_cart,
        )
        current_slot = validate_delivery_slot(current["selected"])
        if canonical(current_slot) != canonical(expected_slot):
            raise HouseholdError("delivery changed while preparing the protected checkout summary")
        if expected_origin in {"explicit", "cheapest"} and current.get("origin") != expected_origin:
            raise HouseholdError("delivery selection provenance changed")
        if expected_origin != "cheapest":
            return current
        if not occurrence:
            raise HouseholdError("automatic cheapest delivery is not bound to a scheduled occurrence")
        state = self.store.read()
        schedule = state.get("schedule")
        if not isinstance(schedule, Mapping):
            raise HouseholdError("scheduled delivery configuration is unavailable")
        preference = schedule["delivery"]
        candidates = [
            slot
            for slot in self._normalized_provider_slots(
                self._scheduled_delivery_dates(schedule, now()),
                deadline=deadline,
                allow_recovery=allow_recovery,
            )
            if delivery_matches(
                preference, slot, timezone_name=str(schedule["timezone"]),
            )
        ]
        if not candidates or any(slot["price_kind"] != "exact" for slot in candidates):
            raise HouseholdError("delivery candidates changed and are no longer all exact")
        digest = delivery_candidate_digest(candidates)
        winner = cheapest_delivery_slot(
            candidates,
            preferred_end=preference.get("preferred_end"),
            timezone_name=str(schedule["timezone"]),
        )
        if digest != binding.get("candidate_digest") or canonical(winner) != canonical(expected_slot):
            raise HouseholdError("cheapest delivery candidates changed")
        return {
            "ready": True,
            "origin": "cheapest",
            "selected": current_slot,
            "candidate_digest": digest,
            "price_display": delivery_price_display(current_slot),
        }

    def _scheduled_checkout_problem(
        self,
        summary: Mapping[str, Any],
        occurrence: str | None,
    ) -> str | None:
        if not occurrence:
            return None
        state = self.store.read()
        schedule = state.get("schedule")
        if not isinstance(schedule, Mapping):
            return "scheduled checkout configuration changed"
        maximum_total = validate_schedule(schedule, self.provider)
        if not schedule.get("enabled") or not schedule.get("auto_checkout"):
            return "scheduled checkout is no longer enabled"
        total = summary.get("total")
        if (
            isinstance(total, bool)
            or not isinstance(total, (int, float))
            or not math.isfinite(float(total))
            or maximum_total is None
            or total > maximum_total
        ):
            return "total exceeds maximum"
        if not delivery_matches(
            schedule["delivery"],
            summary.get("delivery"),
            timezone_name=str(schedule["timezone"]),
        ):
            return "delivery does not match preference"
        return None

    def _revalidate_checkout_delivery(
        self,
        pending: Mapping[str, Any],
        *,
        deadline: float | None,
    ) -> dict[str, Any] | None:
        delivery = (pending.get("summary") or {}).get("delivery")
        expected = delivery.get("slot") if isinstance(delivery, Mapping) else None
        if not isinstance(expected, Mapping):
            raise HouseholdError("protected checkout summary has no normalized delivery binding")
        expected_slot = validate_delivery_slot(expected)
        origin = delivery.get("selection_origin")
        occurrence = str(pending.get("occurrence") or "")
        reselections = pending.get("delivery_reselections", 0)
        if isinstance(reselections, bool) or not isinstance(reselections, int) or reselections not in {0, 1}:
            raise HouseholdError("checkout delivery reselection state is invalid")
        if origin == "cheapest":
            state = self.store.read()
            current = [
                slot for slot in self._normalized_provider_slots(
                    [self._delivery_slot_date(expected_slot)], deadline=deadline,
                )
                if slot["selected"]
            ]
            if len(current) != 1 or not self._same_delivery_identity(current[0], expected_slot):
                raise HouseholdError("provider delivery and local selection provenance disagree")
            schedule = state["schedule"]
            preference = schedule["delivery"]
            candidates = [
                slot for slot in self._normalized_provider_slots(
                    self._scheduled_delivery_dates(schedule, now()), deadline=deadline,
                )
                if delivery_matches(
                    preference, slot, timezone_name=str(schedule["timezone"]),
                )
            ]
            if not candidates or any(slot["price_kind"] != "exact" for slot in candidates):
                raise HouseholdError("delivery candidates changed and no exact cheapest window is available")
            digest = delivery_candidate_digest(candidates)
            winner = cheapest_delivery_slot(
                candidates,
                preferred_end=preference.get("preferred_end"),
                timezone_name=str(schedule["timezone"]),
            )
            changed = (
                canonical(winner) != canonical(expected_slot)
                or digest != delivery.get("candidate_digest")
            )
            if changed and reselections >= 1:
                raise HouseholdError("delivery changed a second time; checkout stopped before payment")
            choice = (
                self._scheduled_delivery_choice(
                    schedule, occurrence=occurrence, deadline=deadline,
                )
                if changed
                else {
                    "ready": True,
                    "origin": "cheapest",
                    "selected": current[0],
                    "candidate_digest": digest,
                    "price_display": delivery_price_display(current[0]),
                }
            )
        else:
            choice = self._current_delivery_choice(
                occurrence=occurrence or None, deadline=deadline, expected_slot=expected_slot,
            )
            current = validate_delivery_slot(choice["selected"])
            if not self._same_delivery_identity(current, expected_slot):
                raise HouseholdError("explicit or external delivery selection changed; inspect it before checkout")
            changed = canonical(current) != canonical(expected_slot)
        if not changed:
            return None
        with self.store.locked() as state:
            if canonical(state.get("pending_checkout")) != canonical(pending):
                raise HouseholdError("checkout state changed while revalidating delivery")
            state["pending_checkout"] = None
        prepared = self._checkout_prepare(
            deadline,
            occurrence=occurrence or None,
            delivery_binding=choice,
            delivery_reselections=reselections + 1,
        )
        problem = self._scheduled_checkout_problem(prepared["summary"], occurrence or None)
        if problem is not None:
            with self.store.locked() as state:
                replacement = state.get("pending_checkout")
                if (
                    isinstance(replacement, Mapping)
                    and replacement.get("confirmation_id") == prepared.get("confirmation_id")
                    and replacement.get("status") == "awaiting_confirmation"
                ):
                    state["pending_checkout"] = None
            raise HouseholdError(f"scheduled checkout stopped: {problem}")
        return {
            "confirmed": False,
            "reprepared": True,
            "reason": "delivery price or cheapest candidate set changed",
            **prepared,
        }

    def _delivery(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "list")
        if action == "list":
            dates = request.get("dates")
            if dates is not None and (
                not isinstance(dates, list)
                or not dates
                or len(dates) > 7
                or not all(isinstance(item, str) for item in dates)
            ):
                raise HouseholdError("delivery dates must be one to seven ISO dates")
            if dates is not None:
                for item in dates:
                    try:
                        if date.fromisoformat(item).isoformat() != item:
                            raise ValueError
                    except ValueError as exc:
                        raise HouseholdError("delivery dates must be one to seven ISO dates") from exc
            display: dict[str, str] = {}
            slots = self._normalized_provider_slots(
                dates,
                deadline=request.get("_deadline"),
                address_id=request.get("address_id"),
                allow_recovery=request.get("_allow_browser_recovery") is True,
                display_metadata=display,
            )
            return {
                "provider": self.provider,
                "slots": slots,
                "price_display": {
                    slot["slot_ref"]: delivery_price_display(slot)
                    for slot in slots
                },
                "display": display,
            }
        if action == "select":
            slot_ref = request.get("slot_ref")
            if slot_ref is None:
                raise HouseholdError("delivery select requires the exact slot_ref returned by delivery list")
            arguments = {"delivery_slot_id": slot_ref}
            if request.get("address_id") is not None:
                arguments["delivery_address_id"] = request["address_id"]
            if request.get("unattended") is not None:
                arguments["is_unattended_delivery"] = bool(request["unattended"])
            deadline = request.get("_deadline")
            if deadline is None and self.provider == "meny":
                deadline = time.monotonic() + MENY_ORDER_TIMEOUT
            with self._browser_operation(deadline):
                state = self.store.read()
                if (state.get("pending_checkout") or {}).get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                    raise HouseholdError("reconcile the pending checkout before changing delivery")
                change = deepcopy(state.get("order_change"))
                if change and change.get("status") != "editing":
                    raise HouseholdError("the order change is still starting")
                if self.provider == "oda":
                    if change:
                        cart = cart_summary(self.oda.call("get_cart", {}, deadline=deadline))
                        if cart["items"]:
                            raise HouseholdError("an Oda delivery-window change must be prepared without staged item additions")
                    requested_dates = None
                    if isinstance(slot_ref, str) and slot_ref.startswith("oda:"):
                        requested_dates = [oda_delivery_slot_date(slot_ref)]
                    available = self._normalized_provider_slots(requested_dates, deadline=deadline)
                    candidates = [slot for slot in available if slot["slot_ref"] == slot_ref]
                    if len(candidates) != 1:
                        raise HouseholdError("the requested Oda delivery slot is no longer available")
                    candidate = candidates[0]
                    provider_slot_id = candidate["provider_slot_id"]
                    if provider_slot_id is None:
                        raise HouseholdError("the requested Oda delivery slot has no provider id")
                    arguments["delivery_slot_id"] = provider_slot_id
                    self.oda.call("select_delivery_slot", arguments, deadline=deadline)
                    selected_date = self._delivery_slot_date(candidate)
                    fresh = [
                        slot for slot in self._normalized_provider_slots([selected_date], deadline=deadline)
                        if slot["selected"]
                    ]
                    if (
                        len(fresh) != 1
                        or fresh[0]["slot_ref"] != slot_ref
                        or not self._same_delivery_identity(fresh[0], candidate)
                    ):
                        raise HouseholdError("Oda delivery selection is uncertain; inspect the provider selection")
                    normalized = fresh[0]
                    raw_cart = self.oda.call("get_cart", {}, deadline=deadline)
                    cart = cart_summary(raw_cart)
                    delivery = cart.get("delivery")
                    if (
                        not isinstance(delivery, Mapping)
                        or not oda_cart_delivery_matches_slot(delivery, normalized)
                    ):
                        raise HouseholdError("Oda delivery selection is uncertain; inspect the provider selection")
                    if change:
                        display = delivery.get("display")
                        if not isinstance(display, str) or not display.strip() or len(display) > 500:
                            raise HouseholdError("Oda delivery selection returned no verified display")
                        requested = {
                            "slot_id": normalized["provider_slot_id"],
                            "display": display.strip(),
                            "slot": normalized,
                        }
                        with self.store.locked() as locked:
                            if canonical(locked.get("order_change")) != canonical(change):
                                raise HouseholdError("order change state changed while selecting delivery")
                            locked["order_change"]["kind"] = "delivery"
                            locked["order_change"]["requested_delivery"] = requested
                    if request.get("_defer_record") is not True:
                        self._record_delivery_selection(
                            normalized,
                            origin=str(request.get("_origin") or "explicit"),
                            candidate_digest=request.get("_candidate_digest"),
                            baseline=self.store.read(),
                            cart=raw_cart,
                            occurrence=str(request.get("_occurrence") or "") or None,
                        )
                    response = {
                        "provider": "oda",
                        "selected": normalized,
                        "price_display": delivery_price_display(normalized),
                    }
                    if change:
                        response.update({
                            "staged_for_order": change["order_id"],
                            "next": "Prepare checkout, review the exact new window and any payment difference, then ask for confirmation.",
                        })
                    return response
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
                    selected = result.get("selected") if isinstance(result, Mapping) else None
                    normalized = validate_delivery_slot(selected)
                    if normalized["slot_ref"] != slot_ref or normalized["selected"] is not True:
                        raise HouseholdError("MENY selected delivery does not match the requested slot")
                    if request.get("_defer_record") is not True:
                        scope_cart = self.oda.call("get_cart", {}, deadline=deadline)
                        self._record_delivery_selection(
                            normalized,
                            origin=str(request.get("_origin") or "explicit"),
                            candidate_digest=request.get("_candidate_digest"),
                            baseline=self.store.read(),
                            cart=scope_cart,
                            occurrence=str(request.get("_occurrence") or "") or None,
                        )
                    return result
        raise HouseholdError("unknown delivery action")

    def _orders(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "list")
        cancellation_deadline = time.monotonic() + CANCELLATION_OPERATION_TIMEOUT if action in {"cancel_prepare", "cancel_confirm", "cancel_reconcile", "cancel_submit"} else None
        if action == "list":
            limit = bounded_limit(request.get("limit"), default=10)
            return self.oda.call("get_orders", {"page": 1, "size": limit}, deadline=request.get("_deadline"), allow_recovery=request.get("_allow_browser_recovery") is True) if self.provider == "meny" else self.oda.call("get_orders", {"page": 1, "size": limit})
        supplied_order_id = request.get("order_id")
        order_id = safe_order_id(supplied_order_id) if supplied_order_id is not None and supplied_order_id != "" else ""
        if action == "get":
            deadline = request.get("_deadline") if self.provider == "meny" else None
            if self.provider == "meny":
                order = self.oda.call("get_order", {"order_number": order_id}, deadline=deadline, allow_recovery=request.get("_allow_browser_recovery") is True)
                require_provider_identity(order, order_id)
                return {
                    "order": order,
                    "tracking": {"order_id": order_id, "status": str(order.get("status") or "unknown")},
                }
            order = self.oda.call("get_order", {"order_number": order_id})
            tracking = self.oda.call("order_tracking", {"order_number": order_id})
            require_provider_identity(order, order_id)
            require_provider_identity(tracking, order_id, tracking=True)
            return {"order": order, "tracking": tracking}
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
                if self.provider == "meny":
                    with self._browser_operation(deadline):
                        started = self.browser.begin_order_change(order_id, deadline=deadline)
                    code = str(started.get("code") or "").strip()
                    if not code:
                        raise HouseholdError("MENY order change identity is unavailable")
                    order = started.get("order")
                    if not isinstance(order, Mapping):
                        raise HouseholdError("MENY order change did not return the verified order")
                    current = {
                        "order": dict(order),
                        "tracking": {"order_id": order_id, "status": str(order.get("status") or "unknown")},
                    }
                else:
                    current = self._orders({"action": "get", "order_id": order_id})
                    status = str((current.get("tracking") or {}).get("status") or "").casefold()
                    if status != "paid_and_modifiable":
                        raise HouseholdError("Oda order is not currently modifiable")
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
            except MenyOrderChangeDispatchError as exc:
                with self.store.locked() as state:
                    if canonical(state.get("order_change")) == canonical(reservation):
                        state["order_change"] = {
                            "provider": "meny", "order_id": exc.order_id, "status": "uncertain",
                            "code": exc.code,
                            "before": {
                                "order": deepcopy(exc.order),
                                "tracking": {"order_id": exc.order_id, "status": str(exc.order.get("status") or "unknown")},
                            },
                            "started_at": reservation["started_at"],
                        }
                raise
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
                if self.provider == "meny":
                    code = str(change.get("code") or "")
                    if not code and change.get("status") == "starting":
                        current = self._orders({"action": "get", "order_id": change["order_id"], "_deadline": deadline})
                        code = str((current.get("order") or {}).get("code") or "")
                    if change.get("status") in {"starting", "uncertain", "abort_uncertain"}:
                        try:
                            self.browser.verify_order_change(change["order_id"], code, deadline=deadline)
                        except HouseholdError as active_error:
                            try:
                                self.browser.verify_order_change(None, None, deadline=deadline)
                            except HouseholdError as neutral_error:
                                raise HouseholdError("MENY order-change mode cannot be reconciled safely") from neutral_error
                            result = {"provider": "meny", "order_id": change["order_id"], "aborted": True, "recovered": True}
                        else:
                            try:
                                result = self.browser.abort_order_change(change["order_id"], code, deadline=deadline)
                            except HouseholdError:
                                with self.store.locked() as state:
                                    if canonical(state.get("order_change")) == canonical(change):
                                        state["order_change"]["status"] = "abort_uncertain"
                                raise
                    else:
                        try:
                            result = self.browser.abort_order_change(change["order_id"], code, deadline=deadline)
                        except HouseholdError:
                            with self.store.locked() as state:
                                if canonical(state.get("order_change")) == canonical(change):
                                    state["order_change"]["status"] = "abort_uncertain"
                            raise
                else:
                    if change.get("status") == "starting":
                        result = {"provider": "oda", "order_id": change["order_id"], "aborted": True, "recovered": True}
                    elif cart_summary(self.oda.call("get_cart", {}))["items"]:
                        raise HouseholdError("remove the staged Oda additions before aborting the order change")
                    else:
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
            idempotency_key = self._idempotency_key(request.get("idempotency_key"), "cancellation")
            with self.store.locked() as state:
                pending = deepcopy(state.get("pending_cancellation"))
                protected_request = deepcopy(self._protected_request(state, "cancellation", idempotency_key, target_id=order_id))
            if protected_request:
                if isinstance(protected_request.get("result"), Mapping):
                    return {**deepcopy(dict(protected_request["result"])), "idempotent": True}
                bound_confirmation = str(protected_request.get("confirmation_id") or "")
                if pending and pending.get("confirmation_id") == bound_confirmation and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending):
                    prepared = {
                        "available": True, "confirmation_id": bound_confirmation,
                        "order": deepcopy((pending.get("before") or {}).get("order")),
                        "tracking": deepcopy((pending.get("before") or {}).get("tracking")),
                        "consequence": deepcopy((pending.get("browser") or {}).get("consequence")),
                    }
                else:
                    return self._cancel_reconcile(cancellation_deadline, bound_confirmation)
            else:
                prepared = self._orders({"action": "cancel_prepare", "order_id": order_id})
            if prepared.get("available") is not True:
                return prepared
            with self.store.locked() as state:
                existing = self._protected_request(state, "cancellation", idempotency_key, target_id=order_id)
                if existing is None:
                    current_pending = state.get("pending_cancellation")
                    if not isinstance(current_pending, Mapping) or current_pending.get("confirmation_id") != prepared["confirmation_id"]:
                        raise HouseholdError("cancellation state changed before binding its idempotency key")
                    self._bind_protected_request(state, "cancellation", idempotency_key, prepared["confirmation_id"], target_id=order_id)
                elif existing.get("confirmation_id") != prepared["confirmation_id"]:
                    raise HouseholdError("cancellation idempotency_key is bound to another attempt")
            result = self._cancel(
                "cancel_confirm",
                cancellation_deadline,
                order_id=order_id,
                confirmation_id=str(prepared.get("confirmation_id") or ""),
            )
            return {**result, "confirmation_id": prepared.get("confirmation_id"), "authorized_summary": {"order": prepared.get("order"), "tracking": prepared.get("tracking"), "consequence": prepared.get("consequence")}}
        if action == "cancel_reconcile":
            return self._cancel(action, cancellation_deadline, confirmation_id=str(request.get("confirmation_id") or ""))
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
            return self._cancel_reconcile(deadline, confirmation_id)
        with self.store.locked() as state:
            pending = deepcopy(state.get("pending_cancellation"))
            recovered = self._read_protected_result(state, confirmation_id, "cancellation")
        if recovered:
            return recovered
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
                require_provider_identity(tracking, order_id, tracking=True)
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
                    self._mark_order_cancelled(
                        state, order_id, provider=self.provider, active_provider=self.provider,
                    )
                    terminal = {
                        "cancelled": True, "order_id": order_id,
                        "tracking_status": str(tracking.get("status") or "").casefold(),
                        "retry_allowed": False, "confirmation_id": pending["confirmation_id"],
                    }
                    self._store_protected_result(state, pending["confirmation_id"], "cancellation", terminal, target_id=order_id)
                else:
                    state["pending_cancellation"]["status"] = "uncertain"
        return {"cancelled": cancelled, "tracking": current["tracking"], "retry_allowed": False}

    @staticmethod
    def _prune_order_snapshots(state: dict[str, Any], *, keep_order_id: str | None = None) -> None:
        keep = {str(keep_order_id)} if keep_order_id else set()
        terminal = set()
        snapshots = state.setdefault("order_snapshots", {})
        snapshot_times = state.setdefault("order_snapshot_times", {})
        snapshot_providers = state.setdefault("order_snapshot_providers", {})
        active_provider = state.get("provider")
        current = state.get("menu")
        if isinstance(current, Mapping) and current.get("order_id"):
            current_order_id = str(current["order_id"])
            if snapshot_providers.get(current_order_id) == active_provider:
                keep.add(current_order_id)
        for job in state.get("email_jobs", []):
            if not isinstance(job, Mapping) or not job.get("order_id"):
                continue
            order_id = str(job["order_id"])
            if email_job_provider(job) != snapshot_providers.get(order_id):
                continue
            if job.get("status") in {"pending", "claimed", "sending"}:
                keep.add(order_id)
            elif job.get("status") in {"sent", "cancelled", "invalid"}:
                terminal.add(order_id)
        unscheduled = [
            (str(snapshot_times.get(order_id) or ""), order_id)
            for order_id in snapshots
            if order_id not in keep and order_id not in terminal
        ]
        keep.update(order_id for _recorded_at, order_id in sorted(unscheduled)[-20:])
        for order_id in list(snapshots):
            if order_id not in keep:
                snapshots.pop(order_id, None)
                snapshot_times.pop(order_id, None)
                snapshot_providers.pop(order_id, None)

    @staticmethod
    def _mark_order_cancelled(
        state: dict[str, Any], order_id: str, *, provider: str | None = None,
        active_provider: str | None = None,
    ) -> None:
        for job in state["email_jobs"]:
            provider_matches = provider is None or email_job_provider(job) == provider
            if provider_matches and job.get("order_id") == order_id and job.get("status") in {"pending", "claimed"}:
                job["status"] = "cancelled"
                job.pop("claim_token", None)
                job.pop("claim_expires_at", None)
                job.pop("html", None)
                job.pop("menu_snapshot", None)
                job.pop("subject", None)
        if provider is not None and provider != active_provider:
            return
        for usage in state.get("recipe_usage", {}).values():
            if isinstance(usage, dict) and usage.get("order_id") == order_id and usage.get("status") == "ordered":
                usage["previous_status"] = "ordered"
                usage["status"] = "planned"
                usage["cancelled_order_id"] = order_id
                usage["order_id"] = None
                usage["updated_at"] = now().isoformat()
        current = state.get("menu")
        if isinstance(current, dict) and current.get("order_id") == order_id:
            current["phase"] = "draft"
            current.pop("order_id", None)
        Application._prune_order_snapshots(state)

    def _cancel_reconcile(self, deadline: float | None = None, confirmation_id: str = "") -> dict[str, Any]:
        with self._browser_operation(deadline):
            with self.store.locked() as state:
                pending = deepcopy(state.get("pending_cancellation"))
                recovered = self._read_protected_result(state, confirmation_id, "cancellation") if confirmation_id else None
            if recovered:
                return recovered
            if confirmation_id and isinstance(pending, Mapping) and pending.get("confirmation_id") != confirmation_id:
                raise HouseholdError("cancellation reconciliation does not match the pending attempt")
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
                    self._mark_order_cancelled(
                        state, order_id, provider=self.provider, active_provider=self.provider,
                    )
                    terminal = {
                        "cancelled": True, "order_id": order_id,
                        "tracking_status": str((current.get("tracking") or {}).get("status") or "").casefold(),
                        "retry_allowed": False, "confirmation_id": pending["confirmation_id"],
                    }
                    self._store_protected_result(state, pending["confirmation_id"], "cancellation", terminal, target_id=order_id)
                else:
                    state["pending_cancellation"]["status"] = "uncertain"
            return {"cancelled": cancelled, "tracking": current["tracking"], "retry_allowed": False}

    def _checkout(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "prepare")
        deadline = time.monotonic() + (MENY_CHECKOUT_OPERATION_TIMEOUT if self.provider == "meny" else 240)
        if action == "prepare":
            occurrence = str(request.get("occurrence") or "") or None
            state = self.store.read()
            observation = state.get("delivery_selection")
            scope = observation.get("scope") if isinstance(observation, Mapping) else None
            scoped_occurrence = scope.get("occurrence") if isinstance(scope, Mapping) else None
            scoped_record = state.get("occurrences", {}).get(scoped_occurrence)
            if (
                occurrence is None
                and scoped_occurrence
                and isinstance(scoped_record, Mapping)
                and scoped_record.get("status") == "cart_ready"
            ):
                raise HouseholdError("carry the cart_ready occurrence into checkout prepare")
            if occurrence is not None:
                record = state.get("occurrences", {}).get(occurrence)
                if not isinstance(record, Mapping) or record.get("status") != "cart_ready":
                    raise HouseholdError("checkout occurrence is not a cart_ready scheduled run")
            return self._checkout_prepare(
                deadline,
                occurrence=occurrence,
                cart_ready_continuation=occurrence is not None,
            )
        if action == "confirm":
            return self._checkout_confirm(deadline, str(request.get("confirmation_id") or ""))
        if action == "submit":
            if self.confirmation_policy != "standing":
                raise HouseholdError("standing authorization is not configured; prepare checkout and ask for confirmation")
            idempotency_key = self._idempotency_key(request.get("idempotency_key"), "checkout")
            with self.store.locked() as state:
                pending = deepcopy(state.get("pending_checkout"))
                protected_request = deepcopy(self._protected_request(state, "checkout", idempotency_key))
            if protected_request:
                if isinstance(protected_request.get("result"), Mapping):
                    return {**deepcopy(dict(protected_request["result"])), "idempotent": True}
                bound_confirmation = str(protected_request.get("confirmation_id") or "")
                if not bound_confirmation:
                    raise HouseholdError("checkout idempotency record is incomplete; reconcile before retrying")
                if pending and pending.get("confirmation_id") == bound_confirmation and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending):
                    prepared = {
                        "confirmation_id": bound_confirmation,
                        "summary": deepcopy(pending["summary"]),
                        "order_change": deepcopy(pending.get("order_change")),
                    }
                else:
                    return self._checkout_reconcile(deadline, bound_confirmation)
            elif pending and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending):
                prepared = {
                    "confirmation_id": pending["confirmation_id"],
                    "summary": deepcopy(pending["summary"]),
                    "order_change": deepcopy(pending.get("order_change")),
                }
            else:
                prepared = self._checkout_prepare(deadline)
            if prepared.get("cart_reconciliation_required") is True:
                return {"confirmed": False, **prepared}
            with self.store.locked() as state:
                existing = self._protected_request(state, "checkout", idempotency_key)
                if existing is None:
                    current_pending = state.get("pending_checkout")
                    if not isinstance(current_pending, Mapping) or current_pending.get("confirmation_id") != prepared["confirmation_id"]:
                        raise HouseholdError("checkout state changed before binding its idempotency key")
                    self._bind_protected_request(state, "checkout", idempotency_key, prepared["confirmation_id"])
                elif existing.get("confirmation_id") != prepared["confirmation_id"]:
                    raise HouseholdError("checkout idempotency_key is bound to another attempt")
            result = self._checkout_confirm(deadline, prepared["confirmation_id"])
            if result.get("reprepared") is True:
                replacement_id = str(result.get("confirmation_id") or "")
                if not replacement_id:
                    raise HouseholdError("reprepared checkout has no confirmation identity")
                with self.store.locked() as state:
                    existing = self._protected_request(state, "checkout", idempotency_key)
                    if (
                        not isinstance(existing, dict)
                        or existing.get("confirmation_id") != prepared["confirmation_id"]
                    ):
                        raise HouseholdError("checkout idempotency binding changed during reprepare")
                    current_pending = state.get("pending_checkout")
                    if (
                        not isinstance(current_pending, Mapping)
                        or current_pending.get("confirmation_id") != replacement_id
                    ):
                        raise HouseholdError("reprepared checkout state changed")
                    existing["confirmation_id"] = replacement_id
                    existing["rebound_at"] = now().isoformat()
                return {
                    **result,
                    "confirmation_id": replacement_id,
                    "authorized_summary": result["summary"],
                    "order_change": result.get("order_change"),
                }
            return {**result, "confirmation_id": prepared["confirmation_id"], "authorized_summary": prepared["summary"], "order_change": prepared.get("order_change")}
        if action == "reconcile":
            return self._checkout_reconcile(deadline, str(request.get("confirmation_id") or ""))
        if action == "auto":
            occurrence = str(request.get("occurrence") or "")
            with self.store.locked() as state:
                schedule = deepcopy(state["schedule"])
                validate_schedule(schedule, self.provider)
                if not schedule.get("enabled"):
                    raise HouseholdError("scheduled run is off")
                if not schedule.get("auto_checkout") and schedule.get("mode") != "cart_ready":
                    raise HouseholdError("scheduled delivery choice requires cart_ready or auto_checkout mode")
                if not isinstance(schedule.get("cron_job_id"), str) or not schedule["cron_job_id"].strip():
                    raise HouseholdError("auto-checkout is not linked to its configured cron job")
                expected_occurrence = scheduled_occurrence(schedule, now())
                if state.get("order_change"):
                    raise HouseholdError("scheduled checkout cannot submit an interactive order change")
                if occurrence != expected_occurrence:
                    raise HouseholdError("scheduled occurrence does not match the currently due local week")
                existing = state["occurrences"].get(occurrence)
                if isinstance(existing, Mapping) and existing.get("status") == "completed":
                    order_id = str(existing.get("order_id") or "")
                    if not order_id:
                        raise HouseholdError("the completed scheduled occurrence has no bound order")
                    return {
                        "completed": True, "confirmed": True, "order_id": order_id,
                        "idempotent": True, "retry_allowed": False,
                    }
                if isinstance(existing, Mapping) and existing.get("status") == "started":
                    try:
                        started_at = datetime.fromisoformat(str(existing.get("at") or ""))
                    except ValueError:
                        started_at = None
                    if started_at is not None and started_at.tzinfo is not None and now() < started_at + SCHEDULE_OCCURRENCE_LEASE:
                        raise HouseholdError("this scheduled occurrence is already running")
                pending = state.get("pending_checkout")
                if pending and pending.get("status") == "awaiting_confirmation" and pending.get("occurrence"):
                    self._abandon_predispatch(state, reason="scheduled run retried")
                elif pending:
                    raise HouseholdError("finish the pending interactive or dispatched checkout before the scheduled run")
                attempts = int(existing.get("attempts", 0)) + 1 if isinstance(existing, Mapping) else 1
                state["occurrences"][occurrence] = {"status": "started", "at": now().isoformat(), "attempts": attempts}
            try:
                delivery_choice = self._scheduled_delivery_choice(
                    schedule, occurrence=occurrence, deadline=deadline,
                )
                if delivery_choice.get("ready") is not True:
                    with self.store.locked() as state:
                        state["occurrences"][occurrence]["status"] = "needs_input"
                    return {
                        "completed": False,
                        "confirmed": False,
                        "mode": "cart_ready",
                        **delivery_choice,
                    }
                if not schedule.get("auto_checkout"):
                    if not delivery_matches(
                        schedule["delivery"],
                        delivery_choice.get("selected"),
                        timezone_name=str(schedule["timezone"]),
                    ):
                        with self.store.locked() as state:
                            state["occurrences"][occurrence]["status"] = "needs_input"
                        return {
                            "completed": False,
                            "confirmed": False,
                            "mode": "cart_ready",
                            "reason": "delivery does not match preference",
                            **delivery_choice,
                        }
                    cart = (
                        self.oda.call("get_cart", {}, deadline=deadline)
                        if self.provider == "meny"
                        else self.oda.call("get_cart", {}, deadline=deadline)
                    )
                    summary = self._bind_delivery_summary(cart_summary(cart), delivery_choice)
                    with self.store.locked() as state:
                        state["occurrences"][occurrence]["status"] = "cart_ready"
                    return {
                        "completed": False,
                        "confirmed": False,
                        "mode": "cart_ready",
                        "occurrence": occurrence,
                        "summary": summary,
                        **delivery_choice,
                    }
                prepared = self._checkout_prepare(
                    deadline, occurrence=occurrence, delivery_binding=delivery_choice,
                )
            except HouseholdError:
                with self.store.locked() as state:
                    state["occurrences"][occurrence]["status"] = "needs_input"
                raise
            if prepared.get("cart_reconciliation_required") is True:
                with self.store.locked() as state:
                    state["occurrences"][occurrence]["status"] = "needs_input"
                return {"completed": False, "confirmed": False, "mode": "cart_ready", **prepared}
            problem = self._scheduled_checkout_problem(prepared["summary"], occurrence)
            if problem is not None:
                with self.store.locked() as state:
                    pending = state.get("pending_checkout")
                    if pending and pending.get("confirmation_id") == prepared["confirmation_id"] and pending.get("status") == "awaiting_confirmation":
                        state["pending_checkout"] = None
                    state["occurrences"][occurrence]["status"] = "needs_input"
                return {"completed": False, "reason": problem, "summary": prepared["summary"]}
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

    def _checkout_prepare(
        self,
        deadline: float | None = None,
        *,
        occurrence: str | None = None,
        delivery_binding: Mapping[str, Any] | None = None,
        delivery_reselections: int = 0,
        cart_ready_continuation: bool = False,
    ) -> dict[str, Any]:
        with self.store.locked() as state:
            if cart_ready_continuation:
                record = state.get("occurrences", {}).get(occurrence)
                if not isinstance(record, Mapping) or record.get("status") != "cart_ready":
                    raise HouseholdError("checkout occurrence is no longer cart_ready")
            current_pending = state.get("pending_checkout")
            inherited_occurrence = (
                current_pending.get("occurrence")
                if occurrence is None
                and isinstance(current_pending, Mapping)
                and current_pending.get("status") == "awaiting_confirmation"
                else None
            )
            if inherited_occurrence:
                occurrence = inherited_occurrence
                occurrence_record = state.get("occurrences", {}).get(occurrence)
                if isinstance(occurrence_record, dict):
                    occurrence_record["status"] = "started"
                    occurrence_record["at"] = now().isoformat()
            if expired_awaiting_confirmation(current_pending):
                state["pending_checkout"] = None
            baseline = deepcopy(state.get("pending_checkout"))
            if baseline and baseline.get("status") in UNRESOLVED_CHECKOUT_STATUSES:
                raise HouseholdError("reconcile the pending checkout before preparing another")
            order_change = deepcopy(state.get("order_change"))
            menu_baseline = deepcopy(state.get("menu"))
            current_usage = (
                state.get("recipe_usage", {}).get(menu_baseline.get("menu_id"))
                if isinstance(menu_baseline, Mapping)
                else None
            )
            if not order_change and isinstance(menu_baseline, Mapping) and (
                menu_baseline.get("phase") == "ordered"
                or menu_baseline.get("order_id")
                or (isinstance(current_usage, Mapping) and current_usage.get("status") == "ordered")
            ):
                raise HouseholdError("the current menu already belongs to an order; save or select a new menu before a new checkout")
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
        cart = self.oda.call("get_cart", {}, deadline=deadline, allow_recovery=allow_recovery) if self.provider == "meny" else self.oda.call("get_cart", {}, deadline=deadline)
        summary = cart_summary(cart)
        cart_plan_baseline = None
        if not order_change and isinstance(menu_baseline, Mapping):
            cart_gate = self._cart_checkout_gate(summary, menu_baseline)
            if cart_gate is not None:
                return cart_gate
            cart_plan_baseline = deepcopy(self.store.read().get("cart_plan"))
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
                if self.provider == "meny":
                    review = self.browser.review_checkout(
                        cart,
                        order_change=order_change,
                        deadline=deadline,
                        allow_recovery=allow_recovery,
                    )
                else:
                    try:
                        review = self.browser.review_checkout(cart, deadline=deadline)
                    except OdaCheckoutMismatchError:
                        refreshed_cart = self.oda.call("get_cart", {}, deadline=deadline)
                        refreshed_summary = cart_summary(refreshed_cart)
                        if canonical(refreshed_summary) == canonical(summary):
                            raise
                        if isinstance(menu_baseline, Mapping):
                            cart_gate = self._cart_checkout_gate(refreshed_summary, menu_baseline)
                            if cart_gate is not None:
                                return cart_gate
                        raise HouseholdError("Oda cart or delivery changed while preparing checkout; prepare a new summary")
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
                reviewed_delivery = summary.get("delivery")
                if not isinstance(reviewed_delivery, Mapping):
                    raise HouseholdError("MENY checkout returned no verified delivery")
                review_cart = dict(cart)
                review_cart["delivery"] = deepcopy(dict(reviewed_delivery))
                for _ in range(3):
                    stable_review = self.browser.review_checkout(
                        review_cart,
                        order_change=order_change,
                        deadline=deadline,
                        allow_recovery=allow_recovery,
                    )
                    stable_summary = stable_review.get("summary")
                    if not isinstance(stable_summary, Mapping):
                        raise HouseholdError("MENY checkout returned no verified summary")
                    if meny_checkout_reviews_match(review, stable_review):
                        review = stable_review
                        summary = deepcopy(dict(stable_summary))
                        break
                    review = stable_review
                    summary = deepcopy(dict(stable_summary))
                else:
                    raise HouseholdError("MENY checkout summary did not settle")
            elif not order_change:
                refreshed_cart = self.oda.call("get_cart", {}, deadline=deadline)
                refreshed_summary = cart_summary(refreshed_cart)
                if canonical(refreshed_summary) != canonical(summary):
                    if isinstance(menu_baseline, Mapping):
                        cart_gate = self._cart_checkout_gate(refreshed_summary, menu_baseline)
                        if cart_gate is not None:
                            return cart_gate
                    raise HouseholdError("Oda cart or delivery changed while preparing checkout; prepare a new summary")
                cart = refreshed_cart
                summary = refreshed_summary
            if self.provider == "oda" and isinstance(review.get("amounts"), Mapping):
                amount_cart = dict(cart)
                amount_cart["amounts"] = deepcopy(dict(review["amounts"]))
                reviewed_amounts = cart_summary(amount_cart).get("amounts")
                if not isinstance(reviewed_amounts, Mapping):
                    raise HouseholdError("Oda checkout returned no verified amounts")
                summary["amounts"] = deepcopy(dict(reviewed_amounts))
            payment_display = None
            if self.provider == "oda":
                payment_display = str((review.get("summary") or {}).get("payment") or review.get("payment_display") or "")
                if re.fullmatch(r"•••• \d{4}", payment_display) is None:
                    raise HouseholdError("Oda checkout returned no verified masked payment identity")
            if delivery_binding is None:
                delivery_binding = self._current_delivery_choice(
                    occurrence=occurrence, deadline=deadline, allow_recovery=allow_recovery,
                    scope_cart=cart,
                )
            else:
                delivery_binding = self._unchanged_delivery_binding(
                    delivery_binding,
                    occurrence=occurrence,
                    deadline=deadline,
                    allow_recovery=allow_recovery,
                    scope_cart=cart,
                )
            summary = self._bind_delivery_summary(summary, delivery_binding)
            if self.provider == "meny":
                review = deepcopy(dict(review))
                review["delivery_guard"] = deepcopy(dict(delivery_binding))
            confirmation_id = secrets.token_urlsafe(18)
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) != canonical(baseline):
                    raise HouseholdError("checkout state changed while preparing the summary")
                if canonical(state.get("order_change")) != canonical(order_change):
                    raise HouseholdError("order change state changed while preparing the summary")
                if canonical(state.get("menu")) != canonical(menu_baseline):
                    raise HouseholdError("menu changed while preparing checkout; prepare a new summary")
                if cart_plan_baseline is not None and canonical(state.get("cart_plan")) != canonical(cart_plan_baseline):
                    raise HouseholdError("cart plan changed while preparing checkout; prepare a new summary")
                state["pending_checkout"] = {
                    "status": "awaiting_confirmation",
                    "confirmation_id": confirmation_id,
                    "cart": cart,
                    "summary": summary,
                    "orders_before": before,
                    "browser_review": review,
                    "expires_at": (now() + timedelta(minutes=20)).isoformat(),
                    "menu": menu_baseline,
                    "cart_plan": cart_plan_baseline,
                    "menu_ref": {
                        "menu_id": menu_baseline.get("menu_id"),
                        "revision": menu_baseline.get("revision"),
                        "digest": menu_baseline.get("digest"),
                    } if isinstance(menu_baseline, Mapping) else None,
                    "occurrence": occurrence,
                    "order_change": order_change,
                    "delivery_reselections": delivery_reselections,
                }
                if occurrence:
                    occurrence_record = state.get("occurrences", {}).get(occurrence)
                    if isinstance(occurrence_record, dict):
                        occurrence_record["status"] = "awaiting_confirmation"
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
            recovered = self._read_protected_result(state, confirmation_id, "checkout")
        if recovered:
            return recovered
        if not pending or pending["status"] != "awaiting_confirmation":
            raise HouseholdError("no fresh checkout confirmation is pending")
        if confirmation_id != pending.get("confirmation_id"):
            raise HouseholdError("checkout confirmation does not match the prepared summary")
        if now() >= datetime.fromisoformat(pending["expires_at"]):
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) == canonical(pending):
                    state["pending_checkout"] = None
            raise HouseholdError("checkout confirmation expired")
        try:
            reprepared = self._revalidate_checkout_delivery(pending, deadline=deadline)
        except HouseholdError:
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) == canonical(pending):
                    state["pending_checkout"] = None
            raise
        if reprepared is not None:
            return reprepared
        cart = self.oda.call("get_cart", {}, deadline=deadline) if self.provider == "meny" else self.oda.call("get_cart", {}, deadline=deadline)
        pending_change = pending.get("order_change") or {}
        expected_cart = cart_summary(pending["cart"])
        if canonical(cart_summary(cart)) != canonical(expected_cart):
            with self.store.locked() as state:
                if canonical(state.get("pending_checkout")) == canonical(pending):
                    state["pending_checkout"] = None
            raise HouseholdError("cart or delivery changed; show a new summary")
        with self.store.locked() as state:
            if canonical(state.get("order_change")) != canonical(pending.get("order_change")):
                raise HouseholdError("order change changed; show a new summary")
            if pending.get("cart_plan") is not None and canonical(state.get("cart_plan")) != canonical(pending.get("cart_plan")):
                raise HouseholdError("cart plan changed; show a new summary")
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
                    if pending.get("cart_plan") is not None and canonical(state.get("cart_plan")) != canonical(pending.get("cart_plan")):
                        raise HouseholdError("cart plan changed; show a new summary")
                    state["pending_checkout"]["status"] = "clicking"

                def before_click() -> None:
                    # MENY's provider client is this same locked browser tab;
                    # submit_checkout performs its own exact fresh review.
                    if self.provider == "oda":
                        fresh = self.oda.call("get_cart", {}, deadline=deadline)
                        expected = cart_summary(pending["cart"])
                        if canonical(cart_summary(fresh)) != canonical(expected):
                            raise CheckoutPreconditionError("cart or delivery changed before the final click")
                        try:
                            self._unchanged_delivery_binding(
                                pending["summary"]["delivery"],
                                occurrence=str(pending.get("occurrence") or "") or None,
                                deadline=deadline,
                            )
                        except HouseholdError as exc:
                            raise CheckoutPreconditionError(
                                f"delivery changed again before the final click: {exc}"
                            ) from exc
                    problem = self._scheduled_checkout_problem(
                        pending["summary"], str(pending.get("occurrence") or "") or None,
                    )
                    if problem is not None:
                        raise CheckoutPreconditionError(
                            f"scheduled checkout stopped before the final click: {problem}"
                        )
                    if pending_change:
                        with self.store.locked() as state:
                            if canonical(state.get("order_change")) != canonical(pending_change):
                                raise CheckoutPreconditionError("order change changed before the final click")
                        if self.provider == "oda":
                            fresh_target = self._orders({"action": "get", "order_id": pending_change["order_id"], "_deadline": deadline})
                            if canonical(fresh_target) != canonical(pending_change["before"]):
                                raise CheckoutPreconditionError("the target order changed before the final click")
                    with self.store.locked() as state:
                        if pending.get("cart_plan") is not None and canonical(state.get("cart_plan")) != canonical(pending.get("cart_plan")):
                            raise CheckoutPreconditionError("cart plan changed before the final click")
                        current_pending = state.get("pending_checkout")
                        expected = {**pending, "status": "clicking"}
                        if not current_pending or canonical(current_pending) != canonical(expected):
                            raise CheckoutPreconditionError("checkout confirmation changed before the final click")
                    if now() >= datetime.fromisoformat(pending["expires_at"]):
                        raise CheckoutPreconditionError("checkout confirmation expired before the final click")

                if self.provider == "meny":
                    try:
                        self._unchanged_delivery_binding(
                            pending["summary"]["delivery"],
                            occurrence=str(pending.get("occurrence") or "") or None,
                            deadline=deadline,
                        )
                    except HouseholdError as exc:
                        raise CheckoutPreconditionError(
                            f"delivery changed again before the final provider action: {exc}"
                        ) from exc

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

    def _record_order_snapshot(self, state: dict[str, Any], pending: Mapping[str, Any], order_id: str) -> None:
        order_id = safe_order_id(order_id)
        occurrence = pending.get("occurrence")
        if occurrence:
            record = state.setdefault("occurrences", {}).get(occurrence)
            if isinstance(record, dict):
                record["status"] = "completed"
                record["order_id"] = order_id
                record["completed_at"] = now().isoformat()
        if pending.get("order_change"):
            return
        if isinstance(pending.get("cart_plan"), Mapping) and canonical(state.get("cart_plan")) == canonical(pending.get("cart_plan")):
            state["cart_plan"] = None
        snapshot = deepcopy(pending.get("menu"))
        if not isinstance(snapshot, dict):
            return
        previous_order_id = snapshot.get("order_id")
        if previous_order_id and previous_order_id != order_id:
            return
        snapshot["phase"] = "ordered"
        snapshot["order_id"] = order_id
        state.setdefault("order_snapshots", {})[order_id] = snapshot
        state.setdefault("order_snapshot_times", {})[order_id] = now().isoformat()
        state.setdefault("order_snapshot_providers", {})[order_id] = self.provider
        menu_id = snapshot.get("menu_id")
        usage = state.setdefault("recipe_usage", {}).get(menu_id)
        if isinstance(usage, dict):
            usage["status"] = "ordered"
            usage["order_id"] = order_id
            usage["updated_at"] = now().isoformat()
        current = state.get("menu")
        if isinstance(current, Mapping) and current.get("menu_id") == snapshot.get("menu_id") and current.get("digest") == snapshot.get("digest"):
            state["menu"] = deepcopy(snapshot)
        Application._prune_order_snapshots(state, keep_order_id=order_id)

    def _checkout_reconcile(self, deadline: float | None = None, confirmation_id: str = "") -> dict[str, Any]:
        with self._browser_operation(deadline):
            return self._checkout_reconcile_unlocked(deadline, confirmation_id)

    def _checkout_reconcile_unlocked(self, deadline: float | None = None, confirmation_id: str = "") -> dict[str, Any]:
        with self.store.locked() as state:
            pending = deepcopy(state.get("pending_checkout"))
            recovered = self._read_protected_result(state, confirmation_id, "checkout") if confirmation_id else None
        if recovered:
            return recovered
        if confirmation_id and isinstance(pending, Mapping) and pending.get("confirmation_id") != confirmation_id:
            raise HouseholdError("checkout reconciliation does not match the pending attempt")
        if not pending:
            raise HouseholdError("no checkout attempt is pending")
        if pending.get("status") not in {"clicking", "uncertain", "awaiting_user_payment"}:
            raise HouseholdError("checkout has not reached reconciliation")
        if pending.get("order_change"):
            return self._order_change_reconcile(pending, deadline)
        payment_expiry = pending.get("payment_expires_at")
        try:
            payment_expires_at = datetime.fromisoformat(str(payment_expiry or ""))
        except ValueError:
            payment_expires_at = None
        if (
            self.provider == "meny"
            and payment_expires_at is not None
            and payment_expires_at.tzinfo is not None
            and now() < payment_expires_at
            and self.browser.checkout_payment_awaiting_user(deadline=deadline)
        ):
            return {
                "confirmed": False,
                "expired": False,
                "order": None,
                "tracking": None,
                "retry_allowed": False,
                "awaiting_user_payment": True,
            }
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
        expirable_payment = pending.get("status") == "awaiting_user_payment" or (
            pending.get("status") == "uncertain" and payment_expiry is not None
        )
        if self.provider == "meny" and expirable_payment and not confirmed and confirmation_order_id is None and len(candidates) <= 1 and not candidate_matches:
            expiry = payment_expiry or pending.get("expires_at")
            if expiry == payment_expiry:
                expires_at = payment_expires_at
            else:
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
                self._record_order_snapshot(state, pending, order_id)
                terminal = {
                    "confirmed": True, "order_id": order_id, "tracking_status": tracking_status,
                    "retry_allowed": False, "confirmation_id": pending["confirmation_id"],
                }
                self._store_protected_result(
                    state, pending["confirmation_id"], "checkout", terminal,
                    target_id=order_id, intent_signature=checkout_intent_signature(pending["summary"]),
                )
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
            expected_signature = oda_delivery_signature(expected_delivery)
            actual_signature = oda_delivery_signature(actual_delivery)
            actual_date_value = current["order"].get("deliveryDate")
            try:
                actual_date = date.fromisoformat(actual_date_value) if isinstance(actual_date_value, str) else None
            except ValueError:
                actual_date = None
            expected_month = {
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "des": 12,
            }.get(expected_signature[5]) if expected_signature is not None else None
            before_address = oda_order_address_identity(change["before"]["order"])
            current_address = oda_order_address_identity(current["order"])
            before_quantities = oda_order_quantities(change["before"]["order"])
            current_quantities = oda_order_quantities(current["order"])
            before_total = money_cents(change["before"]["order"].get("grossAmount"))
            current_total = money_cents(current["order"].get("grossAmount"))
            authorized_delta = money_cents(pending["summary"].get("total"))
            matched = (
                expected_signature is not None
                and expected_signature == actual_signature
                and actual_date is not None
                and actual_date.isoformat() == actual_date_value
                and (actual_date.month, actual_date.day) == (expected_month, expected_signature[4])
                and before_address is not None
                and before_address == current_address
                and before_quantities is not None
                and before_quantities == current_quantities
                and before_total is not None
                and current_total is not None
                and authorized_delta is not None
                and current_total == before_total + authorized_delta
            )
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
                self._record_order_snapshot(state, pending, order_id)
                terminal = {
                    "confirmed": True, "changed_existing_order": True, "order_id": order_id,
                    "tracking_status": status, "retry_allowed": False,
                    "confirmation_id": pending["confirmation_id"],
                }
                self._store_protected_result(
                    state, pending["confirmation_id"], "checkout", terminal,
                    target_id=order_id, intent_signature=checkout_intent_signature(pending["summary"]),
                )
            else:
                state["pending_checkout"]["status"] = "uncertain"
        return {"confirmed": confirmed, "changed_existing_order": confirmed, "order": current["order"] if confirmed else None, "tracking": current["tracking"] if confirmed else None, "retry_allowed": False}

    def _email(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "status")
        requested_provider = request.get("provider")
        if requested_provider is not None and (
            not isinstance(requested_provider, str) or requested_provider not in {"oda", "meny"}
        ):
            raise HouseholdError("email provider must be oda or meny")

        def matching_jobs(state: Mapping[str, Any], order_id: str, statuses: set[str] | None = None) -> list[dict[str, Any]]:
            candidates = [
                job for job in state.get("email_jobs", [])
                if isinstance(job, dict) and job.get("order_id") == order_id
                and (statuses is None or job.get("status") in statuses)
            ]
            if requested_provider is not None:
                candidates = [job for job in candidates if email_job_provider(job) == requested_provider]
            if any(email_job_provider(job) is None for job in candidates):
                raise HouseholdError("email job has no valid bound provider")
            providers = {email_job_provider(job) for job in candidates}
            if len(providers) > 1:
                raise HouseholdError("email order identity is ambiguous; specify provider")
            return candidates

        if action == "status":
            state = self.store.read()
            jobs = [{
                "order_id": job.get("order_id"), "delivery_date": job.get("delivery_date"),
                "status": job.get("status"), "sent_at": job.get("sent_at"),
                "provider": email_job_provider(job),
                "recipient": mask_email(job.get("recipient_snapshot")),
                "automation_update_required": job.get("status") == "pending" and job.get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL,
            } for job in state["email_jobs"]]
            return {"jobs": jobs, "automation_updates_required": sum(bool(job["automation_update_required"]) for job in jobs)}
        if action == "automation_plan":
            state = self.store.read()
            updates = []
            for job in state["email_jobs"]:
                if job.get("status") != "pending" or job.get("automation_protocol") == EMAIL_AUTOMATION_PROTOCOL:
                    continue
                provider = email_job_provider(job)
                order_id = safe_order_id(job.get("order_id"))
                delivery_date = str(job.get("delivery_date") or "")
                automation_key = email_automation_key(provider, order_id) if provider else ""
                if not provider or not order_id or not valid_email_address(job.get("recipient_snapshot")) or not isinstance(job.get("menu_snapshot"), Mapping):
                    raise HouseholdError("pending email automation is not bound to one exact order, menu and recipient")
                updates.append({
                    "provider": provider, "order_id": order_id, "delivery_date": delivery_date, "automation_key": automation_key,
                    "cron_prompt": email_automation_prompt(provider, order_id, delivery_date, automation_key),
                    "ack": email_automation_ack(provider, order_id, delivery_date, automation_key),
                })
            return {"protocol": EMAIL_AUTOMATION_PROTOCOL, "updates": updates}
        if action == "ack_automation":
            order_id = safe_order_id(request.get("order_id"))
            automation_key = str(request.get("automation_key") or "")
            delivery_date = request.get("delivery_date")
            automation_digest = request.get("automation_digest")
            if request.get("protocol") != EMAIL_AUTOMATION_PROTOCOL:
                raise HouseholdError(f"email automation protocol must be {EMAIL_AUTOMATION_PROTOCOL}")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"pending"})
                provider = email_job_provider(jobs[0]) if len(jobs) == 1 else None
                expected_key = (
                    str(jobs[0].get("automation_key") or email_automation_key(provider, order_id))
                    if len(jobs) == 1 and provider and jobs[0].get("automation_protocol") == EMAIL_AUTOMATION_PROTOCOL
                    else email_automation_key(provider, order_id) if len(jobs) == 1 and provider else ""
                )
                current_delivery = str(jobs[0].get("delivery_date") or "") if len(jobs) == 1 else ""
                expected_digest = hashlib.sha256(email_automation_prompt(provider, order_id, current_delivery, expected_key).encode()).hexdigest() if len(jobs) == 1 and provider else ""
                if (
                    len(jobs) != 1 or not automation_key or not secrets.compare_digest(expected_key, automation_key)
                    or delivery_date != current_delivery or not isinstance(automation_digest, str)
                    or not secrets.compare_digest(expected_digest, automation_digest)
                ):
                    raise HouseholdError("email automation acknowledgement does not match one pending job")
                jobs[0]["automation_key"] = automation_key
                jobs[0]["automation_protocol"] = EMAIL_AUTOMATION_PROTOCOL
            return {"acknowledged": True, "provider": provider, "order_id": order_id, "automation_key": automation_key, "protocol": EMAIL_AUTOMATION_PROTOCOL}
        if action == "schedule":
            if requested_provider is not None and requested_provider != self.provider:
                raise HouseholdError("schedule provider must match the active household provider")
            order_id = safe_order_id(request.get("order_id"))
            supplied_delivery_date = request.get("delivery_date")
            if not isinstance(supplied_delivery_date, str):
                raise HouseholdError("delivery_date must be a canonical ISO date")
            try:
                delivery_date = date.fromisoformat(supplied_delivery_date).isoformat()
            except ValueError as exc:
                raise HouseholdError("delivery_date must be a canonical ISO date") from exc
            if delivery_date != supplied_delivery_date:
                raise HouseholdError("delivery_date must be a canonical ISO date")
            with self.store.locked() as locked:
                snapshot = None
                if isinstance(locked.get("menu"), Mapping) and locked["menu"].get("order_id") == order_id:
                    snapshot = deepcopy(locked["menu"])
                elif (locked.get("order_snapshot_providers") or {}).get(order_id) == self.provider:
                    snapshot = deepcopy((locked.get("order_snapshots") or {}).get(order_id))
                recipient = locked.get("email_recipient")
                if not isinstance(snapshot, Mapping) or not isinstance(recipient, str) or not recipient.strip():
                    raise HouseholdError("confirmed order, exact menu and email recipient are required")
                period = menu_email_period(snapshot)
                existing = [
                    job for job in locked["email_jobs"]
                    if job.get("order_id") == order_id and email_job_provider(job) == self.provider
                ]
                automation_key = email_automation_key(self.provider, order_id)
                created = not existing
                rescheduled = False
                if not existing:
                    locked["email_jobs"].append({
                        "order_id": order_id, "delivery_date": delivery_date, "status": "pending", "sent_at": None,
                        "provider": self.provider,
                        "recipient_snapshot": recipient, "menu_snapshot": snapshot,
                        "subject": f"Ukesmeny og oppskrifter – {period}", "html": menu_email_html(snapshot),
                        "automation_key": automation_key, "automation_protocol": 0,
                    })
                elif len(existing) == 1:
                    if existing[0].get("status") != "pending":
                        raise HouseholdError("the order email is already claimed, sent or cancelled")
                    if not valid_email_address(existing[0].get("recipient_snapshot")) or not isinstance(existing[0].get("menu_snapshot"), Mapping):
                        raise HouseholdError("the pending order email is not bound to its original menu and recipient")
                    recipient = existing[0]["recipient_snapshot"]
                    existing[0]["provider"] = self.provider
                    rescheduled = existing[0].get("delivery_date") != delivery_date
                    existing[0]["delivery_date"] = delivery_date
                    if existing[0].get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL:
                        existing[0]["automation_key"] = automation_key
                    if rescheduled:
                        existing[0]["automation_protocol"] = 0
                else:
                    raise HouseholdError("the order has multiple email jobs")
                job = (existing or [locked["email_jobs"][-1]])[0]
                automation_update_required = job.get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL
            return {
                "scheduled": created,
                "provider": self.provider,
                "idempotent": not created and not rescheduled,
                "rescheduled": rescheduled,
                "automation_key": automation_key,
                "delivery_date": delivery_date,
                "recipient": mask_email(recipient),
                "automation_update_required": automation_update_required,
                "cron_prompt": email_automation_prompt(self.provider, order_id, delivery_date, automation_key),
                "automation_ack": email_automation_ack(self.provider, order_id, delivery_date, automation_key),
            }
        if action == "test":
            order_id = safe_order_id(request.get("order_id"))
            state = self.store.read()
            jobs = matching_jobs(state, order_id, {"pending"})
            job = jobs[0] if len(jobs) == 1 else {}
            menu = job.get("menu_snapshot")
            recipient = job.get("recipient_snapshot")
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
            order_id = safe_order_id(request.get("order_id"))
            state = self.store.read()
            matching = matching_jobs(state, order_id, {"pending", "claimed", "sending"})
            if not matching:
                return {"send": False, "reason": "no pending email"}
            if len(matching) != 1:
                return {"send": False, "reason": "multiple email jobs for the provider order"}
            initial_job = deepcopy(matching[0])
            job_provider = email_job_provider(initial_job)
            if job_provider is None:
                raise HouseholdError("email job has no valid bound provider")
            if initial_job.get("status") == "pending" and initial_job.get("automation_protocol") != EMAIL_AUTOMATION_PROTOCOL:
                delivery_date = str(initial_job.get("delivery_date") or "")
                automation_key = email_automation_key(job_provider, order_id)
                return {
                    "send": False, "reason": "email automation update is required",
                    "provider": job_provider, "order_id": order_id, "delivery_date": delivery_date,
                    "automation_key": automation_key, "automation_update_required": True,
                    "cron_prompt": email_automation_prompt(job_provider, order_id, delivery_date, automation_key),
                    "automation_ack": email_automation_ack(job_provider, order_id, delivery_date, automation_key),
                }
            provider_client = self.email_provider_clients.get(job_provider)
            if provider_client is None:
                raise HouseholdError(f"provider {job_provider} is unavailable for the bound email job")
            if job_provider == self.provider:
                current = self._orders({"action": "get", "order_id": order_id, "_deadline": request.get("_deadline")})
            else:
                order = provider_client.call("get_order", {"order_number": order_id})
                tracking = provider_client.call("order_tracking", {"order_number": order_id})
                require_provider_identity(order, order_id)
                require_provider_identity(tracking, order_id, tracking=True)
                current = {"order": order, "tracking": tracking}
            tracking = str((current.get("tracking") or {}).get("status") or "").casefold()
            with self.store.locked() as state:
                matching = matching_jobs(state, order_id, {"pending", "claimed", "sending"})
                if len(matching) != 1 or canonical(matching[0]) != canonical(initial_job):
                    raise HouseholdError("email job changed while checking its provider order")
                pending_cancellation = state.get("pending_cancellation")
                if expired_awaiting_confirmation(pending_cancellation):
                    state["pending_cancellation"] = None
                    pending_cancellation = None
                if job_provider == self.provider and isinstance(pending_cancellation, Mapping) and pending_cancellation.get("order_id") == order_id:
                    return {"send": False, "reason": "order cancellation is pending"}
                if len(matching) == 1 and matching[0].get("status") == "claimed":
                    try:
                        claim_expires_at = datetime.fromisoformat(str(matching[0].get("claim_expires_at") or ""))
                    except ValueError:
                        claim_expires_at = None
                    if claim_expires_at is not None and claim_expires_at.tzinfo is not None and now() >= claim_expires_at:
                        matching[0]["status"] = "pending"
                        matching[0].pop("claim_token", None)
                        matching[0].pop("claim_expires_at", None)
                    else:
                        return {"send": False, "reason": "email is already claimed before dispatch"}
                elif len(matching) == 1 and matching[0].get("status") == "sending":
                    return {"send": False, "reason": "email send outcome needs reconciliation"}
                jobs = [job for job in matching if job.get("status") == "pending"]
                if not jobs:
                    return {"send": False, "reason": "no pending email"}
                job = jobs[0] if len(jobs) == 1 else {}
                menu = job.get("menu_snapshot")
                recipient = job.get("recipient_snapshot")
                if len(jobs) != 1 or not isinstance(menu, Mapping) or menu.get("order_id") != order_id or not isinstance(recipient, str) or not recipient.strip():
                    return {"send": False, "reason": "pending email is not bound to one exact menu and recipient"}
                if tracking in {"cancelled", "canceled"}:
                    self._mark_order_cancelled(
                        state, order_id, provider=job_provider, active_provider=self.provider,
                    )
                    return {"send": False, "reason": "order cancelled"}
                fulfillable = {"confirmed", "delivered"} if job_provider == "meny" else {
                    "paid_and_modifiable", "paid_and_not_modifiable", "picking", "shipped", "delivered",
                }
                if tracking not in fulfillable:
                    return {"send": False, "reason": "order status is not confirmed for recipe email"}
                order = current.get("order") if isinstance(current.get("order"), Mapping) else {}
                delivery_values = [order.get(key) for key in ("deliveryDate", "delivery_date") if key in order]
                if not delivery_values or not all(isinstance(value, str) for value in delivery_values) or len(set(delivery_values)) != 1:
                    raise HouseholdError("provider order does not establish one delivery date")
                delivery = delivery_values[0]
                try:
                    canonical_delivery = date.fromisoformat(delivery).isoformat()
                except ValueError as exc:
                    raise HouseholdError("provider returned an invalid delivery date") from exc
                if canonical_delivery != delivery:
                    raise HouseholdError("provider returned an invalid delivery date")
                automation_key = job.get("automation_key") or email_automation_key(job_provider, order_id)
                job["automation_key"] = automation_key
                local_today = self._household_today(state).isoformat()
                if delivery != local_today:
                    job["delivery_date"] = delivery
                    job["automation_protocol"] = 0
                    return {
                        "send": False, "reason": "delivery moved", "delivery_date": delivery,
                        "automation_key": automation_key,
                        "automation_update_required": True,
                        "cron_prompt": email_automation_prompt(job_provider, order_id, delivery, automation_key),
                        "automation_ack": email_automation_ack(job_provider, order_id, delivery, automation_key),
                    }
                period = menu_email_period(menu)
                claim_token = secrets.token_urlsafe(18)
                job["status"] = "claimed"
                job["claim_token"] = claim_token
                job["claim_expires_at"] = (now() + EMAIL_CLAIM_LEASE).isoformat()
                result = {
                    "send": False,
                    "claim": True,
                    "provider": job_provider,
                    "order_id": order_id,
                    "claim_token": claim_token,
                    "mark_sent_after_success": True,
                    "next": f"Call begin_send with provider={job_provider}, this exact order_id and claim_token. Invoke the sender only with the payload returned when dispatch=true. After confirmed success call mark_sent with provider={job_provider}. On a definite no-send failure only call release with provider={job_provider}; leave uncertain post-dispatch outcomes locked.",
                }
                return result
        if action == "begin_send":
            order_id = safe_order_id(request.get("order_id"))
            claim_token = request.get("claim_token")
            if not isinstance(claim_token, str) or not claim_token:
                raise HouseholdError("the email claim_token is required")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"claimed"})
                if len(jobs) != 1 or not secrets.compare_digest(str(jobs[0].get("claim_token") or ""), claim_token):
                    raise HouseholdError("email claim_token does not match a claimed job")
                pending_cancellation = state.get("pending_cancellation")
                if email_job_provider(jobs[0]) == self.provider and isinstance(pending_cancellation, Mapping) and pending_cancellation.get("order_id") == order_id:
                    raise HouseholdError("order cancellation is pending; do not send its recipe email")
                try:
                    expires_at = datetime.fromisoformat(str(jobs[0].get("claim_expires_at") or ""))
                except ValueError as exc:
                    raise HouseholdError("email claim is invalid; request due again") from exc
                if expires_at.tzinfo is None or now() >= expires_at:
                    jobs[0]["status"] = "pending"
                    jobs[0].pop("claim_token", None)
                    jobs[0].pop("claim_expires_at", None)
                    raise HouseholdError("email claim expired before dispatch; request due again")
                menu = jobs[0].get("menu_snapshot")
                recipient = jobs[0].get("recipient_snapshot")
                if not isinstance(menu, Mapping) or menu.get("order_id") != order_id or not isinstance(recipient, str) or not recipient.strip():
                    raise HouseholdError("claimed email is not bound to one exact menu and recipient")
                period = menu_email_period(menu)
                payload = {
                    "dispatch": True, "send": True, "recipient": recipient,
                    "subject": jobs[0].get("subject") or f"Ukesmeny og oppskrifter – {period}",
                    "html": jobs[0].get("html") or menu_email_html(menu),
                    "provider": email_job_provider(jobs[0]), "order_id": order_id, "claim_token": claim_token,
                }
                if self.email_automation_profile:
                    payload["automation_environment"] = {"HERMES_WORKSPACE_AUTOMATION_PROFILE": self.email_automation_profile}
                if len(json.dumps({"ok": True, "result": payload}, ensure_ascii=True).encode()) > MAX_REQUEST - 1_024:
                    raise HouseholdError("claimed email cannot fit the meal concierge response transport")
                jobs[0]["status"] = "sending"
                jobs[0].pop("claim_expires_at", None)
                jobs[0]["dispatch_started_at"] = now().isoformat()
            return payload
        if action == "mark_sent":
            order_id = safe_order_id(request.get("order_id"))
            claim_token = request.get("claim_token")
            if not isinstance(claim_token, str) or not claim_token:
                raise HouseholdError("the email claim_token is required")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"sending"})
                if len(jobs) != 1:
                    raise HouseholdError("email is not claimed for sending")
                if not secrets.compare_digest(str(jobs[0].get("claim_token") or ""), claim_token):
                    raise HouseholdError("email claim_token does not match")
                jobs[0]["status"] = "sent"
                jobs[0]["sent_at"] = now().isoformat()
                jobs[0].pop("claim_token", None)
                jobs[0].pop("dispatch_started_at", None)
                jobs[0].pop("html", None)
                jobs[0].pop("menu_snapshot", None)
                if email_job_provider(jobs[0]) == self.provider:
                    self._prune_order_snapshots(state)
            return {"sent": True}
        if action == "release":
            order_id = safe_order_id(request.get("order_id"))
            claim_token = request.get("claim_token")
            if not isinstance(claim_token, str) or not claim_token:
                raise HouseholdError("the email claim_token is required")
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id, {"claimed", "sending"})
                if len(jobs) != 1 or not secrets.compare_digest(str(jobs[0].get("claim_token") or ""), claim_token):
                    raise HouseholdError("email claim_token does not match a claimed or sending job")
                jobs[0]["status"] = "pending"
                jobs[0].pop("claim_token", None)
                jobs[0].pop("claim_expires_at", None)
                jobs[0].pop("dispatch_started_at", None)
            return {"released": True}
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
