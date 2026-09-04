"""Complete household workflows and regressions from the independent review."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import HouseholdError, StateStore, cart_summary
from service import Application
from tests.test_meal_concierge import CONFIG, FakeBrowser, MutableFakeOda
from tests import test_meal_concierge as flow_fixture
from tests.test_meal_concierge_recipes import full_recipe, menu


class ReviewAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(Path(self.temp.name), {**CONFIG, "provider": "oda"})
        self.provider = MutableFakeOda()
        self.browser = FakeBrowser()
        self.browser.oda = self.provider
        self.app = Application(self.store, self.provider, self.browser)
        self.app.handle({"operation": "setup", "action": "apply", "keep_current": True})

    def save_menu(self, name="Dinner", **extra):
        return self.app.handle({"operation": "menu", "action": "save", "menu": menu("2026-W40", full_recipe(name)), **extra})["menu"]

    def test_old_menu_requirements_never_mutate_new_menu_cart(self):
        old = self.save_menu()
        old_ref = self.app._cart_menu_ref(old)
        self.save_menu("Replacement", menu_id=old["menu_id"], expected_revision=old["revision"])
        before = deepcopy(self.provider.cart)
        with self.assertRaisesRegex(HouseholdError, "stale"):
            self.app.handle({"operation": "cart", "action": "sync", "menu_ref": old_ref,
                             "requirements": [{"product_id": "10", "product_name": "Old dinner product", "quantity": 2}]})
        self.assertEqual(before, self.provider.cart)

    def test_missing_menu_reference_rejected_before_provider_dispatch(self):
        self.save_menu()
        calls = len(self.provider.calls)
        with self.assertRaisesRegex(HouseholdError, "exact menu_ref"):
            self.app.handle({"operation": "cart", "action": "sync", "requirements": [{"product_id": "10", "product_name": "Pasta", "quantity": 2}]})
        self.assertEqual(calls, len(self.provider.calls))

    def test_profile_and_recurring_invalid_writes_are_atomic(self):
        self.store.update_profile({"diet": {"leafy_green_days": [1, 4]}})
        before = self.store.read()
        for change in ({"meals": {"portions": 0}}, {"meals": {"people": "two"}}, {"diet": {"avoid": "fish"}}, {"diet": {"leafy_green_days": [0, 8]}}):
            with self.subTest(change=change), self.assertRaises(HouseholdError):
                self.app.handle({"operation": "profile", "action": "update", "changes": change})
            self.assertEqual(before, self.store.read())
        with self.assertRaises(HouseholdError):
            self.app.handle({"operation": "recurring", "action": "add", "item": {
                "product_id": "10", "product_name": "Milk", "quantity": 1,
                "schedule": {"every": 0, "unit": "weeks"}}})
        self.assertEqual(before, self.store.read())

    def test_recurring_intervals_keep_anchor_across_restart(self):
        with mock.patch("service.now", return_value=datetime(2026, 9, 1, tzinfo=timezone.utc)):
            for product, unit in (("10", "weeks"), ("20", "months")):
                self.app.handle({"operation": "recurring", "action": "add", "item": {
                    "product_id": product, "product_name": "Fixed item", "quantity": 1,
                    "schedule": {"every": 2, "unit": unit}}})
        reopened = Application(StateStore(Path(self.temp.name), {**CONFIG, "provider": "oda"}), self.provider, self.browser)
        for when, expected in (("2026-09-01", {"10", "20"}), ("2026-09-08", {"20"}), ("2026-09-15", {"10", "20"}), ("2026-10-01", {"10"}), ("2026-11-01", {"10", "20"})):
            due = reopened.handle({"operation": "recurring", "action": "due", "date": when})["due"]
            self.assertEqual(expected, {item["product_id"] for item in due}, when)

    def test_recipe_reference_defaults_to_household_portions(self):
        saved = self.app.handle({"operation": "recipes", "action": "save", "recipe": full_recipe(), "idempotency_key": "portion-recipe"})["recipe"]
        result = self.app.handle({"operation": "menu", "action": "save", "menu": menu("2026-W40", {"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}})})["menu"]
        self.assertEqual(2, result["dishes"][0]["portions"])
        self.assertEqual(200, result["dishes"][0]["shopping_requirements"][0]["quantity"])

    def test_cart_ready_continues_to_confirmed_order_after_restart(self):
        case = flow_fixture.FlowTests("test_cart_ready_occurrence_must_be_carried_into_manual_checkout")
        case.setUp()
        self.addCleanup(case.tearDown)
        case.test_cart_ready_occurrence_must_be_carried_into_manual_checkout()
        pending = case.store.read()["pending_checkout"]
        reopened = Application(StateStore(case.store.directory, case.store.config), case.oda, case.browser)
        with mock.patch("service.now", return_value=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)):
            result = reopened.handle({"operation": "checkout", "action": "confirm", "confirmation_id": pending["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(1, case.browser.checkout_clicks)

    def test_waiting_meny_discovery_rechecks_pending_checkout(self):
        self.app.provider = "meny"
        acquired = threading.Event()
        errors = []
        def discover():
            acquired.set()
            try:
                self.app._provider_recipe_candidates("meny", "soup", 1)
            except HouseholdError as exc:
                errors.append(str(exc))
        self.app.browser_lock.acquire()
        worker = threading.Thread(target=discover)
        worker.start()
        self.assertTrue(acquired.wait(1))
        with self.store.locked() as state:
            state["pending_checkout"] = {"status": "awaiting_user_payment"}
        self.app.browser_lock.release()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(errors))
        self.assertFalse(any(tool == "recipe_search" for tool, _ in self.provider.calls))

    def test_upgrade_preserves_manual_cart_ready_confirmation(self):
        case = flow_fixture.FlowTests("test_cart_ready_occurrence_must_be_carried_into_manual_checkout")
        case.setUp()
        self.addCleanup(case.tearDown)
        case.test_cart_ready_occurrence_must_be_carried_into_manual_checkout()
        import json
        state = case.store.read()
        state["version"] = 11
        state["pending_checkout"].pop("automatic_checkout")
        case.store.path.write_text(json.dumps(state))
        reopened = Application(StateStore(case.store.directory, case.store.config), case.oda, case.browser)
        with mock.patch("service.now", return_value=datetime(2026, 9, 3, 13, 5, tzinfo=timezone.utc)):
            result = reopened.handle({"operation": "checkout", "action": "confirm", "confirmation_id": state["pending_checkout"]["confirmation_id"]})
        self.assertTrue(result["confirmed"])
        self.assertEqual(case.browser.checkout_clicks, 1)
        self.assertTrue((case.store.directory / "state-v11.backup.json").exists())

    def test_pantry_aggregation_keeps_gross_and_exact_net_quantities(self):
        from product_planner import menu_requirements
        from tests import test_meal_concierge_products as products
        value = products.menu({"item": "Flour", "quantity": 0.5, "unit": "kg"}, {"item": "Flour", "quantity": 300, "unit": "g"})
        source = lambda index: {"collection": "dishes", "recipe_index": 0, "ingredient_index": index}
        requirements, unresolved = menu_requirements(value, ingredient_decisions=[
            {"source": source(0), "action": "have_all"},
            {"source": source(1), "action": "have_quantity", "quantity": 0.1, "unit": "kg"},
        ])
        self.assertEqual(unresolved, [])
        self.assertEqual(requirements[0]["quantity"], {"numerator": 200, "denominator": 1})
        self.assertEqual(requirements[0]["gross_quantity"], {"numerator": 800, "denominator": 1})
        self.assertEqual(requirements[0]["confirmed_pantry_quantity"], {"numerator": 600, "denominator": 1})
        with self.assertRaisesRegex(HouseholdError, "compatible unit"):
            menu_requirements(value, ingredient_decisions=[{"source": source(0), "action": "have_quantity", "quantity": 1, "unit": "dl"}])

    def test_unknown_deposit_budget_uses_all_known_minimum_costs(self):
        from tests import test_meal_concierge_products as products
        value = products.menu({"item": "A", "quantity": 1, "unit": "stk"}, {"item": "B", "quantity": 1, "unit": "stk"})
        requirements, _ = products.menu_requirements(value)
        observations = {}
        approvals = []
        for index, requirement in enumerate(requirements):
            offer = products.option(100, deposit=100 if index == 0 else 0)
            if index == 1:
                offer["mandatory_deposit_ore"] = None
                offer.pop("total_payable_ore")
            ref = str(index + 1)
            observations[requirement["requirement_id"]] = products.observation(requirement["item"], [products.product(ref, requirement["item"], 1, "count", [offer])])
            approvals.append({"requirement_id": requirement["requirement_id"], "candidate_refs": [ref]})
        plan = products.build_product_plan(provider="oda", binding={}, menu=value, observations=observations, candidate_approvals=approvals, price_mode="estimate", budget_ore=250)
        self.assertEqual(plan["status"], "needs_input")
        self.assertEqual(plan["budget_status"], "exceeded")
        self.assertIsNone(plan["totals"]["total_payable_ore"])
        self.assertEqual(plan["unresolved_requirements"][-1]["known_minimum_ore"], 300)

    def test_cart_drift_and_new_requirements_invalidate_product_approval(self):
        from tests import test_meal_concierge_products as products
        case = products.ProductRuntimeTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        plan = case.prepare(approve=True)
        result = case.app.handle({"operation": "products", "action": "apply", "product_plan": plan, "product_plan_digest": plan["product_plan_digest"], "cart_change_requested": True})
        self.assertTrue(result["applied"])
        case.provider.cart["items"][0]["quantity"] = 2
        changed = case.app.handle({"operation": "cart", "action": "sync", "menu_ref": case.menu_ref, "requirements": [{"product_id": "10", "product_name": "Fixture Mel", "quantity": 3}]})
        self.assertFalse(changed["synced"])
        self.assertNotIn("product_plan_digest", case.store.read()["cart_plan"])

    def test_all_ingredients_at_home_finishes_without_provider_cart_write(self):
        from tests import test_meal_concierge_products as products
        case = products.ProductRuntimeTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        plan = case.app.handle({"operation": "products", "action": "prepare", "menu_ref": case.menu_ref,
            "ingredient_decisions": [{"source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 0}, "action": "have_all"}]})["product_plan"]
        self.assertEqual(plan["status"], "prepared")
        result = case.app.handle({"operation": "products", "action": "apply", "product_plan": plan, "product_plan_digest": plan["product_plan_digest"], "cart_change_requested": True})
        self.assertTrue(result["nothing_to_buy"])
        status = case.app.handle({"operation": "status"})
        self.assertEqual(status["workflow"]["next_action"]["operation"], "menu")
        self.assertFalse(any(name == "manipulate_cart" for name, _ in case.provider.calls))

    def test_cooking_mcp_wrapper_and_feedback_reach_runtime(self):
        import runpy
        import types
        sys.path.insert(0, str(Path(__file__).parent))
        import test_meal_concierge_replanning as replanning
        case = replanning.ReplanningTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        class ToolServer:
            def __init__(self, *args, **kwargs): pass
            def tool(self, **kwargs): return lambda function: function
        module = types.ModuleType("mcp.server.mcpserver")
        module.MCPServer = ToolServer
        with mock.patch.dict(sys.modules, {"mcp.server.mcpserver": module}):
            surface = runpy.run_path(str(Path(__file__).resolve().parents[1] / "mcp_server.py"))
        for name in ("meal_concierge_cooking", "meal_concierge_feedback"):
            surface[name].__globals__["rpc"] = lambda operation, **arguments: case.app.handle({"operation": operation, **arguments})
        slot = case.menu["slots"][0]
        cooked = surface["meal_concierge_cooking"](action="mark_cooked", menu_id=case.menu["menu_id"], expected_revision=case.menu["revision"], slot_id=slot["slot_id"], idempotency_key="cooking-reported")
        self.assertTrue(cooked)
        target = case.app.handle({"operation": "menu", "action": "get"})["feedback_targets"][0]
        experience = surface["meal_concierge_feedback"](action="experience", target=target, experience={"actual_active_minutes": 27, "portion_fit": "right", "leftover_portions": 1}, idempotency_key="experience-reported")
        self.assertEqual(experience["event"]["kind"], "experience")
        inspected = case.app.handle({"operation": "feedback", "action": "inspect"})
        self.assertEqual(inspected["cooking_experiences"][0]["experience"]["actual_active_minutes"], 27)
        prepared = case.prepare(planner_input={"candidates": case.candidates[3:]})
        successor = case.apply(prepared)["menu"]
        assessment = case.app.handle({"operation": "menu", "action": "assess"})["assessment"]
        self.assertTrue(assessment["ready"])
        self.assertEqual(assessment["dinner_days"], {"expected": 3, "verified": 3})

    def test_mealie_crash_recovery_cleanup_and_new_intent_use_real_adapter(self):
        from tests import test_meal_concierge_recipes as recipes_fixture
        from recipe_libraries import RecipeLibraryExternalMissingError
        fixture = recipes_fixture.MealieAdapterTests()
        fixture.setUp()
        adapter, _ = fixture.adapter(*fixture.capability_responses())
        capabilities = adapter.capabilities()
        records, counts = {}, {"POST": 0, "PATCH": 0, "DELETE": 0}
        def remote(method, path, **arguments):
            if path == "/api/users/self":
                return deepcopy(fixture.fixture["authenticated_user"])
            if method == "POST":
                counts[method] += 1
                slug = "fixture-import-" + str(counts[method])
                identifier = f"11111111-1111-4111-8111-{counts[method]:012d}"
                records[identifier] = {"id": identifier, "slug": slug, "name": arguments["body"]["name"], "updatedAt": "2026-09-05T00:00:00Z", "recipeIngredient": [], "recipeInstructions": [], "notes": [], "tags": [], "extras": {}, "description": "", "orgURL": None}
                return slug
            identifier = path.rsplit("/", 1)[-1]
            record = next((value for key, value in records.items() if identifier in {key, value["slug"]}), None)
            if record is None:
                raise RecipeLibraryExternalMissingError("exact fixture missing")
            if method == "PATCH":
                counts[method] += 1
                if counts[method] == 1:
                    raise OSError("crash before content write")
                record.update(deepcopy(arguments["body"]))
                record["tags"] = [{"name": tag} for tag in record["tags"]]
                record["updatedAt"] = "2026-09-05T00:01:00Z"
            if method == "DELETE":
                counts[method] += 1
                records.pop(record["id"])
            return deepcopy(record)
        adapter._request = remote
        adapter.capabilities = lambda: capabilities
        settings = {**CONFIG, "primary_recipe_library_id": "family-mealie", "recipe_libraries": [fixture.connection]}
        directory = Path(self.temp.name) / "recovery"
        def reopen():
            app = Application(StateStore(directory, settings), self.provider, self.browser, recipe_library_adapters={"family-mealie": adapter})
            app.handle({"operation": "setup", "action": "apply", "keep_current": True})
            return app
        app = reopen()
        ref = app.recipes.persist_discovery(full_recipe("Recoverable import", external_id="recoverable-fixture"))["discovery_ref"]
        intent = {"operation": "recipes", "action": "save", "discovery_ref": ref, "idempotency_key": "import-first"}
        failed = app.handle(intent)
        self.assertEqual(failed["status"], "uncertain")
        app = reopen()
        recovered = app.handle({"operation": "recipes", "action": "import_recovery", "operation_id": failed["operation_id"]})
        self.assertEqual(recovered["recovery"]["status"], "incomplete_import")
        self.assertEqual(counts, {"POST": 1, "PATCH": 1, "DELETE": 0})
        exact = recovered["recovery"]["library_recipe_ref"]
        # Editing the stub makes it ordinary user content and stops cleanup suggestions.
        records[exact["recipe_id"]]["description"] = "User work"
        changed = app.handle({"operation": "recipes", "action": "import_recovery", "operation_id": failed["operation_id"]})
        self.assertEqual(changed["recovery"]["status"], "unresolved")
        records[exact["recipe_id"]]["description"] = ""
        deletion = app.handle({"operation": "recipes", "action": "delete_prepare", "library_recipe_ref": exact})
        deleted = app.handle({"operation": "recipes", "action": "delete_confirm", "confirmation_id": deletion["confirmation_id"], "idempotency_key": "remove-exact-stub"})
        self.assertEqual(deleted["status"], "confirmed")
        closed = app.handle({"operation": "recipes", "action": "import_recovery", "operation_id": failed["operation_id"], "deletion_operation_id": deleted["operation_id"]})
        self.assertEqual(closed["error_code"], "incomplete_import_removed")
        self.assertEqual(app.handle(intent)["status"], "failed")
        saved = app.handle({**intent, "idempotency_key": "import-new-explicit-intent"})
        self.assertEqual(saved["status"], "confirmed")
        self.assertEqual(counts, {"POST": 2, "PATCH": 2, "DELETE": 1})

    def test_lost_mealie_post_response_recovers_marker_without_creating_again(self):
        from tests import test_meal_concierge_recipes as recipes_fixture
        from recipe_library_mealie import _marker
        fixture = recipes_fixture.MealieAdapterTests()
        fixture.setUp()
        adapter, _ = fixture.adapter()
        operation = fixture.operation()
        stub = {"id": fixture.recipe_id, "slug": "exact-marker-stub", "name": "Hermes import " + _marker(operation["operation_id"]), "updatedAt": "2026-09-05T00:00:00Z", "recipeIngredient": [], "recipeInstructions": [], "tags": [], "notes": [], "extras": {}}
        calls = []
        def remote(method, path, **arguments):
            calls.append((method, path))
            if path == "/api/recipes":
                return {"items": [deepcopy(stub)], "page": 1, "perPage": 50, "total": 1, "totalPages": 1}
            return deepcopy(stub)
        adapter._request = remote
        found = adapter.inspect_incomplete_create(full_recipe("Missing response"), operation, {"provider_principal": "fixture", "provider_binding": "a" * 64})
        self.assertFalse(found["complete"])
        self.assertEqual(found["library_recipe_ref"]["recipe_id"], fixture.recipe_id)
        self.assertEqual([method for method, _ in calls], ["GET", "GET"])

    def test_pantry_completion_cannot_hide_existing_or_later_cart_goods(self):
        from tests import test_meal_concierge_products as products
        case = products.ProductRuntimeTests()
        case.setUp()
        self.addCleanup(case.tearDown)
        apply = lambda plan: case.app.handle({"operation": "products", "action": "apply", "product_plan": plan, "product_plan_digest": plan["product_plan_digest"], "cart_change_requested": True})
        self.assertTrue(apply(case.prepare(approve=True))["applied"])
        plan = case.app.handle({"operation": "products", "action": "prepare", "menu_ref": case.menu_ref,
            "ingredient_decisions": [{"source": {"collection": "dishes", "recipe_index": 0, "ingredient_index": 0}, "action": "have_all"}]})["product_plan"]
        result = apply(plan)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "menu_fully_covered_review_existing_cart")
        self.assertNotIn("product_plan_completion", case.store.read())
        self.assertEqual(case.provider.cart["items"][0]["quantity"], 1)
        with case.store.locked() as state:
            state["product_plan_completion"] = {"menu_ref": case.menu_ref, "nothing_to_buy": True}
        case.app.handle({"operation": "cart", "action": "sync", "menu_ref": case.menu_ref,
            "requirements": [{"product_id": "10", "product_name": "Fixture Mel", "quantity": 2}]})
        self.assertNotIn("product_plan_completion", case.store.read())


if __name__ == "__main__":
    unittest.main()
