#!/usr/bin/env python3
"""Bounded native JSON/JSONL import for one household recipe bank."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

from recipes import MAX_IMPORT_RECORDS, MAX_RECIPE_BYTES, RecipeError, RecipeStore


MAX_IMPORT_BYTES = 64 * 1024 * 1024


def _json_records(path: Path) -> Iterator[Any]:
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise RecipeError("import file is too large")
    if path.suffix.casefold() == ".jsonl":
        count = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    if len(line.encode()) > MAX_RECIPE_BYTES * 2:
                        raise RecipeError(f"JSONL line {line_number} is too large")
                    try:
                        value = json.loads(line)
                    except (ValueError, RecursionError) as exc:
                        raise RecipeError(f"JSONL line {line_number} is invalid") from exc
                    count += 1
                    if count > MAX_IMPORT_RECORDS:
                        raise RecipeError(f"import exceeds {MAX_IMPORT_RECORDS} recipes")
                    yield value
        except (OSError, UnicodeDecodeError) as exc:
            raise RecipeError("JSONL import is unreadable or invalid") from exc
        return
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RecipeError("JSON import is unreadable or invalid") from exc
    values = value if isinstance(value, list) else [value]
    if len(values) > MAX_IMPORT_RECORDS:
        raise RecipeError(f"import exceeds {MAX_IMPORT_RECORDS} recipes")
    yield from values


def main() -> None:
    parser = argparse.ArgumentParser(description="Import native Hermes Recipe JSON or JSONL")
    parser.add_argument("path", type=Path)
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", choices=("active", "draft"), default="active")
    parser.add_argument("--backup", type=Path, help="write a verified SQLite backup before a committed import")
    args = parser.parse_args()
    state_path = args.state_directory / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise SystemExit("state directory has no readable household state") from exc
    household = state.get("household") if isinstance(state, dict) else None
    if not isinstance(household, str) or not household:
        raise SystemExit("state directory has no household identity")
    store = RecipeStore(args.state_directory / "recipes.sqlite3", household)
    try:
        backup = None
        if args.backup:
            if args.dry_run:
                raise RecipeError("--backup is not used with --dry-run")
            backup = str(store.backup(args.backup))
        result = store.import_records(_json_records(args.path), dry_run=args.dry_run, default_status=args.status)
    except (OSError, RecipeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({**result, "backup": backup}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
