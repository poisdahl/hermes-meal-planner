#!/usr/bin/env python3
"""Copy only durable household values into the small state format."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

from core import StateStore, _atomic_json, _validate_product_items, initial_state


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not an object")
    return value


def migrate(config: dict[str, Any], planning: dict[str, Any], schedule: dict[str, Any] | None = None) -> dict[str, Any]:
    result = initial_state(config)
    documents = planning.get("documents") if isinstance(planning.get("documents"), dict) else {}
    product_favorites = (documents.get("favorites") or {}).get("items", [])
    recurring = (documents.get("recurring_items") or {}).get("items", [])
    if isinstance(product_favorites, list):
        _validate_product_items(product_favorites, "product_favorites")
        result["product_favorites"] = product_favorites
    if isinstance(recurring, list):
        result["recurring_items"] = recurring
    preferences = (documents.get("preferences") or {}).get("content", "")
    if isinstance(preferences, str):
        email = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", preferences, re.IGNORECASE)
        if email:
            result["email_recipient"] = email.group(0)
    if schedule:
        records = schedule.get("schedules", schedule.get("records", []))
        if isinstance(records, list) and len(records) == 1 and isinstance(records[0], dict):
            source = records[0]
            for key in ("enabled", "weekday", "time", "timezone", "mode", "delivery", "maximum_total", "auto_checkout"):
                if key in source:
                    result["schedule"][key] = source[key]
            if isinstance(result["schedule"].get("delivery"), dict):
                result["schedule"]["delivery"].setdefault("strategy", "cheapest")
    # Deliberately omit menu/history/checkpoints/leases/attempts/generations.
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--old-planning", type=Path, required=True)
    parser.add_argument("--old-schedule", type=Path)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    config = load(args.config)
    planning = load(args.old_planning)
    schedule = load(args.old_schedule) if args.old_schedule and args.old_schedule.exists() else None
    result = migrate(config, planning, schedule)
    store = StateStore(args.output_directory, config)
    _atomic_json(store.path, result)
    print(json.dumps({"ok": True, "product_favorites_count": len(result["product_favorites"]), "recurring_items": len(result["recurring_items"]), "schedule_enabled": result["schedule"]["enabled"], "menu_migrated": False, "checkout_migrated": False, "email_recipient_configured": bool(result["email_recipient"])}))


if __name__ == "__main__":
    main()
