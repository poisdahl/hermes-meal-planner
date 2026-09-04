"""Provider-neutral personal recipe-library identities and trust boundaries."""

from __future__ import annotations

from abc import ABC
import base64
import importlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from core import HouseholdError


LIBRARY_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}")
SUPPORTED_LIBRARY_PROVIDERS = {"builtin", "mealie", "recipesage"}
CAPABILITY_NAMES = (
    "search",
    "get",
    "create_from_discovery",
    "conditional_update",
    "archive_desired_state",
    "delete",
    "favorite_read",
    "favorite_write_desired_state",
    "favorite_conditional_write",
    "label_read",
    "label_apply_existing",
    "label_remove",
    "label_create",
    "label_conditional_write",
    "label_reconcile",
    "reconcile_create",
    "reconcile_archive",
    "reconcile_delete",
    "favorite_reconcile",
)
WRITE_CAPABILITIES = {
    "create_from_discovery",
    "conditional_update",
    "archive_desired_state",
    "delete",
    "favorite_write_desired_state",
    "favorite_conditional_write",
    "label_apply_existing",
    "label_remove",
    "label_create",
    "label_conditional_write",
}
MAX_LIBRARY_CONNECTIONS = 20
MAX_SECRET_BYTES = 16 * 1024
MAX_LIBRARY_RECIPE_KEY = 2_048


class RecipeLibraryError(HouseholdError):
    pass


class RecipeLibraryDefiniteError(RecipeLibraryError):
    """The provider definitely rejected or did not dispatch a request."""


class RecipeLibraryUncertainError(RecipeLibraryError):
    """A request may have reached the provider and must not be retried blindly."""


class RecipeLibraryExternalMissingError(RecipeLibraryDefiniteError):
    """The exact provider recipe no longer exists or is no longer accessible."""


class RecipeLibraryFavoriteConflictError(RecipeLibraryDefiniteError):
    """A provider-side conditional favorite write lost a revision race."""


class RecipeLibraryLabelConflictError(RecipeLibraryDefiniteError):
    """A provider-side conditional label write lost a revision race."""


def _exact_text(value: Any, field: str, maximum: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise RecipeLibraryError(f"{field} must be text")
    if value != value.strip() or unicodedata.normalize("NFC", value) != value:
        raise RecipeLibraryError(f"{field} must use exact normalized text")
    if any(ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise RecipeLibraryError(f"{field} contains forbidden characters")
    if required and not value:
        raise RecipeLibraryError(f"{field} is required")
    if len(value) > maximum:
        raise RecipeLibraryError(f"{field} is too long")
    return value or None


def validate_library_id(value: Any, *, allow_builtin: bool = True) -> str:
    library_id = _exact_text(value, "library_id", 63)
    if LIBRARY_ID_PATTERN.fullmatch(library_id or "") is None:
        raise RecipeLibraryError("library_id is invalid")
    if not allow_builtin and library_id == "builtin":
        raise RecipeLibraryError("builtin has no external credential")
    return library_id


def validate_library_recipe_ref(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) - {"library_id", "recipe_id", "version"}:
        raise RecipeLibraryError("library_recipe_ref must contain only library_id, recipe_id and version")
    result = {
        "library_id": validate_library_id(value.get("library_id")),
        "recipe_id": _exact_text(value.get("recipe_id"), "library_recipe_ref.recipe_id", 300) or "",
    }
    if value.get("version") is not None:
        result["version"] = _exact_text(value.get("version"), "library_recipe_ref.version", 300) or ""
    return result


def normalize_label_name(value: Any) -> tuple[str, str]:
    """Return a safe display name and its comparison key."""
    if not isinstance(value, str):
        raise RecipeLibraryError("recipe library label name must be text")
    display = " ".join(unicodedata.normalize("NFC", value).split())
    if (
        not display
        or len(display) > 100
        or any(
            ord(character) < 32
            or ord(character) == 127
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in display
        )
    ):
        raise RecipeLibraryError("recipe library label name is invalid")
    comparison = unicodedata.normalize("NFKC", display).casefold()
    return display, comparison


def validate_library_label_ref(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) - {"library_id", "label_id", "version"}:
        raise RecipeLibraryError(
            "library_label_ref must contain only library_id, label_id and version"
        )
    result = {
        "library_id": validate_library_id(value.get("library_id"), allow_builtin=False),
        "label_id": _exact_text(
            value.get("label_id"), "library_label_ref.label_id", 300
        )
        or "",
    }
    if value.get("version") is not None:
        result["version"] = _exact_text(
            value.get("version"), "library_label_ref.version", 300
        ) or ""
    return result


def library_recipe_key(value: Any) -> str:
    reference = validate_library_recipe_ref(value)
    encoded = base64.urlsafe_b64encode(reference["recipe_id"].encode("utf-8")).decode("ascii").rstrip("=")
    return f"library:{reference['library_id']}:{encoded}"


def library_recipe_key_aliases(value: Any) -> set[str]:
    if not isinstance(value, str):
        return set()
    result = {value}
    if value.startswith("bank:") and value[5:]:
        result.add(library_recipe_key({"library_id": "builtin", "recipe_id": value[5:]}))
    elif value.startswith("library:builtin:"):
        encoded = value.removeprefix("library:builtin:")
        try:
            padding = "=" * (-len(encoded) % 4)
            recipe_id = base64.b64decode(encoded + padding, altchars=b"-_", validate=True).decode("utf-8")
            reference = validate_library_recipe_ref({"library_id": "builtin", "recipe_id": recipe_id})
        except (ValueError, UnicodeError, RecipeLibraryError):
            return result
        result.add(f"bank:{reference['recipe_id']}")
    return result


def _normalized_host(hostname: str) -> str:
    raw = hostname.casefold().rstrip(".")
    if not raw or "%" in raw:
        raise RecipeLibraryError("recipe library base_url hostname is invalid")
    try:
        return ipaddress.ip_address(raw).compressed
    except ValueError:
        try:
            host = raw.encode("idna").decode("ascii").casefold().rstrip(".")
        except UnicodeError as exc:
            raise RecipeLibraryError("recipe library base_url hostname is invalid") from exc
        if not host or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in host.split(".")
        ):
            raise RecipeLibraryError("recipe library base_url hostname is invalid")
        return host


def normalize_library_origin(value: Any, *, allow_insecure_http: bool = False) -> str:
    text = _exact_text(value, "recipe library base_url", 2_048)
    if any(character == "\\" or character.isspace() for character in text or ""):
        raise RecipeLibraryError("recipe library base_url contains forbidden characters")
    try:
        parsed = urlsplit(text or "")
        port = parsed.port
    except ValueError as exc:
        raise RecipeLibraryError("recipe library base_url is invalid") from exc
    if "@" in parsed.netloc or parsed.netloc.endswith(":") or "?" in (text or "") or "#" in (text or "") or parsed.path not in {"", "/"}:
        raise RecipeLibraryError("recipe library base_url must be one origin without userinfo, path, query or fragment")
    scheme = parsed.scheme.casefold()
    if scheme not in {"https", "http"} or not parsed.hostname:
        raise RecipeLibraryError("recipe library base_url must be an absolute HTTP(S) origin")
    host = _normalized_host(parsed.hostname)
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if scheme == "http" and not loopback and not allow_insecure_http:
        raise RecipeLibraryError("non-loopback recipe library HTTP requires explicit local allow_insecure_http")
    if scheme == "https" and allow_insecure_http:
        raise RecipeLibraryError("allow_insecure_http is valid only for an HTTP origin")
    if port in ({443} if scheme == "https" else {80}):
        port = None
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    return urlunsplit((scheme, netloc, "", "", ""))


def normalize_library_configuration(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_libraries = value.get("recipe_libraries")
    if raw_libraries is None:
        raw_libraries = []
    if not isinstance(raw_libraries, list) or len(raw_libraries) > MAX_LIBRARY_CONNECTIONS:
        raise RecipeLibraryError(f"recipe_libraries must be a list with at most {MAX_LIBRARY_CONNECTIONS} entries")
    libraries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_libraries:
        if not isinstance(raw, Mapping):
            raise RecipeLibraryError("recipe_libraries entries must be objects")
        allowed = {"library_id", "provider", "display_name", "base_url", "read_only", "allow_insecure_http"}
        if set(raw) - allowed:
            raise RecipeLibraryError("recipe library contains unsupported or secret-bearing configuration")
        library_id = validate_library_id(raw.get("library_id"))
        if library_id.casefold() in seen:
            raise RecipeLibraryError("recipe library_id must be unique case-insensitively")
        seen.add(library_id.casefold())
        provider = _exact_text(raw.get("provider"), "recipe library provider", 40)
        if provider not in SUPPORTED_LIBRARY_PROVIDERS:
            raise RecipeLibraryError("recipe library provider is invalid")
        read_only = raw.get("read_only", False)
        if not isinstance(read_only, bool):
            raise RecipeLibraryError("recipe library read_only must be true or false")
        if library_id == "builtin":
            if provider != "builtin" or set(raw) - {"library_id", "provider", "read_only"} or read_only:
                raise RecipeLibraryError("builtin is reserved and cannot be redefined")
            libraries.append({"library_id": "builtin", "provider": "builtin", "read_only": False})
            continue
        if provider == "builtin":
            raise RecipeLibraryError("only builtin may use the builtin provider")
        allow_insecure = raw.get("allow_insecure_http", False)
        if not isinstance(allow_insecure, bool):
            raise RecipeLibraryError("allow_insecure_http must be true or false")
        item: dict[str, Any] = {
            "library_id": library_id,
            "provider": provider,
            "base_url": normalize_library_origin(raw.get("base_url"), allow_insecure_http=allow_insecure),
            "read_only": read_only,
        }
        if allow_insecure:
            item["allow_insecure_http"] = True
        if raw.get("display_name") is not None:
            item["display_name"] = _exact_text(raw.get("display_name"), "recipe library display_name", 100)
        libraries.append(item)
    if "builtin" not in seen:
        libraries.insert(0, {"library_id": "builtin", "provider": "builtin", "read_only": False})
    primary = value.get("primary_recipe_library_id", "builtin")
    if not isinstance(primary, str) or primary not in {item["library_id"] for item in libraries}:
        raise RecipeLibraryError("primary_recipe_library_id must name one exact configured library_id")
    return {"primary_recipe_library_id": primary, "recipe_libraries": libraries}


def secret_path(home: Path | str, library_id: Any) -> Path:
    checked = validate_library_id(library_id, allow_builtin=False)
    return Path(home) / "secrets" / "recipe-libraries" / f"{checked}.json"


def load_library_secret(home: Path | str, library_id: Any, *, service_uid: int | None = None) -> dict[str, Any]:
    path = secret_path(home, library_id)
    expected_uid = os.geteuid() if service_uid is None else service_uid
    try:
        directory = path.parent.stat(follow_symlinks=False)
        secrets_root = path.parent.parent.stat(follow_symlinks=False)
        file_status = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o777 != 0o700 or directory.st_uid != expected_uid:
            raise RecipeLibraryError("recipe library secrets directory is not private")
        if not stat.S_ISDIR(secrets_root.st_mode) or secrets_root.st_mode & 0o777 != 0o700 or secrets_root.st_uid != expected_uid:
            raise RecipeLibraryError("recipe library secrets directory is not private")
        if not stat.S_ISREG(file_status.st_mode) or file_status.st_mode & 0o777 != 0o600 or file_status.st_uid != expected_uid:
            raise RecipeLibraryError("recipe library credential file is not private")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if opened.st_ino != file_status.st_ino or opened.st_dev != file_status.st_dev or opened.st_size > MAX_SECRET_BYTES:
                raise RecipeLibraryError("recipe library credential file is invalid")
            payload = os.read(descriptor, MAX_SECRET_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(payload) > MAX_SECRET_BYTES:
            raise RecipeLibraryError("recipe library credential file is invalid")
        decoded = json.loads(payload.decode("utf-8"))
    except RecipeLibraryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RecipeLibraryError("recipe library credential is unavailable") from exc
    if not isinstance(decoded, dict) or len(decoded) > 20 or not all(isinstance(key, str) for key in decoded):
        raise RecipeLibraryError("recipe library credential file is invalid")
    return decoded


def require_authenticated_origin(configured_origin: Any, request_url: Any) -> str:
    origin = normalize_library_origin(configured_origin, allow_insecure_http=str(configured_origin).startswith("http://"))
    target = _exact_text(request_url, "authenticated recipe library URL", 4_096)
    if any(character == "\\" or character.isspace() for character in target or ""):
        raise RecipeLibraryError("authenticated recipe library URL is invalid")
    try:
        parsed = urlsplit(target or "")
        port = parsed.port
    except ValueError as exc:
        raise RecipeLibraryError("authenticated recipe library URL is invalid") from exc
    if "@" in parsed.netloc or parsed.netloc.endswith(":") or "#" in (target or "") or not parsed.hostname:
        raise RecipeLibraryError("authenticated recipe library URL is invalid")
    host = _normalized_host(parsed.hostname)
    target_port = port or (443 if parsed.scheme.casefold() == "https" else 80 if parsed.scheme.casefold() == "http" else None)
    configured = urlsplit(origin)
    configured_port = configured.port or (443 if configured.scheme == "https" else 80)
    if (parsed.scheme.casefold(), host, target_port) != (configured.scheme, configured.hostname, configured_port):
        raise RecipeLibraryError("refusing to attach recipe library credentials across origins")
    return target or ""


def reject_authenticated_redirect(status_code: Any) -> None:
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise RecipeLibraryError("recipe library response status is invalid")
    if 300 <= status_code < 400:
        raise RecipeLibraryError("authenticated recipe library redirects are forbidden")


class RecipeLibraryAdapter(ABC):
    """Connection-scoped semantic adapter; authenticated calls never follow redirects."""

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "provider": "unknown",
            "server_version": None,
            "read_only": True,
            **{name: False for name in CAPABILITY_NAMES},
        }

    def search(self, query: str, filters: Mapping[str, Any], cursor: str | None, limit: int) -> Mapping[str, Any]:
        raise RecipeLibraryDefiniteError("recipe library search is unsupported")

    def get(self, library_recipe_ref: Mapping[str, str]) -> Mapping[str, Any]:
        raise RecipeLibraryDefiniteError("recipe library get is unsupported")

    def create_from_snapshot(self, snapshot: Mapping[str, Any], operation: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RecipeLibraryDefiniteError("recipe library create is unsupported")

    def reconcile_create(self, snapshot: Mapping[str, Any], operation: Mapping[str, Any]) -> Mapping[str, Any] | None:
        raise RecipeLibraryDefiniteError("recipe library create reconciliation is unsupported")

    def get_favorite(self, library_recipe_ref: Mapping[str, str]) -> Mapping[str, Any]:
        raise RecipeLibraryDefiniteError("recipe library favorite reads are unsupported")

    def set_favorite(
        self,
        library_recipe_ref: Mapping[str, str],
        is_favorite: bool,
        *,
        expected_favorite_revision: Any = None,
    ) -> None:
        raise RecipeLibraryDefiniteError("recipe library favorite mutation is unsupported")

    def list_labels(self) -> list[Mapping[str, Any]]:
        raise RecipeLibraryDefiniteError("recipe library label reads are unsupported")

    def get_recipe_labels(
        self, library_recipe_ref: Mapping[str, str]
    ) -> list[Mapping[str, Any]]:
        raise RecipeLibraryDefiniteError("recipe library label reads are unsupported")

    def set_label(
        self,
        library_recipe_ref: Mapping[str, str],
        library_label_ref: Mapping[str, str],
        present: bool,
        *,
        expected_label_revision: Any = None,
    ) -> None:
        raise RecipeLibraryDefiniteError("recipe library label mutation is unsupported")

    def create_label(self, name: str, *, idempotency_key: str) -> Mapping[str, Any]:
        raise RecipeLibraryDefiniteError("recipe library label creation is unsupported")

    def reconcile_label_create(
        self, name: str, operation: Mapping[str, Any]
    ) -> Mapping[str, Any] | None:
        raise RecipeLibraryDefiniteError(
            "recipe library label creation reconciliation is unsupported"
        )


def verified_capabilities(adapter: RecipeLibraryAdapter, connection: Mapping[str, Any]) -> dict[str, Any]:
    try:
        raw = adapter.capabilities()
    except Exception as exc:
        raise RecipeLibraryError("recipe library capability probe is unavailable") from exc
    if not isinstance(raw, Mapping):
        raise RecipeLibraryError("recipe library capability probe returned invalid data")
    if raw.get("provider") != connection.get("provider"):
        raise RecipeLibraryError("recipe library capability probe returned the wrong provider")
    version = raw.get("server_version")
    if version is not None:
        version = _exact_text(version, "recipe library server_version", 100)
    if not isinstance(raw.get("read_only"), bool):
        raise RecipeLibraryError("recipe library capability probe returned invalid read_only data")
    result: dict[str, Any] = {
        "provider": raw["provider"],
        "server_version": version,
        "read_only": bool(connection.get("read_only")) or raw["read_only"],
    }
    for name in CAPABILITY_NAMES:
        if not isinstance(raw.get(name), bool):
            raise RecipeLibraryError(f"recipe library capability {name} is not semantic boolean data")
        result[name] = raw[name] and not (result["read_only"] and name in WRITE_CAPABILITIES)
    return result


def load_optional_adapter(connection: Mapping[str, Any], credential: Mapping[str, Any]) -> RecipeLibraryAdapter:
    """Load a provider module only for an explicitly configured external connection."""
    provider = connection.get("provider")
    if provider not in {"mealie", "recipesage"}:
        raise RecipeLibraryError("recipe library provider has no optional adapter")
    try:
        module = importlib.import_module(f"recipe_library_{provider}")
        factory = getattr(module, "create_adapter")
    except Exception as exc:
        raise RecipeLibraryError("optional recipe library adapter is not installed") from exc
    try:
        adapter = factory(dict(connection), dict(credential))
    except Exception as exc:
        raise RecipeLibraryError("optional recipe library adapter could not be initialized") from exc
    if not isinstance(adapter, RecipeLibraryAdapter):
        raise RecipeLibraryError("optional recipe library adapter is invalid")
    return adapter
