"""Conservative, fixture-backed grocery product observations."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import math
import re
from typing import Any, Hashable, Mapping
import unicodedata

from core import HouseholdError


MAX_PRODUCTS = 20
MAX_TEXT_BYTES = 1_000
MAX_MONEY_ORE = 100_000_000

_MENY_SIMPLE_MONEY = re.compile(r"^(?P<whole>0|[1-9]\d{0,6}),(?P<cents>\d{2})\s*kr$", re.IGNORECASE)
_MENY_FROM_MONEY = re.compile(r"^fra\s+(?P<whole>0|[1-9]\d{0,6}),(?P<cents>\d{2})\s*kr$", re.IGNORECASE)
_MENY_ORIGINAL_MONEY = re.compile(r"^f\u00f8r\s+(?P<whole>0|[1-9]\d{0,6}),(?P<cents>\d{2})\s*kr$", re.IGNORECASE)
_MENY_DETAIL_MONEY = re.compile(
    r"^(?:tilbud,\s*n\u00e5\s+)?(?P<whole>0|[1-9]\d{0,6}),(?P<cents>\d{2})\s+kroner(?P<deposit>\s+pluss\s+pant)?\.$",
    re.IGNORECASE,
)
_MENY_DETAIL_ORIGINAL_MONEY = re.compile(
    r"^f\u00f8r\s+(?P<whole>0|[1-9]\d{0,6}),(?P<cents>\d{2})\s+kroner\.$",
    re.IGNORECASE,
)
_ODA_MONEY = re.compile(r"^(?P<whole>0|[1-9]\d{0,6})\.(?P<cents>\d{2})$")
_NUMBER = r"(?:0|[1-9]\d{0,5})(?:[.,]\d{1,6})?"
_PACKAGE_BEFORE_COUNT = re.compile(
    rf"(?<![\w.,])(?P<count>[1-9]\d{{0,2}})\s*[x×]\s*(?P<amount>{_NUMBER})\s*(?P<unit>kg|g|l|ml|stk)(?!\w)",
    re.IGNORECASE,
)
_PACKAGE_AFTER_COUNT = re.compile(
    rf"(?<![\w.,])(?P<amount>{_NUMBER})(?P<unit>kg|g|l|ml|stk)[x\u00d7](?P<count>[1-9]\d{{0,2}})(?!\w)",
    re.IGNORECASE,
)
_PACKAGE_SINGLE = re.compile(
    rf"(?<![\w.,])(?P<amount>{_NUMBER})\s*(?P<unit>kg|g|l|ml|stk)(?!\w)",
    re.IGNORECASE,
)
_VARIABLE = re.compile(
    r"(?<!\w)(?:ca\.?|cirka)(?=\s*\d)|\bpr\.?\s*(?:kg|hg|g|l|dl|ml)\b",
    re.IGNORECASE,
)
_ODA_PERCENT_PREFIX = re.compile(r"^(?:0|[1-9]\d{0,2})%$")
_ODA_RANGED_COUNT_PREFIX = re.compile(
    r"^[1-9]\d{0,2}\s*[-–]\s*[1-9]\d{0,2}\s*stk\.\s*"
    r"Fairtrade,\s*Ecuador\s*/\s*Peru$",
    re.IGNORECASE,
)
_ODA_DISCOUNT_PREFIX = re.compile(
    r"^maks\s+[1-9]\d{0,2}\s+til\s+nedsatt\s+pris$", re.IGNORECASE
)
_MULTIBUY = re.compile(r"^(?P<take>[2-9])\s+for\s+(?P<pay>[1-8])$", re.IGNORECASE)
_MENY_MULTI_CAMPAIGN = re.compile(
    r"^del av kampanje: plukk & miks (?P<count>[1-9]\d{0,2})pk brus fra "
    r"[A-Za-z0-9À-ÖØ-öø-ÿ][A-Za-z0-9À-ÖØ-öø-ÿ.\-]{0,49}$",
    re.IGNORECASE,
)
_MEMBER = re.compile(r"\b(?:meny\s+mer|medlem|kupong|coupon)\b", re.IGNORECASE)


def _bounded_text(value: Any, *, required: bool = False, maximum: int = MAX_TEXT_BYTES) -> str | None:
    if value is None:
        if required:
            raise HouseholdError("product observation text is missing")
        return None
    if not isinstance(value, str):
        raise HouseholdError("product observation text is invalid")
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if (required and not text) or len(text.encode("utf-8")) > maximum:
        raise HouseholdError("product observation text is invalid")
    return text or None


def _display_text(value: Any, *, maximum: int = MAX_TEXT_BYTES) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(unicodedata.normalize("NFC", value).split())
    return text if text and len(text.encode("utf-8")) <= maximum else None


def _observed_at(value: Any | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if not isinstance(value, str) or len(value) > 64:
        raise HouseholdError("product observation timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HouseholdError("product observation timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HouseholdError("product observation timestamp is invalid")
    return value


def _meny_ore(value: Any) -> int | None:
    text = _display_text(value, maximum=64)
    if text is None:
        return None
    match = _MENY_SIMPLE_MONEY.fullmatch(text)
    if match is None:
        return None
    amount = int(match["whole"]) * 100 + int(match["cents"])
    return amount if amount <= MAX_MONEY_ORE else None


def _meny_pattern_ore(value: Any, pattern: re.Pattern[str]) -> int | None:
    text = _display_text(value, maximum=100)
    if text is None:
        return None
    match = pattern.fullmatch(text)
    if match is None:
        return None
    amount = int(match["whole"]) * 100 + int(match["cents"])
    return amount if amount <= MAX_MONEY_ORE else None


def _meny_detail_price(value: Any) -> tuple[int | None, bool, bool]:
    text = _display_text(value, maximum=100)
    if text is None:
        return None, False, False
    match = _MENY_DETAIL_MONEY.fullmatch(text)
    if match is None:
        return None, False, False
    amount = int(match["whole"]) * 100 + int(match["cents"])
    return (
        amount if amount <= MAX_MONEY_ORE else None,
        match["deposit"] is not None,
        text.casefold().startswith("tilbud, nå "),
    )


def _mathem_ore(value: Any) -> int | None:
    """Accept exact Swedish kronor displays; estimates remain unavailable."""
    if not isinstance(value, str) or len(value) > 100:
        return None
    match = re.fullmatch(r"(0|[1-9]\d{0,6})(?:[,.](\d{2}))?[ \u00a0](?:kr|SEK)", value)
    if match is None:
        match = re.fullmatch(r"(?:kr|SEK)[ \u00a0](0|[1-9]\d{0,6})(?:[,.](\d{2}))?", value)
    if match is None:
        return _oda_ore(value)
    amount = int(match[1]) * 100 + int(match[2] or "0")
    return amount if amount <= MAX_MONEY_ORE else None


def _oda_ore(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    match = _ODA_MONEY.fullmatch(value)
    if match is None:
        return None
    amount = int(match["whole"]) * 100 + int(match["cents"])
    return amount if amount <= MAX_MONEY_ORE else None


def _fraction(value: str) -> Fraction | None:
    try:
        decimal = Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None
    if not decimal.is_finite() or decimal <= 0:
        return None
    return Fraction(decimal)


def _canonical_quantity(amount: str, unit: str, count: str = "1") -> tuple[Fraction, str, int] | None:
    quantity = _fraction(amount)
    if quantity is None:
        return None
    package_count = int(count)
    lowered = unit.casefold()
    if lowered == "kg":
        quantity *= 1_000
        canonical_unit = "g"
    elif lowered == "g":
        canonical_unit = "g"
    elif lowered == "l":
        quantity *= 1_000
        canonical_unit = "ml"
    elif lowered == "ml":
        canonical_unit = "ml"
    elif lowered == "stk":
        canonical_unit = "count"
    else:
        return None
    quantity *= package_count
    if quantity <= 0 or quantity.numerator > 10**12 or quantity.denominator > 10**9:
        return None
    return quantity, canonical_unit, package_count


def _strict_package(value: str) -> tuple[Fraction, str, int] | None:
    for pattern in (_PACKAGE_BEFORE_COUNT, _PACKAGE_AFTER_COUNT):
        match = pattern.fullmatch(value)
        if match is not None:
            return _canonical_quantity(match["amount"], match["unit"], match["count"])
    single = _PACKAGE_SINGLE.fullmatch(value)
    if single is not None:
        return _canonical_quantity(single["amount"], single["unit"])
    return None


def parse_package(value: Any, *, provider: str | None = None) -> dict[str, Any] | None:
    """Parse only complete package strings whose shapes are fixture-established."""

    text = _display_text(value, maximum=300)
    if text is None or _VARIABLE.search(text):
        return None
    if provider == "mathem":
        text = re.sub(r"\bst\b", "stk", text, flags=re.IGNORECASE)
    parsed = _strict_package(text)
    if provider == "meny" and parsed is None:
        candidate = text
        if candidate.startswith("Økologisk "):
            candidate = candidate[len("Økologisk "):]
        for suffix in (" Q", " Vilje", " Ode", " flaske", " boks"):
            if candidate.endswith(suffix):
                candidate = candidate[:-len(suffix)]
                break
        parsed = _strict_package(candidate)
    elif provider in {"oda", "mathem"} and parsed is None:
        segments = [segment.strip() for segment in text.split(", ")]
        if len(segments) == 2 and _ODA_PERCENT_PREFIX.fullmatch(segments[0]):
            parsed = _strict_package(segments[1])
        elif len(segments) == 2 and segments[0] == "Porsjonspose":
            parsed = _strict_package(segments[1])
        elif len(segments) >= 2 and _ODA_RANGED_COUNT_PREFIX.fullmatch(",".join(segments[:-1])):
            parsed = _strict_package(segments[-1])
        elif (
            len(segments) == 3
            and _ODA_DISCOUNT_PREFIX.fullmatch(segments[0])
            and segments[1] == "Naturell"
        ):
            parsed = _strict_package(segments[2])
        elif len(segments) == 2:
            multipack = _strict_package(segments[0])
            total = _strict_package(segments[1])
            if (
                multipack is not None and total is not None
                and multipack[:2] == total[:2] and multipack[2] > 1
            ):
                parsed = multipack
    if parsed is None:
        return None
    quantity, unit, item_count = parsed
    return {
        "quantity": {"numerator": quantity.numerator, "denominator": quantity.denominator},
        "unit": unit,
        "item_count": item_count,
    }


def _unit_price(merchandise_ore: int, package: Mapping[str, Any], packages: int) -> dict[str, Any]:
    quantity = package["quantity"]
    amount = Fraction(quantity["numerator"], quantity["denominator"]) * packages
    price = Fraction(merchandise_ore, 1) / amount
    with localcontext() as context:
        context.prec = 50
        rounded = (Decimal(price.numerator) / Decimal(price.denominator)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
    return {
        "numerator": price.numerator,
        "denominator": price.denominator,
        "unit": package["unit"],
        "display_ore_per_unit": format(rounded, "f"),
    }


def _purchase_option(
    *,
    package_count: int,
    price_kind: str,
    merchandise_ore: int | None,
    from_ore: int | None,
    deposit_ore: int | None,
    deposit_known: bool,
    offer_kind: str,
    eligibility: str,
    package: Mapping[str, Any] | None,
) -> dict[str, Any]:
    option: dict[str, Any] = {
        "package_count": package_count,
        "price_kind": price_kind,
        "offer_kind": offer_kind,
        "eligibility": eligibility,
    }
    if price_kind == "exact" and merchandise_ore is not None:
        option["merchandise_ore"] = merchandise_ore
    elif price_kind == "from" and from_ore is not None:
        option["from_ore"] = from_ore
    if deposit_known and deposit_ore is not None:
        option["mandatory_deposit_ore"] = deposit_ore
    if (
        price_kind == "exact" and merchandise_ore is not None
        and eligibility == "confirmed" and package is not None
    ):
        option["comparable_merchandise_unit_price"] = _unit_price(
            merchandise_ore, package, package_count
        )
    if (
        price_kind == "exact" and merchandise_ore is not None
        and eligibility == "confirmed" and deposit_known and deposit_ore is not None
    ):
        option["total_payable_ore"] = merchandise_ore + deposit_ore
    return option


def _normalize_meny_product(raw: Any, observed_at: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise HouseholdError("MENY product result is invalid")
    product_ref = _bounded_text(raw.get("product_id"), required=True, maximum=500)
    if not product_ref or not product_ref.startswith("/varer/"):
        raise HouseholdError("MENY product result id is invalid")
    name = _bounded_text(raw.get("name"), required=True, maximum=300)
    package_text = _display_text(raw.get("package"), maximum=300)
    price_text = _display_text(raw.get("price"), maximum=100)
    campaign_tag = _display_text(raw.get("campaign_tag"), maximum=100)
    campaign = _display_text(raw.get("campaign") or raw.get("offer"), maximum=500)
    deposit_text = _display_text(raw.get("deposit"), maximum=100)
    original_price_text = _display_text(
        raw.get("original_price") or raw.get("price_compare"), maximum=100
    )
    detail_price_text = _display_text(raw.get("detail_price"), maximum=100)
    detail_original_text = _display_text(raw.get("detail_original_price"), maximum=100)
    detail_deposit_text = _display_text(raw.get("detail_deposit"), maximum=100)
    available = raw.get("available")
    availability = "available" if available is True else "unavailable" if available is False else "unknown"
    package = parse_package(package_text, provider="meny")
    exact_ore = _meny_ore(price_text)
    from_ore = _meny_pattern_ore(price_text, _MENY_FROM_MONEY)
    variable_price = bool(package_text and _VARIABLE.search(package_text)) and not (
        package is not None and package["unit"] == "count"
    )
    if exact_ore is not None and not variable_price:
        price_kind = "exact"
    elif from_ore is not None:
        price_kind = "from"
    else:
        price_kind = "unavailable"
    detail_ore, detail_has_deposit, detail_is_discount = _meny_detail_price(
        detail_price_text
    )
    deposit_status = raw.get("deposit_status")
    detail_matches = exact_ore is not None and detail_ore == exact_ore
    deposit_known = (
        deposit_status == "none" and detail_matches
        and not detail_has_deposit and deposit_text is None and detail_deposit_text is None
    )
    deposit_ore = 0 if deposit_known else None
    deposit_present_unknown = (
        deposit_status == "present_unknown" and detail_matches
        and detail_has_deposit and detail_deposit_text == "+ pant"
        and deposit_text in {None, "+ pant"}
    )
    if deposit_status not in {None, "none", "present_unknown"}:
        raise HouseholdError("MENY product deposit evidence is invalid")
    if deposit_status is not None and not (deposit_known or deposit_present_unknown):
        raise HouseholdError("MENY product deposit evidence is contradictory")
    member = _MEMBER.search(" ".join(part for part in (campaign_tag, campaign) if part)) is not None
    multi = _MULTIBUY.fullmatch(campaign_tag or "")
    multi_campaign = _MENY_MULTI_CAMPAIGN.fullmatch(campaign or "")
    confirmed_multi = (
        multi is not None
        and multi_campaign is not None
        and package is not None
        and package["item_count"] == int(multi_campaign["count"])
        and original_price_text is None
        and detail_original_text is None
        and detail_matches
        and not detail_is_discount
        and (deposit_known or deposit_present_unknown)
    )
    original_ore = _meny_pattern_ore(original_price_text, _MENY_ORIGINAL_MONEY)
    detail_original_ore = _meny_pattern_ore(
        detail_original_text, _MENY_DETAIL_ORIGINAL_MONEY
    )
    confirmed_discount = (
        exact_ore is not None and original_ore is not None and original_ore > exact_ore
        and detail_matches and detail_original_ore == original_ore
        and detail_is_discount
        and (campaign_tag or "").casefold() == "tilbud"
        and (campaign or "").casefold() == "del av kampanje: tilbud"
    )
    options = []
    if confirmed_multi and int(multi["pay"]) < int(multi["take"]) and exact_ore is not None:
        options.append(_purchase_option(
            package_count=1, price_kind=price_kind, merchandise_ore=exact_ore,
            from_ore=from_ore, deposit_ore=deposit_ore, deposit_known=deposit_known,
            offer_kind="regular", eligibility="confirmed", package=package,
        ))
        take = int(multi["take"])
        options.append(_purchase_option(
            package_count=take, price_kind=price_kind,
            merchandise_ore=exact_ore * int(multi["pay"]), from_ore=None,
            deposit_ore=(deposit_ore * take if deposit_ore is not None else None),
            deposit_known=deposit_known, offer_kind="multi_buy",
            eligibility="unknown" if member else "confirmed", package=package,
        ))
    else:
        has_unconfirmed_promotion = bool(
            raw.get("campaign_tag") or campaign or original_price_text or detail_is_discount
        )
        offer_kind = "discount" if confirmed_discount else "member" if member else "regular"
        eligibility = "confirmed" if confirmed_discount or not has_unconfirmed_promotion else "unknown"
        options.append(_purchase_option(
            package_count=1, price_kind=price_kind, merchandise_ore=exact_ore,
            from_ore=from_ore, deposit_ore=deposit_ore, deposit_known=deposit_known,
            offer_kind=offer_kind, eligibility=eligibility,
            package=package,
        ))
    display = {
        key: value for key, value in {
            "package": package_text,
            "price": price_text,
            "unit_price": _display_text(raw.get("unit_price"), maximum=100),
            "original_price": original_price_text,
            "campaign_tag": campaign_tag,
            "campaign": campaign,
            "deposit": deposit_text,
            "detail_deposit": detail_deposit_text,
        }.items() if value is not None
    }
    result: dict[str, Any] = {
        "provider": "meny", "product_ref": product_ref, "product_id": product_ref,
        "name": name, "availability": availability, "observed_at": observed_at,
        "purchase_options": options, "display": display,
    }
    if package is not None:
        result["package"] = package
    return result


def _oda_items(value: Mapping[str, Any]) -> tuple[str, list[Any], bool]:
    batches = value.get("result")
    if not isinstance(batches, list) or len(batches) != 1 or not isinstance(batches[0], Mapping):
        raise HouseholdError("Oda product search result changed")
    batch = batches[0]
    query = _bounded_text(batch.get("query"), required=True, maximum=200)
    products = batch.get("products")
    has_more = batch.get("hasMore")
    if not isinstance(products, list) or not isinstance(has_more, bool):
        raise HouseholdError("Oda product search result changed")
    return query, products, has_more


def _normalize_oda_product(raw: Any, observed_at: str, *, provider: str = "oda") -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise HouseholdError("Oda product result is invalid")
    product_id = raw.get("id")
    if isinstance(product_id, bool) or not isinstance(product_id, int):
        raise HouseholdError("Oda product result id is invalid")
    product_ref_text = str(product_id)
    if not re.fullmatch(r"[1-9]\d{0,19}", product_ref_text):
        raise HouseholdError("Oda product result id is invalid")
    name = _bounded_text(raw.get("name"), required=True, maximum=300)
    package_text = _display_text(raw.get("description"), maximum=300)
    package = parse_package(package_text, provider=provider)
    availability_value = raw.get("availability")
    if isinstance(availability_value, Mapping):
        is_available = availability_value.get("isAvailable")
    else:
        is_available = availability_value
    availability = "available" if is_available is True else "unavailable" if is_available is False else "unknown"
    price_value = raw.get("price")
    exact_ore = _mathem_ore(price_value) if provider == "mathem" else _oda_ore(price_value)
    variable_price = bool(package_text and _VARIABLE.search(package_text)) and not (
        package is not None and package["unit"] == "count"
    )
    if exact_ore is not None and not variable_price:
        price_kind = "exact"
    else:
        price_kind = "unavailable"
    option = _purchase_option(
        package_count=1, price_kind=price_kind, merchandise_ore=exact_ore,
        from_ore=None, deposit_ore=None, deposit_known=False,
        offer_kind="regular", eligibility="confirmed", package=package,
    )
    display = {
        key: value for key, value in {
            "package": package_text,
            "price": _display_text(price_value, maximum=100),
            "unit_price": _display_text(raw.get("unitPrice"), maximum=100),
            "unit_name": _display_text(raw.get("unitName"), maximum=100),
            "brand": _display_text(raw.get("brand"), maximum=200),
            "availability": _display_text(
                availability_value.get("description")
                if isinstance(availability_value, Mapping) else None,
                maximum=300,
            ),
        }.items() if value is not None
    }
    result: dict[str, Any] = {
        "provider": provider, "product_ref": product_id, "product_id": product_id,
        "name": name, "availability": availability, "observed_at": observed_at,
        "purchase_options": [option], "display": display,
    }
    if package is not None:
        result["package"] = package
    return result


def _deduplicate(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_ref: dict[Hashable, dict[str, Any]] = {}
    ordered = []
    for product in products:
        previous = by_ref.get(product["product_ref"])
        if previous is not None and previous != product:
            raise HouseholdError("provider returned conflicting duplicate product ids")
        if previous is None:
            ordered.append(product)
        by_ref[product["product_ref"]] = product
    return ordered


def normalize_meny_product_search(value: Any, *, observed_at: str | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not isinstance(value.get("products"), list):
        raise HouseholdError("MENY product search result changed")
    timestamp = _observed_at(observed_at)
    products = value["products"]
    if len(products) > MAX_PRODUCTS:
        raise HouseholdError("MENY product search returned too many products")
    normalized = _deduplicate([_normalize_meny_product(product, timestamp) for product in products])
    return {
        "provider": "meny",
        "query": _bounded_text(value.get("query"), required=True, maximum=200),
        "observed_at": timestamp,
        "scope": {"kind": "provider_search", "page": 1, "requested_size": len(products), "returned": len(normalized), "semantics": "bounded_relevance_ranked"},
        "products": normalized,
    }


def normalize_oda_product_search(value: Any, *, observed_at: str | None = None, provider: str = "oda") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HouseholdError("Oda product search result changed")
    timestamp = _observed_at(observed_at)
    query, products, has_more = _oda_items(value)
    if len(products) > MAX_PRODUCTS:
        raise HouseholdError("Oda product search returned too many products")
    normalized = _deduplicate([_normalize_oda_product(product, timestamp, provider=provider) for product in products])
    return {
        "provider": provider, "query": query, "observed_at": timestamp,
        "scope": {"kind": "provider_search", "page": 1, "requested_size": len(products), "returned": len(normalized), "has_more": has_more, "semantics": "bounded_relevance_ranked"},
        "products": normalized,
    }


def compare_unit_prices(left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
    """Compare exact unit-price fractions without float arithmetic."""

    if left.get("unit") != right.get("unit"):
        raise HouseholdError("unit prices use incompatible dimensions")
    values = []
    for item in (left, right):
        numerator = item.get("numerator")
        denominator = item.get("denominator")
        if (
            isinstance(numerator, bool) or not isinstance(numerator, int) or numerator < 0
            or isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 1
        ):
            raise HouseholdError("unit price fraction is invalid")
        values.append((numerator, denominator))
    difference = values[0][0] * values[1][1] - values[1][0] * values[0][1]
    return -1 if difference < 0 else 1 if difference > 0 else 0
