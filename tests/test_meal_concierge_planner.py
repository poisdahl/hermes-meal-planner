from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from core import StateStore
from planner import MAX_EXPLORED_STATES, MAX_HISTORY_RECORDS, PlannerError
from service import Application, Server


CONFIG = {
    "instance": "planner-test",
    "household": "Planner Test",
    "provider": "oda",
    "email_automation_profile": "test-email",
    "profile_overrides": {},
}


class NoProviderCalls:
    def __init__(self):
        self.calls = []

    def probe(self):
        return {
            "protocol_version": "2025-11-25",
            "server": {"name": "fixture", "version": "1"},
            "tool_count": 0,
        }

    def call(self, tool, arguments, **kwargs):
        self.calls.append((tool, arguments, kwargs))
        raise AssertionError("the planner must not call a provider")


def recipe(
    name: str, identity: str, *, ingredient: str = "gulrot", unit: str = "g",
    active_minutes: int | None = 30,
) -> dict:
    value = {
        "name": name,
        "language": "nb-NO",
        "portions": 2,
        "ingredients": [{
            "raw": f"200 {unit} {ingredient}",
            "quantity": 200,
            "unit": unit,
            "item": ingredient,
            "scalable": True,
        }],
        "steps": ["Tilbered."],
        "tags": [],
        "source": {
            "kind": "user",
            "publisher": "Fixture",
            "title": name,
            "external_id": identity,
            "relationship": "user_supplied",
        },
        "rights": {"storage": "full", "credit": "Fixture"},
    }
    if active_minutes is not None:
        value["times"] = {"active_minutes": active_minutes}
    return value


def explicit_facts(
    *, active_minutes: int | None = None, dietary: list[str] | None = None,
    complete: bool = False, vegetables: list[str] | None = None,
    perishability: str | None = None, variety: list[str] | None = None,
) -> dict:
    result = {}
    if active_minutes is not None:
        result["active_minutes"] = {"source": "explicit", "value": active_minutes}
    if dietary is not None:
        result["dietary_facets"] = {
            "source": "explicit",
            "values": dietary,
            "complete": complete,
            "vegetable_types": vegetables or [],
        }
    if perishability is not None:
        result["perishability"] = {"source": "explicit", "value": perishability}
    if variety is not None:
        result["variety_facets"] = {"source": "explicit", "values": variety}
    return result


class WeeklyPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name), CONFIG)
        self.provider = NoProviderCalls()
        self.app = Application(self.store, self.provider, object())
        with self.store.locked() as state:
            state["setup"]["status"] = "complete"

    def tearDown(self):
        self.temp.cleanup()

    def save_candidates(self, count: int, *, ingredient: str = "gulrot") -> list[dict]:
        result = []
        for index in range(count):
            saved = self.app.handle({
                "operation": "recipes",
                "action": "save",
                "recipe": recipe(f"Recipe {index}", f"recipe-{index}", ingredient=ingredient),
                "idempotency_key": f"save-{index}",
            })["recipe"]
            result.append({"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}})
        return result

    @staticmethod
    def request(candidates: list[dict], *, dates: list[str] | None = None, **changes) -> dict:
        return {
            "week": "2026-W37",
            "dates": dates or ["2026-09-07"],
            "candidates": candidates,
            **changes,
        }

    def plan(self, planner_input: dict) -> dict:
        return self.app.handle({
            "operation": "menu", "action": "plan", "planner_input": planner_input,
        })["plan"]

    def socket_call(self, request: dict) -> dict:
        class Connection:
            def __init__(self, payload):
                self.payload = payload
                self.sent = b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def recv(self, _size):
                payload, self.payload = self.payload, b""
                return payload

            def sendall(self, value):
                self.sent += value

        connection = Connection((json.dumps(request) + "\n").encode())
        server = Server(
            Path(self.temp.name) / "service.sock", os.getgid(), os.getuid(), self.app
        )
        with mock.patch("service.peer_uid", return_value=os.getuid()):
            server._serve(connection)
        return json.loads(connection.sent)

    def test_order_restart_ties_and_repeated_input_are_byte_stable(self):
        candidates = self.save_candidates(3)
        request = self.request(
            candidates, dates=["2026-09-07", "2026-09-08"], alternatives=3,
        )
        first = self.plan(request)
        reordered = self.plan({**request, "candidates": list(reversed(candidates))})
        restarted = Application(self.store, NoProviderCalls(), object())
        third = restarted.handle({
            "operation": "menu", "action": "plan", "planner_input": request,
        })["plan"]
        encoded = lambda value: json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertEqual(encoded(first), encoded(reordered))
        self.assertEqual(encoded(first), encoded(third))
        self.assertEqual(first["status"], "planned")
        self.assertEqual(len(first["selections"]), 3)
        for selection in first["selections"]:
            self.assertEqual(
                selection["total_score"],
                sum(slot["score"] for slot in selection["slots"])
                + sum(reason["weight"] for reason in selection["plan_reason_contributions"]),
            )
            for slot in selection["slots"]:
                self.assertEqual(
                    slot["score"], sum(reason["weight"] for reason in slot["reason_contributions"])
                )

    def test_safety_unknown_blocks_without_using_recipe_text(self):
        candidate = self.save_candidates(1)[0]
        with self.store.locked() as state:
            state["profile"]["diet"]["allergies_or_sensitivities"] = ["Milk"]
        unknown = self.plan(self.request([candidate]))
        self.assertEqual(unknown["status"], "needs_input")
        self.assertEqual(
            unknown["candidate_evaluations"][0]["hard_constraints"]["status"], "unknown"
        )
        untrusted_clearance = {
            **candidate,
            "facts": {"safety": {
                "source": "explicit",
                "allergies_or_sensitivities": {"milk": "free"},
                "avoid": {},
            }},
        }
        with self.assertRaisesRegex(PlannerError, "candidate facts have unknown fields"):
            self.plan(self.request([untrusted_clearance]))

    def test_unknown_soft_facts_are_named_and_strict_time_blocks(self):
        saved = self.app.handle({
            "operation": "recipes", "action": "save",
            "recipe": recipe("Unknown time", "unknown-time", active_minutes=None),
            "idempotency_key": "unknown-time",
        })["recipe"]
        candidate = {"recipe_ref": {"id": saved["id"], "revision": saved["revision"]}}
        normal = self.plan(self.request([candidate]))
        self.assertEqual(normal["status"], "planned")
        self.assertIn("active_minutes", normal["selection"]["soft_relaxations"])
        strict = self.plan(self.request([candidate], strict_targets=["active_minutes"]))
        self.assertEqual(strict["status"], "needs_input")
        too_slow = {**candidate, "facts": explicit_facts(active_minutes=61)}
        self.assertEqual(
            self.plan(self.request([too_slow], strict_targets=["active_minutes"]))["status"],
            "no_plan",
        )
        known = {**candidate, "facts": explicit_facts(active_minutes=30)}
        self.assertEqual(
            self.plan(self.request([known], strict_targets=["active_minutes"]))["status"],
            "planned",
        )

    def test_strict_dietary_unknown_infeasible_and_satisfied_are_distinct(self):
        candidate = self.save_candidates(1)[0]
        target = ["minimum_legume_dinners"]
        incomplete = {
            **candidate, "facts": explicit_facts(dietary=[], complete=False),
        }
        self.assertEqual(
            self.plan(self.request([incomplete], strict_targets=target))["status"],
            "needs_input",
        )
        with self.store.locked() as state:
            state["profile"]["diet"]["minimum_legume_dinners"] = 2
        impossible = self.plan(self.request([incomplete], strict_targets=target))
        self.assertEqual(impossible["status"], "no_plan")
        with self.store.locked() as state:
            state["profile"]["diet"]["minimum_legume_dinners"] = 1
        complete = {**candidate, "facts": explicit_facts(dietary=[], complete=True)}
        self.assertEqual(
            self.plan(self.request([complete], strict_targets=target))["status"],
            "no_plan",
        )
        legume = {
            **candidate,
            "facts": explicit_facts(dietary=["legume"], complete=True),
        }
        self.assertEqual(
            self.plan(self.request([legume], strict_targets=target))["status"],
            "planned",
        )

    def test_link_only_and_malformed_references_never_enter_a_plan(self):
        link = self.app.handle({
            "operation": "recipes", "action": "save", "idempotency_key": "link",
            "recipe": {
                "name": "Provider link", "ingredients": [], "steps": [],
                "source": {
                    "kind": "oda", "publisher": "oda.com", "title": "Provider link",
                    "url": "https://oda.com/no/products/1", "external_id": "1",
                    "relationship": "original",
                },
                "rights": {"storage": "link_only", "credit": "Oda"},
            },
        })["recipe"]
        reference = {"recipe_ref": {"id": link["id"], "revision": link["revision"]}}
        result = self.plan(self.request([reference]))
        self.assertEqual(result["status"], "no_plan")
        self.assertEqual(
            result["candidate_evaluations"][0]["hard_constraints"]["reasons"][0]["code"],
            "not_materializable",
        )
        for invalid in ({"name": "guess"}, {"recipe_ref": {"id": link["id"]}}, {"url": "https://example.test"}):
            with self.subTest(invalid=invalid), self.assertRaises(PlannerError):
                self.plan(self.request([invalid]))
        with self.assertRaisesRegex(PlannerError, "exact id"):
            self.plan(self.request([{
                "recipe_ref": {"id": f" {link['id']} ", "revision": link["revision"]},
            }]))
        snapshot = self.app.recipes.persist_discovery(recipe("Exact", "exact-ref"))
        with self.assertRaisesRegex(PlannerError, "bounded exact text"):
            self.plan(self.request([{
                "discovery_ref": f" {snapshot['discovery_ref']} ",
            }]))

    def test_cooldown_requires_exact_current_override_and_save_preserves_it(self):
        candidate = self.save_candidates(1)[0]
        stored = self.app.recipes.get(candidate["recipe_ref"]["id"])
        key = stored["recipe_key"]
        with self.store.locked() as state:
            state["recipe_usage"]["old"] = {
                "week": "2026-W36", "status": "cooked", "recipe_keys": [key],
                "cooked_keys": [key], "not_cooked_keys": [], "cooldown_overrides": {},
                "order_id": None,
            }
        self.assertEqual(self.plan(self.request([candidate]))["status"], "no_plan")
        with self.assertRaisesRegex(PlannerError, "exact candidate"):
            self.plan(self.request([candidate], cooldown_overrides={"wrong": "requested"}))
        plan = self.plan(self.request(
            [candidate], cooldown_overrides={key: "User explicitly requested this repeat"},
        ))
        saved = self.app.handle({
            "operation": "menu", "action": "save", "planner_handoff": plan["save_handoff"],
        })["menu"]
        self.assertEqual(
            self.store.read()["recipe_usage"][saved["menu_id"]]["cooldown_overrides"],
            {key: "User explicitly requested this repeat"},
        )

    def test_planner_save_freezes_exact_selection_and_is_idempotent(self):
        candidates = self.save_candidates(2)
        plan = self.plan(self.request(candidates))
        handoff = plan["save_handoff"]
        saved = self.app.handle({
            "operation": "menu", "action": "save", "planner_handoff": handoff,
        })["menu"]
        self.assertEqual(saved["planner_selection"]["input_digest"], plan["input_digest"])
        self.assertEqual(saved["planner_selection"]["selection_digest"], plan["selection_digest"])
        self.assertEqual(saved["schedule"][0]["reference"], plan["selection"]["slots"][0]["reference"])
        repeated = self.app.handle({
            "operation": "menu", "action": "save", "planner_handoff": handoff,
        })
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["menu"], saved)
        self.assertEqual(self.provider.calls, [])

    def test_handoff_remains_anchored_when_save_crosses_household_midnight(self):
        candidate = self.save_candidates(1)[0]
        before_midnight = datetime(2026, 9, 7, 21, 59, tzinfo=timezone.utc)
        after_midnight = datetime(2026, 9, 7, 22, 1, tzinfo=timezone.utc)
        with mock.patch("service.now", return_value=before_midnight):
            plan = self.plan(self.request([candidate]))
        self.assertEqual(plan["request"]["as_of_date"], "2026-09-07")
        with mock.patch("service.now", return_value=after_midnight):
            saved = self.app.handle({
                "operation": "menu", "action": "save",
                "planner_handoff": plan["save_handoff"],
            })["menu"]
        self.assertEqual(
            saved["planner_selection"]["request"]["as_of_date"], "2026-09-07"
        )

    def test_socket_runtime_path_plans_and_saves_the_returned_handoff(self):
        candidate = self.save_candidates(1)[0]
        planned = self.socket_call({
            "operation": "menu", "action": "plan",
            "planner_input": self.request([candidate]),
            "allow_repeat_keys": [], "interactive": True,
        })
        self.assertTrue(planned["ok"])
        handoff = planned["result"]["plan"]["save_handoff"]
        saved = self.socket_call({
            "operation": "menu", "action": "save", "planner_handoff": handoff,
            "allow_repeat_keys": [], "interactive": True,
        })
        self.assertTrue(saved["ok"])
        self.assertEqual(
            saved["result"]["menu"]["planner_selection"]["selection_digest"],
            handoff["selection_digest"],
        )
        self.assertEqual(self.provider.calls, [])

    def test_profile_or_handoff_drift_fails_before_state_mutation(self):
        plan = self.plan(self.request(self.save_candidates(2)))
        tampered = deepcopy(plan["save_handoff"])
        tampered["selection"]["slots"][0]["reason_contributions"][0]["weight"] += 1
        with self.assertRaisesRegex(PlannerError, "stale, changed or fabricated"):
            self.app.handle({
                "operation": "menu", "action": "save", "planner_handoff": tampered,
            })
        self.assertIsNone(self.store.read()["menu"])
        with self.store.locked() as state:
            state["profile"]["cuisine"]["wanted"] = ["Thai"]
        with self.assertRaisesRegex(PlannerError, "stale, changed or fabricated"):
            self.app.handle({
                "operation": "menu", "action": "save", "planner_handoff": plan["save_handoff"],
            })
        self.assertIsNone(self.store.read()["menu"])

    def test_profile_race_between_verification_and_commit_is_rechecked(self):
        plan = self.plan(self.request(self.save_candidates(2)))
        original = self.app._materialize_planner_menu

        def materialize_then_change_profile(handoff, resolved):
            menu = original(handoff, resolved)
            with self.store.locked() as state:
                state["profile"]["cuisine"]["wanted"] = ["Changed concurrently"]
            return menu

        with mock.patch.object(
            self.app, "_materialize_planner_menu", side_effect=materialize_then_change_profile
        ), self.assertRaisesRegex(PlannerError, "became stale before save"):
            self.app.handle({
                "operation": "menu", "action": "save",
                "planner_handoff": plan["save_handoff"],
            })
        self.assertIsNone(self.store.read()["menu"])

    def test_cross_instance_recipe_archive_waits_until_planner_save_commits(self):
        candidate = self.save_candidates(1)[0]
        plan = self.plan(self.request([candidate]))
        other_app = Application(self.store, NoProviderCalls(), object())
        entered = threading.Event()
        release = threading.Event()
        original = self.app._materialize_planner_menu
        materialize_calls = 0

        def pause_first_materialization(handoff, resolved):
            nonlocal materialize_calls
            menu = original(handoff, resolved)
            materialize_calls += 1
            if materialize_calls == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            return menu

        save_result = {}
        archive_result = {}
        failures = []

        def save():
            try:
                save_result.update(self.app.handle({
                    "operation": "menu", "action": "save",
                    "planner_handoff": plan["save_handoff"],
                }))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def archive():
            try:
                archive_result.update(other_app.handle({
                    "operation": "recipes", "action": "archive",
                    "recipe_id": candidate["recipe_ref"]["id"],
                    "expected_revision": candidate["recipe_ref"]["revision"],
                    "idempotency_key": "archive-after-planner-save",
                }))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        with mock.patch.object(
            self.app, "_materialize_planner_menu", side_effect=pause_first_materialization
        ):
            save_thread = threading.Thread(target=save)
            archive_thread = threading.Thread(target=archive)
            save_thread.start()
            self.assertTrue(entered.wait(2))
            archive_thread.start()
            archive_thread.join(0.05)
            self.assertTrue(archive_thread.is_alive())
            release.set()
            save_thread.join(2)
            archive_thread.join(2)
        self.assertFalse(save_thread.is_alive())
        self.assertFalse(archive_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertIn("menu", save_result)
        self.assertEqual(archive_result["recipe"]["status"], "archived")

    def test_discovery_is_local_exact_expires_and_never_calls_provider(self):
        snapshot = self.app.recipes.persist_discovery(recipe(
            "Discovered", "discovered", ingredient="linser"
        ))
        candidate = {"discovery_ref": snapshot["discovery_ref"]}
        plan = self.plan(self.request([candidate]))
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(self.provider.calls, [])
        connection = sqlite3.connect(self.app.recipes.path)
        try:
            connection.execute(
                "UPDATE discovery_snapshots SET expires_at='2000-01-01T00:00:00+00:00'"
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(Exception, "discovery reference was not found"):
            self.app.handle({
                "operation": "menu", "action": "save",
                "planner_handoff": plan["save_handoff"],
            })
        self.assertIsNone(self.store.read()["menu"])
        self.assertEqual(self.provider.calls, [])

    def test_fresh_is_placed_early_and_reuse_is_capped_by_monotony(self):
        candidates = self.save_candidates(3, ingredient="potet")
        candidates[0]["facts"] = explicit_facts(
            perishability="fresh", variety=["same"]
        )
        candidates[1]["facts"] = explicit_facts(
            perishability="shelf_stable", variety=["same"]
        )
        candidates[2]["facts"] = explicit_facts(
            perishability="unknown", variety=["same"]
        )
        plan = self.plan(self.request(
            candidates,
            dates=["2026-09-07", "2026-09-08", "2026-09-09"],
        ))
        slots = plan["selection"]["slots"]
        fresh_ref = json.dumps(candidates[0]["recipe_ref"], sort_keys=True)
        self.assertEqual(
            next(slot["date"] for slot in slots if json.dumps(slot["reference"]["recipe_ref"], sort_keys=True) == fresh_ref),
            "2026-09-07",
        )
        reasons = {item["code"]: item for item in plan["selection"]["plan_reason_contributions"]}
        self.assertLessEqual(reasons["ingredients:exact_reuse"]["weight"], 16)
        self.assertLess(reasons["variety:monotony"]["weight"], 0)
        self.assertLess(reasons["ingredients:monotony"]["weight"], 0)

    def test_ingredient_reuse_counts_meals_not_duplicate_recipe_rows(self):
        value = recipe("Duplicate row", "duplicate-row", ingredient="fløte")
        value["ingredients"].append(deepcopy(value["ingredients"][0]))
        saved = self.app.handle({
            "operation": "recipes", "action": "save", "recipe": value,
            "idempotency_key": "duplicate-row",
        })["recipe"]
        plan = self.plan(self.request([{
            "recipe_ref": {"id": saved["id"], "revision": saved["revision"]},
        }]))
        reasons = {
            item["code"]: item
            for item in plan["selection"]["plan_reason_contributions"]
        }
        self.assertEqual(reasons["ingredients:exact_reuse"]["weight"], 0)

    def test_latest_usage_week_drives_recency_score(self):
        candidate = self.save_candidates(1)[0]
        key = self.app.recipes.get(candidate["recipe_ref"]["id"])["recipe_key"]
        with self.store.locked() as state:
            state["recipe_usage"]["old-cooked"] = {
                "week": "2026-W01", "status": "manual", "recipe_keys": [key],
                "cooked_keys": [key], "not_cooked_keys": [],
                "cooldown_overrides": {}, "order_id": None,
            }
            state["recipe_usage"]["recent-planned"] = {
                "week": "2026-W40", "status": "planned", "recipe_keys": [key],
                "cooked_keys": [], "not_cooked_keys": [],
                "cooldown_overrides": {}, "order_id": None,
            }
        plan = self.plan(self.request(
            [candidate], dates=["2026-12-07"], week="2026-W50"
        ))
        reason = next(
            item for item in plan["selection"]["slots"][0]["reason_contributions"]
            if item["code"] == "recency:recorded_use"
        )
        self.assertEqual(reason["detail"]["last_week"], "2026-W40")
        self.assertEqual(reason["detail"]["weeks_since"], 10)

    def test_duplicate_revisions_count_as_one_candidate_identity(self):
        first = self.app.handle({
            "operation": "recipes", "action": "save",
            "recipe": recipe("Revisioned", "revisioned"),
            "idempotency_key": "revisioned-save",
        })["recipe"]
        second = self.app.handle({
            "operation": "recipes", "action": "update",
            "recipe_id": first["id"], "expected_revision": first["revision"],
            "recipe": recipe("Revisioned", "revisioned"),
            "idempotency_key": "revisioned-update",
        })["recipe"]
        unique = self.save_candidates(1)[0]
        plan = self.plan(self.request([
            {"recipe_ref": {"id": first["id"], "revision": first["revision"]}},
            {"recipe_ref": {"id": second["id"], "revision": second["revision"]}},
            unique,
        ], dates=["2026-09-07", "2026-09-08"]))
        self.assertEqual(plan["status"], "planned")
        duplicate_reasons = [
            reason
            for evaluation in plan["candidate_evaluations"]
            for reason in evaluation["hard_constraints"]["reasons"]
            if reason["code"] == "duplicate_recipe_identity"
        ]
        self.assertEqual(len(duplicate_reasons), 1)

    def test_extra_effort_is_placed_on_weekend(self):
        short = self.app.handle({
            "operation": "recipes", "action": "save",
            "recipe": recipe("Short", "short", active_minutes=30),
            "idempotency_key": "short",
        })["recipe"]
        long = self.app.handle({
            "operation": "recipes", "action": "save",
            "recipe": recipe("Long", "long", active_minutes=50),
            "idempotency_key": "long",
        })["recipe"]
        plan = self.plan(self.request([
            {"recipe_ref": {"id": short["id"], "revision": short["revision"]}},
            {"recipe_ref": {"id": long["id"], "revision": long["revision"]}},
        ], dates=["2026-09-11", "2026-09-12"]))
        self.assertEqual(
            next(slot["date"] for slot in plan["selection"]["slots"] if slot["name"] == "Long"),
            "2026-09-12",
        )

    def test_work_budget_fails_clearly_without_truncation(self):
        candidates = self.save_candidates(10)
        dates = [f"2026-09-{day:02d}" for day in range(7, 14)]
        with self.assertRaisesRegex(
            PlannerError, rf"states exceeds {MAX_EXPLORED_STATES}"
        ):
            self.plan(self.request(candidates, dates=dates))

    def test_candidate_day_alternative_and_date_bounds_are_exact(self):
        candidates = self.save_candidates(13)
        with self.assertRaisesRegex(PlannerError, "one to 12"):
            self.plan(self.request(candidates))
        accepted = self.plan(self.request(candidates[:12]))
        self.assertEqual(accepted["status"], "planned")
        with self.assertRaisesRegex(PlannerError, "one to 7"):
            self.plan(self.request(
                candidates[:1],
                dates=[f"2026-09-{day:02d}" for day in range(7, 15)],
            ))
        with self.assertRaisesRegex(PlannerError, "one to 3"):
            self.plan(self.request(candidates[:1], alternatives=4))
        with self.assertRaisesRegex(PlannerError, "must belong"):
            self.plan(self.request(candidates[:1], dates=["2026-09-14"]))
        with self.assertRaisesRegex(PlannerError, "unique"):
            self.plan(self.request(
                candidates[:2], dates=["2026-09-07", "2026-09-07"],
            ))

    def test_history_work_bound_fails_without_partial_ranking(self):
        candidate = self.save_candidates(1)[0]
        with self.store.locked() as state:
            state["recipe_usage"] = {
                f"unrelated-{index}": {
                    "week": "2020-W01", "status": "cancelled", "recipe_keys": [f"x-{index}"],
                    "cooked_keys": [], "not_cooked_keys": [], "cooldown_overrides": {},
                    "order_id": None,
                }
                for index in range(MAX_HISTORY_RECORDS + 1)
            }
        with self.assertRaisesRegex(PlannerError, str(MAX_HISTORY_RECORDS)):
            self.plan(self.request([candidate]))


if __name__ == "__main__":
    unittest.main()
