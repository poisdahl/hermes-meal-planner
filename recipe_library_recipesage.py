"""RecipeSage personal recipe-library adapter for the verified v4 REST API."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from recipe_libraries import (
    CAPABILITY_NAMES,
    RecipeLibraryAdapter,
    RecipeLibraryDefiniteError,
    RecipeLibraryError,
    RecipeLibraryUncertainError,
    normalize_label_name,
    normalize_library_origin,
    reject_authenticated_redirect,
    require_authenticated_origin,
    validate_library_id,
    validate_library_recipe_ref,
)
from recipes import RecipeError, normalize_recipe, normalize_source_url


MINIMUM_RECIPESAGE_VERSION = (4, 0, 3)
MINIMUM_RECIPESAGE_VERSION_TEXT = ".".join(
    str(part) for part in MINIMUM_RECIPESAGE_VERSION
)
HOSTED_VERIFIED_VERSION = "v4.0.6"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 512 * 1024
MAX_CURSOR_OFFSET = 1_000_000
METADATA_BEGIN = "[[HERMES RECIPE LIBRARY METADATA v1]]"
METADATA_END = "[[END HERMES RECIPE LIBRARY METADATA]]"
ATTRIBUTION_HEADING = "Hermes attribution"

_REQUIRED_OPERATIONS = {
    "/compat/v2/users/validateSession": ("get", "users-validateSession"),
    "/compat/v2/users/getMe": ("get", "users-getMe"),
    "/compat/v2/recipes/getRecipes": ("post", "recipes-getRecipes"),
    "/compat/v2/recipes/searchRecipes": ("post", "recipes-searchRecipes"),
    "/compat/v2/recipes/getRecipe": ("get", "recipes-getRecipe"),
    "/compat/v2/recipes/getRecipesByUrl": ("get", "recipes-getRecipesByUrl"),
    "/compat/v2/recipes/createRecipe": ("post", "recipes-createRecipe"),
    "/compat/v2/labels/getLabels": ("get", "labels-getLabels"),
    "/compat/v2/labels/createLabel": ("post", "labels-createLabel"),
}

_CREATE_REQUIRED_FIELDS = {
    "title",
    "description",
    "yield",
    "activeTime",
    "totalTime",
    "source",
    "url",
    "notes",
    "ingredients",
    "instructions",
    "rating",
    "folder",
    "labelIds",
    "imageIds",
}
_FULL_RECIPE_REQUIRED_FIELDS = {
    "id",
    "userId",
    "title",
    "description",
    "yield",
    "activeTime",
    "totalTime",
    "source",
    "url",
    "folder",
    "ingredients",
    "instructions",
    "notes",
    "updatedAt",
    "rating",
    "recipeLabels",
    "recipeImages",
}
_SUMMARY_RECIPE_REQUIRED_FIELDS = {
    "id",
    "userId",
    "title",
    "url",
    "updatedAt",
    "recipeLabels",
    "recipeImages",
}
_UUID_PATTERN = (
    "^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-"
    "[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}|"
    "00000000-0000-0000-0000-000000000000|"
    "ffffffff-ffff-ffff-ffff-ffffffffffff)$"
)
MAX_LABELS = 1_000


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class _RecipeSageHTTPStatus(Exception):
    def __init__(self, status: int):
        self.status = status


class _RecipeSageTransportFailure(Exception):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _text(value: Any, field: str, maximum: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise RecipeLibraryError(f"RecipeSage {field} is invalid")
    result = value.strip()
    if required and not result:
        raise RecipeLibraryError(f"RecipeSage {field} is invalid")
    if len(result) > maximum or any(
        ord(character) < 32
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in result
    ):
        raise RecipeLibraryError(f"RecipeSage {field} is invalid")
    return result or None


def _body(value: Any, field: str, maximum: int = MAX_RESPONSE_BYTES) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise RecipeLibraryError(f"RecipeSage {field} is invalid")
    if any(
        (ord(character) < 32 and character not in "\n\r\t")
        or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        raise RecipeLibraryError(f"RecipeSage {field} is invalid")
    return value


def _provider_id(value: Any, field: str = "recipe id") -> str:
    text = _text(value, field, 64)
    try:
        parsed = UUID(text or "")
    except (ValueError, AttributeError) as exc:
        raise RecipeLibraryError(f"RecipeSage {field} is invalid") from exc
    return str(parsed)


def _version_tuple(value: Any) -> tuple[int, int, int]:
    version = _text(value, "server version", 100)
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version or "")
    if match is None:
        raise RecipeLibraryError("RecipeSage server version is incompatible")
    return tuple(int(match.group(index)) for index in range(1, 4))  # type: ignore[return-value]


def _marker(operation_id: str) -> str:
    return hashlib.sha256(operation_id.encode("utf-8")).hexdigest()


def _safe_source_url(value: Any) -> str | None:
    try:
        return normalize_source_url(value)
    except RecipeError:
        return None


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else format(value, ".15g")


class RecipeSageAdapter(RecipeLibraryAdapter):
    """One bearer-authenticated, connection-scoped RecipeSage adapter."""

    def __init__(
        self,
        connection: Mapping[str, Any],
        credential: Mapping[str, Any],
        *,
        opener: Any = None,
        timeout: float = 10.0,
    ):
        if connection.get("provider") != "recipesage":
            raise RecipeLibraryError("RecipeSage adapter received the wrong provider")
        self.library_id = validate_library_id(
            connection.get("library_id"), allow_builtin=False
        )
        self.base_url = normalize_library_origin(
            connection.get("base_url"),
            allow_insecure_http=connection.get("allow_insecure_http") is True,
        )
        self.read_only = connection.get("read_only", False)
        if not isinstance(self.read_only, bool):
            raise RecipeLibraryError("RecipeSage read_only setting is invalid")
        if set(credential) != {"token"}:
            raise RecipeLibraryError(
                "RecipeSage credential file must contain only token"
            )
        token = credential.get("token")
        if (
            not isinstance(token, str)
            or token != token.strip()
            or not 1 <= len(token) <= 8_192
            or any(ord(character) < 33 or ord(character) == 127 for character in token)
        ):
            raise RecipeLibraryError("RecipeSage session token is invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < float(timeout) <= 60
        ):
            raise RecipeLibraryError("RecipeSage request timeout is invalid")
        self._authorization = f"Bearer {token}"
        self._timeout = float(timeout)
        self._opener = opener or build_opener(_NoRedirects())
        self._api_prefix: str | None = None
        self._server_version: str | None = None
        self._user_id: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: list[tuple[str, str]] | None = None,
        body: Mapping[str, Any] | None = None,
        authenticated: bool = True,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        if not path.startswith("/") or "?" in path or "#" in path or "\\" in path:
            raise RecipeLibraryError("RecipeSage API path is invalid")
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        require_authenticated_origin(self.base_url, url)
        payload = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "meal-concierge/recipesage-v1",
        }
        if authenticated:
            headers["Authorization"] = self._authorization
        if body is not None:
            try:
                payload = _canonical(body).encode("utf-8")
            except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
                raise RecipeLibraryError("RecipeSage request payload is invalid") from exc
            headers["Content-Type"] = "application/json"
        request = Request(url, data=payload, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.getcode()
                reject_authenticated_redirect(status)
                if status not in expected:
                    raise _RecipeSageHTTPStatus(status)
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > MAX_RESPONSE_BYTES:
                            raise RecipeLibraryError("RecipeSage response is too large")
                    except ValueError as exc:
                        raise RecipeLibraryError("RecipeSage response is invalid") from exc
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RecipeLibraryError("RecipeSage response is too large")
        except HTTPError as exc:
            status = exc.code
            exc.close()
            reject_authenticated_redirect(status)
            raise _RecipeSageHTTPStatus(status) from None
        except (URLError, TimeoutError, OSError) as exc:
            raise _RecipeSageTransportFailure from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise RecipeLibraryError("RecipeSage response is invalid") from exc

    @staticmethod
    def _json_schema(
        value: Any,
        *,
        schema_type: str,
        required: set[str] | None = None,
        exact_required: bool = False,
        properties: Mapping[str, tuple[str, str | None]] | None = None,
    ) -> Mapping[str, Any]:
        if not isinstance(value, Mapping) or value.get("type") != schema_type:
            raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
        if required is not None:
            raw_required = value.get("required")
            if (
                not isinstance(raw_required, list)
                or not required.issubset(raw_required)
                or (exact_required and set(raw_required) != required)
            ):
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
        if properties is not None:
            raw_properties = value.get("properties")
            if not isinstance(raw_properties, Mapping):
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            for name, (expected_type, expected_format) in properties.items():
                field = raw_properties.get(name)
                if (
                    not isinstance(field, Mapping)
                    or field.get("type") != expected_type
                    or (
                        expected_format is not None
                        and field.get("format") != expected_format
                    )
                ):
                    raise RecipeLibraryError(
                        "RecipeSage OpenAPI contract is incompatible"
                    )
        return value

    @classmethod
    def _request_schema(cls, operation: Mapping[str, Any]) -> Mapping[str, Any]:
        request = operation.get("requestBody")
        content = request.get("content") if isinstance(request, Mapping) else None
        media = content.get("application/json") if isinstance(content, Mapping) else None
        return media.get("schema") if isinstance(media, Mapping) else {}

    @classmethod
    def _response_schema(cls, operation: Mapping[str, Any]) -> Mapping[str, Any]:
        responses = operation.get("responses")
        response = responses.get("200") if isinstance(responses, Mapping) else None
        content = response.get("content") if isinstance(response, Mapping) else None
        media = content.get("application/json") if isinstance(content, Mapping) else None
        return media.get("schema") if isinstance(media, Mapping) else {}

    @classmethod
    def _recipe_schema(
        cls, value: Any, *, full: bool
    ) -> None:
        properties = {
            "id": ("string", "uuid"),
            "userId": ("string", "uuid"),
            "title": ("string", None),
            "url": ("string", None),
            "updatedAt": ("string", None),
            "recipeLabels": ("array", None),
            "recipeImages": ("array", None),
        }
        if full:
            properties.update(
                {
                    "description": ("string", None),
                    "yield": ("string", None),
                    "activeTime": ("string", None),
                    "totalTime": ("string", None),
                    "source": ("string", None),
                    "folder": ("string", None),
                    "ingredients": ("string", None),
                    "instructions": ("string", None),
                    "notes": ("string", None),
                }
            )
        schema = cls._json_schema(
            value,
            schema_type="object",
            required=(
                _FULL_RECIPE_REQUIRED_FIELDS
                if full
                else _SUMMARY_RECIPE_REQUIRED_FIELDS
            ),
            properties=properties,
        )
        if full:
            recipe_labels = schema["properties"]["recipeLabels"]
            cls._recipe_label_schema(recipe_labels.get("items"))

    @classmethod
    def _label_schema(cls, value: Any) -> None:
        cls._json_schema(
            value,
            schema_type="object",
            required={"id", "userId", "title", "updatedAt"},
            properties={
                "id": ("string", "uuid"),
                "userId": ("string", "uuid"),
                "title": ("string", None),
                "updatedAt": ("string", None),
            },
        )

    @classmethod
    def _recipe_label_schema(cls, value: Any) -> None:
        schema = cls._json_schema(
            value,
            schema_type="object",
            required={
                "id", "labelId", "recipeId", "createdAt", "updatedAt", "label",
            },
            properties={
                "id": ("string", "uuid"),
                "labelId": ("string", "uuid"),
                "recipeId": ("string", "uuid"),
                "createdAt": ("string", None),
                "updatedAt": ("string", None),
                "label": ("object", None),
            },
        )
        cls._label_schema(schema["properties"]["label"])

    @classmethod
    def _validate_operation_contract(
        cls, path: str, operation: Mapping[str, Any]
    ) -> None:
        expected_security = (
            []
            if path in {
                "/compat/v2/recipes/getRecipes",
                "/compat/v2/recipes/searchRecipes",
            }
            else [{"Authorization": []}]
        )
        if operation.get("security") != expected_security:
            raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
        response_schema = cls._response_schema(operation)
        if path == "/compat/v2/users/validateSession":
            cls._json_schema(response_schema, schema_type="string")
            return
        if path == "/compat/v2/users/getMe":
            cls._json_schema(
                response_schema,
                schema_type="object",
                required={"id"},
                properties={"id": ("string", "uuid")},
            )
            return
        if path == "/compat/v2/labels/getLabels":
            array_schema = cls._json_schema(response_schema, schema_type="array")
            cls._label_schema(array_schema.get("items"))
            return
        if path == "/compat/v2/labels/createLabel":
            request_schema = cls._json_schema(
                cls._request_schema(operation),
                schema_type="object",
                required={"title", "labelGroupId"},
                exact_required=True,
                properties={"title": ("string", None)},
            )
            if request_schema["properties"].get("title") != {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
            }:
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            label_group = request_schema["properties"].get("labelGroupId")
            if (
                not isinstance(label_group, Mapping)
                or label_group.get("anyOf")
                != [
                    {"type": "string", "format": "uuid", "pattern": _UUID_PATTERN},
                    {"type": "null"},
                ]
            ):
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            cls._label_schema(response_schema)
            return
        if path in {
            "/compat/v2/recipes/getRecipes",
            "/compat/v2/recipes/searchRecipes",
        }:
            listing = path.endswith("getRecipes")
            request_schema = cls._json_schema(
                cls._request_schema(operation),
                schema_type="object",
                required=(
                    {"folder", "orderBy", "orderDirection", "offset", "limit"}
                    if listing
                    else {"searchTerm", "folder"}
                ),
                exact_required=True,
                properties=(
                    {
                        "folder": ("string", None),
                        "orderBy": ("string", None),
                        "orderDirection": ("string", None),
                        "offset": ("number", None),
                        "limit": ("number", None),
                    }
                    if listing
                    else {
                        "searchTerm": ("string", None),
                        "folder": ("string", None),
                    }
                ),
            )
            request_properties = request_schema["properties"]
            if request_properties["folder"] != {
                "type": "string",
                "enum": ["main", "inbox"],
            }:
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            if listing:
                if (
                    request_properties["orderBy"]
                    != {
                        "type": "string",
                        "enum": ["title", "createdAt", "updatedAt", "lastMadeAt"],
                    }
                    or request_properties["orderDirection"]
                    != {"type": "string", "enum": ["asc", "desc"]}
                    or request_properties["offset"]
                    != {"type": "number", "minimum": 0}
                    or request_properties["limit"]
                    != {"type": "number", "minimum": 1, "maximum": 200}
                ):
                    raise RecipeLibraryError(
                        "RecipeSage OpenAPI contract is incompatible"
                    )
            elif request_properties["searchTerm"] != {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
            }:
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            if (
                request_properties.get("labels")
                != {"type": "array", "items": {"type": "string"}}
                or request_properties.get("labelIntersection")
                != {"type": "boolean"}
                or request_properties.get("ratings")
                != {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "integer", "minimum": 0, "maximum": 5},
                            {"type": "null"},
                        ]
                    },
                }
            ):
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            page = cls._json_schema(
                response_schema,
                schema_type="object",
                required={"recipes", "totalCount"},
                properties={
                    "recipes": ("array", None),
                    "totalCount": ("integer", None),
                },
            )
            cls._recipe_schema(page["properties"]["recipes"].get("items"), full=False)
            return
        if path in {
            "/compat/v2/recipes/getRecipe",
            "/compat/v2/recipes/getRecipesByUrl",
        }:
            parameters = operation.get("parameters")
            expected_name = "id" if path.endswith("getRecipe") else "url"
            if (
                not isinstance(parameters, list)
                or len(parameters) != 1
                or not isinstance(parameters[0], Mapping)
                or parameters[0].get("name") != expected_name
                or parameters[0].get("in") != "query"
                or parameters[0].get("required") is not True
            ):
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            parameter_schema = cls._json_schema(
                parameters[0].get("schema"), schema_type="string"
            )
            if expected_name == "id" and parameter_schema.get("format") != "uuid":
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            if path.endswith("getRecipe"):
                cls._recipe_schema(response_schema, full=True)
            else:
                array_schema = cls._json_schema(response_schema, schema_type="array")
                cls._recipe_schema(array_schema.get("items"), full=False)
            return
        if path == "/compat/v2/recipes/createRecipe":
            request_schema = cls._json_schema(
                cls._request_schema(operation),
                schema_type="object",
                required=_CREATE_REQUIRED_FIELDS,
                exact_required=True,
                properties={
                    **{
                        name: ("string", None)
                        for name in _CREATE_REQUIRED_FIELDS
                        if name not in {"rating", "folder", "labelIds", "imageIds"}
                    },
                    "labelIds": ("array", None),
                    "imageIds": ("array", None),
                },
            )
            properties = request_schema["properties"]
            rating = properties["rating"]
            folder = properties["folder"]
            if (
                properties["title"]
                != {"type": "string", "minLength": 1, "maxLength": 254}
                or not isinstance(rating, Mapping)
                or not isinstance(rating.get("anyOf"), list)
                or rating["anyOf"]
                != [
                    {"type": "number", "minimum": 1, "maximum": 5},
                    {"type": "null"},
                ]
                or not isinstance(folder, Mapping)
                or not isinstance(folder.get("anyOf"), list)
                or folder["anyOf"]
                != [
                    {"type": "string", "const": "main"},
                    {"type": "string", "const": "inbox"},
                ]
            ):
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            for name in _CREATE_REQUIRED_FIELDS - {
                "title",
                "rating",
                "folder",
                "labelIds",
                "imageIds",
            }:
                if properties[name] != {"type": "string"}:
                    raise RecipeLibraryError(
                        "RecipeSage OpenAPI contract is incompatible"
                    )
            for name in ("labelIds", "imageIds"):
                if properties[name] != {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "format": "uuid",
                        "pattern": _UUID_PATTERN,
                    },
                }:
                    raise RecipeLibraryError(
                        "RecipeSage OpenAPI contract is incompatible"
                    )
            cls._json_schema(
                response_schema,
                schema_type="object",
                required={"id"},
                properties={"id": ("string", "uuid")},
            )
            return
        raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")

    @classmethod
    def _validate_openapi(cls, value: Any) -> str:
        if not isinstance(value, Mapping) or value.get("openapi") != "3.1.0":
            raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
        info = value.get("info")
        paths = value.get("paths")
        if not isinstance(info, Mapping) or not isinstance(paths, Mapping):
            raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
        version = _text(info.get("version"), "server version", 100)
        if version != "selfhost" and _version_tuple(version) < MINIMUM_RECIPESAGE_VERSION:
            raise RecipeLibraryError(
                f"RecipeSage {MINIMUM_RECIPESAGE_VERSION_TEXT} or newer is required"
            )
        for path, (method, operation_id) in _REQUIRED_OPERATIONS.items():
            path_item = paths.get(path)
            operation = path_item.get(method) if isinstance(path_item, Mapping) else None
            if not isinstance(operation, Mapping) or operation.get("operationId") != operation_id:
                raise RecipeLibraryError("RecipeSage OpenAPI contract is incompatible")
            cls._validate_operation_contract(path, operation)
        return version or ""

    def _load_contract(self) -> tuple[str, str]:
        if self._api_prefix is not None and self._server_version is not None:
            return self._api_prefix, self._server_version
        for prefix in ("", "/api"):
            try:
                raw = self._request(
                    "GET", f"{prefix}/openapi.json", authenticated=False
                )
            except _RecipeSageHTTPStatus as exc:
                if exc.status == 404 and not prefix:
                    continue
                raise
            version = self._validate_openapi(raw)
            self._api_prefix = prefix
            self._server_version = version
            return prefix, version
        raise RecipeLibraryError("RecipeSage OpenAPI contract is unavailable")

    def _api_path(self, path: str) -> str:
        prefix, _version = self._load_contract()
        return f"{prefix}/compat/v2{path}"

    @staticmethod
    def _needs_auth(exc: Exception) -> bool:
        return isinstance(exc, _RecipeSageHTTPStatus) and exc.status in {401, 403}

    def _authenticated_user_id(self) -> str:
        if self._user_id is not None:
            return self._user_id
        try:
            user = self._request("GET", self._api_path("/users/getMe"))
        except _RecipeSageHTTPStatus as exc:
            if self._needs_auth(exc):
                raise RecipeLibraryError("RecipeSage needs_auth") from None
            raise RecipeLibraryError(
                "RecipeSage authenticated-user probe failed"
            ) from None
        except _RecipeSageTransportFailure:
            raise RecipeLibraryError(
                "RecipeSage authenticated-user probe is unavailable"
            ) from None
        if not isinstance(user, Mapping):
            raise RecipeLibraryError(
                "RecipeSage authenticated-user probe is incompatible"
            )
        self._user_id = _provider_id(user.get("id"), "authenticated user id")
        return self._user_id

    def _require_owned_recipe(self, value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise RecipeLibraryError("RecipeSage recipe is incompatible")
        if _provider_id(value.get("userId"), "recipe owner id") != self._authenticated_user_id():
            raise RecipeLibraryError(
                "RecipeSage recipe belongs to a different account"
            )
        return value

    @staticmethod
    def _page(
        value: Any, *, offset: int, limit: int | None, exact_total: bool = False
    ) -> tuple[list[Any], int]:
        if not isinstance(value, Mapping) or not isinstance(value.get("recipes"), list):
            raise RecipeLibraryError("RecipeSage recipe page is incompatible")
        recipes = value["recipes"]
        total = value.get("totalCount")
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or (limit is not None and len(recipes) > limit)
            or len(recipes) > total
            or (exact_total and len(recipes) != total)
            or (not exact_total and recipes and offset >= total)
            or (not exact_total and not recipes and offset < total)
        ):
            raise RecipeLibraryError("RecipeSage recipe page is incompatible")
        return recipes, total

    def capabilities(self) -> Mapping[str, Any]:
        try:
            _prefix, version = self._load_contract()
            validation = self._request(
                "GET", self._api_path("/users/validateSession")
            )
            if validation != "Valid":
                raise RecipeLibraryError(
                    "RecipeSage session validation response is incompatible"
                )
            self._user_id = None
            self._authenticated_user_id()
            page = self._request(
                "POST",
                self._api_path("/recipes/getRecipes"),
                body={
                    "folder": "main",
                    "orderBy": "title",
                    "orderDirection": "asc",
                    "offset": 0,
                    "limit": 1,
                },
            )
            items, _total = self._page(page, offset=0, limit=1)
            for item in items:
                self._require_owned_recipe(item)
            labels = self._request("GET", self._api_path("/labels/getLabels"))
            self._labels(labels)
        except RecipeLibraryError:
            raise
        except _RecipeSageHTTPStatus as exc:
            if self._needs_auth(exc):
                raise RecipeLibraryError("RecipeSage needs_auth") from None
            if exc.status == 429:
                raise RecipeLibraryError(
                    "RecipeSage capability probe was rate limited"
                ) from None
            raise RecipeLibraryError("RecipeSage capability probe failed") from None
        except _RecipeSageTransportFailure:
            raise RecipeLibraryError(
                "RecipeSage capability probe is unavailable"
            ) from None
        capabilities = {
            name: name in {
                "search",
                "get",
                "create_from_discovery",
                "reconcile_create",
                "label_read",
                "label_create",
            }
            for name in CAPABILITY_NAMES
        }
        if self.read_only:
            capabilities["create_from_discovery"] = False
            capabilities["reconcile_create"] = False
            capabilities["label_create"] = False
        return {
            "provider": "recipesage",
            "server_version": version,
            "read_only": self.read_only,
            **capabilities,
        }

    @staticmethod
    def _cursor(value: str | None) -> int:
        if value is None:
            return 0
        if not isinstance(value, str):
            raise RecipeLibraryError("RecipeSage cursor is invalid")
        match = re.fullmatch(r"offset:(0|[1-9]\d{0,6})", value)
        if match is None or int(match.group(1)) > MAX_CURSOR_OFFSET:
            raise RecipeLibraryError("RecipeSage cursor is invalid")
        return int(match.group(1))

    @staticmethod
    def _search_filters(filters: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(filters, Mapping) or len(filters) > 4:
            raise RecipeLibraryError("RecipeSage search filters are invalid")
        allowed = {"folder", "labels", "label_intersection", "ratings"}
        if set(filters) - allowed:
            raise RecipeLibraryError("RecipeSage search filter is unsupported")
        result: dict[str, Any] = {"folder": filters.get("folder", "main")}
        if result["folder"] not in {"main", "inbox"}:
            raise RecipeLibraryError("RecipeSage search filter is invalid")
        if filters.get("labels") is not None:
            values = filters["labels"]
            if not isinstance(values, list) or not 1 <= len(values) <= 50:
                raise RecipeLibraryError("RecipeSage search filter is invalid")
            result["labels"] = [
                _text(value, "search label", 200) or "" for value in values
            ]
        if filters.get("label_intersection") is not None:
            if not isinstance(filters["label_intersection"], bool):
                raise RecipeLibraryError("RecipeSage search filter is invalid")
            result["labelIntersection"] = filters["label_intersection"]
        if filters.get("ratings") is not None:
            values = filters["ratings"]
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= 6
                or any(
                    value is not None
                    and (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or not 0 <= value <= 5
                    )
                    for value in values
                )
            ):
                raise RecipeLibraryError("RecipeSage search filter is invalid")
            result["ratings"] = values
        return result

    def _reference(self, value: Mapping[str, Any]) -> dict[str, str]:
        reference = {
            "library_id": self.library_id,
            "recipe_id": _provider_id(value.get("id")),
        }
        if value.get("updatedAt") is not None:
            reference["version"] = _text(
                value.get("updatedAt"), "recipe version", 100
            ) or ""
        return reference

    @staticmethod
    def _tags(value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 50:
            raise RecipeLibraryError("RecipeSage recipe labels are incompatible")
        result = []
        for item in value:
            if not isinstance(item, Mapping) or not isinstance(item.get("label"), Mapping):
                raise RecipeLibraryError("RecipeSage recipe labels are incompatible")
            result.append(
                _text(item["label"].get("title"), "recipe label", 80) or ""
            )
        return result

    def _label(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RecipeLibraryError("RecipeSage label is incompatible")
        if _provider_id(value.get("userId"), "label owner id") != self._authenticated_user_id():
            raise RecipeLibraryError("RecipeSage label belongs to a different account")
        label_id = _provider_id(value.get("id"), "label id")
        name, normalized_name = normalize_label_name(value.get("title"))
        version = _text(value.get("updatedAt"), "label version", 100)
        return {
            "library_id": self.library_id,
            "library_label_ref": {
                "library_id": self.library_id,
                "label_id": label_id,
                "version": version,
            },
            "name": name,
            "normalized_name": normalized_name,
        }

    def _labels(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > MAX_LABELS:
            raise RecipeLibraryError("RecipeSage labels are incompatible")
        result = [self._label(item) for item in value]
        identities = [item["library_label_ref"]["label_id"] for item in result]
        if len(identities) != len(set(identities)):
            raise RecipeLibraryError("RecipeSage labels are incompatible")
        return result

    def _recipe_labels(self, value: Any, recipe_id: str) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 50:
            raise RecipeLibraryError("RecipeSage recipe labels are incompatible")
        result: list[dict[str, Any]] = []
        for item in value:
            if (
                not isinstance(item, Mapping)
                or _provider_id(item.get("recipeId")) != recipe_id
                or not isinstance(item.get("label"), Mapping)
            ):
                raise RecipeLibraryError("RecipeSage recipe labels are incompatible")
            _provider_id(item.get("id"), "recipe label relation id")
            _text(item.get("createdAt"), "recipe label relation creation", 100)
            _text(item.get("updatedAt"), "recipe label relation update", 100)
            label_id = _provider_id(item.get("labelId"), "label id")
            label = self._label(item["label"])
            if label["library_label_ref"]["label_id"] != label_id:
                raise RecipeLibraryError("RecipeSage recipe labels are incompatible")
            result.append(label)
        identities = [item["library_label_ref"]["label_id"] for item in result]
        if len(identities) != len(set(identities)):
            raise RecipeLibraryError("RecipeSage recipe labels are incompatible")
        return result

    def _summary(self, value: Any) -> dict[str, Any]:
        value = self._require_owned_recipe(value)
        name = _text(value.get("title"), "recipe title", 254)
        source: dict[str, Any] = {
            "kind": "recipesage",
            "publisher": "RecipeSage",
            "title": name,
            "relationship": "user_supplied",
        }
        if (source_url := _safe_source_url(value.get("url"))) is not None:
            source["url"] = source_url
        return {
            "name": name,
            "tags": self._tags(value.get("recipeLabels")),
            "source": source,
            "library_recipe_ref": self._reference(value),
        }

    def _search_raw(self, query: str, filters: Mapping[str, Any]) -> tuple[list[Any], int]:
        body = self._search_filters(filters)
        body["searchTerm"] = query
        raw = self._request(
            "POST", self._api_path("/recipes/searchRecipes"), body=body
        )
        items, total = self._page(raw, offset=0, limit=None, exact_total=True)
        for item in items:
            self._require_owned_recipe(item)
        return items, total

    def search(
        self, query: str, filters: Mapping[str, Any], cursor: str | None, limit: int
    ) -> Mapping[str, Any]:
        if (
            not isinstance(query, str)
            or len(query) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in query)
        ):
            raise RecipeLibraryError("RecipeSage search query is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise RecipeLibraryError("RecipeSage search limit is invalid")
        offset = self._cursor(cursor)
        try:
            if query.strip():
                all_items, total = self._search_raw(query.strip(), filters)
                items = all_items[offset : offset + limit]
            else:
                body = {
                    **self._search_filters(filters),
                    "orderBy": "title",
                    "orderDirection": "asc",
                    "offset": offset,
                    "limit": limit,
                }
                raw = self._request(
                    "POST", self._api_path("/recipes/getRecipes"), body=body
                )
                items, total = self._page(raw, offset=offset, limit=limit)
                for item in items:
                    self._require_owned_recipe(item)
            recipes = [self._summary(item) for item in items]
        except RecipeLibraryError:
            raise
        except _RecipeSageHTTPStatus as exc:
            if self._needs_auth(exc):
                raise RecipeLibraryError("RecipeSage needs_auth") from None
            raise RecipeLibraryError("RecipeSage recipe search failed") from None
        except _RecipeSageTransportFailure:
            raise RecipeLibraryError(
                "RecipeSage recipe search is unavailable"
            ) from None
        next_offset = offset + len(items)
        next_cursor = f"offset:{next_offset}" if next_offset < total else None
        return {"recipes": recipes, "cursor": next_cursor}

    @staticmethod
    def _origin(operation: Mapping[str, Any]) -> dict[str, Any]:
        origin = {
            "operation_id": _text(operation.get("operation_id"), "operation id", 80),
            "library_id": validate_library_id(
                operation.get("library_id"), allow_builtin=False
            ),
            "snapshot_digest": _text(
                operation.get("snapshot_digest"), "snapshot digest", 64
            ),
            "source_identity": _text(
                operation.get("source_identity"), "source identity", 2_048
            ),
        }
        if (
            re.fullmatch(
                r"libop:v1:[A-Za-z0-9_-]{16,64}", origin["operation_id"] or ""
            )
            is None
            or re.fullmatch(r"[a-f0-9]{64}", origin["snapshot_digest"] or "")
            is None
        ):
            raise RecipeLibraryError("RecipeSage create operation is invalid")
        return origin

    @staticmethod
    def _attribution(snapshot: Mapping[str, Any], marker: str) -> str:
        source = (
            snapshot.get("source")
            if isinstance(snapshot.get("source"), Mapping)
            else {}
        )
        rights = (
            snapshot.get("rights")
            if isinstance(snapshot.get("rights"), Mapping)
            else {}
        )
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

    @staticmethod
    def _time_value(document: Mapping[str, Any], key: str) -> str:
        times = document.get("times")
        value = times.get(key) if isinstance(times, Mapping) else None
        if value is None:
            return ""
        return _text(value, f"recipe time {key}", 200) or ""

    @staticmethod
    def _stored_document(document: Mapping[str, Any]) -> dict[str, Any]:
        if document["rights"]["storage"] != "link_only":
            return deepcopy(dict(document))
        return {
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

    def _native_payload(
        self, snapshot: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        document = normalize_recipe(snapshot)
        origin = self._origin(operation)
        if origin["library_id"] != self.library_id:
            raise RecipeLibraryError(
                "RecipeSage create operation names the wrong library"
            )
        _text(document["name"], "recipe title", 254)
        marker = _marker(origin["operation_id"] or "")
        stored_document = self._stored_document(document)
        native_document = normalize_recipe(stored_document)
        metadata = _canonical(
            {"marker": marker, "origin": origin, "recipe": stored_document}
        )
        if len(metadata.encode("utf-8")) > MAX_METADATA_BYTES:
            raise RecipeLibraryError("RecipeSage recipe metadata is too large")
        notes = f"{METADATA_BEGIN}\n{metadata}\n{METADATA_END}"
        link_only = native_document["rights"]["storage"] == "link_only"
        if not link_only and native_document.get("notes"):
            notes = f"{notes}\n\n{native_document['notes']}"
        portions = native_document.get("portions")
        payload = {
            "title": native_document["name"],
            "description": self._attribution(native_document, marker),
            "yield": ""
            if link_only or portions is None
            else f"{_format_number(float(portions))} servings",
            "activeTime": "" if link_only else self._time_value(native_document, "prep"),
            "totalTime": "" if link_only else self._time_value(native_document, "total"),
            "source": native_document["source"].get("publisher")
            or native_document["source"].get("author")
            or native_document["source"].get("title")
            or "",
            "url": native_document["source"].get("url") or "",
            "notes": notes,
            "ingredients": ""
            if link_only
            else "\n".join(
                ingredient["raw"] for ingredient in native_document["ingredients"]
            ),
            "instructions": ""
            if link_only
            else "\n".join(native_document["steps"]),
            "rating": None,
            "folder": "main",
            "labelIds": [],
            "imageIds": [],
        }
        return payload, native_document

    @staticmethod
    def _metadata(
        notes: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str | None] | None:
        raw = _body(notes, "recipe notes")
        prefix = f"{METADATA_BEGIN}\n"
        delimiter = f"\n{METADATA_END}"
        if not raw.startswith(prefix):
            return None
        end = raw.find(delimiter, len(prefix))
        if end < 0:
            raise RecipeLibraryError("RecipeSage Hermes metadata is incompatible")
        encoded = raw[len(prefix) : end]
        if not encoded or len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise RecipeLibraryError("RecipeSage Hermes metadata is incompatible")
        tail = raw[end + len(delimiter) :]
        if tail:
            if not tail.startswith("\n\n"):
                raise RecipeLibraryError("RecipeSage Hermes metadata is incompatible")
            user_notes = tail[2:] or None
        else:
            user_notes = None
        try:
            value = json.loads(encoded)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise RecipeLibraryError(
                "RecipeSage Hermes metadata is incompatible"
            ) from exc
        if not isinstance(value, Mapping) or set(value) != {"marker", "origin", "recipe"}:
            raise RecipeLibraryError("RecipeSage Hermes metadata is incompatible")
        if not isinstance(value["origin"], Mapping) or not isinstance(value["recipe"], Mapping):
            raise RecipeLibraryError("RecipeSage Hermes metadata is incompatible")
        origin = RecipeSageAdapter._origin(value["origin"])
        marker = _text(value["marker"], "operation marker", 64)
        if marker != _marker(origin["operation_id"] or ""):
            raise RecipeLibraryError("RecipeSage Hermes metadata is incompatible")
        try:
            document = normalize_recipe(value["recipe"])
        except RecipeError as exc:
            raise RecipeLibraryError(
                "RecipeSage Hermes metadata is incompatible"
            ) from exc
        return origin, dict(value["recipe"]), document, user_notes

    def _native_matches(
        self,
        raw: Mapping[str, Any],
        payload: Mapping[str, Any],
        document: Mapping[str, Any],
        origin: Mapping[str, Any],
    ) -> bool:
        try:
            metadata = self._metadata(raw.get("notes"))
            if metadata is None:
                return False
            returned_origin, _stored, returned_document, user_notes = metadata
            expected_notes = (
                None
                if document["rights"]["storage"] == "link_only"
                else document.get("notes")
            )
            return (
                returned_origin == origin
                and returned_document == document
                and user_notes == expected_notes
                and raw.get("title") == payload.get("title")
                and raw.get("description") == payload.get("description")
                and raw.get("yield") == payload.get("yield")
                and raw.get("activeTime") == payload.get("activeTime")
                and raw.get("totalTime") == payload.get("totalTime")
                and raw.get("source") == payload.get("source")
                and raw.get("url") == payload.get("url")
                and raw.get("notes") == payload.get("notes")
                and raw.get("ingredients") == payload.get("ingredients")
                and raw.get("instructions") == payload.get("instructions")
                and raw.get("rating") is None
                and raw.get("folder") == "main"
                and self._tags(raw.get("recipeLabels")) == []
                and isinstance(raw.get("recipeImages"), list)
                and not raw.get("recipeImages")
            )
        except (RecipeLibraryError, TypeError, ValueError):
            return False

    @staticmethod
    def _split_lines(value: Any, field: str, maximum: int) -> list[str]:
        raw = _body(value, f"recipe {field}")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not 1 <= len(lines) <= maximum:
            raise RecipeLibraryError(f"RecipeSage recipe {field} is incompatible")
        return lines

    @staticmethod
    def _portions(value: Any) -> float | None:
        raw = _body(value, "recipe yield", 1_000).strip()
        if not raw:
            return None
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:servings?)?", raw, re.IGNORECASE)
        if match is None:
            return None
        result = float(match.group(1))
        return result if math.isfinite(result) and result > 0 else None

    @staticmethod
    def _times(raw: Mapping[str, Any], stored: Mapping[str, Any] | None) -> dict[str, Any] | None:
        result = (
            deepcopy(stored.get("times"))
            if isinstance(stored, Mapping)
            and isinstance(stored.get("times"), Mapping)
            else {}
        )
        for key, field in (("prep", "activeTime"), ("total", "totalTime")):
            value = _body(raw.get(field), f"recipe {field}", 1_000)
            if value:
                result[key] = value
            else:
                result.pop(key, None)
        return result or None

    def _mapped_recipe(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        metadata = self._metadata(raw.get("notes"))
        stored: dict[str, Any] | None = None
        document: dict[str, Any] | None = None
        user_notes: str | None = None
        if metadata is not None:
            origin, _raw_stored, document, user_notes = metadata
            payload, expected_document = self._native_payload(document, origin)
            if self._native_matches(raw, payload, expected_document, origin):
                return document
            stored = document
            source = deepcopy(document["source"])
            rights = deepcopy(document["rights"])
        else:
            name = _text(raw.get("title"), "recipe title", 254)
            source = {
                "kind": "recipesage",
                "publisher": "RecipeSage",
                "title": name,
                "url": _safe_source_url(raw.get("url")),
                "external_id": _provider_id(raw.get("id")),
                "relationship": "user_supplied",
            }
            rights = {
                "storage": "full",
                "license": None,
                "license_url": None,
                "credit": None,
            }
            user_notes = _body(raw.get("notes"), "recipe notes") or None
        name = _text(raw.get("title"), "recipe title", 254)
        if rights.get("storage") == "link_only":
            candidate: dict[str, Any] = {
                "name": name,
                "language": stored.get("language", "nb-NO") if stored else "nb-NO",
                "tags": deepcopy(stored["tags"]) if stored else self._tags(raw.get("recipeLabels")),
                "source": source,
                "rights": rights,
            }
        else:
            candidate = {
                "name": name,
                "language": stored.get("language", "nb-NO") if stored else "nb-NO",
                "tags": deepcopy(stored["tags"]) if stored else self._tags(raw.get("recipeLabels")),
                "source": source,
                "rights": rights,
                "portions": self._portions(raw.get("yield")),
                "ingredients": self._split_lines(raw.get("ingredients"), "ingredients", 200),
                "steps": self._split_lines(raw.get("instructions"), "instructions", 100),
                "notes": user_notes,
                "times": self._times(raw, stored),
            }
            if stored:
                for field in ("external_snapshot", "storage", "reheating"):
                    if field in stored:
                        candidate[field] = deepcopy(stored[field])
        try:
            return normalize_recipe(candidate)
        except RecipeError as exc:
            raise RecipeLibraryError(
                "RecipeSage recipe content is incompatible"
            ) from exc

    def _get_raw(self, recipe_id: str) -> Mapping[str, Any]:
        try:
            raw = self._request(
                "GET",
                self._api_path("/recipes/getRecipe"),
                query=[("id", recipe_id)],
            )
        except _RecipeSageHTTPStatus as exc:
            if self._needs_auth(exc):
                raise RecipeLibraryError("RecipeSage needs_auth") from None
            raise RecipeLibraryError("RecipeSage exact recipe get failed") from None
        except _RecipeSageTransportFailure:
            raise RecipeLibraryError(
                "RecipeSage exact recipe get is unavailable"
            ) from None
        if not isinstance(raw, Mapping) or _provider_id(raw.get("id")) != recipe_id:
            raise RecipeLibraryError(
                "RecipeSage exact recipe response is incompatible"
            )
        return self._require_owned_recipe(raw)

    def _result(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **self._mapped_recipe(raw),
            "library_recipe_ref": self._reference(raw),
        }

    def _create_result(
        self, raw: Mapping[str, Any], snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        remote = self._result(raw)
        frozen = normalize_recipe(snapshot)
        if frozen["rights"]["storage"] != "link_only":
            return remote
        frozen = normalize_recipe(
            {
                key: deepcopy(frozen[key])
                for key in (
                    "schema_version",
                    "name",
                    "language",
                    "tags",
                    "source",
                    "rights",
                    "external_snapshot",
                )
                if key in frozen
            }
        )
        return {**frozen, "library_recipe_ref": remote["library_recipe_ref"]}

    def get(self, library_recipe_ref: Mapping[str, str]) -> Mapping[str, Any]:
        reference = validate_library_recipe_ref(library_recipe_ref)
        if reference["library_id"] != self.library_id:
            raise RecipeLibraryError(
                "RecipeSage recipe reference names the wrong library"
            )
        recipe_id = _provider_id(reference["recipe_id"])
        return self._result(self._get_raw(recipe_id))

    def list_labels(self) -> list[Mapping[str, Any]]:
        try:
            raw = self._request("GET", self._api_path("/labels/getLabels"))
            return self._labels(raw)
        except RecipeLibraryError:
            raise
        except _RecipeSageHTTPStatus as exc:
            if self._needs_auth(exc):
                raise RecipeLibraryError("RecipeSage label read needs_auth") from None
            raise RecipeLibraryError("RecipeSage label read failed") from None
        except _RecipeSageTransportFailure:
            raise RecipeLibraryError("RecipeSage label read is unavailable") from None

    def get_recipe_labels(
        self, library_recipe_ref: Mapping[str, str]
    ) -> list[Mapping[str, Any]]:
        reference = validate_library_recipe_ref(library_recipe_ref)
        if reference["library_id"] != self.library_id:
            raise RecipeLibraryError(
                "RecipeSage recipe reference names the wrong library"
            )
        recipe_id = _provider_id(reference["recipe_id"])
        raw = self._get_raw(recipe_id)
        return self._recipe_labels(raw.get("recipeLabels"), recipe_id)

    def create_label(self, name: str, *, idempotency_key: str) -> Mapping[str, Any]:
        if self.read_only:
            raise RecipeLibraryDefiniteError("RecipeSage connection is read-only")
        display, normalized_name = normalize_label_name(name)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise RecipeLibraryDefiniteError(
                "RecipeSage label idempotency key is invalid"
            )
        provider_title = display.lower().replace(",", "")
        if provider_title == "unlabeled":
            provider_title = "un-labeled"
        try:
            provider_normalized = normalize_label_name(provider_title)[1]
        except RecipeLibraryError:
            raise RecipeLibraryDefiniteError(
                "RecipeSage would reject or transform this label name"
            ) from None
        if provider_normalized != normalized_name:
            raise RecipeLibraryDefiniteError(
                "RecipeSage would transform this label name"
            )
        try:
            raw = self._request(
                "POST",
                self._api_path("/labels/createLabel"),
                body={"title": provider_title, "labelGroupId": None},
            )
            result = self._label(raw)
            if result["normalized_name"] != normalized_name:
                raise RecipeLibraryUncertainError(
                    "RecipeSage label creation response is incompatible"
                )
            return result
        except _RecipeSageHTTPStatus as exc:
            if 400 <= exc.status < 500 and exc.status != 408:
                message = (
                    "RecipeSage label creation needs_auth"
                    if self._needs_auth(exc)
                    else "RecipeSage rejected label creation"
                )
                raise RecipeLibraryDefiniteError(message) from None
            raise RecipeLibraryUncertainError(
                "RecipeSage label creation outcome is uncertain"
            ) from None
        except _RecipeSageTransportFailure:
            raise RecipeLibraryUncertainError(
                "RecipeSage label creation outcome is uncertain"
            ) from None

    @staticmethod
    def _definite_initial_failure(exc: Exception) -> bool:
        return (
            isinstance(exc, _RecipeSageHTTPStatus)
            and 400 <= exc.status < 500
            and exc.status != 408
        )

    def create_from_snapshot(
        self, snapshot: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if self.read_only:
            raise RecipeLibraryDefiniteError("RecipeSage connection is read-only")
        try:
            payload, document = self._native_payload(snapshot, operation)
            origin = self._origin(operation)
            self._authenticated_user_id()
        except RecipeLibraryError as exc:
            if str(exc).endswith("needs_auth"):
                raise RecipeLibraryDefiniteError("RecipeSage needs_auth") from None
            raise RecipeLibraryDefiniteError(
                "RecipeSage create request is invalid"
            ) from None
        except RecipeError:
            raise RecipeLibraryDefiniteError(
                "RecipeSage create request is invalid"
            ) from None
        try:
            created = self._request(
                "POST", self._api_path("/recipes/createRecipe"), body=payload
            )
        except Exception as exc:
            if self._definite_initial_failure(exc):
                if self._needs_auth(exc):
                    raise RecipeLibraryDefiniteError("RecipeSage needs_auth") from None
                raise RecipeLibraryDefiniteError(
                    "RecipeSage rejected recipe creation"
                ) from None
            raise RecipeLibraryUncertainError(
                "RecipeSage recipe creation outcome is uncertain"
            ) from None
        try:
            if not isinstance(created, Mapping):
                raise RecipeLibraryError(
                    "RecipeSage created recipe response is incompatible"
                )
            recipe_id = _provider_id(created.get("id"), "created recipe id")
            raw = self._get_raw(recipe_id)
            if not self._native_matches(raw, payload, document, origin):
                raise RecipeLibraryError(
                    "RecipeSage created recipe did not preserve the frozen content"
                )
            recipe = self._create_result(raw, snapshot)
        except RecipeLibraryError as exc:
            if str(exc).endswith("needs_auth"):
                raise RecipeLibraryUncertainError(
                    "RecipeSage uncertain needs_auth"
                ) from None
            raise RecipeLibraryUncertainError(
                "RecipeSage recipe creation outcome is uncertain"
            ) from None
        except Exception:
            raise RecipeLibraryUncertainError(
                "RecipeSage recipe creation outcome is uncertain"
            ) from None
        return {
            "library_recipe_ref": recipe["library_recipe_ref"],
            "recipe": recipe,
        }

    def reconcile_create(
        self, snapshot: Mapping[str, Any], operation: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        if self.read_only:
            return None
        try:
            payload, document = self._native_payload(snapshot, operation)
            origin = self._origin(operation)
            self._authenticated_user_id()
            marker = _marker(origin["operation_id"] or "")
            items, total = self._search_raw(marker, {})
            if total != 1 or len(items) != 1:
                return None
            recipe_id = self._reference(items[0])["recipe_id"]
            raw = self._get_raw(recipe_id)
            metadata = self._metadata(raw.get("notes"))
            if metadata is None or metadata[0] != origin:
                return None
            if not self._native_matches(raw, payload, document, origin):
                return None
            recipe = self._create_result(raw, snapshot)
            return {
                "library_recipe_ref": recipe["library_recipe_ref"],
                "recipe": recipe,
            }
        except _RecipeSageHTTPStatus as exc:
            if self._needs_auth(exc):
                raise RecipeLibraryError("RecipeSage needs_auth") from None
            return None
        except RecipeLibraryError as exc:
            if str(exc).endswith("needs_auth"):
                raise
            return None
        except Exception:
            return None


def create_adapter(
    connection: Mapping[str, Any], credential: Mapping[str, Any]
) -> RecipeLibraryAdapter:
    return RecipeSageAdapter(connection, credential)
