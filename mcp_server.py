#!/usr/bin/env python3
"""Hermes stdio MCP surface for the household-local meal service."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer


SOCKET = Path(os.environ.get("MEAL_PLANNER_SOCKET", "/run/meal-planner/service.sock"))


def rpc_timeout(operation: str, arguments: dict[str, Any]) -> int:
    order_operation = operation == "orders"
    cart_change = operation == "cart" and arguments.get("action") != "get"
    protected_delivery = operation == "delivery" and arguments.get("action") == "select"
    return 300 if operation == "checkout" or order_operation or cart_change or protected_delivery else 120


def rpc(operation: str, **arguments: Any) -> dict[str, Any]:
    request = {"operation": operation, **arguments}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(rpc_timeout(operation, arguments))
        connection.connect(str(SOCKET))
        connection.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        data = b""
        while b"\n" not in data and len(data) <= 2 * 1024 * 1024:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    try:
        response = json.loads(data.split(b"\n", 1)[0])
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError("meal planner service returned no valid response") from exc
    if response.get("ok") is not True:
        raise RuntimeError(str(response.get("error") or "meal planner operation failed"))
    return response["result"]


server = MCPServer(
    "meal-planner",
    description="Products, recipes, cart, menus and settings for this household's configured grocery provider.",
    instructions="Use the current household and configured provider only. Follow returned checkout instructions; checkout, existing-order payment and cancellation require one fresh explicit confirmation after prepare.",
    version="1.3.0",
)


@server.tool(description="Show the local household name, masked integration state, schedule and guarded scheduled-checkout setting.")
def meal_planner_status() -> dict[str, Any]:
    return rpc("status")


@server.tool(description="Show, update or reset household meal preferences, or set the private email recipient. Reversible preference writes need no code.")
def meal_planner_profile(action: Literal["show", "update", "reset", "set_email"] = "show", changes: dict[str, Any] | None = None, paths: list[str] | None = None, email: str | None = None) -> dict[str, Any]:
    return rpc("profile", action=action, changes=changes or {}, paths=paths, email=email)


@server.tool(description="List, add or remove local favorites. For add, pass the exact product_id and product name returned by search as top-level arguments. Favorites never change the cart.")
def meal_planner_favorites(action: Literal["list", "add", "remove"] = "list", product_id: str | None = None, product_name: str | None = None, quantity: int = 1) -> dict[str, Any]:
    item = {"product_id": product_id, "product_name": product_name, "quantity": quantity} if action == "add" else {}
    return rpc("favorites", action=action, item=item, product_id=product_id)


@server.tool(description="List, add, remove or calculate due fixed items. For add, pass search's exact product_id and name plus a schedule with every, unit weeks/months and optional anchor.")
def meal_planner_recurring(action: Literal["list", "add", "remove", "due"] = "list", product_id: str | None = None, product_name: str | None = None, quantity: int = 1, schedule: dict[str, Any] | None = None, date: str | None = None) -> dict[str, Any]:
    item = {"product_id": product_id, "product_name": product_name, "quantity": quantity, "schedule": schedule} if action == "add" else {}
    return rpc("recurring", action=action, item=item, product_id=product_id, date=date)


@server.tool(description="Search real products or recipes at the configured provider, or read its often-bought signal when available. This tool does not write.")
def meal_planner_catalog(action: Literal["products", "recipes", "usuals"], query: str = "", limit: int = 5) -> dict[str, Any]:
    return rpc("catalog", action=action, query=query, limit=limit)


@server.tool(description="Read the provider cart with action=get, or change it with action=change and delta operations using the exact product_id string returned by catalog unchanged. It may be numeric or a full provider path; never extract or shorten a path suffix. Positive quantity adds and negative quantity removes.")
def meal_planner_cart(action: Literal["get", "change"] = "get", operations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return rpc("cart", action=action, operations=operations or [])


@server.tool(description="List available delivery slots or select the exact slot the user requested. Selection is reversible until checkout.")
def meal_planner_delivery(action: Literal["list", "select"] = "list", dates: list[str] | None = None, address_id: int | None = None, slot_id: str | int | None = None, unattended: bool | None = None) -> dict[str, Any]:
    return rpc("delivery", action=action, dates=dates, address_id=address_id, slot_id=slot_id, unattended=unattended)


@server.tool(description="List/read orders; start or abort an exact existing-order change; or prepare, confirm and reconcile cancellation. After change_begin, use normal cart/delivery tools and protected checkout. Cancellation needs the fresh exact confirmation_id from cancel_prepare.")
def meal_planner_orders(action: Literal["list", "get", "change_begin", "change_abort", "cancel_prepare", "cancel_confirm", "cancel_reconcile"] = "list", order_id: str | None = None, confirmation_id: str | None = None, limit: int = 10) -> dict[str, Any]:
    return rpc("orders", action=action, order_id=order_id, confirmation_id=confirmation_id, limit=limit)


@server.tool(description="Get, save or clear the current complete menu. Saving requires dishes/salads with full ingredients and steps and never changes a provider cart.")
def meal_planner_menu(action: Literal["get", "save", "clear"] = "get", menu: dict[str, Any] | None = None) -> dict[str, Any]:
    return rpc("menu", action=action, menu=menu)


@server.tool(description="Show/update/disable the weekly run and guarded scheduled-checkout settings. A scheduled run always stops for fresh user confirmation.")
def meal_planner_schedule(action: Literal["show", "update", "disable", "set_cron_job"] = "show", changes: dict[str, Any] | None = None, cron_job_id: str | None = None) -> dict[str, Any]:
    return rpc("schedule", action=action, changes=changes or {}, cron_job_id=cron_job_id)


@server.tool(description="Prepare, confirm or reconcile a new checkout or an active existing-order change, or prepare one due scheduled occurrence. Every path stops for fresh explicit confirmation before confirm; for MENY then wait for the user to approve Vipps and call reconcile. Never retry an uncertain result.")
def meal_planner_checkout(action: Literal["prepare", "confirm", "reconcile", "auto"] = "prepare", occurrence: str | None = None, confirmation_id: str | None = None) -> dict[str, Any]:
    return rpc("checkout", action=action, occurrence=occurrence, confirmation_id=confirmation_id)


@server.tool(description="Schedule/status/check/test/mark the one recipe email associated with a confirmed order. Due returns exact recipient, subject and HTML; send them once, then call mark_sent for the same order only after successful delivery. Test returns marked HTML without consuming or marking the pending job.")
def meal_planner_email(action: Literal["status", "schedule", "due", "test", "mark_sent"] = "status", order_id: str | None = None, delivery_date: str | None = None) -> dict[str, Any]:
    return rpc("email", action=action, order_id=order_id, delivery_date=delivery_date)


if __name__ == "__main__":
    server.run()
