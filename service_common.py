"""Shared meal-service values and pure normalization/rendering helpers."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import html
import json
import math
import re
import socket
import struct
from typing import Any, Mapping
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from oda_browser import delivery_signature as oda_delivery_signature
from core import HouseholdError, cart_summary, validate_delivery_slot
from recipes import RecipeError, normalize_source_url, validate_week

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

def expired_awaiting_confirmation(value: Any, instant: datetime | None = None) -> bool:
    if not isinstance(value, Mapping) or value.get("status") != "awaiting_confirmation":
        return False
    try:
        expires_at = datetime.fromisoformat(str(value.get("expires_at") or ""))
    except ValueError:
        return False
    return expires_at.tzinfo is not None and (instant or now()) >= expires_at

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
    return provider if isinstance(provider, str) and provider in {"oda", "meny", "mathem"} else None

def email_automation_key(provider: str, order_id: str) -> str:
    if provider not in {"oda", "meny", "mathem"}:
        raise HouseholdError("email provider is invalid")
    order_id = safe_order_id(order_id)
    return f"meal-concierge-email-{hashlib.sha256(f'{provider}:{order_id}'.encode()).hexdigest()[:16]}"

def email_automation_prompt(provider: str, order_id: str, delivery_date: str, automation_key: str) -> str:
    if provider not in {"oda", "meny", "mathem"}:
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
    if isinstance(menu.get("batch"), Mapping):
        batch = menu["batch"]
        prepared = batch["prepared_portions"]
        consumed = batch["consumed_at_source"]
        parts.append(f"<p><strong>Planlagt batch:</strong> {escape(str(prepared['numerator'])+'/'+str(prepared['denominator']))} porsjoner totalt, "
                     f"{escape(str(consumed['numerator'])+'/'+str(consumed['denominator']))} ved kildemåltidet. Oppskriften nedenfor viser grunnporsjonene. "
                     "Restemåltidene er planlagte avhengigheter, ikke bekreftet beholdning eller garanti for mattrygghet.</p>")
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
            raise HouseholdError(f"{provider.upper()} supports cart_ready scheduling; checkout continues manually in the browser")
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
