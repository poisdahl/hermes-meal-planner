"""Delivery and protected checkout/order state transitions.

Application owns shared state and locks; these methods run on that same instance.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import math
import re
import secrets
import time
from typing import Any, Mapping
import unicodedata
from zoneinfo import ZoneInfo
from oda_browser import OdaCheckoutMismatchError, delivery_signature as oda_delivery_signature
from core import CancellationPreconditionError, CheckoutPreconditionError, HouseholdError, cart_summary, cheapest_delivery_slot, delivery_candidate_digest, delivery_price_display, validate_delivery_slot
from oda import oda_cart_delivery_matches_slot, oda_cart_delivery_window, oda_delivery_slot_date
from meny import MENY_ORDER_TIMEOUT, MenyOrderChangeDispatchError, meny_checkout_reviews_match
from planning_assessment import assess_menu
from service_common import (
    CANCELLATION_OPERATION_TIMEOUT,
    MENY_CHECKOUT_OPERATION_TIMEOUT,
    MENY_VIPPS_EXPIRY_BUFFER,
    SCHEDULE_OCCURRENCE_LEASE,
    SCHEDULE_WEEKDAYS,
    UNRESOLVED_CHECKOUT_STATUSES,
    bounded_limit,
    canonical,
    checkout_intent_signature,
    delivery_matches,
    email_job_provider,
    expired_awaiting_confirmation,
    meny_order_matches_checkout,
    money_cents,
    oda_order_address_identity,
    oda_order_matches_addition,
    oda_order_quantities,
    order_matches_checkout,
    require_provider_identity,
    safe_order_id,
    scheduled_occurrence,
    validate_schedule
)


class OrderOperations:
    @staticmethod
    def _find_delivery_slot(value: Any, slot_id: Any) -> dict[str, Any] | None:
        if isinstance(value, Mapping):
            candidate_id = value.get("id", value.get("slot_id", value.get("deliverySlotId")))
            if candidate_id is not None and str(candidate_id) == str(slot_id):
                display = value.get("name", value.get("display", value.get("description")))
                if isinstance(display, str) and display.strip():
                    return {"slot_id": candidate_id, "display": display.strip()}
            for child in value.values():
                if found := OrderOperations._find_delivery_slot(child, slot_id):
                    return found
        elif isinstance(value, list):
            for child in value:
                if found := OrderOperations._find_delivery_slot(child, slot_id):
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
            "observed_at": self._now().isoformat(),
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
            oda_today = self._now().astimezone(ZoneInfo("Europe/Oslo")).date()
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

        dates = self._scheduled_delivery_dates(schedule, self._now())
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
            oda_today = self._now().astimezone(ZoneInfo("Europe/Oslo")).date()
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
                self._scheduled_delivery_dates(schedule, self._now()),
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
        *, automatic: bool = True,
    ) -> str | None:
        if not occurrence or not automatic:
            return None
        state = self.store.read()
        schedule = state.get("schedule")
        if not isinstance(schedule, Mapping):
            return "scheduled checkout configuration changed"
        maximum_total = validate_schedule(schedule, self.provider)
        if state.get("menu"):
            assessment = assess_menu(state)
            if not assessment["ready"]:
                return "menu coverage or explicit ingredient constraints need review"
            plan = state.get("cart_plan") or {}
            if plan.get("menu_ref") != assessment["menu_ref"] or not plan.get("product_plan_digest"):
                return "menu ingredients need an applied, exact product plan before automatic checkout"
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
                    self._scheduled_delivery_dates(schedule, self._now()), deadline=deadline,
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
            automatic_checkout=pending.get("automatic_checkout", bool(occurrence)),
        )
        problem = self._scheduled_checkout_problem(prepared["summary"], occurrence or None, automatic=pending.get("automatic_checkout", bool(occurrence)))
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
                "started_at": self._now().isoformat(),
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
                    still_starting = self._now() < started_at + timedelta(minutes=5)
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
                    state["pending_cancellation"] = {"order_id": order_id, "confirmation_id": confirmation_id, "before": current, "browser": browser, "expires_at": (self._now() + timedelta(minutes=30)).isoformat(), "status": "awaiting_confirmation"}
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
                if pending and pending.get("confirmation_id") == bound_confirmation and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending, self._now()):
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
        if self._now() >= datetime.fromisoformat(pending["expires_at"]):
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
                if self._now() >= datetime.fromisoformat(pending["expires_at"]):
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

    def _mark_order_cancelled(self,
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
                usage["updated_at"] = self._now().isoformat()
        current = state.get("menu")
        if isinstance(current, dict) and current.get("order_id") == order_id:
            current["phase"] = "draft"
            current.pop("order_id", None)
        OrderOperations._prune_order_snapshots(state)

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
                if pending and pending.get("confirmation_id") == bound_confirmation and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending, self._now()):
                    prepared = {
                        "confirmation_id": bound_confirmation,
                        "summary": deepcopy(pending["summary"]),
                        "order_change": deepcopy(pending.get("order_change")),
                    }
                else:
                    return self._checkout_reconcile(deadline, bound_confirmation)
            elif pending and pending.get("status") == "awaiting_confirmation" and not expired_awaiting_confirmation(pending, self._now()):
                prepared = {
                    "confirmation_id": pending["confirmation_id"],
                    "summary": deepcopy(pending["summary"]),
                    "order_change": deepcopy(pending.get("order_change")),
                }
            else:
                prepared = self._checkout({"action": "prepare", "occurrence": request.get("occurrence")})
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
                    existing["rebound_at"] = self._now().isoformat()
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
                expected_occurrence = scheduled_occurrence(schedule, self._now())
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
                    if started_at is not None and started_at.tzinfo is not None and self._now() < started_at + SCHEDULE_OCCURRENCE_LEASE:
                        raise HouseholdError("this scheduled occurrence is already running")
                pending = state.get("pending_checkout")
                if pending and pending.get("status") == "awaiting_confirmation" and pending.get("occurrence"):
                    self._abandon_predispatch(state, reason="scheduled run retried")
                elif pending:
                    raise HouseholdError("finish the pending interactive or dispatched checkout before the scheduled run")
                attempts = int(existing.get("attempts", 0)) + 1 if isinstance(existing, Mapping) else 1
                state["occurrences"][occurrence] = {"status": "started", "at": self._now().isoformat(), "attempts": attempts}
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
                    deadline, occurrence=occurrence, delivery_binding=delivery_choice, automatic_checkout=True,
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
        automatic_checkout: bool = False,
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
                automatic_checkout = current_pending.get("automatic_checkout", True)
                occurrence = inherited_occurrence
                occurrence_record = state.get("occurrences", {}).get(occurrence)
                if isinstance(occurrence_record, dict):
                    occurrence_record["status"] = "started"
                    occurrence_record["at"] = self._now().isoformat()
            if expired_awaiting_confirmation(current_pending, self._now()):
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
            if expired_awaiting_confirmation(state.get("pending_cancellation"), self._now()):
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
                    "expires_at": (self._now() + timedelta(minutes=20)).isoformat(),
                    "menu": menu_baseline,
                    "cart_plan": cart_plan_baseline,
                    "menu_ref": {
                        "menu_id": menu_baseline.get("menu_id"),
                        "revision": menu_baseline.get("revision"),
                        "digest": menu_baseline.get("digest"),
                    } if isinstance(menu_baseline, Mapping) else None,
                    "occurrence": occurrence,
                    "automatic_checkout": automatic_checkout,
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
        if self._now() >= datetime.fromisoformat(pending["expires_at"]):
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
                        automatic=pending.get("automatic_checkout", bool(pending.get("occurrence"))),
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
                    if self._now() >= datetime.fromisoformat(pending["expires_at"]):
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
                    requested_at = self._now()
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
                record["completed_at"] = self._now().isoformat()
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
        state.setdefault("order_snapshot_times", {})[order_id] = self._now().isoformat()
        state.setdefault("order_snapshot_providers", {})[order_id] = self.provider
        menu_id = snapshot.get("menu_id")
        usage = state.setdefault("recipe_usage", {}).get(menu_id)
        if isinstance(usage, dict):
            usage["status"] = "ordered"
            usage["ordered_slot_ids"] = [s["slot_id"] for s in snapshot.get("slots", []) if s["slot_id"] not in snapshot.get("historical_slot_ids", [])]
            usage["order_id"] = order_id
            usage["updated_at"] = self._now().isoformat()
        current = state.get("menu")
        if isinstance(current, Mapping) and current.get("menu_id") == snapshot.get("menu_id") and current.get("digest") == snapshot.get("digest"):
            state["menu"] = deepcopy(snapshot)
        OrderOperations._prune_order_snapshots(state, keep_order_id=order_id)

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
            and self._now() < payment_expires_at
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
            expired_unpaid = expires_at is not None and expires_at.tzinfo is not None and self._now() >= expires_at
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
