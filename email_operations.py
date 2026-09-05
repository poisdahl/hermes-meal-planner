"""Order-bound recipe email scheduling and dispatch lifecycle.

Application owns shared state and locks; these methods run on that same instance.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import secrets
from typing import Any, Mapping
from core import HouseholdError, mask_email, valid_email_address
from service_common import (
    EMAIL_AUTOMATION_PROTOCOL,
    EMAIL_CLAIM_LEASE,
    MAX_REQUEST,
    canonical,
    email_automation_ack,
    email_automation_key,
    email_automation_prompt,
    email_job_provider,
    expired_awaiting_confirmation,
    menu_email_html,
    menu_email_period,
    require_provider_identity,
    safe_order_id
)


class EmailOperations:
    def _email_order_read(self, provider: str, order_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        client = self.email_provider_clients.get(provider)
        if client is None:
            raise HouseholdError(f"provider {provider} is unavailable for the bound email job")
        if provider in {"oda", "mathem"}:
            # Cancellation tracking can remain available after order details are
            # removed. A generic provider/auth/not-found error is never proof.
            tracking = client.call("order_tracking", {"order_number": order_id})
            require_provider_identity(tracking, order_id, tracking=True)
            if str(tracking.get("status") or "").casefold() in {"cancelled", "canceled"}:
                return {"order": {}, "tracking": tracking}
            order = client.call("get_order", {"order_number": order_id})
            require_provider_identity(order, order_id)
            return {"order": order, "tracking": tracking}
        if provider != self.provider:
            order = client.call("get_order", {"order_number": order_id})
            tracking = client.call("order_tracking", {"order_number": order_id})
            require_provider_identity(order, order_id)
            require_provider_identity(tracking, order_id, tracking=True)
            return {"order": order, "tracking": tracking}
        return self._orders({"action": "get", "order_id": order_id, "_deadline": request.get("_deadline")})

    def _email(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = request.get("action", "status")
        requested_provider = request.get("provider")
        if requested_provider is not None and (
            not isinstance(requested_provider, str) or requested_provider not in {"oda", "meny", "mathem"}
        ):
            raise HouseholdError("email provider must be oda, meny or mathem")

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

        def cleanup_action(job: Mapping[str, Any]) -> dict[str, Any]:
            provider = email_job_provider(job)
            order_id = safe_order_id(job.get("order_id"))
            return {"provider": provider, "order_id": order_id,
                    "automation_key": job.get("automation_key") or email_automation_key(provider, order_id),
                    "action": "remove", "reason": "order cancelled"}

        if action in {"cancel_followup", "reconcile"}:
            order_id = safe_order_id(request.get("order_id"))
            if requested_provider is None:
                raise HouseholdError("follow-up reconciliation requires an exact provider and order_id")
            state = self.store.read()
            jobs = matching_jobs(state, order_id)
            if len(jobs) != 1:
                raise HouseholdError("follow-up reconciliation requires exactly one existing email job")
            initial_job = deepcopy(jobs[0])
            if initial_job.get("status") == "sending":
                raise HouseholdError("email send outcome needs reconciliation before closing its follow-up")
            if initial_job.get("status") == "cancelled":
                return {"send": False, "cancelled": True, "automation_cleanup": cleanup_action(initial_job)}
            if initial_job.get("status") not in {"pending", "claimed"}:
                return {"send": False, "cancelled": False, "reason": "email follow-up is already terminal"}
            if action == "cancel_followup":
                if request.get("owner_confirmed_cancelled") is not True:
                    raise HouseholdError("the owner must explicitly confirm external order cancellation")
            else:
                current = self._email_order_read(requested_provider, order_id, request)
                if str(current["tracking"].get("status") or "").casefold() not in {"cancelled", "canceled"}:
                    return {"send": False, "cancelled": False, "reason": "provider has not confirmed cancellation"}
            with self.store.locked() as state:
                jobs = matching_jobs(state, order_id)
                if len(jobs) != 1 or canonical(jobs[0]) != canonical(initial_job):
                    raise HouseholdError("email job changed while checking its cancellation")
                self._mark_order_cancelled(state, order_id, provider=requested_provider, active_provider=self.provider)
            return {"send": False, "cancelled": True, "automation_cleanup": cleanup_action(initial_job)}

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
            return {"protocol": EMAIL_AUTOMATION_PROTOCOL, "updates": updates,
                    "removals": [cleanup_action(job) for job in state["email_jobs"] if job.get("status") == "cancelled"]}
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
            current = self._email_order_read(job_provider, order_id, request)
            tracking = str((current.get("tracking") or {}).get("status") or "").casefold()
            with self.store.locked() as state:
                matching = matching_jobs(state, order_id, {"pending", "claimed", "sending"})
                if len(matching) != 1 or canonical(matching[0]) != canonical(initial_job):
                    raise HouseholdError("email job changed while checking its provider order")
                pending_cancellation = state.get("pending_cancellation")
                if expired_awaiting_confirmation(pending_cancellation, self._now()):
                    state["pending_cancellation"] = None
                    pending_cancellation = None
                if job_provider == self.provider and isinstance(pending_cancellation, Mapping) and pending_cancellation.get("order_id") == order_id:
                    return {"send": False, "reason": "order cancellation is pending"}
                if len(matching) == 1 and matching[0].get("status") == "claimed":
                    try:
                        claim_expires_at = datetime.fromisoformat(str(matching[0].get("claim_expires_at") or ""))
                    except ValueError:
                        claim_expires_at = None
                    if claim_expires_at is not None and claim_expires_at.tzinfo is not None and self._now() >= claim_expires_at:
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
                    return {"send": False, "reason": "order cancelled", "automation_cleanup": cleanup_action(initial_job)}
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
                job["claim_expires_at"] = (self._now() + EMAIL_CLAIM_LEASE).isoformat()
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
                if expires_at.tzinfo is None or self._now() >= expires_at:
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
                jobs[0]["dispatch_started_at"] = self._now().isoformat()
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
                jobs[0]["sent_at"] = self._now().isoformat()
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
