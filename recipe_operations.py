"""Recipe discovery, libraries and explicit recipe lifecycle operations.

Application owns shared state and locks; these methods run on that same instance.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import math
import re
import secrets
import threading
import time
from typing import Any, Mapping
import unicodedata
from core import HouseholdError
from recipes import RecipeError, normalize_recipe, normalize_source_url, scale_recipe, validate_week
from recipe_libraries import CAPABILITY_NAMES, MAX_LIBRARY_RECIPE_KEY, RecipeLibraryAdapter, RecipeLibraryDefiniteError, RecipeLibraryError, RecipeLibraryExternalMissingError, RecipeLibraryFavoriteConflictError, RecipeLibraryLabelConflictError, RecipeLibraryUncertainError, RecipeLibraryUpdateConflictError, library_recipe_key, library_recipe_key_aliases, normalize_label_name, validate_library_id, validate_library_label_ref, validate_library_recipe_ref, verified_capabilities
from recipe_sources import SOURCE_IDS, provider_recipe_candidates, validate_source_settings
from service_common import (
    LIBRARY_SEARCH_CURSOR_PREFIX,
    MAX_EXTERNAL_FAVORITE_SEARCH_PAGES,
    bounded_limit,
    canonical
)


class RecipeOperations:
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
        usage_records = state.get("recipe_usage") or {}
        ordered_slots = {slot_id for record in usage_records.values() if record.get("status") == "ordered" for slot_id in record.get("ordered_slot_ids", [])}
        historical_ordered_slots = {slot_id for record in usage_records.values() if record.get("status") == "ordered" or record.get("previous_status") == "ordered" for slot_id in record.get("ordered_slot_ids", [])}
        for menu_id, record in usage_records.items():
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
            for slot in record.get("slots", []):
                if slot.get("recipe_key") not in identity_keys:
                    continue
                overlay = state.get("menu_planning", {}).get("outcomes", {}).get(slot["slot_id"])
                if overlay is not None:
                    cooked = overlay["outcome"] == "cooked"
                    not_cooked = overlay["outcome"] == "not_cooked"
                if slot["slot_id"] in ordered_slots:
                    status = "ordered"
                if slot["slot_id"] in historical_ordered_slots:
                    last_ordered = max(filter(None, (last_ordered, record_week)), default=record_week)
            retired = bool(identity_keys.intersection(state.get("menu_planning", {}).get("retired", {}).get(menu_id, [])))
            if retired and status == "planned" and not cooked:
                continue
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

    def _store_usage_request(self, state: dict[str, Any], key: str, digest: str, result: Mapping[str, Any]) -> None:
        requests = state.setdefault("recipe_usage_requests", {})
        requests[key] = {"digest": digest, "result": deepcopy(dict(result)), "at": self._now().isoformat()}
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
            deadline = time.monotonic() + 10
            with self._browser_operation(deadline):
                current = self.store.read()
                if current.get("pending_checkout") or current.get("pending_cancellation") or current.get("order_change"):
                    raise HouseholdError("finish the pending provider operation before recipe discovery")
                response = client.call("recipe_search", arguments, deadline=deadline, allow_recovery=True)
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
                reason = "provider operation is pending" if source in {"oda", "meny", "mathem"} and busy else "source session is unavailable"
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
                "server_version": "5",
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
            checked = RecipeOperations._provider_text(
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
        value = recipe if set(recipe) == {"name", "lifecycle_digest"} else normalize_recipe(recipe)
        return hashlib.sha256(canonical(value).encode()).hexdigest()

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
        allow_incomplete: bool = False,
    ) -> tuple[dict[str, Any], dict[str, str], bool | None]:
        expected = validate_library_recipe_ref(reference)
        try:
            lifecycle_read = getattr(adapter, "get_lifecycle_snapshot", None) if allow_incomplete else None
            raw = (lifecycle_read if callable(lifecycle_read) else adapter.get)({
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
        if callable(lifecycle_read):
            if set(raw) != {"name", "library_recipe_ref", "lifecycle_digest"} or re.fullmatch(r"[a-f0-9]{64}", str(raw.get("lifecycle_digest") or "")) is None:
                raise RecipeLibraryError("invalid lifecycle snapshot")
            recipe = {"name": self._provider_text(raw.get("name"), "recipe name", 300, required=True), "lifecycle_digest": raw["lifecycle_digest"]}
        else:
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
        dispatch_before: str | None = None,
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
            claimed = self.recipes.claim_library_dispatch(operation["operation_id"], dispatch_before=dispatch_before)
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
        dispatch_before: str | None = None,
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
            claimed = self.recipes.claim_library_dispatch(operation["operation_id"], dispatch_before=dispatch_before)
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

    def _import_recovery(self, request):
        self._recover_recipe_library_operations()
        operation = self.recipes.library_operation_snapshot(request.get("operation_id"))
        if operation.get("kind") != "create":
            raise RecipeError("import recovery requires an exact create operation")
        if request.get("deletion_operation_id") is not None:
            return self._library_operation_response(self.recipes.close_removed_library_import(
                operation["operation_id"], request["deletion_operation_id"]
            ))
        response = self._library_operation_response(operation)
        if operation["status"] != "uncertain":
            return response
        progress = operation.get("provider_progress", {})
        adapter = self.recipe_library_adapters.get(operation["library_id"])
        inspect = getattr(adapter, "inspect_incomplete_create", None)
        if not callable(inspect) or not progress:
            response["recovery"] = {"status": "unresolved", "next_action": "Reconcile this same save; never repeat an uncertain create."}
            return response
        principal, binding = self._recipe_library_context(operation["library_id"], adapter)
        if progress.get("provider_principal") != principal or progress.get("provider_binding") != binding:
            raise RecipeError("import recovery provider context changed")
        found = inspect(self._outbound_library_snapshot(operation["snapshot"]), self._outbound_library_operation(operation), progress)
        if found and found.get("complete"):
            reference, recipe = self._validated_library_create_result(found, operation["snapshot"], operation["library_id"])
            response = self._library_operation_response(self.recipes.finish_library_create(operation["operation_id"], "confirmed", library_recipe_ref=reference))
            response["recipe"] = recipe
        elif found:
            self.recipes.record_library_create_progress(operation["operation_id"], {**progress, "slug": found["slug"], "library_recipe_ref": found["library_recipe_ref"]})
            response["recovery"] = {"status": "incomplete_import", "library_recipe_ref": found["library_recipe_ref"],
                "next_action": "Review this exact empty import using delete_prepare; after explicit delete_confirm succeeds, close recovery with its deletion_operation_id. A new save then requires a new idempotency key."}
        else:
            response["recovery"] = {"status": "unresolved", "next_action": "The recorded import was changed or cannot be identified; inspect the library and reconcile, without overwriting or recreating it."}
        return response

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
                    progress = current.get("provider_progress") or {}
                    if progress:
                        return self._import_recovery({"operation_id": current["operation_id"]})
                    reconciled = adapter.reconcile_create(
                        self._outbound_library_snapshot(current["snapshot"]),
                        self._outbound_library_operation(current),
                    )
                    if reconciled is None:
                        return self._import_recovery({"operation_id": current["operation_id"]})
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
        create_context = None
        if target != "builtin" and callable(getattr(adapter, "create_with_progress", None)):
            try:
                create_context = self._recipe_library_context(target, adapter)
            except RecipeLibraryError:
                response = self._library_operation_response(operation)
                response.update({"error_code": "adapter_unavailable", "error": "Authenticate the exact library before create dispatch."})
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
            if callable(getattr(adapter, "create_with_progress", None)):
                principal, binding = create_context
                self.recipes.record_library_create_progress(current["operation_id"], {"provider_principal": principal, "provider_binding": binding})
                created = adapter.create_with_progress(
                    self._outbound_library_snapshot(current["snapshot"]),
                    self._outbound_library_operation(current),
                    lambda progress: self.recipes.record_library_create_progress(
                        current["operation_id"], {**progress, "provider_principal": principal, "provider_binding": binding}
                    ),
                )
            else:
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
                    allow_incomplete=kind == "delete",
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
                    allow_incomplete=kind == "delete",
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
                dispatch_before=request.get("_migration_expires_at"),
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
                    dispatch_before=request.get("_migration_expires_at"),
                    idempotency_key=request.get("idempotency_key"),
                )
            return self._set_external_favorite(
                reference,
                request.get("is_favorite"),
                dispatch_before=request.get("_migration_expires_at"),
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
        if action == "import_recovery":
            return self._import_recovery(request)
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
            if request.get("slot_id") is not None:
                return self._mark_slot(request)
            snapshot = self.store.read()
            current = snapshot.get("menu")
            exact_record = snapshot.get("recipe_usage", {}).get(request.get("menu_id"), {})
            if exact_record.get("slots") or (isinstance(current, Mapping) and current.get("slots") and (not request.get("menu_id") or request.get("menu_id") == current.get("menu_id"))):
                raise HouseholdError("structured cooking requires exact menu revision and slot_id")
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
