from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT
sys.path.insert(0, str(CORE))

from core import DEFAULT_PROFILE, HouseholdError, StateStore, cart_summary, due_recurring, put_item  # noqa: E402
from migrate import migrate  # noqa: E402
from oda_browser import (  # noqa: E402
    CART_URL,
    CANCELLATION_BROWSER_ARGS,
    CHECKOUT_ENTRY_URL,
    DEFAULT_BROWSER_ARGS,
    CancellationPreconditionError,
    CheckoutPreconditionError,
    OdaBrowser,
    cancellation_delivery_matches,
    cancellation_total_matches,
    clear_cancellation_cache,
    checkout_delivery_matches,
    checkout_lines_match,
    product_identity,
)
from service import Application, Server, config, menu_email_html, meny_order_matches_checkout, oda_order_matches_addition, order_matches_checkout  # noqa: E402
from meny import DEFAULT_BROWSER_ARGS as MENY_BROWSER_ARGS, MenyClient, _BrowserTransportError, meny_delivery_window_identity, meny_order_search_completed, normalize_browser_cdp, normalize_cart_snapshot, normalize_checkout_payment_snapshot, normalize_delivery_slot_ref, normalize_product_ref, vipps_dispatch_acknowledged  # noqa: E402


CONFIG = {"instance": "test", "household": "Test", "email_automation_profile": "test-email", "profile_overrides": {}}
MENY_PRODUCT = "/varer/frukt-gront/gronnsaker/kal/brokkoli/brokkoli-2000434900004"


class FakeOda:
    def __init__(self):
        self.calls = []
        self.cart = {
            "items": [{"product_id": 10, "name": "Fullkornspasta", "quantity": 1, "price": 35.0}],
            "count": 1,
            "subtotal": 35.0,
            "delivery": {"display": "Saturday 2026-09-05 09:00 - 12:00"},
            "deliveryAddress": "Eksempelveien 1",
        }
        self.orders = []
        self.tracking = "paid_and_modifiable"
        self.order_delivery = "2026-09-05"
        self.delivery_slots = {"slots": [{"id": 77, "name": "Lør 12. sep 09:00 - 12:00"}]}

    def probe(self, **_kwargs):
        return {"protocol_version": "2025-11-25", "server": {"name": "Oda MCP", "version": "1.1.0"}, "tool_count": 25}

    def call(self, tool, arguments, **_kwargs):
        self.calls.append((tool, deepcopy(arguments)))
        if tool == "get_cart":
            return deepcopy(self.cart)
        if tool == "manipulate_cart":
            return deepcopy(self.cart)
        if tool == "get_orders":
            return {"orders": deepcopy(self.orders)}
        if tool == "get_order":
            for order in self.orders:
                if str(order.get("orderNumber") or order.get("order_number")) == str(arguments["order_number"]):
                    return deepcopy(order)
            return {"order_number": arguments["order_number"], "subtotal": 35.0, "delivery_date": self.order_delivery}
        if tool == "order_tracking":
            return {"order_id": arguments["order_number"], "status": self.tracking}
        if tool == "get_delivery_slots":
            return deepcopy(self.delivery_slots)
        if tool in {"product_search", "recipe_search", "likely_to_buy", "select_delivery_slot"}:
            return {"tool": tool, "arguments": deepcopy(arguments)}
        raise AssertionError(tool)


class FakeBrowser:
    def __init__(self):
        self.checkout_clicks = 0
        self.cancel_clicks = 0
        self.oda = None
        self.cancellation_available = True
        self.review_deadlines = []
        self.submit_deadlines = []
        self.cancellation_review_deadlines = []
        self.cancellation_submit_deadlines = []
        self.confirmation_order_id = None

    def review_checkout(self, cart, *, deadline=None):
        self.review_deadlines.append(deadline)
        return {"page_digest": "a" * 64, "payment_display": "•••• 1234"}

    def submit_checkout(self, cart, review, before_click=None, *, deadline=None):
        self.submit_deadlines.append(deadline)
        if before_click:
            before_click()
        self.checkout_clicks += 1
        self.oda.orders.append({
            "order_number": "new-order",
            "grossAmount": 35.0,
            "deliveryDate": "2026-09-05",
            "deliverySlotDisplay": "Saturday 2026-09-05 09:00 - 12:00",
            "products": [{"product": {"id": 10, "name": "Fullkornspasta"}, "quantity": 1, "totalGrossAmount": "35.00"}],
        })

    def review_order_change(self, cart, order_id, order, *, deadline=None):
        self.review_deadlines.append(deadline)
        return {"page_digest": "b" * 64, "target_order_id": order_id, "payment_display": "•••• 1234"}

    def submit_order_change(self, cart, order_id, order, review, before_click=None, *, deadline=None):
        self.submit_deadlines.append(deadline)
        if before_click:
            before_click()
        self.checkout_clicks += 1
        target = next(item for item in self.oda.orders if str(item.get("orderNumber")) == order_id)
        additions = cart["items"]
        target["products"] = deepcopy(target["products"]) + [
            {"product": {"id": item["product_id"], "name": item["name"]}, "quantity": item["quantity"], "totalGrossAmount": item["price"]}
            for item in additions
        ]
        target["grossAmount"] = float(target["grossAmount"]) + float(cart["subtotal"])

    def review_delivery_change(self, order_id, order, delivery, *, deadline=None):
        return {
            "page_digest": "c" * 64,
            "summary": {"items": [], "count": 0, "total": 0.0, "delivery": deepcopy(delivery), "payment": "•••• 1234"},
            "target_order_id": order_id,
        }

    def submit_delivery_change(self, order_id, order, delivery, review, before_click=None, *, deadline=None):
        if before_click:
            before_click()
        self.checkout_clicks += 1
        target = next(item for item in self.oda.orders if str(item.get("orderNumber")) == order_id)
        target["deliverySlotDisplay"] = delivery["display"]

    def checkout_confirmation_order_id(self, *, deadline=None):
        return self.confirmation_order_id

    def review_cancellation(self, order_id, order, *, deadline=None):
        self.cancellation_review_deadlines.append(deadline)
        return {"available": self.cancellation_available, "consequence": None}

    def submit_cancellation(self, order_id, order, review, before_click=None, *, deadline=None):
        self.cancellation_submit_deadlines.append(deadline)
        if before_click:
            before_click()
        self.cancel_clicks += 1
        self.oda.tracking = "cancelled"


class FakeMeny(FakeOda):
    def __init__(self):
        super().__init__()
        self.cart = {
            "provider": "meny",
            "items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 1, "price": 35.0}],
            "count": 1,
            "subtotal": 35.0,
            "total": 35.0,
            "delivery": None,
        }
        self.confirmation_order_id = None
        self.checkout_clicks = 0
        self.tracking = "confirmed"
        self.change_begins = 0
        self.change_entered = None
        self.change_release = None
        self.cancellation_review_deadlines = []
        self.cancellation_submit_deadlines = []

    def probe(self, **_kwargs):
        return {"protocol_version": "browser-v1", "server": {"name": "MENY website"}, "tool_count": 11}

    def verify_order_change(self, order_id, code, *, deadline=None):
        return {"provider": "meny", "order_id": order_id, "code": code, "editing": order_id is not None}

    def begin_order_change(self, order_id, *, deadline=None):
        self.change_begins += 1
        if self.change_entered:
            self.change_entered.set()
        if self.change_release and not self.change_release.wait(2):
            raise HouseholdError("test MENY change timed out")
        return {"provider": "meny", "order_id": order_id, "code": "TEST-CODE-1", "editing": True}

    def abort_order_change(self, order_id, code=None, *, deadline=None):
        return {"provider": "meny", "order_id": order_id, "code": code, "aborted": True}

    def review_cancellation(self, order_id, order, *, deadline=None):
        self.cancellation_review_deadlines.append(deadline)
        return {"available": True, "consequence": None}

    def submit_cancellation(self, order_id, order, review, before_click=None, *, deadline=None):
        self.cancellation_submit_deadlines.append(deadline)
        if before_click:
            before_click()
        self.tracking = "cancelled"

    def review_checkout(self, cart, *, order_change=None, deadline=None):
        return {
            "page_digest": "d" * 64,
            "summary": {
                "items": deepcopy(cart["items"]),
                "count": cart["count"],
                "total": 40.0,
                "delivery": {"slot_id": None, "display": "Dato og tid torsdag 3. september Kl. 09:00-12:00"},
                "payment": "vipps",
            "order_lines": [{"product_id": MENY_PRODUCT, "identity": "Brokkoli 400g", "quantity": 1}],
            },
            "payment": "vipps",
            "submit_controls": 1,
            "target_order_id": (order_change or {}).get("order_id"),
            "target_order_code": (order_change or {}).get("code"),
        }

    def submit_checkout(self, cart, review, before_click=None, *, order_change=None, deadline=None):
        if before_click:
            before_click()
        self.checkout_clicks += 1
        return {"awaiting_user_payment": True, "payment": "vipps"}

    def checkout_confirmation_order_id(self, *, deadline=None):
        return self.confirmation_order_id

    def call(self, tool, arguments, **kwargs):
        if tool == "get_order":
            return deepcopy(next(item for item in self.orders if str(item.get("orderNumber")) == str(arguments["order_number"])))
        if tool == "order_tracking":
            return {"order_id": str(arguments["order_number"]), "status": self.tracking}
        return super().call(tool, arguments, **kwargs)


class CoreTests(unittest.TestCase):
    def test_mcp_saved_item_tools_build_the_internal_item_shape(self):
        class FakeMCPServer:
            def __init__(self, *_args, **_kwargs):
                pass

            def tool(self, **_kwargs):
                return lambda function: function

        mcp = types.ModuleType("mcp")
        mcp_server_package = types.ModuleType("mcp.server")
        mcp_server_module = types.ModuleType("mcp.server.mcpserver")
        mcp_server_module.MCPServer = FakeMCPServer
        spec = importlib.util.spec_from_file_location("meal_planner_mcp_server_test", CORE / "mcp_server.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "mcp": mcp,
            "mcp.server": mcp_server_package,
            "mcp.server.mcpserver": mcp_server_module,
        }):
            spec.loader.exec_module(module)
        self.assertEqual(module.rpc_timeout("cart", {"action": "change"}), 300)
        self.assertEqual(module.rpc_timeout("cart", {"action": "get"}), 120)
        self.assertEqual(module.rpc_timeout("delivery", {"action": "list"}), 300)
        module.rpc = mock.Mock(return_value={})
        module.meal_planner_favorites("add", product_id=MENY_PRODUCT, product_name="Brokkoli", quantity=2)
        module.rpc.assert_called_with(
            "favorites",
            action="add",
            item={"product_id": MENY_PRODUCT, "product_name": "Brokkoli", "quantity": 2},
            product_id=MENY_PRODUCT,
        )
        schedule = {"unit": "weeks", "every": 2, "anchor": "2026-W36"}
        module.meal_planner_recurring("add", product_id=MENY_PRODUCT, product_name="Brokkoli", quantity=1, schedule=schedule)
        module.rpc.assert_called_with(
            "recurring",
            action="add",
            item={"product_id": MENY_PRODUCT, "product_name": "Brokkoli", "quantity": 1, "schedule": schedule},
            product_id=MENY_PRODUCT,
            date=None,
        )

    def test_unix_socket_is_assigned_to_the_configured_group(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.sock"
            listener = mock.MagicMock()
            listener.accept.side_effect = RuntimeError("stop test server")
            socket_context = mock.MagicMock()
            socket_context.__enter__.return_value = listener
            with (
                mock.patch("service.socket.socket", return_value=socket_context),
                mock.patch("service.os.chown") as chown,
                mock.patch("service.os.chmod") as chmod,
                self.assertRaisesRegex(RuntimeError, "stop test server"),
            ):
                Server(path, 4321, os.getuid(), mock.Mock()).run()
            listener.bind.assert_called_once_with(str(path))
            chown.assert_called_once_with(path, -1, 4321)
            chmod.assert_called_once_with(path, 0o660)

    def test_household_config_defaults_and_casefolds_provider_for_runtime_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"household": "Test"}), encoding="utf-8")
            self.assertEqual(config(path)["provider"], "oda")
            path.write_text(json.dumps({"household": "Test", "provider": "MENY"}), encoding="utf-8")
            self.assertEqual(config(path)["provider"], "meny")

    def test_public_profile_defaults_to_seven_distinct_dinners_for_two(self):
        meals = DEFAULT_PROFILE["meals"]
        self.assertEqual(meals["people"], 2)
        self.assertEqual(meals["dishes"], 7)
        self.assertEqual(meals["batch_dishes"], 0)
        self.assertEqual(len(meals["cook_days"]), 7)
        self.assertIn("different dinner", meals["leftovers"])

    def test_meny_product_identity_is_safe_and_usable_in_lists(self):
        self.assertEqual(normalize_product_ref(MENY_PRODUCT), MENY_PRODUCT)
        self.assertEqual(normalize_product_ref("/varer/frukt-gront/gronnsaker/kal/brokkoli/brokkoli-4349"), "/varer/frukt-gront/gronnsaker/kal/brokkoli/brokkoli-4349")
        self.assertEqual(normalize_product_ref("https://meny.no" + MENY_PRODUCT), MENY_PRODUCT)
        for invalid in (
            "https://example.test" + MENY_PRODUCT,
            MENY_PRODUCT + "?token=secret",
            "/varer/../../private-123456",
            "/varer/kampanjer/plukk-og-miks-153596",
            "/oppskrifter/brokkoli-123456",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(HouseholdError):
                    normalize_product_ref(invalid)
        favorites = put_item([], {"product_id": MENY_PRODUCT, "product_name": "Brokkoli", "quantity": 1})
        self.assertEqual(favorites[0]["product_id"], MENY_PRODUCT)
        short_suffix = "/varer/frukt-gront/gronnsaker/kal/brokkoli/brokkoli-4349"
        saved = put_item([], {"product_id": short_suffix, "product_name": "Brokkoli", "quantity": 1})
        self.assertEqual(saved[0]["product_id"], short_suffix)

    def test_meny_delivery_slot_identity_is_exact_and_canonical(self):
        slot = "fra 0 kr fra 0 kroner, 3. september klokka 10:00 til 12:00"
        self.assertEqual(normalize_delivery_slot_ref(slot), (
            slot,
            "3. september klokka 10:00 til 12:00",
        ))
        for invalid in (
            "fra 0 kr, 3. september klokka 10:00 til 12:00:99",
            "fra 0 kr, 3. september klokka 24:00 til 25:00",
            "fra 0 kr, 3. september klokka 10:00 til 12:00, 4. september klokka 10:00 til 12:00",
            "3. september klokka 09:00 til 10:00, 3. september klokka 10:00 til 12:00",
            "4. september klokka 08:00 til 09:00, 3. september klokka 10:00 til 12:00",
            "4. september, 3. september klokka 10:00 til 12:00",
            "08:00 til 09:00, 3. september klokka 10:00 til 12:00",
            "fra 0 kr, 0. september klokka 10:00 til 12:00",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(HouseholdError):
                    normalize_delivery_slot_ref(invalid)

    def test_live_shaped_cart_is_normalized_for_checkout(self):
        cart = {
            "groups": [{"items": [{"product": {"id": 10, "name": "Fullkornspasta", "description": "500 g", "brand": "Testmerke", "price": "35.00"}, "quantity": 1.0, "totalGrossAmount": "35.00"}]}],
            "productQuantityCount": 1,
            "totalGrossAmount": "35.00",
            "deliveryAddress": "Eksempelveien 1",
            "deliverySlot": {"id": 7, "name": "lørdag 09:00–12:00"},
            "isUnattendedDelivery": False,
        }
        summary = cart_summary(cart)
        self.assertEqual(summary["items"][0]["product_id"], "10")
        self.assertEqual(summary["total"], 35.0)
        self.assertEqual(summary["delivery"]["slot_id"], 7)
        expected = OdaBrowser._cart_expectation(cart)
        self.assertEqual(expected["lines"], [{"name": "Fullkornspasta", "identity": "fullkornspasta 500 g testmerke", "quantity": 1}])

    def test_checkout_product_identity_rejects_a_different_package_size(self):
        expected = [{"identity": product_identity("Karbonadedeig", "350 g", "Testmerke"), "quantity": 1}]
        self.assertTrue(checkout_lines_match(expected, [{"text": "Karbonadedeig 350 g Testmerke", "quantity": 1}]))
        self.assertFalse(checkout_lines_match(expected, [{"text": "Karbonadedeig 700 g Testmerke", "quantity": 1}]))
        self.assertFalse(checkout_lines_match(expected, [{"text": "Karbonadedeig 350 g Testmerke", "quantity": 2}]))
        self.assertFalse(checkout_lines_match(expected, [{"text": ["Karbonadedeig", "350 g", "Testmerke"], "quantity": 1}]))

    def test_checkout_product_identity_preserves_package_order(self):
        expected = [{"identity": product_identity("Melk", "2 x 1 l", "Testmerke"), "quantity": 1}]
        self.assertTrue(checkout_lines_match(expected, [{"text": "Melk 2 x 1 l Testmerke", "quantity": 1}]))
        self.assertFalse(checkout_lines_match(expected, [{"text": "Melk 1 x 2 l Testmerke", "quantity": 1}]))

    def test_checkout_product_identity_accepts_oda_display_deduplication(self):
        expected = [{"identity": product_identity("Store Lime Brasil / Colombia", "Maks 10 per kunde, Brasil / Colombia, 3 stk", ""), "quantity": 1}]
        self.assertTrue(checkout_lines_match(expected, [{"text": "Store Lime Maks 10 per kunde, Brasil / Colombia, 3 stk", "quantity": 1}]))
        self.assertFalse(checkout_lines_match(expected, [{"text": "Store Lime Maks 10 per kunde, Brasil / Colombia, 6 stk", "quantity": 1}]))
        conflicting_size = [{"identity": product_identity("Melk 1 l", "2 x 1 l", "Testmerke"), "quantity": 1}]
        self.assertFalse(checkout_lines_match(conflicting_size, [{"text": "Melk 2 x 1 l Testmerke", "quantity": 1}]))

    def test_checkout_product_identity_accepts_repeated_dom_brand(self):
        expected = [{"identity": product_identity("Zalo Ultra", "500 ml", "Zalo"), "quantity": 1}]
        self.assertTrue(checkout_lines_match(expected, [{"text": "Zalo Ultra 500 ml, Zalo", "quantity": 1}]))
        self.assertFalse(checkout_lines_match(expected, [{"text": "Zalo Ultra 750 ml, Zalo", "quantity": 1}]))

    def test_checkout_delivery_requires_one_exact_selected_tuple(self):
        expected = "Hjemlevering mellom kl 07 og 13, 3. sep"
        selected = "Vi leverer varene dine torsdag 3. september 07:00–13:00 Endre"
        self.assertTrue(checkout_delivery_matches(expected, [selected]))
        self.assertFalse(checkout_delivery_matches(expected, ["Vi leverer varene dine torsdag 3. september 09:00–12:00 Endre"]))
        self.assertFalse(checkout_delivery_matches(expected, ["Vi leverer varene dine torsdag 3. september 07:30–13:00 Endre"]))
        self.assertFalse(checkout_delivery_matches(expected, [selected + " Alternativ 08:00–14:00"]))
        self.assertFalse(checkout_delivery_matches(expected, [selected, selected]))
        self.assertFalse(checkout_delivery_matches(expected, [{"text": selected}]))

    def test_cancellation_delivery_accepts_oda_month_expansion_only(self):
        expected = "Lør 5. sep 07:00 - 13:00"
        actual = "Lør 5. september, 07:00 - 13:00"
        self.assertTrue(cancellation_delivery_matches(expected, [actual]))
        self.assertFalse(cancellation_delivery_matches(expected, ["Lør 6. september, 07:00 - 13:00"]))
        self.assertFalse(cancellation_delivery_matches(expected, ["Lør 5. september, 08:00 - 13:00"]))
        self.assertFalse(cancellation_delivery_matches(expected, [actual, "Søn 6. september, 08:00 - 14:00"]))
        self.assertFalse(cancellation_delivery_matches(expected, [actual, actual]))
        self.assertFalse(cancellation_delivery_matches("Lør 32. sep 07:00 - 13:00", ["Lør 32. september, 07:00 - 13:00"]))
        self.assertFalse(cancellation_delivery_matches("Lør 5. sep 07:99 - 13:00", ["Lør 5. september, 07:99 - 13:00"]))
        self.assertFalse(cancellation_delivery_matches(expected, ["Lør 5. september, 07:00 - 13:00:99"]))
        self.assertFalse(cancellation_delivery_matches(expected, ["Lør 5. september, 07:00 - 13:00.99"]))

    def test_cancellation_total_is_bound_to_one_total_row(self):
        self.assertTrue(cancellation_total_matches(123456, ["Total inkl. MVA Kortbetaling, NOK, kr 1234,56"]))
        self.assertTrue(cancellation_total_matches(123456, ["Totalt 1 234,56 kr"]))
        self.assertFalse(cancellation_total_matches(123456, ["Total inkl. MVA Kortbetaling, NOK, kr 1400,00"]))
        self.assertFalse(cancellation_total_matches(123456, ["Total inkl. MVA kr 1400,00 Vare kr 1234,56"]))
        self.assertFalse(cancellation_total_matches(123456, ["Total inkl. MVA kr 1400,00", "Vare kr 1234,56"]))

    def test_meny_reconcile_binds_total_count_delivery_and_vipps(self):
        summary = {
            "items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 2}],
            "count": 2,
            "total": 1200.0,
            "delivery": {"display": "Dato og tid torsdag 3. september Kl. 09:00-12:00"},
            "payment": "vipps",
            "order_lines": [{"product_id": MENY_PRODUCT, "identity": "Brokkoli 400g", "quantity": 2}],
        }
        order = {"grossAmount": 1200.0, "productQuantityCount": 2, "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00", "products": [{"identity": "Brokkoli 400g", "quantity": 2}]}
        self.assertTrue(meny_order_matches_checkout(order, summary))
        self.assertFalse(meny_order_matches_checkout({**order, "grossAmount": 1199.0}, summary))
        self.assertFalse(meny_order_matches_checkout({**order, "products": [{"identity": "Blomkål 400g", "quantity": 2}]}, summary))
        self.assertFalse(meny_order_matches_checkout(order, {**summary, "payment": "card"}))
        self.assertFalse(meny_order_matches_checkout(order, {
            **summary,
            "order_lines": [
                {"product_id": MENY_PRODUCT, "identity": "Brokkoli 400g", "quantity": 1},
                {"product_id": "/varer/frukt-gront/gronnsaker/kal/brokkoli/brokkoli-4349", "identity": "Brokkoli 400g", "quantity": 1},
            ],
        }))
        for invalid_delivery in (
            "torsdag 3. sep. kl. 09:00-12:00:99",
            "torsdag 3. sep. kl. 09:00-12:00abc",
            "torsdag 3. ukjent kl. 09:00-12:00",
        ):
            with self.subTest(invalid_delivery=invalid_delivery):
                self.assertFalse(meny_order_matches_checkout({**order, "deliverySlotDisplay": invalid_delivery}, summary))

    def test_oda_addition_reconcile_requires_exact_baseline_plus_additions(self):
        before = {"grossAmount": 100.0, "products": [{"product": {"id": 10, "name": "Pasta"}, "quantity": 1, "totalGrossAmount": "100.00"}]}
        after = {"grossAmount": 125.0, "products": before["products"] + [{"product": {"id": 20, "name": "Såpe"}, "quantity": 1, "totalGrossAmount": "25.00"}]}
        additions = {"total": 25.0, "items": [{"product_id": "20", "quantity": 1}]}
        self.assertTrue(oda_order_matches_addition(before, after, additions))
        self.assertFalse(oda_order_matches_addition(before, {**after, "grossAmount": 124.0}, additions))

    def test_cancellation_review_checks_normalized_delivery_before_click(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        opened = []
        invoked = []
        browser._open_order = opened.append
        browser._invoke = lambda *arguments, **kwargs: invoked.append((arguments, kwargs.get("browser_args"))) or {}
        results = iter([
            {"available": True, "delivery_lines": ["Lør 5. september, 07:00 - 13:00"], "total_rows": ["Total inkl. MVA Kortbetaling, NOK, kr 1234,56"]},
            {"available": True, "consequence": None},
            {"closed": True},
        ])
        scripts = []
        browser._eval = lambda script, **kwargs: scripts.append((script, kwargs.get("browser_args"))) or next(results)
        order = {"orderNumber": "test-oda-order", "grossAmount": 1234.56, "deliverySlotDisplay": "Lør 5. sep 07:00 - 13:00"}

        self.assertEqual(browser.review_cancellation("test-oda-order", order), {"available": True, "consequence": None})
        self.assertEqual(opened, ["test-oda-order"])
        self.assertEqual(invoked, [
            (("close",), CANCELLATION_BROWSER_ARGS),
            (("click", "[data-oda-household-cancel-review]"), CANCELLATION_BROWSER_ARGS),
            (("click", "[data-oda-household-cancel-dismiss]"), CANCELLATION_BROWSER_ARGS),
            (("close",), CANCELLATION_BROWSER_ARGS),
        ])
        self.assertTrue(all(browser_args == CANCELLATION_BROWSER_ARGS for _script, browser_args in scripts))
        self.assertIn("document.querySelectorAll('[data-oda-household-cancel-review]')", scripts[0][0])
        self.assertIn("marked.length!==1", scripts[0][0])

        browser._eval = lambda _script, **_kwargs: {"available": True, "delivery_lines": ["Lør 6. september, 07:00 - 13:00"]}
        invoked.clear()
        self.assertFalse(browser.review_cancellation("test-oda-order", order)["available"])
        self.assertEqual(invoked, [
            (("close",), CANCELLATION_BROWSER_ARGS),
            (("close",), CANCELLATION_BROWSER_ARGS),
        ])

        with self.assertRaisesRegex(HouseholdError, "delivery is unavailable"):
            browser.review_cancellation("test-oda-order", {**order, "deliverySlotDisplay": [order["deliverySlotDisplay"]]})

        for total in (True, "nan", "inf"):
            with self.subTest(total=total):
                with self.assertRaisesRegex(HouseholdError, "total is unavailable"):
                    browser.review_cancellation("test-oda-order", {**order, "grossAmount": total})

        browser._eval = lambda _script, **_kwargs: {"available": True, "delivery_lines": ["Lør 5. september, 07:00 - 13:00", "Søn 6. september, 08:00 - 14:00"]}
        invoked.clear()
        self.assertFalse(browser.review_cancellation("test-oda-order", order)["available"])
        self.assertEqual(invoked, [
            (("close",), CANCELLATION_BROWSER_ARGS),
            (("close",), CANCELLATION_BROWSER_ARGS),
        ])

    def test_cancellation_review_waits_for_react_hydration(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser._open_order = lambda _order_id: None
        invoked = []
        browser._invoke = lambda *arguments, **kwargs: invoked.append((arguments, kwargs.get("browser_args"))) or {}
        results = iter([
            {"available": False, "retry": True, "reason": "Oda-siden er ikke klar"},
            {"available": True, "delivery_lines": ["Lør 5. september, 07:00 - 13:00"], "total_rows": ["Total inkl. MVA Kortbetaling, NOK, kr 1234,56"]},
            {"available": True, "consequence": None},
            {"closed": True},
        ])
        browser._eval = lambda _script, **_kwargs: next(results)
        order = {"orderNumber": "test-oda-order", "grossAmount": 1234.56, "deliverySlotDisplay": "Lør 5. sep 07:00 - 13:00"}

        with mock.patch("oda_browser.time.sleep") as sleep:
            result = browser.review_cancellation("test-oda-order", order)

        self.assertEqual(result, {"available": True, "consequence": None})
        sleep.assert_called_once_with(0.5)
        self.assertIn((("click", "[data-oda-household-cancel-review]"), CANCELLATION_BROWSER_ARGS), invoked)

    def test_cancellation_cache_reset_preserves_profile_state(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        with tempfile.TemporaryDirectory() as directory:
            browser.profile = Path(directory)
            for relative in ("Default/Cache", "Default/Code Cache", "Default/Service Worker"):
                path = browser.profile / relative
                path.mkdir(parents=True)
                (path / "entry").write_text("cache")
            cookies = browser.profile / "Default/Cookies"
            cookies.write_text("session")

            clear_cancellation_cache(browser.profile)

            self.assertTrue(cookies.exists())
            self.assertEqual(cookies.read_text(), "session")
            self.assertFalse((browser.profile / "Default/Cache").exists())
            self.assertFalse((browser.profile / "Default/Code Cache").exists())
            self.assertFalse((browser.profile / "Default/Service Worker").exists())

    def test_cancellation_cache_reset_rejects_symlinked_default(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        with tempfile.TemporaryDirectory() as profile_directory, tempfile.TemporaryDirectory() as external_directory:
            browser.profile = Path(profile_directory)
            external = Path(external_directory)
            (external / "Cache").mkdir()
            external_entry = external / "Cache/entry"
            external_entry.write_text("keep")
            (browser.profile / "Default").symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(HouseholdError, "cache cannot be reset"):
                clear_cancellation_cache(browser.profile)

            self.assertTrue(external_entry.exists())
            self.assertEqual(external_entry.read_text(), "keep")

    def test_cancellation_cache_reset_delegates_to_browser_uid_when_root(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser.profile = Path("/private/browser-profile")
        browser.uid = 10001
        browser.gid = 10002
        browser._cancellation_deadline = 100.0
        completed = mock.Mock(returncode=0)

        with mock.patch("oda_browser.os.geteuid", return_value=0), mock.patch("oda_browser.time.monotonic", return_value=10.0), mock.patch("oda_browser.subprocess.run", return_value=completed) as run:
            browser._clear_cancellation_cache()

        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--clear-cancellation-cache", "/private/browser-profile"])
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)
        self.assertIsNotNone(run.call_args.kwargs["preexec_fn"])

    def test_cancellation_opens_stable_entry_and_requires_canonical_order_url(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        calls = []
        browser._invoke = lambda *arguments, **kwargs: calls.append((arguments, kwargs.get("browser_args"))) or {"url": "https://oda.com/no/account/orders/test-oda-order/"}

        browser._open_order("test-oda-order")

        self.assertEqual(calls, [(("open", "https://oda.com/no/orders/test-oda-order/"), CANCELLATION_BROWSER_ARGS)])
        browser._invoke = lambda *_arguments, **_kwargs: {"url": "https://oda.com/no/account/orders/other/"}
        with self.assertRaisesRegex(HouseholdError, "left the requested order page"):
            browser._open_order("test-oda-order")

    def test_cancellation_submit_relaunches_once_and_keeps_final_dispatch_alive(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        review = {"available": True, "consequence": None}
        browser._review_cancellation = lambda *_arguments: review
        invoked = []
        browser._invoke = lambda *arguments, **kwargs: invoked.append((arguments, kwargs.get("browser_args"))) or {}
        evaluated = []
        results = iter([{"ready": True}, {"ready": True}])
        browser._eval = lambda script, **kwargs: evaluated.append((script, kwargs.get("browser_args"))) or next(results)

        browser.submit_cancellation("test-oda-order", {}, review)

        self.assertEqual(invoked, [
            (("close",), CANCELLATION_BROWSER_ARGS),
            (("click", "[data-oda-household-cancel-submit-open]"), CANCELLATION_BROWSER_ARGS),
            (("click", "[data-oda-household-cancel-submit-final]"), CANCELLATION_BROWSER_ARGS),
        ])
        self.assertEqual(len(evaluated), 2)
        self.assertTrue(all(browser_args == CANCELLATION_BROWSER_ARGS for _script, browser_args in evaluated))

    def test_cancellation_submit_does_not_close_after_ambiguous_final_dispatch(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        review = {"available": True, "consequence": None}
        browser._review_cancellation = lambda *_arguments: review
        invoked = []

        def invoke(*arguments, **kwargs):
            invoked.append((arguments, kwargs.get("browser_args")))
            if arguments == ("click", "[data-oda-household-cancel-submit-final]"):
                raise HouseholdError("lost response after possible dispatch")
            return {}

        browser._invoke = invoke
        results = iter([{"ready": True}, {"ready": True}])
        browser._eval = lambda _script, **_kwargs: next(results)

        with self.assertRaisesRegex(HouseholdError, "lost response"):
            browser.submit_cancellation("test-oda-order", {}, review)

        self.assertEqual(invoked, [
            (("close",), CANCELLATION_BROWSER_ARGS),
            (("click", "[data-oda-household-cancel-submit-open]"), CANCELLATION_BROWSER_ARGS),
            (("click", "[data-oda-household-cancel-submit-final]"), CANCELLATION_BROWSER_ARGS),
        ])

    def test_cancellation_submit_requires_margin_before_final_dispatch(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser._cancellation_deadline = None
        review = {"available": True, "consequence": None}
        browser._review_cancellation = lambda *_arguments: review
        invoked = []
        browser._invoke = lambda *arguments, **kwargs: invoked.append((arguments, kwargs.get("browser_args"))) or {}
        results = iter([{"ready": True}, {"ready": True}])
        browser._eval = lambda _script, **_kwargs: next(results)

        with mock.patch("oda_browser.time.monotonic", return_value=10.0):
            with self.assertRaisesRegex(HouseholdError, "cancellation browser deadline reached"):
                browser.submit_cancellation("test-oda-order", {}, review, deadline=20.0)

        self.assertEqual(invoked, [
            (("close",), CANCELLATION_BROWSER_ARGS),
            (("click", "[data-oda-household-cancel-submit-open]"), CANCELLATION_BROWSER_ARGS),
            (("close",), CANCELLATION_BROWSER_ARGS),
        ])
        self.assertIsNone(browser._cancellation_deadline)

    def test_cancellation_relaunch_honors_an_expired_deadline(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser.binary = Path("/shared/agent-browser-native")
        browser.executable = Path("/usr/bin/chromium")
        browser.profile = Path("/profile")
        browser.home = Path("/home")
        browser.socket_directory = Path("/run/browser")
        browser.session = "test"
        browser.uid = 10001
        browser.gid = 10002
        browser._checkout_deadline = None
        browser._cancellation_deadline = None

        with mock.patch("oda_browser.time.monotonic", return_value=10.0):
            with mock.patch("oda_browser.subprocess.run") as run:
                with self.assertRaisesRegex(HouseholdError, "browser deadline reached"):
                    with browser._cancellation_operation(deadline=9.0):
                        pass

        run.assert_not_called()
        self.assertIsNone(browser._cancellation_deadline)

    def test_checkout_relaunches_default_mode_only_for_outer_operation(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser._checkout_deadline = None
        invoked = []
        browser._invoke = lambda *arguments, **kwargs: invoked.append((arguments, kwargs.get("browser_args"))) or {}

        with browser._checkout_operation():
            with browser._checkout_operation():
                pass

        self.assertEqual(invoked, [(("close",), DEFAULT_BROWSER_ARGS)])

    def test_checkout_relaunch_honors_an_expired_deadline(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser.binary = Path("/shared/agent-browser-native")
        browser.executable = Path("/usr/bin/chromium")
        browser.profile = Path("/profile")
        browser.home = Path("/home")
        browser.socket_directory = Path("/run/browser")
        browser.session = "test"
        browser.uid = 10001
        browser.gid = 10002
        browser._checkout_deadline = None

        with mock.patch("oda_browser.time.monotonic", return_value=10.0):
            with mock.patch("oda_browser.subprocess.run") as run:
                with self.assertRaisesRegex(HouseholdError, "deadline reached"):
                    with browser._checkout_operation(deadline=9.0):
                        pass

        run.assert_not_called()
        self.assertIsNone(browser._checkout_deadline)

    def test_order_match_rejects_malformed_or_conflicting_delivery(self):
        summary = {
            "items": [{"product_id": "10", "quantity": 1}],
            "total": 35.0,
            "delivery": {"display": "Hjemlevering mellom kl 07 og 13, 3. sep"},
        }
        order = {
            "grossAmount": 35.0,
            "deliveryDate": "2026-09-03",
            "deliverySlotDisplay": "Tor 3. sep 07:00 - 13:00",
            "products": [{"product": {"id": 10, "name": "Fullkornspasta"}, "quantity": 1, "totalGrossAmount": "35.00"}],
        }
        self.assertTrue(order_matches_checkout(order, summary))

        invalid_minutes = deepcopy(summary)
        invalid_minutes["delivery"]["display"] = "Hjemlevering mellom kl 07 og 13:99, 3. sep"
        self.assertFalse(order_matches_checkout(order, invalid_minutes))
        self.assertFalse(order_matches_checkout({**order, "deliverySlotDisplay": "Tor 3. sep 07:00 - 13:00:99"}, summary))

        conflicting_times = deepcopy(summary)
        conflicting_times["delivery"]["display"] += "; alternativ 08:00 - 14:00"
        conflicting_order = {**order, "deliverySlotDisplay": "Tor 3. sep 08:00 - 14:00"}
        self.assertFalse(order_matches_checkout(conflicting_order, conflicting_times))

        wrong_type = deepcopy(summary)
        wrong_type["delivery"]["display"] = [summary["delivery"]["display"]]
        self.assertFalse(order_matches_checkout(order, wrong_type))
        self.assertFalse(order_matches_checkout({**order, "deliverySlotDisplay": [order["deliverySlotDisplay"]]}, summary))
        self.assertFalse(order_matches_checkout({**order, "deliveryDate": [order["deliveryDate"]]}, summary))
        self.assertFalse(order_matches_checkout({**order, "deliveryDate": "not-a-date"}, summary))
        self.assertFalse(order_matches_checkout({**order, "deliverySlotDisplay": "Tor 4. sep 07:00 - 13:00"}, summary))
        self.assertFalse(order_matches_checkout({**order, "deliverySlotDisplay": "Tor 4. ukjent 07:00 - 13:00"}, summary))
        self.assertFalse(order_matches_checkout({**order, "deliverySlotDisplay": "Tor 3. separat 07:00 - 13:00"}, summary))

    def test_checkout_rejects_non_string_product_identity_fields(self):
        for brand in ({"name": "Testmerke"}, {}):
            with self.subTest(brand=brand):
                cart = {
                    "groups": [{"items": [{"product": {"id": 10, "name": "Melk", "description": "1 l", "brand": brand}, "quantity": 1, "totalGrossAmount": "20.00"}]}],
                    "productQuantityCount": 1,
                    "totalGrossAmount": "20.00",
                    "deliveryAddress": "Eksempelveien 1",
                    "deliverySlot": {"id": 7, "name": "Hjemlevering mellom kl 07 og 13, 3. sep"},
                }
                with self.assertRaisesRegex(HouseholdError, "identity is invalid"):
                    OdaBrowser._cart_expectation(cart)

    def test_checkout_accepts_explicitly_unbranded_products(self):
        cart = {
            "groups": [{"items": [{"product": {"id": 10, "name": "Fennikel Norge", "description": "Norge, 1 stk", "brand": None}, "quantity": 1, "totalGrossAmount": "20.00"}]}],
            "productQuantityCount": 1,
            "totalGrossAmount": "20.00",
            "deliveryAddress": "Eksempelveien 1",
            "deliverySlot": {"id": 7, "name": "Hjemlevering mellom kl 07 og 13, 3. sep"},
        }
        expected = OdaBrowser._cart_expectation(cart)
        self.assertEqual(expected["lines"], [{"name": "Fennikel Norge", "identity": "fennikel norge 1 stk", "quantity": 1}])

    def test_checkout_requires_a_nonempty_normalized_delivery_address(self):
        cart = {
            "groups": [{"items": [{"product": {"id": 10, "name": "Fennikel", "description": "1 stk", "brand": None}, "quantity": 1, "totalGrossAmount": "20.00"}]}],
            "productQuantityCount": 1,
            "totalGrossAmount": "20.00",
            "deliveryAddress": "  A\u030Alesund   1  ",
            "deliverySlot": {"id": 7, "name": "Hjemlevering mellom kl 07 og 13, 3. sep"},
        }
        self.assertEqual(OdaBrowser._cart_expectation(cart)["delivery_address"], "\u00c5lesund 1")
        cart["deliveryAddress"] = "  \t "
        with self.assertRaisesRegex(HouseholdError, "delivery address is unavailable"):
            OdaBrowser._cart_expectation(cart)

    def test_checkout_review_discards_transient_dom_identity_text(self):
        cart = {
            "groups": [{"items": [{"product": {"id": 8816, "name": "Synnøve Gresk Gresk yoghurt 2% Fett", "description": "2% Fett, 350 g", "brand": "Synnøve Gresk"}, "quantity": 1, "totalGrossAmount": "31.10"}]}],
            "productQuantityCount": 1,
            "totalGrossAmount": "241.80",
            "deliveryAddress": "Eksempelveien 1",
            "deliverySlot": {"id": 7, "name": "Hjemlevering mellom kl 07 og 13, 3. sep"},
        }
        browser = OdaBrowser.__new__(OdaBrowser)
        browser._navigate_to_checkout = lambda: None
        scripts = []
        extracted = {
            "url": "https://oda.com/no/checkout/confirm/",
            "authenticated": True,
            "available": True,
            "items": [{"quantity": 1, "text": "Gresk yoghurt 2% Fett, 350 g, Synnøve Gresk"}],
            "total_matches": True,
            "delivery_roots": ["Vi leverer varene dine torsdag 3. september 07:00–13:00 Endre"],
            "address_matches": True,
            "masked_payment": True,
            "payment_display": "•••• 1234",
            "submit_controls": 1,
        }
        results = iter([
            {"expanded": True},
            {"ready": True},
            deepcopy(extracted),
        ])
        browser._eval = lambda script: scripts.append(script) or next(results)

        review = browser._review_checkout(cart)

        self.assertTrue(review["line_matches"])
        self.assertTrue(review["delivery_matches"])
        self.assertNotIn("items", review)
        self.assertNotIn("delivery_roots", review)
        self.assertIn("Vi leverer varene dine", scripts[-1])

        malformed = iter([{"expanded": True}, {"ready": True}, {**extracted, "submit_controls": True}])
        browser._eval = lambda _script: next(malformed)
        with self.assertRaisesRegex(HouseholdError, "page changed"):
            browser._review_checkout(cart)

    def test_intervals(self):
        weekly = {"schedule": {"unit": "weeks", "every": 2, "anchor": "2026-W36"}}
        self.assertTrue(due_recurring(weekly, date.fromisocalendar(2026, 36, 3)))
        self.assertFalse(due_recurring(weekly, date.fromisocalendar(2026, 37, 3)))
        monthly = {"schedule": {"unit": "months", "every": 3, "anchor": "2026-08"}}
        self.assertTrue(due_recurring(monthly, date(2026, 11, 1)))
        self.assertFalse(due_recurring(monthly, date(2026, 10, 1)))

    def test_migration_copies_only_durable_documents(self):
        planning = {"documents": {
            "favorites": {"items": [{"product_id": "1", "product_name": "A", "quantity": 1}]},
            "recurring_items": {"items": [{"product_id": "2", "product_name": "B", "quantity": 1, "schedule": {"unit": "weeks", "every": 1, "anchor": None}}]},
            "preferences": {"content": "Send til owner@example.test"},
            "menu": {"phase": "checkout", "secret_attempt": "old"},
            "history": [{"old": True}],
        }}
        state = migrate(CONFIG, planning, {"schedules": []})
        self.assertEqual(len(state["favorites"]), 1)
        self.assertEqual(len(state["recurring_items"]), 1)
        self.assertEqual(state["email_recipient"], "owner@example.test")
        self.assertIsNone(state["menu"])
        self.assertIsNone(state["pending_checkout"])

    def test_profile_reset_does_not_touch_lists(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), CONFIG)
            with store.locked() as state:
                state["favorites"] = [{"product_id": "1", "product_name": "A", "quantity": 1}]
            store.update_profile({"meals": {"dishes": 4, "maximum_active_minutes": 40}, "cuisine": {"base_style": "Nordic"}, "products": {"priority": ["quality", "price"]}})
            store.reset_profile(["meals.dishes", "products.priority"])
            state = store.read()
            self.assertEqual(state["profile"]["meals"]["dishes"], 7)
            self.assertEqual(state["profile"]["cuisine"]["base_style"], "Nordic")
            self.assertEqual(state["profile"]["meals"]["maximum_active_minutes"], 40)
            self.assertNotEqual(state["profile"]["products"]["priority"], ["quality", "price"])
            self.assertEqual(len(state["favorites"]), 1)

    def test_separate_directories_isolate_households(self):
        with tempfile.TemporaryDirectory() as temp:
            a = StateStore(Path(temp) / "a", {**CONFIG, "household": "A"})
            b = StateStore(Path(temp) / "b", {**CONFIG, "household": "B"})
            with a.locked() as state:
                state["favorites"] = [{"product_id": "1", "product_name": "A", "quantity": 1}]
            self.assertEqual(len(a.read()["favorites"]), 1)
            self.assertEqual(b.read()["favorites"], [])

    def test_state_is_bound_to_one_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), CONFIG)
            self.assertEqual(store.read()["provider"], "oda")
            with self.assertRaisesRegex(HouseholdError, "belongs to provider oda"):
                StateStore(Path(temp), {**CONFIG, "provider": "meny"})

    def test_legacy_state_is_bound_to_the_configured_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), CONFIG)
            with store.locked() as state:
                del state["provider"]
            migrated = StateStore(Path(temp), CONFIG)
            self.assertEqual(migrated.read()["provider"], "oda")

    def test_checkout_starts_from_the_exact_cart_page(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        opened = []
        events = []
        invoked = []
        scripts = []
        actions = []
        results = iter([{"action": "continue"}])
        browser._open = lambda url: (opened.append(url), events.append(("open", url)))
        browser._invoke = lambda *arguments: (invoked.append(arguments), events.append(("invoke", arguments)), {})[-1]
        browser._eval = lambda script: (scripts.append(script), events.append(("eval",)), next(results))[-1]
        browser._click_action = lambda action, mouse=False: actions.append((action, mouse))
        browser._advance_checkout_path = lambda: opened.append("advanced")

        with mock.patch("oda_browser.time.sleep", side_effect=lambda seconds: events.append(("sleep", seconds))):
            browser._navigate_to_checkout()

        self.assertEqual(opened, [CART_URL, "advanced"])
        self.assertEqual(events, [
            ("open", CART_URL),
            ("sleep", 12),
            ("invoke", ("reload",)),
            ("invoke", ("snapshot",)),
            ("sleep", 5),
            ("eval",),
        ])
        self.assertEqual(invoked, [("reload",), ("snapshot",)])
        self.assertEqual(actions, [("continue", True)])
        self.assertIn(json.dumps(CART_URL), scripts[0])
        self.assertIn("data-oda-household-action", scripts[0])

    def test_checkout_waits_for_the_cart_to_render(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        opened = []
        invoked = []
        actions = []
        results = iter([
            {"action": "wait"},
            {"action": "continue"},
        ])
        browser._open = opened.append
        browser._invoke = lambda *arguments: invoked.append(arguments) or {}
        browser._eval = lambda _script: next(results)
        browser._click_action = lambda action, mouse=False: actions.append((action, mouse))
        browser._advance_checkout_path = lambda: opened.append("advanced")

        with mock.patch("oda_browser.time.sleep"):
            browser._navigate_to_checkout()

        self.assertEqual(opened, [CART_URL, "advanced"])
        self.assertEqual(invoked, [("reload",), ("snapshot",)])
        self.assertEqual(actions, [("continue", True)])

    def test_checkout_reloads_a_read_only_cart_shell(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        opened = []
        events = []
        invoked = []
        actions = []
        results = iter([{"action": "wait"}] * 5 + [{"action": "continue"}])
        browser._open = lambda url: (opened.append(url), events.append(("open", url)))
        browser._invoke = lambda *arguments: (invoked.append(arguments), events.append(("invoke", arguments)), {})[-1]
        browser._eval = lambda _script: (events.append(("eval",)), next(results))[-1]
        browser._click_action = lambda action, mouse=False: actions.append((action, mouse))
        browser._advance_checkout_path = lambda: (opened.append("advanced"), events.append(("advanced",)))

        with mock.patch("oda_browser.time.sleep", side_effect=lambda seconds: events.append(("sleep", seconds))):
            browser._navigate_to_checkout()

        self.assertEqual(opened, [CART_URL, "advanced"])
        self.assertEqual(
            invoked,
            [("reload",), ("snapshot",), ("reload",), ("snapshot",)],
        )
        self.assertEqual(
            events,
            [
                ("open", CART_URL),
                ("sleep", 12),
                ("invoke", ("reload",)),
                ("invoke", ("snapshot",)),
                ("sleep", 5),
                *(("eval",), ("sleep", 1)) * 5,
                ("invoke", ("reload",)),
                ("invoke", ("snapshot",)),
                ("sleep", 5),
                ("eval",),
                ("advanced",),
            ],
        )
        self.assertEqual(actions, [("continue", True)])

    def test_checkout_cart_wait_is_bounded(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        evaluations = []
        invoked = []
        browser._open = lambda _url: None
        browser._invoke = lambda *arguments: invoked.append(arguments) or {}
        browser._eval = lambda _script: evaluations.append(None) or {"action": "wait"}

        with mock.patch("oda_browser.time.sleep"):
            with self.assertRaisesRegex(HouseholdError, "cart cannot continue"):
                browser._navigate_to_checkout()

        self.assertEqual(len(evaluations), 10)
        self.assertEqual(
            invoked,
            [("reload",), ("snapshot",), ("reload",), ("snapshot",)],
        )

    def test_checkout_retries_a_failed_read_only_cart_open(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        events = []
        actions = []
        attempts = 0

        def open_cart(url):
            nonlocal attempts
            attempts += 1
            events.append(("open", url))
            if attempts == 1:
                raise HouseholdError("browser command timed out")

        browser._open = open_cart
        browser._invoke = lambda *arguments, **_kwargs: events.append(("invoke", *arguments)) or {}
        browser._eval = lambda _script: {"action": "continue"}
        browser._click_action = lambda action, mouse=False: actions.append((action, mouse))
        browser._advance_checkout_path = lambda: events.append(("advanced",))

        with mock.patch("oda_browser.time.sleep"):
            browser._navigate_to_checkout()

        self.assertEqual(
            events,
            [
                ("open", CART_URL),
                ("invoke", "close"),
                ("open", CART_URL),
                ("invoke", "reload"),
                ("invoke", "snapshot"),
                ("advanced",),
            ],
        )
        self.assertEqual(actions, [("continue", True)])

    def test_checkout_allows_the_entry_route_to_settle_after_cart_click(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        events = []
        scripts = []
        sleeps = []
        browser._eval = lambda script: (scripts.append(script), events.append(("eval",)), {"action": "ready"})[-1]

        with mock.patch("oda_browser.time.sleep", side_effect=lambda seconds: (sleeps.append(seconds), events.append(("sleep", seconds)))):
            browser._advance_checkout_path()

        self.assertEqual(sleeps, [10])
        self.assertEqual(events, [("sleep", 10), ("eval",)])
        self.assertIn(json.dumps(CHECKOUT_ENTRY_URL), scripts[0])
        self.assertNotIn("startsWith('/no/checkout/')", scripts[0])
        self.assertNotIn(".click()", scripts[0])

    def test_checkout_intermediate_routes_use_marked_browser_clicks(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        actions = []
        events = []
        sleeps = []
        results = iter([
            {"action": "new_order"},
            {"action": "new_order"},
            {"action": "payment"},
            {"action": "payment"},
            {"action": "recommendations"},
            {"action": "recommendations"},
            {"action": "ready"},
        ])

        def evaluate(_script):
            result = next(results)
            events.append(("eval", result["action"]))
            return result

        def click(action, mouse=False):
            actions.append((action, mouse))
            events.append(("click", action))

        browser._eval = evaluate
        browser._click_action = click

        with mock.patch("oda_browser.time.sleep", side_effect=lambda seconds: (sleeps.append(seconds), events.append(("sleep", seconds)))):
            browser._advance_checkout_path()

        self.assertEqual(actions, [("new-order", False), ("payment", False), ("recommendations", False)])
        self.assertEqual(sleeps, [10, 10, 0.5, 10, 0.5, 10, 0.5])
        self.assertEqual(events, [
            ("sleep", 10),
            ("eval", "new_order"),
            ("click", "new-order"),
            ("sleep", 10),
            ("eval", "new_order"),
            ("sleep", 0.5),
            ("eval", "payment"),
            ("click", "payment"),
            ("sleep", 10),
            ("eval", "payment"),
            ("sleep", 0.5),
            ("eval", "recommendations"),
            ("click", "recommendations"),
            ("sleep", 10),
            ("eval", "recommendations"),
            ("sleep", 0.5),
            ("eval", "ready"),
        ])

    def test_checkout_route_script_rejects_mixed_new_and_existing_controls(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        scripts = []
        browser._eval = lambda script: scripts.append(script) or {"action": "blocked"}
        with mock.patch("oda_browser.time.sleep"):
            with self.assertRaisesRegex(HouseholdError, "navigation is ambiguous"):
                browser._advance_checkout_path()
        self.assertIn("newOrder.length===1 && previous.length===0 && payment.length===0", scripts[0])
        self.assertIn("newOrder.length===0 && previous.length===1 && payment.length===0", scripts[0])
        self.assertIn("return JSON.stringify({action:'blocked'})", scripts[0])
        self.assertIn("orderTokens", scripts[0])
        self.assertNotIn("includes(ORDER)", scripts[0])

    def test_checkout_navigation_wait_is_bounded(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        evaluations = []
        sleeps = []
        browser._eval = lambda _script: evaluations.append(None) or {"action": "wait"}

        with mock.patch("oda_browser.time.sleep", side_effect=sleeps.append):
            with self.assertRaisesRegex(HouseholdError, "navigation timed out"):
                browser._advance_checkout_path()

        self.assertEqual(len(evaluations), 30)
        self.assertEqual(sleeps, [10] + [0.5] * 30)

    def test_checkout_deadline_caps_each_browser_command(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser.binary = Path("/shared/agent-browser-native")
        browser.executable = Path("/usr/bin/chromium")
        browser.profile = Path("/profile")
        browser.home = Path("/home")
        browser.socket_directory = Path("/run/browser")
        browser.session = "test"
        browser.uid = 10001
        browser.gid = 10002
        browser._checkout_deadline = 20.0
        completed = mock.Mock(returncode=0, stdout='{"success":true,"data":{}}')

        with mock.patch("oda_browser.time.monotonic", return_value=10.0):
            with mock.patch("oda_browser.subprocess.run", return_value=completed) as run:
                browser._invoke("get", "url")

        self.assertEqual(run.call_args.kwargs["timeout"], 10.0)
        self.assertEqual(
            run.call_args.kwargs["env"]["AGENT_BROWSER_ARGS"],
            DEFAULT_BROWSER_ARGS,
        )

    def test_checkout_deadline_blocks_the_final_click(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser._checkout_deadline = None
        browser._invoke = lambda *_arguments, **_kwargs: {}
        browser.review_checkout = lambda _cart: {"review": "same"}
        browser._cart_expectation = lambda _cart: {"total_minor": 100}
        evaluations = []
        browser._eval = lambda script: evaluations.append(script) or {"clicked": True}

        with mock.patch("oda_browser.time.monotonic", return_value=86.0):
            with self.assertRaisesRegex(CheckoutPreconditionError, "deadline reached"):
                browser.submit_checkout({}, {"review": "same"}, deadline=100.0)

        self.assertEqual(evaluations, [])

    def test_checkout_read_only_preclick_failure_is_not_uncertain(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        browser.review_checkout = lambda _cart: {"review": "same"}
        browser._cart_expectation = lambda _cart: {"total_minor": 100}
        evaluations = []
        browser._eval = lambda script: evaluations.append(script) or {"clicked": True}

        def fail_before_click():
            raise HouseholdError("cart read failed")

        with self.assertRaisesRegex(CheckoutPreconditionError, "cart read failed"):
            browser._submit_checkout({}, {"review": "same"}, fail_before_click)

        self.assertEqual(evaluations, [])

    def test_checkout_continue_uses_unobscured_mouse_activation(self):
        browser = OdaBrowser.__new__(OdaBrowser)
        calls = []

        def invoke(*arguments):
            calls.append(arguments)
            if arguments[:2] == ("get", "box"):
                return {"box": {"x": 10, "y": 20, "width": 30, "height": 40}}
            return {}

        browser._invoke = invoke
        browser._eval = lambda script: {"clear": "elementFromPoint" in script}

        browser._click_action("continue", mouse=True)

        self.assertEqual(calls[0][0], "scrollintoview")
        self.assertEqual(calls[1][:2], ("get", "box"))
        self.assertEqual([call[:2] for call in calls[2:]], [("mouse", "move"), ("mouse", "down"), ("mouse", "up")])


class MenyClientTests(unittest.TestCase):
    def client(self):
        return MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
        )

    def test_probe_requires_the_persistent_profile_to_be_logged_in(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(return_value={"ready": True, "authenticated": False})
        with self.assertRaisesRegex(HouseholdError, "login is required"):
            client.probe()
        self.assertEqual(client._eval.call_count, 60)
        self.assertEqual(client._sleep.call_count, 60)
        client._eval.return_value = {"ready": True, "authenticated": True}
        probe = client.probe()
        self.assertEqual(probe["provider"], "meny")
        self.assertEqual(probe["protocol_version"], "browser-v1")

    def test_login_check_waits_for_the_persistent_session_to_settle(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(side_effect=[
            {"ready": True, "authenticated": False},
            {"ready": True, "authenticated": True},
        ])
        client._require_login()
        client._sleep.assert_called_once_with(0.25)

    def test_login_check_allows_a_slow_authenticated_shell_to_hydrate(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(side_effect=[
            *([{"ready": True, "authenticated": False}] * 32),
            {"ready": True, "authenticated": True},
        ])

        client._require_login()

        self.assertEqual(client._eval.call_count, 33)
        self.assertEqual(client._sleep.call_count, 32)

    def test_cdp_is_restricted_to_an_explicit_loopback_endpoint(self):
        self.assertEqual(normalize_browser_cdp("http://127.0.0.1:9224"), "http://127.0.0.1:9224")
        self.assertEqual(normalize_browser_cdp("http://localhost:9224/"), "http://localhost:9224")
        for invalid in ("https://127.0.0.1:9224", "http://example.test:9224", "http://127.0.0.1", "http://user@127.0.0.1:9224"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(HouseholdError):
                    normalize_browser_cdp(invalid)

    def test_cdp_mode_connects_agent_browser_without_launching_another_profile(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        completed = mock.Mock(returncode=0, stdout='{"success":true,"data":{"url":"https://meny.no/varer"}}')
        with mock.patch("meny.subprocess.run", return_value=completed) as run, mock.patch("meny.os.geteuid", return_value=1000):
            client._invoke("get", "url")
        command = run.call_args.args[0]
        self.assertIn("--cdp", command)
        self.assertIn("http://127.0.0.1:9224", command)
        self.assertNotIn("--profile", command)
        self.assertNotIn("--executable-path", command)
        self.assertEqual(run.call_args.kwargs["env"]["AGENT_BROWSER_ARGS"], "--disable-gpu,--disable-quic")
        self.assertEqual(MENY_BROWSER_ARGS, "--disable-gpu,--disable-quic")

    def test_agent_browser_timeout_envelope_is_transport_but_semantic_rejection_is_not(self):
        client = self.client()
        timeout = mock.Mock(returncode=1, stdout=json.dumps({
            "success": False,
            "error": "CDP command timed out: Runtime.evaluate",
        }))
        rejected = mock.Mock(returncode=1, stdout=json.dumps({
            "success": False,
            "error": "JavaScript evaluation failed: SyntaxError",
        }))
        with mock.patch("meny.subprocess.run", side_effect=[timeout, rejected]), mock.patch("meny.os.geteuid", return_value=1000):
            with self.assertRaises(_BrowserTransportError):
                client._invoke_once("eval", "--stdin", stdin="script")
            with self.assertRaises(HouseholdError) as caught:
                client._invoke_once("eval", "--stdin", stdin="bad script")
        self.assertNotIsInstance(caught.exception, _BrowserTransportError)

    def test_safe_cdp_read_recovers_one_tab_but_click_never_retries(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        client.recovery_allowed = True
        client._sleep = mock.Mock()
        client._invoke_once = mock.Mock(side_effect=[_BrowserTransportError("tab hung"), {"result": "{}"}])
        client._recover_cdp_tab = mock.Mock(return_value=True)
        self.assertEqual(client._invoke("eval", "--stdin", stdin="script"), {"result": "{}"})
        self.assertEqual(client._invoke_once.call_count, 2)
        client._recover_cdp_tab.assert_called_once_with()
        self.assertTrue(client._recovery_consumed)
        client._sleep.assert_called_once_with(0.5)

        client._invoke_once = mock.Mock(side_effect=_BrowserTransportError("tab hung"))
        client._recover_cdp_tab.reset_mock()
        with self.assertRaisesRegex(HouseholdError, "tab hung"):
            client._invoke("click", "button")
        client._invoke_once.assert_called_once_with("click", "button", stdin=None)
        client._recover_cdp_tab.assert_not_called()

        client._invoke_once = mock.Mock(side_effect=_BrowserTransportError("tab hung again"))
        with self.assertRaisesRegex(HouseholdError, "tab hung again"):
            client._invoke("eval", "--stdin", stdin="script")
        client._recover_cdp_tab.assert_not_called()

        client._invoke_once = mock.Mock(side_effect=HouseholdError("rejected eval"))
        with self.assertRaisesRegex(HouseholdError, "rejected eval"):
            client._invoke("eval", "--stdin", stdin="bad script")
        client._recover_cdp_tab.assert_not_called()

    def test_failed_cdp_recovery_attempt_is_consumed_for_the_operation(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        client.recovery_allowed = True
        client._invoke_once = mock.Mock(side_effect=_BrowserTransportError("tab hung"))
        client._recover_cdp_tab = mock.Mock(return_value=False)

        for _ in range(2):
            with self.assertRaisesRegex(HouseholdError, "tab hung"):
                client._invoke("eval", "--stdin", stdin="script")

        client._recover_cdp_tab.assert_called_once_with()
        self.assertTrue(client._recovery_consumed)

    def test_cdp_recovery_replaces_only_dedicated_meny_pages(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        old_id = "A" * 32
        new_id = "B" * 32
        url = "https://meny.no/sok?query=frosne%20erter&expanded=products"
        client._cdp_request = mock.Mock(side_effect=[
            json.dumps([{"type": "page", "id": old_id, "url": url}]),
            json.dumps({"type": "page", "id": new_id, "url": url}),
            json.dumps([
                {"type": "page", "id": old_id, "url": url},
                {"type": "page", "id": new_id, "url": url},
            ]),
            "Target is closing",
            json.dumps([{"type": "page", "id": new_id, "url": url}]),
        ])
        client._terminate_browser_session = mock.Mock(return_value=True)
        self.assertTrue(client._recover_cdp_tab())
        self.assertEqual(client._cdp_request.call_args_list, [
            mock.call("GET", "/json/list", mock.ANY),
            mock.call("PUT", "/json/new?https%3A%2F%2Fmeny.no%2Fsok%3Fquery%3Dfrosne%2520erter%26expanded%3Dproducts", mock.ANY),
            mock.call("GET", "/json/list", mock.ANY),
            mock.call("PUT", f"/json/close/{old_id}", mock.ANY),
            mock.call("GET", "/json/list", mock.ANY),
        ])
        client._terminate_browser_session.assert_called_once_with(mock.ANY)
        self.assertFalse(client._cdp_primed)

    def test_cdp_recovery_rejects_ambiguous_targets_and_short_deadlines(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        client._cdp_request = mock.Mock(return_value=json.dumps([
            {"type": "page", "id": "A" * 32, "url": "https://meny.no/varer"},
            {"type": "page", "id": "B" * 32, "url": "https://meny.no/sok?query=purre"},
        ]))
        client._terminate_browser_session = mock.Mock()
        self.assertFalse(client._recover_cdp_tab())
        client._terminate_browser_session.assert_not_called()

        client._cdp_request.reset_mock()
        client.deadline = time.monotonic() + 11
        self.assertFalse(client._recover_cdp_tab())
        client._cdp_request.assert_not_called()

    def test_cdp_recovery_keeps_the_valid_replacement_if_old_close_is_uncertain(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        old_id = "A" * 32
        new_id = "B" * 32
        client._cdp_request = mock.Mock(side_effect=[
            json.dumps([{"type": "page", "id": old_id, "url": "https://meny.no/varer"}]),
            json.dumps({"type": "page", "id": new_id, "url": "https://meny.no/varer"}),
            json.dumps([
                {"type": "page", "id": old_id, "url": "https://meny.no/varer"},
                {"type": "page", "id": new_id, "url": "https://meny.no/varer"},
            ]),
            HouseholdError("close response lost"),
            json.dumps([{"type": "page", "id": new_id, "url": "https://meny.no/varer"}]),
        ])
        client._terminate_browser_session = mock.Mock(return_value=True)
        self.assertTrue(client._recover_cdp_tab())
        self.assertEqual(client._cdp_request.call_count, 5)

    def test_cdp_recovery_retries_a_close_that_had_no_effect(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        old_id = "A" * 32
        new_id = "B" * 32
        url = "https://meny.no/varer"
        client._cdp_request = mock.Mock(side_effect=[
            json.dumps([{"type": "page", "id": old_id, "url": url}]),
            json.dumps({"type": "page", "id": new_id, "url": url}),
            json.dumps([
                {"type": "page", "id": old_id, "url": url},
                {"type": "page", "id": new_id, "url": url},
            ]),
            HouseholdError("close request lost before delivery"),
            json.dumps([
                {"type": "page", "id": old_id, "url": url},
                {"type": "page", "id": new_id, "url": url},
            ]),
            "Target is closing",
            json.dumps([{"type": "page", "id": new_id, "url": url}]),
        ])
        client._terminate_browser_session = mock.Mock(return_value=True)
        self.assertTrue(client._recover_cdp_tab())
        close = mock.call("PUT", f"/json/close/{old_id}", mock.ANY)
        self.assertEqual(client._cdp_request.call_args_list.count(close), 2)

    def test_cdp_recovery_reconciles_a_lost_create_response(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        old_id = "A" * 32
        new_id = "B" * 32
        url = "https://meny.no/varer"
        client._cdp_request = mock.Mock(side_effect=[
            json.dumps([{"type": "page", "id": old_id, "url": url}]),
            HouseholdError("create response lost"),
            json.dumps([
                {"type": "page", "id": old_id, "url": url},
                {"type": "page", "id": new_id, "url": url},
            ]),
            "Target is closing",
            json.dumps([{"type": "page", "id": new_id, "url": url}]),
        ])
        client._terminate_browser_session = mock.Mock(return_value=True)
        self.assertTrue(client._recover_cdp_tab())

    def test_js_wrapper_resolves_its_one_native_linux_daemon(self):
        with tempfile.TemporaryDirectory() as temp:
            bin_directory = Path(temp)
            wrapper = bin_directory / "agent-browser.js"
            native_arch = "arm64" if os.uname().machine.casefold() in {"arm64", "aarch64"} else "x64"
            native = bin_directory / f"agent-browser-linux-{native_arch}"
            wrapper.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            native.write_bytes(b"native")
            native.chmod(0o755)
            client = self.client()
            client.binary = wrapper
            self.assertEqual(client._browser_daemon_executable(), native.resolve())
            second = bin_directory / f"agent-browser-linux-musl-{native_arch}"
            second.write_bytes(b"native")
            second.chmod(0o755)
            self.assertIsNone(client._browser_daemon_executable())

    def test_first_cdp_navigation_reloads_before_readiness_evaluation(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        events = []
        client._invoke = mock.Mock(side_effect=lambda command, *args: events.append(command) or ({"url": "https://meny.no/varer"} if command == "open" else {}))
        client._site_shell_ready = mock.Mock(side_effect=lambda: events.append("ready") or True)
        client._sleep = mock.Mock()
        client._open("https://meny.no/varer")
        self.assertEqual(events, ["open", "reload", "ready"])
        self.assertTrue(client._cdp_primed)

    def test_readiness_evaluation_failure_gets_one_bounded_reload(self):
        client = self.client()
        client._invoke = mock.Mock(side_effect=[{"url": "https://meny.no/varer"}, {}])
        client._site_shell_ready = mock.Mock(side_effect=[HouseholdError("evaluation timed out"), True])
        client._sleep = mock.Mock()
        client._open("https://meny.no/varer")
        self.assertEqual(client._invoke.call_args_list, [mock.call("open", "https://meny.no/varer"), mock.call("reload")])
        self.assertEqual(client._site_shell_ready.call_count, 2)

    def test_authenticated_shell_can_recover_on_a_second_bounded_reload(self):
        client = self.client()
        client._invoke = mock.Mock(side_effect=[{"url": "https://meny.no/varer"}, {}, {}])
        client._site_shell_ready = mock.Mock(side_effect=[False] * 21 + [True])
        client._sleep = mock.Mock()
        client._open("https://meny.no/varer")
        self.assertEqual(client._invoke.call_args_list, [
            mock.call("open", "https://meny.no/varer"),
            mock.call("reload"),
            mock.call("reload"),
        ])
        self.assertEqual(client._site_shell_ready.call_count, 22)

    def test_read_only_cdp_navigation_replaces_one_persistently_unhydrated_target(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        client._cdp_primed = True
        client.recovery_allowed = True
        client._invoke = mock.Mock(side_effect=lambda command, *_args: {"url": "https://meny.no/varer"} if command == "open" else {})
        client._invoke_once = mock.Mock(return_value={"url": "https://meny.no/varer"})
        client._site_shell_ready = mock.Mock(side_effect=[False] * 61 + [True])
        client._recover_cdp_tab = mock.Mock(return_value=True)
        client._sleep = mock.Mock()

        client._open("https://meny.no/varer")

        client._recover_cdp_tab.assert_called_once_with()
        self.assertTrue(client._recovery_consumed)
        client._invoke_once.assert_called_once_with("open", "https://meny.no/varer")
        self.assertEqual([call.args[0] for call in client._invoke.call_args_list], ["open", "reload", "reload", "reload", "reload"])
        self.assertEqual(client._site_shell_ready.call_count, 62)
        self.assertTrue(client._cdp_primed)

    def test_protected_cdp_navigation_does_not_replace_an_unhydrated_target(self):
        client = MenyClient(
            instance="test",
            binary="agent-browser",
            executable="/usr/bin/chromium",
            profile="/private/profile",
            home="/private/home",
            socket_directory="/private/socket",
            uid=1000,
            gid=1000,
            cdp="http://127.0.0.1:9224",
        )
        client._cdp_primed = True
        client.recovery_allowed = False
        client._invoke = mock.Mock(side_effect=lambda command, *_args: {"url": "https://meny.no/varer"} if command == "open" else {})
        client._site_shell_ready = mock.Mock(return_value=False)
        client._recover_cdp_tab = mock.Mock(return_value=True)
        client._sleep = mock.Mock()

        with self.assertRaisesRegex(HouseholdError, "did not finish rendering"):
            client._open("https://meny.no/varer")

        client._recover_cdp_tab.assert_not_called()
        self.assertEqual(client._site_shell_ready.call_count, 61)

    def test_visible_ssr_shell_is_not_ready_until_react_handler_is_hydrated(self):
        client = self.client()
        client._eval = mock.Mock(return_value={"dom_ready": True, "hydrated": False})
        self.assertFalse(client._site_shell_ready())
        client._eval.return_value = {"dom_ready": True, "hydrated": True}
        self.assertTrue(client._site_shell_ready())
        script = client._eval.call_args.args[0]
        self.assertIn("__reactProps$", script)
        self.assertIn("typeof props.onClick === 'function'", script)

    def test_cart_change_uses_exact_catalog_path_and_delta(self):
        client = self.client()
        changes = []
        client._change_one = lambda product, delta, **kwargs: changes.append((product, delta, kwargs.get("order_change_code")))
        client._read_cart = mock.Mock(return_value={"provider": "meny", "items": []})
        client._sleep = mock.Mock()
        result = client._change_cart({"operations": [{"productId": MENY_PRODUCT, "quantity": 2}]})
        self.assertEqual(changes, [(MENY_PRODUCT, 1, None), (MENY_PRODUCT, 1, None)])
        self.assertEqual(result["provider"], "meny")
        self.assertEqual(client._read_cart.call_count, 4)
        self.assertEqual(client._sleep.call_count, 3)

    def test_cart_change_uses_the_final_two_matching_readbacks(self):
        client = self.client()
        client._change_one = mock.Mock()
        stale = {"provider": "meny", "items": [{"product_id": MENY_PRODUCT, "quantity": 1}], "total": 100.0}
        settled = {**stale, "total": 125.0}
        client._read_cart = mock.Mock(side_effect=[stale, stale, settled, settled])
        client._sleep = mock.Mock()
        result = client._change_cart({"operations": [{"productId": MENY_PRODUCT, "quantity": 1}]})
        self.assertEqual(result, settled)
        self.assertEqual(client._read_cart.call_count, 4)
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.5), mock.call(0.5), mock.call(0.5)])

    def test_cart_change_marks_an_unsettled_readback_as_partial(self):
        client = self.client()
        client._change_one = mock.Mock()
        client._read_cart = mock.Mock(side_effect=[
            {"provider": "meny", "items": [], "total": float(total)} for total in range(4)
        ])
        client._sleep = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "changed partially.*do not retry"):
            client._change_cart({"operations": [{"productId": MENY_PRODUCT, "quantity": 1}]})
        self.assertEqual(client._read_cart.call_count, 4)
        self.assertEqual(client._sleep.call_count, 3)

    def test_cart_batch_is_fully_validated_before_the_first_click(self):
        client = self.client()
        client._change_one = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "product_id is invalid"):
            client._change_cart({"operations": [
                {"productId": MENY_PRODUCT, "quantity": 1},
                {"productId": "/not-a-product", "quantity": 1},
            ]})
        client._change_one.assert_not_called()

    def test_cart_batch_keeps_margin_for_the_required_readback(self):
        client = self.client()
        client._change_one = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "at most 2 units"):
            client._change_cart({"operations": [{"productId": MENY_PRODUCT, "quantity": 3}]})
        client._change_one.assert_not_called()

    def test_cart_deadline_stops_later_clicks_and_marks_partial_result(self):
        client = self.client()
        client._require_login = mock.Mock()
        client._change_one = mock.Mock()
        client._read_cart = mock.Mock()
        with mock.patch("meny.time.monotonic", side_effect=[0, 0, 0, 235]):
            with self.assertRaisesRegex(HouseholdError, "changed partially.*do not retry"):
                client.call("manipulate_cart", {"operations": [{"productId": MENY_PRODUCT, "quantity": 2}]})
        client._change_one.assert_called_once_with(MENY_PRODUCT, 1, order_change_code=None)
        client._read_cart.assert_not_called()
        self.assertIsNone(client.deadline)

    def test_each_provider_call_rechecks_the_logged_in_session(self):
        client = self.client()
        client._require_login = mock.Mock()
        client._search = mock.Mock(return_value={"provider": "meny", "products": []})
        result = client.call("product_search", {"queries": ["brokkoli"], "size": 3})
        client._require_login.assert_called_once_with()
        self.assertEqual(result["provider"], "meny")

    def test_delivery_page_preparation_waits_for_the_search_control(self):
        client = self.client()
        client._eval = mock.Mock(side_effect=[
            {"ready": False, "identity": True, "authenticated": True},
            {"ready": True, "identity": True, "authenticated": True, "action": "search"},
        ])
        client._sleep = mock.Mock()
        client._invoke = mock.Mock(return_value={})

        client._prepare_search()

        client._sleep.assert_called_once_with(0.25)
        client._invoke.assert_not_called()

    def test_delivery_page_preparation_closes_an_open_cart_at_most_once(self):
        client = self.client()
        client._eval = mock.Mock(side_effect=[
            {"ready": True, "identity": True, "authenticated": True, "action": "close"},
            {"ready": True, "identity": True, "authenticated": True, "action": "close"},
            {"ready": True, "identity": True, "authenticated": True, "action": "search"},
        ])
        client._sleep = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._invoke = mock.Mock(return_value={})

        client._prepare_search()

        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="close-cart"]')
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.3), mock.call(0.25)])
        client._assert_authenticated.assert_called_once_with()

    def test_delivery_page_preparation_stops_before_click_on_context_loss(self):
        for state, message in (
            ({"ready": False, "identity": False, "authenticated": True}, "route changed"),
            ({"ready": False, "identity": True, "authenticated": False}, "login is required"),
        ):
            with self.subTest(state=state):
                client = self.client()
                client._eval = mock.Mock(return_value=state)
                client._invoke = mock.Mock(return_value={})
                with self.assertRaisesRegex(HouseholdError, message):
                    client._prepare_search()
                client._invoke.assert_not_called()

    def test_delivery_picker_waits_for_the_control_and_accepts_a_native_dialog(self):
        client = self.client()
        client._open = mock.Mock()
        client._prepare_search = mock.Mock()
        client._sleep = mock.Mock()
        scripts = []
        results = iter([
            {"ready": False, "identity": True, "authenticated": True},
            {"ready": True, "identity": True, "authenticated": True},
            {"ready": False, "identity": True, "authenticated": True},
            {"ready": True, "identity": True, "authenticated": True},
        ])
        client._eval = lambda script: scripts.append(script) or next(results)
        client._invoke = mock.Mock(return_value={})

        client._open_delivery_picker()

        self.assertEqual(client._open.call_args_list, [
            mock.call("https://meny.no/sok?query=levering"),
            mock.call("https://meny.no/varer"),
        ])
        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="delivery-open"]')
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.25), mock.call(0.25)])
        self.assertIn("querySelectorAll('dialog,[role=\"dialog\"]')", scripts[-1])
        self.assertIn("=== 'Når skal vi levere til deg?'", scripts[-1])
        self.assertIn("location.pathname === '/varer'", scripts[-1])
        self.assertIn("Brukermeny", scripts[-1])
        self.assertIn("dialogs.length !== 0", scripts[1])

    def test_delivery_picker_stops_before_click_on_route_or_login_loss(self):
        for state, message in (
            ({"ready": False, "identity": False, "authenticated": True}, "route changed"),
            ({"ready": False, "identity": True, "authenticated": False}, "login is required"),
        ):
            with self.subTest(state=state):
                client = self.client()
                client._open = mock.Mock()
                client._prepare_search = mock.Mock()
                client._eval = mock.Mock(return_value=state)
                client._invoke = mock.Mock(return_value={})
                with self.assertRaisesRegex(HouseholdError, message):
                    client._open_delivery_picker()
                client._invoke.assert_not_called()

    def test_delivery_slots_are_read_from_the_bound_native_dialog(self):
        client = self.client()
        client._open_delivery_picker = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(side_effect=[
            {"ready": False, "identity": True, "authenticated": True, "slots": []},
            {
                "ready": True,
                "identity": True,
                "authenticated": True,
                "slots": [{
                "slot_id": "fra 0 kr, 2. september klokka 07:00 til 08:00",
                "date": "2026-09-02",
                "start": "07:00",
                "end": "08:00",
                "display": "fra 0 kr, 2. september klokka 07:00 til 08:00",
                "selected": False,
                }],
            },
        ])
        client._invoke = mock.Mock(return_value={})
        client._wait_delivery_picker_closed = mock.Mock()

        result = client._delivery_slots("2026-09-02")

        self.assertEqual(len(result["slots"]), 1)
        self.assertIn("querySelectorAll('dialog,[role=\"dialog\"]')", client._eval.call_args.args[0])
        self.assertIn("=== 'Lukk'", client._eval.call_args.args[0])
        self.assertNotIn("['Avbryt','Lukk']", client._eval.call_args.args[0])
        client._sleep.assert_called_once_with(0.25)
        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="delivery-dismiss"]')
        client._wait_delivery_picker_closed.assert_called_once_with()

    def test_delivery_selection_binds_both_native_dialog_steps(self):
        client = self.client()
        client._open_delivery_picker = mock.Mock()
        client._sleep = mock.Mock()
        scripts = []
        results = iter([
            {"ready": False, "identity": True, "authenticated": True},
            {"ready": True, "identity": True, "authenticated": True, "already_selected": False},
            {"ready": True, "identity": True, "authenticated": True, "selected_count": 1, "total_selected_count": 1},
            {"ready": True, "identity": True, "authenticated": True, "dialog_count": 0},
            {"ready": True, "identity": True, "authenticated": True, "selected_count": 1, "total_selected_count": 1},
            {"ready": True, "identity": True, "authenticated": True, "dialog_count": 0},
        ])
        client._eval = lambda script: scripts.append(script) or next(results)
        client._invoke = mock.Mock(return_value={})

        result = client._select_delivery_slot("fra 0 kr fra 0 kroner, 3. september klokka 10:00 til 12:00")

        self.assertEqual(result["selected"]["display"], "fra 0 kr fra 0 kroner, 3. september klokka 10:00 til 12:00")
        self.assertIn("querySelectorAll('dialog,[role=\"dialog\"]')", scripts[1])
        self.assertIn("querySelectorAll('dialog,[role=\"dialog\"]')", scripts[2])
        self.assertIn("button[aria-pressed=\"true\"]", scripts[2])
        self.assertIn("slotPattern.test", scripts[2])
        self.assertIn("allSelected.length !== 1", scripts[2])
        self.assertIn("parts[1].toLocaleLowerCase('nb-NO') === expectedSuffix", scripts[2])
        self.assertNotIn("startsWith('Bekreft levering ')", scripts[2])
        self.assertNotIn("endsWith(expectedSuffix)", scripts[2])
        client._sleep.assert_called_once_with(0.25)
        self.assertEqual(client._invoke.call_args_list, [
            mock.call("click", '[data-hermes-meal-planner-action="delivery-slot"]'),
            mock.call("click", '[data-hermes-meal-planner-action="delivery-confirm"]'),
            mock.call("click", '[data-hermes-meal-planner-action="delivery-dismiss"]'),
        ])

    def test_already_selected_delivery_waits_until_the_dialog_is_closed(self):
        client = self.client()
        client._open_delivery_picker = mock.Mock()
        client._eval = mock.Mock(return_value={
            "ready": True,
            "identity": True,
            "authenticated": True,
            "already_selected": True,
        })
        client._invoke = mock.Mock(return_value={})
        client._wait_delivery_picker_closed = mock.Mock()

        result = client._select_delivery_slot("fra 0 kr fra 0 kroner, 3. september klokka 10:00 til 12:00")

        self.assertEqual(result["selected"]["display"], "fra 0 kr fra 0 kroner, 3. september klokka 10:00 til 12:00")
        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="delivery-dismiss"]')
        client._wait_delivery_picker_closed.assert_called_once_with()

    def test_delivery_selection_cannot_reuse_a_tentative_open_dialog(self):
        client = self.client()
        client._open_delivery_picker = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(side_effect=[
            {"ready": True, "identity": True, "authenticated": True, "already_selected": False},
            {"ready": True, "identity": True, "authenticated": True, "selected_count": 1, "total_selected_count": 1},
            *([{"ready": False, "identity": True, "authenticated": True, "dialog_count": 1}] * 20),
        ])
        client._invoke = mock.Mock(return_value={})

        with self.assertRaisesRegex(HouseholdError, "selection is uncertain"):
            client._select_delivery_slot("fra 0 kr fra 0 kroner, 3. september klokka 10:00 til 12:00")

        self.assertEqual(client._open_delivery_picker.call_count, 1)
        self.assertEqual(client._invoke.call_args_list, [
            mock.call("click", '[data-hermes-meal-planner-action="delivery-slot"]'),
            mock.call("click", '[data-hermes-meal-planner-action="delivery-confirm"]'),
        ])

    def test_delivery_selection_never_confirms_a_mismatched_selected_slot(self):
        client = self.client()
        client._open_delivery_picker = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(side_effect=[
            {"ready": True, "identity": True, "authenticated": True, "already_selected": False},
            *([{"ready": False, "identity": True, "authenticated": True, "selected_count": 1, "total_selected_count": 2}] * 20),
        ])
        client._invoke = mock.Mock(return_value={})

        with self.assertRaisesRegex(HouseholdError, "confirmation changed"):
            client._select_delivery_slot("fra 0 kr fra 0 kroner, 3. september klokka 10:00 til 12:00")

        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="delivery-slot"]')

    def test_delivery_selection_stops_before_slot_click_after_context_loss(self):
        for state, message in (
            ({"ready": False, "identity": False, "authenticated": True}, "route changed"),
            ({"ready": False, "identity": True, "authenticated": False}, "login is required"),
        ):
            with self.subTest(state=state):
                client = self.client()
                client._open_delivery_picker = mock.Mock()
                client._eval = mock.Mock(return_value=state)
                client._invoke = mock.Mock(return_value={})
                with self.assertRaisesRegex(HouseholdError, message):
                    client._select_delivery_slot("fra 0 kr fra 0 kroner, 3. september klokka 10:00 til 12:00")
                client._invoke.assert_not_called()

    def test_cart_read_opens_the_visible_cart_and_returns_provider_shape(self):
        client = self.client()
        results = iter([
            {"open": False, "ready": True, "authenticated": True, "root_count": 0, "open_count": 1},
            {"ready": True, "authenticated": True, "root_count": 1, "item_root_count": 1, "control_count": 1, "empty": False, "total_count": 1, "delivery_count": 1, "delivery": {"display": "torsdag 3. sep. kl. 10:00-12:00"}, "items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 1, "price": 19.9}], "count": 1, "total": 19.9},
        ])
        scripts = []
        client._eval = lambda script: scripts.append(script) or next(results)
        invoked = []
        client._invoke = lambda *arguments, **_kwargs: invoked.append(arguments) or {}
        client._sleep = mock.Mock()
        cart = client._read_cart()
        self.assertEqual(invoked, [("click", '[data-hermes-meal-planner-action="open-cart"]')])
        client._sleep.assert_called_once_with(0.5)
        self.assertEqual(cart["items"][0]["product_id"], MENY_PRODUCT)
        self.assertEqual(cart["delivery"], {"slot_id": None, "display": "torsdag 3. sep. kl. 10:00-12:00"})
        self.assertEqual(cart["checkout"]["mode"], "protected_vipps")
        self.assertTrue(all("'Til kassen','Fortsett'" in script for script in scripts))
        self.assertIn("deliveryPrefix = 'Du har valgt at varene leveres på døren'", scripts[-1])
        self.assertNotIn("ws-cart-notification", scripts[-1])

    def test_order_details_wait_for_current_delivered_shape_and_expand_items(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        waiting = {"ready": False, "expand": False, "authenticated": True}
        expandable = {"ready": False, "expand": True, "authenticated": True}
        ready = {
            "ready": True,
            "expand": False,
            "authenticated": True,
            "order_number": "99990001",
            "code": "TEST-CODE-1",
            "status": "delivered",
            "total": 123.45,
            "delivery": "31. august 2026",
            "item_count": 1,
            "products": [{"identity": "Testprodukt", "name": "Testprodukt", "quantity": 1}],
        }
        scripts = []
        client._eval = mock.Mock(side_effect=lambda script: scripts.append(script) or [waiting, expandable, ready][len(scripts) - 1])
        client._invoke = mock.Mock(return_value={})

        order = client._get_order("99990001")

        self.assertEqual(order["status"], "delivered")
        self.assertEqual(order["grossAmount"], 123.45)
        self.assertEqual(order["deliverySlotDisplay"], "31. august 2026")
        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="order-items"]')
        self.assertEqual(client._sleep.call_args_list, [mock.call(1.5), mock.call(0.25), mock.call(0.25)])
        self.assertIn("valueAfter('Betalt beløp (kort)')", scripts[-1])
        self.assertIn(r"/^Bestilling\s+\S+/i", scripts[-1])
        self.assertIn("deliveredDatePattern", scripts[-1])

    def test_order_list_waits_for_the_completed_order_search_before_reading_dom(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._invoke = mock.Mock(side_effect=[
            {},
            {"requests": []},
            {"requests": [{
                "method": "GET",
                "status": 200,
                "url": "https://platform-rest-prod.ngdata.no/api/order/search/store/user",
            }]},
        ])
        client._eval = mock.Mock(side_effect=[
            {"ready": True, "authenticated": True, "orders": []},
            {"ready": True, "authenticated": True, "orders": [{"order_number": "99990001"}]},
        ])

        result = client._get_orders(10)

        self.assertEqual(len(result["orders"]), 1)
        self.assertEqual(client._invoke.call_args_list, [
            mock.call("network", "requests", "--clear"),
            mock.call("network", "requests", "--filter", "/api/order/search/"),
            mock.call("network", "requests", "--filter", "/api/order/search/"),
        ])
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.25), mock.call(0.5)])

    def test_cart_read_polls_a_transient_missing_cart_control(self):
        client = self.client()
        valid = {
            "ready": True,
            "authenticated": True,
            "root_count": 1,
            "item_root_count": 1,
            "control_count": 1,
            "empty": False,
            "total_count": 1,
            "delivery_count": 0,
            "delivery": None,
            "items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 1, "price": 19.9}],
            "count": 1,
            "total": 19.9,
        }
        client._eval = mock.Mock(side_effect=[
            {"open": False, "ready": False, "authenticated": True, "root_count": 0, "open_count": 0},
            {"open": False, "ready": True, "authenticated": True, "root_count": 0, "open_count": 1},
            valid,
        ])
        client._invoke = mock.Mock(return_value={})
        client._sleep = mock.Mock()
        self.assertEqual(client._read_cart()["total"], 19.9)
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.25), mock.call(0.5)])
        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="open-cart"]')

    def test_cart_read_polls_the_opening_panel_snapshot(self):
        client = self.client()
        waiting = {"ready": False, "authenticated": True, "root_count": 0}
        valid = {
            "ready": True,
            "authenticated": True,
            "root_count": 1,
            "item_root_count": 0,
            "control_count": 0,
            "empty": True,
            "total_count": 0,
            "delivery_count": 0,
            "delivery": None,
            "items": [],
            "count": 0,
            "total": 0,
        }
        client._eval = mock.Mock(side_effect=[
            {"open": False, "ready": True, "authenticated": True, "root_count": 0, "open_count": 1},
            waiting,
            valid,
        ])
        client._invoke = mock.Mock(return_value={})
        client._sleep = mock.Mock()
        self.assertEqual(client._read_cart()["total"], 0.0)
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.5), mock.call(0.25)])

    def test_cart_snapshot_rejects_ambiguous_or_incomplete_dom(self):
        valid = {
            "ready": True,
            "authenticated": True,
            "root_count": 1,
            "item_root_count": 1,
            "control_count": 1,
            "empty": False,
            "total_count": 1,
            "delivery_count": 0,
            "delivery": None,
            "items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 1, "price": 19.9}],
            "count": 1,
            "total": 19.9,
        }
        failures = (
            {**valid, "authenticated": False},
            {**valid, "root_count": 2},
            {**valid, "control_count": 0},
            {**valid, "total_count": 2},
            {**valid, "items": [], "item_root_count": 0, "control_count": 0, "count": 0, "total": 0, "empty": False},
            {**valid, "items": [], "item_root_count": 0, "control_count": 0, "count": 0, "total": 500, "empty": True},
            {**valid, "empty": True},
            {**valid, "total": 0},
            {**valid, "delivery_count": 2},
            {**valid, "delivery_count": 1, "delivery": {"display": "en gang senere"}},
            {**valid, "delivery_count": 1, "delivery": {"display": 123}},
        )
        for value in failures:
            with self.subTest(value=value):
                with self.assertRaises(HouseholdError):
                    normalize_cart_snapshot(value)

    def test_cart_snapshot_accepts_one_explicit_empty_cart_without_a_total_row(self):
        self.assertEqual(normalize_cart_snapshot({
            "ready": True,
            "authenticated": True,
            "root_count": 1,
            "item_root_count": 0,
            "control_count": 0,
            "empty": True,
            "total_count": 0,
            "delivery_count": 0,
            "delivery": None,
            "items": [],
            "count": 0,
            "total": 0,
        }), {"items": [], "count": 0, "total": 0.0, "delivery": None})

    def test_checkout_payment_snapshot_rejects_malformed_boundary_values(self):
        valid = {
            "ready": True,
            "authenticated": True,
            "vipps_checked": True,
            "home_delivery": True,
            "submit_enabled": True,
            "total": 99.9,
            "delivery": "torsdag 3. september Kl. 10:00-12:00",
            "submit_controls": 1,
        }
        self.assertEqual(normalize_checkout_payment_snapshot(valid), {
            "total": 99.9,
            "delivery": "torsdag 3. september Kl. 10:00-12:00",
        })
        self.assertEqual(
            meny_delivery_window_identity("torsdag 3. sep. kl. 10:00-12:00"),
            meny_delivery_window_identity("tor 3. september Kl. 10:00–12:00"),
        )
        self.assertEqual(
            meny_delivery_window_identity("torsdag 29. februar Kl. 10:00-12:00"),
            ("tor", 29, "feb", "10:00", "12:00"),
        )
        for invalid_delivery in (
            None,
            123,
            "torsdag 30. februar Kl. 10:00-12:00",
            "torsdag 31. februar Kl. 10:00-12:00",
            "torsdag 31. april Kl. 10:00-12:00",
            "torsdag 3. september Kl. 10:00-10:00",
            "torsdag 3. september Kl. 12:00-10:00",
        ):
            with self.subTest(invalid_delivery=invalid_delivery):
                with self.assertRaisesRegex(HouseholdError, "delivery window is invalid"):
                    meny_delivery_window_identity(invalid_delivery)
        for malformed in (
            {**valid, "total": None},
            {**valid, "total": True},
            {**valid, "total": float("inf")},
            {**valid, "total": 0},
            {**valid, "delivery": ""},
            {**valid, "delivery": None},
            {**valid, "submit_controls": True},
            {**valid, "submit_controls": 1.0},
            {**valid, "submit_controls": 2},
            {**valid, "total": 10**400},
            {**valid, "submit_enabled": False},
            {key: value for key, value in valid.items() if key != "total"},
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(HouseholdError, "checkout page changed"):
                    normalize_checkout_payment_snapshot(malformed)

    def test_cart_read_reloads_one_nonempty_zero_total_snapshot(self):
        client = self.client()
        zero = {
            "ready": False,
            "authenticated": True,
            "root_count": 1,
            "item_root_count": 1,
            "control_count": 1,
            "empty": False,
            "total_count": 1,
            "delivery_count": 0,
            "delivery": None,
            "items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 1, "price": 19.9}],
            "count": 1,
            "total": 0,
        }
        valid = {**zero, "ready": True, "total": 19.9}
        client._eval = mock.Mock(side_effect=[
            {"open": True, "ready": True, "authenticated": True, "root_count": 1},
            zero,
            {"open": False, "ready": True, "authenticated": True, "root_count": 0, "open_count": 1},
            valid,
        ])
        client._invoke = mock.Mock(return_value={})
        client._sleep = mock.Mock()
        self.assertEqual(client._read_cart()["total"], 19.9)
        self.assertEqual(client._invoke.call_args_list, [
            mock.call("reload"),
            mock.call("click", '[data-hermes-meal-planner-action="open-cart"]'),
        ])
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.5), mock.call(0.5)])

    def test_search_uses_encoded_bound_results_route_and_one_scoped_root(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        scripts = []
        client._eval = lambda script: scripts.append(script) or {
            "ready": True,
            "identity": True,
            "route": True,
            "authenticated": True,
            "root_count": 1,
            "state_root_count": 1,
            "query_count": 1,
            "heading_count": 1,
            "products": [{"product_id": MENY_PRODUCT, "name": "Hvitløk"}],
            "recipes": [],
        }
        result = client._search("  hvit   løk/KIND/EXPANDED/HEADING  ", 5, "products")
        client._open.assert_called_once_with("https://meny.no/sok?query=hvit+l%C3%B8k%2FKIND%2FEXPANDED%2FHEADING")
        self.assertEqual(result["products"][0]["name"], "Hvitløk")
        self.assertIn('"query": "hvit løk/KIND/EXPANDED/HEADING"', scripts[0])
        self.assertIn("const {query, expanded, heading, kind}", scripts[0])
        self.assertIn("parameters.getAll('query')", scripts[0])
        self.assertIn("parameters.getAll('expanded')", scripts[0])
        self.assertIn("keys.length === 2", scripts[0])
        self.assertIn("roots.length !== 1", scripts[0])
        self.assertIn('`Resultater for "${query}"`', scripts[0])
        self.assertIn(":scope > .ws-search-result__header", scripts[0])
        self.assertIn(":scope > h2.ws-search-result__title", scripts[0])
        self.assertIn("queryHeaders.length === 0", scripts[0])
        self.assertIn("queryHeaders.length === 1 && visibleQueryHeaders.length === 1", scripts[0])
        self.assertIn("queryHeaderElements.length === 1 && visibleQueryHeaderElements.length === 1", scripts[0])
        self.assertIn("root.querySelectorAll('li.ws-product-list-vertical__item')", scripts[0])
        self.assertIn("paths.size !== 1", scripts[0])
        self.assertIn("visiblePaths.length === 0", scripts[0])

    def test_product_search_accepts_current_results_shell_without_optional_query_header(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(return_value={
            "ready": True,
            "identity": True,
            "route": True,
            "authenticated": True,
            "root_count": 1,
            "state_root_count": 1,
            "query_header_count": 0,
            "query_count": 0,
            "heading_count": 1,
            "products": [{"product_id": MENY_PRODUCT, "name": "Purre"}],
            "recipes": [],
        })
        result = client._search("purre", 5, "products")
        self.assertEqual(result["products"][0]["name"], "Purre")
        client._sleep.assert_not_called()

    def test_product_search_rejects_an_existing_header_without_the_exact_query_title(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(return_value={
            "ready": True,
            "identity": True,
            "route": True,
            "authenticated": True,
            "root_count": 1,
            "state_root_count": 1,
            "query_header_count": 1,
            "query_count": 0,
            "heading_count": 1,
            "products": [{"product_id": MENY_PRODUCT, "name": "Feil resultat"}],
            "recipes": [],
        })
        with self.assertRaisesRegex(HouseholdError, "did not finish rendering"):
            client._search("purre", 5, "products")

    def test_search_rejects_wrong_or_duplicate_route_identity_without_polling(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(return_value={
            "ready": False,
            "identity": False,
            "route": False,
            "authenticated": True,
            "root_count": 1,
            "state_root_count": 1,
            "query_count": 1,
            "heading_count": 1,
            "products": [],
            "recipes": [],
        })
        with self.assertRaisesRegex(HouseholdError, "route changed"):
            client._search("brokkoli", 5, "products")
        client._sleep.assert_not_called()

    def test_search_accepts_only_explicit_scoped_empty_state(self):
        client = self.client()
        client._open = mock.Mock()
        client._select_recipe_results = mock.Mock()
        client._sleep = mock.Mock()
        scripts = []
        client._eval = lambda script: scripts.append(script) or {
            "ready": True,
            "identity": True,
            "route": True,
            "authenticated": True,
            "root_count": 1,
            "state_root_count": 1,
            "query_count": 1,
            "heading_count": 1,
            "products": [],
            "recipes": [],
        }
        self.assertEqual(client._search("ingen treff", 5, "recipes")["recipes"], [])
        client._select_recipe_results.assert_called_once_with("ingen treff")
        self.assertIn(":scope > p.ws-search-result-full__empty", scripts[0])
        self.assertIn("`Ingen treff på ${query}`", scripts[0])
        self.assertIn("root.querySelectorAll('li.ws-search-item--type-recipe')", scripts[0])

    def test_search_fails_closed_on_ambiguous_root_or_still_loading(self):
        client = self.client()
        client._open = mock.Mock()
        client._invoke = mock.Mock(return_value={})
        client._sleep = mock.Mock()
        client._eval = mock.Mock(return_value={
            "ready": False,
            "identity": True,
            "route": True,
            "authenticated": True,
            "root_count": 2,
            "state_root_count": 0,
            "query_count": 0,
            "heading_count": 0,
            "products": [],
            "recipes": [],
        })
        with self.assertRaisesRegex(HouseholdError, "did not finish rendering"):
            client._search("brokkoli", 5, "products")
        self.assertEqual(client._eval.call_count, 40)
        self.assertEqual(client._sleep.call_count, 40)
        client._invoke.assert_called_once_with("reload")

    def test_search_reprobes_login_before_classifying_a_missing_shell(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._invoke = mock.Mock(return_value={})
        client._require_login = mock.Mock()
        client._eval = mock.Mock(return_value={
            "ready": False,
            "identity": True,
            "route": True,
            "authenticated": False,
            "root_count": 1,
            "state_root_count": 1,
            "query_count": 1,
            "heading_count": 1,
            "products": [],
            "recipes": [],
        })
        with self.assertRaisesRegex(HouseholdError, "did not finish rendering"):
            client._search("brokkoli", 5, "products")
        self.assertEqual(client._eval.call_count, 40)
        self.assertEqual(client._sleep.call_count, 40)
        client._invoke.assert_called_once_with("reload")
        client._require_login.assert_called_once_with()

    def test_search_reports_login_only_when_the_store_reprobe_is_logged_out(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._invoke = mock.Mock(return_value={})
        client._require_login = mock.Mock(side_effect=HouseholdError(
            "MENY login is required in the configured browser profile"
        ))
        client._eval = mock.Mock(return_value={
            "ready": False,
            "identity": True,
            "route": True,
            "authenticated": False,
            "root_count": 0,
            "state_root_count": 0,
            "query_count": 0,
            "heading_count": 0,
            "products": [],
            "recipes": [],
        })
        with self.assertRaisesRegex(HouseholdError, "login is required"):
            client._search("brokkoli", 5, "products")
        client._require_login.assert_called_once_with()

    def test_search_accepts_login_shell_hydration_for_the_same_route(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(side_effect=[
            {
                "ready": False,
                "identity": True,
                "route": True,
                "authenticated": False,
                "root_count": 0,
                "state_root_count": 0,
                "query_count": 0,
                "heading_count": 0,
                "products": [],
                "recipes": [],
            },
            {
                "ready": True,
                "identity": True,
                "route": True,
                "authenticated": True,
                "root_count": 1,
                "state_root_count": 1,
                "query_count": 1,
                "heading_count": 1,
                "products": [{"product_id": MENY_PRODUCT}],
                "recipes": [],
            },
        ])
        result = client._search("brokkoli", 5, "products")
        self.assertEqual(result["products"], [{"product_id": MENY_PRODUCT}])
        client._sleep.assert_called_once_with(0.25)

    def test_recipe_search_selects_bound_visible_kind_control(self):
        client = self.client()
        client._eval = mock.Mock(return_value={"ready": True, "identity": True, "route": True, "authenticated": True})
        client._invoke = mock.Mock(return_value={})
        client._sleep = mock.Mock()
        client._select_recipe_results('KIND "torsk"')
        script = client._eval.call_args.args[0]
        self.assertIn('const query = "KIND \\"torsk\\""', script)
        self.assertIn(":scope > .ws-search-result__header", script)
        self.assertIn("queryValues.length === 1", script)
        self.assertIn("radios[0].labels", script)
        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="search-kind"]')
        client._sleep.assert_not_called()

    def test_recipe_search_reprobes_login_before_classifying_a_missing_shell(self):
        client = self.client()
        client._eval = mock.Mock(return_value={
            "ready": False,
            "identity": True,
            "route": True,
            "authenticated": False,
        })
        client._invoke = mock.Mock(return_value={})
        client._sleep = mock.Mock()
        client._require_login = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "did not finish rendering"):
            client._select_recipe_results("torsk")
        self.assertEqual(client._eval.call_count, 40)
        self.assertEqual(client._sleep.call_count, 40)
        client._invoke.assert_called_once_with("reload")
        client._require_login.assert_called_once_with()

    def test_search_polls_same_query_while_result_kind_transition_settles(self):
        client = self.client()
        client._open = mock.Mock()
        client._select_recipe_results = mock.Mock()
        client._sleep = mock.Mock()
        waiting = {
            "ready": False,
            "identity": True,
            "route": False,
            "authenticated": True,
            "root_count": 1,
            "state_root_count": 1,
            "query_count": 1,
            "heading_count": 1,
            "products": [],
            "recipes": [],
        }
        ready = {
            **waiting,
            "ready": True,
            "route": True,
            "recipes": [{"recipe_id": "/oppskrifter/fisk/torsk", "name": "Torsk"}],
        }
        client._eval = mock.Mock(side_effect=[waiting, ready])
        result = client._search("torsk", 5, "recipes")
        self.assertEqual(result["recipes"][0]["name"], "Torsk")
        client._sleep.assert_called_once_with(0.25)
        client._select_recipe_results.assert_called_once_with("torsk")

    def test_search_polls_exact_route_while_card_snapshot_settles(self):
        client = self.client()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        incomplete = {
            "ready": False,
            "identity": True,
            "route": True,
            "authenticated": True,
            "root_count": 1,
            "heading_count": 1,
            "products": [],
            "recipes": [],
        }
        ready = {
            **incomplete,
            "ready": True,
            "state_root_count": 1,
            "query_count": 1,
            "products": [{"product_id": MENY_PRODUCT, "name": "Brokkoli"}],
        }
        client._eval = mock.Mock(side_effect=[incomplete, ready])
        result = client._search("brokkoli", 5, "products")
        self.assertEqual(result["products"][0]["name"], "Brokkoli")
        client._sleep.assert_called_once_with(0.25)

    def test_cart_click_rechecks_login_before_and_after_mutation(self):
        client = self.client()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._product_control = mock.Mock(return_value={"ready": True, "authenticated": True, "quantity": 0, "label": "Legg Brokkoli i handlevognen"})
        client._click_cart_control = mock.Mock()
        client._resolve_order_route = mock.Mock()
        client._wait_for_quantity = mock.Mock(return_value=1)
        client._change_one(MENY_PRODUCT, 1)
        self.assertEqual(client._assert_authenticated.call_count, 2)
        client._click_cart_control.assert_called_once_with(MENY_PRODUCT, "Legg Brokkoli i handlevognen")

    def test_cart_change_waits_for_the_product_controls_to_render(self):
        client = self.client()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._sleep = mock.Mock()
        client._product_control = mock.Mock(side_effect=[
            {"ready": False, "page_ready": False, "authenticated": True},
            {"ready": True, "page_ready": True, "authenticated": True, "quantity": 1, "label": "Fjern Brokkoli fra handlevognen"},
        ])
        client._click_cart_control = mock.Mock()
        client._resolve_order_route = mock.Mock()
        client._wait_for_quantity = mock.Mock(return_value=0)

        client._change_one(MENY_PRODUCT, -1)

        client._sleep.assert_called_once_with(0.25)
        client._click_cart_control.assert_called_once_with(MENY_PRODUCT, "Fjern Brokkoli fra handlevognen")

    def test_cart_remove_falls_back_to_the_exact_cart_control(self):
        client = self.client()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._sleep = mock.Mock()
        client._product_control = mock.Mock(return_value={"ready": False, "page_ready": False, "authenticated": True})
        client._read_cart = mock.Mock(return_value={"items": [{"product_id": MENY_PRODUCT, "quantity": 1}]})
        client._wait_for_cart_quantity = mock.Mock(return_value=0)
        scripts = []
        client._eval = lambda script: scripts.append(script) or {"ready": True}
        calls = []
        client._invoke = lambda *arguments: calls.append(arguments) or ({"box": {"x": 1, "y": 2, "width": 20, "height": 10}} if arguments[:2] == ("get", "box") else {})

        client._change_one(MENY_PRODUCT, -1)

        self.assertEqual(client._product_control.call_count, 20)
        self.assertIn(("mouse", "down"), calls)
        self.assertIn(("mouse", "up"), calls)
        self.assertIn(MENY_PRODUCT, scripts[0])
        self.assertIn("elementFromPoint", scripts[1])

    def test_cart_remove_fallback_marks_post_dispatch_failure_uncertain(self):
        client = self.client()
        client._read_cart = mock.Mock(return_value={"items": [{"product_id": MENY_PRODUCT, "quantity": 1}]})

        def fail_after_dispatch(_product, _quantity, _code, before_dispatch):
            before_dispatch()
            raise HouseholdError("transport failed")

        client._click_cart_remove_control = fail_after_dispatch
        with self.assertRaisesRegex(HouseholdError, "uncertain.*do not retry"):
            client._remove_one_from_cart(MENY_PRODUCT, None)

    def test_checkout_review_rejects_same_quantity_with_a_different_product_path(self):
        client = self.client()
        client._verify_order_change = mock.Mock()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        settling = {
            "ready": True,
            "authenticated": True,
            "step": 1,
            "next_enabled": False,
            "items": [{"product_id": "/varer/frukt-gront/gronnsaker/kal/blomkal/blomkal-1234", "identity": "Blomkål 400g", "quantity": 1}],
            "unavailable_items": [],
            "active_order_change": False,
        }
        ready = {
            "ready": True,
            "authenticated": True,
            "step": 1,
            "next_enabled": True,
            "items": [{"product_id": "/varer/frukt-gront/gronnsaker/kal/blomkal/blomkal-1234", "identity": "Blomkål 400g", "quantity": 1}],
            "unavailable_items": [],
            "active_order_change": False,
        }
        client._eval = mock.Mock(side_effect=[settling, ready])
        client._invoke = mock.Mock()
        cart = {"items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 1, "price": 19.9}], "total": 19.9}
        with self.assertRaisesRegex(HouseholdError, "items changed"):
            client._review_checkout(cart)
        client._invoke.assert_not_called()
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.8), mock.call(0.25)])

    def test_checkout_review_stops_before_next_for_inline_unavailable_items(self):
        client = self.client()
        client._verify_order_change = mock.Mock()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._eval = mock.Mock(return_value={
            "ready": True,
            "authenticated": True,
            "step": 1,
            "next_enabled": True,
            "items": [{"product_id": MENY_PRODUCT, "identity": "Brokkoli 400g", "quantity": 1}],
            "unavailable_items": [{"product_id": MENY_PRODUCT, "identity": "Brokkoli 400g", "quantity": 1}],
            "active_order_change": False,
        })
        client._invoke = mock.Mock()
        cart = {"items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 1, "price": 19.9}], "total": 19.9}

        with self.assertRaisesRegex(HouseholdError, "unavailable items: Brokkoli 400g"):
            client._review_checkout(cart)

        client._invoke.assert_not_called()
        script = client._eval.call_args.args[0]
        self.assertIn("Disse varene vil du ikke motta", script)
        self.assertIn("closest('.ws-checkout-page-section')", script)

    def test_checkout_review_waits_for_the_verified_payment_submit_to_enable(self):
        client = self.client()
        client._verify_order_change = mock.Mock()
        client._open = mock.Mock()
        client._sleep = mock.Mock()
        client._click_checkout_control = mock.Mock()
        step = {
            "ready": True,
            "authenticated": True,
            "step": 1,
            "next_enabled": True,
            "items": [{"product_id": MENY_PRODUCT, "identity": "Brokkoli 400g", "quantity": 1}],
            "unavailable_items": [],
            "active_order_change": False,
        }
        payment = {"ready": True, "checked": True}
        disabled = {
            "ready": True,
            "authenticated": True,
            "vipps_checked": True,
            "home_delivery": True,
            "submit_enabled": False,
            "total": 99.9,
            "delivery": "torsdag 3. september Kl. 10:00-12:00",
            "submit_controls": 1,
        }
        client._eval = mock.Mock(side_effect=[
            step,
            {"unavailable": False, "dismiss": False},
            payment,
            disabled,
            {**disabled, "submit_enabled": True},
        ])
        cart = {
            "items": [{"product_id": MENY_PRODUCT, "name": "Brokkoli", "quantity": 1, "price": 19.9}],
            "total": 19.9,
            "delivery": {"display": "torsdag 3. sep. kl. 10:00-12:00"},
        }

        review = client._review_checkout(cart)

        self.assertEqual(review["summary"]["total"], 99.9)
        self.assertEqual(review["payment"], "vipps")
        payment_summary_script = client._eval.call_args_list[3].args[0]
        self.assertIn("Endre dato og tid", payment_summary_script)
        self.assertIn("deliveryBinding", payment_summary_script)
        self.assertNotIn("new Set", payment_summary_script)
        self.assertNotIn("deliveryRoots", payment_summary_script)
        client._click_checkout_control.assert_called_once_with(
            "checkout-next",
            expected_items=[(MENY_PRODUCT, 1)],
            target_code=None,
        )
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.8), mock.call(0.6), mock.call(0.25)])

    def test_new_order_prompt_is_resolved_explicitly_and_bound_to_target_code(self):
        client = self.client()
        scripts = []
        results = iter([{"dialog": True, "ready": True, "route": "existing"}, {"clear": True}])
        client._eval = lambda script: scripts.append(script) or next(results)
        client._invoke = mock.Mock(return_value={})
        client._sleep = mock.Mock()
        client._resolve_order_route("TEST-CODE-1")
        self.assertIn(json.dumps("TEST-CODE-1"), scripts[0])
        self.assertIn("Start ny bestilling", scripts[0])
        self.assertIn("Endre bestilling", scripts[0])
        self.assertIn("matchAll", scripts[0])
        self.assertNotIn("text.includes", scripts[0])
        client._invoke.assert_called_once_with("click", '[data-hermes-meal-planner-action="order-route"]')

    def test_post_click_verification_error_is_always_uncertain(self):
        client = self.client()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock()
        client._product_control = mock.Mock(return_value={"ready": True, "authenticated": True, "quantity": 0, "label": "Legg Brokkoli i handlevognen"})
        client._click_cart_control = mock.Mock()
        client._wait_for_quantity = mock.Mock(side_effect=HouseholdError("MENY operation deadline reached"))
        with self.assertRaisesRegex(HouseholdError, "uncertain.*do not retry"):
            client._change_one(MENY_PRODUCT, 1)
        client._click_cart_control.assert_called_once()

    def test_cart_control_fails_before_mouse_dispatch_if_identity_or_occlusion_changes(self):
        client = self.client()
        scripts = []
        results = iter([{"ready": True}, {"clear": False}])
        client._eval = lambda script: scripts.append(script) or next(results)
        calls = []
        client._invoke = lambda *arguments, **_kwargs: calls.append(arguments) or ({"box": {"x": 1, "y": 2, "width": 20, "height": 10}} if arguments[:2] == ("get", "box") else {})
        with self.assertRaisesRegex(HouseholdError, "obscured or changed"):
            client._click_cart_control(MENY_PRODUCT, "Legg Brokkoli i handlevognen")
        self.assertNotIn(("mouse", "down"), calls)
        self.assertIn("location.pathname ===", scripts[0])
        self.assertIn("aria-disabled", scripts[0])
        self.assertIn("marked.length === 1", scripts[0])
        self.assertIn("Brukermeny", scripts[0])
        self.assertIn("elementFromPoint", scripts[1])
        self.assertIn("Brukermeny", scripts[1])

    def test_checkout_control_scrolls_and_hit_tests_before_mouse_activation(self):
        client = self.client()
        calls = []

        def invoke(*arguments):
            calls.append(arguments)
            if arguments[:2] == ("get", "box"):
                return {"box": {"x": 10, "y": 20, "width": 30, "height": 40}}
            return {}

        client._invoke = invoke
        client._eval = mock.Mock(side_effect=[{"ready": True}, {"ready": True}, {"ready": True}])
        client._click_checkout_control("checkout-next", expected_items=[(MENY_PRODUCT, 1)], target_code="TEST-CODE-1")

        selector = '[data-hermes-meal-planner-action="checkout-next"]'
        self.assertEqual(calls[0], ("scrollintoview", selector))
        self.assertEqual(calls[1], ("get", "box", selector))
        self.assertEqual([call[:2] for call in calls[2:]], [("mouse", "move"), ("mouse", "down"), ("mouse", "up")])
        self.assertIn("location.href === 'https://meny.no/kassen'", client._eval.call_args_list[0].args[0])
        self.assertIn("Se over varene", client._eval.call_args_list[2].args[0])
        self.assertIn("enabled(target)", client._eval.call_args_list[2].args[0])
        self.assertIn("candidates.length === 1", client._eval.call_args_list[2].args[0])
        self.assertIn("removeAttribute", client._eval.call_args_list[2].args[0])
        self.assertIn(MENY_PRODUCT, client._eval.call_args_list[2].args[0])
        self.assertIn("TEST-CODE-1", client._eval.call_args_list[2].args[0])
        self.assertIn("elementFromPoint", client._eval.call_args_list[2].args[0])

    def test_checkout_control_waits_for_smooth_scroll_to_reach_the_target(self):
        client = self.client()
        client._sleep = mock.Mock()
        client._invoke = mock.Mock(side_effect=lambda *arguments: (
            {"box": {"x": 10, "y": 2000, "width": 30, "height": 40}}
            if arguments[:2] == ("get", "box") else {}
        ))
        client._eval = mock.Mock(side_effect=[
            {"ready": True},
            {"ready": False},
            {"ready": False},
            {"ready": True},
            {"ready": True},
        ])

        client._click_checkout_control("checkout-next")

        self.assertEqual(client._invoke.call_args_list.count(mock.call("get", "box", '[data-hermes-meal-planner-action="checkout-next"]')), 3)
        self.assertEqual(client._sleep.call_args_list, [mock.call(0.1), mock.call(0.1)])
        self.assertIn(mock.call("mouse", "down"), client._invoke.call_args_list)

    def test_checkout_control_fails_before_mouse_dispatch_if_obscured(self):
        client = self.client()
        calls = []

        def invoke(*arguments):
            calls.append(arguments)
            if arguments[:2] == ("get", "box"):
                return {"box": {"x": 10, "y": 20, "width": 30, "height": 40}}
            return {}

        client._invoke = invoke
        client._eval = mock.Mock(side_effect=[{"ready": True}, *([{"ready": False}] * 20)])
        with self.assertRaisesRegex(HouseholdError, "obscured or changed"):
            client._click_checkout_control("checkout-next")
        self.assertNotIn(("mouse", "down"), calls)

    def test_vipps_activation_requires_enabled_radio_and_noninteractive_label_hit(self):
        client = self.client()
        client._eval = mock.Mock(return_value={"ready": False})
        client._invoke = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "control changed"):
            client._click_checkout_control("vipps")
        script = client._eval.call_args.args[0]
        self.assertIn("enabled(radio)", script)
        self.assertIn("hitInteractive", script)
        self.assertIn("label === target", script)
        self.assertIn("vipps.length === 1", script)
        client._invoke.assert_not_called()

    def test_checkout_submit_revalidates_exact_gate_after_hover_before_dispatch(self):
        client = self.client()
        review = {
            "summary": {"total": 1234.56, "delivery": {"display": "torsdag 3. september Kl. 09:00-12:00"}},
            "target_order_code": "XY-CODE-1",
        }
        events = []

        def evaluate(script):
            events.append(("eval", script))
            return {"ready": True}

        def invoke(*arguments):
            events.append(arguments)
            if arguments[:2] == ("get", "box"):
                return {"box": {"x": 10.4, "y": 20.4, "width": 30.3, "height": 40.3}}
            return {}

        client._eval = evaluate
        client._invoke = invoke
        client._require_time = lambda value: events.append(("require_time", value))
        client._wait_for_vipps_dispatch = lambda: events.append(("wait_for_vipps_dispatch",))
        client._click_checkout_submit(review, lambda: events.append(("dispatch_fence",)))

        kinds = [event[0] for event in events]
        self.assertEqual(kinds, ["eval", "scrollintoview", "get", "eval", "mouse", "eval", "require_time", "network", "dispatch_fence", "click", "wait_for_vipps_dispatch"])
        self.assertEqual(events[4], ("mouse", "move", "26", "41"))
        self.assertEqual(events[7], ("network", "requests", "--clear"))
        self.assertEqual(events[9], ("click", '[data-hermes-meal-planner-action="checkout-submit"]'))
        second_gate = events[5][1]
        self.assertIn("elementFromPoint(26, 41)", second_gate)
        self.assertIn("location.href ===", second_gate)
        self.assertIn("123456", second_gate)
        self.assertIn("XY-CODE-1", second_gate)
        self.assertIn("torsdag 3. september Kl. 09:00-12:00", second_gate)
        self.assertIn("Endre dato og tid", second_gate)
        self.assertIn("deliveryBinding", second_gate)
        self.assertNotIn("deliveryRoots", second_gate)

    def test_vipps_dispatch_requires_one_successful_payment_post(self):
        self.assertTrue(vipps_dispatch_acknowledged({"requests": [{
            "method": "POST",
            "status": 200,
            "url": "https://platform-rest-prod.ngdata.no/api/order/payment",
        }]}))
        for request in (
            {"method": "GET", "status": 200, "url": "https://platform-rest-prod.ngdata.no/api/order/payment"},
            {"method": "POST", "status": 500, "url": "https://platform-rest-prod.ngdata.no/api/order/payment"},
            {"method": "POST", "status": 200, "url": "https://platform-rest-prod.ngdata.no/api/client-notifications/"},
            {"method": "POST", "status": 200, "url": "https://meny.no/api/visitor-group-cookie/refresh"},
            {"method": "POST", "status": 200, "url": "https://platform-rest-prod.ngdata.no/api/calculator/"},
            {"method": "POST", "status": 200, "url": "https://analytics.example/payment"},
        ):
            with self.subTest(request=request):
                self.assertFalse(vipps_dispatch_acknowledged({"requests": [request]}))
        with self.assertRaisesRegex(HouseholdError, "request log changed"):
            vipps_dispatch_acknowledged({"requests": {}})

    def test_order_search_requires_the_exact_successful_meny_endpoint(self):
        self.assertTrue(meny_order_search_completed({"requests": [{
            "method": "GET",
            "status": 200,
            "url": "https://platform-rest-prod.ngdata.no/api/order/search/store/user?page=1",
        }]}))
        self.assertFalse(meny_order_search_completed({"requests": [{
            "method": "GET",
            "status": 200,
            "url": "https://platform-rest-prod.ngdata.no/api/client-notifications/",
        }]}))

    def test_vipps_dispatch_waits_for_the_payment_response(self):
        client = self.client()
        client._sleep = mock.Mock()
        client._invoke = mock.Mock(side_effect=[
            {"requests": [{"method": "POST", "status": None, "url": "https://platform-rest-prod.ngdata.no/api/order/payment"}]},
            {"requests": [{"method": "POST", "status": 201, "url": "https://platform-rest-prod.ngdata.no/api/order/payment"}]},
        ])

        client._wait_for_vipps_dispatch()

        self.assertEqual(client._invoke.call_args_list, [
            mock.call("network", "requests"),
            mock.call("network", "requests"),
        ])
        client._sleep.assert_called_once_with(0.25)

    def test_vipps_dispatch_without_acknowledgement_is_uncertain(self):
        client = self.client()
        client._sleep = mock.Mock()
        client._invoke = mock.Mock(return_value={"requests": []})

        with self.assertRaisesRegex(HouseholdError, "uncertain.*do not retry"):
            client._wait_for_vipps_dispatch()

        self.assertEqual(client._invoke.call_count, 40)
        self.assertEqual(client._sleep.call_count, 39)

    def test_checkout_submit_does_not_open_dispatch_fence_when_post_hover_gate_fails(self):
        client = self.client()
        review = {"summary": {"total": 1234.56, "delivery": {"display": "delivery"}}, "target_order_code": None}
        calls = []
        client._eval = mock.Mock(side_effect=[{"ready": True}, {"ready": True}, {"ready": False}])
        client._invoke = lambda *arguments: calls.append(arguments) or ({"box": {"x": 1, "y": 2, "width": 20, "height": 10}} if arguments[:2] == ("get", "box") else {})
        fence = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "changed or is obscured"):
            client._click_checkout_submit(review, fence)
        fence.assert_not_called()
        self.assertNotIn(("mouse", "down"), calls)

    def test_cart_click_never_dispatches_after_login_loss(self):
        client = self.client()
        client._open = mock.Mock()
        client._assert_authenticated = mock.Mock(side_effect=HouseholdError("MENY login is required"))
        client._invoke = mock.Mock()
        with self.assertRaisesRegex(HouseholdError, "login is required"):
            client._change_one(MENY_PRODUCT, 1)
        client._invoke.assert_not_called()

    def test_expired_client_deadline_does_not_acquire_the_meny_lock_or_click(self):
        client = self.client()
        client._require_login = mock.Mock()
        client._change_cart = mock.Mock()
        client.lock.acquire()
        try:
            with mock.patch("meny.time.monotonic", side_effect=[0, 101]):
                with self.assertRaisesRegex(HouseholdError, "deadline reached"):
                    client.call("manipulate_cart", {"operations": [{"productId": MENY_PRODUCT, "quantity": 1}]}, deadline=100)
        finally:
            client.lock.release()
        client._require_login.assert_not_called()
        client._change_cart.assert_not_called()

    def test_vipps_submit_dispatches_one_exact_final_click(self):
        client = self.client()
        review = {
            "page_digest": "a" * 64,
            "summary": {"payment": "vipps", "total": 1234.56, "delivery": {"display": "torsdag 3. september Kl. 09:00-12:00"}},
            "target_order_code": None,
        }
        client._review_checkout = mock.Mock(return_value=review)
        client._eval = mock.Mock(return_value={"ready": True})
        client._click_checkout_submit = mock.Mock()
        result = client.submit_checkout({"items": []}, review)
        self.assertTrue(result["awaiting_user_payment"])
        client._click_checkout_submit.assert_called_once()
        self.assertEqual(client._click_checkout_submit.call_args.args[0], review)
        self.assertTrue(callable(client._click_checkout_submit.call_args.args[1]))
        self.assertIn("Vipps", client._eval.call_args.args[0])
        self.assertIn("Levert på døren", client._eval.call_args.args[0])
        self.assertIn("123456", client._eval.call_args.args[0])
        self.assertIn("torsdag 3. september Kl. 09:00-12:00", client._eval.call_args.args[0])
        self.assertIn("Endre dato og tid", client._eval.call_args.args[0])
        self.assertIn("deliveryBinding", client._eval.call_args.args[0])
        self.assertNotIn("deliveryRoots", client._eval.call_args.args[0])

    def test_vipps_mouse_failure_after_dispatch_fence_is_uncertain(self):
        client = self.client()
        review = {
            "page_digest": "a" * 64,
            "summary": {"payment": "vipps", "total": 1234.56, "delivery": {"display": "delivery"}},
            "target_order_code": None,
        }
        client._review_checkout = mock.Mock(return_value=review)
        client._eval = mock.Mock(return_value={"ready": True})

        def fail_after_fence(_review, fence):
            fence()
            raise HouseholdError("mouse down outcome is uncertain")

        client._click_checkout_submit = fail_after_fence
        with self.assertRaisesRegex(HouseholdError, "outcome is uncertain") as caught:
            client.submit_checkout({"items": []}, review)
        self.assertNotIsInstance(caught.exception, CheckoutPreconditionError)

    def test_vipps_precondition_failure_dispatches_no_click(self):
        client = self.client()
        client._review_checkout = mock.Mock(side_effect=HouseholdError("checkout changed"))
        client._invoke = mock.Mock()
        with self.assertRaises(CheckoutPreconditionError):
            client.submit_checkout({"items": []}, {"page_digest": "old"})
        client._invoke.assert_not_called()

    def test_vipps_final_gate_failure_dispatches_no_click(self):
        client = self.client()
        review = {
            "page_digest": "a" * 64,
            "summary": {"payment": "vipps", "total": 1234.56, "delivery": {"display": "torsdag 3. september Kl. 09:00-12:00"}},
            "target_order_code": "TEST-CODE-1",
        }
        client._review_checkout = mock.Mock(return_value=review)
        client._eval = mock.Mock(return_value={"ready": False})
        client._invoke = mock.Mock()
        with self.assertRaises(CheckoutPreconditionError):
            client.submit_checkout({"items": []}, review, order_change={"order_id": "99990001", "code": "TEST-CODE-1"})
        client._invoke.assert_not_called()

    def test_meny_cancellation_final_gate_binds_exact_order_path_and_visible_id(self):
        client = self.client()
        order = {
            "provider": "meny", "orderNumber": "99990001", "order_number": "99990001", "id": "99990001",
            "code": "TEST-CODE-1", "status": "confirmed", "grossAmount": 1200.0,
            "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00", "productQuantityCount": 1,
            "products": [{"identity": "Brokkoli 400g", "name": "Brokkoli 400g", "quantity": 1}],
        }
        review = {"available": True, "consequence": None, "order_digest": "a" * 64}
        client.review_cancellation = mock.Mock(return_value=review)
        client._get_order = mock.Mock(return_value=deepcopy(order))
        scripts = []
        client._eval = lambda script: scripts.append(script) or {"ready": True}
        client._invoke = mock.Mock(return_value={})
        client._sleep = mock.Mock()
        client.submit_cancellation("99990001", order, review)
        self.assertEqual(client._invoke.call_count, 2)
        self.assertTrue(all("99990001" in script and "location.pathname" in script and "Ordrenummer" in script for script in scripts))

    def test_meny_cancellation_rejects_a_fresh_order_change_before_any_click(self):
        client = self.client()
        order = {"orderNumber": "99990001", "grossAmount": 1200.0}
        review = {"available": True}
        client.review_cancellation = mock.Mock(return_value=review)
        client._get_order = mock.Mock(return_value={"orderNumber": "99990001", "grossAmount": 1201.0})
        client._invoke = mock.Mock()
        with self.assertRaises(CancellationPreconditionError):
            client.submit_cancellation("99990001", order, review)
        client._invoke.assert_not_called()


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name), CONFIG)
        self.oda = FakeOda()
        self.browser = FakeBrowser()
        self.browser.oda = self.oda
        self.app = Application(self.store, self.oda, self.browser)

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_and_reversible_cart_use_mcp(self):
        self.app.handle({"operation": "catalog", "action": "products", "query": "fullkorn"})
        self.app.handle({"operation": "cart", "action": "change", "operations": [{"productId": 10, "quantity": 1}]})
        self.assertEqual([call[0] for call in self.oda.calls[-2:]], ["product_search", "manipulate_cart"])

    def test_menu_clear_discards_only_an_expired_pre_dispatch_checkout(self):
        current = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with self.store.locked() as state:
            state["menu"] = {"week": "2026-W36"}
            state["pending_checkout"] = {
                "status": "awaiting_confirmation",
                "expires_at": (current - timedelta(seconds=1)).isoformat(),
            }
        with mock.patch("service.now", return_value=current):
            self.assertEqual(self.app.handle({"operation": "menu", "action": "clear"}), {"menu": None})
        state = self.store.read()
        self.assertIsNone(state["menu"])
        self.assertIsNone(state["pending_checkout"])

        with self.store.locked() as state:
            state["menu"] = {"week": "2026-W36"}
            state["pending_checkout"] = {"status": "uncertain", "expires_at": "invalid"}
        with self.assertRaisesRegex(HouseholdError, "checkout is pending"):
            self.app.handle({"operation": "menu", "action": "clear"})

    def test_meny_transport_recovery_is_disabled_during_every_protected_state(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.call = mock.Mock(wraps=provider.call)
            app = Application(store, provider, self.browser)
            request = {"operation": "catalog", "action": "products", "query": "brokkoli"}

            app.handle(request)
            self.assertTrue(provider.call.call_args.kwargs["allow_recovery"])
            for key, value in (
                ("pending_checkout", {"status": "awaiting_confirmation"}),
                ("pending_cancellation", {"status": "awaiting_confirmation"}),
                ("order_change", {"status": "editing"}),
            ):
                with store.locked() as state:
                    state[key] = value
                app.handle(request)
                self.assertFalse(provider.call.call_args.kwargs["allow_recovery"])
                with store.locked() as state:
                    state[key] = None

    def test_meny_order_reads_use_the_full_order_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.call = mock.Mock(wraps=provider.call)
            app = Application(store, provider, self.browser)
            with mock.patch("service.time.monotonic", return_value=10.0):
                app.handle({"operation": "orders", "action": "list"})
            self.assertEqual(provider.call.call_args.kwargs["deadline"], 190.0)

    def test_meny_delivery_reads_use_the_full_order_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.call = mock.Mock(wraps=provider.call)
            app = Application(store, provider, self.browser)
            with mock.patch("service.time.monotonic", return_value=10.0):
                app.handle({"operation": "delivery", "action": "list"})
            self.assertEqual(provider.call.call_args.kwargs["deadline"], 190.0)

    def test_cart_change_accepts_intuitive_action_and_snake_case_product_id(self):
        self.app.handle({"operation": "cart", "action": "update", "operations": [{"product_id": "10", "quantity": 1}]})
        self.assertEqual(
            self.oda.calls[-1],
            ("manipulate_cart", {"operations": [{"productId": 10, "quantity": 1}]}),
        )

    def test_meny_keeps_opaque_product_path_and_waits_for_vipps(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            app = Application(store, provider, self.browser)
            app.handle({"operation": "cart", "action": "update", "operations": [{"product_id": MENY_PRODUCT, "quantity": 1}]})
            self.assertEqual(
                provider.calls[-1],
                ("manipulate_cart", {"operations": [{"productId": MENY_PRODUCT, "quantity": 1}]}),
            )
            provider.call = mock.Mock(wraps=provider.call)
            with mock.patch("service.time.monotonic", return_value=10.0):
                prepared = app.handle({"operation": "checkout", "action": "prepare"})
            self.assertEqual(prepared["summary"]["payment"], "vipps")
            prepare_calls = provider.call.call_args_list[:]
            self.assertEqual([call.args[0] for call in prepare_calls], ["get_cart", "get_orders"])
            self.assertTrue(all(call.kwargs.get("deadline") == 250.0 for call in prepare_calls))
            with mock.patch("service.time.monotonic", return_value=20.0):
                result = app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
            self.assertTrue(result["awaiting_user_payment"])
            confirm_calls = provider.call.call_args_list[len(prepare_calls):]
            self.assertEqual([call.args[0] for call in confirm_calls], ["get_cart"])
            self.assertEqual(confirm_calls[0].kwargs.get("deadline"), 260.0)
            self.assertEqual(store.read()["pending_checkout"]["status"], "awaiting_user_payment")
            self.assertEqual(provider.checkout_clicks, 1)
            provider.confirmation_order_id = "99990002"
            provider.orders.append({
                "orderNumber": "99990002",
                "order_number": "99990002",
                "id": "99990002",
                "status": "confirmed",
                "grossAmount": 40.0,
                "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00",
                "productQuantityCount": 1,
                "products": [{"identity": "Brokkoli 400g", "name": "Brokkoli 400g", "quantity": 1}],
            })
            provider.checkout_confirmation_order_id = mock.Mock(wraps=provider.checkout_confirmation_order_id)
            reconcile_start = len(provider.call.call_args_list)
            with mock.patch("service.time.monotonic", return_value=20.0):
                reconciled = app.handle({"operation": "checkout", "action": "reconcile"})
            self.assertTrue(reconciled["confirmed"])
            self.assertIsNone(store.read()["pending_checkout"])
            reconcile_calls = provider.call.call_args_list[reconcile_start:]
            self.assertEqual(len(reconcile_calls), 3)
            self.assertTrue(all(call.kwargs.get("deadline") == 260.0 for call in reconcile_calls))
            self.assertEqual(provider.checkout_confirmation_order_id.call_args.kwargs["deadline"], 260.0)
            with self.assertRaisesRegex(HouseholdError, "cart_ready"):
                app.handle({
                    "operation": "schedule",
                    "action": "update",
                    "changes": {"enabled": True, "maximum_total": 1000, "delivery": {"weekday": "Saturday"}, "auto_checkout": True},
                })
            self.assertFalse(store.read()["schedule"]["auto_checkout"])

    def test_expired_unapproved_vipps_releases_the_checkout_for_a_fresh_prepare(self):
        started = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            app = Application(store, provider, self.browser)
            with mock.patch("service.now", return_value=started):
                prepared = app.handle({"operation": "checkout", "action": "prepare"})
                app.handle({
                    "operation": "checkout",
                    "action": "confirm",
                    "confirmation_id": prepared["confirmation_id"],
                })
            pending = store.read()["pending_checkout"]
            self.assertEqual(
                datetime.fromisoformat(pending["payment_expires_at"]),
                started + timedelta(minutes=11),
            )

            with mock.patch("service.now", return_value=started + timedelta(minutes=12)):
                reconciled = app.handle({"operation": "checkout", "action": "reconcile"})

            self.assertFalse(reconciled["confirmed"])
            self.assertTrue(reconciled["expired"])
            self.assertTrue(reconciled["retry_allowed"])
            self.assertIsNone(store.read()["pending_checkout"])
            self.assertEqual(provider.checkout_clicks, 1)

    def test_expired_vipps_ignores_one_unrelated_order_missing_from_the_baseline(self):
        started = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            app = Application(store, provider, self.browser)
            with mock.patch("service.now", return_value=started):
                prepared = app.handle({"operation": "checkout", "action": "prepare"})
                app.handle({
                    "operation": "checkout",
                    "action": "confirm",
                    "confirmation_id": prepared["confirmation_id"],
                })
            provider.orders.append({
                "orderNumber": "99990001", "order_number": "99990001", "id": "99990001",
                "status": "confirmed", "grossAmount": 10.0,
                "deliverySlotDisplay": "fredag 4. sep. kl. 12:00-14:00",
                "productQuantityCount": 1,
                "products": [{"identity": "Et annet produkt", "quantity": 1}],
            })

            with mock.patch("service.now", return_value=started + timedelta(minutes=12)):
                reconciled = app.handle({"operation": "checkout", "action": "reconcile"})

            self.assertTrue(reconciled["expired"])
            self.assertTrue(reconciled["retry_allowed"])
            self.assertIsNone(store.read()["pending_checkout"])

    def test_expired_vipps_keeps_an_exact_unconfirmed_order_candidate_locked(self):
        started = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            app = Application(store, provider, self.browser)
            with mock.patch("service.now", return_value=started):
                prepared = app.handle({"operation": "checkout", "action": "prepare"})
                app.handle({
                    "operation": "checkout",
                    "action": "confirm",
                    "confirmation_id": prepared["confirmation_id"],
                })
            provider.orders.append({
                "orderNumber": "99990002", "order_number": "99990002", "id": "99990002",
                "status": "confirmed", "grossAmount": 40.0,
                "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00",
                "productQuantityCount": 1,
                "products": [{"identity": "Brokkoli 400g", "quantity": 1}],
            })

            with mock.patch("service.now", return_value=started + timedelta(minutes=12)):
                reconciled = app.handle({"operation": "checkout", "action": "reconcile"})

            self.assertFalse(reconciled["expired"])
            self.assertFalse(reconciled["retry_allowed"])
            self.assertEqual(store.read()["pending_checkout"]["status"], "uncertain")

    def test_legacy_unapproved_vipps_uses_the_guarded_confirmation_expiry(self):
        started = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            app = Application(store, provider, self.browser)
            with mock.patch("service.now", return_value=started):
                prepared = app.handle({"operation": "checkout", "action": "prepare"})
                app.handle({
                    "operation": "checkout",
                    "action": "confirm",
                    "confirmation_id": prepared["confirmation_id"],
                })
            with store.locked() as state:
                state["pending_checkout"].pop("payment_requested_at")
                state["pending_checkout"].pop("payment_expires_at")

            with mock.patch("service.now", return_value=started + timedelta(minutes=21)):
                reconciled = app.handle({"operation": "checkout", "action": "reconcile"})

            self.assertTrue(reconciled["expired"])
            self.assertTrue(reconciled["retry_allowed"])
            self.assertIsNone(store.read()["pending_checkout"])

    def test_awaiting_vipps_payment_locks_every_other_order_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.orders.append({
                "orderNumber": "99990001", "order_number": "99990001", "id": "99990001", "status": "confirmed",
                "grossAmount": 1200.0, "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00",
                "productQuantityCount": 1, "products": [{"identity": "Brokkoli 400g", "quantity": 1}],
            })
            app = Application(store, provider, self.browser)
            with store.locked() as state:
                state["pending_checkout"] = {"status": "awaiting_user_payment"}
            blocked = (
                {"operation": "catalog", "action": "products", "query": "brokkoli"},
                {"operation": "cart", "action": "get"},
                {"operation": "cart", "action": "change", "operations": [{"product_id": MENY_PRODUCT, "quantity": 1}]},
                {"operation": "delivery", "action": "list"},
                {"operation": "delivery", "action": "select", "slot_id": "thursday-09"},
                {"operation": "checkout", "action": "prepare"},
                {"operation": "orders", "action": "list"},
                {"operation": "orders", "action": "get", "order_id": "99990001"},
                {"operation": "orders", "action": "change_begin", "order_id": "99990001"},
                {"operation": "orders", "action": "cancel_prepare", "order_id": "99990001"},
                {"operation": "email", "action": "due"},
            )
            for request in blocked:
                with self.subTest(request=request), self.assertRaisesRegex(HouseholdError, "pending|reconcile"):
                    app.handle(request)
            self.assertEqual(provider.checkout_clicks, 0)

    def test_unapproved_vipps_without_an_order_fails_closed_after_reconcile(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            app = Application(store, provider, self.browser)

            prepared = app.handle({"operation": "checkout", "action": "prepare"})
            dispatched = app.handle({
                "operation": "checkout",
                "action": "confirm",
                "confirmation_id": prepared["confirmation_id"],
            })
            self.assertTrue(dispatched["awaiting_user_payment"])
            self.assertFalse(dispatched["retry_allowed"])
            self.assertEqual(provider.checkout_clicks, 1)

            reconciled = app.handle({"operation": "checkout", "action": "reconcile"})

            self.assertFalse(reconciled["confirmed"])
            self.assertFalse(reconciled["retry_allowed"])
            self.assertIsNone(reconciled["order"])
            self.assertEqual(store.read()["pending_checkout"]["status"], "uncertain")
            with self.assertRaisesRegex(HouseholdError, "reconcile the pending checkout"):
                app.handle({"operation": "checkout", "action": "prepare"})
            self.assertEqual(provider.checkout_clicks, 1)

    def test_meny_post_click_transport_failure_never_retries_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            app = Application(store, provider, self.browser)

            def fail_after_dispatch(*_args, **_kwargs):
                provider.checkout_clicks += 1
                raise HouseholdError("MENY payment result is uncertain")

            provider.submit_checkout = fail_after_dispatch
            prepared = app.handle({"operation": "checkout", "action": "prepare"})
            with self.assertRaisesRegex(HouseholdError, "result is uncertain"):
                app.handle({
                    "operation": "checkout",
                    "action": "confirm",
                    "confirmation_id": prepared["confirmation_id"],
                })
            self.assertEqual(store.read()["pending_checkout"]["status"], "uncertain")

            reconciled = app.handle({"operation": "checkout", "action": "reconcile"})

            self.assertFalse(reconciled["confirmed"])
            self.assertFalse(reconciled["retry_allowed"])
            self.assertEqual(store.read()["pending_checkout"]["status"], "uncertain")
            with self.assertRaisesRegex(HouseholdError, "no fresh checkout confirmation"):
                app.handle({
                    "operation": "checkout",
                    "action": "confirm",
                    "confirmation_id": prepared["confirmation_id"],
                })
            with self.assertRaisesRegex(HouseholdError, "reconcile the pending checkout"):
                app.handle({"operation": "checkout", "action": "prepare"})
            self.assertEqual(provider.checkout_clicks, 1)

    def test_meny_read_rechecks_pending_vipps_after_waiting_for_browser_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            app = Application(store, provider, self.browser)
            entered = threading.Event()
            release = threading.Event()

            @contextmanager
            def delayed_browser_operation(_deadline=None):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test browser release timed out")
                yield

            app._browser_operation = delayed_browser_operation
            errors = []

            def read_catalog():
                try:
                    app.handle({"operation": "catalog", "action": "products", "query": "brokkoli"})
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=read_catalog)
            thread.start()
            self.assertTrue(entered.wait(1))
            with store.locked() as state:
                state["pending_checkout"] = {"status": "awaiting_user_payment"}
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertRegex(str(errors[0]), "reconcile the pending MENY checkout")
            self.assertEqual(provider.calls, [])

    def test_concurrent_meny_change_begin_reserves_only_one_browser_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.orders.append({
                "orderNumber": "99990001", "order_number": "99990001", "id": "99990001", "status": "confirmed",
                "grossAmount": 1200.0, "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00",
                "productQuantityCount": 1, "products": [{"identity": "Brokkoli 400g", "quantity": 1}],
            })
            provider.change_entered = threading.Event()
            provider.change_release = threading.Event()
            app = Application(store, provider, self.browser)
            first = {}

            def begin():
                first.update(app.handle({"operation": "orders", "action": "change_begin", "order_id": "99990001"}))

            thread = threading.Thread(target=begin)
            thread.start()
            self.assertTrue(provider.change_entered.wait(1))
            with self.assertRaisesRegex(HouseholdError, "another order change is active"):
                app.handle({"operation": "orders", "action": "change_begin", "order_id": "99990001"})
            provider.change_release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(provider.change_begins, 1)
            self.assertEqual(first["code"], "TEST-CODE-1")
            self.assertEqual(store.read()["order_change"]["status"], "editing")

    def test_meny_change_begin_uses_one_deadline_from_before_target_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.orders.append({
                "orderNumber": "99990001", "order_number": "99990001", "id": "99990001", "status": "confirmed",
                "grossAmount": 1200.0, "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00",
                "productQuantityCount": 1, "products": [{"identity": "Brokkoli 400g", "quantity": 1}],
            })
            original_call = provider.call
            provider.call = mock.Mock(wraps=original_call)
            provider.begin_order_change = mock.Mock(wraps=provider.begin_order_change)
            app = Application(store, provider, self.browser)
            with mock.patch("service.time.monotonic", return_value=10.0):
                app.handle({"operation": "orders", "action": "change_begin", "order_id": "99990001"})
            protected_calls = [call for call in provider.call.call_args_list if call.args[0] in {"get_order", "order_tracking"}]
            self.assertEqual(len(protected_calls), 2)
            self.assertTrue(all(call.kwargs.get("deadline") == 190.0 for call in protected_calls))
            self.assertEqual(provider.begin_order_change.call_args.kwargs["deadline"], 190.0)

    def test_meny_cancellation_propagates_each_absolute_deadline_to_all_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.orders.append({
                "orderNumber": "99990001", "order_number": "99990001", "id": "99990001", "status": "confirmed",
                "grossAmount": 1200.0, "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00",
                "productQuantityCount": 1, "products": [{"identity": "Brokkoli 400g", "quantity": 1}],
            })
            provider.call = mock.Mock(wraps=provider.call)
            app = Application(store, provider, self.browser)
            with mock.patch("service.time.monotonic", return_value=10.0):
                prepared = app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "99990001"})
            prepare_calls = provider.call.call_args_list[:]
            self.assertEqual([call.args[0] for call in prepare_calls], ["get_order", "order_tracking"])
            self.assertTrue(all(call.kwargs.get("deadline") == 115.0 for call in prepare_calls))
            with mock.patch("service.time.monotonic", return_value=20.0):
                result = app.handle({
                    "operation": "orders", "action": "cancel_confirm", "order_id": "99990001",
                    "confirmation_id": prepared["confirmation_id"],
                })
            self.assertTrue(result["cancelled"])
            confirm_calls = provider.call.call_args_list[len(prepare_calls):]
            self.assertEqual([call.args[0] for call in confirm_calls], ["get_order", "order_tracking", "order_tracking"])
            self.assertTrue(all(call.kwargs.get("deadline") == 125.0 for call in confirm_calls))
            self.assertEqual(provider.cancellation_review_deadlines, [115.0])
            self.assertEqual(provider.cancellation_submit_deadlines, [125.0])

    def test_cancel_prepare_rechecks_a_racing_order_change_inside_browser_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.orders.append({
                "orderNumber": "99990001", "order_number": "99990001", "id": "99990001", "status": "confirmed",
                "grossAmount": 1200.0, "deliverySlotDisplay": "torsdag 3. sep. kl. 09:00-12:00",
                "productQuantityCount": 1, "products": [{"identity": "Brokkoli 400g", "quantity": 1}],
            })
            original_call = provider.call

            def racing_call(tool, arguments, **kwargs):
                result = original_call(tool, arguments, **kwargs)
                if tool == "order_tracking":
                    with store.locked() as state:
                        state["order_change"] = {"provider": "meny", "order_id": "99990001", "status": "starting"}
                return result

            provider.call = racing_call
            app = Application(store, provider, self.browser)
            with self.assertRaisesRegex(HouseholdError, "active order change"):
                app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "99990001"})
            self.assertEqual(provider.cancellation_review_deadlines, [])
            self.assertIsNone(store.read()["pending_cancellation"])

    def test_checkout_rejects_a_starting_order_change_before_dereferencing_it(self):
        with self.store.locked() as state:
            state["order_change"] = {
                "provider": "oda", "order_id": "test-oda-order", "status": "starting",
                "token": "test", "started_at": datetime.now(timezone.utc).isoformat(),
            }
        with self.assertRaisesRegex(HouseholdError, "not ready for checkout"):
            self.app.handle({"operation": "checkout", "action": "prepare"})
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_stale_starting_order_change_can_be_recovered_without_a_provider_click(self):
        with self.store.locked() as state:
            state["order_change"] = {
                "provider": "oda", "order_id": "test-oda-order", "status": "starting",
                "token": "test", "started_at": (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(),
            }
        result = self.app.handle({"operation": "orders", "action": "change_abort", "order_id": "test-oda-order"})
        self.assertTrue(result["recovered"])
        self.assertIsNone(self.store.read()["order_change"])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_change_abort_rechecks_state_after_waiting_for_the_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeMeny()
            provider.abort_order_change = mock.Mock(return_value={"aborted": True})
            app = Application(store, provider, self.browser)
            original = {
                "provider": "meny", "order_id": "99990001", "status": "editing", "code": "TEST-CODE-1",
                "started_at": datetime.now(timezone.utc).isoformat(), "kind": "addition", "before": {},
            }
            with store.locked() as state:
                state["order_change"] = deepcopy(original)
            entered = threading.Event()
            release = threading.Event()

            @contextmanager
            def delayed_browser_operation(_deadline=None):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test browser release timed out")
                yield

            app._browser_operation = delayed_browser_operation
            errors = []

            def abort():
                try:
                    app.handle({"operation": "orders", "action": "change_abort", "order_id": "99990001"})
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=abort)
            thread.start()
            self.assertTrue(entered.wait(1))
            with store.locked() as state:
                state["order_change"]["kind"] = "full_order"
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertRegex(str(errors[0]), "state changed before aborting")
            provider.abort_order_change.assert_not_called()
            self.assertEqual(store.read()["order_change"]["kind"], "full_order")

    def test_saved_items_must_match_the_configured_provider(self):
        with self.assertRaisesRegex(HouseholdError, "configured Oda provider"):
            self.app.handle({"operation": "favorites", "action": "add", "item": {"product_id": MENY_PRODUCT, "product_name": "Brokkoli", "quantity": 1}})
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            app = Application(store, FakeOda(), self.browser)
            added = app.handle({"operation": "favorites", "action": "add", "item": {"product_id": MENY_PRODUCT, "product_name": "Brokkoli", "quantity": 1}})
            self.assertEqual(added["favorites"][0]["product_id"], MENY_PRODUCT)
            with self.assertRaisesRegex(HouseholdError, "MENY product_id"):
                app.handle({"operation": "recurring", "action": "add", "item": {"product_id": "10", "product_name": "Pasta", "quantity": 1}})

    def test_incompatible_saved_item_is_not_silently_listed(self):
        with self.store.locked() as state:
            state["favorites"] = [{"product_id": MENY_PRODUCT, "product_name": "Brokkoli", "quantity": 1}]
        with self.assertRaisesRegex(HouseholdError, "configured Oda provider"):
            self.app.handle({"operation": "favorites", "action": "list"})

    def test_expired_service_cart_deadline_dispatches_no_provider_call(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeOda()
            app = Application(store, provider, self.browser)
            provider.calls.clear()
            with mock.patch("service.time.monotonic", side_effect=[0, 241]):
                with self.assertRaisesRegex(HouseholdError, "deadline reached"):
                    app.handle({"operation": "cart", "action": "change", "operations": [{"product_id": MENY_PRODUCT, "quantity": 1}]})
            self.assertEqual(provider.calls, [])

    def test_meny_status_tracks_login_expiry_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeOda()
            app = Application(store, provider, self.browser)
            provider.call = mock.Mock(side_effect=[
                HouseholdError("MENY login is required in the configured browser profile"),
                {"provider": "meny", "products": []},
            ])
            with self.assertRaisesRegex(HouseholdError, "login is required"):
                app.handle({"operation": "catalog", "action": "products", "query": "brokkoli"})
            self.assertEqual(app.integration["status"], "awaiting_login")
            app.handle({"operation": "catalog", "action": "products", "query": "brokkoli"})
            self.assertEqual(app.integration["status"], "ready")

    def test_meny_status_reprobes_after_interactive_login(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeOda()
            provider.probe = mock.Mock(side_effect=[
                HouseholdError("MENY login is required in the configured browser profile"),
                {"protocol_version": "browser-v1", "server": {"name": "MENY website"}, "tool_count": 11},
            ])
            app = Application(store, provider, self.browser)
            self.assertEqual(app.integration["status"], "awaiting_login")
            with mock.patch("service.time.monotonic", return_value=100.0):
                status = app.handle({"operation": "status"})
            self.assertEqual(status["integration"]["status"], "ready")
            self.assertEqual(provider.probe.call_args_list, [mock.call(), mock.call(deadline=210.0, allow_recovery=True)])

    def test_meny_status_does_not_navigate_during_unresolved_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeOda()
            provider.probe = mock.Mock(side_effect=HouseholdError("MENY login is required in the configured browser profile"))
            app = Application(store, provider, self.browser)
            with store.locked() as state:
                state["pending_checkout"] = {"status": "awaiting_user_payment"}
            status = app.handle({"operation": "status"})
            self.assertEqual(status["integration"]["status"], "awaiting_login")
            provider.probe.assert_called_once_with()

    def test_wrapped_post_click_login_loss_updates_meny_status(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StateStore(Path(temp), {**CONFIG, "provider": "meny"})
            provider = FakeOda()
            app = Application(store, provider, self.browser)

            def uncertain(*_args, **_kwargs):
                try:
                    raise HouseholdError("MENY login is required in the configured browser profile")
                except HouseholdError as exc:
                    raise HouseholdError("MENY cart change is uncertain; read the cart and do not retry this request") from exc

            provider.call = uncertain
            with self.assertRaisesRegex(HouseholdError, "uncertain.*do not retry"):
                app.handle({"operation": "cart", "action": "get"})
            self.assertEqual(app.integration["status"], "awaiting_login")

    def test_oda_adds_to_one_exact_existing_order_without_creating_another(self):
        self.oda.orders = [{
            "orderNumber": "test-oda-order",
            "grossAmount": 100.0,
            "deliveryDate": "2026-09-05",
            "deliverySlotDisplay": "Lør 5. sep 09:00 - 12:00",
            "products": [{"product": {"id": 10, "name": "Pasta"}, "quantity": 1, "totalGrossAmount": "100.00"}],
        }]
        self.oda.cart = {"items": [], "count": 0, "subtotal": 0.0, "delivery": None}
        started = self.app.handle({"operation": "orders", "action": "change_begin", "order_id": "test-oda-order"})
        self.assertEqual(started["order_id"], "test-oda-order")
        self.oda.cart = {
            "items": [{"product_id": 20, "name": "Såpe", "quantity": 1, "price": 25.0}],
            "count": 1,
            "subtotal": 25.0,
            "delivery": {"display": "Lør 5. sep 09:00 - 12:00"},
            "deliveryAddress": "Eksempelveien 1",
        }
        self.app.handle({"operation": "cart", "action": "change", "operations": [{"productId": 20, "quantity": 1}]})
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        self.assertEqual(prepared["order_change"], {"order_id": "test-oda-order", "kind": "addition"})
        result = self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["changed_existing_order"])
        self.assertEqual(len(self.oda.orders), 1)
        self.assertEqual(self.oda.orders[0]["grossAmount"], 125.0)
        self.assertIsNone(self.store.read()["order_change"])

    def test_oda_delivery_change_is_staged_and_reconciled_on_the_same_order(self):
        self.oda.orders = [{
            "orderNumber": "test-oda-order",
            "grossAmount": 100.0,
            "deliveryDate": "2026-09-05",
            "deliverySlotDisplay": "Lør 5. sep 09:00 - 12:00",
            "products": [{"product": {"id": 10, "name": "Pasta"}, "quantity": 1, "totalGrossAmount": "100.00"}],
        }]
        self.oda.cart = {"items": [], "count": 0, "subtotal": 0.0, "delivery": None}
        self.app.handle({"operation": "orders", "action": "change_begin", "order_id": "test-oda-order"})
        selected = self.app.handle({"operation": "delivery", "action": "select", "slot_id": 77})
        self.assertEqual(selected["staged_for_order"], "test-oda-order")
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        self.assertEqual(prepared["order_change"], {"order_id": "test-oda-order", "kind": "delivery"})
        result = self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.oda.orders[0]["deliverySlotDisplay"], "Lør 12. sep 09:00 - 12:00")
        self.assertEqual(len(self.oda.orders), 1)

    def test_oda_delivery_confirmation_is_rejected_after_a_newer_slot_selection(self):
        self.oda.orders = [{
            "orderNumber": "test-oda-order", "grossAmount": 100.0, "deliveryDate": "2026-09-05",
            "deliverySlotDisplay": "Lør 5. sep 09:00 - 12:00",
            "products": [{"product": {"id": 10, "name": "Pasta"}, "quantity": 1, "totalGrossAmount": "100.00"}],
        }]
        self.oda.cart = {"items": [], "count": 0, "subtotal": 0.0, "delivery": None}
        self.app.handle({"operation": "orders", "action": "change_begin", "order_id": "test-oda-order"})
        self.app.handle({"operation": "delivery", "action": "select", "slot_id": 77})
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        with self.store.locked() as state:
            state["order_change"]["requested_delivery"] = {"slot_id": 88, "display": "Lør 19. sep 09:00 - 12:00"}
        with self.assertRaisesRegex(HouseholdError, "order change changed"):
            self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_manual_checkout_has_one_prepare_and_one_confirm(self):
        with mock.patch("service.time.monotonic", return_value=10.0):
            prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        self.assertEqual(prepared["summary"]["total"], 35.0)
        self.assertEqual(prepared["summary"]["delivery"]["address"], "Eksempelveien 1")
        self.assertEqual(prepared["summary"]["payment"], "•••• 1234")
        self.assertEqual(self.browser.review_deadlines, [250.0])
        with mock.patch("service.time.monotonic", return_value=20.0):
            result = self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(self.browser.submit_deadlines, [260.0])
        self.assertEqual(self.browser.checkout_clicks, 1)
        with self.assertRaises(HouseholdError):
            self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertEqual(self.browser.checkout_clicks, 1)

    def test_oda_checkout_requires_a_delivery_address_before_browser_review(self):
        self.oda.cart["deliveryAddress"] = " \t "
        with self.assertRaisesRegex(HouseholdError, "select a delivery address"):
            self.app.handle({"operation": "checkout", "action": "prepare"})
        self.assertEqual(self.browser.review_deadlines, [])

    def test_checkout_reconcile_accepts_live_compact_delivery_hours(self):
        self.oda.cart["delivery"]["display"] = "Hjemlevering mellom kl 07 og 13, 3. sep"

        def live_shaped_submit(cart, review, before_click=None, *, deadline=None):
            if before_click:
                before_click()
            self.browser.checkout_clicks += 1
            self.oda.orders.append({
                "order_number": "new-order",
                "grossAmount": 35.0,
                "deliveryDate": "2026-09-03",
                "deliverySlotDisplay": "Tor 3. sep 07:00 - 13:00",
                "products": [{"product": {"id": 10, "name": "Fullkornspasta"}, "quantity": 1, "totalGrossAmount": "35.00"}],
            })

        self.browser.submit_checkout = live_shaped_submit
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        result = self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})

        self.assertTrue(result["confirmed"])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertIsNone(self.store.read()["pending_checkout"])

    def test_checkout_reconcile_rejects_ambiguous_compact_delivery_hours(self):
        self.oda.cart["delivery"]["display"] = "Hjemlevering mellom kl 07 og 13, 3. sep; alternativ 08:00"

        def live_shaped_submit(cart, review, before_click=None, *, deadline=None):
            if before_click:
                before_click()
            self.browser.checkout_clicks += 1
            self.oda.orders.append({
                "order_number": "new-order",
                "grossAmount": 35.0,
                "deliveryDate": "2026-09-03",
                "deliverySlotDisplay": "Tor 3. sep 07:00 - 13:00",
                "products": [{"product": {"id": 10, "name": "Fullkornspasta"}, "quantity": 1, "totalGrossAmount": "35.00"}],
            })

        self.browser.submit_checkout = live_shaped_submit
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        result = self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})

        self.assertFalse(result["confirmed"])
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(self.browser.checkout_clicks, 1)
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")

    def test_cart_change_requires_new_checkout_summary(self):
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        self.oda.cart["subtotal"] = 36.0
        with self.assertRaises(HouseholdError):
            self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_concurrent_checkout_confirm_reserves_one_click(self):
        entered = threading.Event()
        release = threading.Event()
        original_submit = self.browser.submit_checkout

        def blocked_submit(cart, review, before_click=None, *, deadline=None):
            if before_click:
                before_click()
            entered.set()
            if not release.wait(2):
                raise HouseholdError("test checkout timed out")
            original_submit(cart, review)

        self.browser.submit_checkout = blocked_submit
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        first = {}

        def confirm():
            first.update(self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]}))

        thread = threading.Thread(target=confirm)
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(HouseholdError, "no fresh checkout confirmation"):
            self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        with self.assertRaisesRegex(HouseholdError, "reconcile the pending checkout"):
            self.app.handle({"operation": "checkout", "action": "prepare"})
        reconcile_errors = []

        def reconcile():
            try:
                self.app.handle({"operation": "checkout", "action": "reconcile"})
            except Exception as exc:
                reconcile_errors.append(exc)

        reconcile_thread = threading.Thread(target=reconcile)
        reconcile_thread.start()
        reconcile_thread.join(0.05)
        self.assertTrue(reconcile_thread.is_alive())
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "clicking")
        release.set()
        thread.join(2)
        reconcile_thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(reconcile_thread.is_alive())
        self.assertTrue(first["confirmed"])
        self.assertEqual(len(reconcile_errors), 1)
        self.assertRegex(str(reconcile_errors[0]), "no checkout attempt is pending")
        self.assertEqual(self.browser.checkout_clicks, 1)

    def test_checkout_reconcile_rejects_unrelated_new_order(self):
        def unrelated_submit(cart, review, before_click=None, *, deadline=None):
            if before_click:
                before_click()
            self.browser.checkout_clicks += 1
            self.oda.orders.append({
                "order_number": "other-order",
                "grossAmount": 99.0,
                "deliveryDate": "2026-09-06",
                "deliverySlotDisplay": "Sunday 2026-09-06 10:00 - 13:00",
                "products": [{"product": {"id": 99, "name": "Other"}, "quantity": 1, "totalGrossAmount": "99.00"}],
            })

        self.browser.submit_checkout = unrelated_submit
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        result = self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "uncertain")

    def test_known_preclick_failure_requires_new_prepare(self):
        def stop_before_click(cart, review, before_click=None, *, deadline=None):
            raise CheckoutPreconditionError("checkout changed")

        self.browser.submit_checkout = stop_before_click
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        with self.assertRaises(CheckoutPreconditionError):
            self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertIsNone(self.store.read()["pending_checkout"])

    def test_checkout_expiration_is_rechecked_at_the_final_click(self):
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})
        started = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with self.store.locked() as state:
            state["pending_checkout"]["expires_at"] = (started + timedelta(seconds=1)).isoformat()
        with (
            mock.patch("service.now", side_effect=[started, started + timedelta(seconds=2)]),
            self.assertRaisesRegex(CheckoutPreconditionError, "expired before the final click"),
        ):
            self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})
        self.assertEqual(self.browser.checkout_clicks, 0)
        self.assertIsNone(self.store.read()["pending_checkout"])

    def test_checkout_lock_timeout_does_not_create_false_uncertain_state(self):
        prepared = self.app.handle({"operation": "checkout", "action": "prepare"})

        class BusyLock:
            def acquire(self, timeout=None):
                return False

            def release(self):
                raise AssertionError("unacquired lock released")

        self.app.browser_lock = BusyLock()
        with self.assertRaisesRegex(HouseholdError, "browser deadline reached"):
            self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": prepared["confirmation_id"]})

        self.assertEqual(self.store.read()["pending_checkout"]["status"], "awaiting_confirmation")
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_checkout_reconcile_requires_a_click_attempt(self):
        self.app.handle({"operation": "checkout", "action": "prepare"})
        with self.assertRaisesRegex(HouseholdError, "has not reached reconciliation"):
            self.app.handle({"operation": "checkout", "action": "reconcile"})
        self.assertEqual(self.store.read()["pending_checkout"]["status"], "awaiting_confirmation")

    def test_stale_checkout_confirmation_cannot_confirm_a_newer_prepare(self):
        first = self.app.handle({"operation": "checkout", "action": "prepare"})
        second = self.app.handle({"operation": "checkout", "action": "prepare"})

        self.assertNotEqual(first["confirmation_id"], second["confirmation_id"])
        with self.assertRaisesRegex(HouseholdError, "does not match the prepared summary"):
            self.app.handle({"operation": "checkout", "action": "confirm", "confirmation_id": first["confirmation_id"]})

        self.assertEqual(self.store.read()["pending_checkout"]["confirmation_id"], second["confirmation_id"])
        self.assertEqual(self.browser.checkout_clicks, 0)

    def test_cart_change_is_blocked_while_checkout_is_uncertain(self):
        self.app.handle({"operation": "checkout", "action": "prepare"})
        with self.store.locked() as state:
            state["pending_checkout"]["status"] = "uncertain"
        with self.assertRaisesRegex(HouseholdError, "before changing the cart"):
            self.app.handle({"operation": "cart", "action": "change", "operations": [{"productId": 10, "quantity": 2}]})

    def test_cancellation_stops_pending_email(self):
        with self.store.locked() as state:
            state["email_jobs"] = [{"order_id": "old", "delivery_date": "2026-09-05", "status": "pending", "sent_at": None}]
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "old"})
        result = self.app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "old", "confirmation_id": prepared["confirmation_id"]})
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.browser.cancel_clicks, 1)
        self.assertEqual(self.store.read()["email_jobs"][0]["status"], "cancelled")

    def test_stale_cancellation_confirmation_cannot_cancel_a_newer_prepare(self):
        first = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-a"})
        second = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "order-b"})

        self.assertNotEqual(first["confirmation_id"], second["confirmation_id"])
        with self.assertRaisesRegex(HouseholdError, "does not match the prepared order"):
            self.app.handle({
                "operation": "orders",
                "action": "cancel_confirm",
                "order_id": "order-a",
                "confirmation_id": first["confirmation_id"],
            })

        pending = self.store.read()["pending_cancellation"]
        self.assertEqual((pending["order_id"], pending["confirmation_id"]), ("order-b", second["confirmation_id"]))
        self.assertEqual(self.browser.cancel_clicks, 0)

    def test_cancellation_uses_an_internal_deadline_below_the_rpc_timeout(self):
        with mock.patch("service.time.monotonic", return_value=10.0):
            prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "old"})
        with mock.patch("service.time.monotonic", return_value=20.0):
            result = self.app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "old", "confirmation_id": prepared["confirmation_id"]})

        self.assertTrue(result["cancelled"])
        self.assertEqual(self.browser.cancellation_review_deadlines, [115.0])
        self.assertEqual(self.browser.cancellation_submit_deadlines, [125.0])

    def test_cancellation_pre_dispatch_failure_requires_a_new_prepare(self):
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "old"})

        def stop_before_click(order_id, order, review, before_click=None, *, deadline=None):
            raise CancellationPreconditionError("final control changed before dispatch")

        self.browser.submit_cancellation = stop_before_click
        with self.assertRaises(CancellationPreconditionError):
            self.app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "old", "confirmation_id": prepared["confirmation_id"]})

        self.assertIsNone(self.store.read()["pending_cancellation"])
        self.assertEqual(self.browser.cancel_clicks, 0)

    def test_cancellation_expiration_is_rechecked_at_the_final_click(self):
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "old"})
        started = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        with self.store.locked() as state:
            state["pending_cancellation"]["expires_at"] = (started + timedelta(seconds=1)).isoformat()
        with (
            mock.patch("service.now", side_effect=[started, started + timedelta(seconds=2)]),
            self.assertRaisesRegex(CancellationPreconditionError, "expired before the final click"),
        ):
            self.app.handle({
                "operation": "orders",
                "action": "cancel_confirm",
                "order_id": "old",
                "confirmation_id": prepared["confirmation_id"],
            })
        self.assertEqual(self.browser.cancel_clicks, 0)
        self.assertIsNone(self.store.read()["pending_cancellation"])

    def test_cancellation_holds_browser_until_post_click_tracking_reconciles(self):
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "old"})
        tracking_started = threading.Event()
        release_tracking = threading.Event()
        checkout_entered = threading.Event()
        original_call = self.oda.call
        original_review = self.browser.review_checkout

        def blocked_tracking(tool, arguments):
            if tool == "order_tracking" and self.browser.cancel_clicks == 1:
                tracking_started.set()
                release_tracking.wait(1)
            return original_call(tool, arguments)

        def observed_review(cart, *, deadline=None):
            checkout_entered.set()
            return original_review(cart, deadline=deadline)

        self.oda.call = blocked_tracking
        self.browser.review_checkout = observed_review
        cancelled = {}
        checkout = {}
        cancel_thread = threading.Thread(target=lambda: cancelled.setdefault("result", self.app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "old", "confirmation_id": prepared["confirmation_id"]})))
        cancel_thread.start()
        self.assertTrue(tracking_started.wait(1))
        checkout_thread = threading.Thread(target=lambda: checkout.setdefault("result", self.app.handle({"operation": "checkout", "action": "prepare"})))
        checkout_thread.start()

        self.assertFalse(checkout_entered.wait(0.05))
        release_tracking.set()
        cancel_thread.join(1)
        checkout_thread.join(1)

        self.assertFalse(cancel_thread.is_alive())
        self.assertFalse(checkout_thread.is_alive())
        self.assertTrue(cancelled["result"]["cancelled"])
        self.assertTrue(checkout_entered.is_set())
        self.assertEqual(checkout["result"]["summary"]["total"], 35.0)

    def test_cancellation_reconcile_serializes_with_active_confirm(self):
        prepared = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "old"})
        tracking_started = threading.Event()
        release_tracking = threading.Event()
        reconcile_finished = threading.Event()
        original_call = self.oda.call

        def blocked_tracking(tool, arguments):
            if tool == "order_tracking" and self.browser.cancel_clicks == 1:
                tracking_started.set()
                release_tracking.wait(1)
            return original_call(tool, arguments)

        self.oda.call = blocked_tracking
        confirmed = {}
        reconciled = {}

        def confirm():
            confirmed["result"] = self.app.handle({"operation": "orders", "action": "cancel_confirm", "order_id": "old", "confirmation_id": prepared["confirmation_id"]})

        def reconcile():
            try:
                reconciled["result"] = self.app.handle({"operation": "orders", "action": "cancel_reconcile"})
            except HouseholdError as exc:
                reconciled["error"] = str(exc)
            finally:
                reconcile_finished.set()

        confirm_thread = threading.Thread(target=confirm)
        confirm_thread.start()
        self.assertTrue(tracking_started.wait(1))
        reconcile_thread = threading.Thread(target=reconcile)
        reconcile_thread.start()

        self.assertFalse(reconcile_finished.wait(0.05))
        release_tracking.set()
        confirm_thread.join(1)
        reconcile_thread.join(1)

        self.assertFalse(confirm_thread.is_alive())
        self.assertFalse(reconcile_thread.is_alive())
        self.assertTrue(confirmed["result"]["cancelled"])
        self.assertIn("no order cancellation is pending", reconciled["error"])

    def test_unavailable_cancellation_stops_without_click(self):
        self.browser.cancellation_available = False
        result = self.app.handle({"operation": "orders", "action": "cancel_prepare", "order_id": "old"})
        self.assertFalse(result["available"])
        self.assertEqual(self.browser.cancel_clicks, 0)

    def test_email_is_single_and_moves_or_stops_after_fresh_order_read(self):
        with self.store.locked() as state:
            state["email_recipient"] = "owner@example.test"
            state["menu"] = {"order_id": "old", "week": "2026-W36", "dishes": [{"name": "A", "ingredients": ["x"], "steps": ["y"]}]}
        delivery = date.today().isoformat()
        self.app.handle({"operation": "email", "action": "schedule", "order_id": "old", "delivery_date": delivery})
        self.app.handle({"operation": "email", "action": "schedule", "order_id": "old", "delivery_date": delivery})
        self.assertEqual(len(self.store.read()["email_jobs"]), 1)
        self.oda.order_delivery = delivery
        due = self.app.handle({"operation": "email", "action": "due", "order_id": "old"})
        self.assertTrue(due["send"])
        self.assertTrue(due["mark_sent_after_success"])
        self.assertIn("Ukesmeny og oppskrifter", due["subject"])
        self.assertIn("<h2>A</h2>", due["html"])
        self.assertEqual(due["automation_environment"], {"HERMES_WORKSPACE_AUTOMATION_PROFILE": "test-email"})
        self.assertEqual(self.store.read()["email_jobs"][0]["status"], "pending")
        moved = (date.today() + timedelta(days=1)).isoformat()
        self.oda.order_delivery = moved
        result = self.app.handle({"operation": "email", "action": "due", "order_id": "old"})
        self.assertEqual(result, {"send": False, "reason": "delivery moved", "delivery_date": moved})
        self.oda.tracking = "cancelled"
        result = self.app.handle({"operation": "email", "action": "due", "order_id": "old"})
        self.assertEqual(result, {"send": False, "reason": "order cancelled"})

    def test_test_email_returns_escaped_html_without_consuming_job(self):
        menu = {
            "order_id": "old",
            "week": "2026-W36",
            "schedule": [{"day": "Mandag", "meal": "Fisk & grønt", "portions": 4}],
            "dishes": [{
                "name": "Fisk <middag>",
                "portions": 4,
                "ingredients": [{"amount": "500 g", "item": "fisk & sitron"}],
                "steps": ["Stek <forsiktig>"],
                "storage": "Kjølig",
            }],
            "salads": [{"name": "Salat", "portions": 4, "ingredients": ["grønt"], "steps": ["Bland"]}],
        }
        with self.store.locked() as state:
            state["email_recipient"] = "owner@example.test"
            state["menu"] = menu
            state["email_jobs"] = [{"order_id": "old", "delivery_date": "2026-09-05", "status": "pending", "sent_at": None}]

        before = deepcopy(self.store.read()["email_jobs"])
        result = self.app.handle({"operation": "email", "action": "test", "order_id": "old"})

        self.assertTrue(result["send"])
        self.assertTrue(result["test"])
        self.assertFalse(result["mark_sent_after_success"])
        self.assertTrue(result["subject"].startswith("TEST – "))
        self.assertIn("Denne testmailen endrer ikke den planlagte utsendingen", result["html"])
        self.assertIn("Fisk &lt;middag&gt;", result["html"])
        self.assertIn("fisk &amp; sitron", result["html"])
        self.assertNotIn("Fisk <middag>", result["html"])
        self.assertEqual(result["automation_environment"], {"HERMES_WORKSPACE_AUTOMATION_PROFILE": "test-email"})
        self.assertEqual(self.store.read()["email_jobs"], before)

    def test_due_requires_exact_menu_and_one_job_before_send(self):
        delivery = date.today().isoformat()
        with self.store.locked() as state:
            state["email_recipient"] = "owner@example.test"
            state["menu"] = {"order_id": "other", "week": "2026-W36", "dishes": []}
            state["email_jobs"] = [{"order_id": "old", "delivery_date": delivery, "status": "pending", "sent_at": None}]
        self.oda.order_delivery = delivery
        before = deepcopy(self.store.read()["email_jobs"])

        wrong_menu = self.app.handle({"operation": "email", "action": "due", "order_id": "old"})
        self.assertFalse(wrong_menu["send"])
        self.assertEqual(self.store.read()["email_jobs"], before)

        with self.store.locked() as state:
            state["menu"] = None
        cleared_menu = self.app.handle({"operation": "email", "action": "due", "order_id": "old"})
        self.assertFalse(cleared_menu["send"])
        self.assertEqual(self.store.read()["email_jobs"], before)

        with self.store.locked() as state:
            state["menu"] = {"order_id": "old", "week": "2026-W36", "dishes": []}
            state["email_jobs"].append(deepcopy(state["email_jobs"][0]))
        duplicate = deepcopy(self.store.read()["email_jobs"])
        duplicate_jobs = self.app.handle({"operation": "email", "action": "due", "order_id": "old"})
        self.assertFalse(duplicate_jobs["send"])
        self.assertEqual(self.store.read()["email_jobs"], duplicate)

    def test_due_stays_pending_until_separate_mark_sent(self):
        delivery = date.today().isoformat()
        with self.store.locked() as state:
            state["email_recipient"] = "owner@example.test"
            state["menu"] = {"order_id": "old", "week": "2026-W36", "dishes": [{"name": "A", "ingredients": ["x"], "steps": ["y"]}]}
            state["email_jobs"] = [{"order_id": "old", "delivery_date": delivery, "status": "pending", "sent_at": None}]
        self.oda.order_delivery = delivery

        due = self.app.handle({"operation": "email", "action": "due", "order_id": "old"})
        self.assertTrue(due["send"])
        self.assertTrue(due["mark_sent_after_success"])
        self.assertEqual(self.store.read()["email_jobs"][0]["status"], "pending")

        marked = self.app.handle({"operation": "email", "action": "mark_sent", "order_id": "old"})
        self.assertTrue(marked["sent"])
        self.assertEqual(self.store.read()["email_jobs"][0]["status"], "sent")
        self.assertIsNotNone(self.store.read()["email_jobs"][0]["sent_at"])

    def test_menu_rejects_non_iso_week_before_email_subject(self):
        with self.assertRaisesRegex(HouseholdError, "valid ISO week"):
            self.app.handle({
                "operation": "menu",
                "action": "save",
                "menu": {"week": "2026-W36\r\nBcc: x@example.test", "dishes": [{"name": "A", "ingredients": ["x"], "steps": ["y"]}]},
            })

    def test_menu_email_html_omits_test_banner_for_due_mail(self):
        value = menu_email_html({"week": "2026-W36", "dishes": [], "salads": []})
        self.assertIn("Ukesmeny og oppskrifter", value)
        self.assertNotIn("Denne testmailen endrer ikke", value)

    def test_auto_checkout_defaults_off_and_occurrence_is_single_use(self):
        with self.assertRaises(HouseholdError):
            self.app.handle({"operation": "checkout", "action": "auto", "occurrence": "2026-W36"})
        self.app.handle({"operation": "schedule", "action": "update", "changes": {"enabled": True, "maximum_total": 100.0, "delivery": {"weekday": "Saturday"}, "auto_checkout": True}})
        with (
            mock.patch("service.now", return_value=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)),
            self.assertRaisesRegex(HouseholdError, "not linked"),
        ):
            self.app.handle({"operation": "checkout", "action": "auto", "occurrence": "2026-W36"})
        self.app.handle({"operation": "schedule", "action": "set_cron_job", "cron_job_id": "test-cron"})
        with (
            mock.patch("service.now", return_value=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)),
            self.assertRaisesRegex(HouseholdError, "does not match"),
        ):
            self.app.handle({"operation": "checkout", "action": "auto", "occurrence": "invented"})
        with (
            mock.patch("service.now", return_value=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)),
            mock.patch("service.time.monotonic", return_value=10.0),
        ):
            result = self.app.handle({"operation": "checkout", "action": "auto", "occurrence": "2026-W36"})
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["awaiting_confirmation"])
        self.assertEqual(self.browser.review_deadlines[-1], 250.0)
        self.assertEqual(self.browser.submit_deadlines, [])
        with (
            mock.patch("service.now", return_value=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)),
            self.assertRaises(HouseholdError),
        ):
            self.app.handle({"operation": "checkout", "action": "auto", "occurrence": "2026-W36"})
        self.assertEqual(self.browser.checkout_clicks, 0)
        self.assertEqual(self.store.read()["occurrences"]["2026-W36"]["status"], "awaiting_confirmation")

    def test_auto_prepare_failure_needs_input_instead_of_staying_started(self):
        self.app.handle({"operation": "schedule", "action": "update", "changes": {"enabled": True, "maximum_total": 100.0, "delivery": {"weekday": "Saturday"}, "auto_checkout": True}})
        self.app.handle({"operation": "schedule", "action": "set_cron_job", "cron_job_id": "test-cron"})

        def fail_review(_cart, *, deadline=None):
            raise HouseholdError("browser deadline")

        self.browser.review_checkout = fail_review

        with (
            mock.patch("service.now", return_value=datetime(2026, 9, 10, 13, 5, tzinfo=timezone.utc)),
            self.assertRaisesRegex(HouseholdError, "browser deadline"),
        ):
            self.app.handle({"operation": "checkout", "action": "auto", "occurrence": "2026-W37"})

        self.assertEqual(self.store.read()["occurrences"]["2026-W37"]["status"], "needs_input")

if __name__ == "__main__":
    unittest.main()
