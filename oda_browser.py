"""Operation-specific Oda browser fallback for protected order actions."""

from __future__ import annotations

from contextlib import contextmanager
import calendar
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping
import unicodedata

from core import CancellationPreconditionError, CheckoutPreconditionError, HouseholdError, cart_summary


class OdaCheckoutMismatchError(HouseholdError):
    """The live checkout surface no longer matches the supplied cart snapshot."""


STORE_URL = "https://oda.com/no/"
CART_URL = "https://oda.com/no/cart/"
CHECKOUT_ENTRY_URL = "https://oda.com/no/checkout/"
RECOMMENDATIONS_URL = "https://oda.com/no/checkout/recommendations/"
CHECKOUT_URL = "https://oda.com/no/checkout/confirm/"
CHECKOUT_BROWSER_TIMEOUT = 90
CANCELLATION_BROWSER_TIMEOUT = 105
FINAL_CLICK_MARGIN = 15
DEFAULT_BROWSER_ARGS = "--disable-quic"
CANCELLATION_BROWSER_ARGS = "--disable-quic,--disable-http2,--blink-settings=imagesEnabled=false"


def identity_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFC", value).lower()))


def product_identity(name: str, description: str, brand: str) -> str:
    name_tokens = list(identity_tokens(name))
    description_tokens = list(identity_tokens(description))
    brand_tokens = list(identity_tokens(brand))
    if brand_tokens and name_tokens[:len(brand_tokens)] == brand_tokens:
        name_tokens = name_tokens[len(brand_tokens):]
    for length in range(min(len(name_tokens) - 1, len(description_tokens)), 0, -1):
        suffix = name_tokens[-length:]
        repeated_at_start = description_tokens[:length] == suffix
        repeated_later = not any(token.isdigit() for token in suffix) and any(description_tokens[start:start + length] == suffix for start in range(1, len(description_tokens) - length + 1))
        if repeated_at_start or repeated_later:
            name_tokens = name_tokens[:-length]
            break
    return " ".join(name_tokens + description_tokens + brand_tokens)


def checkout_identity_tokens(value: str) -> tuple[str, ...]:
    tokens = identity_tokens(value)
    for length in range((len(tokens) - 1) // 2, 0, -1):
        if tokens[:length] == tokens[-length:]:
            return tokens[length:]
    return tokens


def checkout_lines_match(expected: list[Mapping[str, Any]], actual: Any) -> bool:
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    candidates = []
    for line in expected:
        wanted = checkout_identity_tokens(str(line.get("identity") or ""))
        matches = []
        for index, item in enumerate(actual):
            if not isinstance(item, Mapping) or isinstance(item.get("quantity"), bool) or not isinstance(item.get("quantity"), (int, float)):
                continue
            if not isinstance(item.get("text"), str):
                continue
            if item.get("quantity") != line.get("quantity"):
                continue
            if wanted == checkout_identity_tokens(item["text"]):
                matches.append(index)
        candidates.append(matches)

    def assign(line: int, used: set[int]) -> bool:
        return line == len(candidates) or any(index not in used and assign(line + 1, used | {index}) for index in candidates[line])

    return assign(0, set())


def delivery_signature(value: str) -> tuple[int, int, int, int, int, str] | None:
    normalized = " ".join(unicodedata.normalize("NFC", value).lower().split())
    hours = re.findall(r"\b(\d{1,2})(?::(\d{2}))?\s*(?:-|–|og|til)\s*(\d{1,2})(?::(\d{2}))?(?![:.]\d)\b", normalized)
    dates = re.findall(r"\b(\d{1,2})\.?\s*(jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\b", normalized)
    if len(hours) != 1 or len(dates) != 1:
        return None
    start, start_minute, end, end_minute = hours[0]
    day, month = dates[0]
    start_hour, start_minute, end_hour, end_minute, day = int(start), int(start_minute or 0), int(end), int(end_minute or 0), int(day)
    month = month[:3]
    month_number = {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"), 1)}[month]
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return None
    if (start_hour, start_minute) >= (end_hour, end_minute) or not 1 <= day <= calendar.monthrange(2024, month_number)[1]:
        return None
    return start_hour, start_minute, end_hour, end_minute, day, month


def checkout_delivery_matches(expected: str, roots: Any) -> bool:
    if not expected:
        return True
    return isinstance(roots, list) and len(roots) == 1 and isinstance(roots[0], str) and delivery_signature(expected) is not None and delivery_signature(expected) == delivery_signature(roots[0])


def cancellation_delivery_matches(expected: str, lines: Any) -> bool:
    signature = delivery_signature(expected)
    return signature is not None and isinstance(lines, list) and len(lines) == 1 and isinstance(lines[0], str) and delivery_signature(lines[0]) == signature


def cancellation_total_matches(expected_minor: int, rows: Any) -> bool:
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], str):
        return False
    normalized = " ".join(unicodedata.normalize("NFC", rows[0]).split())
    patterns = (
        r"\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|NOK)\b",
        r"\b(?:kr|NOK)[,\s]*(\d+(?:[ .]\d{3})*),(\d{2})\b",
    )
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, normalized, re.IGNORECASE):
            values.append(int(match.group(1).replace(" ", "").replace(".", "")) * 100 + int(match.group(2)))
    return values == [expected_minor]


def clear_cancellation_cache(profile: Path | str) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise HouseholdError("Oda browser cache cannot be reset safely")
    for relative in ("Default/Cache", "Default/Code Cache", "Default/Service Worker"):
        parent, name = relative.split("/", 1)
        profile_fd = None
        parent_fd = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            profile_fd = os.open(Path(profile), flags)
            parent_fd = os.open(parent, flags, dir_fd=profile_fd)
            shutil.rmtree(name, dir_fd=parent_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HouseholdError("Oda browser cache cannot be reset") from exc
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
            if profile_fd is not None:
                os.close(profile_fd)


class OdaBrowser:
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
    ):
        self.instance = instance
        self.binary = Path(binary)
        self.executable = Path(executable)
        self.profile = Path(profile)
        self.home = Path(home)
        self.socket_directory = Path(socket_directory)
        self.uid = uid
        self.gid = gid
        self.session = f"oda-household-{instance}"
        self._checkout_deadline: float | None = None
        self._cancellation_deadline: float | None = None

    def review_checkout(self, cart: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            return self._review_checkout(cart)

    def review_order_change(self, cart: Mapping[str, Any], order_id: str, order: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            expected_order = self._order_expectation(order_id, order)
            return self._review_checkout(cart, order_id=order_id, delivery_text=expected_order["delivery_text"])

    def _review_checkout(self, cart: Mapping[str, Any], *, order_id: str | None = None, delivery_text: str | None = None) -> dict[str, Any]:
        expected = self._cart_expectation(cart)
        if delivery_text is not None:
            expected["delivery_text"] = delivery_text
        if order_id is None:
            self._navigate_to_checkout()
        else:
            self._navigate_to_checkout(order_id)
        expanded = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const buttons=[...document.querySelectorAll('button')].filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Vis varene');
 if(buttons.length>1)return JSON.stringify({expanded:false});
 if(buttons.length===1)buttons[0].click();
 return JSON.stringify({expanded:true});
})()
""")
        if expanded != {"expanded": True}:
            raise HouseholdError("Oda checkout items cannot be reviewed")
        for _ in range(20):
            ready = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const show=[...document.querySelectorAll('button')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Vis varene');
 const inputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(input=>/\bAntall\b/i.test(norm(input.closest('li,article')?.innerText||'')));
 return JSON.stringify({ready:show.length===0&&inputs.length===COUNT});
})()
""".replace("COUNT", str(len(expected["lines"]))))
            if ready == {"ready": True}:
                break
            self._settle(0.25)
        else:
            raise HouseholdError("Oda checkout items did not finish rendering")
        script = r"""
(() => {
 const expected=EXPECTED;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const text=norm(document.body?.innerText||'');
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const labels=[...document.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>/^(Bekreft og betal|Confirm and pay)\s+\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|NOK)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 const login=!!document.querySelector('form[action*="login"],input[type="password"]');
 const unavailable=/ikke tilgjengelig|utsolgt|unavailable/i.test(text);
 const itemInputs=[...document.querySelectorAll('input[type="number"]')].filter(visible).filter(input=>/\bAntall\b/i.test(norm(input.closest('li,article')?.innerText||'')));
 const items=itemInputs.map(input=>{const root=input.closest('li,article');return {quantity:Number(input.value),text:norm([...(root?.querySelectorAll('p')||[])].filter(visible).slice(0,2).map(x=>x.innerText).join(' '))};});
 const money=value=>[...norm(value).matchAll(/\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|NOK)\b/gi)].map(match=>Number(match[1].replace(/[ .]/g,''))*100+Number(match[2]));
 const amounts=labels.length===1?money(labels[0].innerText||labels[0].getAttribute('aria-label')||''):[];
 const totalMatch=amounts.length===1&&amounts[0]===expected.total_minor;
 const deliveryRoots=[...document.querySelectorAll('h1,h2,h3,h4')].filter(visible).filter(x=>norm(x.innerText||'')==='Vi leverer varene dine').map(x=>x.closest('section,article,.k-card')).filter(Boolean);
 const escaped=norm(expected.delivery_address).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
 const addressMatch=Boolean(expected.delivery_address)&&new RegExp(`(?:^|[\\s,:])${escaped}(?=$|[\\s,])`,'i').test(text);
 const paymentMatch=text.match(/(?:[*•·xX]{2,}\s*|slutter på\s*|ending in\s*)(\d{4})\b/i);
 const maskedPayment=Boolean(paymentMatch) && !/(?:\d[ -]?){12,19}/.test(text);
 const paymentDisplay=maskedPayment?`•••• ${paymentMatch[1]}`:null;
 return JSON.stringify({url:location.href,authenticated:!login,available:!unavailable,items,total_matches:totalMatch,delivery_roots:deliveryRoots.map(root=>norm(root.innerText||'')),address_matches:addressMatch,masked_payment:maskedPayment,payment_display:paymentDisplay,submit_controls:labels.length});
})()
""".replace("EXPECTED", json.dumps(expected, ensure_ascii=False, separators=(",", ":")))
        result = self._eval(script)
        required = {"url", "authenticated", "available", "items", "total_matches", "delivery_roots", "address_matches", "masked_payment", "payment_display", "submit_controls"}
        expected_url = CHECKOUT_URL if order_id is None else f"{CHECKOUT_URL}?orderNumber={order_id}"
        if set(result) != required or result["url"] != expected_url or type(result["submit_controls"]) is not int:
            raise HouseholdError("Oda checkout page changed")
        result["line_matches"] = checkout_lines_match(expected["lines"], result.pop("items"))
        result["delivery_matches"] = checkout_delivery_matches(expected["delivery_text"], result.pop("delivery_roots"))
        if not all(result[key] is True for key in ("authenticated", "available", "line_matches", "total_matches", "delivery_matches", "address_matches", "masked_payment")) or result["submit_controls"] != 1:
            raise OdaCheckoutMismatchError("Oda checkout does not match the reviewed cart")
        if re.fullmatch(r"•••• \d{4}", str(result.get("payment_display") or "")) is None:
            raise HouseholdError("Oda checkout payment identity is unavailable")
        return result

    def _navigate_to_checkout(self, order_id: str | None = None) -> None:
        for attempt in range(3):
            try:
                self._open(CART_URL)
                break
            except HouseholdError:
                if attempt == 2:
                    raise
                try:
                    self.close()
                except HouseholdError:
                    pass
                self._settle(0.5 * (attempt + 1))
        self._settle(12)
        self._invoke("reload")
        self._invoke("snapshot")
        action = "wait"
        for attempt in range(2):
            self._settle(5)
            for _ in range(5):
                surface = self._cart_surface()
                action = surface.get("action")
                if action in {"continue", "blocked"}:
                    break
                self._settle(1)
            if action != "wait":
                break
            if attempt < 1:
                self._invoke("reload")
                self._invoke("snapshot")
        if action != "continue":
            raise HouseholdError("Oda cart cannot continue to checkout")
        self._click_action("continue", mouse=True)
        if order_id is None:
            self._advance_checkout_path()
        else:
            self._advance_checkout_path(order_id)

    def _cart_surface(self) -> dict[str, Any]:
        return self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const unavailable=/ikke tilgjengelig|utsolgt|unavailable/i;
 document.querySelectorAll('[data-oda-household-action]').forEach(x=>x.removeAttribute('data-oda-household-action'));
 if(![STORE,CART].includes(location.href))return JSON.stringify({action:'blocked'});
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(dialogs.some(root=>unavailable.test(norm(root.innerText||''))))return JSON.stringify({action:'blocked'});
 let roots=[];
 if(location.href===CART){
   const main=document.querySelector('main');
   roots=[main||document];
   if(unavailable.test(norm((main||document.body).innerText||'')))return JSON.stringify({action:'blocked'});
 }else{
   roots=dialogs.filter(root=>{
     const text=norm(root.innerText||'');
     const next=[...root.querySelectorAll('button')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Fortsett');
     const full=[...root.querySelectorAll('a')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Gå til handlekurven'&&x.href===CART);
     return full.length===1||(next.length===1&&/(?:Delsum|Tøm handlekurv|Du har \d+ varer)/i.test(text));
   });
 }
 if(roots.length>1)return JSON.stringify({action:'blocked'});
 if(roots.length===0)return JSON.stringify({action:'wait'});
 const root=roots[0];
 const next=[...root.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Fortsett');
 const full=[...root.querySelectorAll('a')].filter(visible).filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Gå til handlekurven'&&x.href===CART);
 if(next.length>1||full.length>1)return JSON.stringify({action:'blocked'});
 if(next.length===1){next[0].setAttribute('data-oda-household-action','continue');return JSON.stringify({action:'continue'});}
 if(full.length===1){full[0].setAttribute('data-oda-household-action','full-cart');return JSON.stringify({action:'full_cart'});}
 return JSON.stringify({action:'wait'});
})()
""".replace("STORE", json.dumps(STORE_URL)).replace("CART", json.dumps(CART_URL)))

    def _click_action(self, action: str, *, mouse: bool = False) -> None:
        if action not in {"open-cart", "full-cart", "continue", "new-order", "previous-order", "payment", "recommendations"}:
            raise HouseholdError("invalid Oda browser action")
        selector = f'[data-oda-household-action="{action}"]'
        if not mouse:
            self._invoke("click", selector)
            return
        self._invoke("scrollintoview", selector)
        box = self._find_box(self._invoke("get", "box", selector))
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise HouseholdError("Oda cart control is not clickable")
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        clear = self._eval(r"""
(() => {
 const target=document.elementFromPoint(X,Y);
 return JSON.stringify({clear:!!target&&!!target.closest(SELECTOR)});
})()
""".replace("X", json.dumps(x)).replace("Y", json.dumps(y)).replace("SELECTOR", json.dumps(selector)))
        if clear != {"clear": True}:
            raise HouseholdError("Oda cart control is obscured")
        self._invoke("mouse", "move", str(round(x)), str(round(y)))
        self._invoke("mouse", "down")
        self._invoke("mouse", "up")

    @staticmethod
    def _find_box(value: Any) -> dict[str, float] | None:
        if isinstance(value, Mapping):
            if all(isinstance(value.get(key), (int, float)) for key in ("x", "y", "width", "height")):
                return {key: float(value[key]) for key in ("x", "y", "width", "height")}
            for child in value.values():
                if found := OdaBrowser._find_box(child):
                    return found
        elif isinstance(value, list):
            for child in value:
                if found := OdaBrowser._find_box(child):
                    return found
        return None

    def _advance_checkout_path(self, order_id: str | None = None) -> None:
        script = r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const unavailable=/ikke tilgjengelig|utsolgt|unavailable/i;
 document.querySelectorAll('[data-oda-household-action]').forEach(x=>x.removeAttribute('data-oda-household-action'));
 const confirmPage=location.origin==='https://oda.com'&&location.pathname==='/no/checkout/confirm/';
 if(![STORE,CART,CHECKOUT_ENTRY,RECOMMENDATIONS].includes(location.href)&&!confirmPage)return JSON.stringify({action:'blocked'});
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(unavailable.test(norm(document.body?.innerText||''))||dialogs.some(root=>unavailable.test(norm(root.innerText||''))))return JSON.stringify({action:'blocked'});
 if(confirmPage){
   const controls=[...document.querySelectorAll('button')].filter(enabled);
   const submit=controls.filter(x=>/^(Bekreft og betal|Legg inn bestilling|Confirm and pay|Place order)(\b|\s)/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
   if(submit.length===1){
     const expected=ORDER;
     const actual=new URL(location.href).searchParams.get('orderNumber');
     if((expected===null&&actual===null)||(expected!==null&&actual===expected))return JSON.stringify({action:'ready'});
     return JSON.stringify({action:'blocked'});
   }
   if(submit.length>1)return JSON.stringify({action:'blocked'});
   return JSON.stringify({action:'wait'});
 }
 if(location.href===CHECKOUT_ENTRY)return JSON.stringify({action:'wait'});
 if(location.href===RECOMMENDATIONS){
   const controls=[...document.querySelectorAll('a')].filter(enabled);
   const next=controls.filter(x=>norm(x.innerText||x.getAttribute('aria-label')||'')==='Fortsett'&&x.href===CHECKOUT);
   if(next.length!==1)return JSON.stringify({action:'blocked'});
   next[0].setAttribute('data-oda-household-action','recommendations');
   return JSON.stringify({action:'recommendations'});
 }
 const main=document.querySelector('main');
 const candidates=dialogs.length?dialogs:(main&&visible(main)?[main]:[]);
 const roots=candidates.filter(root=>{
   const controls=[...root.querySelectorAll('button,a')].filter(enabled);
   const orderTokens=norm(root.innerText||'').split(/[^A-Za-z0-9_-]+/).filter(Boolean);
   const exactOrder=ORDER!==null&&orderTokens.filter(x=>x===ORDER).length===1;
   return controls.some(x=>{
     const label=norm(x.innerText||x.getAttribute('aria-label')||'');
     if(/^Legg til i forrige bestilling$/i.test(label)||label===ORDER)return exactOrder;
     return /^(Ny bestilling|Ny levering|separat(?: bestilling| levering)?|egen levering|Gå til betaling)$/i.test(label);
   });
 });
 if(roots.length>1)return JSON.stringify({action:'blocked'});
 if(roots.length===0)return JSON.stringify({action:'wait'});
 const controls=[...roots[0].querySelectorAll('button,a')].filter(enabled);
   const orderTokens=norm(roots[0].innerText||'').split(/[^A-Za-z0-9_-]+/).filter(Boolean);
   const exactOrder=ORDER!==null&&orderTokens.filter(x=>x===ORDER).length===1;
   const newOrder=controls.filter(x=>/^(Ny bestilling|Ny levering|separat(?: bestilling| levering)?|egen levering)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
   const previous=exactOrder?controls.filter(x=>/^Legg til i forrige bestilling$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')) || norm(x.innerText||x.getAttribute('aria-label')||'')===ORDER):[];
   const payment=controls.filter(x=>/^Gå til betaling$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
   if(ORDER===null && newOrder.length===1 && previous.length===0 && payment.length===0){newOrder[0].setAttribute('data-oda-household-action','new-order');return JSON.stringify({action:'new_order'});}
   if(ORDER===null && newOrder.length===0 && previous.length===0 && payment.length===1){payment[0].setAttribute('data-oda-household-action','payment');return JSON.stringify({action:'payment'});}
   if(ORDER!==null && newOrder.length===0 && previous.length===1 && payment.length===0){previous[0].setAttribute('data-oda-household-action','previous-order');return JSON.stringify({action:'previous_order'});}
   if(ORDER!==null && newOrder.length===0 && previous.length===0 && payment.length===1){payment[0].setAttribute('data-oda-household-action','payment');return JSON.stringify({action:'payment'});}
   return JSON.stringify({action:'blocked'});
})()
""".replace("STORE", json.dumps(STORE_URL)).replace("CART", json.dumps(CART_URL)).replace("CHECKOUT_ENTRY", json.dumps(CHECKOUT_ENTRY_URL)).replace("RECOMMENDATIONS", json.dumps(RECOMMENDATIONS_URL)).replace("CHECKOUT", json.dumps(CHECKOUT_URL)).replace("ORDER", json.dumps(order_id))
        dispatched: set[str] = set()
        self._settle(10)
        for _ in range(30):
            action = self._eval(script).get("action")
            if action == "ready":
                return
            if action == "blocked":
                raise HouseholdError("Oda checkout navigation is ambiguous")
            if action in {"new_order", "previous_order", "payment", "recommendations"}:
                if action not in dispatched:
                    dispatched.add(action)
                    self._click_action(action.replace("_", "-"))
                    self._settle(10)
                else:
                    self._settle(0.5)
                continue
            self._settle(0.5)
        raise HouseholdError("Oda checkout navigation timed out")

    def submit_checkout(self, cart: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None) -> None:
        with self._checkout_operation(deadline):
            self._submit_checkout(cart, review, before_click)

    def submit_order_change(self, cart: Mapping[str, Any], order_id: str, order: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None) -> None:
        with self._checkout_operation(deadline):
            try:
                current = self.review_order_change(cart, order_id, order)
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            if current != dict(review):
                raise CheckoutPreconditionError("Oda order change changed after confirmation")
            expected_total = self._cart_expectation(cart)["total_minor"]
            self._click_checkout_submit(expected_total, f"{CHECKOUT_URL}?orderNumber={order_id}", before_click)

    def review_delivery_change(self, order_id: str, order: Mapping[str, Any], delivery: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        with self._checkout_operation(deadline):
            expected_order = self._order_expectation(order_id, order)
            target = str(delivery.get("display") or "")
            signature = delivery_signature(target)
            if signature is None:
                raise HouseholdError("Oda delivery slot identity is unavailable")
            signature_value = [*signature[:5], {name: index for index, name in enumerate(("jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"), 1)}[signature[5]]]
            self._open_order(order_id)
            opened = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 document.querySelectorAll('[data-oda-household-action]').forEach(x=>x.removeAttribute('data-oda-household-action'));
 const buttons=[...document.querySelectorAll('main button')].filter(enabled).filter(x=>/^(Endre levering(?:stid)?|Endre tidspunkt|Flytt levering)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(buttons.length!==1)return JSON.stringify({ready:false});
 buttons[0].setAttribute('data-oda-household-action','delivery-change-open');
 return JSON.stringify({ready:true});
})()
""")
            if opened != {"ready": True}:
                raise HouseholdError("Oda does not currently expose delivery changes for this order")
            self._invoke("click", '[data-oda-household-action="delivery-change-open"]')
            selected = False
            dispatched: set[str] = set()
            expected_url = f"{CHECKOUT_URL}?orderNumber={order_id}"
            for _ in range(50):
                surface = self._eval(r"""
(() => {
 const wanted=SIGNATURE;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const months={jan:1,januar:1,feb:2,februar:2,mar:3,mars:3,apr:4,april:4,mai:5,jun:6,juni:6,jul:7,juli:7,aug:8,august:8,sep:9,september:9,okt:10,oktober:10,nov:11,november:11,des:12,desember:12};
 const signature=text=>{const value=norm(text).toLocaleLowerCase('nb-NO'),d=value.match(/\b(\d{1,2})\.?\s*(jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\b/i),t=value.match(/\b(\d{1,2})(?::(\d{2}))?\s*(?:-|–|og|til)\s*(\d{1,2})(?::(\d{2}))?\b/i);return d&&t?[Number(t[1]),Number(t[2]||0),Number(t[3]),Number(t[4]||0),Number(d[1]),months[d[2]]]:null};
 document.querySelectorAll('[data-oda-household-action]').forEach(x=>x.removeAttribute('data-oda-household-action'));
 const controls=[...document.querySelectorAll('button,a,[role="radio"]')].filter(enabled);
 const final=controls.filter(x=>/^(Bekreft og betal|Confirm and pay)(\b|\s)/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(location.href===URL&&final.length===1){
   const money=[...norm(final[0].innerText||final[0].getAttribute('aria-label')||'').matchAll(/\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|NOK)\b/gi)].map(m=>Number(m[1].replace(/[ .]/g,''))*100+Number(m[2]));
   const roots=[...document.querySelectorAll('h1,h2,h3,h4')].filter(visible).filter(x=>norm(x.innerText)==='Vi leverer varene dine').map(x=>x.closest('section,article,.k-card')).filter(Boolean).map(x=>norm(x.innerText));
   const text=norm(document.body?.innerText||''),payment=text.match(/(?:[*•·xX]{2,}\s*|slutter på\s*|ending in\s*)(\d{4})\b/i);
   return JSON.stringify({action:'ready',amounts:money,delivery_roots:roots,payment_display:payment?`•••• ${payment[1]}`:null,submit_controls:final.length});
 }
 const slots=controls.filter(x=>{const found=signature(x.innerText||x.getAttribute('aria-label')||'');return found&&JSON.stringify(found)===JSON.stringify(wanted)});
 if(!SELECTED){if(slots.length!==1)return JSON.stringify({action:'wait'});slots[0].setAttribute('data-oda-household-action','delivery-change-slot');return JSON.stringify({action:'slot'});}
 const next=controls.filter(x=>/^(Fortsett|Bekreft(?: levering)?|Gå til betaling)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(next.length!==1)return JSON.stringify({action:'wait'});
 next[0].setAttribute('data-oda-household-action','delivery-change-next');
 return JSON.stringify({action:'next'});
})()
""".replace("SIGNATURE", json.dumps(signature_value)).replace("URL", json.dumps(expected_url)).replace("SELECTED", "true" if selected else "false"))
                action = surface.get("action")
                if action == "ready":
                    amounts = surface.get("amounts")
                    roots = surface.get("delivery_roots")
                    payment_display = str(surface.get("payment_display") or "")
                    if not isinstance(amounts, list) or len(amounts) != 1 or not checkout_delivery_matches(target, roots) or re.fullmatch(r"•••• \d{4}", payment_display) is None or surface.get("submit_controls") != 1:
                        raise HouseholdError("Oda delivery change review does not match the requested slot")
                    summary = {
                        "items": [],
                        "count": 0,
                        "total": amounts[0] / 100,
                        "delivery": {"slot_id": delivery.get("slot_id"), "display": target},
                        "payment": payment_display,
                    }
                    return {"page_digest": hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True).encode()).hexdigest(), "summary": summary, "target_order_id": order_id, "before_delivery": expected_order["delivery_text"]}
                if action == "slot":
                    if "slot" in dispatched:
                        raise HouseholdError("Oda delivery slot selection did not advance")
                    dispatched.add("slot")
                    self._invoke("click", '[data-oda-household-action="delivery-change-slot"]')
                    selected = True
                elif action == "next":
                    if "next" in dispatched:
                        raise HouseholdError("Oda delivery change payment step did not advance")
                    dispatched.add("next")
                    self._invoke("click", '[data-oda-household-action="delivery-change-next"]')
                self._settle(0.5)
            raise HouseholdError("Oda delivery change navigation timed out")

    def submit_delivery_change(self, order_id: str, order: Mapping[str, Any], delivery: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None) -> None:
        with self._checkout_operation(deadline):
            try:
                current = self.review_delivery_change(order_id, order, delivery)
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
            if current != dict(review):
                raise CheckoutPreconditionError("Oda delivery change changed after confirmation")
            self._click_checkout_submit(
                int(round(float(review["summary"]["total"]) * 100)),
                f"{CHECKOUT_URL}?orderNumber={order_id}",
                before_click,
            )

    def _submit_checkout(self, cart: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None) -> None:
        try:
            current = self.review_checkout(cart)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        if current != dict(review):
            raise CheckoutPreconditionError("Oda checkout changed after confirmation")
        expected_total = self._cart_expectation(cart)["total_minor"]
        try:
            self._require_checkout_time(FINAL_CLICK_MARGIN)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        self._click_checkout_submit(expected_total, CHECKOUT_URL, before_click)

    def _click_checkout_submit(self, expected_total: int, expected_url: str, before_click: Callable[[], None] | None = None) -> None:
        try:
            self._require_checkout_time(FINAL_CLICK_MARGIN)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        if before_click:
            try:
                before_click()
            except HouseholdError as exc:
                raise CheckoutPreconditionError(str(exc)) from exc
        try:
            self._require_checkout_time(FINAL_CLICK_MARGIN)
        except HouseholdError as exc:
            raise CheckoutPreconditionError(str(exc)) from exc
        script = r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const money=value=>[...norm(value).matchAll(/\b(\d+(?:[ .]\d{3})*),(\d{2})\s*(?:kr|NOK)\b/gi)].map(match=>Number(match[1].replace(/[ .]/g,''))*100+Number(match[2]));
 const labels=[...document.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>/^(Bekreft og betal|Confirm and pay)\s+\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|NOK)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||''))).filter(x=>{const amounts=money(x.innerText||x.getAttribute('aria-label')||'');return amounts.length===1&&amounts[0]===TOTAL;});
 if(location.href!==URL||labels.length!==1)return JSON.stringify({clicked:false});
 labels[0].click(); return JSON.stringify({clicked:true});
})()
""".replace("URL", json.dumps(expected_url)).replace("TOTAL", json.dumps(expected_total))
        if self._eval(script) != {"clicked": True}:
            raise CheckoutPreconditionError("Oda checkout button changed before click")

    def review_cancellation(self, order_id: str, order: Mapping[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        with self._cancellation_operation(deadline):
            return self._review_cancellation(order_id, order)

    def _review_cancellation(self, order_id: str, order: Mapping[str, Any]) -> dict[str, Any]:
        url = self._order_url(order_id)
        self._open_order(order_id)
        expected = self._order_expectation(order_id, order)
        for _ in range(20):
            result = self._eval(r"""
(() => {
 const expected=EXPECTED;
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const hasMoney=value=>/\b\d+(?:[ .]\d{3})*,\d{2}\s*(?:kr|NOK)\b/i.test(norm(value))||/\b(?:kr|NOK)[,\s]*\d+(?:[ .]\d{3})*,\d{2}\b/i.test(norm(value));
 if(location.href!==URL)return JSON.stringify({available:false,reason:'Ordredetaljene avviker'});
 const roots=[...document.querySelectorAll('main')].filter(visible);
 if(roots.length!==1)return JSON.stringify({available:false,reason:'Ordredetaljene avviker'});
 const root=roots[0];
 const lines=(root.innerText||'').split(/\n+/).map(norm).filter(Boolean);
 if(!lines.includes(`Bestilling ${expected.order_id}`)&&!lines.includes(`Order ${expected.order_id}`))return JSON.stringify({available:false,reason:'Ordredetaljene avviker'});
 const deliveryLines=lines.filter(x=>/\b\d{1,2}\.?\s*(?:jan(?:uar)?|feb(?:ruar)?|mar(?:s)?|apr(?:il)?|mai|jun(?:i)?|jul(?:i)?|aug(?:ust)?|sep(?:tember)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\b/i.test(x)&&/\b\d{1,2}(?::\d{2})?\s*(?:-|–|og|til)\s*\d{1,2}(?::\d{2})?(?![:.]\d)\b/i.test(x));
 const totalPattern=/^(?:Total|Totalt)(?: inkl\.? MVA)?$/i;
 const totalLabels=[...root.querySelectorAll('*')].filter(visible).filter(x=>totalPattern.test(norm(x.innerText||''))).filter(x=>![...x.children].some(child=>visible(child)&&totalPattern.test(norm(child.innerText||''))));
 const totalRows=totalLabels.map(label=>{let row=label.parentElement;while(row&&row!==root){const text=norm(row.innerText||'');if(hasMoney(text))return text;row=row.parentElement;}return null;}).filter(Boolean);
 if(totalLabels.length!==1||totalRows.length!==1)return JSON.stringify({available:false,reason:'Ordredetaljene avviker'});
 document.querySelectorAll('[data-oda-household-cancel-review]').forEach(x=>x.removeAttribute('data-oda-household-cancel-review'));
 const buttons=[...root.querySelectorAll('button')].filter(visible).filter(x=>!x.disabled&&x.getAttribute('aria-disabled')!=='true').filter(x=>/^(Kanseller bestillingen|Avbestill bestillingen|Cancel order)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(buttons.length!==1)return JSON.stringify({available:false,reason:'Oda tilbyr ikke avbestilling nå'});
 if(!Object.keys(buttons[0]).some(key=>key.startsWith('__reactProps')))return JSON.stringify({available:false,retry:true,reason:'Oda-siden er ikke klar'});
 buttons[0].setAttribute('data-oda-household-cancel-review','');
 const marked=[...document.querySelectorAll('[data-oda-household-cancel-review]')];
 if(marked.length!==1||marked[0]!==buttons[0]||!visible(marked[0])){buttons[0].removeAttribute('data-oda-household-cancel-review');return JSON.stringify({available:false,reason:'Oda tilbyr ikke avbestilling nå'});}
 return JSON.stringify({available:true,delivery_lines:deliveryLines,total_rows:totalRows});
})()
""".replace("EXPECTED", json.dumps(expected, ensure_ascii=False, separators=(",", ":"))).replace("URL", json.dumps(url)), browser_args=CANCELLATION_BROWSER_ARGS)
            if result.get("retry") is not True:
                break
            self._settle_cancellation(0.5)
        else:
            return {"available": False, "reason": "Oda-siden ble ikke ferdig lastet"}
        if (
            result.get("available") is not True
            or not cancellation_delivery_matches(expected["delivery_text"], result.get("delivery_lines"))
            or not cancellation_total_matches(expected["total_minor"], result.get("total_rows"))
        ):
            return {"available": False, "reason": str(result.get("reason") or "Oda tilbyr ikke avbestilling nå")}
        self._invoke("click", "[data-oda-household-cancel-review]", browser_args=CANCELLATION_BROWSER_ARGS)
        for _ in range(20):
            dialog = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(dialogs.length!==1)return JSON.stringify({available:false,consequence:null});
 const root=dialogs[0];
 const text=norm(root.innerText||'');
 const final=[...root.querySelectorAll('button')].filter(enabled).filter(x=>/^(Kanseller bestillingen min|Avbestill bestillingen|Confirm cancellation)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 const dismiss=[...root.querySelectorAll('button')].filter(enabled).filter(x=>/^(Nei, ikke kanseller|Ikke avbestill|Do not cancel)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 document.querySelectorAll('[data-oda-household-cancel-dismiss]').forEach(x=>x.removeAttribute('data-oda-household-cancel-dismiss'));
 if(final.length===1&&dismiss.length===1&&final[0]!==dismiss[0])dismiss[0].setAttribute('data-oda-household-cancel-dismiss','');
 const marked=[...document.querySelectorAll('[data-oda-household-cancel-dismiss]')];
 const fees=(text.match(/[^.]{0,80}(?:gebyr|fee)[^.]{0,80}/i)||[])[0]||null;
 return JSON.stringify({available:marked.length===1&&marked[0]===dismiss[0]&&enabled(marked[0]),consequence:fees});
})()
""", browser_args=CANCELLATION_BROWSER_ARGS)
            if dialog.get("available") is True:
                break
            self._settle_cancellation(0.25)
        else:
            return {"available": False, "reason": "Oda tilbyr ikke avbestilling nå"}
        if dialog.get("available") is not True:
            return {"available": False, "reason": "Oda tilbyr ikke avbestilling nå"}
        self._invoke("click", "[data-oda-household-cancel-dismiss]", browser_args=CANCELLATION_BROWSER_ARGS)
        for _ in range(20):
            closed = self._eval(r"""
(() => {
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 return JSON.stringify({closed:![...document.querySelectorAll('[role="dialog"]')].some(visible)});
})()
""", browser_args=CANCELLATION_BROWSER_ARGS)
            if closed == {"closed": True}:
                break
            self._settle_cancellation(0.25)
        return dialog if closed == {"closed": True} else {"available": False, "reason": "Oda tilbyr ikke avbestilling nå"}

    def submit_cancellation(self, order_id: str, order: Mapping[str, Any], review: Mapping[str, Any], before_click: Callable[[], None] | None = None, *, deadline: float | None = None) -> None:
        operation = None
        try:
            with self._cancellation_operation(deadline) as operation:
                self._submit_cancellation(order_id, order, review, operation, before_click)
        except HouseholdError as exc:
            if operation is None or operation["final_dispatched"] is not True:
                raise CancellationPreconditionError(str(exc)) from exc
            raise

    def _submit_cancellation(self, order_id: str, order: Mapping[str, Any], review: Mapping[str, Any], operation: dict[str, bool], before_click: Callable[[], None] | None = None) -> None:
        if self._review_cancellation(order_id, order) != dict(review):
            raise HouseholdError("Oda tilbyr ikke avbestilling nå")
        ready = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 if(location.href!==URL)return JSON.stringify({ready:false});
 const roots=[...document.querySelectorAll('main')].filter(visible);
 if(roots.length!==1)return JSON.stringify({ready:false});
 const lines=(roots[0].innerText||'').split(/\n+/).map(norm).filter(Boolean);
 if(!lines.includes(`Bestilling ${ORDER}`)&&!lines.includes(`Order ${ORDER}`))return JSON.stringify({ready:false});
 document.querySelectorAll('[data-oda-household-cancel-submit-open]').forEach(x=>x.removeAttribute('data-oda-household-cancel-submit-open'));
 const buttons=[...roots[0].querySelectorAll('button')].filter(enabled).filter(x=>/^(Kanseller bestillingen|Avbestill bestillingen|Cancel order)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 if(buttons.length!==1)return JSON.stringify({ready:false});
 buttons[0].setAttribute('data-oda-household-cancel-submit-open','');
 const marked=[...document.querySelectorAll('[data-oda-household-cancel-submit-open]')];
 return JSON.stringify({ready:marked.length===1&&marked[0]===buttons[0]&&enabled(marked[0])});
})()
""".replace("URL", json.dumps(self._order_url(order_id))).replace("ORDER", json.dumps(order_id)), browser_args=CANCELLATION_BROWSER_ARGS)
        if ready != {"ready": True}:
            raise HouseholdError("Oda cancellation control changed")
        self._invoke("click", "[data-oda-household-cancel-submit-open]", browser_args=CANCELLATION_BROWSER_ARGS)
        final = self._eval(r"""
(() => {
 const norm=v=>(v||'').normalize('NFC').replace(/\s+/g,' ').trim();
 const visible=x=>{const style=getComputedStyle(x),box=x.getBoundingClientRect();return style.display!=='none'&&style.visibility!=='hidden'&&box.width>0&&box.height>0};
 const enabled=x=>visible(x)&&!x.disabled&&x.getAttribute('aria-disabled')!=='true';
 const dialogs=[...document.querySelectorAll('[role="dialog"]')].filter(visible);
 if(dialogs.length!==1)return JSON.stringify({ready:false});
 const root=dialogs[0];
 const confirm=[...root.querySelectorAll('button')].filter(enabled).filter(x=>/^(Kanseller bestillingen min|Avbestill bestillingen|Confirm cancellation)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 const dismiss=[...root.querySelectorAll('button')].filter(enabled).filter(x=>/^(Nei, ikke kanseller|Ikke avbestill|Do not cancel)$/i.test(norm(x.innerText||x.getAttribute('aria-label')||'')));
 document.querySelectorAll('[data-oda-household-cancel-submit-final]').forEach(x=>x.removeAttribute('data-oda-household-cancel-submit-final'));
 if(confirm.length!==1||dismiss.length!==1||confirm[0]===dismiss[0])return JSON.stringify({ready:false});
 confirm[0].setAttribute('data-oda-household-cancel-submit-final','');
 const marked=[...document.querySelectorAll('[data-oda-household-cancel-submit-final]')];
 return JSON.stringify({ready:marked.length===1&&marked[0]===confirm[0]&&enabled(marked[0])});
})()
""", browser_args=CANCELLATION_BROWSER_ARGS)
        if final != {"ready": True}:
            raise HouseholdError("Oda cancellation confirmation changed")
        self._require_cancellation_time(FINAL_CLICK_MARGIN)
        if before_click:
            before_click()
        self._require_cancellation_time(FINAL_CLICK_MARGIN)
        operation["final_dispatched"] = True
        self._invoke("click", "[data-oda-household-cancel-submit-final]", browser_args=CANCELLATION_BROWSER_ARGS)

    def close(self) -> None:
        self._invoke("close", check=False)

    @contextmanager
    def _cancellation_operation(self, deadline: float | None = None):
        previous = getattr(self, "_cancellation_deadline", None)
        outer = previous is None
        if outer:
            self._cancellation_deadline = deadline if deadline is not None else time.monotonic() + CANCELLATION_BROWSER_TIMEOUT
        elif deadline is not None:
            self._cancellation_deadline = min(previous, deadline)
        operation = {"final_dispatched": False}
        started = False
        try:
            if outer:
                self._invoke("close", browser_args=CANCELLATION_BROWSER_ARGS)
                self._clear_cancellation_cache()
            started = True
            yield operation
        finally:
            try:
                if outer and started and operation["final_dispatched"] is not True:
                    self._invoke("close", browser_args=CANCELLATION_BROWSER_ARGS)
            finally:
                self._cancellation_deadline = previous

    def _clear_cancellation_cache(self) -> None:
        profile = getattr(self, "profile", None)
        if profile is None:
            return
        if os.geteuid() != 0 or self.uid == 0:
            clear_cancellation_cache(profile)
            return

        def drop_privileges() -> None:
            os.setgroups([])
            os.setgid(self.gid)
            os.setuid(self.uid)

        deadline = getattr(self, "_cancellation_deadline", None)
        remaining = 30.0 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise HouseholdError("Oda browser deadline reached")
        try:
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--clear-cancellation-cache", str(profile)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=min(30.0, remaining),
                check=False,
                preexec_fn=drop_privileges,
                env={"LANG": "C.UTF-8", "PATH": os.environ.get("PATH", os.defpath)},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HouseholdError("Oda browser cache cannot be reset") from exc
        if completed.returncode != 0:
            raise HouseholdError("Oda browser cache cannot be reset")

    @contextmanager
    def _checkout_operation(self, deadline: float | None = None):
        previous = getattr(self, "_checkout_deadline", None)
        if previous is None:
            self._checkout_deadline = deadline if deadline is not None else time.monotonic() + CHECKOUT_BROWSER_TIMEOUT
        elif deadline is not None:
            self._checkout_deadline = min(previous, deadline)
        try:
            if previous is None:
                self._invoke("close", browser_args=DEFAULT_BROWSER_ARGS)
            yield
        finally:
            self._checkout_deadline = previous

    def _require_checkout_time(self, minimum: float = 0) -> None:
        deadline = getattr(self, "_checkout_deadline", None)
        if deadline is not None and deadline - time.monotonic() < minimum:
            raise HouseholdError("Oda checkout browser deadline reached")

    def _require_cancellation_time(self, minimum: float = 0) -> None:
        deadline = getattr(self, "_cancellation_deadline", None)
        if deadline is not None and deadline - time.monotonic() < minimum:
            raise HouseholdError("Oda cancellation browser deadline reached")

    def _settle(self, seconds: float) -> None:
        self._require_checkout_time(seconds)
        time.sleep(seconds)

    def _settle_cancellation(self, seconds: float) -> None:
        self._require_cancellation_time(seconds)
        time.sleep(seconds)

    @staticmethod
    def _order_url(order_id: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", order_id) is None:
            raise HouseholdError("invalid order identity")
        return f"https://oda.com/no/account/orders/{order_id}/"

    def _open_order(self, order_id: str) -> None:
        expected = self._order_url(order_id)
        data = self._invoke("open", f"https://oda.com/no/orders/{order_id}/", browser_args=CANCELLATION_BROWSER_ARGS)
        if str(data.get("url") or "").rstrip("/") != expected.rstrip("/"):
            raise HouseholdError("Oda browser left the requested order page")

    @staticmethod
    def _order_expectation(order_id: str, order: Mapping[str, Any]) -> dict[str, Any]:
        actual_id = str(order.get("orderNumber") or order.get("order_number") or order.get("id") or "")
        if actual_id != order_id:
            raise HouseholdError("Oda order identity changed")
        total = order.get("grossAmount", order.get("subtotal", order.get("total")))
        if isinstance(total, bool):
            raise HouseholdError("Oda order total is unavailable")
        try:
            total_value = float(total)
            if not math.isfinite(total_value):
                raise ValueError
            total_minor = int(round(total_value * 100))
            total_text = f"{total_value:.2f}".replace(".", ",")
        except (TypeError, ValueError, OverflowError) as exc:
            raise HouseholdError("Oda order total is unavailable") from exc
        delivery = next((order[key] for key in ("deliverySlotDisplay", "deliveryDate", "delivery_date") if key in order), "")
        if not isinstance(delivery, str) or not delivery.strip():
            raise HouseholdError("Oda order delivery is unavailable")
        return {"order_id": order_id, "total_text": total_text, "total_minor": total_minor, "delivery_text": delivery}

    @staticmethod
    def _cart_expectation(cart: Mapping[str, Any]) -> dict[str, Any]:
        summary = cart_summary(cart)
        if not summary["items"]:
            raise HouseholdError("cart is empty")
        raw_lines = []
        groups = cart.get("groups")
        if isinstance(groups, list):
            for group in groups:
                if isinstance(group, Mapping) and isinstance(group.get("items"), list):
                    raw_lines.extend(group["items"])
        elif isinstance(cart.get("items"), list):
            raw_lines = list(cart["items"])
        if len(raw_lines) != len(summary["items"]):
            raise HouseholdError("cart line identity is unavailable")
        lines = []
        for item, raw in zip(summary["items"], raw_lines, strict=True):
            name = item["name"].strip()
            quantity = item["quantity"]
            if not name or not isinstance(raw, Mapping):
                raise HouseholdError("cart line is incomplete")
            product = raw.get("product") if isinstance(raw.get("product"), Mapping) else raw
            raw_name = product.get("name")
            description = product.get("description", "")
            brand = product.get("brand", "")
            if brand is None:
                brand = ""
            if not isinstance(raw_name, str) or not isinstance(description, str) or not isinstance(brand, str):
                raise HouseholdError("cart line identity is invalid")
            if raw_name.strip() != name:
                raise HouseholdError("cart line identity changed")
            identity = product_identity(name, description.strip(), brand.strip())
            if not identity:
                raise HouseholdError("cart line identity is unavailable")
            lines.append({"name": name, "identity": identity, "quantity": quantity})
        delivery = summary.get("delivery") or {}
        address = delivery.get("address")
        if not isinstance(address, str) or not address.strip():
            raise HouseholdError("Oda checkout delivery address is unavailable")
        return {
            "lines": lines,
            "total_text": f"{summary['total']:.2f}".replace(".", ","),
            "total_minor": int(round(summary["total"] * 100)),
            "delivery_text": str(delivery.get("display") or ""),
            "delivery_address": unicodedata.normalize("NFC", " ".join(address.split())),
        }

    def _open(self, url: str) -> None:
        data = self._invoke("open", url)
        if str(data.get("url") or "").rstrip("/") != url.rstrip("/"):
            raise HouseholdError("Oda browser left the requested page")

    def _eval(self, script: str, *, browser_args: str = DEFAULT_BROWSER_ARGS) -> dict[str, Any]:
        data = self._invoke("eval", "--stdin", stdin=script, browser_args=browser_args)
        raw = data.get("result")
        if not isinstance(raw, str):
            raise HouseholdError("Oda browser returned no result")
        try:
            value = json.loads(raw)
            if isinstance(value, str):
                value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise HouseholdError("Oda browser returned malformed data") from exc
        if not isinstance(value, dict):
            raise HouseholdError("Oda browser result is invalid")
        return value

    def _invoke(self, *arguments: str, stdin: str | None = None, check: bool = True, browser_args: str = DEFAULT_BROWSER_ARGS) -> dict[str, Any]:
        command = [str(self.binary), "--json", "--session", self.session, "--profile", str(self.profile), "--executable-path", str(self.executable), *arguments]
        environment = {
            "AGENT_BROWSER_ARGS": browser_args,
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

        timeout = 90.0
        deadlines = [
            value
            for value in (getattr(self, "_checkout_deadline", None), getattr(self, "_cancellation_deadline", None))
            if value is not None
        ]
        if deadlines:
            deadline = min(deadlines)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HouseholdError("Oda browser deadline reached")
            timeout = min(timeout, max(0.1, remaining))
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
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HouseholdError("Oda browser is unavailable") from exc
        if completed.returncode != 0:
            if not check:
                return {}
            raise HouseholdError("Oda browser operation failed")
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise HouseholdError("Oda browser response is malformed") from exc
        if envelope.get("success") is not True or not isinstance(envelope.get("data"), dict):
            if not check:
                return {}
            raise HouseholdError("Oda browser rejected the operation")
        return envelope["data"]


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--clear-cancellation-cache":
        raise SystemExit(2)
    clear_cancellation_cache(sys.argv[2])
