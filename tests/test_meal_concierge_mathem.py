"""Synthetic Mathem contract and household flow; no customer session or orders."""
from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import httpx
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_meal_concierge as existing
from core import HouseholdError, StateStore, cart_summary
from oda import OdaClient, REQUIRED_TOOLS, normalize_oda_delivery_slots, oda_delivery_slot_date
from product_observations import _mathem_ore, normalize_oda_product_search, parse_package
from recipe_sources import provider_recipe_candidates
from recipes import RecipeError, normalize_recipe
from service import Application, config, validate_schedule
from service_common import email_automation_key


PRODUCTS = {'result': [{'query': 'ägg', 'hasMore': False, 'products': [
    {'id': 10, 'name': 'Ägg', 'description': '6 st', 'price': '29,90 kr', 'availability': True},
]}]}
SLOTS = {'deliveryDate': '2026-09-12', 'slots': [{
    'id': 77, 'openDatetime': '2026-09-12T09:00:00+02:00',
    'closeDatetime': '2026-09-12T12:00:00+02:00', 'price': '19,90 kr',
    'isSelected': False, 'isFull': False, 'isUnavailable': False,
}]}


class MathemShop(existing.FakeOda):
    def __init__(self):
        super().__init__()
        self.slots = deepcopy(SLOTS)
        self.cart['items'] = []
        self.cart['subtotal'] = 0
        self.cart['delivery'] = None
        self.fail_after_write = False

    def call(self, tool, arguments, **kwargs):
        self.calls.append((tool, deepcopy(arguments)))
        if tool == 'product_search':
            return normalize_oda_product_search(PRODUCTS, provider='mathem')
        if tool == 'recipe_search':
            return {'recipes': [{'id': '42', 'name': 'Soppa', 'url': 'https://www.mathem.se/se/recipes/42-soppa/'}]}
        if tool == 'get_delivery_slots':
            return normalize_oda_delivery_slots(self.slots, provider='mathem')
        if tool == 'select_delivery_slot':
            assert type(arguments['delivery_slot_id']) is int
            assert arguments['delivery_slot_id'] == 77
            self.slots['slots'][0]['isSelected'] = True
            self.cart['delivery'] = {'slot_id': 77, 'display': 'Hemleverans 09–12, 12 sep'}
            return {'selected': True}
        if tool == 'manipulate_cart':
            for operation in arguments['operations']:
                assert type(operation['productId']) is int
                previous = next((p for p in self.cart['items'] if p['product_id'] == operation['productId']), None)
                if previous is None:
                    previous = {'product_id': operation['productId'], 'name': 'Ägg', 'quantity': 0, 'price': 29.9}
                    self.cart['items'].append(previous)
                previous['quantity'] += operation['quantity']
            self.cart['items'] = [p for p in self.cart['items'] if p['quantity']]
            self.cart['subtotal'] = sum(p['price'] * p['quantity'] for p in self.cart['items'])
            if self.fail_after_write:
                raise HouseholdError('Mathem response timed out after dispatch')
            return deepcopy(self.cart)
        self.calls.pop()
        return super().call(tool, arguments, **kwargs)


class MathemFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = {**existing.CONFIG, 'provider': 'mathem', 'confirmation_policy': 'standing'}
        self.store = StateStore(self.root / 'state', self.settings)
        self.shop = MathemShop()
        self.app = Application(self.store, self.shop, None)

    def test_search_cart_delivery_and_manual_checkout(self):
        self.app.handle({'operation': 'setup', 'action': 'apply', 'keep_current': True})
        search = self.app.handle({'operation': 'catalog', 'action': 'products', 'query': 'ägg'})
        self.assertEqual(search['provider'], 'mathem')
        self.assertEqual(search['products'][0]['provider'], 'mathem')
        cart = self.app.handle({'operation': 'cart', 'action': 'change', 'operations': [{'productId': '10', 'quantity': 2}]})
        self.assertEqual(cart_summary(cart)['items'][0]['quantity'], 2)
        slots = self.app.handle({'operation': 'delivery', 'action': 'list'})
        slot = slots['slots'][0]
        self.assertEqual(slot['slot_ref'], 'mathem:2026-09-12:77')
        self.assertEqual(slot['price_ore'], 1990)
        selected = self.app.handle({'operation': 'delivery', 'action': 'select', 'slot_ref': slot['slot_ref']})
        self.assertEqual(selected['provider'], 'mathem')
        self.assertEqual(self.store.read()['delivery_selection']['provider'], 'mathem')
        prepared = self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.assertTrue(prepared['manual_checkout_required'])
        self.assertFalse(prepared['confirmed'])
        self.assertEqual(prepared['currency'], 'SEK')
        self.assertEqual(prepared['summary']['total'], 59.8)
        self.assertIn('www.mathem.se', prepared['checkout_url'])
        self.assertIsNone(self.store.read()['pending_checkout'])
        self.assertIsNone(self.app.browser)

    def test_weekly_cart_ready_selects_delivery_and_hands_off_manual_payment(self):
        self.app.handle({'operation': 'schedule', 'action': 'update', 'changes': {
            'enabled': True, 'mode': 'cart_ready', 'auto_checkout': False,
            'delivery': {'weekday': 'Saturday', 'strategy': 'cheapest'},
        }})
        self.app.handle({'operation': 'schedule', 'action': 'set_cron_job', 'cron_job_id': 'test-cron'})
        with mock.patch('service.now', return_value=datetime(2026, 9, 10, 13, 5, tzinfo=timezone.utc)):
            result = self.app.handle({'operation': 'checkout', 'action': 'auto', 'occurrence': '2026-W37'})
        self.assertEqual(result['mode'], 'cart_ready')
        self.assertEqual(result['selected']['slot_ref'], 'mathem:2026-09-12:77')
        self.assertIsNone(self.store.read()['pending_checkout'])
        continuation = self.app.handle({'operation': 'checkout', 'action': 'prepare', 'occurrence': '2026-W37'})
        self.assertTrue(continuation['manual_checkout_required'])
        self.assertEqual(continuation['occurrence'], '2026-W37')

    def test_uncertain_cart_is_reconciled_without_second_dispatch(self):
        self.shop.fail_after_write = True
        with self.assertRaisesRegex(HouseholdError, 'timed out'):
            self.app.handle({'operation': 'cart', 'action': 'change', 'operations': [{'productId': 10, 'quantity': 1}]})
        with self.assertRaisesRegex(HouseholdError, 'reconcile_change'):
            self.app.handle({'operation': 'checkout', 'action': 'prepare'})
        self.app.handle({'operation': 'cart', 'action': 'reconcile_change'})
        self.assertIsNone(self.store.read().get('pending_cart_change'))
        self.assertEqual(sum(tool == 'manipulate_cart' for tool, _ in self.shop.calls), 1)

    def test_protected_actions_never_use_oda_browser_or_create_attempts(self):
        before = self.store.read()
        for operation, actions in [('checkout', ['confirm', 'submit', 'reconcile']), ('orders', ['change_begin', 'change_abort', 'cancel_prepare', 'cancel_submit', 'cancel_confirm', 'cancel_reconcile'])]:
            for action in actions:
                with self.subTest(operation=operation, action=action), self.assertRaisesRegex(HouseholdError, 'Mathem'):
                    self.app.handle({'operation': operation, 'action': action, 'order_id': '1', 'idempotency_key': 'test'})
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.shop.calls, [])
        with self.assertRaisesRegex(HouseholdError, 'cart_ready'):
            validate_schedule({**before['schedule'], 'auto_checkout': True}, 'mathem')

    def test_mathem_defaults_recipes_and_provider_isolation(self):
        status = self.app.handle({'operation': 'status'})
        self.assertEqual(status['currency'], 'SEK')
        self.assertEqual(status['schedule']['timezone'], 'Europe/Stockholm')
        sources = status['recipe_sources']
        self.assertTrue(sources['mathem'])
        self.assertFalse(sources['oda'])
        self.assertFalse(sources['meny'])
        candidates = self.app._provider_recipe_candidates('mathem', 'soppa', 5)
        self.assertEqual(candidates[0]['language'], 'sv-SE')
        self.assertEqual(candidates[0]['source']['kind'], 'mathem')
        self.assertEqual(candidates[0]['rights']['storage'], 'link_only')
        self.assertEqual(provider_recipe_candidates('oda', self.shop.call('recipe_search', {}), 5), [])
        with self.assertRaisesRegex(HouseholdError, 'belongs to provider mathem'):
            StateStore(self.root / 'state', {**self.settings, 'provider': 'oda'})
        self.assertNotEqual(email_automation_key('oda', '123'), email_automation_key('mathem', '123'))
        order = self.app.handle({'operation': 'orders', 'action': 'get', 'order_id': '123'})
        self.assertEqual(order['tracking']['order_id'], '123')

    def test_existing_source_preferences_survive_upgrade(self):
        oda_root = self.root / 'oda'
        oda_store = StateStore(oda_root, existing.CONFIG)
        with oda_store.locked() as state:
            del state['profile']['recipes']['sources']['mathem']
            state['profile']['recipes']['sources']['oda'] = False
        upgraded = StateStore(oda_root, existing.CONFIG).read()
        self.assertFalse(upgraded['profile']['recipes']['sources']['mathem'])
        self.assertFalse(upgraded['profile']['recipes']['sources']['oda'])

    def test_mathem_config_and_strict_prices(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.settings))
        self.assertEqual(config(path)['provider'], 'mathem')
        for text, expected in [('29,90 kr', 2990), ('29.90', 2990), ('kr\u00a00', 0), ('0 kr', 0), ('SEK 12,50', 1250)]:
            self.assertEqual(_mathem_ore(text), expected)
        for text in ['Från 29,90 kr', 'ca 29,90 kr', '29,90 NOK', '-1 kr', '1 2 kr']:
            self.assertIsNone(_mathem_ore(text))
        self.assertEqual(parse_package('6 st', provider='mathem')['unit'], 'count')
        self.assertEqual(oda_delivery_slot_date('mathem:2026-09-12:77', provider='mathem'), '2026-09-12')
        with self.assertRaises(HouseholdError):
            oda_delivery_slot_date('oda:2026-09-12:77', provider='mathem')

    def test_mathem_content_keeps_provider_recipe_storage_restriction(self):
        value = provider_recipe_candidates('mathem', self.shop.call('recipe_search', {}), 5)[0]
        value['rights']['storage'] = 'full'
        value['rights']['license'] = 'unknown'
        with self.assertRaises(RecipeError):
            normalize_recipe(value)


class MathemTransportTests(unittest.TestCase):
    def test_oauth_endpoint_storage_and_result_use_mathem_identity(self):
        captured = {}
        supported_tools = set(REQUIRED_TOOLS)
        class Storage:
            def __init__(self, server_name, hermes_home):
                captured['storage_name'] = server_name
            def has_cached_tokens(self):
                return True
        def oauth_provider(**kwargs):
            captured['oauth'] = kwargs
            return SimpleNamespace()
        class Context:
            def __init__(self, *args, **kwargs):
                if 'event_hooks' in kwargs:
                    captured['redirect_check'] = kwargs['event_hooks']['response'][0]
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        class Session(Context):
            async def initialize(self):
                return SimpleNamespace(server_info={'name': 'Mathem MCP'}, protocol_version='2025-11-25')
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in supported_tools])
            async def call_tool(self, tool, arguments):
                captured['call'] = (tool, arguments)
                return SimpleNamespace(is_error=False, structured_content=PRODUCTS)
        @asynccontextmanager
        async def stream(url, **kwargs):
            captured['url'] = url
            yield (None, None, None)
        modules = {
            'httpx2': SimpleNamespace(AsyncClient=Context, Timeout=lambda *a, **k: None, URL=httpx.URL),
            'mcp': SimpleNamespace(ClientSession=Session),
            'mcp.client.streamable_http': SimpleNamespace(streamable_http_client=stream),
            'mcp.types': SimpleNamespace(LATEST_PROTOCOL_VERSION='2025-11-25'),
            'tools.mcp_oauth': SimpleNamespace(HermesTokenStorage=Storage, _build_client_metadata=lambda _: {}, _configure_callback_port=lambda *a: None, _make_callback_waiter=lambda *a, **k: None, _make_redirect_handler=lambda *a: None),
            'tools.mcp_oauth_manager': SimpleNamespace(_HERMES_PROVIDER_CLS=oauth_provider),
        }
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(sys.modules, modules):
            client = OdaClient(temp, provider='mathem')
            result = client.call('product_search', {'queries': ['ägg'], 'size': 5})
            self.assertEqual(result['provider'], 'mathem')
            self.assertEqual(captured['url'], 'https://www.mathem.se/mcp')
            self.assertEqual(captured['oauth']['server_url'], captured['url'])
            self.assertEqual(captured['storage_name'], 'mathem-weekly')
            self.assertEqual(captured['oauth']['storage']._tokens_path(), Path(temp) / 'mathem-weekly.json')
            self.assertEqual(captured['oauth']['storage']._client_info_path(), Path(temp) / 'mathem-weekly.client.json')
            self.assertEqual(captured['oauth']['storage']._meta_path(), Path(temp) / 'mathem-weekly.meta.json')
            self.assertTrue((Path(temp) / '.mathem-household.lock').exists())
            self.assertFalse((Path(temp) / '.oda-household.lock').exists())
            self.assertEqual(OdaClient(temp).endpoint, 'https://oda.com/mcp')
            response = SimpleNamespace(is_redirect=True, next_request=SimpleNamespace(url=httpx.URL('https://oda.com/mcp')))
            with self.assertRaisesRegex(HouseholdError, 'outside its store'):
                asyncio.run(captured['redirect_check'](response))
            supported_tools.remove('get_cart')
            with self.assertRaisesRegex(HouseholdError, 'lacks required operations: get_cart'):
                client.probe()
            with mock.patch.object(Storage, 'has_cached_tokens', return_value=False):
                with self.assertRaisesRegex(HouseholdError, 'Mathem login is required'):
                    client.probe()

    def test_runtime_launcher_does_not_require_browser_for_mathem(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = root / 'config.json'
            settings.write_text(json.dumps({**existing.CONFIG, 'provider': 'mathem'}))
            wrapper = root / 'python'
            wrapper.write_text(f"""#!{sys.executable}
import json, os, sys
if len(sys.argv) > 1 and sys.argv[1].endswith('/service.py'):
    print(json.dumps(sys.argv[1:]))
else:
    os.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])
""")
            wrapper.chmod(0o755)
            result = subprocess.run(['/bin/bash', str(existing.CORE / 'run-service.sh')],
                env={**os.environ, 'HERMES_PYTHON': str(wrapper), 'HERMES_HOME': str(root),
                     'MEAL_CONCIERGE_HOME': str(root / 'private'), 'MEAL_CONCIERGE_CONFIG': str(settings),
                     'MEAL_CONCIERGE_BROWSER_SOCKET_DIR': str(root / 'browser-socket'),
                     'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = json.loads(result.stdout)
            self.assertIn('--tokens', arguments)
            self.assertNotIn('--browser-binary', arguments)
            self.assertNotIn('--browser-executable', arguments)
            import service
            with mock.patch.object(sys, 'argv', ['service.py', *arguments[1:]]), mock.patch.object(service.OdaClient, 'probe', return_value={}), mock.patch.object(service, 'OdaBrowser') as browser, mock.patch.object(service.Server, 'run') as run:
                service.main()
            browser.assert_not_called()
            run.assert_called_once()

    def test_installer_supports_mathem_without_browser_automation(self):
        original_run = subprocess.run
        def run_mathem(args, **kwargs):
            if len(args) > 1 and str(args[1]).endswith('/install.sh'):
                args = list(args)
                args[args.index('--provider') + 1] = 'mathem'
                env = kwargs['env']
                hermes = Path(env['HERMES_HOME'])
                (hermes / 'mcp-tokens').mkdir()
                (hermes / 'mcp-tokens' / 'mathem-weekly.json').write_text('{}')
                (hermes / 'config.yaml').write_text(json.dumps({'mcp_servers': {'mathem-weekly': {'enabled': False}}}))
                Path(env['MEAL_CONCIERGE_AGENT_BROWSER']).unlink()
                Path(env['MEAL_CONCIERGE_BROWSER_EXECUTABLE']).unlink()
            return original_run(args, **kwargs)
        for platform in ['Linux', 'Darwin']:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as temp, mock.patch.object(existing.subprocess, 'run', side_effect=run_mathem):
                outcome = existing.CoreTests().fake_install(Path(temp), clean=True, platform=platform)
                completed, *_, hermes, private = outcome
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(config(private / 'config.json')['provider'], 'mathem')
                self.assertTrue(StateStore(private / 'state', config(private / 'config.json')).read()['profile']['recipes']['sources']['mathem'])
                self.assertIn('https://www.mathem.se/se/cart/', completed.stdout)
                self.assertNotIn('Login command:', completed.stdout)


if __name__ == '__main__':
    unittest.main()
