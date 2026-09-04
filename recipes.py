"""Household-local recipe documents, validation, scaling and SQLite storage."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
from typing import Any, Iterable, Mapping
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from core import HouseholdError
from recipe_libraries import (
    RecipeLibraryError,
    normalize_label_name,
    validate_library_id,
    validate_library_label_ref,
    validate_library_recipe_ref,
)


MAX_RECIPE_BYTES = 256 * 1024
MAX_IMPORT_RECORDS = 10_000
MAX_TEXT = 4_000
DISCOVERY_TTL_DAYS = 30
MAX_UNBOUND_DISCOVERY_SNAPSHOTS = 2_000
MAX_UNBOUND_DISCOVERY_BYTES = 64 * 1024 * 1024
FAILED_DISCOVERY_BINDING_TTL_DAYS = 30
MAX_FAILED_DISCOVERY_BINDINGS = 2_000
MAX_LIBRARY_OPERATIONS = 10_000
MAX_LIBRARY_MAPPINGS = 10_000
FAILED_LIBRARY_OPERATION_TTL_DAYS = 90
LIBRARY_LIFECYCLE_CONFIRMATION_TTL = timedelta(minutes=10)
ACTIVE_DISCOVERY_BINDING_STATUSES = {"pending", "uncertain"}
LIBRARY_RECIPE_MUTATION_KINDS = {
    "conditional_update", "archive", "delete", "favorite", "label",
}
DISCOVERY_DESTINATION_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}")
VALID_RELATIONSHIPS = {"original", "adapted", "inspired_by", "generated", "user_supplied", "unknown"}
VALID_STORAGE = {"full", "link_only"}
RESTRICTED_FULL_HOSTS = {"meny.no", "www.meny.no", "oda.com", "www.oda.com"}
SERVER_FIELDS = {
    "id", "revision", "status", "created_at", "updated_at", "created_via",
    "recipe_key", "content_fingerprint", "content_hash", "shopping_requirements",
    "library_id", "library_recipe_ref", "is_favorite", "favorite_revision",
}


class RecipeError(HouseholdError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _normalized_text(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip().casefold()


def _bounded_text(value: Any, field: str, *, required: bool = False, maximum: int = MAX_TEXT) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise RecipeError(f"{field} must be text")
    result = unicodedata.normalize("NFC", value).strip()
    if any(0xD800 <= ord(character) <= 0xDFFF for character in result):
        raise RecipeError(f"{field} contains invalid Unicode")
    if required and not result:
        raise RecipeError(f"{field} is required")
    if len(result) > maximum:
        raise RecipeError(f"{field} is too long")
    return result or None


def _favorite_revision(value: Any, field: str) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RecipeError(f"{field} must be a non-negative integer or exact text")
    if isinstance(value, int):
        if value < 0:
            raise RecipeError(f"{field} must be a non-negative integer or exact text")
        return value
    if isinstance(value, str):
        checked = _bounded_text(value, field, required=True, maximum=300)
        if checked != value:
            raise RecipeError(f"{field} must be exact text")
        return checked
    raise RecipeError(f"{field} must be a non-negative integer or exact text")


def _finite_positive(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecipeError(f"{field} must be a positive finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RecipeError(f"{field} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise RecipeError(f"{field} must be a positive finite number")
    return number


def _check_shape(value: Any, depth: int = 0) -> None:
    if depth > 12:
        raise RecipeError("recipe nesting is too deep")
    if isinstance(value, Mapping):
        if len(value) > 100:
            raise RecipeError("recipe object has too many fields")
        for key, child in value.items():
            if not isinstance(key, str):
                raise RecipeError("recipe keys must be text")
            if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                raise RecipeError("recipe keys contain invalid Unicode")
            _check_shape(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 500:
            raise RecipeError("recipe list is too long")
        for child in value:
            _check_shape(child, depth + 1)
    elif isinstance(value, str) and any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise RecipeError("recipe contains invalid Unicode")
    elif isinstance(value, float) and not math.isfinite(value):
        raise RecipeError("recipe numbers must be finite")
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise RecipeError("recipe contains an unsupported value")


def validate_week(value: Any) -> str:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", str(value or ""))
    if not match:
        raise RecipeError("week must be a valid ISO week in YYYY-Www format")
    try:
        datetime.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError as exc:
        raise RecipeError("week must be a valid ISO week in YYYY-Www format") from exc
    return str(value)


def normalize_source_url(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = _bounded_text(value, "source.url", maximum=2_048)
    if text is None:
        return None
    if any(character == "\\" or character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text):
        raise RecipeError("source.url contains forbidden characters")
    try:
        parsed = urlsplit(text or "")
    except ValueError as exc:
        raise RecipeError("source.url is invalid") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RecipeError("source.url must be a credential-free HTTPS URL")
    raw_host = parsed.hostname.casefold().rstrip(".")
    if "%" in raw_host:
        raise RecipeError("source.url hostname must not contain percent escapes")
    try:
        host = ipaddress.ip_address(raw_host).compressed
    except ValueError:
        try:
            host = raw_host.encode("idna").decode("ascii").casefold().rstrip(".")
        except UnicodeError as exc:
            raise RecipeError("source.url hostname is invalid") from exc
        if not host or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in host.split(".")
        ):
            raise RecipeError("source.url hostname is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RecipeError("source.url is invalid") from exc
    if port not in {None, 443}:
        raise RecipeError("source.url must use the standard HTTPS port")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    rendered_host = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", rendered_host, path, "", ""))


def _source(value: Any, *, required: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        if required:
            raise RecipeError("source metadata is required")
        value = {}
    if required and (not isinstance(value.get("kind"), str) or not value["kind"].strip()):
        raise RecipeError("source.kind must be explicit")
    if required and (not isinstance(value.get("relationship"), str) or not value["relationship"].strip()):
        raise RecipeError("source.relationship must be explicit")
    relationship = str(value.get("relationship") or "unknown").casefold()
    if relationship not in VALID_RELATIONSHIPS:
        raise RecipeError("source.relationship is invalid")
    result = {
        "kind": _bounded_text(value.get("kind") or "unknown", "source.kind", required=True, maximum=40),
        "publisher": _bounded_text(value.get("publisher"), "source.publisher", maximum=200),
        "title": _bounded_text(value.get("title"), "source.title", maximum=300),
        "author": _bounded_text(value.get("author"), "source.author", maximum=200),
        "url": normalize_source_url(value.get("url")),
        "external_id": _bounded_text(value.get("external_id"), "source.external_id", maximum=300),
        "relationship": relationship,
    }
    return result


def _rights(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecipeError("rights metadata is required")
    storage = str(value.get("storage") or value.get("content_mode") or "").casefold()
    if storage == "transient":
        raise RecipeError("transient recipes cannot be persisted")
    if storage not in VALID_STORAGE:
        raise RecipeError("rights.storage must be full or link_only")
    return {
        "storage": storage,
        "license": _bounded_text(value.get("license"), "rights.license", maximum=200),
        "license_url": normalize_source_url(value.get("license_url")),
        "credit": _bounded_text(value.get("credit"), "rights.credit", maximum=500),
    }


def _external_snapshot(value: Any, source: Mapping[str, Any]) -> dict[str, Any] | None:
    required = str(source.get("kind") or "").casefold() in {"themealdb", "wikibooks"}
    if value is None and not required:
        return None
    if not isinstance(value, Mapping):
        raise RecipeError("external_snapshot metadata is required")
    fetched_at = _bounded_text(value.get("fetched_at"), "external_snapshot.fetched_at", required=True, maximum=100)
    try:
        parsed_at = datetime.fromisoformat(fetched_at)
    except ValueError as exc:
        raise RecipeError("external_snapshot.fetched_at must be an ISO timestamp") from exc
    if parsed_at.tzinfo is None:
        raise RecipeError("external_snapshot.fetched_at must include a timezone")
    content_hash = _bounded_text(value.get("content_hash"), "external_snapshot.content_hash", required=True, maximum=64)
    if re.fullmatch(r"[a-f0-9]{64}", content_hash or "") is None:
        raise RecipeError("external_snapshot.content_hash must be a SHA-256 digest")
    return {
        "fetched_at": fetched_at,
        "content_hash": content_hash,
        "source_revision_id": _bounded_text(
            value.get("source_revision_id"), "external_snapshot.source_revision_id", maximum=200,
        ),
        "permanent_url": normalize_source_url(value.get("permanent_url")),
        "changes": _bounded_text(value.get("changes"), "external_snapshot.changes", required=required, maximum=1_000),
    }


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return format(value, ".15g")


def _ingredient(value: Any, index: int) -> dict[str, Any]:
    if isinstance(value, str):
        text = _bounded_text(value, f"ingredients[{index}]", required=True, maximum=500)
        return {"raw": text, "item": text, "quantity": None, "unit": None, "scalable": False, "notes": None, "optional": False, "pantry": False}
    if not isinstance(value, Mapping):
        raise RecipeError(f"ingredients[{index}] must be text or an object")
    raw = _bounded_text(value.get("raw"), f"ingredients[{index}].raw", maximum=500)
    supplied_amount = _bounded_text(value.get("amount"), f"ingredients[{index}].amount", maximum=100)
    item = _bounded_text(value.get("item") or value.get("name"), f"ingredients[{index}].item", required=True, maximum=300)
    quantity = value.get("quantity")
    unit = _bounded_text(value.get("unit"), f"ingredients[{index}].unit", maximum=40)
    scalable = value.get("scalable", quantity is not None)
    if not isinstance(scalable, bool):
        raise RecipeError(f"ingredients[{index}].scalable must be true or false")
    if scalable:
        quantity = _finite_positive(quantity, f"ingredients[{index}].quantity")
        if not unit:
            raise RecipeError(f"ingredients[{index}].unit is required when scalable")
    elif quantity is not None:
        quantity = _finite_positive(quantity, f"ingredients[{index}].quantity")
    optional = value.get("optional", False)
    pantry = value.get("pantry", False)
    if not isinstance(optional, bool) or not isinstance(pantry, bool):
        raise RecipeError(f"ingredients[{index}].optional and pantry must be true or false")
    if scalable:
        display = " ".join((_format_number(quantity), unit, item))
    else:
        display = raw or " ".join(part for part in (supplied_amount, item) if part) or item
    result = {
        "raw": display,
        "item": item,
        "quantity": quantity,
        "unit": unit,
        "scalable": scalable,
        "notes": _bounded_text(value.get("notes"), f"ingredients[{index}].notes", maximum=500),
        "optional": optional,
        "pantry": pantry,
    }
    if isinstance(quantity, float) and unit:
        result["amount"] = f"{_format_number(quantity)} {unit}"
    elif supplied_amount:
        result["amount"] = supplied_amount
    return result


def normalize_recipe(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RecipeError("recipe must be an object")
    _check_shape(value)
    cleaned = {key: child for key, child in value.items() if key not in SERVER_FIELDS and key != "recipe_ref"}
    source = _source(cleaned.get("source"))
    rights = _rights(cleaned.get("rights"))
    name = _bounded_text(cleaned.get("name"), "name", required=True, maximum=300)
    tags = cleaned.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 50:
        raise RecipeError("tags must be a list with at most 50 values")
    normalized_tags = [_bounded_text(item, f"tags[{index}]", required=True, maximum=80) for index, item in enumerate(tags)]
    result: dict[str, Any] = {
        "schema_version": 1,
        "name": name,
        "language": _bounded_text(cleaned.get("language") or "nb-NO", "language", required=True, maximum=20),
        "tags": normalized_tags,
        "source": source,
        "rights": rights,
        "notes": _bounded_text(cleaned.get("notes"), "notes", maximum=MAX_TEXT),
    }
    external_snapshot = _external_snapshot(cleaned.get("external_snapshot"), source)
    if external_snapshot is not None:
        result["external_snapshot"] = external_snapshot
    if rights["storage"] == "link_only":
        if not source.get("url"):
            raise RecipeError("link_only recipes require a source URL")
        if cleaned.get("ingredients") or cleaned.get("steps"):
            raise RecipeError("link_only recipes cannot store ingredients or steps")
        result.update({"portions": None, "ingredients": [], "steps": []})
    else:
        host = urlsplit(source.get("url") or "").hostname
        normalized_host = str(host or "").casefold().rstrip(".")
        publisher = _normalized_text(source.get("publisher"))
        publisher_domain = publisher.removeprefix("www.").rstrip(".")
        kind = _normalized_text(source.get("kind"))
        kind_domain = kind.removeprefix("www.").rstrip(".")
        restricted_source = (
            any(normalized_host == domain or normalized_host.endswith(f".{domain}") for domain in ("meny.no", "oda.com"))
            or re.match(r"^(meny|oda)(?:\b|[._-])", publisher) is not None
            or publisher_domain in {"meny.no", "oda.com"}
            or re.match(r"^(meny|oda)(?:\b|[._-])", kind) is not None
            or kind_domain in {"meny.no", "oda.com"}
        )
        if restricted_source and source["relationship"] not in {"adapted", "inspired_by"}:
            raise RecipeError("original Oda or MENY content is link_only; store only an adapted or inspired recipe as full")
        portions = None if cleaned.get("portions") is None else _finite_positive(cleaned.get("portions"), "portions")
        ingredients = cleaned.get("ingredients")
        steps = cleaned.get("steps")
        if not isinstance(ingredients, list) or not 1 <= len(ingredients) <= 200:
            raise RecipeError("ingredients must contain one to 200 entries")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            raise RecipeError("steps must contain one to 100 entries")
        result.update({
            "portions": portions,
            "ingredients": [_ingredient(item, index) for index, item in enumerate(ingredients)],
            "steps": [_bounded_text(item, f"steps[{index}]", required=True) for index, item in enumerate(steps)],
            "times": deepcopy(cleaned.get("times")) if isinstance(cleaned.get("times"), Mapping) else None,
            "storage": _bounded_text(cleaned.get("storage"), "storage", maximum=1_000),
            "reheating": _bounded_text(cleaned.get("reheating"), "reheating", maximum=1_000),
        })
    if len(_canonical(result).encode()) > MAX_RECIPE_BYTES:
        raise RecipeError("recipe is too large")
    return result


def _stored_recipe_document(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise RecipeError("recipe bank is unavailable") from exc
    if not isinstance(decoded, Mapping):
        raise RecipeError("recipe bank is unavailable")
    try:
        normalized = normalize_recipe(decoded)
    except RecipeError as exc:
        raise RecipeError("recipe bank is unavailable") from exc
    if _canonical(normalized) != _canonical(decoded):
        raise RecipeError("recipe bank is unavailable")
    return normalized


def source_key(recipe: Mapping[str, Any]) -> str | None:
    source = recipe.get("source") if isinstance(recipe.get("source"), Mapping) else {}
    external_id = _normalized_text(source.get("external_id"))
    publisher = _normalized_text(source.get("publisher"))
    if external_id and publisher:
        return f"{publisher}:{external_id}"
    url = source.get("url")
    return str(url) if url else None


def content_fingerprint(recipe: Mapping[str, Any]) -> str:
    ingredients = recipe.get("ingredients") if isinstance(recipe.get("ingredients"), list) else []
    identity = {
        "name": _normalized_text(recipe.get("name")),
        "ingredients": sorted(_normalized_text(item.get("item") if isinstance(item, Mapping) else item) for item in ingredients),
    }
    return _hash(identity)


def recipe_key(recipe: Mapping[str, Any]) -> str:
    if recipe.get("id"):
        return f"bank:{recipe['id']}"
    source = recipe.get("source") if isinstance(recipe.get("source"), Mapping) else {}
    if source.get("publisher") and source.get("external_id"):
        return f"source:{_normalized_text(source['publisher'])}:{_normalized_text(source['external_id'])}"
    if source.get("url"):
        return f"source-url:{source['url']}"
    return f"content:{content_fingerprint(recipe)}"


def scale_recipe(recipe: Mapping[str, Any], portions: Any | None = None) -> dict[str, Any]:
    result = deepcopy(dict(recipe))
    if (result.get("rights") or {}).get("storage") != "full":
        raise RecipeError("link_only recipes cannot be materialized into a menu")
    base = None if result.get("portions") is None else _finite_positive(result.get("portions"), "portions")
    if base is None and portions is not None:
        raise RecipeError("a recipe with unknown portions cannot be scaled")
    target = base if portions is None else _finite_positive(portions, "target portions")
    if base is None:
        result["shopping_requirements"] = [
            {
                "query": value.get("item"), "item": value.get("item"),
                "quantity": value.get("quantity"), "unit": value.get("unit"),
                "optional": value.get("optional", False), "pantry": value.get("pantry", False),
                "scalable": value.get("scalable") is True,
            }
            for value in result.get("ingredients", [])
        ]
        result["recipe_key"] = recipe_key(result)
        if result.get("id"):
            result["recipe_ref"] = {"id": result["id"], "revision": result["revision"]}
        return result
    factor = target / base
    if not math.isfinite(factor) or factor <= 0:
        raise RecipeError("portion scaling factor must be positive and finite")
    scaled = []
    requirements = []
    try:
        base_decimal = Decimal(str(base))
        target_decimal = Decimal(str(target))
    except InvalidOperation as exc:
        raise RecipeError("portion scaling factor must be positive and finite") from exc
    for value in result.get("ingredients", []):
        item = deepcopy(dict(value))
        if item.get("scalable") is True:
            source_quantity = _finite_positive(item.get("quantity"), "ingredient quantity")
            try:
                quantity = float(Decimal(str(source_quantity)) * target_decimal / base_decimal)
            except (InvalidOperation, OverflowError) as exc:
                raise RecipeError("scaled ingredient quantity must be positive and finite") from exc
            if not math.isfinite(quantity) or quantity <= 0:
                raise RecipeError("scaled ingredient quantity must be positive and finite")
            item["quantity"] = quantity
            item["amount"] = f"{_format_number(quantity)} {item['unit']}"
            item["raw"] = f"{item['amount']} {item['item']}"
        scaled.append(item)
        requirements.append({
            "query": item.get("item"),
            "item": item.get("item"),
            "quantity": item.get("quantity"),
            "unit": item.get("unit"),
            "optional": item.get("optional", False),
            "pantry": item.get("pantry", False),
            "scalable": item.get("scalable") is True,
        })
    result["ingredients"] = scaled
    result["portions"] = target
    result["scaled_from_portions"] = base
    result["recipe_key"] = recipe_key(result)
    if result.get("id"):
        result["recipe_ref"] = {"id": result["id"], "revision": result["revision"]}
    result["shopping_requirements"] = requirements
    return result


class RecipeStore:
    """Open SQLite only for recipe operations so provider paths stay independent."""

    def __init__(self, path: Path | str, household: str):
        self.path = Path(path)
        self.household = str(household)

    @staticmethod
    def _metadata(connection: sqlite3.Connection) -> dict[str, str] | None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if not exists:
            return None
        return dict(connection.execute(
            "SELECT key, value FROM metadata WHERE key IN ('household', 'schema_version', 'discovery_namespace')"
        ).fetchall())

    @staticmethod
    def _create_v1_schema(connection: sqlite3.Connection) -> None:
        statements = (
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            """CREATE TABLE recipes (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
                name TEXT NOT NULL, search_text TEXT NOT NULL, source_key TEXT,
                content_fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL,
                document TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                created_via TEXT NOT NULL)""",
            "CREATE UNIQUE INDEX recipes_source_key ON recipes(source_key) WHERE source_key IS NOT NULL",
            "CREATE INDEX recipes_search ON recipes(status, name)",
            "CREATE INDEX recipes_fingerprint ON recipes(content_fingerprint)",
            """CREATE TABLE revisions (
                recipe_id TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
                document TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (recipe_id, revision))""",
            """CREATE TABLE idempotency (
                key TEXT PRIMARY KEY, operation TEXT NOT NULL, request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
        )
        for statement in statements:
            connection.execute(statement)

    def _migrate_v1_to_v2(self, connection: sqlite3.Connection) -> None:
        namespace = secrets.token_urlsafe(12)
        connection.execute("""CREATE TABLE discovery_snapshots (
            discovery_ref TEXT PRIMARY KEY, snapshot_key TEXT NOT NULL UNIQUE, document TEXT NOT NULL,
            source_identity TEXT, content_hash TEXT NOT NULL, attribution_digest TEXT NOT NULL,
            created_at TEXT NOT NULL, renewed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            document_bytes INTEGER NOT NULL)""")
        connection.execute(
            "CREATE INDEX discovery_snapshots_expiry ON discovery_snapshots(expires_at)"
        )
        connection.execute("""CREATE TABLE discovery_bindings (
            destination TEXT NOT NULL, discovery_ref TEXT NOT NULL,
            snapshot_key TEXT NOT NULL, status TEXT NOT NULL,
            recipe_id TEXT, recipe_revision INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(destination, discovery_ref))""")
        connection.execute(
            "CREATE INDEX discovery_bindings_pin ON discovery_bindings(discovery_ref, status)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX discovery_bindings_builtin_revision "
            "ON discovery_bindings(destination, recipe_id, recipe_revision) "
            "WHERE destination='builtin' AND status='confirmed'"
        )
        connection.execute(
            "CREATE UNIQUE INDEX discovery_bindings_confirmed_snapshot "
            "ON discovery_bindings(destination, snapshot_key) WHERE status='confirmed'"
        )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('discovery_namespace', ?)", (namespace,)
        )
        cursor = connection.execute(
            "UPDATE metadata SET value='2' WHERE key='schema_version' AND value='1'"
        )
        if cursor.rowcount != 1:
            raise RecipeError("recipe bank migration could not advance schema version")

    def _migrate_v2_to_v3(self, connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE library_operations (
            operation_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('create','conditional_update','archive','delete','favorite','label','migration')),
            library_id TEXT NOT NULL,
            discovery_ref TEXT,
            target_recipe_id TEXT,
            request_digest TEXT NOT NULL,
            request_metadata TEXT,
            idempotency_key TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','confirmed','failed','uncertain')),
            source_identity TEXT,
            snapshot_digest TEXT,
            result_metadata TEXT,
            provider_recipe_id TEXT,
            provider_version TEXT,
            error_code TEXT,
            error_text TEXT,
            dispatched_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        connection.execute(
            "CREATE UNIQUE INDEX library_operations_discovery "
            "ON library_operations(library_id, discovery_ref, kind) WHERE discovery_ref IS NOT NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX library_operations_idempotency "
            "ON library_operations(library_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        connection.execute(
            "CREATE INDEX library_operations_status ON library_operations(status, updated_at)"
        )
        connection.execute(
            "CREATE INDEX library_operations_source ON library_operations(library_id, source_identity, snapshot_digest)"
        )
        connection.execute("""CREATE TABLE library_mappings (
            library_id TEXT NOT NULL,
            source_identity TEXT NOT NULL,
            snapshot_digest TEXT NOT NULL,
            recipe_id TEXT NOT NULL,
            version TEXT,
            operation_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(library_id, source_identity, snapshot_digest),
            UNIQUE(library_id, recipe_id),
            FOREIGN KEY(operation_id) REFERENCES library_operations(operation_id)
        )""")
        connection.execute("""CREATE TABLE library_connection_controls (
            library_id TEXT PRIMARY KEY,
            disabled_at TEXT NOT NULL
        )""")
        cursor = connection.execute(
            "UPDATE metadata SET value='3' WHERE key='schema_version' AND value='2'"
        )
        if cursor.rowcount != 1:
            raise RecipeError("recipe bank migration could not advance schema version")

    def _migrate_v3_to_v4(self, connection: sqlite3.Connection) -> None:
        connection.execute("""CREATE TABLE recipe_favorites (
            library_id TEXT NOT NULL CHECK(library_id = 'builtin'),
            recipe_id TEXT NOT NULL,
            is_favorite INTEGER NOT NULL CHECK(is_favorite IN (0, 1)),
            favorite_revision INTEGER NOT NULL CHECK(favorite_revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(library_id, recipe_id),
            FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
        )""")
        connection.execute(
            "CREATE INDEX recipe_favorites_state ON recipe_favorites(library_id, is_favorite)"
        )
        cursor = connection.execute(
            "UPDATE metadata SET value='4' WHERE key='schema_version' AND value='3'"
        )
        if cursor.rowcount != 1:
            raise RecipeError("recipe bank migration could not advance schema version")

    def _schema(self, connection: sqlite3.Connection) -> None:
        metadata = self._metadata(connection)
        if metadata is None:
            self._create_v1_schema(connection)
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES(?,?)",
                (("household", self.household), ("schema_version", "1")),
            )
            metadata = {"household": self.household, "schema_version": "1"}
        if metadata.get("household") not in {None, self.household}:
            raise RecipeError("recipe bank belongs to a different household")
        version = metadata.get("schema_version")
        if version is None:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', '1')"
            )
            version = "1"
        if version not in {"1", "2", "3", "4"}:
            raise RecipeError("recipe bank schema is newer than this meal concierge")
        row = connection.execute("SELECT value FROM metadata WHERE key='household'").fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('household', ?)", (self.household,)
            )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(recipes)")}
        if "created_via" not in columns:
            connection.execute(
                "ALTER TABLE recipes ADD COLUMN created_via TEXT NOT NULL DEFAULT 'hermes'"
            )
        if version == "1":
            self._migrate_v1_to_v2(connection)
            version = "2"
        if version == "2":
            self._migrate_v2_to_v3(connection)
            version = "3"
        if version == "3":
            self._migrate_v3_to_v4(connection)
        namespace = connection.execute(
            "SELECT value FROM metadata WHERE key='discovery_namespace'"
        ).fetchone()
        if namespace is None or re.fullmatch(r"[A-Za-z0-9_-]{16}", namespace[0]) is None:
            raise RecipeError("recipe bank discovery namespace is invalid")

    @staticmethod
    def _v1_digest(connection: sqlite3.Connection) -> str:
        digest = hashlib.sha256()
        for table, order in (
            ("metadata", "key"),
            ("recipes", "id"),
            ("revisions", "recipe_id, revision"),
            ("idempotency", "key"),
        ):
            digest.update(table.encode())
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update(_canonical(list(row)).encode())
        return digest.hexdigest()

    @staticmethod
    def _v2_digest(connection: sqlite3.Connection) -> str:
        digest = hashlib.sha256()
        for table, order in (
            ("metadata", "key"),
            ("recipes", "id"),
            ("revisions", "recipe_id, revision"),
            ("idempotency", "key"),
            ("discovery_snapshots", "discovery_ref"),
            ("discovery_bindings", "destination, discovery_ref"),
        ):
            digest.update(table.encode())
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update(_canonical(list(row)).encode())
        return digest.hexdigest()

    @staticmethod
    def _v3_digest(connection: sqlite3.Connection) -> str:
        digest = hashlib.sha256()
        for table, order in (
            ("metadata", "key"),
            ("recipes", "id"),
            ("revisions", "recipe_id, revision"),
            ("idempotency", "key"),
            ("discovery_snapshots", "discovery_ref"),
            ("discovery_bindings", "destination, discovery_ref"),
            ("library_operations", "operation_id"),
            ("library_mappings", "library_id, source_identity, snapshot_digest"),
            ("library_connection_controls", "library_id"),
        ):
            digest.update(table.encode())
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                digest.update(_canonical(list(row)).encode())
        return digest.hexdigest()

    def _validate_v1_backup(
        self, backup: Path, current: sqlite3.Connection
    ) -> None:
        if not backup.is_file():
            raise RecipeError("existing recipe v1 backup is invalid")
        try:
            source = sqlite3.connect(
                f"{backup.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0
            )
            try:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RecipeError("existing recipe v1 backup is invalid")
                metadata = dict(source.execute(
                    "SELECT key, value FROM metadata WHERE key IN ('household', 'schema_version')"
                ).fetchall())
                if (
                    metadata.get("household") != self.household
                    or metadata.get("schema_version") != "1"
                ):
                    raise RecipeError("existing recipe v1 backup is invalid")
                source.execute("SELECT 1 FROM recipes LIMIT 1").fetchone()
                source.execute("SELECT 1 FROM revisions LIMIT 1").fetchone()
                source.execute("SELECT 1 FROM idempotency LIMIT 1").fetchone()
                if self._v1_digest(source) != self._v1_digest(current):
                    raise RecipeError("existing recipe v1 backup is stale")
            finally:
                source.close()
        except sqlite3.Error as exc:
            raise RecipeError("existing recipe v1 backup is invalid") from exc
        if backup.stat().st_mode & 0o777 != 0o600:
            raise RecipeError("existing recipe v1 backup is not private")

    def _backup_v1(self, connection: sqlite3.Connection) -> None:
        backup = self.path.with_name("recipes-v1.backup.sqlite3")
        if backup.exists():
            self._validate_v1_backup(backup, connection)
            return
        created = False
        try:
            descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
            created = True
            source = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0
            )
            destination = sqlite3.connect(str(backup), timeout=2.0)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            os.chmod(backup, 0o600)
            self._validate_v1_backup(backup, connection)
        except Exception:
            if created and backup.exists():
                backup.unlink()
            raise

    def _validate_v2_backup(self, backup: Path, current: sqlite3.Connection) -> None:
        try:
            backup_status = backup.stat(follow_symlinks=False)
        except OSError as exc:
            raise RecipeError("existing recipe v2 backup is invalid") from exc
        if not stat.S_ISREG(backup_status.st_mode):
            raise RecipeError("existing recipe v2 backup is invalid")
        try:
            live_status = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise RecipeError("recipe bank is unavailable") from exc
        if (backup_status.st_dev, backup_status.st_ino) == (live_status.st_dev, live_status.st_ino):
            raise RecipeError("existing recipe v2 backup is invalid")
        try:
            source = sqlite3.connect(
                f"{backup.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0
            )
            try:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RecipeError("existing recipe v2 backup is invalid")
                metadata = dict(source.execute(
                    "SELECT key, value FROM metadata WHERE key IN ('household', 'schema_version')"
                ).fetchall())
                if metadata != {"household": self.household, "schema_version": "2"}:
                    raise RecipeError("existing recipe v2 backup is invalid")
                if self._v2_digest(source) != self._v2_digest(current):
                    raise RecipeError("existing recipe v2 backup is stale")
            finally:
                source.close()
        except sqlite3.Error as exc:
            raise RecipeError("existing recipe v2 backup is invalid") from exc
        if backup_status.st_mode & 0o777 != 0o600:
            raise RecipeError("existing recipe v2 backup is not private")

    def _backup_v2(self, connection: sqlite3.Connection) -> None:
        backup = self.path.with_name("recipes-v2.backup.sqlite3")
        try:
            backup.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            self._validate_v2_backup(backup, connection)
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".recipes-v2.backup.", suffix=".sqlite3", dir=backup.parent
        )
        temporary = Path(temporary_name)
        try:
            os.close(descriptor)
            source = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0
            )
            destination = sqlite3.connect(str(temporary), timeout=2.0)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            os.chmod(temporary, 0o600)
            self._validate_v2_backup(temporary, connection)
            with temporary.open("rb") as copied:
                os.fsync(copied.fileno())
            try:
                os.link(temporary, backup, follow_symlinks=False)
            except FileExistsError:
                self._validate_v2_backup(backup, connection)
            directory_descriptor = os.open(backup.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_v3_backup(self, backup: Path, current: sqlite3.Connection) -> None:
        try:
            backup_status = backup.stat(follow_symlinks=False)
        except OSError as exc:
            raise RecipeError("existing recipe v3 backup is invalid") from exc
        if not stat.S_ISREG(backup_status.st_mode):
            raise RecipeError("existing recipe v3 backup is invalid")
        try:
            live_status = self.path.stat(follow_symlinks=False)
        except OSError as exc:
            raise RecipeError("recipe bank is unavailable") from exc
        if (backup_status.st_dev, backup_status.st_ino) == (live_status.st_dev, live_status.st_ino):
            raise RecipeError("existing recipe v3 backup is invalid")
        try:
            source = sqlite3.connect(
                f"{backup.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0
            )
            try:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RecipeError("existing recipe v3 backup is invalid")
                metadata = dict(source.execute(
                    "SELECT key, value FROM metadata WHERE key IN ('household', 'schema_version')"
                ).fetchall())
                if metadata != {"household": self.household, "schema_version": "3"}:
                    raise RecipeError("existing recipe v3 backup is invalid")
                if self._v3_digest(source) != self._v3_digest(current):
                    raise RecipeError("existing recipe v3 backup is stale")
            finally:
                source.close()
        except sqlite3.Error as exc:
            raise RecipeError("existing recipe v3 backup is invalid") from exc
        if backup_status.st_mode & 0o777 != 0o600:
            raise RecipeError("existing recipe v3 backup is not private")

    def _backup_v3(self, connection: sqlite3.Connection) -> None:
        backup = self.path.with_name("recipes-v3.backup.sqlite3")
        try:
            backup.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            self._validate_v3_backup(backup, connection)
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".recipes-v3.backup.", suffix=".sqlite3", dir=backup.parent
        )
        temporary = Path(temporary_name)
        try:
            os.close(descriptor)
            source = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0
            )
            destination = sqlite3.connect(str(temporary), timeout=2.0)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            os.chmod(temporary, 0o600)
            self._validate_v3_backup(temporary, connection)
            with temporary.open("rb") as copied:
                os.fsync(copied.fileno())
            try:
                os.link(temporary, backup, follow_symlinks=False)
            except FileExistsError:
                self._validate_v3_backup(backup, connection)
            directory_descriptor = os.open(backup.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _connect(self, target: str | None = None) -> sqlite3.Connection:
        if target is None:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            existed = self.path.exists()
            connection = sqlite3.connect(str(self.path), timeout=2.0)
            os.chmod(self.path, 0o600)
        else:
            connection = sqlite3.connect(target, timeout=2.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            metadata = self._metadata(connection)
            if metadata is not None:
                if metadata.get("household") not in {None, self.household}:
                    raise RecipeError("recipe bank belongs to a different household")
                if metadata.get("schema_version") not in {None, "1", "2", "3", "4"}:
                    raise RecipeError("recipe bank schema is newer than this meal concierge")
            recipes_exist = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recipes'"
            ).fetchone()
            nonempty_v1 = (
                target is None
                and existed
                and metadata is not None
                and metadata.get("schema_version") in {None, "1"}
                and recipes_exist is not None
                and connection.execute("SELECT 1 FROM recipes LIMIT 1").fetchone() is not None
            )
            if nonempty_v1:
                self._backup_v1(connection)
            nonempty_v2 = (
                target is None
                and existed
                and metadata is not None
                and metadata.get("schema_version") == "2"
                and any(
                    connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                    for table in (
                        "recipes", "revisions", "idempotency",
                        "discovery_snapshots", "discovery_bindings",
                    )
                )
            )
            if nonempty_v2:
                self._backup_v2(connection)
            nonempty_v3 = (
                target is None
                and existed
                and metadata is not None
                and metadata.get("schema_version") == "3"
                and any(
                    connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                    for table in (
                        "recipes", "revisions", "idempotency",
                        "discovery_snapshots", "discovery_bindings",
                        "library_operations", "library_mappings", "library_connection_controls",
                    )
                )
            )
            if nonempty_v3:
                self._backup_v3(connection)
            self._schema(connection)
            self._cleanup_library_data(connection)
            self._cleanup_discoveries(connection)
            connection.commit()
            return connection
        except Exception:
            connection.rollback()
            connection.close()
            raise

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _idempotency_key(value: Any) -> str | None:
        if value is None or value == "":
            return None
        return _bounded_text(value, "idempotency_key", required=True, maximum=200)

    @staticmethod
    def _search_text(recipe: Mapping[str, Any]) -> str:
        source = recipe.get("source") if isinstance(recipe.get("source"), Mapping) else {}
        return " ".join(_normalized_text(value) for value in [recipe.get("name"), *(recipe.get("tags") or []), source.get("publisher"), source.get("author")] if value)

    @staticmethod
    def _favorite_state(connection: sqlite3.Connection, recipe_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT is_favorite, favorite_revision, created_at, updated_at "
            "FROM recipe_favorites WHERE library_id='builtin' AND recipe_id=?",
            (recipe_id,),
        ).fetchone()
        if row is None:
            return {
                "library_id": "builtin",
                "is_favorite": False,
                "favorite_revision": 0,
                "favorite_created_at": None,
                "favorite_updated_at": None,
            }
        return {
            "library_id": "builtin",
            "is_favorite": bool(row["is_favorite"]),
            "favorite_revision": row["favorite_revision"],
            "favorite_created_at": row["created_at"],
            "favorite_updated_at": row["updated_at"],
        }

    @classmethod
    def _record(
        cls,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        created: bool | None = None,
    ) -> dict[str, Any]:
        result = _stored_recipe_document(row["document"])
        result.update({
            "id": row["id"], "revision": row["revision"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "created_via": row["created_via"],
            "content_fingerprint": row["content_fingerprint"], "recipe_key": f"bank:{row['id']}",
            "library_recipe_ref": {
                "library_id": "builtin", "recipe_id": row["id"], "version": str(row["revision"]),
            },
        })
        favorite = cls._favorite_state(connection, row["id"])
        result.update({key: favorite[key] for key in ("library_id", "is_favorite", "favorite_revision")})
        if created is not None:
            result["created"] = created
        return result

    def _idem(self, connection: sqlite3.Connection, key: str | None, operation: str, request_hash: str) -> dict[str, Any] | None:
        if not key:
            return None
        row = connection.execute("SELECT operation, request_hash, response_json FROM idempotency WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            raise RecipeError("idempotency key was already used with different content")
        try:
            decoded = json.loads(row["response_json"])
        except (json.JSONDecodeError, TypeError, RecursionError) as exc:
            raise RecipeError("recipe bank is unavailable") from exc
        if not isinstance(decoded, Mapping):
            raise RecipeError("recipe bank is unavailable")
        result = dict(decoded)
        reference = result.get("library_recipe_ref")
        result_recipe_id = result.get("id")
        if isinstance(reference, Mapping) and reference.get("library_id") == "builtin":
            result_recipe_id = reference.get("recipe_id")
        if isinstance(result_recipe_id, str) and connection.execute(
            "SELECT 1 FROM recipes WHERE id=?", (result_recipe_id,)
        ).fetchone() is None:
            raise RecipeError("idempotency key belongs to a permanently deleted recipe")
        result["idempotent"] = True
        result["created"] = False
        return result

    @staticmethod
    def _store_idem(connection: sqlite3.Connection, key: str | None, operation: str, request_hash: str, response: Mapping[str, Any]) -> None:
        if key:
            connection.execute(
                "INSERT INTO idempotency(key, operation, request_hash, response_json, created_at) VALUES(?,?,?,?,?)",
                (key, operation, request_hash, _canonical(response), _now()),
            )

    @staticmethod
    def _snapshot_parts(recipe: Mapping[str, Any]) -> tuple[dict[str, Any], str, str, str]:
        document = normalize_recipe(recipe)
        source_identity = source_key(document)
        if not source_identity:
            raise RecipeError("discovered recipes require an exact source identity")
        stable_document = deepcopy(document)
        snapshot = stable_document.get("external_snapshot")
        if isinstance(snapshot, dict):
            snapshot.pop("fetched_at", None)
        content_hash = _hash({
            key: value for key, value in stable_document.items()
            if key not in {"source", "rights", "external_snapshot"}
        })
        attribution_digest = _hash({
            "source": stable_document.get("source"),
            "rights": stable_document.get("rights"),
            "external_snapshot": stable_document.get("external_snapshot"),
        })
        snapshot_key = _hash({
            "source_identity": source_identity,
            "content_hash": content_hash,
            "attribution_digest": attribution_digest,
        })
        return document, snapshot_key, content_hash, attribution_digest

    @staticmethod
    def _expiry(current: datetime | None = None) -> str:
        return ((current or datetime.now(timezone.utc)) + timedelta(days=DISCOVERY_TTL_DAYS)).isoformat()

    @staticmethod
    def _destination(value: Any) -> str:
        destination = _bounded_text(value, "discovery destination", required=True, maximum=63)
        if DISCOVERY_DESTINATION_PATTERN.fullmatch(destination or "") is None:
            raise RecipeError("discovery destination is invalid")
        return destination

    def _discovery_ref(self, connection: sqlite3.Connection, value: Any) -> str:
        ref = _bounded_text(value, "discovery_ref", required=True, maximum=200)
        match = re.fullmatch(
            r"discovery:v1:([A-Za-z0-9_-]{16}):([A-Za-z0-9_-]{16,64})", ref or ""
        )
        namespace = connection.execute(
            "SELECT value FROM metadata WHERE key='discovery_namespace'"
        ).fetchone()
        if match is None or namespace is None or match.group(1) != namespace[0]:
            raise RecipeError("discovery reference was not found")
        return ref

    def _resolved_snapshot(self, connection: sqlite3.Connection, ref: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM discovery_snapshots WHERE discovery_ref=?", (ref,)
        ).fetchone()
        active_pin = connection.execute(
            "SELECT 1 FROM discovery_bindings WHERE discovery_ref=? AND status IN ('pending','uncertain') "
            "UNION ALL SELECT 1 FROM library_operations WHERE discovery_ref=? "
            "AND status IN ('pending','uncertain') LIMIT 1",
            (ref, ref),
        ).fetchone()
        if row is None or (row["expires_at"] <= _now() and active_pin is None):
            raise RecipeError("discovery reference was not found")
        recipe = _stored_recipe_document(row["document"])
        _, snapshot_key, content_hash, attribution_digest = self._snapshot_parts(recipe)
        if (
            snapshot_key != row["snapshot_key"]
            or content_hash != row["content_hash"]
            or attribution_digest != row["attribution_digest"]
        ):
            raise RecipeError("recipe bank is unavailable")
        return {
            "recipe": recipe,
            "discovery_ref": row["discovery_ref"],
            "content_hash": row["content_hash"],
            "attribution_digest": row["attribution_digest"],
            "expires_at": row["expires_at"],
        }

    @staticmethod
    def _library_operation(row: sqlite3.Row, *, created: bool = False, claimed: bool = False) -> dict[str, Any]:
        reference = None
        try:
            request_metadata = json.loads(row["request_metadata"]) if row["request_metadata"] else {}
            if not isinstance(request_metadata, Mapping) or request_metadata.get("status") not in {"active", "draft"}:
                raise RecipeError("recipe library journal is unavailable")
            if row["result_metadata"]:
                reference = validate_library_recipe_ref(json.loads(row["result_metadata"]))
        except (json.JSONDecodeError, TypeError, RecipeLibraryError) as exc:
            raise RecipeError("recipe library journal is unavailable") from exc
        result = {
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "library_id": row["library_id"],
            "discovery_ref": row["discovery_ref"],
            "target_recipe_id": row["target_recipe_id"],
            "request_digest": row["request_digest"],
            "idempotency_key": row["idempotency_key"],
            "requested_status": request_metadata["status"],
            "status": row["status"],
            "source_identity": row["source_identity"],
            "snapshot_digest": row["snapshot_digest"],
            "library_recipe_ref": reference,
            "error_code": row["error_code"],
            "error": row["error_text"],
            "dispatched_at": row["dispatched_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "created": created,
            "claimed": claimed,
        }
        return result

    @staticmethod
    def _favorite_operation(
        row: sqlite3.Row, *, created: bool = False, claimed: bool = False
    ) -> dict[str, Any]:
        try:
            request_metadata = json.loads(row["request_metadata"])
            if (
                not isinstance(request_metadata, Mapping)
                or set(request_metadata) - {"is_favorite", "expected_favorite_revision"}
                or not isinstance(request_metadata.get("is_favorite"), bool)
            ):
                raise RecipeError("recipe library journal is unavailable")
            expected = _favorite_revision(
                request_metadata.get("expected_favorite_revision"),
                "expected_favorite_revision",
            )
            reference = validate_library_recipe_ref({
                "library_id": row["library_id"],
                "recipe_id": row["target_recipe_id"],
                "version": row["provider_version"],
            })
            favorite_result = None
            if row["result_metadata"]:
                raw_result = json.loads(row["result_metadata"])
                if (
                    not isinstance(raw_result, Mapping)
                    or set(raw_result) - {
                        "library_id", "library_recipe_ref", "is_favorite",
                        "favorite_revision", "idempotent", "reconciled",
                    }
                    or raw_result.get("library_id") != row["library_id"]
                    or not isinstance(raw_result.get("is_favorite"), bool)
                ):
                    raise RecipeError("recipe library journal is unavailable")
                returned = validate_library_recipe_ref(
                    raw_result.get("library_recipe_ref")
                )
                if (
                    returned["library_id"] != row["library_id"]
                    or returned["recipe_id"] != row["target_recipe_id"]
                ):
                    raise RecipeError("recipe library journal is unavailable")
                favorite_result = dict(raw_result)
                favorite_result["library_recipe_ref"] = returned
                if "favorite_revision" in favorite_result:
                    favorite_result["favorite_revision"] = _favorite_revision(
                        favorite_result["favorite_revision"], "favorite_revision"
                    )
                for name in ("idempotent", "reconciled"):
                    if name in favorite_result and not isinstance(
                        favorite_result[name], bool
                    ):
                        raise RecipeError("recipe library journal is unavailable")
            if row["status"] == "confirmed" and favorite_result is None:
                raise RecipeError("recipe library journal is unavailable")
        except (json.JSONDecodeError, TypeError, RecipeLibraryError) as exc:
            raise RecipeError("recipe library journal is unavailable") from exc
        return {
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "library_id": row["library_id"],
            "target_recipe_id": row["target_recipe_id"],
            "request_digest": row["request_digest"],
            "idempotency_key": row["idempotency_key"],
            "requested_is_favorite": request_metadata["is_favorite"],
            "expected_favorite_revision": expected,
            "status": row["status"],
            "library_recipe_ref": reference,
            "result": favorite_result,
            "error_code": row["error_code"],
            "error": row["error_text"],
            "dispatched_at": row["dispatched_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "created": created,
            "claimed": claimed,
        }

    @staticmethod
    def _label_operation(
        row: sqlite3.Row, *, created: bool = False, claimed: bool = False
    ) -> dict[str, Any]:
        try:
            metadata = json.loads(row["request_metadata"])
            if not isinstance(metadata, Mapping) or metadata.get("action") not in {
                "create",
                "apply",
                "remove",
            }:
                raise RecipeError("recipe library journal is unavailable")
            action = metadata["action"]
            label_ref = None
            recipe_ref = None
            name = None
            normalized_name = None
            expected = None
            if action == "create":
                if set(metadata) != {"action", "name", "normalized_name"}:
                    raise RecipeError("recipe library journal is unavailable")
                name, normalized_name = normalize_label_name(metadata.get("name"))
                if normalized_name != metadata.get("normalized_name") or row["target_recipe_id"] is not None:
                    raise RecipeError("recipe library journal is unavailable")
            else:
                if set(metadata) - {
                    "action",
                    "library_label_ref",
                    "expected_label_revision",
                }:
                    raise RecipeError("recipe library journal is unavailable")
                label_ref = validate_library_label_ref(metadata.get("library_label_ref"))
                if label_ref["library_id"] != row["library_id"]:
                    raise RecipeError("recipe library journal is unavailable")
                recipe_ref = validate_library_recipe_ref({
                    "library_id": row["library_id"],
                    "recipe_id": row["target_recipe_id"],
                    "version": row["provider_version"],
                })
                expected = _favorite_revision(
                    metadata.get("expected_label_revision"),
                    "expected_label_revision",
                )
            label_result = None
            if row["result_metadata"]:
                raw_result = json.loads(row["result_metadata"])
                if (
                    not isinstance(raw_result, Mapping)
                    or set(raw_result) - {
                        "library_id",
                        "library_label_ref",
                        "name",
                        "normalized_name",
                        "library_recipe_ref",
                        "present",
                        "idempotent",
                        "reconciled",
                    }
                    or raw_result.get("library_id") != row["library_id"]
                ):
                    raise RecipeError("recipe library journal is unavailable")
                returned_label = validate_library_label_ref(
                    raw_result.get("library_label_ref")
                )
                if returned_label["library_id"] != row["library_id"]:
                    raise RecipeError("recipe library journal is unavailable")
                returned_name, returned_normalized = normalize_label_name(
                    raw_result.get("name")
                )
                if returned_normalized != raw_result.get("normalized_name"):
                    raise RecipeError("recipe library journal is unavailable")
                label_result = dict(raw_result)
                label_result.update({
                    "library_label_ref": returned_label,
                    "name": returned_name,
                    "normalized_name": returned_normalized,
                })
                if action == "create":
                    if returned_normalized != normalized_name or any(
                        key in label_result for key in ("library_recipe_ref", "present")
                    ):
                        raise RecipeError("recipe library journal is unavailable")
                else:
                    returned_recipe = validate_library_recipe_ref(
                        raw_result.get("library_recipe_ref")
                    )
                    if (
                        returned_recipe["library_id"] != row["library_id"]
                        or returned_recipe["recipe_id"] != row["target_recipe_id"]
                        or returned_label["label_id"] != label_ref["label_id"]
                        or raw_result.get("present") != (action == "apply")
                    ):
                        raise RecipeError("recipe library journal is unavailable")
                    label_result["library_recipe_ref"] = returned_recipe
                    if not isinstance(label_result.get("present"), bool):
                        raise RecipeError("recipe library journal is unavailable")
                for key in ("idempotent", "reconciled"):
                    if key in label_result and not isinstance(label_result[key], bool):
                        raise RecipeError("recipe library journal is unavailable")
            if row["status"] == "confirmed" and label_result is None:
                raise RecipeError("recipe library journal is unavailable")
        except (json.JSONDecodeError, TypeError, RecipeLibraryError) as exc:
            raise RecipeError("recipe library journal is unavailable") from exc
        return {
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "action": action,
            "library_id": row["library_id"],
            "target_recipe_id": row["target_recipe_id"],
            "request_digest": row["request_digest"],
            "idempotency_key": row["idempotency_key"],
            "library_recipe_ref": recipe_ref,
            "library_label_ref": label_ref,
            "name": name,
            "normalized_name": normalized_name,
            "expected_label_revision": expected,
            "status": row["status"],
            "result": label_result,
            "error_code": row["error_code"],
            "error": row["error_text"],
            "dispatched_at": row["dispatched_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "created": created,
            "claimed": claimed,
        }

    @staticmethod
    def _lifecycle_operation(
        row: sqlite3.Row, *, created: bool = False, claimed: bool = False
    ) -> dict[str, Any]:
        kind = row["kind"]
        if kind not in {"conditional_update", "archive", "delete"}:
            raise RecipeError("recipe library journal is unavailable")
        try:
            metadata = json.loads(row["request_metadata"])
            action = "update" if kind == "conditional_update" else kind
            if not isinstance(metadata, Mapping) or metadata.get("action") != action:
                raise RecipeError("recipe library journal is unavailable")
            reference = validate_library_recipe_ref({
                "library_id": row["library_id"],
                "recipe_id": row["target_recipe_id"],
                "version": row["provider_version"],
            })
            if "version" not in reference:
                raise RecipeError("recipe library journal is unavailable")
            provider_principal = _bounded_text(
                metadata.get("provider_principal"),
                "recipe library provider principal",
                required=True,
                maximum=300,
            )
            provider_binding = _bounded_text(
                metadata.get("provider_binding"),
                "recipe library provider binding",
                required=True,
                maximum=64,
            )
            if re.fullmatch(r"[a-f0-9]{64}", provider_binding or "") is None:
                raise RecipeError("recipe library journal is unavailable")
            replacement = None
            name = None
            current_archived = None
            requested_archived = None
            if kind == "conditional_update":
                if set(metadata) != {
                    "action", "provider_binding", "provider_principal",
                    "replacement",
                }:
                    raise RecipeError("recipe library journal is unavailable")
                replacement = normalize_recipe(metadata.get("replacement"))
            elif kind == "archive":
                if set(metadata) != {
                    "action", "provider_binding", "provider_principal", "name",
                    "current_archived", "requested_archived",
                }:
                    raise RecipeError("recipe library journal is unavailable")
                name = _bounded_text(
                    metadata.get("name"), "recipe library recipe name",
                    required=True, maximum=300,
                )
                current_archived = metadata.get("current_archived")
                requested_archived = metadata.get("requested_archived")
                if not isinstance(current_archived, bool) or not isinstance(
                    requested_archived, bool
                ):
                    raise RecipeError("recipe library journal is unavailable")
            else:
                if set(metadata) != {
                    "action", "provider_binding", "provider_principal", "name",
                }:
                    raise RecipeError("recipe library journal is unavailable")
                name = _bounded_text(
                    metadata.get("name"), "recipe library recipe name",
                    required=True, maximum=300,
                )
            if re.fullmatch(r"[a-f0-9]{64}", row["snapshot_digest"] or "") is None:
                raise RecipeError("recipe library journal is unavailable")
            result_metadata = None
            if row["result_metadata"]:
                raw_result = json.loads(row["result_metadata"])
                if not isinstance(raw_result, Mapping):
                    raise RecipeError("recipe library journal is unavailable")
                result_reference = validate_library_recipe_ref(
                    raw_result.get("library_recipe_ref")
                )
                if (
                    result_reference["library_id"] != row["library_id"]
                    or result_reference["recipe_id"] != row["target_recipe_id"]
                    or (
                        kind in {"conditional_update", "archive"}
                        and "version" not in result_reference
                    )
                ):
                    raise RecipeError("recipe library journal is unavailable")
                if kind == "conditional_update":
                    if set(raw_result) != {"library_recipe_ref", "updated"} or raw_result.get("updated") is not True:
                        raise RecipeError("recipe library journal is unavailable")
                elif kind == "archive":
                    if (
                        set(raw_result) != {"library_recipe_ref", "archived"}
                        or raw_result.get("archived") is not requested_archived
                    ):
                        raise RecipeError("recipe library journal is unavailable")
                elif set(raw_result) != {"library_recipe_ref", "deleted"} or raw_result.get("deleted") is not True:
                    raise RecipeError("recipe library journal is unavailable")
                result_metadata = dict(raw_result)
                result_metadata["library_recipe_ref"] = result_reference
            if row["status"] == "confirmed" and result_metadata is None:
                raise RecipeError("recipe library journal is unavailable")
            created_at = datetime.fromisoformat(row["created_at"])
            if created_at.tzinfo is None:
                raise ValueError
        except (
            json.JSONDecodeError, TypeError, ValueError, RecipeLibraryError,
        ) as exc:
            raise RecipeError("recipe library journal is unavailable") from exc
        result = {
            "operation_id": row["operation_id"],
            "confirmation_id": (
                row["operation_id"] if kind in {"archive", "delete"} else None
            ),
            "kind": kind,
            "action": action,
            "library_id": row["library_id"],
            "target_recipe_id": row["target_recipe_id"],
            "library_recipe_ref": reference,
            "request_digest": row["request_digest"],
            "idempotency_key": row["idempotency_key"],
            "snapshot_digest": row["snapshot_digest"],
            "provider_principal": provider_principal,
            "provider_binding": provider_binding,
            "replacement": replacement,
            "name": name,
            "current_archived": current_archived,
            "requested_archived": requested_archived,
            "status": row["status"],
            "result": result_metadata,
            "error_code": row["error_code"],
            "error": row["error_text"],
            "dispatched_at": row["dispatched_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "created": created,
            "claimed": claimed,
        }
        if kind in {"archive", "delete"}:
            result["expires_at"] = (
                created_at + LIBRARY_LIFECYCLE_CONFIRMATION_TTL
            ).isoformat()
        return result

    @classmethod
    def _operation(
        cls, row: sqlite3.Row, *, created: bool = False, claimed: bool = False
    ) -> dict[str, Any]:
        if row["kind"] == "favorite":
            return cls._favorite_operation(row, created=created, claimed=claimed)
        if row["kind"] == "label":
            return cls._label_operation(row, created=created, claimed=claimed)
        if row["kind"] in {"conditional_update", "archive", "delete"}:
            return cls._lifecycle_operation(row, created=created, claimed=claimed)
        return cls._library_operation(row, created=created, claimed=claimed)

    def _cleanup_library_data(self, connection: sqlite3.Connection) -> None:
        confirmation_cutoff = (
            datetime.now(timezone.utc) - LIBRARY_LIFECYCLE_CONFIRMATION_TTL
        ).isoformat()
        connection.execute(
            "UPDATE library_operations SET status='failed', "
            "error_code='confirmation_expired', "
            "error_text='recipe lifecycle confirmation expired', updated_at=? "
            "WHERE kind IN ('archive','delete') AND status='pending' "
            "AND dispatched_at IS NULL AND created_at <= ?",
            (_now(), confirmation_cutoff),
        )
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=FAILED_LIBRARY_OPERATION_TTL_DAYS)
        ).isoformat()
        connection.execute(
            "DELETE FROM library_operations WHERE status='failed' "
            "AND (error_code IS NULL OR error_code!='recipe_deleted') AND updated_at <= ?",
            (cutoff,),
        )

    def begin_library_create(
        self,
        discovery_ref: Any,
        library_id: Any,
        *,
        status: Any = "active",
        idempotency_key: Any = None,
    ) -> dict[str, Any]:
        ref = _bounded_text(discovery_ref, "discovery_ref", required=True, maximum=200)
        try:
            library = validate_library_id(library_id)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        if status not in {"active", "draft"}:
            raise RecipeError("recipe status must be active or draft")
        key = self._idempotency_key(idempotency_key)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                ref = self._discovery_ref(connection, ref)
                if connection.execute(
                    "SELECT 1 FROM library_connection_controls WHERE library_id=?", (library,)
                ).fetchone() is not None:
                    raise RecipeError("recipe library connection is disabled")
                if key is not None:
                    keyed = connection.execute(
                        "SELECT * FROM library_operations WHERE library_id=? AND idempotency_key=?",
                        (library, key),
                    ).fetchone()
                    if keyed is not None:
                        if keyed["error_code"] == "recipe_deleted":
                            raise RecipeError(
                                "idempotency key was already used for a permanently deleted recipe"
                            )
                        keyed_result = self._library_operation(keyed)
                        if keyed["kind"] != "create" or keyed_result["requested_status"] != status:
                            raise RecipeError(
                                "idempotency key was already used for another library operation"
                            )
                        if keyed["discovery_ref"] == ref:
                            return keyed_result
                        snapshot = connection.execute(
                            "SELECT snapshot_key, source_identity FROM discovery_snapshots "
                            "WHERE discovery_ref=?",
                            (ref,),
                        ).fetchone()
                        if snapshot is None:
                            raise RecipeError("discovery reference was not found")
                        request_digest = _hash({
                            "kind": "create",
                            "library_id": library,
                            "snapshot_digest": snapshot["snapshot_key"],
                            "status": status,
                        })
                        if (
                            keyed["request_digest"] != request_digest
                            or keyed["source_identity"] != snapshot["source_identity"]
                            or keyed["snapshot_digest"] != snapshot["snapshot_key"]
                        ):
                            raise RecipeError(
                                "idempotency key was already used for another library operation"
                            )
                        return keyed_result
                existing = connection.execute(
                    "SELECT * FROM library_operations WHERE library_id=? AND discovery_ref=? AND kind='create'",
                    (library, ref),
                ).fetchone()
                if existing is not None:
                    if existing["error_code"] == "recipe_deleted":
                        raise RecipeError(
                            "the external recipe previously saved from this discovery "
                            "reference was permanently deleted"
                        )
                    if key is not None:
                        raise RecipeError(
                            "discovery was already saved with a different idempotency key"
                        )
                    return self._library_operation(existing)
                resolved = self._resolved_snapshot(connection, ref)
                snapshot = connection.execute(
                    "SELECT snapshot_key, source_identity FROM discovery_snapshots WHERE discovery_ref=?",
                    (ref,),
                ).fetchone()
                if snapshot is None:
                    raise RecipeError("discovery reference was not found")
                request_digest = _hash({
                    "kind": "create",
                    "library_id": library,
                    "snapshot_digest": snapshot["snapshot_key"],
                    "status": status,
                })
                mapped = connection.execute(
                    "SELECT operation_id FROM library_mappings WHERE library_id=? "
                    "AND source_identity=? AND snapshot_digest=?",
                    (library, snapshot["source_identity"], snapshot["snapshot_key"]),
                ).fetchone()
                if mapped is not None:
                    prior = connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (mapped["operation_id"],),
                    ).fetchone()
                    if prior is None or prior["status"] != "confirmed":
                        raise RecipeError("recipe library journal is unavailable")
                    self._cleanup_library_data(connection)
                    if connection.execute("SELECT COUNT(*) FROM library_operations").fetchone()[0] >= MAX_LIBRARY_OPERATIONS:
                        raise RecipeError("recipe library operation journal is full")
                    operation_id = f"libop:v1:{secrets.token_urlsafe(18)}"
                    timestamp = _now()
                    connection.execute("""INSERT INTO library_operations(
                        operation_id, kind, library_id, discovery_ref, target_recipe_id,
                        request_digest, request_metadata, idempotency_key, status, source_identity, snapshot_digest,
                        result_metadata, provider_recipe_id, provider_version, error_code, error_text,
                        dispatched_at, created_at, updated_at
                    ) VALUES(?, 'create', ?, ?, NULL, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""", (
                        operation_id, library, ref, request_digest,
                        _canonical({"status": status}), key,
                        snapshot["source_identity"], snapshot["snapshot_key"], prior["result_metadata"],
                        prior["provider_recipe_id"], prior["provider_version"], prior["dispatched_at"],
                        timestamp, timestamp,
                    ))
                    return self._library_operation(connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?", (operation_id,)
                    ).fetchone())
                conflict = connection.execute(
                    "SELECT 1 FROM library_operations WHERE library_id=? AND source_identity=? "
                    "AND snapshot_digest != ? AND status IN ('pending','confirmed','uncertain') LIMIT 1",
                    (library, snapshot["source_identity"], snapshot["snapshot_key"]),
                ).fetchone()
                if library != "builtin" and conflict is not None:
                    raise RecipeError("source identity has different content in the selected recipe library")
                self._cleanup_library_data(connection)
                if connection.execute("SELECT COUNT(*) FROM library_operations").fetchone()[0] >= MAX_LIBRARY_OPERATIONS:
                    raise RecipeError("recipe library operation journal is full")
                reserved_mappings = connection.execute(
                    "SELECT COUNT(*) FROM library_mappings"
                ).fetchone()[0] + connection.execute(
                    "SELECT COUNT(*) FROM library_operations WHERE kind='create' "
                    "AND status IN ('pending','uncertain')"
                ).fetchone()[0]
                if reserved_mappings >= MAX_LIBRARY_MAPPINGS:
                    raise RecipeError("recipe library mapping journal is full")
                operation_id = f"libop:v1:{secrets.token_urlsafe(18)}"
                timestamp = _now()
                connection.execute("""INSERT INTO library_operations(
                    operation_id, kind, library_id, discovery_ref, target_recipe_id,
                    request_digest, request_metadata, idempotency_key, status, source_identity, snapshot_digest,
                    result_metadata, provider_recipe_id, provider_version, error_code, error_text,
                    dispatched_at, created_at, updated_at
                ) VALUES(?, 'create', ?, ?, NULL, ?, ?, ?, 'pending', ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""", (
                    operation_id, library, ref, request_digest, _canonical({"status": status}), key,
                    snapshot["source_identity"], snapshot["snapshot_key"], timestamp, timestamp,
                ))
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                result = self._library_operation(row, created=True)
                result["snapshot"] = resolved["recipe"]
                return result
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def begin_library_favorite(
        self,
        library_recipe_ref: Any,
        is_favorite: Any,
        *,
        expected_favorite_revision: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        try:
            reference = validate_library_recipe_ref(library_recipe_ref)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        if reference["library_id"] == "builtin":
            raise RecipeError("external favorite journal requires an external library")
        if not isinstance(is_favorite, bool):
            raise RecipeError("is_favorite must be true or false")
        expected = _favorite_revision(
            expected_favorite_revision, "expected_favorite_revision"
        )
        key = self._idempotency_key(idempotency_key)
        if key is None:
            raise RecipeError("idempotency_key is required")
        request_metadata = {"is_favorite": is_favorite}
        if expected is not None:
            request_metadata["expected_favorite_revision"] = expected
        request_digest = _hash({
            "kind": "favorite",
            "library_id": reference["library_id"],
            "recipe_id": reference["recipe_id"],
            **request_metadata,
        })
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM library_connection_controls WHERE library_id=?",
                    (reference["library_id"],),
                ).fetchone() is not None:
                    raise RecipeError("recipe library connection is disabled")
                if connection.execute(
                    "SELECT 1 FROM idempotency WHERE key=?", (key,)
                ).fetchone() is not None:
                    raise RecipeError(
                        "idempotency key was already used with different content"
                    )
                keyed = connection.execute(
                    "SELECT * FROM library_operations WHERE idempotency_key=?",
                    (key,),
                ).fetchall()
                if keyed:
                    if len(keyed) != 1:
                        raise RecipeError(
                            "idempotency key was already used with different content"
                        )
                    keyed = keyed[0]
                    if (
                        keyed["kind"] != "favorite"
                        or keyed["library_id"] != reference["library_id"]
                        or keyed["target_recipe_id"] != reference["recipe_id"]
                        or keyed["request_digest"] != request_digest
                    ):
                        raise RecipeError(
                            "idempotency key was already used for another library operation"
                        )
                    return self._favorite_operation(keyed)
                self._cleanup_library_data(connection)
                active = connection.execute(
                    "SELECT 1 FROM library_operations WHERE kind IN "
                    "('conditional_update','archive','delete','favorite','label') "
                    "AND library_id=? AND target_recipe_id=? "
                    "AND status IN ('pending','uncertain') LIMIT 1",
                    (reference["library_id"], reference["recipe_id"]),
                ).fetchone()
                if active is not None:
                    raise RecipeError(
                        "another favorite operation for this exact external recipe is "
                        "pending or uncertain; retry its original idempotency key"
                    )
                if connection.execute(
                    "SELECT COUNT(*) FROM library_operations"
                ).fetchone()[0] >= MAX_LIBRARY_OPERATIONS:
                    raise RecipeError("recipe library operation journal is full")
                operation_id = f"libop:v1:{secrets.token_urlsafe(18)}"
                timestamp = _now()
                connection.execute("""INSERT INTO library_operations(
                    operation_id, kind, library_id, discovery_ref, target_recipe_id,
                    request_digest, request_metadata, idempotency_key, status,
                    source_identity, snapshot_digest, result_metadata,
                    provider_recipe_id, provider_version, error_code, error_text,
                    dispatched_at, created_at, updated_at
                ) VALUES(?, 'favorite', ?, NULL, ?, ?, ?, ?, 'pending',
                    NULL, NULL, NULL, ?, ?, NULL, NULL, NULL, ?, ?)""", (
                    operation_id, reference["library_id"], reference["recipe_id"],
                    request_digest, _canonical(request_metadata), key,
                    reference["recipe_id"], reference.get("version"), timestamp,
                    timestamp,
                ))
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                return self._favorite_operation(row, created=True)
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def begin_library_label_create(
        self, library_id: Any, name: Any, *, idempotency_key: Any
    ) -> dict[str, Any]:
        try:
            library = validate_library_id(library_id, allow_builtin=False)
            display, normalized_name = normalize_label_name(name)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        key = self._idempotency_key(idempotency_key)
        if key is None:
            raise RecipeError("idempotency_key is required")
        metadata = {
            "action": "create",
            "name": display,
            "normalized_name": normalized_name,
        }
        digest = _hash({"kind": "label", "library_id": library, **metadata})
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM library_connection_controls WHERE library_id=?",
                    (library,),
                ).fetchone() is not None:
                    raise RecipeError("recipe library connection is disabled")
                if connection.execute(
                    "SELECT 1 FROM idempotency WHERE key=?", (key,)
                ).fetchone() is not None:
                    raise RecipeError(
                        "idempotency key was already used with different content"
                    )
                keyed = connection.execute(
                    "SELECT * FROM library_operations WHERE idempotency_key=?",
                    (key,),
                ).fetchall()
                if keyed:
                    if (
                        len(keyed) != 1
                        or keyed[0]["kind"] != "label"
                        or keyed[0]["library_id"] != library
                        or keyed[0]["request_digest"] != digest
                    ):
                        raise RecipeError(
                            "idempotency key was already used for another library operation"
                        )
                    return self._label_operation(keyed[0])
                self._cleanup_library_data(connection)
                if connection.execute(
                    "SELECT 1 FROM library_operations WHERE kind='label' "
                    "AND library_id=? AND target_recipe_id IS NULL AND source_identity=? "
                    "AND status IN ('pending','uncertain') LIMIT 1",
                    (library, normalized_name),
                ).fetchone() is not None:
                    raise RecipeError(
                        "another creation for this normalized label name is pending or uncertain"
                    )
                if connection.execute(
                    "SELECT COUNT(*) FROM library_operations"
                ).fetchone()[0] >= MAX_LIBRARY_OPERATIONS:
                    raise RecipeError("recipe library operation journal is full")
                operation_id = f"libop:v1:{secrets.token_urlsafe(18)}"
                timestamp = _now()
                connection.execute("""INSERT INTO library_operations(
                    operation_id, kind, library_id, discovery_ref, target_recipe_id,
                    request_digest, request_metadata, idempotency_key, status,
                    source_identity, snapshot_digest, result_metadata,
                    provider_recipe_id, provider_version, error_code, error_text,
                    dispatched_at, created_at, updated_at
                ) VALUES(?, 'label', ?, NULL, NULL, ?, ?, ?, 'pending', ?, NULL,
                    NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)""", (
                    operation_id,
                    library,
                    digest,
                    _canonical(metadata),
                    key,
                    normalized_name,
                    timestamp,
                    timestamp,
                ))
                return self._label_operation(
                    connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone(),
                    created=True,
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def begin_library_label_change(
        self,
        library_recipe_ref: Any,
        library_label_ref: Any,
        present: Any,
        *,
        expected_label_revision: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        try:
            recipe_ref = validate_library_recipe_ref(library_recipe_ref)
            label_ref = validate_library_label_ref(library_label_ref)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        if (
            recipe_ref["library_id"] == "builtin"
            or label_ref["library_id"] != recipe_ref["library_id"]
        ):
            raise RecipeError(
                "external label operation requires exact refs from one external library"
            )
        if not isinstance(present, bool):
            raise RecipeError("present must be true or false")
        expected = _favorite_revision(
            expected_label_revision, "expected_label_revision"
        )
        key = self._idempotency_key(idempotency_key)
        if key is None:
            raise RecipeError("idempotency_key is required")
        metadata: dict[str, Any] = {
            "action": "apply" if present else "remove",
            "library_label_ref": label_ref,
        }
        if expected is not None:
            metadata["expected_label_revision"] = expected
        digest = _hash({
            "kind": "label",
            "library_recipe_ref": recipe_ref,
            **metadata,
        })
        library = recipe_ref["library_id"]
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM library_connection_controls WHERE library_id=?",
                    (library,),
                ).fetchone() is not None:
                    raise RecipeError("recipe library connection is disabled")
                if connection.execute(
                    "SELECT 1 FROM idempotency WHERE key=?", (key,)
                ).fetchone() is not None:
                    raise RecipeError(
                        "idempotency key was already used with different content"
                    )
                keyed = connection.execute(
                    "SELECT * FROM library_operations WHERE idempotency_key=?",
                    (key,),
                ).fetchall()
                if keyed:
                    if (
                        len(keyed) != 1
                        or keyed[0]["kind"] != "label"
                        or keyed[0]["library_id"] != library
                        or keyed[0]["target_recipe_id"] != recipe_ref["recipe_id"]
                        or keyed[0]["request_digest"] != digest
                    ):
                        raise RecipeError(
                            "idempotency key was already used for another library operation"
                        )
                    return self._label_operation(keyed[0])
                self._cleanup_library_data(connection)
                if connection.execute(
                    "SELECT 1 FROM library_operations WHERE kind IN "
                    "('conditional_update','archive','delete','favorite','label') "
                    "AND library_id=? AND target_recipe_id=? "
                    "AND status IN ('pending','uncertain') LIMIT 1",
                    (library, recipe_ref["recipe_id"]),
                ).fetchone() is not None:
                    raise RecipeError(
                        "another label operation for this exact external recipe is "
                        "pending or uncertain; retry its original idempotency key"
                    )
                if connection.execute(
                    "SELECT COUNT(*) FROM library_operations"
                ).fetchone()[0] >= MAX_LIBRARY_OPERATIONS:
                    raise RecipeError("recipe library operation journal is full")
                operation_id = f"libop:v1:{secrets.token_urlsafe(18)}"
                timestamp = _now()
                connection.execute("""INSERT INTO library_operations(
                    operation_id, kind, library_id, discovery_ref, target_recipe_id,
                    request_digest, request_metadata, idempotency_key, status,
                    source_identity, snapshot_digest, result_metadata,
                    provider_recipe_id, provider_version, error_code, error_text,
                    dispatched_at, created_at, updated_at
                ) VALUES(?, 'label', ?, NULL, ?, ?, ?, ?, 'pending', ?, NULL,
                    NULL, ?, ?, NULL, NULL, NULL, ?, ?)""", (
                    operation_id,
                    library,
                    recipe_ref["recipe_id"],
                    digest,
                    _canonical(metadata),
                    key,
                    label_ref["label_id"],
                    recipe_ref["recipe_id"],
                    recipe_ref.get("version"),
                    timestamp,
                    timestamp,
                ))
                return self._label_operation(
                    connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone(),
                    created=True,
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def begin_library_conditional_update(
        self,
        library_recipe_ref: Any,
        replacement: Any,
        current_digest: Any,
        *,
        provider_binding: Any,
        provider_principal: Any,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        try:
            reference = validate_library_recipe_ref(library_recipe_ref)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        if reference["library_id"] == "builtin" or "version" not in reference:
            raise RecipeError(
                "external conditional update requires an exact versioned recipe reference"
            )
        document = normalize_recipe(replacement)
        digest = _bounded_text(
            current_digest, "recipe lifecycle snapshot digest", required=True,
            maximum=64,
        )
        if re.fullmatch(r"[a-f0-9]{64}", digest or "") is None:
            raise RecipeError("recipe lifecycle snapshot digest is invalid")
        key = self._idempotency_key(idempotency_key)
        if key is None:
            raise RecipeError("idempotency_key is required")
        principal = _bounded_text(
            provider_principal,
            "recipe library provider principal",
            required=True,
            maximum=300,
        )
        binding = _bounded_text(
            provider_binding,
            "recipe library provider binding",
            required=True,
            maximum=64,
        )
        if re.fullmatch(r"[a-f0-9]{64}", binding or "") is None:
            raise RecipeError("recipe library provider binding is invalid")
        metadata = {
            "action": "update",
            "provider_binding": binding,
            "provider_principal": principal,
            "replacement": document,
        }
        request_digest = _hash({
            "kind": "conditional_update",
            "library_recipe_ref": reference,
            "replacement": document,
        })
        library = reference["library_id"]
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM library_connection_controls WHERE library_id=?",
                    (library,),
                ).fetchone() is not None:
                    raise RecipeError("recipe library connection is disabled")
                if connection.execute(
                    "SELECT 1 FROM idempotency WHERE key=?", (key,)
                ).fetchone() is not None:
                    raise RecipeError(
                        "idempotency key was already used with different content"
                    )
                keyed = connection.execute(
                    "SELECT * FROM library_operations WHERE idempotency_key=?", (key,)
                ).fetchall()
                if keyed:
                    if (
                        len(keyed) != 1
                        or keyed[0]["kind"] != "conditional_update"
                        or keyed[0]["library_id"] != library
                        or keyed[0]["target_recipe_id"] != reference["recipe_id"]
                        or keyed[0]["request_digest"] != request_digest
                    ):
                        raise RecipeError(
                            "idempotency key was already used for another library operation"
                        )
                    return self._lifecycle_operation(keyed[0])
                self._cleanup_library_data(connection)
                active = connection.execute(
                    "SELECT 1 FROM library_operations WHERE library_id=? "
                    "AND target_recipe_id=? AND kind IN "
                    "('conditional_update','archive','delete','favorite','label') "
                    "AND status IN ('pending','uncertain') LIMIT 1",
                    (library, reference["recipe_id"]),
                ).fetchone()
                if active is not None:
                    raise RecipeError(
                        "another mutation for this exact external recipe is pending or "
                        "uncertain; retry its original operation"
                    )
                if connection.execute(
                    "SELECT COUNT(*) FROM library_operations"
                ).fetchone()[0] >= MAX_LIBRARY_OPERATIONS:
                    raise RecipeError("recipe library operation journal is full")
                operation_id = f"libop:v1:{secrets.token_urlsafe(18)}"
                timestamp = _now()
                connection.execute("""INSERT INTO library_operations(
                    operation_id, kind, library_id, discovery_ref, target_recipe_id,
                    request_digest, request_metadata, idempotency_key, status,
                    source_identity, snapshot_digest, result_metadata,
                    provider_recipe_id, provider_version, error_code, error_text,
                    dispatched_at, created_at, updated_at
                ) VALUES(?, 'conditional_update', ?, NULL, ?, ?, ?, ?, 'pending',
                    NULL, ?, NULL, ?, ?, NULL, NULL, NULL, ?, ?)""", (
                    operation_id, library, reference["recipe_id"], request_digest,
                    _canonical(metadata), key, digest, reference["recipe_id"],
                    reference["version"], timestamp, timestamp,
                ))
                return self._lifecycle_operation(
                    connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone(),
                    created=True,
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def prepare_library_lifecycle(
        self,
        kind: Any,
        library_recipe_ref: Any,
        name: Any,
        snapshot_digest: Any,
        *,
        provider_binding: Any,
        provider_principal: Any,
        current_archived: Any = None,
        requested_archived: Any = None,
    ) -> dict[str, Any]:
        if kind not in {"archive", "delete"}:
            raise RecipeError("recipe lifecycle prepare action is invalid")
        try:
            reference = validate_library_recipe_ref(library_recipe_ref)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        if reference["library_id"] == "builtin" or "version" not in reference:
            raise RecipeError(
                "external lifecycle prepare requires an exact versioned recipe reference"
            )
        recipe_name = _bounded_text(
            name, "recipe library recipe name", required=True, maximum=300
        )
        digest = _bounded_text(
            snapshot_digest, "recipe lifecycle snapshot digest", required=True,
            maximum=64,
        )
        if re.fullmatch(r"[a-f0-9]{64}", digest or "") is None:
            raise RecipeError("recipe lifecycle snapshot digest is invalid")
        principal = _bounded_text(
            provider_principal,
            "recipe library provider principal",
            required=True,
            maximum=300,
        )
        binding = _bounded_text(
            provider_binding,
            "recipe library provider binding",
            required=True,
            maximum=64,
        )
        if re.fullmatch(r"[a-f0-9]{64}", binding or "") is None:
            raise RecipeError("recipe library provider binding is invalid")
        metadata: dict[str, Any] = {
            "action": kind,
            "provider_binding": binding,
            "provider_principal": principal,
            "name": recipe_name,
        }
        if kind == "archive":
            if not isinstance(current_archived, bool) or not isinstance(
                requested_archived, bool
            ):
                raise RecipeError(
                    "archive prepare requires current and requested archive states"
                )
            metadata.update({
                "current_archived": current_archived,
                "requested_archived": requested_archived,
            })
        elif current_archived is not None or requested_archived is not None:
            raise RecipeError("delete prepare does not accept archive state")
        request_digest = _hash({
            "kind": kind,
            "library_recipe_ref": reference,
            "snapshot_digest": digest,
            **metadata,
        })
        library = reference["library_id"]
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM library_connection_controls WHERE library_id=?",
                    (library,),
                ).fetchone() is not None:
                    raise RecipeError("recipe library connection is disabled")
                self._cleanup_library_data(connection)
                active = connection.execute(
                    "SELECT * FROM library_operations WHERE library_id=? "
                    "AND target_recipe_id=? AND kind IN "
                    "('conditional_update','archive','delete','favorite','label') "
                    "AND status IN ('pending','uncertain') ORDER BY created_at LIMIT 1",
                    (library, reference["recipe_id"]),
                ).fetchone()
                if active is not None:
                    if (
                        active["kind"] == kind
                        and active["request_digest"] == request_digest
                        and active["idempotency_key"] is None
                        and active["dispatched_at"] is None
                    ):
                        return self._lifecycle_operation(active)
                    raise RecipeError(
                        "another mutation for this exact external recipe is pending or "
                        "uncertain; finish or expire it first"
                    )
                if connection.execute(
                    "SELECT COUNT(*) FROM library_operations"
                ).fetchone()[0] >= MAX_LIBRARY_OPERATIONS:
                    raise RecipeError("recipe library operation journal is full")
                operation_id = f"libop:v1:{secrets.token_urlsafe(18)}"
                timestamp = _now()
                connection.execute("""INSERT INTO library_operations(
                    operation_id, kind, library_id, discovery_ref, target_recipe_id,
                    request_digest, request_metadata, idempotency_key, status,
                    source_identity, snapshot_digest, result_metadata,
                    provider_recipe_id, provider_version, error_code, error_text,
                    dispatched_at, created_at, updated_at
                ) VALUES(?, ?, ?, NULL, ?, ?, ?, NULL, 'pending', NULL, ?, NULL,
                    ?, ?, NULL, NULL, NULL, ?, ?)""", (
                    operation_id, kind, library, reference["recipe_id"], request_digest,
                    _canonical(metadata), digest, reference["recipe_id"],
                    reference["version"], timestamp, timestamp,
                ))
                return self._lifecycle_operation(
                    connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (operation_id,),
                    ).fetchone(),
                    created=True,
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def confirm_library_lifecycle(
        self, confirmation_id: Any, *, idempotency_key: Any
    ) -> dict[str, Any]:
        operation = _bounded_text(
            confirmation_id, "confirmation_id", required=True, maximum=80
        )
        if re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", operation or "") is None:
            raise RecipeError("recipe lifecycle confirmation was not found")
        key = self._idempotency_key(idempotency_key)
        if key is None:
            raise RecipeError("idempotency_key is required")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation,)
                ).fetchone()
                if row is None or row["kind"] not in {"archive", "delete"}:
                    raise RecipeError("recipe lifecycle confirmation was not found")
                if row["idempotency_key"] is not None:
                    if row["idempotency_key"] != key:
                        raise RecipeError(
                            "recipe lifecycle confirmation already uses another "
                            "idempotency key"
                        )
                    return self._lifecycle_operation(row)
                created_at = datetime.fromisoformat(row["created_at"])
                if (
                    created_at.tzinfo is None
                    or datetime.now(timezone.utc)
                    >= created_at + LIBRARY_LIFECYCLE_CONFIRMATION_TTL
                ):
                    timestamp = _now()
                    connection.execute(
                        "UPDATE library_operations SET status='failed', "
                        "error_code='confirmation_expired', "
                        "error_text='recipe lifecycle confirmation expired', "
                        "updated_at=? WHERE operation_id=?",
                        (timestamp, operation),
                    )
                    return self._lifecycle_operation(
                        connection.execute(
                            "SELECT * FROM library_operations WHERE operation_id=?",
                            (operation,),
                        ).fetchone()
                    )
                if connection.execute(
                    "SELECT 1 FROM idempotency WHERE key=?", (key,)
                ).fetchone() is not None or connection.execute(
                    "SELECT 1 FROM library_operations WHERE idempotency_key=?", (key,)
                ).fetchone() is not None:
                    raise RecipeError(
                        "idempotency key was already used for another operation"
                    )
                timestamp = _now()
                connection.execute(
                    "UPDATE library_operations SET idempotency_key=?, updated_at=? "
                    "WHERE operation_id=? AND idempotency_key IS NULL",
                    (key, timestamp, operation),
                )
                return self._lifecycle_operation(
                    connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (operation,),
                    ).fetchone()
                )
        except (ValueError, TypeError) as exc:
            raise RecipeError("recipe lifecycle confirmation is invalid") from exc
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def finish_library_lifecycle(
        self,
        operation_id: Any,
        status: Any,
        *,
        result: Any = None,
        error_code: Any = None,
        error: Any = None,
    ) -> dict[str, Any]:
        operation = _bounded_text(
            operation_id, "operation_id", required=True, maximum=80
        )
        if re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", operation or "") is None:
            raise RecipeError("recipe lifecycle operation was not found")
        if status not in {"confirmed", "failed", "uncertain"}:
            raise RecipeError("recipe lifecycle operation status is invalid")
        lifecycle_result = None
        if result is not None:
            if not isinstance(result, Mapping):
                raise RecipeError("recipe lifecycle result is invalid")
            try:
                reference = validate_library_recipe_ref(
                    result.get("library_recipe_ref")
                )
            except RecipeLibraryError as exc:
                raise RecipeError(str(exc)) from exc
            lifecycle_result = dict(result)
            lifecycle_result["library_recipe_ref"] = reference
        if (status == "confirmed") != (lifecycle_result is not None):
            raise RecipeError(
                "confirmed recipe lifecycle operation requires exactly one result"
            )
        code = _bounded_text(
            error_code, "recipe library error_code", maximum=80
        )
        if code is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
            raise RecipeError("recipe library error_code is invalid")
        error_text = _bounded_text(error, "recipe library error", maximum=500)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation,)
                ).fetchone()
                if row is None or row["kind"] not in {
                    "conditional_update", "archive", "delete",
                }:
                    raise RecipeError("recipe lifecycle operation was not found")
                if row["status"] in {"confirmed", "failed"}:
                    return self._lifecycle_operation(row)
                if lifecycle_result is not None:
                    reference = lifecycle_result["library_recipe_ref"]
                    if (
                        reference["library_id"] != row["library_id"]
                        or reference["recipe_id"] != row["target_recipe_id"]
                        or (
                            row["kind"] in {"conditional_update", "archive"}
                            and "version" not in reference
                        )
                    ):
                        raise RecipeError(
                            "recipe lifecycle result does not match the bound target"
                        )
                    expected_keys = (
                        {"library_recipe_ref", "updated"}
                        if row["kind"] == "conditional_update"
                        else {"library_recipe_ref", "archived"}
                        if row["kind"] == "archive"
                        else {"library_recipe_ref", "deleted"}
                    )
                    expected_value = (
                        lifecycle_result.get("updated") is True
                        if row["kind"] == "conditional_update"
                        else isinstance(lifecycle_result.get("archived"), bool)
                        if row["kind"] == "archive"
                        else lifecycle_result.get("deleted") is True
                    )
                    if set(lifecycle_result) != expected_keys or not expected_value:
                        raise RecipeError("recipe lifecycle result is invalid")
                    if row["kind"] == "archive":
                        metadata = json.loads(row["request_metadata"])
                        if lifecycle_result["archived"] != metadata["requested_archived"]:
                            raise RecipeError("recipe lifecycle result is invalid")
                timestamp = _now()
                provider_version = (
                    lifecycle_result["library_recipe_ref"].get("version")
                    if lifecycle_result is not None
                    else row["provider_version"]
                )
                connection.execute("""UPDATE library_operations SET
                    status=?, result_metadata=?, error_code=?, error_text=?,
                    updated_at=? WHERE operation_id=?""", (
                    status,
                    _canonical(lifecycle_result) if lifecycle_result is not None else None,
                    code, error_text, timestamp, operation,
                ))
                if status == "confirmed" and row["kind"] == "delete":
                    connection.execute(
                        "DELETE FROM library_mappings WHERE library_id=? AND recipe_id=?",
                        (row["library_id"], row["target_recipe_id"]),
                    )
                    connection.execute(
                        "UPDATE library_operations SET error_code='recipe_deleted', "
                        "error_text='the mapped external recipe was permanently deleted', "
                        "updated_at=? WHERE kind='create' AND library_id=? "
                        "AND provider_recipe_id=? AND status='confirmed'",
                        (timestamp, row["library_id"], row["target_recipe_id"]),
                    )
                elif status == "confirmed" and provider_version is not None:
                    connection.execute(
                        "UPDATE library_mappings SET version=?, updated_at=? "
                        "WHERE library_id=? AND recipe_id=?",
                        (
                            provider_version, timestamp, row["library_id"],
                            row["target_recipe_id"],
                        ),
                    )
                self._cleanup_library_data(connection)
                return self._lifecycle_operation(
                    connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (operation,),
                    ).fetchone()
                )
        except (json.JSONDecodeError, TypeError) as exc:
            raise RecipeError("recipe library journal is unavailable") from exc
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def disable_library_connection(self, library_id: Any) -> None:
        try:
            library = validate_library_id(library_id, allow_builtin=False)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT 1 FROM library_operations WHERE library_id=? "
                    "AND status IN ('pending','uncertain') LIMIT 1",
                    (library,),
                ).fetchone()
                if active is not None:
                    raise RecipeError(
                        "resolve pending or uncertain operations before removing this connection"
                    )
                connection.execute(
                    "INSERT INTO library_connection_controls(library_id, disabled_at) VALUES(?,?) "
                    "ON CONFLICT(library_id) DO UPDATE SET disabled_at=excluded.disabled_at",
                    (library, _now()),
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def enable_library_connection(self, library_id: Any) -> None:
        try:
            library = validate_library_id(library_id, allow_builtin=False)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM library_connection_controls WHERE library_id=?", (library,)
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def bound_library_for_discovery(self, discovery_ref: Any, *, idempotency_key: Any = None) -> str | None:
        ref = _bounded_text(discovery_ref, "discovery_ref", required=True, maximum=200)
        key = self._idempotency_key(idempotency_key)
        try:
            with self._connection() as connection:
                ref = self._discovery_ref(connection, ref)
                if key is not None:
                    snapshot = connection.execute(
                        "SELECT snapshot_key, source_identity FROM discovery_snapshots "
                        "WHERE discovery_ref=?",
                        (ref,),
                    ).fetchone()
                    if snapshot is None:
                        rows = connection.execute(
                            "SELECT DISTINCT library_id FROM library_operations "
                            "WHERE kind='create' AND idempotency_key=?",
                            (key,),
                        ).fetchall()
                    else:
                        rows = connection.execute(
                            "SELECT DISTINCT library_id FROM library_operations WHERE kind='create' "
                            "AND idempotency_key=? AND (discovery_ref=? OR "
                            "(source_identity IS ? AND snapshot_digest=?))",
                            (key, ref, snapshot["source_identity"], snapshot["snapshot_key"]),
                        ).fetchall()
                    if len(rows) > 1:
                        raise RecipeError("discovery save has multiple bound targets; exact library_id is required")
                    if rows:
                        return rows[0]["library_id"]
                rows = connection.execute(
                    "SELECT library_id FROM library_operations WHERE discovery_ref=? "
                    "AND kind='create' ORDER BY library_id",
                    (ref,),
                ).fetchall()
                if len(rows) > 1:
                    raise RecipeError("discovery save has multiple bound targets; exact library_id is required")
                return rows[0]["library_id"] if rows else None
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def library_operation_snapshot(self, operation_id: Any) -> dict[str, Any]:
        operation = _bounded_text(operation_id, "operation_id", required=True, maximum=80)
        if re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", operation or "") is None:
            raise RecipeError("recipe library operation was not found")
        try:
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation,)
                ).fetchone()
                if row is None:
                    raise RecipeError("recipe library operation was not found")
                result = self._operation(row)
                if row["discovery_ref"] is not None and row["status"] in ACTIVE_DISCOVERY_BINDING_STATUSES:
                    result["snapshot"] = self._resolved_snapshot(connection, row["discovery_ref"])["recipe"]
                return result
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def library_operation_for_idempotency(
        self, idempotency_key: Any
    ) -> dict[str, Any] | None:
        key = self._idempotency_key(idempotency_key)
        if key is None:
            raise RecipeError("idempotency_key is required")
        try:
            with self._connection() as connection:
                if connection.execute(
                    "SELECT 1 FROM idempotency WHERE key=?", (key,)
                ).fetchone() is not None:
                    raise RecipeError(
                        "idempotency key was already used with different content"
                    )
                rows = connection.execute(
                    "SELECT * FROM library_operations WHERE idempotency_key=?", (key,)
                ).fetchall()
                if len(rows) > 1:
                    raise RecipeError("recipe library journal is unavailable")
                return self._operation(rows[0]) if rows else None
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def claim_library_dispatch(self, operation_id: Any) -> dict[str, Any]:
        operation = _bounded_text(operation_id, "operation_id", required=True, maximum=80)
        if re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", operation or "") is None:
            raise RecipeError("recipe library operation was not found")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation,)
                ).fetchone()
                if row is None:
                    raise RecipeError("recipe library operation was not found")
                if row["status"] != "pending" or row["dispatched_at"] is not None:
                    return self._operation(row)
                if row["kind"] in {"archive", "delete"}:
                    try:
                        created_at = datetime.fromisoformat(row["created_at"])
                    except (TypeError, ValueError) as exc:
                        raise RecipeError(
                            "recipe library journal is unavailable"
                        ) from exc
                    if (
                        created_at.tzinfo is None
                        or datetime.now(timezone.utc)
                        >= created_at + LIBRARY_LIFECYCLE_CONFIRMATION_TTL
                    ):
                        timestamp = _now()
                        connection.execute(
                            "UPDATE library_operations SET status='failed', "
                            "error_code='confirmation_expired', "
                            "error_text='recipe lifecycle confirmation expired', "
                            "updated_at=? WHERE operation_id=?",
                            (timestamp, operation),
                        )
                        return self._operation(
                            connection.execute(
                                "SELECT * FROM library_operations WHERE operation_id=?",
                                (operation,),
                            ).fetchone()
                        )
                timestamp = _now()
                cursor = connection.execute(
                    "UPDATE library_operations SET dispatched_at=?, error_code=NULL, "
                    "error_text=NULL, updated_at=? "
                    "WHERE operation_id=? AND status='pending' AND dispatched_at IS NULL",
                    (timestamp, timestamp, operation),
                )
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation,)
                ).fetchone()
                return self._operation(row, claimed=cursor.rowcount == 1)
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def defer_library_create_for_auth(self, operation_id: Any) -> dict[str, Any]:
        """Release a dispatch claim after a definite pre-create auth rejection."""
        operation = _bounded_text(
            operation_id, "operation_id", required=True, maximum=80
        )
        if re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", operation or "") is None:
            raise RecipeError("recipe library operation was not found")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation,)
                ).fetchone()
                if row is None or row["kind"] != "create":
                    raise RecipeError("recipe library operation was not found")
                if row["status"] != "pending":
                    return self._library_operation(row)
                timestamp = _now()
                connection.execute(
                    "UPDATE library_operations SET dispatched_at=NULL, error_code='needs_auth', "
                    "error_text='recipe library needs_auth', updated_at=? "
                    "WHERE operation_id=? AND status='pending'",
                    (timestamp, operation),
                )
                return self._library_operation(
                    connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (operation,),
                    ).fetchone()
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def recover_library_operations(self) -> None:
        """Turn possibly dispatched provider writes from an earlier process uncertain."""
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                timestamp = _now()
                connection.execute(
                    "UPDATE library_operations SET status='uncertain', updated_at=? "
                    "WHERE status='pending' AND dispatched_at IS NOT NULL",
                    (timestamp,),
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def finish_library_create(
        self,
        operation_id: Any,
        status: Any,
        *,
        library_recipe_ref: Any = None,
        error_code: Any = None,
        error: Any = None,
    ) -> dict[str, Any]:
        operation = _bounded_text(operation_id, "operation_id", required=True, maximum=80)
        if re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", operation or "") is None:
            raise RecipeError("recipe library operation was not found")
        if status not in {"confirmed", "failed", "uncertain"}:
            raise RecipeError("recipe library operation status is invalid")
        try:
            reference = None if library_recipe_ref is None else validate_library_recipe_ref(library_recipe_ref)
        except RecipeLibraryError as exc:
            raise RecipeError(str(exc)) from exc
        if (status == "confirmed") != (reference is not None):
            raise RecipeError("confirmed recipe library operation requires exactly one library_recipe_ref")
        code = _bounded_text(error_code, "recipe library error_code", maximum=80)
        if code is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
            raise RecipeError("recipe library error_code is invalid")
        error_text = _bounded_text(error, "recipe library error", maximum=500)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation,)
                ).fetchone()
                if row is None or row["kind"] != "create":
                    raise RecipeError("recipe library operation was not found")
                if row["status"] in {"confirmed", "failed"}:
                    return self._library_operation(row)
                if reference is not None and reference["library_id"] != row["library_id"]:
                    raise RecipeError("library_recipe_ref does not match the bound library")
                timestamp = _now()
                if reference is not None:
                    connection.execute("""INSERT INTO library_mappings(
                        library_id, source_identity, snapshot_digest, recipe_id, version,
                        operation_id, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)""", (
                        row["library_id"], row["source_identity"], row["snapshot_digest"],
                        reference["recipe_id"], reference.get("version"), operation, timestamp, timestamp,
                    ))
                connection.execute("""UPDATE library_operations SET
                    status=?, result_metadata=?, provider_recipe_id=?, provider_version=?,
                    error_code=?, error_text=?, updated_at=? WHERE operation_id=?""", (
                        status, _canonical(reference) if reference is not None else None,
                        reference["recipe_id"] if reference is not None else None,
                        reference.get("version") if reference is not None else None,
                        code, error_text, timestamp, operation,
                    ))
                self._cleanup_library_data(connection)
                self._cleanup_discoveries(connection)
                return self._library_operation(connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?", (operation,)
                ).fetchone())
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def finish_library_favorite(
        self,
        operation_id: Any,
        status: Any,
        *,
        result: Any = None,
        error_code: Any = None,
        error: Any = None,
    ) -> dict[str, Any]:
        operation = _bounded_text(
            operation_id, "operation_id", required=True, maximum=80
        )
        if re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", operation or "") is None:
            raise RecipeError("recipe library operation was not found")
        if status not in {"confirmed", "failed", "uncertain"}:
            raise RecipeError("recipe library operation status is invalid")
        favorite_result = None
        if result is not None:
            if not isinstance(result, Mapping):
                raise RecipeError("recipe library favorite result is invalid")
            allowed = {
                "library_id", "library_recipe_ref", "is_favorite",
                "favorite_revision", "idempotent", "reconciled",
            }
            if set(result) - allowed or not isinstance(result.get("is_favorite"), bool):
                raise RecipeError("recipe library favorite result is invalid")
            try:
                reference = validate_library_recipe_ref(
                    result.get("library_recipe_ref")
                )
            except RecipeLibraryError as exc:
                raise RecipeError(str(exc)) from exc
            favorite_result = {
                "library_id": reference["library_id"],
                "library_recipe_ref": reference,
                "is_favorite": result["is_favorite"],
            }
            if result.get("library_id") != reference["library_id"]:
                raise RecipeError("recipe library favorite result is invalid")
            if "favorite_revision" in result:
                favorite_result["favorite_revision"] = _favorite_revision(
                    result["favorite_revision"], "favorite_revision"
                )
            for name in ("idempotent", "reconciled"):
                if name in result:
                    if not isinstance(result[name], bool):
                        raise RecipeError("recipe library favorite result is invalid")
                    favorite_result[name] = result[name]
        if (status == "confirmed") != (favorite_result is not None):
            raise RecipeError(
                "confirmed recipe library favorite requires exactly one result"
            )
        code = _bounded_text(
            error_code, "recipe library error_code", maximum=80
        )
        if code is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
            raise RecipeError("recipe library error_code is invalid")
        error_text = _bounded_text(error, "recipe library error", maximum=500)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?",
                    (operation,),
                ).fetchone()
                if row is None or row["kind"] != "favorite":
                    raise RecipeError("recipe library operation was not found")
                if row["status"] in {"confirmed", "failed"}:
                    return self._favorite_operation(row)
                if favorite_result is not None:
                    reference = favorite_result["library_recipe_ref"]
                    if (
                        reference["library_id"] != row["library_id"]
                        or reference["recipe_id"] != row["target_recipe_id"]
                        or favorite_result["is_favorite"]
                        != bool(json.loads(row["request_metadata"])["is_favorite"])
                    ):
                        raise RecipeError(
                            "recipe library favorite result does not match the bound target"
                        )
                timestamp = _now()
                connection.execute("""UPDATE library_operations SET
                    status=?, result_metadata=?, provider_version=?, error_code=?,
                    error_text=?, updated_at=? WHERE operation_id=?""", (
                    status,
                    _canonical(favorite_result) if favorite_result is not None else None,
                    (
                        favorite_result["library_recipe_ref"].get("version")
                        if favorite_result is not None
                        else row["provider_version"]
                    ),
                    code, error_text, timestamp, operation,
                ))
                self._cleanup_library_data(connection)
                return self._favorite_operation(connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?",
                    (operation,),
                ).fetchone())
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def finish_library_label(
        self,
        operation_id: Any,
        status: Any,
        *,
        result: Any = None,
        error_code: Any = None,
        error: Any = None,
    ) -> dict[str, Any]:
        operation = _bounded_text(
            operation_id, "operation_id", required=True, maximum=80
        )
        if re.fullmatch(r"libop:v1:[A-Za-z0-9_-]{16,64}", operation or "") is None:
            raise RecipeError("recipe library operation was not found")
        if status not in {"confirmed", "failed", "uncertain"}:
            raise RecipeError("recipe library operation status is invalid")
        label_result = None
        if result is not None:
            if not isinstance(result, Mapping):
                raise RecipeError("recipe library label result is invalid")
            allowed = {
                "library_id",
                "library_label_ref",
                "name",
                "normalized_name",
                "library_recipe_ref",
                "present",
                "idempotent",
                "reconciled",
            }
            if set(result) - allowed:
                raise RecipeError("recipe library label result is invalid")
            try:
                label_ref = validate_library_label_ref(
                    result.get("library_label_ref")
                )
                name, normalized_name = normalize_label_name(result.get("name"))
            except RecipeLibraryError as exc:
                raise RecipeError(str(exc)) from exc
            if (
                result.get("library_id") != label_ref["library_id"]
                or result.get("normalized_name") != normalized_name
            ):
                raise RecipeError("recipe library label result is invalid")
            label_result = {
                "library_id": label_ref["library_id"],
                "library_label_ref": label_ref,
                "name": name,
                "normalized_name": normalized_name,
            }
            if "library_recipe_ref" in result:
                try:
                    label_result["library_recipe_ref"] = validate_library_recipe_ref(
                        result["library_recipe_ref"]
                    )
                except RecipeLibraryError as exc:
                    raise RecipeError(str(exc)) from exc
            if "present" in result:
                if not isinstance(result["present"], bool):
                    raise RecipeError("recipe library label result is invalid")
                label_result["present"] = result["present"]
            for name_key in ("idempotent", "reconciled"):
                if name_key in result:
                    if not isinstance(result[name_key], bool):
                        raise RecipeError("recipe library label result is invalid")
                    label_result[name_key] = result[name_key]
        if (status == "confirmed") != (label_result is not None):
            raise RecipeError(
                "confirmed recipe library label operation requires exactly one result"
            )
        code = _bounded_text(error_code, "recipe library error_code", maximum=80)
        if code is not None and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code) is None:
            raise RecipeError("recipe library error_code is invalid")
        error_text = _bounded_text(error, "recipe library error", maximum=500)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM library_operations WHERE operation_id=?",
                    (operation,),
                ).fetchone()
                if row is None or row["kind"] != "label":
                    raise RecipeError("recipe library operation was not found")
                if row["status"] in {"confirmed", "failed"}:
                    return self._label_operation(row)
                metadata = json.loads(row["request_metadata"])
                if label_result is not None:
                    label_ref = label_result["library_label_ref"]
                    if label_ref["library_id"] != row["library_id"]:
                        raise RecipeError(
                            "recipe library label result does not match the bound target"
                        )
                    if metadata["action"] == "create":
                        if (
                            label_result["normalized_name"]
                            != metadata["normalized_name"]
                            or "library_recipe_ref" in label_result
                            or "present" in label_result
                        ):
                            raise RecipeError(
                                "recipe library label result does not match the bound target"
                            )
                    else:
                        recipe_ref = label_result.get("library_recipe_ref")
                        requested_label = validate_library_label_ref(
                            metadata["library_label_ref"]
                        )
                        if (
                            recipe_ref is None
                            or recipe_ref["library_id"] != row["library_id"]
                            or recipe_ref["recipe_id"] != row["target_recipe_id"]
                            or label_ref["label_id"] != requested_label["label_id"]
                            or label_result.get("present")
                            != (metadata["action"] == "apply")
                        ):
                            raise RecipeError(
                                "recipe library label result does not match the bound target"
                            )
                timestamp = _now()
                connection.execute("""UPDATE library_operations SET
                    status=?, result_metadata=?, error_code=?, error_text=?, updated_at=?
                    WHERE operation_id=?""", (
                    status,
                    _canonical(label_result) if label_result is not None else None,
                    code,
                    error_text,
                    timestamp,
                    operation,
                ))
                self._cleanup_library_data(connection)
                return self._label_operation(
                    connection.execute(
                        "SELECT * FROM library_operations WHERE operation_id=?",
                        (operation,),
                    ).fetchone()
                )
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def _cleanup_discoveries(self, connection: sqlite3.Connection) -> None:
        current = _now()
        failed_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=FAILED_DISCOVERY_BINDING_TTL_DAYS)
        ).isoformat()
        connection.execute(
            "DELETE FROM discovery_bindings WHERE status='failed' AND updated_at <= ?",
            (failed_cutoff,),
        )
        failed = connection.execute(
            "SELECT destination, discovery_ref FROM discovery_bindings "
            "WHERE status='failed' ORDER BY updated_at, destination, discovery_ref"
        ).fetchall()
        while len(failed) > MAX_FAILED_DISCOVERY_BINDINGS:
            row = failed.pop(0)
            connection.execute(
                "DELETE FROM discovery_bindings WHERE destination=? AND discovery_ref=?",
                (row["destination"], row["discovery_ref"]),
            )
        connection.execute("""
            DELETE FROM discovery_snapshots
            WHERE expires_at <= ? AND NOT EXISTS (
                SELECT 1 FROM discovery_bindings
                WHERE discovery_bindings.discovery_ref = discovery_snapshots.discovery_ref
                  AND discovery_bindings.status IN ('pending', 'uncertain')
            ) AND NOT EXISTS (
                SELECT 1 FROM library_operations
                WHERE library_operations.discovery_ref = discovery_snapshots.discovery_ref
                  AND library_operations.status IN ('pending', 'uncertain')
            )
        """, (current,))
        rows = connection.execute("""
            SELECT discovery_ref, document_bytes FROM discovery_snapshots
            WHERE NOT EXISTS (
                SELECT 1 FROM discovery_bindings
                WHERE discovery_bindings.discovery_ref = discovery_snapshots.discovery_ref
                  AND discovery_bindings.status IN ('pending', 'uncertain')
            ) AND NOT EXISTS (
                SELECT 1 FROM library_operations
                WHERE library_operations.discovery_ref = discovery_snapshots.discovery_ref
                  AND library_operations.status IN ('pending', 'uncertain')
            )
            ORDER BY expires_at, discovery_ref
        """).fetchall()
        total = sum(row["document_bytes"] for row in rows)
        while len(rows) > MAX_UNBOUND_DISCOVERY_SNAPSHOTS or total > MAX_UNBOUND_DISCOVERY_BYTES:
            row = rows.pop(0)
            connection.execute("DELETE FROM discovery_snapshots WHERE discovery_ref=?", (row["discovery_ref"],))
            total -= row["document_bytes"]

    def cleanup_discoveries(self) -> None:
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._cleanup_discoveries(connection)
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def persist_discovery(self, value: Any) -> dict[str, Any]:
        document, snapshot_key, content_hash, attribution_digest = self._snapshot_parts(value)
        serialized = _canonical(document)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._cleanup_discoveries(connection)
                row = connection.execute(
                    "SELECT discovery_ref FROM discovery_snapshots WHERE snapshot_key=?", (snapshot_key,)
                ).fetchone()
                current = datetime.now(timezone.utc)
                if row is None:
                    binding = connection.execute(
                        "SELECT discovery_ref, recipe_id, recipe_revision FROM discovery_bindings "
                        "WHERE destination='builtin' AND status='confirmed' AND snapshot_key=?",
                        (snapshot_key,),
                    ).fetchone()
                    if binding is None:
                        namespace = connection.execute(
                            "SELECT value FROM metadata WHERE key='discovery_namespace'"
                        ).fetchone()[0]
                        ref = f"discovery:v1:{namespace}:{secrets.token_urlsafe(18)}"
                    else:
                        ref = binding["discovery_ref"]
                        revision = connection.execute(
                            "SELECT document FROM revisions WHERE recipe_id=? AND revision=?",
                            (binding["recipe_id"], binding["recipe_revision"]),
                        ).fetchone()
                        if revision is None:
                            raise RecipeError("recipe bank is unavailable")
                        document, bound_key, content_hash, attribution_digest = (
                            self._snapshot_parts(_stored_recipe_document(revision["document"]))
                        )
                        if bound_key != snapshot_key:
                            raise RecipeError("recipe bank is unavailable")
                        serialized = _canonical(document)
                    created = current.isoformat()
                    connection.execute("""
                        INSERT INTO discovery_snapshots(
                            discovery_ref, snapshot_key, document, source_identity, content_hash, attribution_digest,
                            created_at, renewed_at, expires_at, document_bytes
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """, (ref, snapshot_key, serialized, source_key(document), content_hash, attribution_digest,
                          created, created, self._expiry(current), len(serialized.encode("utf-8"))))
                else:
                    ref = row["discovery_ref"]
                    connection.execute(
                        "UPDATE discovery_snapshots SET renewed_at=?, expires_at=? WHERE discovery_ref=?",
                        (current.isoformat(), self._expiry(current), ref),
                    )
                self._cleanup_discoveries(connection)
                return self._resolved_snapshot(connection, ref)
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def resolve_discovery(
        self,
        value: Any,
        *,
        destination: Any = None,
        binding_status: Any = None,
    ) -> dict[str, Any]:
        if (destination is None) != (binding_status is None):
            raise RecipeError("destination and binding_status must be supplied together")
        resolved_destination = None if destination is None else self._destination(destination)
        if resolved_destination == "builtin":
            raise RecipeError("built-in discovery bindings are created only by save")
        if binding_status is not None and (
            not isinstance(binding_status, str)
            or binding_status not in ACTIVE_DISCOVERY_BINDING_STATUSES
        ):
            raise RecipeError("discovery binding status must be pending or uncertain")
        try:
            with self._connection() as connection:
                connection.execute(
                    "BEGIN IMMEDIATE" if resolved_destination is not None else "BEGIN"
                )
                ref = self._discovery_ref(connection, value)
                resolved = self._resolved_snapshot(connection, ref)
                if resolved_destination is not None:
                    existing = connection.execute(
                        "SELECT status FROM discovery_bindings WHERE destination=? AND discovery_ref=?",
                        (resolved_destination, ref),
                    ).fetchone()
                    if existing is not None and existing["status"] not in ACTIVE_DISCOVERY_BINDING_STATUSES:
                        raise RecipeError("discovery binding is already terminal")
                    timestamp = _now()
                    connection.execute("""
                        INSERT INTO discovery_bindings(
                            destination, discovery_ref, snapshot_key, status,
                            recipe_id, recipe_revision,
                            created_at, updated_at
                        ) VALUES(?,?,?, ?,NULL,NULL,?,?)
                        ON CONFLICT(destination, discovery_ref) DO UPDATE SET
                            snapshot_key=excluded.snapshot_key,
                            status=excluded.status, updated_at=excluded.updated_at
                    """, (
                        resolved_destination, ref,
                        connection.execute(
                            "SELECT snapshot_key FROM discovery_snapshots WHERE discovery_ref=?", (ref,)
                        ).fetchone()[0],
                        binding_status, timestamp, timestamp,
                    ))
                    resolved["binding"] = {
                        "destination": resolved_destination,
                        "status": binding_status,
                    }
                return resolved
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def _bound_builtin_recipe(
        self, connection: sqlite3.Connection, binding: sqlite3.Row
    ) -> dict[str, Any]:
        current = connection.execute(
            "SELECT * FROM recipes WHERE id=?", (binding["recipe_id"],)
        ).fetchone()
        revision = connection.execute(
            "SELECT status, document, created_at FROM revisions WHERE recipe_id=? AND revision=?",
            (binding["recipe_id"], binding["recipe_revision"]),
        ).fetchone()
        if current is None or revision is None:
            raise RecipeError("recipe bank is unavailable")
        result = _stored_recipe_document(revision["document"])
        result.update({
            "id": current["id"],
            "revision": binding["recipe_revision"],
            "status": revision["status"],
            "created_at": current["created_at"],
            "updated_at": revision["created_at"],
            "created_via": current["created_via"],
            "content_fingerprint": content_fingerprint(result),
            "recipe_key": f"bank:{current['id']}",
            "library_recipe_ref": {
                "library_id": "builtin",
                "recipe_id": current["id"],
                "version": str(binding["recipe_revision"]),
            },
            "created": False,
            "idempotent": True,
        })
        favorite = self._favorite_state(connection, current["id"])
        result.update({key: favorite[key] for key in ("library_id", "is_favorite", "favorite_revision")})
        return result

    def save_discovery(self, value: Any, *, status: str = "active", idempotency_key: Any = None) -> dict[str, Any]:
        if not isinstance(status, str) or status not in {"active", "draft"}:
            raise RecipeError("recipe status must be active or draft")
        ref = _bounded_text(value, "discovery_ref", required=True, maximum=200)
        key = self._idempotency_key(idempotency_key)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                ref = self._discovery_ref(connection, ref)
                request_hash = _hash({"discovery_ref": ref, "status": status})
                if existing := self._idem(connection, key, "save_discovery", request_hash):
                    return existing
                bound = connection.execute(
                    "SELECT * FROM discovery_bindings WHERE destination='builtin' AND discovery_ref=?",
                    (ref,),
                ).fetchone()
                if bound is not None:
                    if bound["status"] != "confirmed":
                        raise RecipeError("built-in discovery binding is invalid")
                    result = self._bound_builtin_recipe(connection, bound)
                    self._store_idem(connection, key, "save_discovery", request_hash, result)
                    return result
                resolved = self._resolved_snapshot(connection, ref)
                recipe = resolved["recipe"]
                source_identity = source_key(recipe)
                existing = connection.execute("SELECT * FROM recipes WHERE source_key=?", (source_identity,)).fetchone() if source_identity else None
                if existing is not None:
                    _, _, existing_content_hash, existing_attribution_digest = self._snapshot_parts(
                        _stored_recipe_document(existing["document"])
                    )
                    if (
                        existing_content_hash != resolved["content_hash"]
                        or existing_attribution_digest != resolved["attribution_digest"]
                    ):
                        result = self._record(connection, existing, created=False)
                        result["conflict"] = {
                            "kind": "source_changed",
                            "discovery_ref": ref,
                            "existing_recipe_ref": {
                                "id": existing["id"],
                                "revision": existing["revision"],
                            },
                            "requires": "explicit update with expected_revision",
                        }
                        self._store_idem(connection, key, "save_discovery", request_hash, result)
                        return result
                result = self._record(connection, existing, created=False) if existing is not None else self._save(connection, recipe, status, None, "discovery")
                timestamp = _now()
                connection.execute("""
                    INSERT INTO discovery_bindings(
                        destination, discovery_ref, snapshot_key, status,
                        recipe_id, recipe_revision,
                        created_at, updated_at
                    ) VALUES('builtin',?,?,'confirmed',?,?,?,?)
                """, (
                    ref,
                    connection.execute(
                        "SELECT snapshot_key FROM discovery_snapshots WHERE discovery_ref=?", (ref,)
                    ).fetchone()[0],
                    result["id"], result["revision"], timestamp, timestamp,
                ))
                self._store_idem(connection, key, "save_discovery", request_hash, result)
                self._cleanup_discoveries(connection)
                return result
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def _save(self, connection: sqlite3.Connection, recipe: Mapping[str, Any], status: str, key: str | None, created_via: str) -> dict[str, Any]:
        request_hash = _hash({"recipe": recipe, "status": status})
        if existing := self._idem(connection, key, "save", request_hash):
            return existing
        content_hash = _hash(recipe)
        source_identity = source_key(recipe)
        if source_identity:
            duplicate = connection.execute("SELECT * FROM recipes WHERE source_key=?", (source_identity,)).fetchone()
            if duplicate is not None:
                if duplicate["content_hash"] != content_hash:
                    raise RecipeError("source identity already belongs to a different recipe revision")
                result = self._record(connection, duplicate, created=False)
                result["duplicate"] = "source_key"
                self._store_idem(connection, key, "save", request_hash, result)
                return result
        fingerprint = content_fingerprint(recipe)
        warning = connection.execute("SELECT id FROM recipes WHERE content_fingerprint=? LIMIT 1", (fingerprint,)).fetchone()
        created_at = _now()
        recipe_id = f"rec_{secrets.token_hex(12)}"
        connection.execute(
            "INSERT INTO recipes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (recipe_id, 1, status, recipe["name"], self._search_text(recipe), source_identity, fingerprint, content_hash, _canonical(recipe), created_at, created_at, created_via),
        )
        connection.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (recipe_id, 1, status, _canonical(recipe), created_at))
        row = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        result = self._record(connection, row, created=True)
        if warning:
            result["duplicate_warning"] = {"kind": "content_fingerprint", "recipe_id": warning["id"]}
        self._store_idem(connection, key, "save", request_hash, result)
        return result

    def save(self, value: Any, *, status: str = "active", idempotency_key: Any = None) -> dict[str, Any]:
        if not isinstance(status, str) or status not in {"active", "draft"}:
            raise RecipeError("recipe status must be active or draft")
        recipe = normalize_recipe(value)
        key = self._idempotency_key(idempotency_key)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._save(connection, recipe, status, key, "hermes")
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def get(self, recipe_id: Any, revision: Any = None) -> dict[str, Any]:
        recipe_id = _bounded_text(recipe_id, "recipe_id", required=True, maximum=80)
        if isinstance(revision, str) and re.fullmatch(r"[1-9][0-9]*", revision):
            revision = int(revision)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN")
                if revision is None:
                    row = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
                    if row is None:
                        raise RecipeError("recipe was not found")
                    return self._record(connection, row)
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                    raise RecipeError("revision must be a positive integer")
                version = connection.execute("SELECT status, document, created_at FROM revisions WHERE recipe_id=? AND revision=?", (recipe_id, revision)).fetchone()
                current = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
                if version is None or current is None:
                    raise RecipeError("recipe revision was not found")
                result = _stored_recipe_document(version["document"])
                result.update({"id": recipe_id, "revision": revision, "status": current["status"], "revision_status": version["status"], "created_at": current["created_at"], "updated_at": version["created_at"], "created_via": current["created_via"], "content_fingerprint": content_fingerprint(result), "recipe_key": f"bank:{recipe_id}", "library_recipe_ref": {"library_id": "builtin", "recipe_id": recipe_id, "version": str(revision)}})
                favorite = self._favorite_state(connection, recipe_id)
                result.update({key: favorite[key] for key in ("library_id", "is_favorite", "favorite_revision")})
                return result
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def search(
        self,
        query: Any = "",
        *,
        limit: Any = 10,
        include_archived: bool = False,
        favorites_only: bool = False,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        text = _bounded_text(query, "query", maximum=200) or ""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise RecipeError("limit must be between one and 50")
        if not isinstance(include_archived, bool) or not isinstance(favorites_only, bool):
            raise RecipeError("recipe search filters must be true or false")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise RecipeError("search offset must be a non-negative integer")
        literal = _normalized_text(text).replace("!", "!!").replace("%", "!%").replace("_", "!_")
        needle = f"%{literal}%"
        status = "" if include_archived else "AND status != 'archived'"
        favorite = (
            "AND EXISTS (SELECT 1 FROM recipe_favorites AS favorite "
            "WHERE favorite.library_id='builtin' AND favorite.recipe_id=recipes.id "
            "AND favorite.is_favorite=1)"
            if favorites_only else ""
        )
        try:
            with self._connection() as connection:
                connection.execute("BEGIN")
                rows = connection.execute(
                    f"SELECT * FROM recipes WHERE (lower(name) LIKE ? ESCAPE '!' OR search_text LIKE ? ESCAPE '!') {status} {favorite} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (needle, needle, limit, offset),
                ).fetchall()
                return [self._record(connection, row) for row in rows]
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def set_favorite(
        self,
        library_recipe_ref: Any,
        is_favorite: Any,
        *,
        expected_favorite_revision: Any = None,
        idempotency_key: Any,
    ) -> dict[str, Any]:
        reference = validate_library_recipe_ref(library_recipe_ref)
        if reference["library_id"] != "builtin":
            raise RecipeError("recipe favorites are supported only for the builtin library")
        if not isinstance(is_favorite, bool):
            raise RecipeError("is_favorite must be true or false")
        if expected_favorite_revision is not None and (
            isinstance(expected_favorite_revision, bool)
            or not isinstance(expected_favorite_revision, int)
            or expected_favorite_revision < 0
        ):
            raise RecipeError("expected_favorite_revision must be a non-negative integer")
        key = self._idempotency_key(idempotency_key)
        if key is None:
            raise RecipeError("idempotency_key is required")
        recipe_id = reference["recipe_id"]
        request_hash = _hash({
            "library_id": "builtin",
            "recipe_id": recipe_id,
            "is_favorite": is_favorite,
            "expected_favorite_revision": expected_favorite_revision,
        })
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                recipe = connection.execute(
                    "SELECT id, revision FROM recipes WHERE id=?", (recipe_id,)
                ).fetchone()
                if recipe is None:
                    raise RecipeError("recipe was not found")
                if connection.execute(
                    "SELECT 1 FROM library_operations WHERE idempotency_key=? LIMIT 1",
                    (key,),
                ).fetchone() is not None:
                    raise RecipeError(
                        "idempotency key was already used with different content"
                    )
                if existing := self._idem(connection, key, "set_favorite", request_hash):
                    existing.pop("created", None)
                    return existing
                current = self._favorite_state(connection, recipe_id)
                if (
                    expected_favorite_revision is not None
                    and expected_favorite_revision != current["favorite_revision"]
                ):
                    raise RecipeError(
                        "favorite revision conflict; current favorite revision is "
                        f"{current['favorite_revision']}"
                    )
                changed = current["is_favorite"] != is_favorite
                if changed:
                    timestamp = _now()
                    favorite_revision = current["favorite_revision"] + 1
                    connection.execute("""
                        INSERT INTO recipe_favorites(
                            library_id, recipe_id, is_favorite, favorite_revision,
                            created_at, updated_at
                        ) VALUES('builtin',?,?,?,?,?)
                        ON CONFLICT(library_id, recipe_id) DO UPDATE SET
                            is_favorite=excluded.is_favorite,
                            favorite_revision=excluded.favorite_revision,
                            updated_at=excluded.updated_at
                    """, (
                        recipe_id, int(is_favorite), favorite_revision,
                        current["favorite_created_at"] or timestamp, timestamp,
                    ))
                    current = self._favorite_state(connection, recipe_id)
                result = {
                    "library_id": "builtin",
                    "library_recipe_ref": {
                        "library_id": "builtin",
                        "recipe_id": recipe_id,
                        "version": str(recipe["revision"]),
                    },
                    "is_favorite": current["is_favorite"],
                    "favorite_revision": current["favorite_revision"],
                    "created_at": current["favorite_created_at"],
                    "updated_at": current["favorite_updated_at"],
                }
                if not changed:
                    result["idempotent"] = True
                self._store_idem(connection, key, "set_favorite", request_hash, result)
                return result
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def delete(self, recipe_id: Any, expected_revision: Any) -> dict[str, Any]:
        """Permanently remove one exact built-in recipe and its local identity metadata."""
        recipe_id = _bounded_text(recipe_id, "recipe_id", required=True, maximum=80)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise RecipeError("expected_revision must be a positive integer")
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT revision FROM recipes WHERE id=?", (recipe_id,)
                ).fetchone()
                if current is None:
                    raise RecipeError("recipe was not found")
                if current["revision"] != expected_revision:
                    raise RecipeError(f"recipe revision conflict; current revision is {current['revision']}")
                connection.execute(
                    "DELETE FROM discovery_bindings WHERE destination='builtin' AND recipe_id=?",
                    (recipe_id,),
                )
                connection.execute(
                    "DELETE FROM library_mappings WHERE library_id='builtin' AND recipe_id=?",
                    (recipe_id,),
                )
                connection.execute(
                    "UPDATE library_operations SET discovery_ref=NULL, status='failed', "
                    "result_metadata=NULL, provider_recipe_id=NULL, provider_version=NULL, "
                    "error_code='recipe_deleted', "
                    "error_text='built-in recipe was permanently deleted', updated_at=? "
                    "WHERE library_id='builtin' AND provider_recipe_id=?",
                    (_now(), recipe_id),
                )
                connection.execute("DELETE FROM revisions WHERE recipe_id=?", (recipe_id,))
                connection.execute("DELETE FROM recipes WHERE id=?", (recipe_id,))
                return {"library_id": "builtin", "recipe_id": recipe_id, "deleted": True}
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def update(self, recipe_id: Any, expected_revision: Any, value: Any, *, status: str | None = None, idempotency_key: Any = None) -> dict[str, Any]:
        recipe_id = _bounded_text(recipe_id, "recipe_id", required=True, maximum=80)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise RecipeError("expected_revision must be a positive integer")
        if status is not None and (not isinstance(status, str) or status not in {"active", "draft"}):
            raise RecipeError("recipe status must be active or draft")
        recipe = normalize_recipe(value)
        key = self._idempotency_key(idempotency_key)
        request_hash = _hash({"recipe_id": recipe_id, "expected_revision": expected_revision, "recipe": recipe, "status": status})
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if existing := self._idem(connection, key, "update", request_hash):
                    return existing
                current = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
                if current is None:
                    raise RecipeError("recipe was not found")
                if current["revision"] != expected_revision:
                    raise RecipeError(f"recipe revision conflict; current revision is {current['revision']}")
                identity = source_key(recipe)
                collision = connection.execute("SELECT id FROM recipes WHERE source_key=? AND id != ?", (identity, recipe_id)).fetchone() if identity else None
                if collision:
                    raise RecipeError("source identity already belongs to another recipe")
                revision = expected_revision + 1
                updated_at = _now()
                fingerprint = content_fingerprint(recipe)
                next_status = status or current["status"]
                cursor = connection.execute(
                    "UPDATE recipes SET revision=?, status=?, name=?, search_text=?, source_key=?, content_fingerprint=?, content_hash=?, document=?, updated_at=? WHERE id=? AND revision=?",
                    (revision, next_status, recipe["name"], self._search_text(recipe), identity, fingerprint, _hash(recipe), _canonical(recipe), updated_at, recipe_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise RecipeError("recipe revision conflict")
                connection.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (recipe_id, revision, next_status, _canonical(recipe), updated_at))
                result = self._record(connection, connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone(), created=False)
                self._store_idem(connection, key, "update", request_hash, result)
                return result
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def archive(self, recipe_id: Any, expected_revision: Any = None, *, idempotency_key: Any = None) -> dict[str, Any]:
        recipe_id = _bounded_text(recipe_id, "recipe_id", required=True, maximum=80)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            raise RecipeError("expected_revision must be a positive integer")
        key = self._idempotency_key(idempotency_key)
        request_hash = _hash({"recipe_id": recipe_id, "expected_revision": expected_revision})
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if existing := self._idem(connection, key, "archive", request_hash):
                    return existing
                current = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
                if current is None:
                    raise RecipeError("recipe was not found")
                if current["revision"] != expected_revision:
                    raise RecipeError(f"recipe revision conflict; current revision is {current['revision']}")
                if current["status"] == "archived":
                    result = self._record(connection, current, created=False)
                    result["idempotent"] = True
                else:
                    revision = current["revision"] + 1
                    updated_at = _now()
                    connection.execute("UPDATE recipes SET revision=?, status='archived', updated_at=? WHERE id=?", (revision, updated_at, recipe_id))
                    connection.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (recipe_id, revision, "archived", current["document"], updated_at))
                    result = self._record(connection, connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone(), created=False)
                self._store_idem(connection, key, "archive", request_hash, result)
                return result
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def import_records(self, records: Iterable[Any], *, dry_run: bool = False, default_status: str = "active") -> dict[str, Any]:
        if default_status not in {"active", "draft"}:
            raise RecipeError("default import status is invalid")
        created = skipped = warnings = 0
        try:
            if dry_run:
                connection = sqlite3.connect(":memory:", timeout=2.0)
                if self.path.exists():
                    source = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
                    try:
                        source.backup(connection)
                    finally:
                        source.close()
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=2000")
                self._schema(connection)
                connection.commit()
            else:
                connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for index, value in enumerate(records, start=1):
                    if index > MAX_IMPORT_RECORDS:
                        raise RecipeError(f"import exceeds {MAX_IMPORT_RECORDS} recipes")
                    if isinstance(value, Mapping) and isinstance(value.get("recipe"), Mapping):
                        recipe_value = value["recipe"]
                        status = str(value.get("status") or default_status)
                        key = self._idempotency_key(value.get("idempotency_key"))
                    else:
                        recipe_value = value
                        status = default_status
                        key = None
                    if status not in {"active", "draft"}:
                        raise RecipeError(f"record {index} has an invalid status")
                    recipe = normalize_recipe(recipe_value)
                    key = key or f"import:{_hash(recipe)}"
                    result = self._save(connection, recipe, status, key, "import")
                    if result.get("created"):
                        created += 1
                    else:
                        skipped += 1
                    if result.get("duplicate_warning"):
                        warnings += 1
                if dry_run:
                    connection.rollback()
                else:
                    connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc
        return {"dry_run": dry_run, "created": created, "skipped": skipped, "duplicate_warnings": warnings}

    def backup(self, destination: Path | str) -> Path:
        destination = Path(destination)
        if destination.exists():
            raise RecipeError("backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            raise RecipeError("recipe bank does not exist")
        try:
            source = self._connect()
            target = sqlite3.connect(str(destination))
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            os.chmod(destination, 0o600)
        except sqlite3.Error as exc:
            if destination.exists():
                destination.unlink()
            raise RecipeError("recipe bank backup failed") from exc
        return destination
