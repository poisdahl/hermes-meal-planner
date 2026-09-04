"""Mealie personal recipe-library adapter verified against Mealie v3.24.0."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from recipe_libraries import (
    CAPABILITY_NAMES,
    RecipeLibraryAdapter,
    RecipeLibraryDefiniteError,
    RecipeLibraryError,
    RecipeLibraryExternalMissingError,
    RecipeLibraryUncertainError,
    normalize_label_name,
    normalize_library_origin,
    reject_authenticated_redirect,
    require_authenticated_origin,
    validate_library_id,
    validate_library_recipe_ref,
)
from recipes import RecipeError, normalize_recipe, normalize_source_url


MINIMUM_MEALIE_VERSION = (3, 24, 0)
MINIMUM_MEALIE_VERSION_TEXT = ".".join(str(part) for part in MINIMUM_MEALIE_VERSION)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EXTRA_BYTES = 512 * 1024
MAX_SEARCH_PAGE = 1_000_000
MAX_LABELS = 1_000
ORIGIN_EXTRA = "hermes_origin"
RECIPE_EXTRA = "hermes_recipe"
MARKER_EXTRA = "hermes_operation_marker"
ATTRIBUTION_HEADING = "Hermes attribution"


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class _MealieHTTPStatus(Exception):
    def __init__(self, status: int):
        self.status = status


class _MealieTransportFailure(Exception):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: Any, field: str, maximum: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise RecipeLibraryError(f"Mealie {field} is invalid")
    result = value.strip()
    if required and not result:
        raise RecipeLibraryError(f"Mealie {field} is invalid")
    if len(result) > maximum or any(ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF for character in result):
        raise RecipeLibraryError(f"Mealie {field} is invalid")
    return result or None


def _provider_id(value: Any) -> str:
    text = _text(value, "recipe id", 64)
    try:
        parsed = UUID(text or "")
    except (ValueError, AttributeError) as exc:
        raise RecipeLibraryError("Mealie recipe id is invalid") from exc
    return str(parsed)


def _version_tuple(value: Any) -> tuple[int, int, int]:
    version = _text(value, "server version", 100)
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version or "")
    if match is None:
        raise RecipeLibraryError("Mealie server version is incompatible")
    return tuple(int(match.group(index)) for index in range(1, 4))  # type: ignore[return-value]


def _marker(operation_id: str) -> str:
    return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()


def _safe_source_url(value: Any) -> str | None:
    try:
        return normalize_source_url(value)
    except RecipeError:
        return None


class MealieAdapter(RecipeLibraryAdapter):
    """One authenticated, connection-scoped Mealie adapter."""

    def __init__(
        self,
        connection: Mapping[str, Any],
        credential: Mapping[str, Any],
        *,
        opener: Any = None,
        timeout: float = 10.0,
    ):
        if connection.get("provider") != "mealie":
            raise RecipeLibraryError("Mealie adapter received the wrong provider")
        self.library_id = validate_library_id(connection.get("library_id"), allow_builtin=False)
        self.base_url = normalize_library_origin(
            connection.get("base_url"),
            allow_insecure_http=connection.get("allow_insecure_http") is True,
        )
        self.read_only = connection.get("read_only", False)
        if not isinstance(self.read_only, bool):
            raise RecipeLibraryError("Mealie read_only setting is invalid")
        if set(credential) != {"token"}:
            raise RecipeLibraryError("Mealie credential file must contain only token")
        token = credential.get("token")
        if (
            not isinstance(token, str)
            or token != token.strip()
            or not 1 <= len(token) <= 8_192
            or any(ord(character) < 33 or ord(character) == 127 for character in token)
        ):
            raise RecipeLibraryError("Mealie credential token is invalid")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < float(timeout) <= 60:
            raise RecipeLibraryError("Mealie request timeout is invalid")
        self._authorization = f"Bearer {token}"
        self._timeout = float(timeout)
        self._opener = opener or build_opener(_NoRedirects())
        self._favorite_read = False
        self._favorite_user_id: str | None = None
        self._label_read = False
        self._label_create = False

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | None = None,
        body: Mapping[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        if not path.startswith("/api/") or "?" in path or "#" in path:
            raise RecipeLibraryError("Mealie API path is invalid")
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        require_authenticated_origin(self.base_url, url)
        payload = None
        headers = {
            "Accept": "application/json",
            "Authorization": self._authorization,
            "User-Agent": "meal-concierge/mealie-v1",
        }
        if body is not None:
            try:
                payload = _canonical(body).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                raise RecipeLibraryError("Mealie request payload is invalid") from exc
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.getcode()
                reject_authenticated_redirect(status)
                if status not in expected:
                    raise _MealieHTTPStatus(status)
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > MAX_RESPONSE_BYTES:
                            raise RecipeLibraryError("Mealie response is too large")
                    except ValueError as exc:
                        raise RecipeLibraryError("Mealie response is invalid") from exc
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RecipeLibraryError("Mealie response is too large")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            reject_authenticated_redirect(status)
            raise _MealieHTTPStatus(status) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise _MealieTransportFailure from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise RecipeLibraryError("Mealie response is invalid") from exc

    @staticmethod
    def _page(value: Any, *, expected_per_page: int) -> tuple[list[Any], int, int, int]:
        if not isinstance(value, Mapping) or not isinstance(value.get("items"), list):
            raise RecipeLibraryError("Mealie pagination response is incompatible")
        items = value["items"]
        page = value.get("page")
        per_page = value.get("perPage")
        total = value.get("total")
        total_pages = value.get("totalPages")
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or isinstance(per_page, bool)
            or not isinstance(per_page, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or isinstance(total_pages, bool)
            or not isinstance(total_pages, int)
            or not 1 <= page <= MAX_SEARCH_PAGE
            or per_page != expected_per_page
            or total < 0
            or not 0 <= total_pages <= MAX_SEARCH_PAGE
            or total_pages != math.ceil(total / per_page)
            or len(items) > per_page
            or len(items) > total
        ):
            raise RecipeLibraryError("Mealie pagination response is incompatible")
        return items, page, total, total_pages

    def capabilities(self) -> Mapping[str, Any]:
        try:
            about = self._request("GET", "/api/app/about")
            if not isinstance(about, Mapping):
                raise RecipeLibraryError("Mealie server probe is incompatible")
            version = _text(about.get("version"), "server version", 100)
            if _version_tuple(version) < MINIMUM_MEALIE_VERSION:
                raise RecipeLibraryError(
                    f"Mealie {MINIMUM_MEALIE_VERSION_TEXT} or newer is required"
                )
            user = self._request("GET", "/api/users/self")
            if not isinstance(user, Mapping):
                raise RecipeLibraryError("Mealie authenticated-user probe is incompatible")
            favorite_user_id = _provider_id(user.get("id"))
            can_organize = user.get("canOrganize")
            if not isinstance(can_organize, bool):
                raise RecipeLibraryError(
                    "Mealie authenticated-user organizer permission is incompatible"
                )
            page = self._request(
                "GET", "/api/recipes", query=[("page", "1"), ("perPage", "1")]
            )
            self._page(page, expected_per_page=1)
            favorite_read = True
            try:
                favorites = self._request("GET", "/api/users/self/favorites")
                self._favorite_ids(favorites)
            except _MealieHTTPStatus as exc:
                if exc.status == 404:
                    favorite_read = False
                else:
                    raise
            label_read = True
            try:
                labels = self._request(
                    "GET",
                    "/api/organizers/tags",
                    query=[("page", "1"), ("perPage", "1")],
                )
                label_items, _page, _total, _pages = self._page(
                    labels, expected_per_page=1
                )
                for item in label_items:
                    self._label(item)
            except _MealieHTTPStatus as exc:
                if exc.status == 404:
                    label_read = False
                else:
                    raise
        except RecipeLibraryError:
            raise
        except _MealieHTTPStatus as exc:
            if exc.status in {401, 403}:
                raise RecipeLibraryError("Mealie authentication failed") from None
            if exc.status == 429:
                raise RecipeLibraryError("Mealie capability probe was rate limited") from None
            raise RecipeLibraryError("Mealie capability probe failed") from None
        except _MealieTransportFailure:
            raise RecipeLibraryError("Mealie capability probe is unavailable") from None
        capabilities = {
            name: name in {
                "search", "get", "create_from_discovery", "delete",
                "reconcile_create", "reconcile_delete",
            }
            for name in CAPABILITY_NAMES
        }
        capabilities["favorite_read"] = favorite_read
        capabilities["favorite_write_desired_state"] = favorite_read and not self.read_only
        capabilities["favorite_reconcile"] = favorite_read
        capabilities["label_read"] = label_read
        capabilities["label_create"] = (
            label_read and can_organize and not self.read_only
        )
        if self.read_only:
            capabilities["create_from_discovery"] = False
            capabilities["delete"] = False
            capabilities["reconcile_create"] = False
        self._favorite_read = favorite_read
        self._favorite_user_id = favorite_user_id
        self._label_read = label_read
        self._label_create = capabilities["label_create"]
        return {
            "provider": "mealie",
            "server_version": version,
            "read_only": self.read_only,
            **capabilities,
        }

    @staticmethod
    def _cursor(value: str | None) -> int:
        if value is None:
            return 1
        if not isinstance(value, str):
            raise RecipeLibraryError("Mealie cursor is invalid")
        match = re.fullmatch(r"page:([1-9]\d{0,6})", value)
        if match is None:
            raise RecipeLibraryError("Mealie cursor is invalid")
        page = int(match.group(1))
        if page > MAX_SEARCH_PAGE:
            raise RecipeLibraryError("Mealie cursor is invalid")
        return page

    @staticmethod
    def _search_query(
        query: str, filters: Mapping[str, Any], page: int, limit: int
    ) -> list[tuple[str, str]]:
        if not isinstance(query, str) or len(query) > 200:
            raise RecipeLibraryError("Mealie search query is invalid")
        if not isinstance(filters, Mapping) or len(filters) > 20:
            raise RecipeLibraryError("Mealie search filters are invalid")
        allowed = {
            "categories", "tags", "tools", "foods", "households",
            "require_all_categories", "require_all_tags", "require_all_tools", "require_all_foods",
        }
        if set(filters) - allowed:
            raise RecipeLibraryError("Mealie search filter is unsupported")
        result = [("page", str(page)), ("perPage", str(limit))]
        if query:
            result.append(("search", query))
        for name in ("categories", "tags", "tools", "foods", "households"):
            values = filters.get(name)
            if values is None:
                continue
            if not isinstance(values, list) or not 1 <= len(values) <= 20:
                raise RecipeLibraryError("Mealie search filter is invalid")
            for value in values:
                checked = _text(value, f"search filter {name}", 100)
                result.append((name, checked or ""))
        for name in (
            "require_all_categories", "require_all_tags", "require_all_tools", "require_all_foods",
        ):
            value = filters.get(name)
            if value is None:
                continue
            if not isinstance(value, bool):
                raise RecipeLibraryError("Mealie search filter is invalid")
            camel = "requireAll" + "".join(part.title() for part in name.removeprefix("require_all_").split("_"))
            result.append((camel, str(value).lower()))
        return result

    def _reference(self, value: Mapping[str, Any]) -> dict[str, str]:
        reference = {"library_id": self.library_id, "recipe_id": _provider_id(value.get("id"))}
        if value.get("updatedAt") is not None:
            reference["version"] = _text(value.get("updatedAt"), "recipe version", 100) or ""
        return reference

    @staticmethod
    def _tags(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 50:
            raise RecipeLibraryError("Mealie recipe tags are incompatible")
        result = []
        for item in value:
            if not isinstance(item, Mapping):
                raise RecipeLibraryError("Mealie recipe tags are incompatible")
            result.append(_text(item.get("name"), "recipe tag", 80) or "")
        return result

    def _label(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RecipeLibraryError("Mealie recipe tag is incompatible")
        label_id = _provider_id(value.get("id"))
        name, normalized_name = normalize_label_name(value.get("name"))
        return {
            "library_id": self.library_id,
            "library_label_ref": {
                "library_id": self.library_id,
                "label_id": label_id,
            },
            "name": name,
            "normalized_name": normalized_name,
        }

    def _labels(self, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > MAX_LABELS:
            raise RecipeLibraryError("Mealie recipe tags are incompatible")
        result = [self._label(item) for item in value]
        identities = [item["library_label_ref"]["label_id"] for item in result]
        if len(identities) != len(set(identities)):
            raise RecipeLibraryError("Mealie recipe tags are incompatible")
        return result

    def _summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RecipeLibraryError("Mealie recipe summary is incompatible")
        name = _text(value.get("name"), "recipe name", 300)
        source: dict[str, Any] = {
            "kind": "mealie",
            "publisher": "Mealie",
            "title": name,
            "relationship": "user_supplied",
        }
        if (source_url := _safe_source_url(value.get("orgURL"))) is not None:
            source["url"] = source_url
        result = {
            "name": name,
            "tags": self._tags(value.get("tags")),
            "source": source,
            "library_recipe_ref": self._reference(value),
        }
        if value.get("slug") is not None:
            result["provider_slug"] = _text(value.get("slug"), "recipe slug", 300)
        return result

    @staticmethod
    def _favorite_ids(value: Any) -> set[str]:
        if not isinstance(value, Mapping) or not isinstance(value.get("ratings"), list):
            raise RecipeLibraryError("Mealie favorite response is incompatible")
        result: set[str] = set()
        for rating in value["ratings"]:
            if not isinstance(rating, Mapping) or rating.get("isFavorite") is not True:
                raise RecipeLibraryError("Mealie favorite response is incompatible")
            recipe_id = _provider_id(rating.get("recipeId"))
            if recipe_id in result:
                raise RecipeLibraryError("Mealie favorite response is incompatible")
            result.add(recipe_id)
        return result

    @staticmethod
    def _favorite_rating(value: Any, recipe_id: str) -> bool:
        if (
            not isinstance(value, Mapping)
            or _provider_id(value.get("recipeId")) != recipe_id
            or not isinstance(value.get("isFavorite"), bool)
        ):
            raise RecipeLibraryError("Mealie favorite response is incompatible")
        return value["isFavorite"]

    def _exact_favorite_state(self, recipe_id: str, *, recipe_exists: bool) -> bool:
        if not self._favorite_read:
            raise RecipeLibraryDefiniteError("Mealie favorite reads are unsupported")
        if not recipe_exists:
            self._get_raw(recipe_id)
        try:
            rating = self._request(
                "GET", f"/api/users/self/ratings/{quote(recipe_id, safe='')}"
            )
        except _MealieHTTPStatus as exc:
            if exc.status == 404:
                # The rating endpoint uses the same 404 for "unrated" and for a
                # recipe deleted between the exact recipe read and this call.
                # Recheck existence before treating the state as native false.
                self._get_raw(recipe_id)
                return False
            if exc.status in {401, 403}:
                raise RecipeLibraryError("Mealie favorite read needs_auth") from None
            if exc.status == 429:
                raise RecipeLibraryError("Mealie favorite read was rate limited") from None
            raise RecipeLibraryError("Mealie favorite read failed") from None
        except _MealieTransportFailure:
            raise RecipeLibraryError("Mealie favorite read is unavailable") from None
        return self._favorite_rating(rating, recipe_id)

    def search(
        self, query: str, filters: Mapping[str, Any], cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise RecipeLibraryError("Mealie search limit is invalid")
        if not isinstance(filters, Mapping):
            raise RecipeLibraryError("Mealie search filters are invalid")
        favorites_only = filters.get("favorites_only", False)
        if not isinstance(favorites_only, bool):
            raise RecipeLibraryError("Mealie favorites_only filter is invalid")
        if favorites_only and not self._favorite_read:
            raise RecipeLibraryDefiniteError("Mealie favorite reads are unsupported")
        provider_filters = dict(filters)
        provider_filters.pop("favorites_only", None)
        page_number = self._cursor(cursor)
        parameters = self._search_query(query, provider_filters, page_number, limit)
        try:
            raw = self._request("GET", "/api/recipes", query=parameters)
            items, returned_page, _total, total_pages = self._page(
                raw, expected_per_page=limit
            )
            if returned_page != page_number:
                raise RecipeLibraryError("Mealie pagination response is incompatible")
            recipes = [self._summary(item) for item in items]
            if self._favorite_read:
                favorite_ids = self._favorite_ids(
                    self._request("GET", "/api/users/self/favorites")
                )
                for recipe in recipes:
                    recipe["is_favorite"] = (
                        recipe["library_recipe_ref"]["recipe_id"] in favorite_ids
                    )
                if favorites_only:
                    recipes = [recipe for recipe in recipes if recipe["is_favorite"]]
        except RecipeLibraryError:
            raise
        except _MealieHTTPStatus:
            raise RecipeLibraryError("Mealie recipe search failed") from None
        except _MealieTransportFailure:
            raise RecipeLibraryError("Mealie recipe search is unavailable") from None
        next_cursor = f"page:{page_number + 1}" if page_number < total_pages else None
        return {"recipes": recipes, "cursor": next_cursor}

    @staticmethod
    def _origin(snapshot: Mapping[str, Any], operation: Mapping[str, Any]) -> dict[str, Any]:
        origin = {
            "operation_id": _text(operation.get("operation_id"), "operation id", 80),
            "library_id": validate_library_id(operation.get("library_id"), allow_builtin=False),
            "snapshot_digest": _text(operation.get("snapshot_digest"), "snapshot digest", 64),
            "source_identity": _text(operation.get("source_identity"), "source identity", 2_048),
        }
        if (
            re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", origin["operation_id"] or "") is None
            or re.fullmatch(r"[a-f0-9]{64}", origin["snapshot_digest"] or "") is None
        ):
            raise RecipeLibraryError("Mealie create operation is invalid")
        return origin

    @staticmethod
    def _attribution(snapshot: Mapping[str, Any], marker: str) -> str:
        source = snapshot.get("source") if isinstance(snapshot.get("source"), Mapping) else {}
        rights = snapshot.get("rights") if isinstance(snapshot.get("rights"), Mapping) else {}
        lines = [ATTRIBUTION_HEADING]
        for label, value in (
            ("Source", source.get("title") or snapshot.get("name")),
            ("Author", source.get("author")),
            ("Publisher", source.get("publisher")),
            ("Relationship", source.get("relationship")),
            ("Credit", rights.get("credit")),
            ("License", rights.get("license")),
            ("License URL", rights.get("license_url")),
            ("Source URL", source.get("url")),
        ):
            if value is not None:
                lines.append(f"{label}: {value}")
        lines.append(f"Hermes reference: {marker}")
        return "\n".join(lines)

    def _native_payload(
        self, snapshot: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        document = normalize_recipe(snapshot)
        origin = self._origin(document, operation)
        if origin["library_id"] != self.library_id:
            raise RecipeLibraryError("Mealie create operation names the wrong library")
        marker = _marker(origin["operation_id"] or "")
        attribution = self._attribution(document, marker)
        link_only = document["rights"]["storage"] == "link_only"
        stored_document = document
        if link_only:
            stored_document = {
                "name": document["name"],
                "source": {
                    "kind": "link",
                    "publisher": document["source"]["publisher"],
                    "title": document["source"]["title"],
                    "author": document["source"]["author"],
                    "url": document["source"]["url"],
                    "relationship": document["source"]["relationship"],
                },
                "rights": deepcopy(document["rights"]),
            }
            document = normalize_recipe(stored_document)
        ingredients = [] if link_only else [
            {
                "quantity": None,
                "unit": None,
                "food": None,
                "note": ingredient["raw"],
                "display": ingredient["raw"],
                "originalText": ingredient["raw"],
            }
            for ingredient in document["ingredients"]
        ]
        instructions = [] if link_only else [{"text": step} for step in document["steps"]]
        notes = []
        if not link_only and document.get("notes"):
            notes.append({"title": "Hermes notes", "text": document["notes"]})
        extras = {
            ORIGIN_EXTRA: _canonical(origin),
            RECIPE_EXTRA: _canonical(stored_document),
            MARKER_EXTRA: marker,
        }
        if len(_canonical(extras).encode("utf-8")) > MAX_EXTRA_BYTES:
            raise RecipeLibraryError("Mealie recipe extras are too large")
        payload: dict[str, Any] = {
            "name": document["name"],
            "description": attribution,
            "orgURL": document["source"].get("url"),
            "tags": [],
            "recipeServings": 0 if link_only or document.get("portions") is None else document["portions"],
            "recipeYieldQuantity": 0 if link_only or document.get("portions") is None else document["portions"],
            "recipeYield": None if link_only or document.get("portions") is None else "servings",
            "recipeIngredient": ingredients,
            "recipeInstructions": instructions,
            "notes": notes,
            "extras": extras,
        }
        return payload, document

    @staticmethod
    def _extra_object(raw: Mapping[str, Any], name: str) -> Any:
        extras = raw.get("extras")
        if not isinstance(extras, Mapping):
            return None
        value = extras.get(name)
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_EXTRA_BYTES:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            return None

    @staticmethod
    def _native_texts(value: Any, field: str) -> list[str]:
        if not isinstance(value, list) or len(value) > 500:
            raise RecipeLibraryError(f"Mealie {field} is incompatible")
        result = []
        for item in value:
            if not isinstance(item, Mapping):
                raise RecipeLibraryError(f"Mealie {field} is incompatible")
            candidate = item.get("originalText") or item.get("display") or item.get("note") if field == "ingredients" else item.get("text")
            result.append(_text(candidate, f"recipe {field}", 4_000) or "")
        return result

    def _native_matches(
        self, raw: Mapping[str, Any], payload: Mapping[str, Any], document: Mapping[str, Any]
    ) -> bool:
        try:
            expected_ingredients = [item["originalText"] for item in payload["recipeIngredient"]]
            expected_steps = [item["text"] for item in payload["recipeInstructions"]]
            returned_notes = raw.get("notes")
            if not isinstance(returned_notes, list):
                return False
            if any(not isinstance(item, Mapping) for item in returned_notes):
                return False
            notes = [
                {"title": item.get("title"), "text": item.get("text")}
                for item in returned_notes
            ]
            returned_servings = raw.get("recipeServings")
            expected_servings = payload.get("recipeServings")
            returned_yield_quantity = raw.get("recipeYieldQuantity")
            expected_yield_quantity = payload.get("recipeYieldQuantity")
            return (
                raw.get("name") == payload.get("name")
                and raw.get("description") == payload.get("description")
                and raw.get("orgURL") == payload.get("orgURL")
                and sorted(self._tags(raw.get("tags"))) == sorted(payload.get("tags", []))
                and isinstance(returned_servings, (int, float))
                and not isinstance(returned_servings, bool)
                and isinstance(expected_servings, (int, float))
                and not isinstance(expected_servings, bool)
                and returned_servings == expected_servings
                and isinstance(returned_yield_quantity, (int, float))
                and not isinstance(returned_yield_quantity, bool)
                and isinstance(expected_yield_quantity, (int, float))
                and not isinstance(expected_yield_quantity, bool)
                and returned_yield_quantity == expected_yield_quantity
                and raw.get("recipeYield") == payload.get("recipeYield")
                and self._native_texts(raw.get("recipeIngredient"), "ingredients") == expected_ingredients
                and self._native_texts(raw.get("recipeInstructions"), "steps") == expected_steps
                and notes == payload.get("notes")
                and self._extra_object(raw, ORIGIN_EXTRA) == self._extra_object(payload, ORIGIN_EXTRA)
                and normalize_recipe(self._extra_object(raw, RECIPE_EXTRA)) == document
                and isinstance(raw.get("extras"), Mapping)
                and raw["extras"].get(MARKER_EXTRA) == payload["extras"].get(MARKER_EXTRA)
            )
        except (RecipeError, RecipeLibraryError, TypeError, ValueError):
            return False

    def _mapped_recipe(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        stored = self._extra_object(raw, RECIPE_EXTRA)
        origin = self._extra_object(raw, ORIGIN_EXTRA)
        if stored is not None or origin is not None:
            if not isinstance(stored, Mapping) or not isinstance(origin, Mapping):
                raise RecipeLibraryError("Mealie Hermes metadata is incompatible")
            try:
                document = normalize_recipe(stored)
                payload, _ = self._native_payload(document, origin)
            except (RecipeError, RecipeLibraryError) as exc:
                raise RecipeLibraryError("Mealie Hermes metadata is incompatible") from exc
            if self._native_matches(raw, payload, document):
                return document
            source = deepcopy(document["source"])
            rights = deepcopy(document["rights"])
        else:
            name = _text(raw.get("name"), "recipe name", 300)
            source = {
                "kind": "mealie", "publisher": "Mealie", "title": name,
                "url": _safe_source_url(raw.get("orgURL")), "external_id": _provider_id(raw.get("id")),
                "relationship": "user_supplied",
            }
            rights = {"storage": "full", "license": None, "license_url": None, "credit": None}
        name = _text(raw.get("name"), "recipe name", 300)
        if rights.get("storage") == "link_only":
            candidate = {
                "name": name,
                "language": document.get("language", "nb-NO") if isinstance(stored, Mapping) else "nb-NO",
                "tags": deepcopy(document["tags"]) if isinstance(stored, Mapping) else self._tags(raw.get("tags")),
                "source": source, "rights": rights,
            }
        else:
            servings = raw.get("recipeServings")
            if (
                not isinstance(servings, (int, float))
                or isinstance(servings, bool)
                or not math.isfinite(float(servings))
                or servings < 0
            ):
                raise RecipeLibraryError("Mealie recipe servings are incompatible")
            portions = float(servings) if servings > 0 else None
            ingredients = self._native_texts(raw.get("recipeIngredient"), "ingredients")
            steps = self._native_texts(raw.get("recipeInstructions"), "steps")
            note_values = []
            notes = raw.get("notes")
            if not isinstance(notes, list) or any(not isinstance(note, Mapping) for note in notes):
                raise RecipeLibraryError("Mealie recipe notes are incompatible")
            for note in notes:
                if note.get("text"):
                    note_values.append(_text(note.get("text"), "recipe note", 4_000) or "")
            native_times = {
                key: raw.get(field)
                for key, field in (
                    ("total", "totalTime"), ("prep", "prepTime"),
                    ("cook", "cookTime"), ("perform", "performTime"),
                )
                if raw.get(field) is not None
            } or None
            candidate = {
                "name": name,
                "language": document.get("language", "nb-NO") if isinstance(stored, Mapping) else "nb-NO",
                "tags": deepcopy(document["tags"]) if isinstance(stored, Mapping) else self._tags(raw.get("tags")),
                "source": source,
                "rights": rights,
                "portions": portions,
                "ingredients": ingredients,
                "steps": steps,
                "notes": "\n\n".join(note_values) or None,
                "times": native_times if native_times is not None else (
                    deepcopy(document.get("times")) if isinstance(stored, Mapping) else None
                ),
            }
            if isinstance(stored, Mapping):
                for field in ("external_snapshot", "storage", "reheating"):
                    if field in document:
                        candidate[field] = deepcopy(document[field])
        try:
            return normalize_recipe(candidate)
        except RecipeError as exc:
            raise RecipeLibraryError("Mealie recipe content is incompatible") from exc

    def _get_raw(self, recipe_id: str) -> Mapping[str, Any]:
        try:
            raw = self._request("GET", f"/api/recipes/{quote(recipe_id, safe='')}")
        except _MealieHTTPStatus as exc:
            if exc.status == 404:
                raise RecipeLibraryExternalMissingError(
                    "Mealie exact recipe is externally missing"
                ) from None
            if exc.status in {401, 403}:
                raise RecipeLibraryError("Mealie exact recipe get needs_auth") from None
            if exc.status == 429:
                raise RecipeLibraryError("Mealie exact recipe get was rate limited") from None
            raise RecipeLibraryError("Mealie exact recipe get failed") from None
        except _MealieTransportFailure:
            raise RecipeLibraryError("Mealie exact recipe get is unavailable") from None
        if not isinstance(raw, Mapping) or _provider_id(raw.get("id")) != recipe_id:
            raise RecipeLibraryError("Mealie exact recipe response is incompatible")
        return raw

    def _result(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        document = self._mapped_recipe(raw)
        result = {**document, "library_recipe_ref": self._reference(raw)}
        if raw.get("slug") is not None:
            result["provider_slug"] = _text(raw.get("slug"), "recipe slug", 300)
        return result

    def _create_result(
        self, raw: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        remote = self._result(raw)
        frozen = normalize_recipe(snapshot)
        if frozen["rights"]["storage"] != "link_only":
            return remote
        frozen = normalize_recipe({
            key: deepcopy(frozen[key])
            for key in (
                "schema_version", "name", "language", "tags", "source", "rights",
                "external_snapshot",
            )
            if key in frozen
        })
        result = {**frozen, "library_recipe_ref": remote["library_recipe_ref"]}
        if remote.get("provider_slug") is not None:
            result["provider_slug"] = remote["provider_slug"]
        return result

    def get(self, library_recipe_ref: Mapping[str, str]) -> Mapping[str, Any]:
        reference = validate_library_recipe_ref(library_recipe_ref)
        if reference["library_id"] != self.library_id:
            raise RecipeLibraryError("Mealie recipe reference names the wrong library")
        recipe_id = _provider_id(reference["recipe_id"])
        raw = self._get_raw(recipe_id)
        result = self._result(raw)
        if self._favorite_read:
            result["is_favorite"] = self._exact_favorite_state(
                recipe_id, recipe_exists=True
            )
        return result

    def get_lifecycle_snapshot(self, reference):
        """Deletion can inspect incomplete native records without inventing recipe content."""
        reference = validate_library_recipe_ref(reference)
        if reference["library_id"] != self.library_id:
            raise RecipeLibraryError("Mealie lifecycle reference names the wrong library")
        raw = self._get_raw(_provider_id(reference["recipe_id"]))
        return {"name": _text(raw.get("name"), "recipe name", 300),
                "library_recipe_ref": self._reference(raw),
                "lifecycle_digest": hashlib.sha256(_canonical(raw).encode()).hexdigest()}

    def delete_recipe(
        self,
        library_recipe_ref: Mapping[str, str],
        operation: Mapping[str, Any],
    ) -> None:
        reference = validate_library_recipe_ref(library_recipe_ref)
        if (
            reference["library_id"] != self.library_id
            or operation.get("kind") != "delete"
            or operation.get("library_id") != self.library_id
            or operation.get("target_recipe_id") != reference["recipe_id"]
        ):
            raise RecipeLibraryDefiniteError("Mealie delete request is invalid")
        if self.read_only:
            raise RecipeLibraryDefiniteError("Mealie connection is read-only")
        recipe_id = _provider_id(reference["recipe_id"])
        try:
            raw = self._request(
                "DELETE", f"/api/recipes/{quote(recipe_id, safe='')}", expected=(200,)
            )
        except _MealieHTTPStatus as exc:
            if exc.status == 404 or exc.status == 408:
                raise RecipeLibraryUncertainError(
                    "Mealie recipe deletion outcome is uncertain"
                ) from None
            if 400 <= exc.status < 500:
                message = (
                    "Mealie recipe deletion needs_auth"
                    if exc.status in {401, 403}
                    else "Mealie rejected recipe deletion"
                )
                raise RecipeLibraryDefiniteError(message) from None
            raise RecipeLibraryUncertainError(
                "Mealie recipe deletion outcome is uncertain"
            ) from None
        except (_MealieTransportFailure, RecipeLibraryError):
            raise RecipeLibraryUncertainError(
                "Mealie recipe deletion outcome is uncertain"
            ) from None
        try:
            if not isinstance(raw, Mapping) or _provider_id(raw.get("id")) != recipe_id:
                raise RecipeLibraryError("Mealie delete response is incompatible")
        except RecipeLibraryError:
            raise RecipeLibraryUncertainError(
                "Mealie recipe deletion outcome is uncertain"
            ) from None

    def authenticated_principal(self) -> str:
        try:
            user = self._request("GET", "/api/users/self")
        except _MealieHTTPStatus as exc:
            if exc.status in {401, 403}:
                raise RecipeLibraryError("Mealie authenticated principal needs_auth") from None
            raise RecipeLibraryError(
                "Mealie authenticated principal is unavailable"
            ) from None
        except _MealieTransportFailure:
            raise RecipeLibraryError(
                "Mealie authenticated principal is unavailable"
            ) from None
        if not isinstance(user, Mapping):
            raise RecipeLibraryError(
                "Mealie authenticated principal response is incompatible"
            )
        return _canonical({
            "group_id": _provider_id(user.get("groupId")),
            "household_id": _provider_id(user.get("householdId")),
            "user_id": _provider_id(user.get("id")),
        })

    def reconcile_delete(
        self,
        library_recipe_ref: Mapping[str, str],
        operation: Mapping[str, Any],
    ) -> bool | None:
        reference = validate_library_recipe_ref(library_recipe_ref)
        if (
            reference["library_id"] != self.library_id
            or operation.get("kind") != "delete"
            or operation.get("library_id") != self.library_id
            or operation.get("target_recipe_id") != reference["recipe_id"]
        ):
            raise RecipeLibraryDefiniteError("Mealie delete reconciliation is invalid")
        recipe_id = _provider_id(reference["recipe_id"])
        try:
            principal = self.authenticated_principal()
            if principal != operation.get("provider_principal"):
                raise RecipeLibraryError(
                    "Mealie delete reconciliation principal changed"
                )
            self._get_raw(recipe_id)
            return False
        except RecipeLibraryExternalMissingError:
            return True
        except _MealieHTTPStatus as exc:
            if exc.status in {401, 403}:
                raise RecipeLibraryError("Mealie delete reconciliation needs_auth") from None
            raise RecipeLibraryError("Mealie delete reconciliation is unavailable") from None
        except _MealieTransportFailure:
            raise RecipeLibraryError("Mealie delete reconciliation is unavailable") from None

    def get_favorite(self, library_recipe_ref: Mapping[str, str]) -> Mapping[str, Any]:
        reference = validate_library_recipe_ref(library_recipe_ref)
        if reference["library_id"] != self.library_id:
            raise RecipeLibraryError("Mealie recipe reference names the wrong library")
        recipe_id = _provider_id(reference["recipe_id"])
        raw = self._get_raw(recipe_id)
        current = self._exact_favorite_state(recipe_id, recipe_exists=True)
        return {
            "library_id": self.library_id,
            "library_recipe_ref": self._reference(raw),
            "is_favorite": current,
        }

    def set_favorite(
        self,
        library_recipe_ref: Mapping[str, str],
        is_favorite: bool,
        *,
        expected_favorite_revision: Any = None,
    ) -> None:
        reference = validate_library_recipe_ref(library_recipe_ref)
        if reference["library_id"] != self.library_id:
            raise RecipeLibraryDefiniteError(
                "Mealie recipe reference names the wrong library"
            )
        if not isinstance(is_favorite, bool):
            raise RecipeLibraryDefiniteError("Mealie favorite state is invalid")
        if expected_favorite_revision is not None:
            raise RecipeLibraryDefiniteError(
                "Mealie does not support conditional favorite mutation"
            )
        if self.read_only:
            raise RecipeLibraryDefiniteError("Mealie connection is read-only")
        if not self._favorite_read or self._favorite_user_id is None:
            raise RecipeLibraryDefiniteError(
                "Mealie favorite mutation capability is unverified"
            )
        recipe_id = _provider_id(reference["recipe_id"])
        path = (
            f"/api/users/{quote(self._favorite_user_id, safe='')}/favorites/"
            f"{quote(recipe_id, safe='')}"
        )
        try:
            self._request(
                "POST" if is_favorite else "DELETE", path, expected=(200, 204)
            )
        except _MealieHTTPStatus as exc:
            if exc.status == 404:
                raise RecipeLibraryExternalMissingError(
                    "Mealie exact recipe is externally missing"
                ) from None
            if 400 <= exc.status < 500 and exc.status != 408:
                message = (
                    "Mealie favorite mutation needs_auth"
                    if exc.status in {401, 403}
                    else "Mealie rejected favorite mutation"
                )
                raise RecipeLibraryDefiniteError(message) from None
            raise RecipeLibraryUncertainError(
                "Mealie favorite mutation outcome is uncertain"
            ) from None
        except (_MealieTransportFailure, RecipeLibraryError):
            raise RecipeLibraryUncertainError(
                "Mealie favorite mutation outcome is uncertain"
            ) from None

    def list_labels(self) -> list[Mapping[str, Any]]:
        if not self._label_read:
            raise RecipeLibraryDefiniteError("Mealie tag reads are unsupported")
        result: list[dict[str, Any]] = []
        page = 1
        try:
            while True:
                raw = self._request(
                    "GET",
                    "/api/organizers/tags",
                    query=[("page", str(page)), ("perPage", "100")],
                )
                items, returned_page, total, total_pages = self._page(
                    raw, expected_per_page=100
                )
                if returned_page != page or total > MAX_LABELS:
                    raise RecipeLibraryError("Mealie tag list is incompatible")
                result.extend(self._labels(items))
                if page >= total_pages:
                    break
                page += 1
        except _MealieHTTPStatus as exc:
            if exc.status in {401, 403}:
                raise RecipeLibraryError("Mealie tag read needs_auth") from None
            raise RecipeLibraryError("Mealie tag read failed") from None
        except _MealieTransportFailure:
            raise RecipeLibraryError("Mealie tag read is unavailable") from None
        identities = [item["library_label_ref"]["label_id"] for item in result]
        if len(result) != total or len(identities) != len(set(identities)):
            raise RecipeLibraryError("Mealie tag list is incompatible")
        return result

    def get_recipe_labels(
        self, library_recipe_ref: Mapping[str, str]
    ) -> list[Mapping[str, Any]]:
        if not self._label_read:
            raise RecipeLibraryDefiniteError("Mealie tag reads are unsupported")
        reference = validate_library_recipe_ref(library_recipe_ref)
        if reference["library_id"] != self.library_id:
            raise RecipeLibraryError("Mealie recipe reference names the wrong library")
        raw = self._get_raw(_provider_id(reference["recipe_id"]))
        return self._labels(raw.get("tags"))

    def create_label(self, name: str, *, idempotency_key: str) -> Mapping[str, Any]:
        if not self._label_create or self.read_only:
            raise RecipeLibraryDefiniteError("Mealie tag creation is unsupported")
        display, normalized_name = normalize_label_name(name)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise RecipeLibraryDefiniteError("Mealie tag idempotency key is invalid")
        try:
            raw = self._request(
                "POST",
                "/api/organizers/tags",
                body={"name": display},
                expected=(201,),
            )
            result = self._label(raw)
            if result["normalized_name"] != normalized_name:
                raise RecipeLibraryUncertainError(
                    "Mealie tag creation response is incompatible"
                )
            return result
        except _MealieHTTPStatus as exc:
            if 400 <= exc.status < 500 and exc.status != 408:
                message = (
                    "Mealie tag creation needs_auth"
                    if exc.status in {401, 403}
                    else "Mealie rejected tag creation"
                )
                raise RecipeLibraryDefiniteError(message) from None
            raise RecipeLibraryUncertainError(
                "Mealie tag creation outcome is uncertain"
            ) from None
        except _MealieTransportFailure:
            raise RecipeLibraryUncertainError(
                "Mealie tag creation outcome is uncertain"
            ) from None

    @staticmethod
    def _definite_initial_failure(exc: Exception) -> bool:
        return isinstance(exc, _MealieHTTPStatus) and 400 <= exc.status < 500 and exc.status != 408

    def create_from_snapshot(
        self, snapshot: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return self.create_with_progress(snapshot, operation, lambda progress: None)

    def create_with_progress(self, snapshot, operation, record_progress):
        if self.read_only:
            raise RecipeLibraryDefiniteError("Mealie connection is read-only")
        try:
            payload, document = self._native_payload(snapshot, operation)
            origin = self._origin(document, operation)
        except (RecipeError, RecipeLibraryError):
            raise RecipeLibraryDefiniteError("Mealie create request is invalid") from None
        stub_name = f"Hermes import {_marker(origin['operation_id'] or '')}"
        try:
            slug = self._request("POST", "/api/recipes", body={"name": stub_name}, expected=(201,))
        except Exception as exc:
            if self._definite_initial_failure(exc):
                raise RecipeLibraryDefiniteError("Mealie rejected recipe creation") from None
            raise RecipeLibraryUncertainError("Mealie recipe creation outcome is uncertain") from None
        try:
            slug = _text(slug, "created recipe slug", 300)
            record_progress({"slug": slug})
            raw_stub = self._request("GET", f"/api/recipes/{quote(slug or '', safe='')}")
            if not isinstance(raw_stub, Mapping):
                raise RecipeLibraryError("Mealie created recipe response is incompatible")
            if (
                _text(raw_stub.get("name"), "created recipe name", 300) != stub_name
                or _text(raw_stub.get("slug"), "created recipe slug", 300) != slug
            ):
                raise RecipeLibraryError("Mealie created recipe response is incompatible")
            recipe_id = _provider_id(raw_stub.get("id"))
            record_progress({"slug": slug, "library_recipe_ref": self._reference(raw_stub)})
            raw = self._request(
                "PATCH", f"/api/recipes/{quote(recipe_id, safe='')}", body=payload
            )
            if not isinstance(raw, Mapping) or _provider_id(raw.get("id")) != recipe_id:
                raise RecipeLibraryError("Mealie created recipe response is incompatible")
            if not self._native_matches(raw, payload, document):
                raise RecipeLibraryError("Mealie created recipe did not preserve the frozen content")
            recipe = self._create_result(raw, snapshot)
        except Exception:
            raise RecipeLibraryUncertainError("Mealie recipe creation outcome is uncertain") from None
        return {"library_recipe_ref": recipe["library_recipe_ref"], "recipe": recipe}

    def inspect_incomplete_create(self, snapshot, operation, progress):
        """Locate only the exact journalled stub. Never overwrite it during recovery."""
        payload, document = self._native_payload(snapshot, operation)
        origin = self._origin(document, operation)
        slug = _text(progress.get("slug"), "created recipe slug", 300, required=False)
        reference = progress.get("library_recipe_ref")
        if not slug:
            marker = _marker(origin["operation_id"] or "")
            page = self._request("GET", "/api/recipes", query=[("page", "1"), ("perPage", "50"), ("search", f'"{marker}"')])
            items, number, total, pages = self._page(page, expected_per_page=50)
            if number != 1 or total != 1 or pages != 1 or len(items) != 1:
                return None
            raw = self._get_raw(_provider_id(items[0].get("id")))
            slug = _text(raw.get("slug"), "created recipe slug", 300)
        else:
            raw = self._get_raw(_provider_id(reference["recipe_id"])) if reference else self._request(
                "GET", f"/api/recipes/{quote(slug, safe='')}"
            )
        if not isinstance(raw, Mapping):
            return None
        if self._native_matches(raw, payload, document):
            recipe = self._create_result(raw, snapshot)
            return {"complete": True, "library_recipe_ref": recipe["library_recipe_ref"], "recipe": recipe}
        stub_name = f"Hermes import {_marker(origin['operation_id'] or '')}"
        if raw.get("slug") != slug or raw.get("name") != stub_name:
            return None
        # An edited stub is ordinary user content; its removal is not proposed.
        for field in ("recipeIngredient", "recipeInstructions", "notes", "tags"):
            if raw.get(field):
                return None
        if raw.get("description") or raw.get("orgURL") or raw.get("extras"):
            return None
        return {"complete": False, "slug": slug, "library_recipe_ref": self._reference(raw)}

    def reconcile_create(
        self, snapshot: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        if self.read_only:
            return None
        try:
            payload, document = self._native_payload(snapshot, operation)
            origin = self._origin(document, operation)
            marker = _marker(origin["operation_id"] or "")
            raw_page = self._request(
                "GET", "/api/recipes",
                query=[("page", "1"), ("perPage", "50"), ("search", f'"{marker}"')],
            )
            items, page, total, total_pages = self._page(
                raw_page, expected_per_page=50
            )
            if page != 1 or total != 1 or total_pages != 1 or len(items) != 1:
                return None
            summary = self._summary(items[0])
            recipe_id = summary["library_recipe_ref"]["recipe_id"]
            raw = self._get_raw(recipe_id)
            if self._extra_object(raw, ORIGIN_EXTRA) != origin:
                return None
            if not self._native_matches(raw, payload, document):
                return None
            recipe = self._create_result(raw, snapshot)
            return {"library_recipe_ref": recipe["library_recipe_ref"], "recipe": recipe}
        except Exception:
            return None


def create_adapter(
    connection: Mapping[str, Any], credential: Mapping[str, Any]
) -> RecipeLibraryAdapter:
    return MealieAdapter(connection, credential)
