"""Small browser adapter for MENY product search, recipes and cart."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlparse

from core import CancellationPreconditionError, CheckoutPreconditionError, HouseholdError, cart_summary


BASE_URL = "https://meny.no"
STORE_URL = f"{BASE_URL}/varer"
CHECKOUT_URL = f"{BASE_URL}/kassen"
ORDERS_URL = f"{BASE_URL}/profil/nettbutikk#/bestillinger"
ORDER_PATH = re.compile(r"/profil/nettbutikk/bestilling/(\d{1,20})")
PRODUCT_PATH = re.compile(r"/varer/(?!kampanjer/)[A-Za-z0-9._~%/-]+-\d{4,14}")
DELIVERY_SLOT = re.compile(
    r"^(?:fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kr\s+"
    r"fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kroner,\s*)?"
    r"(?P<day>0?[1-9]|[12]\d|3[01])\.\s*"
    r"(?P<month>januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember)\s+"
    r"klokka\s+(?P<start_hour>[01]?\d|2[0-3]):(?P<start_minute>[0-5]\d)\s+"
    r"til\s+(?P<end_hour>[01]?\d|2[0-3]):(?P<end_minute>[0-5]\d)$",
    re.IGNORECASE,
)
MENY_DELIVERY_WINDOW = re.compile(
    r"^(?P<weekday>man(?:dag)?|tir(?:sdag)?|ons(?:dag)?|tor(?:sdag)?|fre(?:dag)?|lør(?:dag)?|søn(?:dag)?)\s+"
    r"(?P<day>[1-9]|[12]\d|3[01])\.\s+"
    r"(?P<month>jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|"
    r"aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\.?\s+"
    r"kl\.\s+(?P<start>(?:[01]\d|2[0-3]):[0-5]\d)[-–](?P<end>(?:[01]\d|2[0-3]):[0-5]\d)$",
    re.IGNORECASE,
)
MENY_MONTH_NUMBERS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "okt": 10,
    "nov": 11,
    "des": 12,
}
MENY_WEEKDAY_NAMES = ("mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag")
MENY_MONTH_NAMES = (
    "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
)
CHECKOUT_DELIVERY_BINDING_JS = r"""
  const deliveryPattern = /^(?:man(?:dag)?|tir(?:sdag)?|ons(?:dag)?|tor(?:sdag)?|fre(?:dag)?|lør(?:dag)?|søn(?:dag)?)\s+(?:[1-9]|[12]\d|3[01])\.\s+(?:jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\.?\s+kl\.\s+(?:[01]\d|2[0-3]):[0-5]\d[-–](?:[01]\d|2[0-3]):[0-5]\d$/i;
  const deliveryHeadings = [...root.querySelectorAll('h2,h3')].filter(visible).filter(x => norm(x.innerText) === 'Dato og tid');
  let deliveryBinding = null;
  if (deliveryHeadings.length === 1) {
    let node = deliveryHeadings[0].parentElement;
    for (let depth=0; node && node !== root && depth<5; depth++, node=node.parentElement) {
      const editControls = [...node.querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Endre dato og tid');
      if (editControls.length === 0) continue;
      const deliveryValues = [...node.querySelectorAll('*')].filter(visible).filter(x => deliveryPattern.test(norm(x.innerText))).filter(x => ![...x.children].some(child => visible(child) && norm(child.innerText) === norm(x.innerText)));
      deliveryBinding = editControls.length === 1 && deliveryValues.length === 1
        ? {root: node, display: norm(deliveryValues[0].innerText)}
        : {root: null, display: null};
      break;
    }
  }
"""
DEFAULT_BROWSER_ARGS = "--disable-gpu,--disable-quic"
MENY_READ_TIMEOUT = 110
MENY_CART_TIMEOUT = 240
MENY_ORDER_TIMEOUT = 240
MAX_CART_CLICKS = 2
MENY_VIEWPORT = (1280, 900)


class _BrowserTransportError(HouseholdError):
    pass


def normalize_browser_cdp(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    parsed = urlparse(str(value).strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise HouseholdError("MENY browser CDP URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise HouseholdError("MENY browser CDP must be a loopback HTTP URL with an explicit port")
    return f"http://{parsed.hostname}:{port}"


def normalize_product_ref(value: Any) -> str:
    reference = str(value or "").strip()
    parsed = urlparse(reference)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != "meny.no" or parsed.query or parsed.fragment:
            raise HouseholdError("MENY product_id must be a meny.no product URL or path")
        reference = parsed.path
    if (
        len(reference) > 512
        or PRODUCT_PATH.fullmatch(reference) is None
        or ".." in reference
        or "//" in reference
    ):
        raise HouseholdError("MENY product_id is invalid; use the exact path returned by product search")
    return reference


def meny_delivery_window_identity(value: Any) -> tuple[str, int, str, str, str]:
    if not isinstance(value, str):
        raise HouseholdError("MENY delivery window is invalid")
    display = " ".join(value.split())
    match = MENY_DELIVERY_WINDOW.fullmatch(display)
    if match is None:
        raise HouseholdError("MENY delivery window is invalid")
    day = int(match["day"])
    month = match["month"].casefold()[:3]
    try:
        date(2000, MENY_MONTH_NUMBERS[month], day)
    except (KeyError, ValueError) as exc:
        raise HouseholdError("MENY delivery window is invalid") from exc
    if match["start"] >= match["end"]:
        raise HouseholdError("MENY delivery window is invalid")
    return (
        match["weekday"].casefold()[:3],
        day,
        month,
        match["start"],
        match["end"],
    )


def meny_selected_delivery(value: Any) -> dict[str, Any] | None:
    """Return one provider-selected slot as a checkout-comparable delivery window."""

    if not isinstance(value, list):
        raise HouseholdError("MENY delivery slots are invalid")
    selected = [slot for slot in value if isinstance(slot, Mapping) and slot.get("selected") is True]
    if not selected:
        return None
    if len(selected) != 1:
        raise HouseholdError("MENY selected delivery slot is ambiguous")
    slot = selected[0]
    slot_id = str(slot.get("slot_id") or "").strip()
    match = DELIVERY_SLOT.fullmatch(slot_id)
    try:
        slot_date = date.fromisoformat(str(slot.get("date") or ""))
    except ValueError as exc:
        raise HouseholdError("MENY selected delivery slot is invalid") from exc
    start = str(slot.get("start") or "")
    end = str(slot.get("end") or "")
    month = match["month"].casefold() if match is not None else ""
    if (
        match is None
        or slot_date.day != int(match["day"])
        or slot_date.month != MENY_MONTH_NUMBERS.get(month[:3])
        or start != f"{int(match['start_hour']):02d}:{match['start_minute']}"
        or end != f"{int(match['end_hour']):02d}:{match['end_minute']}"
        or start >= end
    ):
        raise HouseholdError("MENY selected delivery slot is invalid")
    display = (
        f"{MENY_WEEKDAY_NAMES[slot_date.weekday()]} {slot_date.day}. "
        f"{MENY_MONTH_NAMES[slot_date.month - 1]} kl. {start}-{end}"
    )
    meny_delivery_window_identity(display)
    return {"slot_id": slot_id, "display": display}


def normalize_delivery_slot_ref(value: Any) -> tuple[str, str]:
    slot_id = str(value or "").strip()
    if not slot_id or len(slot_id) > 500:
        raise HouseholdError("MENY delivery slot_id is required")
    match = DELIVERY_SLOT.fullmatch(slot_id)
    if match is None:
        raise HouseholdError("MENY delivery slot_id is invalid; use the exact value returned by delivery list")
    suffix = (
        f"{int(match['day'])}. {match['month'].casefold()} klokka "
        f"{int(match['start_hour']):02d}:{match['start_minute']} til "
        f"{int(match['end_hour']):02d}:{match['end_minute']}"
    )
    return slot_id, suffix


def _vipps_dispatch_requests(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Mapping) or not isinstance(value.get("requests"), list):
        raise HouseholdError("MENY browser payment request log changed")
    result = []
    for request in value["requests"]:
        if not isinstance(request, Mapping):
            raise HouseholdError("MENY browser payment request log changed")
        if str(request.get("method") or "").upper() != "POST":
            continue
        parsed = urlparse(str(request.get("url") or ""))
        if parsed.scheme != "https":
            continue
        path = parsed.path.casefold()
        if parsed.hostname == "platform-rest-prod.ngdata.no" and path.startswith((
            "/order/", "/api/order/", "/api/payment/", "/api/vipps/", "/api/checkout/",
        )):
            result.append(request)
        elif parsed.hostname == "meny.no" and path.startswith((
            "/api/user/order", "/api/payment/", "/api/vipps/", "/api/checkout/",
        )):
            result.append(request)
    return result


def vipps_dispatch_attempted(value: Any) -> bool:
    """Return true when a provider payment/order POST crossed the browser boundary."""

    return bool(_vipps_dispatch_requests(value))


def vipps_dispatch_acknowledged(value: Any) -> bool:
    """Return true only when MENY has created the provider-side Vipps payment."""

    for request in _vipps_dispatch_requests(value):
        status = request.get("status")
        if not isinstance(status, bool) and isinstance(status, int) and 200 <= status < 300:
            return True
    return False


def meny_order_search_completed(value: Any) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("requests"), list):
        raise HouseholdError("MENY browser order request log changed")
    for request in value["requests"]:
        if not isinstance(request, Mapping):
            raise HouseholdError("MENY browser order request log changed")
        status = request.get("status")
        parsed = urlparse(str(request.get("url") or ""))
        if (
            str(request.get("method") or "").upper() == "GET"
            and not isinstance(status, bool)
            and isinstance(status, int)
            and 200 <= status < 300
            and parsed.scheme == "https"
            and parsed.hostname == "platform-rest-prod.ngdata.no"
            and parsed.path.casefold().startswith("/api/order/search/")
        ):
            return True
    return False


def meny_delivery_reservation_acknowledged(value: Any) -> bool:
    """Require both the delivery reservation and household selection writes."""

    if not isinstance(value, Mapping) or not isinstance(value.get("requests"), list):
        raise HouseholdError("MENY browser delivery request log changed")
    reservation = False
    household = False
    for request in value["requests"]:
        if not isinstance(request, Mapping):
            raise HouseholdError("MENY browser delivery request log changed")
        status = request.get("status")
        parsed = urlparse(str(request.get("url") or ""))
        if (
            str(request.get("method") or "").upper() == "POST"
            and not isinstance(status, bool)
            and isinstance(status, int)
            and 200 <= status < 300
            and parsed.scheme == "https"
            and parsed.hostname == "api.ngdata.no"
            and parsed.path == "/sylinder/hentevinduer/reservasjoner/v1/api"
        ):
            reservation = True
        if (
            str(request.get("method") or "").upper() == "PUT"
            and not isinstance(status, bool)
            and isinstance(status, int)
            and 200 <= status < 300
            and parsed.scheme == "https"
            and parsed.hostname == "platform-rest-prod.ngdata.no"
            and re.fullmatch(r"/api/extended-user/\d{1,20}/household", parsed.path) is not None
        ):
            household = True
    return reservation and household


def normalize_cart_snapshot(value: Any) -> dict[str, Any]:
    """Reject incomplete or ambiguous MENY cart DOM snapshots."""

    if not isinstance(value, Mapping) or value.get("authenticated") is not True:
        raise HouseholdError("MENY login is required in the configured browser profile")
    if (
        value.get("ready") is not True
        or isinstance(value.get("root_count"), bool)
        or value.get("root_count") != 1
    ):
        raise HouseholdError("MENY cart did not finish rendering")
    items = value.get("items")
    if not isinstance(items, list):
        raise HouseholdError("MENY cart did not finish rendering")
    item_count = value.get("item_root_count")
    control_count = value.get("control_count")
    if (
        isinstance(item_count, bool)
        or not isinstance(item_count, int)
        or isinstance(control_count, bool)
        or not isinstance(control_count, int)
        or item_count != control_count
        or item_count != len(items)
    ):
        raise HouseholdError("MENY cart item controls are ambiguous")
    empty = value.get("empty")
    if not isinstance(empty, bool) or empty != (len(items) == 0):
        raise HouseholdError("MENY cart empty state is ambiguous")
    expected_totals = 0 if empty else 1
    if isinstance(value.get("total_count"), bool) or value.get("total_count") != expected_totals:
        raise HouseholdError("MENY cart total is ambiguous")
    expected_subtotals = 0 if empty else 1
    subtotal_count = value.get("subtotal_count")
    subtotal = value.get("subtotal")
    if isinstance(subtotal_count, bool) or subtotal_count != expected_subtotals:
        raise HouseholdError("MENY cart subtotal is ambiguous")
    if empty:
        if subtotal is not None:
            raise HouseholdError("MENY cart subtotal is invalid")
    elif (
        isinstance(subtotal, bool)
        or not isinstance(subtotal, (int, float))
        or not math.isfinite(float(subtotal))
        or float(subtotal) <= 0
    ):
        raise HouseholdError("MENY cart subtotal is invalid")

    normalized: list[dict[str, Any]] = []
    count = 0
    for item in items:
        if not isinstance(item, Mapping):
            raise HouseholdError("MENY cart item is invalid")
        product_id = normalize_product_ref(item.get("product_id"))
        name = str(item.get("name") or "").strip()
        quantity = item.get("quantity")
        price = item.get("price")
        if not name or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise HouseholdError("MENY cart item is invalid")
        if price is not None and (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(float(price))
            or float(price) < 0
        ):
            raise HouseholdError("MENY cart item price is invalid")
        normalized.append({
            "product_id": product_id,
            "name": name,
            "quantity": quantity,
            "price": float(price) if price is not None else None,
        })
        count += quantity

    total = value.get("total")
    if (
        isinstance(total, bool)
        or not isinstance(total, (int, float))
        or not math.isfinite(float(total))
        or float(total) < 0
        or isinstance(value.get("count"), bool)
        or value.get("count") != count
        or (empty and float(total) != 0)
        or (not empty and float(total) <= 0)
    ):
        raise HouseholdError("MENY cart total is invalid")
    delivery_count = value.get("delivery_count")
    delivery = value.get("delivery")
    if isinstance(delivery_count, bool) or delivery_count not in {0, 1}:
        raise HouseholdError("MENY cart delivery is ambiguous")
    if delivery_count == 0:
        if delivery is not None:
            raise HouseholdError("MENY cart delivery is ambiguous")
        normalized_delivery = None
    else:
        if not isinstance(delivery, Mapping) or set(delivery) != {"display"}:
            raise HouseholdError("MENY cart delivery is invalid")
        raw_display = delivery.get("display")
        if not isinstance(raw_display, str):
            raise HouseholdError("MENY cart delivery is invalid")
        display = " ".join(raw_display.split())
        try:
            meny_delivery_window_identity(display)
        except HouseholdError as exc:
            raise HouseholdError("MENY cart delivery is invalid") from exc
        normalized_delivery = {"slot_id": None, "display": display}
    return {"items": normalized, "count": count, "total": float(total), "delivery": normalized_delivery}


def normalize_checkout_payment_snapshot(value: Any) -> dict[str, Any]:
    """Accept only one complete, enabled home-delivery Vipps summary."""

    required = {"ready", "authenticated", "vipps_checked", "home_delivery", "submit_enabled", "total", "delivery", "submit_controls"}
    if not isinstance(value, Mapping) or set(value) != required or any(value.get(key) is not True for key in ("ready", "authenticated", "vipps_checked", "home_delivery", "submit_enabled")):
        raise HouseholdError("MENY checkout page changed")
    total = value.get("total")
    delivery = " ".join(str(value.get("delivery") or "").split())
    submit_controls = value.get("submit_controls")
    try:
        normalized_total = float(total) if type(total) in {int, float} else math.nan
    except (TypeError, ValueError, OverflowError):
        normalized_total = math.nan
    if (
        not math.isfinite(normalized_total)
        or normalized_total <= 0
        or not isinstance(value.get("delivery"), str)
        or not delivery
        or len(delivery) > 1000
        or type(submit_controls) is not int
        or submit_controls != 1
    ):
        raise HouseholdError("MENY checkout page changed")
    try:
        meny_delivery_window_identity(delivery)
    except HouseholdError as exc:
        raise HouseholdError("MENY checkout page changed") from exc
    return {"total": normalized_total, "delivery": delivery}


def meny_checkout_reviews_match(expected: Any, observed: Any) -> bool:
    """Compare only the checkout fields that bind the protected payment."""

    def identity(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, Mapping) or not isinstance(value.get("summary"), Mapping):
            return None
        summary = value["summary"]
        items = summary.get("items")
        order_lines = summary.get("order_lines")
        delivery = summary.get("delivery")
        total = summary.get("total")
        count = summary.get("count")
        if (
            not isinstance(items, list)
            or not items
            or not isinstance(order_lines, list)
            or not order_lines
            or not isinstance(delivery, Mapping)
            or isinstance(total, bool)
            or not isinstance(total, (int, float))
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or summary.get("payment") != "vipps"
            or value.get("payment") != "vipps"
            or isinstance(value.get("submit_controls"), bool)
            or value.get("submit_controls") != 1
            or not (
                value.get("target_order_id") is None
                or isinstance(value.get("target_order_id"), str)
            )
            or not (
                value.get("target_order_code") is None
                or isinstance(value.get("target_order_code"), str)
            )
        ):
            return None
        try:
            total_cents = float(total) * 100
        except (OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(total_cents) or round(total_cents) < 1:
            return None
        for item in [*items, *order_lines]:
            if (
                not isinstance(item, Mapping)
                or isinstance(item.get("quantity"), bool)
                or not isinstance(item.get("quantity"), int)
                or item.get("quantity") < 1
            ):
                return None
        try:
            item_identity = sorted(
                (normalize_product_ref(item.get("product_id")), item["quantity"])
                for item in items
            )
            line_identity = sorted(
                (
                    normalize_product_ref(item.get("product_id")),
                    " ".join(str(item.get("identity") or "").split()),
                    item["quantity"],
                )
                for item in order_lines
            )
            delivery_identity = meny_delivery_window_identity(delivery.get("display"))
        except (HouseholdError, TypeError, ValueError):
            return None
        if (
            any(not item_name for _, item_name, _ in line_identity)
            or sum(quantity for _, quantity in item_identity) != count
        ):
            return None
        return {
            "items": item_identity,
            "order_lines": line_identity,
            "count": count,
            "total_cents": round(total_cents),
            "delivery": delivery_identity,
            "summary_payment": summary.get("payment"),
            "payment": value.get("payment"),
            "submit_controls": value.get("submit_controls"),
            "target_order_id": value.get("target_order_id"),
            "target_order_code": value.get("target_order_code"),
        }

    expected_identity = identity(expected)
    return expected_identity is not None and expected_identity == identity(observed)


class MenyClient:
    """Expose the small provider interface used by the household service."""

    def __init__(
        self,
        *,
        instance: str,
        binary: Path | str,
        executable: Path | str,
        profile: Path | str,
        home: Path | str,
        socket_directory: Path | str,
        uid: int,
        gid: int,
        cdp: str | None = None,
        vipps_phone_number: str | None = None,
    ):
        self.instance = instance
        self.binary = Path(binary)
        self.executable = Path(executable)
        self.profile = Path(profile)
        self.home = Path(home)
        self.socket_directory = Path(socket_directory)
        self.uid = uid
        self.gid = gid
        self.cdp = normalize_browser_cdp(cdp)
        self.vipps_phone_number = vipps_phone_number
        self._cdp_primed = False
        self._viewport_primed = False
        self.session = f"hermes-meal-planner-meny-{instance}"
        self.lock = threading.Lock()
        self.deadline: float | None = None
        self.recovery_allowed = False
        self._recovery_consumed = False
        self._shell_target: str | None = None

    def probe(self, *, deadline: float | None = None, allow_recovery: bool = False) -> dict[str, Any]:
        with self._locked_operation(MENY_READ_TIMEOUT, deadline, allow_recovery=allow_recovery):
            self._require_login()
            return {
                "status": "ready",
                "protocol_version": "browser-v1",
                "server": {"name": "MENY website"},
                "tool_count": 11,
                "provider": "meny",
            }

    def _require_login(self) -> None:
        self._open(STORE_URL)
        result: dict[str, Any] = {}
        for attempt in range(2):
            for _ in range(120):
                result = self._eval(r"""
(() => {
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  return JSON.stringify({
    ready: location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash && Boolean(document.querySelector('main')),
    authenticated: authenticated.length === 1
  });
})()
""")
                if result.get("ready") is True and result.get("authenticated") is True:
                    return
                self._sleep(0.25)
            if attempt == 0:
                self._invoke("reload")
        if result.get("ready") is not True:
            raise HouseholdError("MENY website is unavailable")
        raise HouseholdError("MENY login is required in the configured browser profile")

    def _assert_authenticated(self) -> None:
        result = self._eval(r"""
(() => {
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  return JSON.stringify({authenticated: authenticated.length === 1});
})()
""")
        if result.get("authenticated") is not True:
            raise HouseholdError("MENY login is required in the configured browser profile")

    def call(self, tool: str, arguments: Mapping[str, Any], *, deadline: float | None = None, allow_recovery: bool = False) -> dict[str, Any]:
        supported = {
            "product_search", "recipe_search", "likely_to_buy", "get_cart", "manipulate_cart",
            "get_delivery_slots", "select_delivery_slot", "get_orders", "get_order", "order_tracking",
        }
        if tool not in supported:
            raise HouseholdError("MENY operation is not supported")
        timeout = MENY_CART_TIMEOUT if tool == "manipulate_cart" else MENY_ORDER_TIMEOUT if tool in {"get_delivery_slots", "select_delivery_slot", "get_orders", "get_order", "order_tracking"} else MENY_READ_TIMEOUT
        with self._locked_operation(timeout, deadline, allow_recovery=allow_recovery):
            self._require_login()
            if tool == "product_search":
                queries = arguments.get("queries")
                if not isinstance(queries, list) or len(queries) != 1:
                    raise HouseholdError("MENY product search needs one query")
                limit = self._limit(arguments.get("size", 5))
                return self._search(str(queries[0] or ""), limit, "products")
            if tool == "recipe_search":
                limit = self._limit(arguments.get("size", 5))
                return self._search(str(arguments.get("query") or ""), limit, "recipes")
            if tool == "likely_to_buy":
                return {
                    "provider": "meny",
                    "available": False,
                    "products": [],
                    "message": "MENY's personalised often-bought list is not available through the browser flow.",
                }
            if tool == "get_cart":
                return self._read_cart()
            if tool == "manipulate_cart":
                return self._change_cart(arguments)
            if tool == "get_delivery_slots":
                return self._delivery_slots(str(arguments.get("delivery_date") or ""))
            if tool == "select_delivery_slot":
                return self._select_delivery_slot(arguments.get("delivery_slot_id"))
            if tool == "get_orders":
                return self._get_orders(self._limit(arguments.get("size", 10)))
            order_id = self._order_id(arguments.get("order_number"))
            order = self._get_order(order_id)
            if tool == "get_order":
                return order
            return {"order_id": order_id, "status": order["status"]}

    @staticmethod
    def _limit(value: Any) -> int:
        if isinstance(value, bool):
            raise HouseholdError("search limit is invalid")
        try:
            limit = int(value)
        except (TypeError, ValueError) as exc:
            raise HouseholdError("search limit is invalid") from exc
        if not 1 <= limit <= 20:
            raise HouseholdError("search limit must be between 1 and 20")
        return limit

    def _search(self, query: str, limit: int, kind: str) -> dict[str, Any]:
        query = " ".join(query.split())
        if not query or len(query) > 200:
            raise HouseholdError("MENY search query is missing or too long")
        expanded = {"products": "products", "recipes": "recipes"}.get(kind)
        if expanded is None:
            raise HouseholdError("MENY search kind is invalid")
        self._open(f"{BASE_URL}/sok?{urlencode({'query': query})}")
        if kind == "recipes":
            self._select_recipe_results(query)
        result: dict[str, Any] = {}
        for attempt in range(2):
            for _ in range(20):
                result = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const {query, expanded, heading, kind} = EXPECTED;
  const parameters = new URLSearchParams(location.search);
  const keys = [...parameters.keys()];
  const queryValues = parameters.getAll('query');
  const expandedValues = parameters.getAll('expanded');
  const identity = location.origin === 'https://meny.no' && location.pathname === '/sok' &&
    keys.every(key => key === 'query' || key === 'expanded') &&
    queryValues.length === 1 && queryValues[0] === query && expandedValues.length <= 1;
  const route = identity && keys.length === 2 && expandedValues.length === 1 && expandedValues[0] === expanded;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const mains = [...document.querySelectorAll('main')].filter(visible);
  const roots = mains.length === 1 ? [...mains[0].querySelectorAll('.ws-search-result-full')].filter(visible) : [];
  const state = roots.length === 1 ? roots[0].closest('.ws-search-result') : null;
  const stateRoots = state ? [...state.querySelectorAll(':scope > .ws-search-result-full')].filter(visible) : [];
  const queryHeaders = state ? [...state.querySelectorAll(':scope > .ws-search-result__header')] : [];
  const visibleQueryHeaders = queryHeaders.filter(visible);
  const queryHeaderElements = queryHeaders.length === 1 ? [...queryHeaders[0].querySelectorAll(':scope > h2.ws-search-result__title')] : [];
  const visibleQueryHeaderElements = queryHeaderElements.filter(visible);
  const queryHeadings = visibleQueryHeaderElements.filter(x => norm(x.innerText) === `Resultater for "${query}"`);
  const queryHeadingValid = queryHeaders.length === 0 || (queryHeaders.length === 1 && visibleQueryHeaders.length === 1 && queryHeaderElements.length === 1 && visibleQueryHeaderElements.length === 1 && queryHeadings.length === 1);
  const headings = roots.length === 1 ? [...roots[0].querySelectorAll(':scope > h2')].filter(visible).filter(x => norm(x.innerText) === heading) : [];
  if (!route || authenticated.length !== 1 || mains.length !== 1 || roots.length !== 1 || stateRoots.length !== 1 || stateRoots[0] !== roots[0] || !queryHeadingValid || headings.length !== 1) {
    return JSON.stringify({ready:false, identity, route, authenticated:authenticated.length === 1, root_count:roots.length, state_root_count:stateRoots.length, query_header_count:queryHeaders.length, query_count:queryHeadings.length, heading_count:headings.length, products:[], recipes:[]});
  }
  const root = roots[0];
  const productPattern = /^\/varer\/(?!kampanjer\/)[A-Za-z0-9._~%/-]+-\d{4,14}$/;
  const products = [];
  const seenProducts = new Set();
  const productCards = [...root.querySelectorAll('li.ws-product-list-vertical__item')].filter(visible);
  for (const card of productCards) {
    const paths = new Set([...card.querySelectorAll('a[href]')].map(anchor => new URL(anchor.href, location.origin)).filter(url => url.origin === location.origin && !url.search && !url.hash && productPattern.test(url.pathname)).map(url => url.pathname));
    if (paths.size !== 1) return JSON.stringify({ready:false, identity:true, route:true, authenticated:true, root_count:1, heading_count:1, products:[], recipes:[]});
    const path = [...paths][0];
    const visiblePaths = [...card.querySelectorAll('a[href]')].filter(visible).map(anchor => new URL(anchor.href, location.origin)).filter(url => url.origin === location.origin && !url.search && !url.hash && productPattern.test(url.pathname) && url.pathname === path);
    if (visiblePaths.length === 0) return JSON.stringify({ready:false, identity:true, route:true, authenticated:true, root_count:1, heading_count:1, products:[], recipes:[]});
    if (seenProducts.has(path)) return JSON.stringify({ready:false, identity:true, route:true, authenticated:true, root_count:1, heading_count:1, products:[], recipes:[]});
    seenProducts.add(path);
    const name = norm(card.querySelector('h3')?.innerText);
    if (!name) return JSON.stringify({ready:false, identity:true, route:true, authenticated:true, root_count:1, heading_count:1, products:[], recipes:[]});
    const packageText = norm(card.querySelector('[class*="product__subtitle"]')?.innerText || card.querySelector('h3')?.parentElement?.innerText).replace(name, '').trim();
    const price = norm(card.querySelector('strong')?.innerText).match(/\d+(?:[ .]\d{3})*,\d{2}\s*kr/i)?.[0] || null;
    const offer = norm(card.querySelector('a[href*="/kampanjer/"]')?.innerText) || null;
    products.push({product_id:path, product_url:location.origin + path, name, package:packageText || null, price, offer});
  }
  const recipes = [];
  const seenRecipes = new Set();
  const recipeCards = [...root.querySelectorAll('li.ws-search-item--type-recipe')].filter(visible);
  for (const card of recipeCards) {
    const paths = new Set([...card.querySelectorAll('a[href]')].map(anchor => new URL(anchor.href, location.origin)).filter(url => url.origin === location.origin && !url.search && !url.hash && /^\/oppskrifter\/[A-Za-z0-9._~%/-]+$/.test(url.pathname)).map(url => url.pathname.replace(/\/$/, '')));
    if (paths.size !== 1) return JSON.stringify({ready:false, identity:true, route:true, authenticated:true, root_count:1, heading_count:1, products:[], recipes:[]});
    const path = [...paths][0];
    const visiblePaths = [...card.querySelectorAll('a[href]')].filter(visible).map(anchor => new URL(anchor.href, location.origin)).filter(url => url.origin === location.origin && !url.search && !url.hash && url.pathname.replace(/\/$/, '') === path);
    if (visiblePaths.length === 0) return JSON.stringify({ready:false, identity:true, route:true, authenticated:true, root_count:1, heading_count:1, products:[], recipes:[]});
    if (seenRecipes.has(path)) return JSON.stringify({ready:false, identity:true, route:true, authenticated:true, root_count:1, heading_count:1, products:[], recipes:[]});
    seenRecipes.add(path);
    const name = norm(card.querySelector('h3')?.innerText);
    if (!name) return JSON.stringify({ready:false, identity:true, route:true, authenticated:true, root_count:1, heading_count:1, products:[], recipes:[]});
    recipes.push({recipe_id:path, recipe_url:location.origin + path, name, summary:norm(card.innerText)});
  }
  const emptyText = kind === 'products'
    ? `Ingen treff på ${query} med valgt filtrering. Prøv å endre på filtreringsvalgene dine, eller gjør et nytt søk.`
    : `Ingen treff på ${query}`;
  const empty = [...root.querySelectorAll(':scope > p.ws-search-result-full__empty')].filter(visible).filter(x => norm(x.innerText) === emptyText);
  const cards = kind === 'products' ? productCards : recipeCards;
  const ready = (cards.length > 0 && empty.length === 0) || (cards.length === 0 && empty.length === 1);
  return JSON.stringify({ready, identity:true, route:true, authenticated:true, root_count:1, state_root_count:1, query_header_count:queryHeaders.length, query_count:queryHeadings.length, heading_count:1, products, recipes});
})()
""".replace("EXPECTED", json.dumps({
                    "query": query,
                    "expanded": expanded,
                    "heading": "Varer" if kind == "products" else "Oppskrifter",
                    "kind": kind,
                }, ensure_ascii=False)))
                if result.get("identity") is not True:
                    raise HouseholdError("MENY search route changed")
                if result.get("authenticated") is not True:
                    self._sleep(0.25)
                    continue
                if result.get("ready") is True:
                    break
                self._sleep(0.25)
            if result.get("ready") is True:
                break
            if attempt == 0:
                self._invoke("reload")
        if result.get("authenticated") is not True:
            self._require_login()
        query_header_count = result.get("query_header_count", result.get("query_count"))
        if (
            result.get("ready") is not True
            or result.get("root_count") != 1
            or result.get("state_root_count") != 1
            or (query_header_count, result.get("query_count")) not in {(0, 0), (1, 1)}
            or result.get("heading_count") != 1
        ):
            raise HouseholdError("MENY search results did not finish rendering")
        values = result[kind][:limit]
        return {"provider": "meny", "query": query, kind: values}

    def _select_recipe_results(self, query: str) -> None:
        state: dict[str, Any] = {}
        for attempt in range(2):
            for _ in range(20):
                state = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const query = EXPECTED;
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const parameters = new URLSearchParams(location.search);
  const keys = [...parameters.keys()];
  const queryValues = parameters.getAll('query');
  const expandedValues = parameters.getAll('expanded');
  const identity = location.origin === 'https://meny.no' && location.pathname === '/sok' &&
    keys.every(key => key === 'query' || key === 'expanded') &&
    queryValues.length === 1 && queryValues[0] === query && expandedValues.length <= 1;
  const route = identity && keys.length === 2 && expandedValues.length === 1 && expandedValues[0] === 'products';
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const mains = [...document.querySelectorAll('main')].filter(visible);
  const roots = mains.length === 1 ? [...mains[0].querySelectorAll('.ws-search-result-full')].filter(visible) : [];
  const result = roots.length === 1 ? roots[0].closest('.ws-search-result') : null;
  const resultRoots = result ? [...result.querySelectorAll(':scope > .ws-search-result-full')].filter(visible) : [];
  const queryHeadings = result ? [...result.querySelectorAll(':scope > .ws-search-result__header > h2.ws-search-result__title')].filter(visible).filter(x => norm(x.innerText) === `Resultater for "${query}"`) : [];
  const kindHeadings = roots.length === 1 ? [...roots[0].querySelectorAll(':scope > h2')].filter(visible).filter(x => norm(x.innerText) === 'Varer') : [];
  const radios = result ? [...result.querySelectorAll(':scope > .ws-search-result__header input[type="radio"]')].filter(x => x.value === 'recipes' && !x.disabled) : [];
  const labels = radios.length === 1 ? [...radios[0].labels].filter(visible).filter(x => /^Oppskrifter \(\d+\)$/.test(norm(x.innerText))) : [];
  const ready = route && authenticated.length === 1 && mains.length === 1 && roots.length === 1 && resultRoots.length === 1 && resultRoots[0] === roots[0] && queryHeadings.length === 1 && kindHeadings.length === 1 && radios.length === 1 && labels.length === 1;
  if (ready) labels[0].setAttribute('data-hermes-meal-planner-action', 'search-kind');
  return JSON.stringify({ready, identity, route, authenticated:authenticated.length === 1});
})()
""".replace("EXPECTED", json.dumps(query, ensure_ascii=False)))
                if state.get("identity") is not True:
                    raise HouseholdError("MENY search route changed")
                if state.get("authenticated") is not True:
                    self._sleep(0.25)
                    continue
                if state.get("ready") is True:
                    self._invoke("click", '[data-hermes-meal-planner-action="search-kind"]')
                    return
                self._sleep(0.25)
            if attempt == 0:
                self._invoke("reload")
        if state.get("authenticated") is not True:
            self._require_login()
        raise HouseholdError("MENY search results did not finish rendering")

    def _prepare_search(self) -> None:
        closed_cart = False
        for _ in range(20):
            state = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const searches = [...document.querySelectorAll('input[placeholder="Hva lurer du på?"]')].filter(visible);
  if (searches.length === 1) {
    searches[0].setAttribute('data-hermes-meal-planner-action', 'search');
    return JSON.stringify({ready:identity && authenticated.length === 1, identity, authenticated:authenticated.length === 1, action:'search'});
  }
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible).filter(x => [...x.querySelectorAll('button')].filter(visible).some(button => ['Til kassen','Fortsett'].includes(norm(button.innerText))));
  const closers = carts.length === 1 ? [...carts[0].querySelectorAll('button[aria-label="Lukk"]')].filter(visible) : [];
  if (!identity || authenticated.length !== 1 || carts.length !== 1 || closers.length !== 1) return JSON.stringify({ready:false, identity, authenticated:authenticated.length === 1});
  closers[0].setAttribute('data-hermes-meal-planner-action', 'close-cart');
  return JSON.stringify({ready:true, identity, authenticated:true, action:'close'});
})()
""")
            if state.get("identity") is not True:
                raise HouseholdError("MENY delivery route changed")
            if state.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if state.get("ready") is True and state.get("action") == "search":
                return
            if state.get("ready") is True and state.get("action") == "close" and not closed_cart:
                closed_cart = True
                self._invoke("click", '[data-hermes-meal-planner-action="close-cart"]')
                self._sleep(0.3)
                self._assert_authenticated()
                continue
            self._sleep(0.25)
        raise HouseholdError("MENY search is unavailable")

    def _change_cart(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        operations = arguments.get("operations")
        if not isinstance(operations, list) or not operations:
            raise HouseholdError("MENY cart change needs operations")
        order_change_code = arguments.get("order_change_code")
        if order_change_code is not None:
            order_change_code = str(order_change_code).strip()
            if not order_change_code or not re.fullmatch(r"[A-Za-z0-9-]{2,40}", order_change_code):
                raise HouseholdError("MENY order change identity is invalid")
        validated: list[tuple[str, int]] = []
        clicks = 0
        for operation in operations:
            if not isinstance(operation, Mapping):
                raise HouseholdError("MENY cart operations must be objects")
            product = normalize_product_ref(operation.get("productId", operation.get("product_id")))
            quantity = operation.get("quantity")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity == 0:
                raise HouseholdError("MENY cart quantity must be a non-zero integer")
            clicks += abs(quantity)
            if clicks > MAX_CART_CLICKS:
                raise HouseholdError(f"one MENY cart request can change at most {MAX_CART_CLICKS} units")
            validated.append((product, quantity))
        applied = 0
        try:
            for product, quantity in validated:
                for _ in range(abs(quantity)):
                    self._require_time(8)
                    self._change_one(product, 1 if quantity > 0 else -1, order_change_code=order_change_code)
                    applied += 1
            return self._read_settled_cart()
        except HouseholdError as exc:
            if applied:
                raise HouseholdError("MENY cart changed partially; read the cart and do not retry this request") from exc
            raise

    def _read_settled_cart(self) -> dict[str, Any]:
        snapshots: list[dict[str, Any]] = []
        for attempt in range(4):
            snapshots.append(self._read_cart())
            if attempt < 3:
                self._sleep(0.5)
        if snapshots[-2] == snapshots[-1]:
            return snapshots[-1]
        raise HouseholdError("MENY cart readback did not settle")

    def _change_one(self, product: str, delta: int, *, order_change_code: str | None = None) -> None:
        self._open(BASE_URL + product)
        self._assert_authenticated()
        before: dict[str, Any] = {}
        for _ in range(20):
            before = self._product_control("mark", delta, product)
            if before.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if before.get("ready") is True:
                break
            self._sleep(0.25)
        else:
            if delta < 0:
                self._remove_one_from_cart(product, order_change_code)
                return
            reason = "product control is unavailable"
            raise HouseholdError(f"MENY {reason}")
        previous = before.get("quantity")
        if isinstance(previous, bool) or not isinstance(previous, int) or previous < 0:
            raise HouseholdError("MENY product quantity is invalid")
        expected = previous + delta
        try:
            self._click_cart_control(product, str(before.get("label") or ""))
            self._resolve_order_route(order_change_code)
            observed = self._wait_for_quantity(expected, previous, product)
            if observed != expected:
                raise HouseholdError("MENY product quantity did not settle")
            self._assert_authenticated()
        except HouseholdError as exc:
            raise HouseholdError("MENY cart change is uncertain; read the cart and do not retry this request") from exc

    def _resolve_order_route(self, order_change_code: str | None) -> None:
        result = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const code = CODE;
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible).filter(x => /Vil du legge til varer på eksisterende bestilling\?/i.test(norm(x.innerText)));
  if (dialogs.length === 0) return JSON.stringify({dialog:false});
  if (dialogs.length !== 1) return JSON.stringify({dialog:true, ready:false});
  const root = dialogs[0], text = norm(root.innerText);
  const existing = [...root.querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Endre bestilling');
  const fresh = [...root.querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Start ny bestilling');
  if (existing.length !== 1 || fresh.length !== 1) return JSON.stringify({dialog:true, ready:false});
  const codes = [...text.matchAll(/\bbestilling\s+([A-Za-z0-9-]+)/gi)].map(x => x[1]);
  if (code !== null && (codes.length !== 1 || codes[0] !== code)) return JSON.stringify({dialog:true, ready:false});
  const target = code === null ? fresh[0] : existing[0];
  target.setAttribute('data-hermes-meal-planner-action', 'order-route');
  return JSON.stringify({dialog:true, ready:true, route:code === null ? 'new' : 'existing'});
})()
""".replace("CODE", json.dumps(order_change_code)))
        if result.get("dialog") is not True:
            return
        if result.get("ready") is not True:
            raise HouseholdError("MENY order routing prompt changed")
        self._invoke("click", '[data-hermes-meal-planner-action="order-route"]')
        self._sleep(0.5)
        remaining = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  return JSON.stringify({clear:[...document.querySelectorAll('[role="dialog"]')].filter(visible).filter(x => /Vil du legge til varer på eksisterende bestilling\?/i.test(norm(x.innerText))).length === 0});
})()
""")
        if remaining != {"clear": True}:
            raise HouseholdError("MENY order routing did not settle")

    def _remove_one_from_cart(self, product: str, order_change_code: str | None) -> None:
        cart = self._read_cart()
        matches = [item for item in cart.get("items", []) if item.get("product_id") == product]
        if len(matches) != 1 or isinstance(matches[0].get("quantity"), bool) or not isinstance(matches[0].get("quantity"), int):
            raise HouseholdError("MENY product is not in the cart")
        current = matches[0]["quantity"]
        if current < 1:
            raise HouseholdError("MENY product is not in the cart")
        dispatched = False

        def before_dispatch() -> None:
            nonlocal dispatched
            dispatched = True

        try:
            self._click_cart_remove_control(product, current, order_change_code, before_dispatch)
            observed = self._wait_for_cart_quantity(product, current - 1, order_change_code)
            if observed != current - 1:
                raise HouseholdError("MENY cart quantity did not settle")
            self._assert_authenticated()
        except HouseholdError as exc:
            if dispatched:
                raise HouseholdError("MENY cart change is uncertain; read the cart and do not retry this request") from exc
            raise

    def _click_cart_remove_control(self, product: str, quantity: int, order_change_code: str | None, before_dispatch: Any) -> None:
        selector = '[data-hermes-meal-planner-action="cart-remove"]'
        gate = r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible);
  if (authenticated.length !== 1 || carts.length !== 1) return JSON.stringify({ready:false});
  const cart = carts[0], expectedProduct = __PRODUCT__, expectedQuantity = __QUANTITY__, expectedCode = __CODE__, requireHit = __REQUIRE_HIT__;
  const activeCodes = [...norm(cart.innerText).matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(x => x[1]);
  const aborts = [...cart.querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Avbryt endring');
  const modeReady = expectedCode === null ? activeCodes.length === 0 && aborts.length === 0 : activeCodes.length === 1 && activeCodes[0] === expectedCode && aborts.length === 1;
  const roots = [];
  for (const anchor of cart.querySelectorAll('a[href]')) {
    const url = new URL(anchor.href, location.origin), root = url.origin === location.origin && url.pathname === expectedProduct ? anchor.closest('li') : null;
    if (root && visible(root) && !roots.includes(root)) roots.push(root);
  }
  if (!modeReady || roots.length !== 1) return JSON.stringify({ready:false});
  const root = roots[0], selects = [...root.querySelectorAll('select[aria-label*="endre mengde"]')].filter(visible);
  if (selects.length !== 1) return JSON.stringify({ready:false});
  const match = norm(selects[0].getAttribute('aria-label')).match(/^(\d+)\s+stk,\s*endre mengde\s+(.+)$/i);
  const current = Number.parseInt(norm(selects[0].selectedOptions?.[0]?.innerText), 10), name = match?.[2] || '';
  const labels = current === 1 ? [`Fjern ${name} fra handlevognen`] : [`Fjern 1 stk ${name} fra handlevognen`, `Fjern én stk ${name} fra handlevognen`];
  const candidates = [...root.querySelectorAll('button')].filter(enabled).filter(x => labels.includes(norm(x.getAttribute('aria-label') || x.innerText)));
  if (!match || Number(match[1]) !== current || current !== expectedQuantity || candidates.length !== 1) return JSON.stringify({ready:false});
  const target = candidates[0];
  target.setAttribute('data-hermes-meal-planner-action', 'cart-remove');
  const marked = [...document.querySelectorAll('[data-hermes-meal-planner-action="cart-remove"]')];
  const hit = requireHit ? document.elementFromPoint(__HIT_X__, __HIT_Y__) : target;
  return JSON.stringify({ready:marked.length === 1 && marked[0] === target && Boolean(hit) && (hit === target || target.contains(hit))});
})()
"""

        def render(require_hit: bool, x: int = 0, y: int = 0) -> str:
            return gate.replace("__PRODUCT__", json.dumps(product)).replace("__QUANTITY__", str(quantity)).replace("__CODE__", json.dumps(order_change_code)).replace("__REQUIRE_HIT__", "true" if require_hit else "false").replace("__HIT_X__", str(x)).replace("__HIT_Y__", str(y))

        if self._eval(render(False)) != {"ready": True}:
            raise HouseholdError("MENY cart remove control changed")
        self._invoke("scrollintoview", selector)
        box = self._find_box(self._invoke("get", "box", selector))
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise HouseholdError("MENY cart remove control is not clickable")
        x = round(box["x"] + box["width"] / 2)
        y = round(box["y"] + box["height"] / 2)
        self._invoke("mouse", "move", str(x), str(y))
        if self._eval(render(True, x, y)) != {"ready": True}:
            raise HouseholdError("MENY cart remove control changed or is obscured")
        before_dispatch()
        self._invoke("mouse", "down")
        self._invoke("mouse", "up")

    def _wait_for_cart_quantity(self, product: str, expected: int, order_change_code: str | None) -> int:
        observed = -1
        for _ in range(12):
            self._sleep(0.25)
            result = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible);
  if (authenticated.length !== 1 || carts.length !== 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1});
  const cart = carts[0], expectedProduct = __PRODUCT__, expectedCode = __CODE__;
  const activeCodes = [...norm(cart.innerText).matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(x => x[1]);
  const aborts = [...cart.querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Avbryt endring');
  const modeReady = expectedCode === null ? activeCodes.length === 0 && aborts.length === 0 : activeCodes.length === 1 && activeCodes[0] === expectedCode && aborts.length === 1;
  const roots = [];
  for (const anchor of cart.querySelectorAll('a[href]')) {
    const url = new URL(anchor.href, location.origin), root = url.origin === location.origin && url.pathname === expectedProduct ? anchor.closest('li') : null;
    if (root && visible(root) && !roots.includes(root)) roots.push(root);
  }
  if (!modeReady || roots.length > 1) return JSON.stringify({ready:false, authenticated:true});
  if (roots.length === 0) return JSON.stringify({ready:true, authenticated:true, quantity:0});
  const selects = [...roots[0].querySelectorAll('select[aria-label*="endre mengde"]')].filter(visible);
  const quantity = selects.length === 1 ? Number.parseInt(norm(selects[0].selectedOptions?.[0]?.innerText), 10) : null;
  return JSON.stringify({ready:Number.isInteger(quantity) && quantity > 0, authenticated:true, quantity});
})()
""".replace("__PRODUCT__", json.dumps(product)).replace("__CODE__", json.dumps(order_change_code)))
            if result.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if result.get("ready") is True and result.get("quantity") == expected:
                return expected
            if isinstance(result.get("quantity"), int):
                observed = result["quantity"]
        return observed

    def _product_control(self, action: str, delta: int, product: str) -> dict[str, Any]:
        return self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const headings = [...document.querySelectorAll('main h1')].filter(visible);
  const abouts = [...document.querySelectorAll('main h2')].filter(visible).filter(x => norm(x.innerText) === 'Om produktet');
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  if (location.origin !== 'https://meny.no' || location.pathname !== PRODUCT) return JSON.stringify({ready:false, page_ready:false, authenticated:authenticated.length === 1});
  if (headings.length !== 1 || abouts.length !== 1) return JSON.stringify({ready:false, page_ready:false, authenticated:authenticated.length === 1});
  const heading = headings[0], about = abouts[0];
  const between = x => (heading.compareDocumentPosition(x) & Node.DOCUMENT_POSITION_FOLLOWING) && (x.compareDocumentPosition(about) & Node.DOCUMENT_POSITION_FOLLOWING);
  const selects = [...document.querySelectorAll('main select[aria-label*="endre mengde"]')].filter(x => visible(x) && between(x));
  if (selects.length > 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1});
  const select = selects[0];
  const selected = norm(select?.selectedOptions?.[0]?.innerText || '');
  const quantity = Number.parseInt(selected, 10) || 0;
  if (ACTION === 'read') return JSON.stringify({ready:true, page_ready:true, authenticated:authenticated.length === 1, quantity});
  const name = norm(heading.innerText);
  const buttons = [...document.querySelectorAll('main button')].filter(x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true' && between(x));
  const candidates = buttons.filter(button => {
    const label = norm(button.getAttribute('aria-label') || button.innerText);
    return DELTA > 0
      ? (label === `Legg ${name} i handlevognen` || label === `Legg til 1 stk ${name} i handlevognen`)
      : label === `Fjern ${name} fra handlevognen`;
  });
  if (candidates.length !== 1 || (DELTA < 0 && quantity < 1)) return JSON.stringify({ready:false, page_ready:true, authenticated:authenticated.length === 1, quantity});
  candidates[0].setAttribute('data-hermes-meal-planner-action', 'cart');
  const marked = [...document.querySelectorAll('[data-hermes-meal-planner-action="cart"]')];
  const label = norm(candidates[0].getAttribute('aria-label') || candidates[0].innerText);
  return JSON.stringify({ready:marked.length === 1 && marked[0] === candidates[0], page_ready:true, authenticated:authenticated.length === 1, quantity, label});
})()
""".replace("ACTION", json.dumps(action)).replace("DELTA", str(delta)).replace("PRODUCT", json.dumps(product)))

    def _click_cart_control(self, product: str, label: str) -> None:
        if not label:
            raise HouseholdError("MENY cart control identity is unavailable")
        selector = '[data-hermes-meal-planner-action="cart"]'
        ready = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const marked = [...document.querySelectorAll('[data-hermes-meal-planner-action="cart"]')];
  const target = marked[0];
  return JSON.stringify({ready:authenticated.length === 1 && location.origin === 'https://meny.no' && location.pathname === PRODUCT && marked.length === 1 && enabled(target) && norm(target.getAttribute('aria-label') || target.innerText) === LABEL});
})()
""".replace("PRODUCT", json.dumps(product)).replace("LABEL", json.dumps(label)))
        if ready != {"ready": True}:
            raise HouseholdError("MENY cart control changed")
        self._invoke("scrollintoview", selector)
        box = self._find_box(self._invoke("get", "box", selector))
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise HouseholdError("MENY cart control is not clickable")
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        clear = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const marked = [...document.querySelectorAll('[data-hermes-meal-planner-action="cart"]')];
  const target = marked[0], hit = document.elementFromPoint(X, Y);
  return JSON.stringify({clear:authenticated.length === 1 && location.origin === 'https://meny.no' && location.pathname === PRODUCT && marked.length === 1 && enabled(target) && norm(target.getAttribute('aria-label') || target.innerText) === LABEL && Boolean(hit) && (hit === target || target.contains(hit))});
})()
""".replace("PRODUCT", json.dumps(product)).replace("LABEL", json.dumps(label)).replace("X", json.dumps(x)).replace("Y", json.dumps(y)))
        if clear != {"clear": True}:
            raise HouseholdError("MENY cart control is obscured or changed")
        self._require_time(3)
        self._invoke("mouse", "move", str(round(x)), str(round(y)))
        self._invoke("mouse", "down")
        self._invoke("mouse", "up")

    def _wait_for_checkout_hit(self, selector: str, render_gate: Any, error: str) -> tuple[int, int]:
        self._invoke("scrollintoview", selector)
        for attempt in range(20):
            box = self._find_box(self._invoke("get", "box", selector))
            if box and box["width"] > 0 and box["height"] > 0:
                x = round(box["x"] + box["width"] / 2)
                y = round(box["y"] + box["height"] / 2)
                if self._eval(render_gate(True, x, y)) == {"ready": True}:
                    return x, y
            if attempt < 19:
                self._sleep(0.1)
        raise HouseholdError(error)

    def _click_checkout_control(
        self,
        action: str,
        *,
        expected_items: list[tuple[str, int]] | None = None,
        target_code: str | None = None,
    ) -> None:
        if action not in {"checkout-next", "vipps"}:
            raise HouseholdError("invalid MENY checkout action")
        selector = f'[data-hermes-meal-planner-action="{action}"]'
        expected_pairs = sorted([[str(product_id), int(quantity)] for product_id, quantity in (expected_items or [])])
        gate = r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const action = __ACTION__, expectedItems = __EXPECTED_ITEMS__, expectedCode = __TARGET_CODE__, requireHit = __REQUIRE_HIT__;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const main = [...document.querySelectorAll('main')].filter(visible);
  let target = null, radio = null;
  let semantic = false;
  if (action === 'checkout-next' && main.length === 1) {
    const text = norm(main[0].innerText), activeCodes = [...text.matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(match => match[1]);
    const targetReady = expectedCode === null ? activeCodes.length === 0 : activeCodes.length === 1 && activeCodes[0] === expectedCode;
    const unavailable = [...main[0].querySelectorAll('h1,h2,h3')].filter(visible).filter(x => norm(x.innerText) === 'Disse varene vil du ikke motta');
    const productPattern = /^\/varer\/(?!kampanjer\/)[A-Za-z0-9._~%/-]+-\d{4,14}$/;
    const observed = [];
    let itemsReady = true;
    const controls = [...main[0].querySelectorAll('select[aria-label*="endre mengde"]')].filter(visible);
    for (const select of controls) {
      const item = select.closest('.ws-product');
      const paths = item ? [...new Set([...item.querySelectorAll('a[href]')].map(link => new URL(link.href, location.origin)).filter(url => url.origin === location.origin && productPattern.test(url.pathname)).map(url => url.pathname))] : [];
      const quantity = Number.parseInt(norm(select.selectedOptions?.[0]?.innerText), 10);
      if (!item || paths.length !== 1 || !Number.isInteger(quantity) || quantity < 1) { itemsReady=false; break; }
      observed.push([paths[0], quantity]);
    }
    observed.sort((a,b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1]-b[1]);
    const candidates = [...main[0].querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Neste');
    target = candidates.length === 1 ? candidates[0] : null;
    semantic = candidates.length === 1 && /Se over varene/.test(text) && targetReady && unavailable.length === 0 && controls.length > 0 && itemsReady && JSON.stringify(observed) === JSON.stringify(expectedItems);
  } else if (action === 'vipps' && main.length === 1) {
    const vipps = [...main[0].querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Vipps(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
    radio = vipps.length === 1 ? vipps[0] : null;
    const label = radio?.closest('label');
    target = radio ? label || radio : null;
    const activationSurface = target === radio || (target?.tagName === 'LABEL' && label === target);
    semantic = vipps.length === 1 && activationSurface && enabled(radio) && radio.checked !== true && radio.getAttribute('aria-checked') !== 'true' && /Leverings- og betalingsinformasjon/.test(norm(main[0].innerText));
  }
  if (semantic && target) target.setAttribute('data-hermes-meal-planner-action', action);
  const marked = [...document.querySelectorAll(__SELECTOR__)];
  const hit = requireHit ? document.elementFromPoint(__HIT_X__, __HIT_Y__) : target;
  const hitInteractive = hit?.closest('button,a,input,select,textarea,[role="button"],[role="radio"]');
  const hitReady = action === 'vipps' && target !== radio
    ? Boolean(hit) && (hit === target || target.contains(hit)) && (!hitInteractive || hitInteractive === radio)
    : Boolean(hit) && (hit === target || target.contains(hit));
  const ready = location.href === 'https://meny.no/kassen' && authenticated.length === 1 && marked.length === 1 && marked[0] === target && enabled(target) && semantic && hitReady;
  return JSON.stringify({ready});
})()
"""

        def render_gate(require_hit: bool, x: int = 0, y: int = 0) -> str:
            return gate.replace("__REQUIRE_HIT__", "true" if require_hit else "false").replace("__HIT_X__", str(x)).replace("__HIT_Y__", str(y)).replace("__ACTION__", json.dumps(action)).replace("__SELECTOR__", json.dumps(selector)).replace("__EXPECTED_ITEMS__", json.dumps(expected_pairs)).replace("__TARGET_CODE__", json.dumps(target_code))

        ready = self._eval(render_gate(False))
        if ready != {"ready": True}:
            raise HouseholdError("MENY checkout control changed")
        x, y = self._wait_for_checkout_hit(
            selector,
            render_gate,
            "MENY checkout control is obscured or changed",
        )
        self._invoke("mouse", "move", str(x), str(y))
        ready = self._eval(render_gate(True, x, y))
        if ready != {"ready": True}:
            raise HouseholdError("MENY checkout control is obscured or changed")
        self._invoke("mouse", "down")
        self._invoke("mouse", "up")

    def _click_checkout_submit(self, review: Mapping[str, Any], before_dispatch: Any) -> None:
        if self.vipps_phone_number is None:
            raise HouseholdError("MENY checkout requires vipps_phone_number in the private household config")
        selector = '[data-hermes-meal-planner-action="checkout-submit"]'
        gate = r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const requireHit = __REQUIRE_HIT__;
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const main = [...document.querySelectorAll('main')].filter(visible);
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  if (main.length !== 1 || authenticated.length !== 1) return JSON.stringify({ready:false});
  const root = main[0], leaf = selector => [...root.querySelectorAll(selector)].filter(visible).filter(x => ![...x.children].some(child => visible(child) && norm(child.innerText) === norm(x.innerText)));
  const text = norm(root.innerText), expectedCode = __CODE__;
  const activeCodes = [...text.matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(match => match[1]);
  const active = activeCodes.length === 1;
  const targetReady = expectedCode === null ? activeCodes.length === 0 : active && activeCodes[0] === expectedCode;
  const vipps = [...root.querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Vipps(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  const checked = vipps.length === 1 && (vipps[0].checked === true || vipps[0].getAttribute('aria-checked') === 'true');
  const home = [...root.querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Levert på døren(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  const homeChecked = home.length === 1 && (home[0].checked === true || home[0].getAttribute('aria-checked') === 'true');
  const buttons = [...root.querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Til betaling');
  const blockingDialogs = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"],[role="alert"],[aria-modal="true"]')].filter(visible);
  const totalLabels = leaf('*').filter(x => norm(x.innerText) === 'Totalsum');
  const totals = [];
  for (const label of totalLabels) {
    let row = label.parentElement;
    for (let depth=0; row && depth<3; depth++, row=row.parentElement) {
      const values = [...norm(row.innerText).matchAll(/(?:^|\s)(\d+(?:[ .]\d{3})*),([0-9]{2})(?:\s|$)/g)].map(m => Number(`${m[1].replace(/[ .]/g,'')}.${m[2]}`));
      if (values.length === 1) { totals.push(values[0]); break; }
    }
  }
__DELIVERY_BINDING__
  const exact = checked && homeChecked && targetReady && buttons.length === 1 && blockingDialogs.length === 0 && totalLabels.length === 1 && totals.length === 1 && Math.round(totals[0]*100) === __TOTAL__ && deliveryBinding?.root && deliveryBinding.display === __DELIVERY__ && location.href === __URL__;
  if (!exact) return JSON.stringify({ready:false});
  const target = buttons[0];
  target.setAttribute('data-hermes-meal-planner-action', 'checkout-submit');
  const marked = [...document.querySelectorAll('[data-hermes-meal-planner-action="checkout-submit"]')];
  const hit = requireHit ? document.elementFromPoint(__HIT_X__, __HIT_Y__) : target;
  return JSON.stringify({ready:marked.length === 1 && marked[0] === target && Boolean(hit) && (hit === target || target.contains(hit))});
})()
"""
        def render_gate(require_hit: bool, x: int = 0, y: int = 0) -> str:
            return gate.replace("__DELIVERY_BINDING__", CHECKOUT_DELIVERY_BINDING_JS).replace("__REQUIRE_HIT__", "true" if require_hit else "false").replace("__HIT_X__", str(x)).replace("__HIT_Y__", str(y)).replace("__CODE__", json.dumps(review.get("target_order_code"))).replace("__TOTAL__", str(int(round(float(review["summary"]["total"]) * 100)))).replace("__DELIVERY__", json.dumps(review["summary"]["delivery"]["display"], ensure_ascii=False)).replace("__URL__", json.dumps(CHECKOUT_URL))

        if self._eval(render_gate(False)) != {"ready": True}:
            raise HouseholdError("MENY Vipps payment control changed")
        x, y = self._wait_for_checkout_hit(
            selector,
            render_gate,
            "MENY Vipps payment control changed or is obscured",
        )
        self._invoke("mouse", "move", str(x), str(y))
        if self._eval(render_gate(True, x, y)) != {"ready": True}:
            raise HouseholdError("MENY Vipps payment control changed or is obscured")
        self._require_time(15)
        self._invoke("network", "requests", "--clear")
        before_dispatch()
        self._invoke("mouse", "down")
        self._invoke("mouse", "up")
        self._wait_for_vipps_dispatch(
            lambda: self._eval(render_gate(False)) == {"ready": True},
            lambda: self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const notices = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"],[role="alert"],[aria-modal="true"]')].filter(visible).filter(x => norm(x.innerText) === 'Du har dessverre mistet din reservasjon. Velg tidspunkt på nytt for å kunne betale.');
  return JSON.stringify({reservation_expired:location.href === 'https://meny.no/kassen' && authenticated.length === 1 && notices.length === 1});
})()
"""),
        )

    def _wait_for_vipps_dispatch(self, exact_checkout: Any = None, known_failure: Any = None) -> None:
        requests: Any = {"requests": []}
        for attempt in range(40):
            requests = self._invoke("network", "requests")
            if vipps_dispatch_acknowledged(requests):
                self._complete_vipps_request()
                return
            if attempt < 39:
                self._sleep(0.25)
        if not vipps_dispatch_attempted(requests):
            if known_failure is not None and known_failure() == {"reservation_expired": True}:
                raise CheckoutPreconditionError(
                    "MENY delivery reservation expired before payment; select the same delivery time again"
                )
            if exact_checkout is not None and exact_checkout():
                final_requests = self._invoke("network", "requests")
                if not vipps_dispatch_attempted(final_requests) and exact_checkout():
                    raise CheckoutPreconditionError(
                        "MENY did not dispatch the Vipps payment request; one fresh prepare is safe"
                    )
        raise HouseholdError(
            "MENY did not acknowledge the Vipps payment request; "
            "the outcome is uncertain and must be reconciled; do not retry"
        )

    def _complete_vipps_request(self) -> None:
        form: dict[str, Any] = {}
        for attempt in range(40):
            form = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const identity = location.origin === 'https://api.vipps.no' && location.pathname === '/dwo-api-application/v1/deeplink/vippsgateway';
  const text = norm(document.body.innerText);
  const sent = identity && /We've sent a payment request to/i.test(text) && /Open Vipps/i.test(text);
  const phones = [...document.querySelectorAll('input[type="tel"][name="phone-number"]')].filter(visible).filter(x => x.maxLength === 8 && x.autocomplete === 'tel-national');
  const buttons = [...document.querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Next');
  const remember = [...document.querySelectorAll('input[type="checkbox"]')].filter(visible);
  const ready = identity && !sent && /Continue to pay with Vipps/i.test(text) && phones.length === 1 && buttons.length === 1 && remember.length === 1 && remember[0].checked === false;
  if (ready) buttons[0].setAttribute('data-hermes-meal-planner-action', 'vipps-next');
  return JSON.stringify({identity, ready, sent});
})()
""")
            if form.get("identity") is True and (form.get("ready") is True or form.get("sent") is True):
                break
            if attempt < 39:
                self._sleep(0.25)
        if form.get("sent") is True:
            return
        if form.get("identity") is not True or form.get("ready") is not True:
            raise HouseholdError("Vipps payment page did not finish rendering; the outcome is uncertain; do not retry")
        try:
            self._invoke("fill", 'input[name="phone-number"]', self.vipps_phone_number)
        except HouseholdError as exc:
            raise HouseholdError("Vipps mobile number could not be entered; the outcome is uncertain; do not retry") from exc
        selector = '[data-hermes-meal-planner-action="vipps-next"]'
        gate = r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const requireHit = __REQUIRE_HIT__;
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const phone = document.querySelector('input[type="tel"][name="phone-number"]');
  const remember = [...document.querySelectorAll('input[type="checkbox"]')].filter(visible);
  const buttons = [...document.querySelectorAll('button')].filter(enabled).filter(x => (x.innerText || '').trim() === 'Next');
  const exact = location.origin === 'https://api.vipps.no' && location.pathname === '/dwo-api-application/v1/deeplink/vippsgateway' && phone && phone.value.replace(/\D/g,'') === __PHONE__ && remember.length === 1 && remember[0].checked === false && buttons.length === 1;
  const target = exact ? buttons[0] : null;
  if (target) target.setAttribute('data-hermes-meal-planner-action', 'vipps-next');
  const hit = requireHit ? document.elementFromPoint(__HIT_X__, __HIT_Y__) : target;
  return JSON.stringify({ready:Boolean(target && hit && (hit === target || target.contains(hit)))});
})()
"""

        def render(require_hit: bool, x: int = 0, y: int = 0) -> str:
            return gate.replace("__REQUIRE_HIT__", "true" if require_hit else "false").replace("__HIT_X__", str(x)).replace("__HIT_Y__", str(y)).replace("__PHONE__", json.dumps(self.vipps_phone_number))

        if self._eval(render(False)) != {"ready": True}:
            raise HouseholdError("Vipps mobile request control changed; the outcome is uncertain; do not retry")
        x, y = self._wait_for_checkout_hit(selector, render, "Vipps mobile request control changed or is obscured")
        self._invoke("mouse", "move", str(x), str(y))
        if self._eval(render(True, x, y)) != {"ready": True}:
            raise HouseholdError("Vipps mobile request control changed or is obscured; the outcome is uncertain; do not retry")
        self._invoke("mouse", "down")
        self._invoke("mouse", "up")
        for attempt in range(40):
            sent = self._eval(r"""
(() => {
  const text = (document.body.innerText || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  return JSON.stringify({sent:location.origin === 'https://api.vipps.no' && location.pathname === '/dwo-api-application/v1/deeplink/vippsgateway' && /We've sent a payment request to/i.test(text) && /Open Vipps/i.test(text)});
})()
""")
            if sent == {"sent": True}:
                return
            if attempt < 39:
                self._sleep(0.25)
        raise HouseholdError("Vipps did not confirm the mobile payment request; the outcome is uncertain; do not retry")

    @staticmethod
    def _find_box(value: Any) -> dict[str, float] | None:
        if isinstance(value, Mapping):
            keys = ("x", "y", "width", "height")
            if all(not isinstance(value.get(key), bool) and isinstance(value.get(key), (int, float)) for key in keys):
                return {key: float(value[key]) for key in keys}
            for child in value.values():
                if box := MenyClient._find_box(child):
                    return box
        return None

    def _wait_for_quantity(self, expected: int, previous: int, product: str) -> int:
        observed = previous
        for _ in range(12):
            self._sleep(0.25)
            result = self._product_control("read", 1, product)
            if result.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            observed = result.get("quantity")
            if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
                raise HouseholdError("MENY product quantity is invalid")
            if observed == expected:
                break
        return observed

    def _read_cart(self, *, allow_reload: bool = True) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for _ in range(20):
            state = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible).filter(x => [...x.querySelectorAll('button')].filter(visible).some(button => ['Til kassen','Fortsett'].includes(norm(button.innerText))));
  if (carts.length === 1) return JSON.stringify({open:true, ready:true, authenticated:authenticated.length === 1, root_count:1});
  const open = [...document.querySelectorAll('button')].filter(visible).filter(button => !button.disabled && norm(button.getAttribute('aria-label')) === 'Åpne handlevognen');
  if (carts.length !== 0 || open.length !== 1) return JSON.stringify({open:false, ready:false, authenticated:authenticated.length === 1, root_count:carts.length, open_count:open.length});
  open[0].setAttribute('data-hermes-meal-planner-action', 'open-cart');
  return JSON.stringify({open:false, ready:true, authenticated:authenticated.length === 1, root_count:0, open_count:1});
})()
""")
            if state.get("ready") is True:
                break
            self._sleep(0.25)
        if state.get("authenticated") is not True:
            raise HouseholdError("MENY login is required in the configured browser profile")
        if state.get("ready") is not True:
            raise HouseholdError("MENY cart is unavailable")
        if state.get("open") is not True:
            self._invoke("click", '[data-hermes-meal-planner-action="open-cart"]')
            self._sleep(0.5)
        result: dict[str, Any] = {}
        for _ in range(60):
            result = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible).filter(x => [...x.querySelectorAll('button')].filter(visible).some(button => ['Til kassen','Fortsett'].includes(norm(button.innerText))));
  if (carts.length !== 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1, root_count:carts.length});
  const cart = carts[0];
  const productPattern = /^\/varer\/(?!kampanjer\/)[A-Za-z0-9._~%/-]+-\d{4,14}$/;
  const itemRoots = [];
  for (const anchor of cart.querySelectorAll('a[href]')) {
    const url = new URL(anchor.href, location.origin);
    const root = url.origin === location.origin && productPattern.test(url.pathname) ? anchor.closest('li') : null;
    if (root && visible(root) && !itemRoots.includes(root)) itemRoots.push(root);
  }
  const controls = [...cart.querySelectorAll('select[aria-label*="endre mengde"]')].filter(visible);
  const items = [];
  for (const root of itemRoots) {
    const selects = [...root.querySelectorAll('select[aria-label*="endre mengde"]')].filter(visible);
    const paths = [...new Set([...root.querySelectorAll('a[href]')].map(link => new URL(link.href, location.origin)).filter(url => url.origin === location.origin && productPattern.test(url.pathname)).map(url => url.pathname))];
    if (selects.length !== 1 || paths.length !== 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1, root_count:1, item_root_count:itemRoots.length, control_count:controls.length});
    const select = selects[0];
    const label = norm(select.getAttribute('aria-label'));
    const quantity = Number.parseInt(norm(select.selectedOptions?.[0]?.innerText), 10);
    if (!Number.isInteger(quantity) || quantity < 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1, root_count:1, item_root_count:itemRoots.length, control_count:controls.length});
    const name = label.replace(/^\d+\s+stk,\s*endre mengde\s+/i, '');
    const priceText = norm(root.querySelector('strong')?.innerText).match(/\d+(?:[ .]\d{3})*,\d{2}\s*kr/i)?.[0] || null;
    const price = priceText ? Number(priceText.replace(/\s*kr/i, '').replace(/ /g, '').replace(',', '.')) : null;
    items.push({product_id:paths[0], name, quantity, price});
  }
  const totals = [...cart.querySelectorAll('strong')].filter(visible).map(x => norm(x.innerText)).filter(x => /^Totalsum\s+\d+(?:[ .]\d{3})*,\d{2}\s*kr$/i.test(x));
  const totalText = totals.length === 1 ? totals[0] : null;
  const match = totalText?.match(/(\d+(?:[ .]\d{3})*),([0-9]{2})/);
  const priceSummaries = [...cart.querySelectorAll('.ws-price-summary.ws-cart__price-summary')].filter(visible);
  const subtotalRows = priceSummaries.length === 1 ? [...priceSummaries[0].querySelectorAll('.ws-summary-line__main')].filter(visible).filter(row => {
    const titles = [...row.querySelectorAll('.ws-summary-line__title')].filter(visible).filter(x => norm(x.innerText) === 'Sum');
    return titles.length === 1;
  }) : [];
  const subtotalValues = subtotalRows.length === 1 ? [...norm(subtotalRows[0].innerText).matchAll(/(?:^|\s)(\d+(?:[ .]\d{3})*),([0-9]{2})(?:\s|$)/g)].map(value => Number(`${value[1].replace(/[ .]/g, '')}.${value[2]}`)) : [];
  const subtotal = subtotalValues.length === 1 ? subtotalValues[0] : null;
  const empty = itemRoots.length === 0 && /(?:handlevognen(?: din)? er tom|ingen varer i handlevognen)/i.test(norm(cart.innerText));
  const total = match ? Number(`${match[1].replace(/[ .]/g, '')}.${match[2]}`) : empty && totals.length === 0 ? 0 : null;
  const totalReady = empty ? totals.length === 0 && total === 0 : totals.length === 1 && total !== null && total > 0;
  const subtotalReady = empty ? subtotalRows.length === 0 && subtotal === null : priceSummaries.length === 1 && subtotalRows.length === 1 && subtotal !== null && subtotal > 0;
  const deliveryPrefix = 'Du har valgt at varene leveres på døren';
  const deliveryHint = norm(cart.innerText).toLocaleLowerCase('nb-NO').includes(deliveryPrefix.toLocaleLowerCase('nb-NO'));
  const deliveryParagraphs = [...cart.querySelectorAll('p')].filter(visible).filter(x => norm(x.innerText).toLocaleLowerCase('nb-NO').startsWith(deliveryPrefix.toLocaleLowerCase('nb-NO')));
  let delivery = null, deliveryReady = !deliveryHint && deliveryParagraphs.length === 0;
  if (deliveryHint && deliveryParagraphs.length === 1) {
    const text = norm(deliveryParagraphs[0].innerText);
    const selected = text.match(/^Du har valgt at varene leveres på døren (.+)\. Du kan endre dette i kassen eller her\s*\.$/);
    const display = selected?.[1] || '';
    const validDisplay = /^(?:mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(?:[1-9]|[12]\d|3[01])\.\s+(?:jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\.?\s+kl\.\s+(?:[01]\d|2[0-3]):[0-5]\d[-–](?:[01]\d|2[0-3]):[0-5]\d$/i.test(display);
    if (selected && validDisplay) {
      delivery = {display};
      deliveryReady = true;
    }
  }
  const ready = authenticated.length === 1 && itemRoots.length === controls.length && items.length === itemRoots.length && (items.length > 0 || empty) && totalReady && subtotalReady && deliveryReady;
  return JSON.stringify({ready, authenticated:authenticated.length === 1, root_count:1, item_root_count:itemRoots.length, control_count:controls.length, empty, total_count:totals.length, subtotal_count:subtotalRows.length, subtotal, delivery_count:deliveryParagraphs.length, delivery, items, count:items.reduce((sum,item) => sum + item.quantity, 0), total});
})()
""")
            if result.get("ready") is True:
                break
            if (
                result.get("authenticated") is True
                and result.get("item_root_count", 0) > 0
                and (
                    (result.get("total_count") == 1 and result.get("total") == 0)
                    or (result.get("subtotal_count") == 1 and result.get("subtotal") == 0)
                )
            ):
                break
            self._sleep(0.25)
        if (
            allow_reload
            and result.get("authenticated") is True
            and result.get("item_root_count", 0) > 0
            and (
                (result.get("total_count") == 1 and result.get("total") == 0)
                or (result.get("subtotal_count") == 1 and result.get("subtotal") == 0)
            )
        ):
            self._invoke("reload")
            self._sleep(0.5)
            return self._read_cart(allow_reload=False)
        snapshot = normalize_cart_snapshot(result)
        return {
            "provider": "meny",
            "items": snapshot["items"],
            "count": snapshot["count"],
            "subtotal": snapshot["total"],
            "total": snapshot["total"],
            "delivery": snapshot["delivery"],
            "checkout": {"mode": "protected_vipps", "url": CHECKOUT_URL},
        }

    @staticmethod
    def _order_id(value: Any) -> str:
        order_id = str(value or "").strip()
        if re.fullmatch(r"\d{1,20}", order_id) is None:
            raise HouseholdError("MENY order_id is invalid")
        return order_id

    def _open_delivery_picker(self) -> None:
        self._open(f"{BASE_URL}/sok?{urlencode({'query': 'levering'})}")
        self._open(STORE_URL)
        self._prepare_search()
        for _ in range(20):
            result = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const dialogs = [...document.querySelectorAll('dialog,[role="dialog"]')].filter(visible).filter(x => [...x.querySelectorAll('h1')].filter(visible).filter(h => norm(h.innerText) === 'Når skal vi levere til deg?').length === 1);
  const buttons = [...document.querySelectorAll('button')].filter(visible).filter(x => !x.disabled && norm(x.getAttribute('aria-label') || x.innerText) === 'Velg leveringstid');
  if (!identity || authenticated.length !== 1 || dialogs.length !== 0 || buttons.length !== 1) return JSON.stringify({ready:false, identity, authenticated:authenticated.length === 1, dialog_count:dialogs.length});
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'delivery-open');
  return JSON.stringify({ready:[...document.querySelectorAll('[data-hermes-meal-planner-action="delivery-open"]')].length === 1, identity, authenticated:true});
})()
""")
            if result.get("identity") is not True:
                raise HouseholdError("MENY delivery route changed")
            if result.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if result.get("ready") is True:
                break
            self._sleep(0.25)
        else:
            raise HouseholdError("MENY delivery picker is unavailable")
        self._invoke("click", '[data-hermes-meal-planner-action="delivery-open"]')
        for _ in range(60):
            ready = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const dialogs = [...document.querySelectorAll('dialog,[role="dialog"]')].filter(visible).filter(x => [...x.querySelectorAll('h1')].filter(visible).filter(h => norm(h.innerText) === 'Når skal vi levere til deg?').length === 1);
  return JSON.stringify({ready:identity && authenticated.length === 1 && dialogs.length === 1, identity, authenticated:authenticated.length === 1});
})()
""")
            if ready.get("identity") is not True:
                raise HouseholdError("MENY delivery route changed")
            if ready.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if ready.get("ready") is True:
                return
            self._sleep(0.25)
        raise HouseholdError("MENY delivery picker did not finish rendering")

    def _wait_delivery_picker_closed(self) -> None:
        for _ in range(20):
            state = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const dialogs = [...document.querySelectorAll('dialog,[role="dialog"]')].filter(visible).filter(x => [...x.querySelectorAll('h1')].filter(visible).filter(h => norm(h.innerText) === 'Når skal vi levere til deg?').length === 1);
  return JSON.stringify({ready:identity && authenticated.length === 1 && dialogs.length === 0, identity, authenticated:authenticated.length === 1, dialog_count:dialogs.length});
})()
""")
            if state.get("identity") is not True:
                raise HouseholdError("MENY delivery route changed")
            if state.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if state.get("ready") is True:
                return
            self._sleep(0.25)
        raise HouseholdError("MENY delivery selection is uncertain; inspect the selected slot before retrying")

    def _delivery_slots(self, delivery_date: str = "") -> dict[str, Any]:
        if delivery_date:
            try:
                date.fromisoformat(delivery_date)
            except ValueError as exc:
                raise HouseholdError("delivery date is invalid") from exc
        self._open_delivery_picker()
        result: dict[str, Any] = {}
        for _ in range(20):
            result = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const dialogs = [...document.querySelectorAll('dialog,[role="dialog"]')].filter(visible).filter(x => [...x.querySelectorAll('h1')].filter(visible).filter(h => norm(h.innerText) === 'Når skal vi levere til deg?').length === 1);
  if (!identity || authenticated.length !== 1 || dialogs.length !== 1) return JSON.stringify({ready:false, identity, authenticated:authenticated.length === 1, slots:[]});
  const root = dialogs[0];
  const months = {januar:0,februar:1,mars:2,april:3,mai:4,juni:5,juli:6,august:7,september:8,oktober:9,november:10,desember:11};
  const today = new Date();
  const slots = [];
  for (const button of [...root.querySelectorAll('button')].filter(visible)) {
    const label = norm(button.getAttribute('aria-label') || button.innerText);
    const match = label.match(/^(?:fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kr\s+fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kroner,\s*)?(0?[1-9]|[12]\d|3[01])\.\s*(januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember)\s+klokka\s+([01]?\d|2[0-3]):([0-5]\d)\s+til\s+([01]?\d|2[0-3]):([0-5]\d)$/i);
    if (!match || button.disabled || button.getAttribute('aria-disabled') === 'true') continue;
    let year = today.getFullYear();
    const month = months[match[2].toLocaleLowerCase('nb-NO')];
    if (month < today.getMonth() - 6) year += 1;
    const iso = `${year}-${String(month + 1).padStart(2,'0')}-${String(Number(match[1])).padStart(2,'0')}`;
    slots.push({slot_id:label, date:iso, start:`${String(Number(match[3])).padStart(2,'0')}:${match[4]}`, end:`${String(Number(match[5])).padStart(2,'0')}:${match[6]}`, display:label, selected:button.getAttribute('aria-pressed') === 'true'});
  }
  const dismiss = [...root.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText) === 'Lukk');
  if (dismiss.length !== 1 || slots.length === 0) return JSON.stringify({ready:false, identity, authenticated:true, slots});
  dismiss[0].setAttribute('data-hermes-meal-planner-action', 'delivery-dismiss');
  return JSON.stringify({ready:true, identity, authenticated:true, slots});
})()
""")
            if result.get("identity") is not True:
                raise HouseholdError("MENY delivery route changed")
            if result.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if result.get("ready") is True and isinstance(result.get("slots"), list):
                break
            self._sleep(0.25)
        else:
            raise HouseholdError("MENY delivery slots are unavailable")
        self._invoke("click", '[data-hermes-meal-planner-action="delivery-dismiss"]')
        self._wait_delivery_picker_closed()
        slots = result["slots"]
        if delivery_date:
            slots = [slot for slot in slots if slot.get("date") == delivery_date]
        return {"provider": "meny", "slots": slots}

    def _wait_for_delivery_reservation(self) -> None:
        requests: Any = {"requests": []}
        for attempt in range(120):
            requests = self._invoke("network", "requests")
            if meny_delivery_reservation_acknowledged(requests):
                return
            if attempt < 119:
                self._sleep(0.25)
        raise HouseholdError(
            "MENY did not acknowledge the delivery reservation; inspect the selected slot before retrying"
        )

    def _select_delivery_slot(self, value: Any, *, _allow_refresh: bool = True) -> dict[str, Any]:
        slot_id, expected_suffix = normalize_delivery_slot_ref(value)
        self._open_delivery_picker()
        marked: dict[str, Any] = {}
        for _ in range(20):
            marked = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const wanted = WANTED, expectedSuffix = EXPECTED_SUFFIX;
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const dialogs = [...document.querySelectorAll('dialog,[role="dialog"]')].filter(visible).filter(x => [...x.querySelectorAll('h1')].filter(visible).filter(h => norm(h.innerText) === 'Når skal vi levere til deg?').length === 1);
  if (!identity || authenticated.length !== 1 || dialogs.length !== 1) return JSON.stringify({ready:false, identity, authenticated:authenticated.length === 1});
  const dismiss = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText) === 'Lukk');
  const buttons = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => norm(x.getAttribute('aria-label') || x.innerText) === wanted);
  if (dismiss.length !== 1 || buttons.length !== 1) return JSON.stringify({ready:false, identity, authenticated:true});
  if (buttons[0].getAttribute('aria-pressed') === 'true') {
    const slotPattern = /^(?:fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kr\s+fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kroner,\s*)?(?:0?[1-9]|[12]\d|3[01])\.\s*(?:januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember)\s+klokka\s+(?:[01]?\d|2[0-3]):[0-5]\d\s+til\s+(?:[01]?\d|2[0-3]):[0-5]\d$/i;
    const selected = [...dialogs[0].querySelectorAll('button[aria-pressed="true"]')].filter(visible).filter(x => slotPattern.test(norm(x.getAttribute('aria-label') || x.innerText)));
    const keep = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => {
      const parts = norm(x.getAttribute('aria-label') || x.innerText).match(/^Behold levering (?:mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(.+)$/i);
      return parts && parts[1].toLocaleLowerCase('nb-NO') === expectedSuffix;
    });
    const alternatives = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => x !== buttons[0] && slotPattern.test(norm(x.getAttribute('aria-label') || x.innerText)));
    if (selected.length !== 1 || keep.length > 1 || (keep.length === 1 && alternatives.length === 0)) return JSON.stringify({ready:false, identity, authenticated:true, selected_count:selected.length});
    if (keep.length === 1) {
      alternatives[0].setAttribute('data-hermes-meal-planner-action', 'delivery-refresh-slot');
      return JSON.stringify({ready:true, identity, authenticated:true, already_selected:true, refresh_available:true, refresh_slot:norm(alternatives[0].getAttribute('aria-label') || alternatives[0].innerText)});
    }
    dismiss[0].setAttribute('data-hermes-meal-planner-action', 'delivery-dismiss');
    return JSON.stringify({ready:true, identity, authenticated:true, already_selected:true, refresh_available:false});
  }
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'delivery-slot');
  return JSON.stringify({ready:true, identity, authenticated:true, already_selected:false});
})()
""".replace("WANTED", json.dumps(slot_id, ensure_ascii=False)).replace("EXPECTED_SUFFIX", json.dumps(expected_suffix, ensure_ascii=False)))
            if marked.get("identity") is not True:
                raise HouseholdError("MENY delivery route changed")
            if marked.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if marked.get("ready") is True:
                break
            self._sleep(0.25)
        else:
            raise HouseholdError("MENY delivery slot changed or is unavailable")
        selected_slot_id = slot_id
        selected_suffix = expected_suffix
        refreshing = False
        if marked.get("already_selected") is True and marked.get("refresh_available") is not True:
            self._invoke("click", '[data-hermes-meal-planner-action="delivery-dismiss"]')
            self._wait_delivery_picker_closed()
            return {"provider": "meny", "selected": {"slot_id": slot_id, "display": slot_id}}
        if marked.get("already_selected") is True:
            if not _allow_refresh:
                raise HouseholdError("MENY delivery reservation could not be refreshed")
            selected_slot_id, selected_suffix = normalize_delivery_slot_ref(marked.get("refresh_slot"))
            if selected_slot_id == slot_id:
                raise HouseholdError("MENY delivery refresh slot is invalid")
            refreshing = True
            self._invoke("click", '[data-hermes-meal-planner-action="delivery-refresh-slot"]')
        else:
            self._invoke("click", '[data-hermes-meal-planner-action="delivery-slot"]')
        for _ in range(20):
            confirmation = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const wanted = WANTED;
  const expectedSuffix = EXPECTED_SUFFIX;
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const dialogs = [...document.querySelectorAll('dialog,[role="dialog"]')].filter(visible).filter(x => [...x.querySelectorAll('h1')].filter(visible).filter(h => norm(h.innerText) === 'Når skal vi levere til deg?').length === 1);
  if (!identity || authenticated.length !== 1 || dialogs.length !== 1) return JSON.stringify({ready:false, identity, authenticated:authenticated.length === 1});
  const slotPattern = /^(?:fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kr\s+fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kroner,\s*)?(?:0?[1-9]|[12]\d|3[01])\.\s*(?:januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember)\s+klokka\s+(?:[01]?\d|2[0-3]):[0-5]\d\s+til\s+(?:[01]?\d|2[0-3]):[0-5]\d$/i;
  const allSelected = [...dialogs[0].querySelectorAll('button[aria-pressed="true"]')].filter(visible).filter(x => slotPattern.test(norm(x.getAttribute('aria-label') || x.innerText)));
  const selected = allSelected.filter(x => norm(x.getAttribute('aria-label') || x.innerText) === wanted);
  const confirm = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => {
    const label = norm(x.getAttribute('aria-label') || x.innerText);
    const parts = label.match(/^Bekreft levering (?:mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(.+)$/i);
    return label === 'Bekreft levering' || (parts && parts[1].toLocaleLowerCase('nb-NO') === expectedSuffix);
  });
  const keep = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => {
    const label = norm(x.getAttribute('aria-label') || x.innerText);
    const parts = label.match(/^Behold levering (?:mandag|tirsdag|onsdag|torsdag|fredag|lørdag|søndag)\s+(.+)$/i);
    return parts && parts[1].toLocaleLowerCase('nb-NO') === expectedSuffix;
  });
  if (allSelected.length !== 1 || selected.length !== 1 || confirm.length + keep.length !== 1) return JSON.stringify({ready:false, identity, authenticated:true, selected_count:selected.length, total_selected_count:allSelected.length});
  (confirm[0] || keep[0]).setAttribute('data-hermes-meal-planner-action', 'delivery-confirm');
  return JSON.stringify({ready:true, identity, authenticated:true, selected_count:1, total_selected_count:1, keeping_existing:keep.length === 1});
})()
""".replace("WANTED", json.dumps(selected_slot_id, ensure_ascii=False)).replace("EXPECTED_SUFFIX", json.dumps(selected_suffix, ensure_ascii=False)))
            if confirmation.get("identity") is not True:
                raise HouseholdError("MENY delivery route changed")
            if confirmation.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if confirmation.get("ready") is True:
                break
            self._sleep(0.25)
        else:
            raise HouseholdError("MENY delivery confirmation changed")
        self._invoke("network", "requests", "--clear")
        self._invoke("click", '[data-hermes-meal-planner-action="delivery-confirm"]')
        self._wait_for_delivery_reservation()
        self._wait_delivery_picker_closed()
        if refreshing:
            try:
                return self._select_delivery_slot(slot_id, _allow_refresh=False)
            except HouseholdError as exc:
                raise HouseholdError(
                    "MENY delivery refresh stopped on a temporary slot; select the requested slot again"
                ) from exc
        self._open_delivery_picker()
        selected: dict[str, Any] = {}
        for _ in range(20):
            selected = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const wanted = WANTED;
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const dialogs = [...document.querySelectorAll('dialog,[role="dialog"]')].filter(visible).filter(x => [...x.querySelectorAll('h1')].filter(visible).filter(h => norm(h.innerText) === 'Når skal vi levere til deg?').length === 1);
  if (!identity || authenticated.length !== 1 || dialogs.length !== 1) return JSON.stringify({ready:false, identity, authenticated:authenticated.length === 1});
  const slotPattern = /^(?:fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kr\s+fra\s+\d+(?:[ .]\d{3})*(?:,\d{2})?\s+kroner,\s*)?(?:0?[1-9]|[12]\d|3[01])\.\s*(?:januar|februar|mars|april|mai|juni|juli|august|september|oktober|november|desember)\s+klokka\s+(?:[01]?\d|2[0-3]):[0-5]\d\s+til\s+(?:[01]?\d|2[0-3]):[0-5]\d$/i;
  const allSelected = [...dialogs[0].querySelectorAll('button[aria-pressed="true"]')].filter(visible).filter(x => slotPattern.test(norm(x.getAttribute('aria-label') || x.innerText)));
  const selected = allSelected.filter(x => norm(x.getAttribute('aria-label') || x.innerText) === wanted);
  const dismiss = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText) === 'Lukk');
  if (allSelected.length !== 1 || selected.length !== 1 || dismiss.length !== 1) return JSON.stringify({ready:false, identity, authenticated:true, selected_count:selected.length, total_selected_count:allSelected.length});
  dismiss[0].setAttribute('data-hermes-meal-planner-action', 'delivery-dismiss');
  return JSON.stringify({ready:true, identity, authenticated:true, selected_count:1, total_selected_count:1});
})()
""".replace("WANTED", json.dumps(slot_id, ensure_ascii=False)))
            if selected.get("identity") is not True:
                raise HouseholdError("MENY delivery route changed")
            if selected.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if selected.get("ready") is True:
                break
            self._sleep(0.25)
        else:
            raise HouseholdError("MENY selected delivery could not be verified")
        self._invoke("click", '[data-hermes-meal-planner-action="delivery-dismiss"]')
        self._wait_delivery_picker_closed()
        return {"provider": "meny", "selected": {"slot_id": slot_id, "display": slot_id}}

    def _get_orders(self, limit: int) -> dict[str, Any]:
        self._invoke("network", "requests", "--clear")
        self._open(ORDERS_URL)
        script = r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const orders = [], seen = new Set();
  for (const anchor of [...document.querySelectorAll('a[href*="/profil/nettbutikk/bestilling/"]')].filter(visible)) {
    const url = new URL(anchor.href, location.origin), match = url.pathname.match(/^\/profil\/nettbutikk\/bestilling\/(\d{1,20})$/);
    if (!match || seen.has(match[1])) continue;
    seen.add(match[1]);
    const root = anchor.closest('tr,li,article,section') || anchor;
    const summary = norm(root.innerText);
    const status = /kansellert/i.test(summary) ? 'cancelled' : /levert/i.test(summary) ? 'delivered' : /bekreftet|mottatt/i.test(summary) ? 'confirmed' : 'unknown';
    orders.push({order_number:match[1], id:match[1], status, summary});
  }
  return JSON.stringify({ready:authenticated.length === 1 && Boolean(document.querySelector('main')), authenticated:authenticated.length === 1, orders});
})()
"""
        result: dict[str, Any] = {}
        search_completed = False
        for phase in range(2):
            for attempt in range(40):
                if not search_completed:
                    search_completed = meny_order_search_completed(
                        self._invoke("network", "requests", "--filter", "/api/order/search/")
                    )
                    if search_completed:
                        self._sleep(0.5)
                result = self._eval(script)
                if result.get("authenticated") is True and search_completed and result.get("ready") is True and isinstance(result.get("orders"), list):
                    return {"provider": "meny", "orders": result["orders"][:limit]}
                if (
                    phase == 0
                    and attempt >= 3
                    and not search_completed
                    and result.get("authenticated") is True
                    and result.get("ready") is True
                    and isinstance(result.get("orders"), list)
                ):
                    break
                if attempt < 39:
                    self._sleep(0.25)
            if phase == 0 and not search_completed and result.get("authenticated") is True:
                self._invoke("network", "requests", "--clear")
                self._invoke("reload")
                self._sleep(0.5)
                continue
            break
        if result.get("authenticated") is not True:
            raise HouseholdError("MENY login is required in the configured browser profile")
        raise HouseholdError("MENY orders did not finish rendering")

    def _get_order(self, order_id: str) -> dict[str, Any]:
        path = f"/profil/nettbutikk/bestilling/{order_id}"
        self._open(BASE_URL + path)
        self._sleep(1.5)
        script = r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const expected = EXPECTED;
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const mains = [...document.querySelectorAll('main')].filter(visible);
  if (authenticated.length !== 1 || mains.length !== 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1});
  const root = mains[0], lines = (root.innerText || '').split(/\n+/).map(norm).filter(Boolean);
  const position = label => lines.findIndex(line => line === label);
  const valueAfter = label => { const index=position(label); return index >= 0 ? lines[index+1] || null : null; };
  const actual = valueAfter('Ordrenummer');
  const heading = [...root.querySelectorAll('h1')].filter(visible).map(x => norm(x.innerText)).filter(x => /^Bestilling\s+\S+/i.test(x));
  const totalText = valueAfter('Betalt beløp (kort)') || valueAfter('Reservert beløp (kort)') || valueAfter('Reservert beløp') || valueAfter('Totalsum');
  const money = totalText?.match(/(\d+(?:[ .]\d{3})*),([0-9]{2})/);
  const total = money ? Number(`${money[1].replace(/[ .]/g,'')}.${money[2]}`) : null;
  const deliveredDatePattern = /^(?:0?[1-9]|[12]\d|3[01])\.(?:\s+(?:jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\.?\s+\d{4}|\d{2}\.\d{4})$/i;
  const deliveredDates = lines.flatMap((line, index) => /\blevert$/i.test(line) && deliveredDatePattern.test(lines[index + 1] || '') ? [lines[index + 1]] : []);
  const delivery = valueAfter('Varene leveres') || (deliveredDates.length === 1 ? deliveredDates[0] : null);
  const code = valueAfter('Bestillingskode (for henting/levering)') || heading[0]?.replace(/^Bestilling\s+/, '') || null;
  const text = norm(root.innerText);
  const status = /bestillingen er kansellert|kansellert bestilling/i.test(text) ? 'cancelled' : deliveredDates.length === 1 ? 'delivered' : /bestillingen kan oppdateres|bekreftet/i.test(text) ? 'confirmed' : 'unknown';
  const itemButtons = [...root.querySelectorAll('button')].filter(visible).map(x => norm(x.innerText)).map(x => x.match(/^Bestilte varer \((\d+)\)$/)).filter(Boolean);
  const itemCount = itemButtons.length === 1 ? Number(itemButtons[0][1]) : null;
  const tables = [...root.querySelectorAll('table')].filter(visible).filter(table => {
    const cells = [...(table.querySelector('tr')?.querySelectorAll('th,td') || [])].map(x => norm(x.innerText).toLocaleUpperCase('nb-NO'));
    return cells.length === 2 && cells[0] === 'VARE' && cells[1] === 'MENGDE';
  });
  const products = [];
  let rowsReady = false;
  if (tables.length === 1) {
    const rows = [...tables[0].querySelectorAll('tr')].slice(1);
    rowsReady = rows.length > 0;
    for (const row of rows) {
      const cells = [...row.querySelectorAll('td')].map(x => norm(x.innerText));
      const quantity = cells.length === 2 ? cells[1].match(/^(\d+)\s*stk$/i) : null;
      if (cells.length !== 2 || !cells[0] || !quantity || Number(quantity[1]) < 1) { rowsReady=false; break; }
      products.push({identity:cells[0], name:cells[0], quantity:Number(quantity[1])});
    }
    if (products.reduce((sum,item) => sum + item.quantity, 0) !== itemCount) rowsReady=false;
  }
  const buttons = [...root.querySelectorAll('button')].filter(visible).filter(x => /^Bestilte varer \(\d+\)$/.test(norm(x.innerText)));
  const expand = !rowsReady && buttons.length === 1 && buttons[0].getAttribute('aria-expanded') !== 'true';
  if (expand) buttons[0].setAttribute('data-hermes-meal-planner-action', 'order-items');
  const baseReady = actual === expected && location.pathname === `/profil/nettbutikk/bestilling/${expected}` && heading.length === 1 && Number.isFinite(total) && Boolean(delivery) && Number.isInteger(itemCount) && itemCount > 0;
  return JSON.stringify({ready:baseReady && rowsReady, expand:baseReady && expand, authenticated:true, order_number:actual, code, status, total, delivery, item_count:itemCount, products});
})()
""".replace("EXPECTED", json.dumps(order_id))
        result: dict[str, Any] = {}
        expanded = False
        for _ in range(20):
            result = self._eval(script)
            if result.get("authenticated") is not True:
                raise HouseholdError("MENY login is required in the configured browser profile")
            if result.get("ready") is True:
                break
            if result.get("expand") is True and not expanded:
                self._invoke("click", '[data-hermes-meal-planner-action="order-items"]')
                expanded = True
            self._sleep(0.25)
        else:
            raise HouseholdError("MENY order details changed or are unavailable")
        return {
            "provider": "meny",
            "orderNumber": order_id,
            "order_number": order_id,
            "id": order_id,
            "code": result.get("code"),
            "status": result["status"],
            "grossAmount": result["total"],
            "deliverySlotDisplay": result["delivery"],
            "productQuantityCount": result["item_count"],
            "products": result["products"],
        }

    def checkout_confirmation_order_id(self, *, deadline: float | None = None) -> str | None:
        with self._locked_operation(MENY_READ_TIMEOUT, deadline):
            result = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const main = [...document.querySelectorAll('main')].filter(visible);
  const url = new URL(location.href), orderId = url.pathname === '/kassen/bekreftelse' ? url.searchParams.get('orderid') : null;
  const vippsGateway = url.origin === 'https://api.vipps.no' && url.pathname === '/dwo-api-application/v1/deeplink/vippsgateway';
  const confirmed = main.length === 1 && /Takk for (?:din )?bestilling(?:en)?|Bestillingen (?:er|ble) (?:mottatt|oppdatert)|Ordrebekreftelse/i.test(norm(main[0].innerText));
  return JSON.stringify({authenticated:authenticated.length === 1, vipps_gateway:vippsGateway, order_id:confirmed && /^\d{1,20}$/.test(orderId || '') ? orderId : null});
})()
""")
            if result.get("authenticated") is not True:
                if result.get("vipps_gateway") is True and result.get("order_id") is None:
                    return None
                raise HouseholdError("MENY login is required in the configured browser profile")
            order_id = result.get("order_id")
            return str(order_id) if order_id is not None else None

    def checkout_payment_not_dispatched(self, review: Mapping[str, Any], *, deadline: float | None = None) -> bool:
        """Prove that a fenced click never left the unchanged MENY checkout page."""

        with self._locked_operation(MENY_READ_TIMEOUT, deadline):
            if vipps_dispatch_attempted(self._invoke("network", "requests")):
                return False
            result = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const main = [...document.querySelectorAll('main')].filter(visible);
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  if (main.length !== 1 || authenticated.length !== 1) return JSON.stringify({ready:false});
  const root = main[0], leaf = selector => [...root.querySelectorAll(selector)].filter(visible).filter(x => ![...x.children].some(child => visible(child) && norm(child.innerText) === norm(x.innerText)));
  const text = norm(root.innerText), expectedCode = __CODE__;
  const activeCodes = [...text.matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(match => match[1]);
  const targetReady = expectedCode === null ? activeCodes.length === 0 : activeCodes.length === 1 && activeCodes[0] === expectedCode;
  const vipps = [...root.querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Vipps(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  const checked = vipps.length === 1 && (vipps[0].checked === true || vipps[0].getAttribute('aria-checked') === 'true');
  const home = [...root.querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Levert på døren(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  const homeChecked = home.length === 1 && (home[0].checked === true || home[0].getAttribute('aria-checked') === 'true');
  const buttons = [...root.querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Til betaling');
  const blockingDialogs = [...document.querySelectorAll('[role="dialog"],[role="alertdialog"],[aria-modal="true"]')].filter(visible);
  const totalLabels = leaf('*').filter(x => norm(x.innerText) === 'Totalsum');
  const totals = [];
  for (const label of totalLabels) {
    let row = label.parentElement;
    for (let depth=0; row && depth<3; depth++, row=row.parentElement) {
      const values = [...norm(row.innerText).matchAll(/(?:^|\s)(\d+(?:[ .]\d{3})*),([0-9]{2})(?:\s|$)/g)].map(m => Number(`${m[1].replace(/[ .]/g,'')}.${m[2]}`));
      if (values.length === 1) { totals.push(values[0]); break; }
    }
  }
__DELIVERY_BINDING__
  const exact = checked && homeChecked && targetReady && buttons.length === 1 && blockingDialogs.length === 0 && totalLabels.length === 1 && totals.length === 1 && Math.round(totals[0]*100) === __TOTAL__ && deliveryBinding?.root && deliveryBinding.display === __DELIVERY__ && location.href === __URL__;
  return JSON.stringify({ready:Boolean(exact)});
})()
""".replace("__DELIVERY_BINDING__", CHECKOUT_DELIVERY_BINDING_JS).replace("__CODE__", json.dumps(review.get("target_order_code"))).replace("__TOTAL__", str(int(round(float(review["summary"]["total"]) * 100)))).replace("__DELIVERY__", json.dumps(review["summary"]["delivery"]["display"], ensure_ascii=False)).replace("__URL__", json.dumps(CHECKOUT_URL)))
            if result != {"ready": True}:
                return False
            return not vipps_dispatch_attempted(self._invoke("network", "requests"))

    def verify_order_change(self, order_id: str | None, code: str | None, *, deadline: float | None = None) -> dict[str, Any]:
        with self._locked_operation(MENY_ORDER_TIMEOUT, deadline):
            return self._verify_order_change(order_id, code)

    def _verify_order_change(self, order_id: str | None, code: str | None) -> dict[str, Any]:
        if (order_id is None) != (code is None):
            raise HouseholdError("MENY order change identity is incomplete")
        if order_id is not None:
            self._order_id(order_id)
            code = str(code).strip()
            if not re.fullmatch(r"[A-Za-z0-9-]{2,40}", code):
                raise HouseholdError("MENY order change identity is invalid")
        self._open(STORE_URL)
        self._sleep(0.25)
        state_script = r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible);
  if (identity && carts.length === 1) return JSON.stringify({ready:true, open:true, authenticated:authenticated.length === 1});
  const open = [...document.querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => norm(x.getAttribute('aria-label')) === 'Åpne handlevognen');
  if (!identity || carts.length !== 0 || open.length !== 1) return JSON.stringify({ready:false, open:false, authenticated:authenticated.length === 1});
  open[0].setAttribute('data-hermes-meal-planner-action', 'verify-cart-open');
  return JSON.stringify({ready:true, open:false, authenticated:authenticated.length === 1});
})()
"""
        state: dict[str, Any] = {}
        for attempt in range(20):
            state = self._eval(state_script)
            if state.get("ready") is True:
                break
            if attempt < 19:
                self._sleep(0.25)
        if state.get("authenticated") is not True:
            raise HouseholdError("MENY login is required in the configured browser profile")
        if state.get("ready") is not True:
            raise HouseholdError("MENY cart is unavailable")
        if state.get("open") is not True:
            self._invoke("click", '[data-hermes-meal-planner-action="verify-cart-open"]')
            self._sleep(0.4)
        result_script = r"""
(() => {
  const expected = CODE;
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const identity = location.origin === 'https://meny.no' && location.pathname === '/varer' && !location.search && !location.hash;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible);
  if (!identity || authenticated.length !== 1 || carts.length !== 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1});
  const text = norm(carts[0].innerText);
  const aborts = [...carts[0].querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Avbryt endring');
  const activeCodes = [...text.matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(x => x[1]);
  const active = activeCodes.length === 1 && aborts.length === 1;
  const ready = expected === null ? (!active && activeCodes.length === 0 && aborts.length === 0) : (active && activeCodes[0] === expected);
  return JSON.stringify({ready, authenticated:true, active, code:active ? activeCodes[0] : null});
})()
""".replace("CODE", json.dumps(code))
        result: dict[str, Any] = {}
        for attempt in range(20):
            result = self._eval(result_script)
            if result.get("ready") is True:
                break
            if attempt < 19:
                self._sleep(0.25)
        if result.get("authenticated") is not True:
            raise HouseholdError("MENY login is required in the configured browser profile")
        if result.get("ready") is not True:
            target = f" {order_id}" if order_id else ""
            raise HouseholdError(f"MENY browser is not in the expected order-change mode{target}")
        return {"provider": "meny", "order_id": order_id, "code": code, "editing": order_id is not None}

    def begin_order_change(self, order_id: str, *, deadline: float | None = None) -> dict[str, Any]:
        with self._locked_operation(MENY_ORDER_TIMEOUT, deadline):
            order_id = self._order_id(order_id)
            order = self._get_order(order_id)
            code = str(order.get("code") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9-]{2,40}", code):
                raise HouseholdError("MENY order change identity is unavailable")
            result = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const buttons = [...document.querySelectorAll('main button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => norm(x.innerText) === 'Endre');
  if (buttons.length !== 1) return JSON.stringify({ready:false});
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'change-open');
  return JSON.stringify({ready:true});
})()
""")
            if result != {"ready": True}:
                raise HouseholdError("MENY order cannot be changed now")
            self._invoke("click", '[data-hermes-meal-planner-action="change-open"]')
            self._sleep(0.25)
            dialog = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible).filter(x => /Vil du endre bestillingen\?/i.test(norm(x.innerText)));
  if (dialogs.length !== 1) return JSON.stringify({ready:false});
  const buttons = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => norm(x.innerText) === 'Endre bestilling');
  if (buttons.length !== 1) return JSON.stringify({ready:false});
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'change-confirm');
  return JSON.stringify({ready:true});
})()
""")
            if dialog != {"ready": True}:
                raise HouseholdError("MENY order change confirmation changed")
            self._invoke("click", '[data-hermes-meal-planner-action="change-confirm"]')
            for _ in range(40):
                ready = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const expected = CODE;
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible);
  const exact = carts.filter(x => {
    const codes = [...norm(x.innerText).matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(match => match[1]);
    return codes.length === 1 && codes[0] === expected;
  });
  return JSON.stringify({ready:authenticated.length === 1 && exact.length === 1});
})()
""".replace("CODE", json.dumps(code)))
                if ready == {"ready": True}:
                    verified = self._verify_order_change(order_id, code)
                    return {"provider": "meny", "order_id": order_id, "code": verified["code"], "order": order, "editing": True}
                self._sleep(0.25)
            raise HouseholdError("MENY order did not enter change mode")

    def abort_order_change(self, order_id: str, code: str | None = None, *, deadline: float | None = None) -> dict[str, Any]:
        with self._locked_operation(MENY_ORDER_TIMEOUT, deadline):
            order_id = self._order_id(order_id)
            self._verify_order_change(order_id, code)
            opened = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const expected = CODE;
  const carts = [...document.querySelectorAll('[aria-label="Handlevogn"]')].filter(visible).filter(x => {
    const codes = [...norm(x.innerText).matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(match => match[1]);
    return codes.length === 1 && codes[0] === expected;
  });
  const buttons = carts.length === 1 ? [...carts[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => norm(x.innerText) === 'Avbryt endring') : [];
  if (buttons.length !== 1) return JSON.stringify({ready:false});
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'change-abort-open');
  return JSON.stringify({ready:true});
})()
""".replace("CODE", json.dumps(str(code))))
            if opened != {"ready": True}:
                raise HouseholdError("MENY order change is not active")
            self._invoke("click", '[data-hermes-meal-planner-action="change-abort-open"]')
            self._sleep(0.25)
            final = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible).filter(x => /Vil du avbryte endringen\?/i.test(norm(x.innerText)));
  if (dialogs.length !== 1) return JSON.stringify({ready:false});
  const buttons = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => norm(x.innerText) === 'Avbryt endring');
  if (buttons.length !== 1) return JSON.stringify({ready:false});
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'change-abort-final');
  return JSON.stringify({ready:true});
})()
""")
            if final != {"ready": True}:
                raise HouseholdError("MENY order change abort confirmation changed")
            self._invoke("click", '[data-hermes-meal-planner-action="change-abort-final"]')
            self._sleep(0.5)
            order = self._get_order(order_id)
            return {"provider": "meny", "order_id": order_id, "aborted": True, "order": order}

    def review_checkout(self, cart: Mapping[str, Any], *, order_change: Mapping[str, Any] | None = None, deadline: float | None = None, allow_recovery: bool = False) -> dict[str, Any]:
        with self._locked_operation(MENY_ORDER_TIMEOUT, deadline, allow_recovery=allow_recovery):
            return self._review_checkout(cart, order_change=order_change)

    def _review_checkout(self, cart: Mapping[str, Any], *, order_change: Mapping[str, Any] | None = None) -> dict[str, Any]:
        expected = cart_summary(cart)
        if not expected["items"]:
            raise HouseholdError("cart is empty")
        if expected.get("delivery") is None:
            selected_delivery = meny_selected_delivery(self._delivery_slots().get("slots"))
            if selected_delivery is not None:
                self._select_delivery_slot(selected_delivery["slot_id"])
            expected["delivery"] = selected_delivery
        if expected.get("delivery") is None:
            raise HouseholdError("select a MENY delivery slot before checkout")
        target_order_id = str(order_change.get("order_id") or "") if order_change else ""
        target_code = str(order_change.get("code") or "") if order_change else ""
        self._verify_order_change(target_order_id or None, target_code or None)
        self._open(CHECKOUT_URL)
        self._sleep(0.8)
        step_script = r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const main = [...document.querySelectorAll('main')].filter(visible);
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  if (main.length !== 1 || authenticated.length !== 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1});
  const root = main[0], text = norm(root.innerText), target = TARGET;
  const activeCodes = [...text.matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(match => match[1]);
  const active = activeCodes.length === 1;
  const targetReady = target === null ? activeCodes.length === 0 : active && activeCodes[0] === target;
  const buttons = [...root.querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Neste');
  const enabled = buttons.length === 1 && !buttons[0].disabled && buttons[0].getAttribute('aria-disabled') !== 'true';
  const productPattern = /^\/varer\/(?!kampanjer\/)[A-Za-z0-9._~%/-]+-\d{4,14}$/;
  const unavailableHeadings = [...root.querySelectorAll('h1,h2,h3')].filter(visible).filter(x => norm(x.innerText) === 'Disse varene vil du ikke motta');
  const unavailableSections = unavailableHeadings.map(x => x.closest('.ws-checkout-page-section')).filter(x => x && visible(x));
  const unavailableItems = [];
  let unavailableReady = unavailableHeadings.length === 0;
  if (unavailableHeadings.length === 1 && unavailableSections.length === 1) {
    const unavailableControls = [...unavailableSections[0].querySelectorAll('select[aria-label*="endre mengde"]')].filter(visible);
    unavailableReady = unavailableControls.length > 0;
    for (const select of unavailableControls) {
      const item = select.closest('.ws-product');
      const paths = item ? [...new Set([...item.querySelectorAll('a[href]')].map(link => new URL(link.href, location.origin)).filter(url => url.origin === location.origin && productPattern.test(url.pathname)).map(url => url.pathname))] : [];
      const name = norm(item?.querySelector('.ws-product__title')?.innerText);
      const packageText = norm(item?.querySelector('.ws-product__subtitle')?.innerText);
      const quantity = Number.parseInt(norm(select.selectedOptions?.[0]?.innerText), 10);
      const identity = norm(`${name} ${packageText}`);
      if (!item || paths.length !== 1 || !identity || !Number.isInteger(quantity) || quantity < 1) { unavailableReady=false; break; }
      unavailableItems.push({product_id:paths[0], identity, quantity});
    }
  }
  const controls = [...root.querySelectorAll('select[aria-label*="endre mengde"]')].filter(visible);
  const items = [];
  for (const select of controls) {
    const item = select.closest('.ws-product');
    const paths = item ? [...new Set([...item.querySelectorAll('a[href]')].map(link => new URL(link.href, location.origin)).filter(url => url.origin === location.origin && productPattern.test(url.pathname)).map(url => url.pathname))] : [];
    const name = norm(item?.querySelector('.ws-product__title')?.innerText);
    const packageText = norm(item?.querySelector('.ws-product__subtitle')?.innerText);
    const quantity = Number.parseInt(norm(select.selectedOptions?.[0]?.innerText), 10);
    const identity = norm(`${name} ${packageText}`);
    if (!item || paths.length !== 1 || !name || !identity || !Number.isInteger(quantity) || quantity < 1) return JSON.stringify({ready:false, authenticated:true});
    items.push({product_id:paths[0], identity, quantity});
  }
  const minimumMessage = text.match(/Du må handle for \d+(?:[ .]\d{3})*,\d{2}\s*kr til for å få varene levert på døren\.?/i)?.[0] || null;
  const ready = /Se over varene/.test(text) && targetReady && unavailableReady && buttons.length === 1 && controls.length > 0 && items.length === controls.length;
  if (ready && enabled) buttons[0].setAttribute('data-hermes-meal-planner-action', 'checkout-next');
  return JSON.stringify({ready, authenticated:true, step:1, next_enabled:enabled, minimum_message:minimumMessage, items, unavailable_items:unavailableItems, active_order_change:active});
})()
""".replace("TARGET", json.dumps(target_code or None))
        step: dict[str, Any] = {}
        for attempt in range(120):
            step = self._eval(step_script)
            unavailable_items = step.get("unavailable_items")
            if (
                step.get("authenticated") is True
                and step.get("ready") is True
                and isinstance(unavailable_items, list)
                and (unavailable_items or step.get("next_enabled") is True)
            ):
                break
            if attempt < 119:
                self._sleep(0.25)
        if step.get("authenticated") is not True:
            raise HouseholdError("MENY login is required in the configured browser profile")
        if step.get("ready") is not True:
            raise HouseholdError("MENY checkout did not finish rendering")
        unavailable_items = step.get("unavailable_items")
        if not isinstance(unavailable_items, list):
            raise HouseholdError("MENY checkout unavailable-item state changed")
        if unavailable_items:
            identities = []
            for item in unavailable_items:
                if not isinstance(item, Mapping):
                    raise HouseholdError("MENY checkout unavailable-item state changed")
                normalize_product_ref(item.get("product_id"))
                identity = " ".join(str(item.get("identity") or "").split())
                quantity = item.get("quantity")
                if not identity or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                    raise HouseholdError("MENY checkout unavailable-item state changed")
                identities.append(identity)
            raise HouseholdError(f"MENY cart contains unavailable items: {', '.join(identities)}")
        observed_items: list[tuple[str, int]] = []
        order_lines: list[dict[str, Any]] = []
        for item in step.get("items", []):
            if not isinstance(item, Mapping):
                raise HouseholdError("MENY checkout item identity changed")
            product_id = normalize_product_ref(item.get("product_id"))
            identity = " ".join(str(item.get("identity") or "").split())
            quantity = item.get("quantity")
            if not identity or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
                raise HouseholdError("MENY checkout item identity changed")
            observed_items.append((product_id, quantity))
            order_lines.append({"product_id": product_id, "identity": identity, "quantity": quantity})
        expected_items = sorted((str(item["product_id"]), int(item["quantity"])) for item in expected["items"])
        if sorted(observed_items) != expected_items:
            raise HouseholdError("MENY checkout items changed after the cart was reviewed")
        if step.get("next_enabled") is not True:
            minimum_message = " ".join(str(step.get("minimum_message") or "").split())
            if re.fullmatch(
                r"Du må handle for \d+(?:[ .]\d{3})*,\d{2}\s*kr til for å få varene levert på døren\.?",
                minimum_message,
                flags=re.IGNORECASE,
            ):
                raise HouseholdError(f"MENY checkout cannot continue: {minimum_message}")
            raise HouseholdError("MENY checkout cannot continue; check the home-delivery minimum and cart messages")
        self._click_checkout_control("checkout-next", expected_items=expected_items, target_code=target_code or None)
        self._sleep(0.6)
        unavailable = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible).filter(x => /Noen av varene har vi dessverre ikke/i.test(norm(x.innerText)));
  if (dialogs.length !== 1) return JSON.stringify({unavailable:false, dismiss:false});
  const dismiss = [...dialogs[0].querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Avbryt');
  if (dismiss.length === 1) dismiss[0].setAttribute('data-hermes-meal-planner-action', 'checkout-unavailable-dismiss');
  return JSON.stringify({unavailable:true, dismiss:dismiss.length === 1});
})()
""")
        if unavailable.get("unavailable") is True:
            if unavailable.get("dismiss") is True:
                self._invoke("click", '[data-hermes-meal-planner-action="checkout-unavailable-dismiss"]')
            raise HouseholdError("MENY cart contains unavailable items; adjust the cart before preparing checkout")
        for _ in range(20):
            payment = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const main = [...document.querySelectorAll('main')].filter(visible);
  if (main.length !== 1 || !/Leverings- og betalingsinformasjon/.test(norm(main[0].innerText))) return JSON.stringify({ready:false});
  const vipps = [...main[0].querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Vipps(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  if (vipps.length !== 1) return JSON.stringify({ready:false});
  const checked = vipps[0].checked === true || vipps[0].getAttribute('aria-checked') === 'true';
  if (!checked) (vipps[0].closest('label') || vipps[0]).setAttribute('data-hermes-meal-planner-action', 'vipps');
  return JSON.stringify({ready:true, checked});
})()
""")
            if payment.get("ready") is True:
                break
            self._sleep(0.25)
        else:
            raise HouseholdError("MENY payment page did not finish rendering")
        if payment.get("checked") is not True:
            self._click_checkout_control("vipps")
            self._sleep(0.25)
        reservation = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const main = [...document.querySelectorAll('main')].filter(visible);
  const text = main.length === 1 ? norm(main[0].innerText) : '';
  return JSON.stringify({ready:location.href === 'https://meny.no/kassen' && main.length === 1, lost:/Du har dessverre mistet din reservasjon/i.test(text)});
})()
""")
        if reservation.get("ready") is not True:
            raise HouseholdError("MENY checkout page changed")
        if reservation.get("lost") is True:
            raise HouseholdError("MENY delivery reservation expired; select the same delivery time again before checkout")
        payment_summary_script = r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const main = [...document.querySelectorAll('main')].filter(visible);
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  if (main.length !== 1 || authenticated.length !== 1) return JSON.stringify({ready:false, authenticated:authenticated.length === 1});
  const root = main[0], leaf = selector => [...root.querySelectorAll(selector)].filter(visible).filter(x => ![...x.children].some(child => visible(child) && norm(child.innerText) === norm(x.innerText)));
  const vipps = [...root.querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Vipps(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  const vippsChecked = vipps.length === 1 && (vipps[0].checked === true || vipps[0].getAttribute('aria-checked') === 'true');
  const home = [...root.querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Levert på døren(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  const homeChecked = home.length === 1 && (home[0].checked === true || home[0].getAttribute('aria-checked') === 'true');
  const buttons = [...root.querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Til betaling');
  const enabled = buttons.length === 1 && !buttons[0].disabled && buttons[0].getAttribute('aria-disabled') !== 'true';
  const totalLabels = leaf('*').filter(x => norm(x.innerText) === 'Totalsum');
  const totals = [];
  for (const label of totalLabels) {
    let row = label.parentElement;
    for (let depth=0; row && depth<3; depth++, row=row.parentElement) {
      const values = [...norm(row.innerText).matchAll(/(?:^|\s)(\d+(?:[ .]\d{3})*),([0-9]{2})(?:\s|$)/g)].map(m => Number(`${m[1].replace(/[ .]/g,'')}.${m[2]}`));
      if (values.length === 1) { totals.push(values[0]); break; }
    }
  }
__DELIVERY_BINDING__
  return JSON.stringify({ready:buttons.length===1 && totalLabels.length===1 && totals.length===1 && Boolean(deliveryBinding?.root) && Boolean(deliveryBinding.display), authenticated:true, vipps_checked:vippsChecked, home_delivery:homeChecked, submit_enabled:enabled, total:totals[0], delivery:deliveryBinding?.display, submit_controls:buttons.length});
})()
""".replace("__DELIVERY_BINDING__", CHECKOUT_DELIVERY_BINDING_JS)
        required = {"ready", "authenticated", "vipps_checked", "home_delivery", "submit_enabled", "total", "delivery", "submit_controls"}
        result: dict[str, Any] = {}
        payment_summary: dict[str, Any] | None = None
        for attempt in range(20):
            result = self._eval(payment_summary_script)
            if result.get("authenticated") is not True:
                break
            try:
                payment_summary = normalize_checkout_payment_snapshot(result)
            except HouseholdError:
                payment_summary = None
            if payment_summary is not None:
                break
            if attempt < 19:
                self._sleep(0.25)
        if set(result) != required or result.get("authenticated") is not True:
            raise HouseholdError("MENY checkout page changed")
        if result.get("ready") is not True or result.get("vipps_checked") is not True or result.get("home_delivery") is not True:
            raise HouseholdError("MENY checkout does not have one verified home-delivery Vipps payment")
        if result.get("submit_enabled") is not True:
            raise HouseholdError("MENY checkout cannot continue; check the home-delivery minimum and cart messages")
        payment_summary = normalize_checkout_payment_snapshot(result)
        expected_delivery = expected.get("delivery")
        if not isinstance(expected_delivery, Mapping) or meny_delivery_window_identity(expected_delivery.get("display")) != meny_delivery_window_identity(payment_summary["delivery"]):
            raise HouseholdError("MENY checkout delivery changed after the cart was reviewed")
        summary = {
            "items": expected["items"],
            "count": expected["count"],
            "total": payment_summary["total"],
            "delivery": {"slot_id": None, "display": payment_summary["delivery"], "address": None, "unattended": None},
            "payment": "vipps",
            "order_lines": order_lines,
        }
        digest = hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {
            "page_digest": digest,
            "summary": summary,
            "payment": "vipps",
            "submit_controls": 1,
            "target_order_id": target_order_id or None,
            "target_order_code": target_code or None,
        }

    def submit_checkout(self, cart: Mapping[str, Any], review: Mapping[str, Any], before_click: Any = None, *, order_change: Mapping[str, Any] | None = None, deadline: float | None = None) -> dict[str, Any]:
        final_dispatched = False
        try:
            with self._locked_operation(MENY_ORDER_TIMEOUT, deadline):
                final_cart = dict(cart)
                review_summary = review.get("summary")
                review_delivery = review_summary.get("delivery") if isinstance(review_summary, Mapping) else None
                if not isinstance(review_delivery, Mapping):
                    raise HouseholdError("MENY checkout delivery identity is unavailable")
                final_cart["delivery"] = dict(review_delivery)
                fresh = self._review_checkout(final_cart, order_change=order_change)
                if not meny_checkout_reviews_match(review, fresh):
                    raise HouseholdError("MENY checkout changed after review")
                ready = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const main = [...document.querySelectorAll('main')].filter(visible);
  const authenticated = [...document.querySelectorAll('button')].filter(visible).filter(x => norm(x.getAttribute('aria-label') || x.innerText).startsWith('Brukermeny'));
  if (main.length !== 1 || authenticated.length !== 1) return JSON.stringify({ready:false});
  const root = main[0], leaf = selector => [...root.querySelectorAll(selector)].filter(visible).filter(x => ![...x.children].some(child => visible(child) && norm(child.innerText) === norm(x.innerText)));
  const text = norm(root.innerText), expectedCode = __CODE__;
  const activeCodes = [...text.matchAll(/Du endrer bestilling\s+([A-Za-z0-9-]+)/gi)].map(match => match[1]);
  const active = activeCodes.length === 1;
  const targetReady = expectedCode === null ? activeCodes.length === 0 : active && activeCodes[0] === expectedCode;
  const vipps = [...root.querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Vipps(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  const checked = vipps.length === 1 && (vipps[0].checked === true || vipps[0].getAttribute('aria-checked') === 'true');
  const home = [...root.querySelectorAll('input[type="radio"], [role="radio"]')].filter(visible).filter(x => /^Levert på døren(?:\s|$)/i.test(norm(x.getAttribute('aria-label') || x.closest('label')?.innerText || x.parentElement?.innerText)));
  const homeChecked = home.length === 1 && (home[0].checked === true || home[0].getAttribute('aria-checked') === 'true');
  const buttons = [...root.querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Til betaling');
  const totalLabels = leaf('*').filter(x => norm(x.innerText) === 'Totalsum');
  const totals = [];
  for (const label of totalLabels) {
    let row = label.parentElement;
    for (let depth=0; row && depth<3; depth++, row=row.parentElement) {
      const values = [...norm(row.innerText).matchAll(/(?:^|\s)(\d+(?:[ .]\d{3})*),([0-9]{2})(?:\s|$)/g)].map(m => Number(`${m[1].replace(/[ .]/g,'')}.${m[2]}`));
      if (values.length === 1) { totals.push(values[0]); break; }
    }
  }
__DELIVERY_BINDING__
  const exact = checked && homeChecked && targetReady && buttons.length === 1 && totalLabels.length === 1 && totals.length === 1 && Math.round(totals[0]*100) === __TOTAL__ && deliveryBinding?.root && deliveryBinding.display === __DELIVERY__ && location.href === __URL__;
  if (!exact) return JSON.stringify({ready:false});
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'checkout-submit');
  return JSON.stringify({ready:true});
})()
""".replace("__DELIVERY_BINDING__", CHECKOUT_DELIVERY_BINDING_JS).replace("__CODE__", json.dumps(review.get("target_order_code"))).replace("__TOTAL__", str(int(round(float(review["summary"]["total"]) * 100)))).replace("__DELIVERY__", json.dumps(review["summary"]["delivery"]["display"], ensure_ascii=False)).replace("__URL__", json.dumps(CHECKOUT_URL)))
                if ready != {"ready": True}:
                    raise HouseholdError("MENY Vipps payment control changed")
                def mark_dispatched() -> None:
                    nonlocal final_dispatched
                    if before_click:
                        before_click()
                    final_dispatched = True

                self._click_checkout_submit(review, mark_dispatched)
                return {"awaiting_user_payment": True, "payment": "vipps"}
        except HouseholdError as exc:
            if not final_dispatched:
                raise CheckoutPreconditionError(
                    f"{exc}; no payment was dispatched; one fresh prepare is safe"
                ) from exc
            raise

    def review_cancellation(self, order_id: str, order: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        with self._locked_operation(MENY_ORDER_TIMEOUT, deadline):
            current = self._get_order(self._order_id(order_id))
            if str(current.get("orderNumber")) != order_id or current.get("status") == "cancelled":
                return {"available": False, "reason": "MENY order is not cancellable"}
            if current != dict(order):
                raise HouseholdError("MENY order changed before cancellation review")
            result = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const expected = ORDER, path = `/profil/nettbutikk/bestilling/${expected}`;
  const main = [...document.querySelectorAll('main')].filter(visible);
  const lines = main.length === 1 ? (main[0].innerText || '').split(/\n+/).map(norm).filter(Boolean) : [];
  const index = lines.findIndex(x => x === 'Ordrenummer');
  if (location.pathname !== path || main.length !== 1 || index < 0 || lines[index+1] !== expected) return JSON.stringify({available:false});
  const buttons = [...main[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => norm(x.getAttribute('aria-label') || x.innerText) === 'Kanseller bestilling');
  if (buttons.length !== 1) return JSON.stringify({available:false});
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'cancel-open');
  return JSON.stringify({available:true});
})()
""".replace("ORDER", json.dumps(order_id)))
            if result.get("available") is not True:
                return {"available": False, "reason": "MENY order is not cancellable"}
            self._invoke("click", '[data-hermes-meal-planner-action="cancel-open"]')
            self._sleep(0.25)
            dialog = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible).filter(x => /Sikker på at du vil kansellere bestilling/i.test(norm(x.innerText)));
  if (dialogs.length !== 1) return JSON.stringify({ready:false});
  const root = dialogs[0], final = [...root.querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Kanseller');
  const dismiss = [...root.querySelectorAll('button')].filter(visible).filter(x => norm(x.innerText) === 'Avbryt');
  if (final.length !== 1 || dismiss.length !== 1) return JSON.stringify({ready:false});
  dismiss[0].setAttribute('data-hermes-meal-planner-action', 'cancel-dismiss');
  return JSON.stringify({ready:true, consequence:null});
})()
""")
            if dialog.get("ready") is not True:
                raise HouseholdError("MENY cancellation dialog changed")
            self._invoke("click", '[data-hermes-meal-planner-action="cancel-dismiss"]')
            for _ in range(20):
                settled = self._eval(r"""
(() => {
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible).filter(x => /Sikker på at du vil kansellere bestilling/i.test(norm(x.innerText)));
  return JSON.stringify({clear:dialogs.length === 0});
})()
""")
                if settled == {"clear": True}:
                    break
                self._sleep(0.1)
            else:
                raise HouseholdError("MENY cancellation review did not close safely")
            digest = hashlib.sha256(json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return {"available": True, "consequence": dialog.get("consequence"), "order_digest": digest}

    def submit_cancellation(self, order_id: str, order: Mapping[str, Any], review: Mapping[str, Any], before_click: Any = None, *, deadline: float | None = None) -> None:
        final_dispatched = False
        try:
            if self.review_cancellation(order_id, order, deadline=deadline) != dict(review):
                raise HouseholdError("MENY cancellation changed after review")
        except HouseholdError as exc:
            raise CancellationPreconditionError(str(exc)) from exc
        try:
            with self._locked_operation(MENY_ORDER_TIMEOUT, deadline):
                current = self._get_order(self._order_id(order_id))
                if current != dict(order):
                    raise HouseholdError("MENY order changed after cancellation confirmation")
                opened = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const expected = ORDER, path = `/profil/nettbutikk/bestilling/${expected}`;
  const main = [...document.querySelectorAll('main')].filter(visible);
  const lines = main.length === 1 ? (main[0].innerText || '').split(/\n+/).map(norm).filter(Boolean) : [];
  const index = lines.findIndex(x => x === 'Ordrenummer');
  if (location.pathname !== path || main.length !== 1 || index < 0 || lines[index+1] !== expected) return JSON.stringify({ready:false});
  const buttons = [...main[0].querySelectorAll('button')].filter(visible).filter(x => !x.disabled && x.getAttribute('aria-disabled') !== 'true').filter(x => norm(x.getAttribute('aria-label') || x.innerText) === 'Kanseller bestilling');
  if (buttons.length !== 1) return JSON.stringify({ready:false});
  buttons[0].setAttribute('data-hermes-meal-planner-action', 'cancel-submit-open');
  return JSON.stringify({ready:true});
})()
""".replace("ORDER", json.dumps(order_id)))
                if opened != {"ready": True}:
                    raise HouseholdError("MENY cancellation control changed")
                self._invoke("click", '[data-hermes-meal-planner-action="cancel-submit-open"]')
                self._sleep(0.25)
                final = self._eval(r"""
(() => {
  document.querySelectorAll('[data-hermes-meal-planner-action]').forEach(x => x.removeAttribute('data-hermes-meal-planner-action'));
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const enabled = x => visible(x) && !x.disabled && x.getAttribute('aria-disabled') !== 'true';
  const expected = ORDER, path = `/profil/nettbutikk/bestilling/${expected}`;
  const main = [...document.querySelectorAll('main')].filter(visible);
  const lines = main.length === 1 ? (main[0].innerText || '').split(/\n+/).map(norm).filter(Boolean) : [];
  const index = lines.findIndex(x => x === 'Ordrenummer');
  if (location.pathname !== path || main.length !== 1 || index < 0 || lines[index+1] !== expected) return JSON.stringify({ready:false});
  const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(visible).filter(x => /Sikker på at du vil kansellere bestilling/i.test(norm(x.innerText)));
  if (dialogs.length !== 1) return JSON.stringify({ready:false});
  const confirm = [...dialogs[0].querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Kanseller');
  const dismiss = [...dialogs[0].querySelectorAll('button')].filter(enabled).filter(x => norm(x.innerText) === 'Avbryt');
  if (confirm.length !== 1 || dismiss.length !== 1) return JSON.stringify({ready:false});
  confirm[0].setAttribute('data-hermes-meal-planner-action', 'cancel-submit-final');
  return JSON.stringify({ready:true});
})()
""".replace("ORDER", json.dumps(order_id)))
                if final != {"ready": True}:
                    raise HouseholdError("MENY cancellation confirmation changed")
                self._require_time(5)
                if before_click:
                    before_click()
                final_dispatched = True
                self._invoke("click", '[data-hermes-meal-planner-action="cancel-submit-final"]')
        except HouseholdError as exc:
            if not final_dispatched:
                raise CancellationPreconditionError(str(exc)) from exc
            raise

    def _open(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "meny.no":
            raise HouseholdError("MENY browser URL is invalid")
        self._shell_target = url
        if self.cdp is not None:
            if not self._viewport_primed:
                self._invoke("set", "viewport", *(str(value) for value in MENY_VIEWPORT))
                self._viewport_primed = True
            try:
                if self._site_shell_ready():
                    self._cdp_primed = True
                    return
            except HouseholdError:
                pass
        data = self._invoke("open", url)
        actual = urlparse(str(data.get("url") or ""))
        if actual.scheme != "https" or actual.netloc != "meny.no" or actual.path.rstrip("/") != parsed.path.rstrip("/"):
            raise HouseholdError("MENY browser left the requested page")
        self._sleep(0.5)
        def wait_for_shell() -> bool:
            # First let the requested route finish its own navigation. A
            # premature reload can otherwise cancel a still-loading route.
            # If MENY's Next.js shell remains unhydrated, try at most three
            # bounded reloads; these change no cookies, cart or account data.
            for attempt in range(4):
                if attempt:
                    self._invoke("reload")
                for poll in range(20):
                    try:
                        if self._site_shell_ready():
                            self._cdp_primed = True
                            return True
                    except HouseholdError:
                        pass
                    if poll < 19:
                        self._sleep(0.5)
            return False

        if wait_for_shell():
            return
        # A hydrated target can later hang while Chromium and the persisted
        # authenticated profile remain healthy. Reads may replace that one
        # dedicated target once; protected mutations disable recovery.
        if self.cdp is not None and self.recovery_allowed and not self._recovery_consumed:
            self._recovery_consumed = True
            if self._recover_cdp_tab():
                self._sleep(0.5)
                previous_recovery = self.recovery_allowed
                self.recovery_allowed = False
                try:
                    data = self._invoke_once("open", url)
                    actual = urlparse(str(data.get("url") or ""))
                    if actual.scheme != "https" or actual.netloc != "meny.no" or actual.path.rstrip("/") != parsed.path.rstrip("/"):
                        raise HouseholdError("MENY browser left the requested page")
                    self._sleep(0.5)
                    if wait_for_shell():
                        return
                finally:
                    self.recovery_allowed = previous_recovery
        raise HouseholdError("MENY website did not finish rendering")

    def _site_shell_ready(self) -> bool:
        expected = None
        if self._shell_target is not None:
            parsed = urlparse(self._shell_target)
            expected = {
                "origin": f"{parsed.scheme}://{parsed.netloc}",
                "pathname": parsed.path or "/",
                "search": f"?{parsed.query}" if parsed.query else "",
                "hash": f"#{parsed.fragment}" if parsed.fragment else "",
            }
        result = self._eval(r"""
(() => {
  const visible = x => { const style=getComputedStyle(x), box=x.getBoundingClientRect(); return style.display!=='none' && style.visibility!=='hidden' && box.width>0 && box.height>0; };
  const norm = value => (value || '').normalize('NFC').replace(/\s+/g, ' ').trim();
  const expected = __EXPECTED__;
  let searchReady = expected === null || location.search === expected.search;
  if (!searchReady && expected?.pathname === '/sok') {
    const wanted = new URLSearchParams(expected.search), actual = new URLSearchParams(location.search);
    const wantedQuery = wanted.getAll('query'), actualQuery = actual.getAll('query'), expanded = actual.getAll('expanded');
    searchReady = [...wanted.keys()].every(key => key === 'query') && [...actual.keys()].every(key => key === 'query' || key === 'expanded') &&
      wantedQuery.length === 1 && actualQuery.length === 1 && actualQuery[0] === wantedQuery[0] && expanded.length === 1 && ['products','recipes'].includes(expanded[0]);
  }
  const target = expected === null || (location.origin === expected.origin && location.pathname === expected.pathname && searchReady && location.hash === expected.hash);
  const account = [...document.querySelectorAll('header button')].filter(visible).filter(button => {
    const label = norm(button.getAttribute('aria-label') || button.innerText);
    return label.startsWith('Brukermeny') || label === 'Logg inn';
  });
  const propsKey = account.length === 1 && Object.keys(account[0]).find(key => key.startsWith('__reactProps$'));
  const props = propsKey && account[0][propsKey];
  return JSON.stringify({
    dom_ready: target && location.origin === 'https://meny.no' && Boolean(document.querySelector('main')) && account.length === 1,
    hydrated: Boolean(props && typeof props.onClick === 'function')
  });
})()
""".replace("__EXPECTED__", json.dumps(expected)))
        return result.get("dom_ready") is True and result.get("hydrated") is True

    @contextmanager
    def _locked_operation(self, seconds: float, deadline: float | None = None, *, allow_recovery: bool = False):
        operation_deadline = time.monotonic() + seconds
        if deadline is not None:
            operation_deadline = min(operation_deadline, deadline)
        remaining = operation_deadline - time.monotonic()
        acquired = remaining > 0 and self.lock.acquire(timeout=remaining)
        if not acquired:
            raise HouseholdError("MENY operation deadline reached")
        previous = self.deadline
        previous_recovery = self.recovery_allowed
        previous_recovery_consumed = self._recovery_consumed
        self.deadline = operation_deadline if previous is None else min(previous, operation_deadline)
        self.recovery_allowed = previous_recovery or allow_recovery
        self._recovery_consumed = False
        try:
            yield
        finally:
            self.deadline = previous
            self.recovery_allowed = previous_recovery
            self._recovery_consumed = previous_recovery_consumed
            self.lock.release()

    def _require_time(self, minimum: float = 0) -> None:
        if self.deadline is not None and self.deadline - time.monotonic() < minimum:
            raise HouseholdError("MENY operation deadline reached")

    def _sleep(self, seconds: float) -> None:
        self._require_time(seconds)
        time.sleep(seconds)

    def _eval(self, script: str) -> dict[str, Any]:
        data = self._invoke("eval", "--stdin", stdin=script)
        raw = data.get("result")
        if not isinstance(raw, str):
            raise HouseholdError("MENY browser returned no result")
        try:
            value = json.loads(raw)
            if isinstance(value, str):
                value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise HouseholdError("MENY browser returned malformed data") from exc
        if not isinstance(value, dict):
            raise HouseholdError("MENY browser result is invalid")
        return value

    def _invoke(self, *arguments: str, stdin: str | None = None) -> dict[str, Any]:
        try:
            return self._invoke_once(*arguments, stdin=stdin)
        except _BrowserTransportError:
            safe = bool(arguments) and arguments[0] in {"open", "reload", "eval"}
            if self.cdp is None or not self.recovery_allowed or self._recovery_consumed or not safe:
                raise
            self._recovery_consumed = True
            if not self._recover_cdp_tab():
                raise
            self._sleep(0.5)
        return self._invoke_once(*arguments, stdin=stdin)

    def _recover_cdp_tab(self) -> bool:
        if self.cdp is None:
            return False
        started = time.monotonic()
        if self.deadline is not None and self.deadline - started < 18:
            return False
        recovery_deadline = started + 12
        if self.deadline is not None:
            recovery_deadline = min(recovery_deadline, self.deadline - 5)
        try:
            targets = self._cdp_page_targets(min(recovery_deadline - 10, started + 2))
            if len(targets) != 1:
                return False
            target_id, recovery_url = next(iter(targets.items()))
            parsed_url = urlparse(recovery_url)
            if parsed_url.scheme != "https" or parsed_url.netloc != "meny.no":
                return False
            if not self._terminate_browser_session(min(recovery_deadline - 7, time.monotonic() + 2.5)):
                return False
            created_hint = ""
            try:
                create_deadline = min(recovery_deadline - 4, time.monotonic() + 2.5)
                created = json.loads(self._cdp_request("PUT", f"/json/new?{quote(recovery_url, safe='')}", create_deadline))
                if isinstance(created, Mapping):
                    created_hint = str(created.get("id") or "")
            except (HouseholdError, OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
                pass
            observed = self._cdp_page_targets(min(recovery_deadline - 2, time.monotonic() + 2))
            new_ids = set(observed) - {target_id}
            if len(new_ids) != 1:
                return False
            created_id = next(iter(new_ids))
            if observed[created_id] != recovery_url or (created_hint and created_hint != created_id):
                return False
            if set(observed) == {created_id}:
                self._cdp_primed = False
                self._viewport_primed = False
                return True
            if set(observed) != {target_id, created_id}:
                return False
            try:
                self._cdp_request("PUT", f"/json/close/{target_id}", min(recovery_deadline - 1, time.monotonic() + 1))
            except (HouseholdError, OSError, ValueError, http.client.HTTPException):
                pass
            while time.monotonic() < recovery_deadline:
                observed = self._cdp_page_targets(min(recovery_deadline, time.monotonic() + 0.5))
                if set(observed) == {created_id} and observed[created_id] == recovery_url:
                    self._cdp_primed = False
                    self._viewport_primed = False
                    return True
                if not set(observed).issubset({target_id, created_id}) or created_id not in observed:
                    return False
                remaining = recovery_deadline - time.monotonic()
                if remaining <= 0.1:
                    break
                if set(observed) == {target_id, created_id}:
                    try:
                        self._cdp_request(
                            "PUT",
                            f"/json/close/{target_id}",
                            min(recovery_deadline, time.monotonic() + 0.5),
                        )
                    except (HouseholdError, OSError, ValueError, http.client.HTTPException):
                        pass
                time.sleep(min(0.1, remaining))
            return False
        except (HouseholdError, OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
            return False

    def _cdp_page_targets(self, deadline: float) -> dict[str, str]:
        pages = json.loads(self._cdp_request("GET", "/json/list", deadline))
        if not isinstance(pages, list):
            raise HouseholdError("MENY browser target list is invalid")
        targets: dict[str, str] = {}
        for page in pages:
            if not isinstance(page, Mapping) or page.get("type") != "page":
                continue
            target_id = str(page.get("id") or "")
            target_url = str(page.get("url") or "")
            if not re.fullmatch(r"[A-Fa-f0-9]{16,64}", target_id) or not target_url or target_id in targets:
                raise HouseholdError("MENY browser target list is invalid")
            targets[target_id] = target_url
        return targets

    def _cdp_request(self, method: str, path: str, deadline: float) -> str:
        if self.cdp is None or method not in {"GET", "PUT"} or not path.startswith("/json/"):
            raise HouseholdError("MENY browser recovery request is invalid")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HouseholdError("MENY browser recovery deadline reached")
        parsed = urlparse(self.cdp)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=min(3, remaining))
        try:
            connection.request(method, path)
            response = connection.getresponse()
            payload = response.read(1024 * 1024 + 1)
        finally:
            connection.close()
        if response.status != 200 or len(payload) > 1024 * 1024:
            raise HouseholdError("MENY browser recovery failed")
        return payload.decode("utf-8")

    def _terminate_browser_session(self, deadline: float) -> bool:
        pid_file = self.socket_directory / f"{self.session}.pid"

        def drop_privileges() -> None:
            os.setgroups([])
            os.setgid(self.gid)
            os.setuid(self.uid)

        privilege_drop = drop_privileges if os.geteuid() == 0 else None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        expected = self._browser_daemon_executable()
        if expected is None:
            return False
        wait_ms = max(1, min(1500, int(max(0, remaining - 0.2) * 1000)))
        script = """import os, select, signal, stat, sys
path, expected, uid_text, wait_text = sys.argv[1:]
uid = int(uid_text)
fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
info = os.fstat(fd)
if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
    raise SystemExit(2)
raw = os.read(fd, 32).decode('ascii').strip()
if not raw.isdigit() or not 1 <= int(raw) <= 99_999_999:
    raise SystemExit(2)
pid = int(raw)
pidfd = os.pidfd_open(pid)
if os.path.realpath(os.readlink(f'/proc/{pid}/exe')) != os.path.realpath(expected):
    raise SystemExit(2)
signal.pidfd_send_signal(pidfd, signal.SIGTERM)
poller = select.poll()
poller.register(pidfd, select.POLLIN)
raise SystemExit(0 if poller.poll(int(wait_text)) else 3)
"""
        try:
            stopped = subprocess.run(
                [sys.executable, "-c", script, str(pid_file), str(expected), str(self.uid), str(wait_ms)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(2, remaining),
                check=False,
                preexec_fn=privilege_drop,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return stopped.returncode == 0 and time.monotonic() < deadline

    def _browser_daemon_executable(self) -> Path | None:
        resolved = Path(shutil.which(str(self.binary)) or self.binary).resolve()
        if resolved.name != "agent-browser.js":
            return resolved if resolved.is_file() else None
        machine = os.uname().machine.casefold()
        architecture = "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine in {"x64", "x86_64", "amd64"} else ""
        if not architecture:
            return None
        candidates = [path.resolve() for path in resolved.parent.glob(f"agent-browser-linux*-{architecture}") if path.is_file() and os.access(path, os.X_OK)]
        return candidates[0] if len(candidates) == 1 else None

    def _invoke_once(self, *arguments: str, stdin: str | None = None) -> dict[str, Any]:
        self._require_time()
        timeout = 70.0
        if self.deadline is not None:
            timeout = min(timeout, max(0.1, self.deadline - time.monotonic()))
        command = [str(self.binary), "--json", "--session", self.session]
        if self.cdp is None:
            command.extend(["--profile", str(self.profile), "--executable-path", str(self.executable)])
        else:
            command.extend(["--cdp", self.cdp])
        command.extend(arguments)
        environment = {
            "AGENT_BROWSER_ARGS": DEFAULT_BROWSER_ARGS,
            "AGENT_BROWSER_DEFAULT_TIMEOUT": "60000",
            "AGENT_BROWSER_SOCKET_DIR": str(self.socket_directory),
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "PATH": os.environ.get("PATH", os.defpath),
        }
        for name in ("AGENT_BROWSER_PROXY", "AGENT_BROWSER_PROXY_BYPASS"):
            if value := os.environ.get(name):
                environment[name] = value

        def drop_privileges() -> None:
            os.setgroups([])
            os.setgid(self.gid)
            os.setuid(self.uid)

        try:
            completed = subprocess.run(
                command,
                input=stdin,
                stdin=subprocess.DEVNULL if stdin is None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=timeout,
                check=False,
                preexec_fn=drop_privileges if os.geteuid() == 0 else None,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise _BrowserTransportError("MENY browser is unavailable") from exc
        except OSError as exc:
            raise HouseholdError("MENY browser is unavailable") from exc
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            if completed.returncode != 0:
                raise HouseholdError("MENY browser operation failed") from exc
            raise HouseholdError("MENY browser response is malformed") from exc
        error = str(envelope.get("error") or "").casefold() if isinstance(envelope, Mapping) else ""
        transport = any(marker in error for marker in (
            "tab is not responding",
            "browser is not connected",
            "failed to connect to browser",
            "connection refused",
        )) or error.startswith("cdp command timed out:")
        if completed.returncode != 0:
            if transport:
                raise _BrowserTransportError("MENY browser is unavailable")
            raise HouseholdError("MENY browser operation failed")
        if envelope.get("success") is not True or not isinstance(envelope.get("data"), dict):
            if transport:
                raise _BrowserTransportError("MENY browser is unavailable")
            raise HouseholdError("MENY browser rejected the operation")
        self._require_time()
        return envelope["data"]
