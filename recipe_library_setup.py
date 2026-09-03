#!/usr/bin/env python3
"""Interactive local setup for optional personal recipe-library connections."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import getpass
import json
import os
from pathlib import Path
import stat
import subprocess
import sqlite3
import tempfile
from typing import Any, Mapping

from recipe_libraries import (
    MAX_SECRET_BYTES,
    RecipeLibraryError,
    load_library_secret,
    load_optional_adapter,
    normalize_library_configuration,
    secret_path,
    validate_library_id,
    verified_capabilities,
)
from recipes import RecipeError, RecipeStore


def _read_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecipeLibraryError("household config is unavailable") from exc
    if not isinstance(value, dict):
        raise RecipeLibraryError("household config is invalid")
    normalize_library_configuration(value)
    return value


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    status = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        raise RecipeLibraryError("private setup directory is invalid")
    os.chmod(path, 0o700)


def atomic_private_json(path: Path, value: object) -> None:
    if path.parent.name == "recipe-libraries":
        _private_directory(path.parent.parent)
    _private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def _configuration_lock(config_path: Path):
    """Serialize setup helpers across prompts and all config/credential writes."""
    descriptor = os.open(
        config_path.with_name(f".{config_path.name}.lock"),
        os.O_RDWR | os.O_CREAT,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _prompt_credential() -> dict[str, Any]:
    raw = getpass.getpass("Credential JSON (hidden; never passed as a command argument): ")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecipeLibraryError("credential JSON is invalid") from exc
    if (
        not isinstance(value, dict)
        or not value
        or len(value) > 20
        or not all(isinstance(key, str) for key in value)
        or len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_SECRET_BYTES
    ):
        raise RecipeLibraryError("credential JSON must be a non-empty object")
    return value


def _confirm(expected: str) -> None:
    entered = input(f"Type exactly '{expected}' to confirm: ")
    if entered != expected:
        raise RecipeLibraryError("no change made")


def _probe(connection: Mapping[str, Any], credential: Mapping[str, Any]) -> dict[str, Any]:
    try:
        adapter = load_optional_adapter(connection, credential)
        capabilities = verified_capabilities(adapter, connection)
    except RecipeLibraryError:
        raise
    except Exception as exc:
        raise RecipeLibraryError("recipe library read-only probe failed") from exc
    if not capabilities["search"] or not capabilities["get"]:
        raise RecipeLibraryError("connection cannot meet the required read-only search/get contract")
    return capabilities


def _restart_running_service() -> None:
    if os.uname().sysname == "Darwin":
        label = os.environ.get("MEAL_PLANNER_LAUNCHD_LABEL", "com.hermes-agent.meal-planner")
        target = f"gui/{os.getuid()}/{label}"
        status = subprocess.run(
            ["launchctl", "print", target],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if status.returncode != 0 or "state = running" not in status.stdout:
            return
        subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            check=True,
            stdin=subprocess.DEVNULL,
        )
    else:
        subprocess.run(
            ["systemctl", "--user", "try-restart", "hermes-meal-planner.service"],
            check=True,
            stdin=subprocess.DEVNULL,
        )


def _restart_after_change(args: argparse.Namespace) -> None:
    if not getattr(args, "no_restart", False):
        _restart_running_service()


def _connection(config: Mapping[str, Any], library_id: str) -> dict[str, Any]:
    normalized = normalize_library_configuration(config)
    matches = [item for item in normalized["recipe_libraries"] if item["library_id"] == library_id]
    if len(matches) != 1:
        raise RecipeLibraryError("library_id must name one exact configured recipe library")
    return matches[0]


def _ensure_no_active_operations(state_directory: Path, library_id: str) -> None:
    database = state_directory / "recipes.sqlite3"
    if not database.exists():
        return
    try:
        connection = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0
        )
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_operations'"
            ).fetchone()
            if table is None:
                return
            active = connection.execute(
                "SELECT 1 FROM library_operations WHERE library_id=? "
                "AND status IN ('pending','uncertain') LIMIT 1",
                (library_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RecipeLibraryError("recipe library operation journal is unavailable") from exc
    if active is not None:
        raise RecipeLibraryError(
            "resolve pending or uncertain operations before removing this connection"
        )


def add_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    with _configuration_lock(args.config):
        return _add_connection(args, _read_config(args.config))


def _add_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id, allow_builtin=False)
    current = normalize_library_configuration(config)
    if library_id in {item["library_id"] for item in current["recipe_libraries"]}:
        raise RecipeLibraryError("library_id is already configured")
    allow_insecure = False
    if str(args.base_url).startswith("http://") and not any(
        marker in str(args.base_url).casefold() for marker in ("localhost", "127.0.0.1", "[::1]")
    ):
        _confirm(f"allow insecure HTTP for {library_id}")
        allow_insecure = True
    candidate = {
        "library_id": library_id,
        "provider": args.provider,
        "base_url": args.base_url,
        "read_only": args.read_only,
    }
    if args.display_name is not None:
        candidate["display_name"] = args.display_name
    if allow_insecure:
        candidate["allow_insecure_http"] = True
    proposed = dict(config)
    proposed["recipe_libraries"] = [*current["recipe_libraries"], candidate]
    normalized = normalize_library_configuration(proposed)
    checked = next(item for item in normalized["recipe_libraries"] if item["library_id"] == library_id)
    credential = _prompt_credential()
    capabilities = _probe(checked, credential)
    _confirm(f"add {library_id}")
    path = secret_path(args.home, library_id)
    if path.exists():
        raise RecipeLibraryError("credential file already exists; use update-credential")
    _private_directory(path.parent.parent)
    atomic_private_json(path, credential)
    config_written = False
    try:
        proposed["recipe_libraries"] = normalized["recipe_libraries"]
        proposed.setdefault("primary_recipe_library_id", current["primary_recipe_library_id"])
        atomic_private_json(args.config, proposed)
        config_written = True
        state_directory = getattr(args, "state_directory", None) or args.home / "state"
        RecipeStore(state_directory / "recipes.sqlite3", proposed["household"]).enable_library_connection(library_id)
    except Exception as exc:
        if config_written:
            atomic_private_json(args.config, config)
        path.unlink(missing_ok=True)
        if isinstance(exc, RecipeError):
            raise RecipeLibraryError(str(exc)) from exc
        raise
    _restart_after_change(args)
    return {"changed": True, "library_id": library_id, "primary": False, "capabilities": capabilities}


def test_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id, allow_builtin=False)
    connection = _connection(config, library_id)
    credential = load_library_secret(args.home, library_id)
    return {"changed": False, "library_id": library_id, "capabilities": _probe(connection, credential)}


def update_credential(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    with _configuration_lock(args.config):
        return _update_credential(args, _read_config(args.config))


def _update_credential(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id, allow_builtin=False)
    connection = _connection(config, library_id)
    credential = _prompt_credential()
    capabilities = _probe(connection, credential)
    _confirm(f"update credential {library_id}")
    path = secret_path(args.home, library_id)
    _private_directory(path.parent.parent)
    atomic_private_json(path, credential)
    _restart_after_change(args)
    return {"changed": True, "library_id": library_id, "capabilities": capabilities}


def set_primary(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    with _configuration_lock(args.config):
        return _set_primary(args, _read_config(args.config))


def _set_primary(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id)
    connection = _connection(config, library_id)
    capabilities = None
    if library_id != "builtin":
        capabilities = _probe(connection, load_library_secret(args.home, library_id))
        if not capabilities["create_from_discovery"]:
            raise RecipeLibraryError("connection cannot be primary because it cannot save recipes")
    _confirm(f"set primary {library_id}")
    config["primary_recipe_library_id"] = library_id
    normalized = normalize_library_configuration(config)
    config.update(normalized)
    atomic_private_json(args.config, config)
    _restart_after_change(args)
    return {"changed": True, "library_id": library_id, "primary": True, "capabilities": capabilities}


def remove_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    with _configuration_lock(args.config):
        return _remove_connection(args, _read_config(args.config))


def _remove_connection(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    library_id = validate_library_id(args.library_id, allow_builtin=False)
    normalized = normalize_library_configuration(config)
    _connection(config, library_id)
    if normalized["primary_recipe_library_id"] == library_id:
        raise RecipeLibraryError("change primary with set-primary before removing this connection")
    state_directory = getattr(args, "state_directory", None) or args.home / "state"
    _ensure_no_active_operations(state_directory, library_id)
    _confirm(f"remove {library_id} and its credential")
    recipe_store = RecipeStore(state_directory / "recipes.sqlite3", config["household"])
    try:
        recipe_store.disable_library_connection(library_id)
    except RecipeError as exc:
        raise RecipeLibraryError(str(exc)) from exc
    original_config = deepcopy(config)
    config["recipe_libraries"] = [
        item for item in normalized["recipe_libraries"] if item["library_id"] != library_id
    ]
    config.update(normalize_library_configuration(config))
    try:
        atomic_private_json(args.config, config)
    except Exception:
        recipe_store.enable_library_connection(library_id)
        raise
    try:
        secret_path(args.home, library_id).unlink(missing_ok=True)
    except OSError as exc:
        atomic_private_json(args.config, original_config)
        try:
            recipe_store.enable_library_connection(library_id)
        except RecipeError as rollback_exc:
            raise RecipeLibraryError(
                "credential removal failed and journal rollback is unavailable"
            ) from rollback_exc
        raise RecipeLibraryError("credential removal failed; no change made") from exc
    _restart_after_change(args)
    return {"changed": True, "library_id": library_id, "removed": True}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Configure optional Meal Planner recipe libraries locally")
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--home", type=Path, required=True)
    result.add_argument(
        "--state-directory", type=Path,
        help="Recipe state directory; defaults to HOME/state",
    )
    result.add_argument(
        "--no-restart", action="store_true",
        help="Do not restart a user service; use only when an approved external supervisor restart follows",
    )
    commands = result.add_subparsers(dest="action", required=True)
    add = commands.add_parser("add")
    add.add_argument("--library-id", required=True)
    add.add_argument("--provider", choices=("mealie", "recipesage"), required=True)
    add.add_argument("--base-url", required=True)
    add.add_argument("--display-name")
    add.add_argument("--read-only", action="store_true")
    for action in ("test", "update-credential", "set-primary", "remove"):
        command = commands.add_parser(action)
        command.add_argument("--library-id", required=True)
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        config = _read_config(args.config)
        handler = {
            "add": add_connection,
            "test": test_connection,
            "update-credential": update_credential,
            "set-primary": set_primary,
            "remove": remove_connection,
        }[args.action]
        result = handler(args, config)
    except RecipeLibraryError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
