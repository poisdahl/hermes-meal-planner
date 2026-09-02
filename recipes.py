"""Household-local recipe documents, validation, scaling and SQLite storage."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
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
from typing import Any, Iterable, Mapping
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from core import HouseholdError


MAX_RECIPE_BYTES = 256 * 1024
MAX_IMPORT_RECORDS = 10_000
MAX_TEXT = 4_000
VALID_RELATIONSHIPS = {"original", "adapted", "inspired_by", "generated", "user_supplied", "unknown"}
VALID_STORAGE = {"full", "link_only"}
RESTRICTED_FULL_HOSTS = {"meny.no", "www.meny.no", "oda.com", "www.oda.com"}
SERVER_FIELDS = {
    "id", "revision", "status", "created_at", "updated_at", "created_via",
    "recipe_key", "content_fingerprint", "content_hash", "shopping_requirements",
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

    def _schema(self, connection: sqlite3.Connection) -> None:
        metadata_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
        ).fetchone()
        if metadata_exists:
            metadata = dict(connection.execute(
                "SELECT key, value FROM metadata WHERE key IN ('household', 'schema_version')"
            ).fetchall())
            if metadata.get("household") not in {None, self.household}:
                raise RecipeError("recipe bank belongs to a different household")
            if metadata.get("schema_version") not in {None, "1"}:
                raise RecipeError("recipe bank schema is newer than this meal planner")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS recipes (
                id TEXT PRIMARY KEY,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                name TEXT NOT NULL,
                search_text TEXT NOT NULL,
                source_key TEXT,
                content_fingerprint TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_via TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS recipes_source_key ON recipes(source_key) WHERE source_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS recipes_search ON recipes(status, name);
            CREATE INDEX IF NOT EXISTS recipes_fingerprint ON recipes(content_fingerprint);
            CREATE TABLE IF NOT EXISTS revisions (
                recipe_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                status TEXT NOT NULL,
                document TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (recipe_id, revision)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(recipes)")}
        if "created_via" not in columns:
            connection.execute("ALTER TABLE recipes ADD COLUMN created_via TEXT NOT NULL DEFAULT 'hermes'")
        row = connection.execute("SELECT value FROM metadata WHERE key='household'").fetchone()
        if row is None:
            connection.execute("INSERT INTO metadata(key, value) VALUES('household', ?)", (self.household,))
        elif row[0] != self.household:
            raise RecipeError("recipe bank belongs to a different household")
        connection.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES('schema_version', '1')")

    def _connect(self, target: str | None = None) -> sqlite3.Connection:
        if target is None:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = sqlite3.connect(str(self.path), timeout=2.0)
            os.chmod(self.path, 0o600)
        else:
            connection = sqlite3.connect(target, timeout=2.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=2000")
            self._schema(connection)
            connection.commit()
            return connection
        except Exception:
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
    def _record(row: sqlite3.Row, *, created: bool | None = None) -> dict[str, Any]:
        result = _stored_recipe_document(row["document"])
        result.update({
            "id": row["id"], "revision": row["revision"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "created_via": row["created_via"],
            "content_fingerprint": row["content_fingerprint"], "recipe_key": f"bank:{row['id']}",
        })
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
                result = self._record(duplicate, created=False)
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
        result = self._record(row, created=True)
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
        try:
            with self._connection() as connection:
                if revision is None:
                    row = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
                    if row is None:
                        raise RecipeError("recipe was not found")
                    return self._record(row)
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                    raise RecipeError("revision must be a positive integer")
                version = connection.execute("SELECT status, document, created_at FROM revisions WHERE recipe_id=? AND revision=?", (recipe_id, revision)).fetchone()
                current = connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
                if version is None or current is None:
                    raise RecipeError("recipe revision was not found")
                result = _stored_recipe_document(version["document"])
                result.update({"id": recipe_id, "revision": revision, "status": current["status"], "revision_status": version["status"], "created_at": current["created_at"], "updated_at": version["created_at"], "created_via": current["created_via"], "content_fingerprint": content_fingerprint(result), "recipe_key": f"bank:{recipe_id}"})
                return result
        except sqlite3.Error as exc:
            raise RecipeError("recipe bank is unavailable") from exc

    def search(self, query: Any = "", *, limit: Any = 10, include_archived: bool = False, offset: int = 0) -> list[dict[str, Any]]:
        text = _bounded_text(query, "query", maximum=200) or ""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise RecipeError("limit must be between one and 50")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise RecipeError("search offset must be a non-negative integer")
        literal = _normalized_text(text).replace("!", "!!").replace("%", "!%").replace("_", "!_")
        needle = f"%{literal}%"
        status = "" if include_archived else "AND status != 'archived'"
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    f"SELECT * FROM recipes WHERE (lower(name) LIKE ? ESCAPE '!' OR search_text LIKE ? ESCAPE '!') {status} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (needle, needle, limit, offset),
                ).fetchall()
                return [self._record(row) for row in rows]
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
                result = self._record(connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone(), created=False)
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
                    result = self._record(current, created=False)
                    result["idempotent"] = True
                else:
                    revision = current["revision"] + 1
                    updated_at = _now()
                    connection.execute("UPDATE recipes SET revision=?, status='archived', updated_at=? WHERE id=?", (revision, updated_at, recipe_id))
                    connection.execute("INSERT INTO revisions VALUES(?,?,?,?,?)", (recipe_id, revision, "archived", current["document"], updated_at))
                    result = self._record(connection.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone(), created=False)
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
