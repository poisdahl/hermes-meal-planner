from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
import test_meal_concierge_recipes as fixtures
from core import StateStore
from service import Application
from recipes import RecipeStore, RecipeError, normalize_recipe
from recipe_libraries import RecipeLibraryDefiniteError, RecipeLibraryError
import recipe_migration as migration


class Library(fixtures.SyntheticLibraryAdapter):
    def __init__(self, library_id, recipes=()):
        super().__init__(library_id, fixtures.full_recipe(), reconcile=True, favorite_state=False)
        self.docs = {str(i): normalize_recipe(doc) for i, doc in enumerate(recipes)}
        self.versions = {key: 'v1' for key in self.docs}
        self.created_operations = {}
        self.labels = []
        self.associations = []
        self.label_writes = []
        self.labels_enabled = False
        self.page_size = 2
        self.fail_names = set()
        self.timeout = False
        self.reconcile_available = True
        self.storage_supported = True

    def ref(self, key):
        return {'library_id': self.library_id, 'recipe_id': key, 'version': self.versions[key]}

    def capabilities(self):
        result = super().capabilities()
        result['create_from_discovery'] = self.storage_supported
        result['label_read'] = result['label_apply_existing'] = result['label_reconcile'] = self.labels_enabled
        return result

    def search(self, query, filters, cursor, limit):
        offset = int(cursor or 0)
        keys = [k for k, d in self.docs.items() if query in d['name']]
        selected = keys[offset:offset + min(limit, self.page_size)]
        return {'recipes': [{'library_recipe_ref': self.ref(k)} for k in selected],
                'cursor': str(offset + len(selected)) if offset + len(selected) < len(keys) else None}

    def get(self, ref):
        return {**deepcopy(self.docs[ref['recipe_id']]), 'library_recipe_ref': self.ref(ref['recipe_id']), 'is_favorite': self.favorite_state}

    def _native_payload(self, snapshot, operation):
        return {}, deepcopy(snapshot)

    def create_from_snapshot(self, snapshot, operation):
        self.create_calls += 1
        if snapshot['name'] in self.fail_names:
            raise RecipeLibraryDefiniteError('private provider rejection')
        key = 'new-' + str(self.create_calls)
        self.docs[key] = normalize_recipe(snapshot)
        self.versions[key] = 'v1'
        self.reference = self.ref(key)
        result = {'library_recipe_ref': self.ref(key), 'recipe': deepcopy(snapshot)}
        self.created_operations[operation['operation_id']] = result
        if self.timeout:
            raise TimeoutError('private provider response')
        return result

    def reconcile_create(self, snapshot, operation):
        self.reconcile_calls += 1
        return deepcopy(self.created_operations.get(operation['operation_id'])) if self.reconcile_available else None

    def list_labels(self):
        return deepcopy(self.labels)

    def get_recipe_labels(self, ref):
        return deepcopy(self.associations)

    def set_label(self, reference, label_reference, present, *, expected_label_revision=None):
        self.label_writes.append(label_reference)
        self.associations = [label for label in self.labels if label["library_label_ref"] == label_reference]
        return {'library_id': self.library_id, 'library_recipe_ref': reference, 'library_label_ref': label_reference, 'present': present}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name), {**fixtures.CONFIG, 'provider': 'oda'})
        self.app = Application(self.store, fixtures.FakeOda(), fixtures.FakeBrowser())
        self.source = Library('source', [fixtures.full_recipe('First', external_id='one'), fixtures.full_recipe('Second', external_id='two')])
        self.dest = Library('destination')
        for library in (self.source, self.dest):
            self.app.recipe_libraries[library.library_id] = {'library_id': library.library_id, 'provider': 'mealie', 'display_name': library.library_id, 'read_only': False}
            self.app.recipe_library_adapters[library.library_id] = library

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **kwargs):
        return self.app.handle({'operation': 'migration', 'action': 'prepare', 'source_library_id': 'source', 'destination_library_id': 'destination', 'metadata_options': {'favorites': 'omit', 'labels': 'omit'}, **kwargs})

    def execute(self, plan):
        return self.app.handle({'operation': 'migration', 'action': 'execute', 'plan_id': plan['plan_id'], 'confirmation': {'plan_digest': plan['plan_digest'], 'statement': plan['confirmation_statement']}})

    def test_copy_preview_exact_confirmation_pagination_idempotency_private_output(self):
        self.source.page_size = 1
        source_before = deepcopy(self.source.docs)
        plan = self.prepare()
        self.assertEqual([i['status'] for i in plan['items']], ['create', 'create'])
        self.assertEqual(self.dest.create_calls, 0)
        self.assertNotIn('Stek fisken', json.dumps(plan))
        with self.assertRaises(RecipeError):
            self.app.handle({'operation': 'migration', 'action': 'execute', 'plan_id': plan['plan_id'], 'confirmation': True})
        result = self.execute(plan)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.dest.create_calls, 2)
        self.assertEqual(self.execute(plan), result)
        self.assertEqual(self.source.docs, source_before)
        self.assertEqual(self.app.primary_recipe_library_id, 'builtin')
        again = self.prepare()
        self.assertEqual([i['status'] for i in again['items']], ['already_mapped', 'already_mapped'])

    def test_exact_existing_conflicts_and_no_name_url_dedup(self):
        self.dest.docs['exact'] = deepcopy(self.source.docs['0']); self.dest.versions['exact'] = 'v1'
        conflict = deepcopy(self.source.docs['1']); conflict['steps'] = ['Different']
        self.dest.docs['conflict'] = conflict; self.dest.versions['conflict'] = 'v1'
        plan = self.prepare()
        self.assertEqual([i['status'] for i in plan['items']], ['exact_existing', 'conflict'])
        result = self.execute(plan)
        self.assertEqual([i['copy_status'] for i in result['items']], ['confirmed', 'skipped'])
        self.assertEqual(self.dest.create_calls, 0)
        for doc in self.source.docs.values():
            doc['source']['external_id'] = None
            doc['source']['url'] = 'https://example.org/same'
            doc['name'] = 'Same name'
        plan = self.prepare(source_refs=[self.source.ref('1')])
        self.assertEqual(plan['items'][0]['status'], 'create')

    def test_source_and_destination_drift_block_dispatch(self):
        for drift in ('source', 'destination', 'metadata'):
            with self.subTest(drift=drift):
                plan = self.prepare(source_refs=[self.source.ref('0')], metadata_options={'favorites': 'preserve', 'labels': 'omit'})
                if drift == 'source': self.source.versions['0'] = 'v2'
                elif drift == 'metadata': self.source.favorite_state = True
                else:
                    self.dest.docs['drift'] = deepcopy(self.source.docs['0']); self.dest.versions['drift'] = 'v1'
                result = self.execute(plan)
                self.assertEqual(result['status'], 'needs_review', result)
                self.assertEqual(self.dest.create_calls, 0)
                self.source.versions['0'] = 'v1'; self.source.favorite_state = False; self.dest.docs.clear(); self.dest.versions.clear()

    def test_timeout_restart_reconciliation_and_cross_plan_block(self):
        self.dest.timeout = True; self.dest.reconcile_available = False
        plan = self.prepare(source_refs=[self.source.ref('0')])
        result = self.execute(plan)
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(self.prepare(source_refs=[self.source.ref('0')])['items'][0]['status'], 'unavailable')
        self.app._recipe_operations_recovered = False
        self.assertEqual(self.execute(plan)['status'], 'uncertain')
        self.dest.reconcile_available = True
        self.assertEqual(self.execute(plan)['status'], 'complete')
        self.assertEqual(self.dest.create_calls, 1)

    def test_partial_failure_preserves_confirmed_and_concurrent_resume(self):
        self.dest.fail_names.add('Second')
        plan = self.prepare()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.execute(plan))) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(10)
        self.assertEqual(len(results), 2)
        self.assertEqual([i['copy_status'] for i in results[-1]['items']], ['confirmed', 'failed'])
        self.assertEqual(self.dest.create_calls, 2)
        self.assertEqual(len(self.dest.docs), 1)

    def test_rights_unsupported_metadata_and_explicit_omit(self):
        self.dest.storage_supported = False
        self.assertEqual(self.prepare()['items'][0]['status'], 'unsupported_rights')
        self.dest.storage_supported = True
        plan = self.prepare(metadata_options={'favorites': 'omit', 'labels': 'preserve'})
        self.assertEqual(plan['items'][0]['metadata']['labels']['status'], 'unsupported')
        self.execute(plan)
        self.assertEqual(self.dest.create_calls, 0)
        self.assertEqual(self.execute(self.prepare())['status'], 'complete')

    def test_favorite_metadata_partial_never_rolls_back_recipe(self):
        self.source.favorite_state = True
        self.dest.favorite_set_mode = 'definite'
        plan = self.prepare(source_refs=[self.source.ref('0')], metadata_options={'favorites': 'preserve', 'labels': 'omit'})
        result = self.execute(plan)
        self.assertEqual(result['items'][0]['copy_status'], 'confirmed')
        self.assertEqual(result['items'][0]['metadata_status'], 'partial', result)
        self.assertIn('metadata not fully applied', result['items'][0]['message'])
        self.assertEqual(len(self.dest.docs), 1)
        self.execute(plan)
        self.assertEqual(self.dest.create_calls, 1)

    def test_duplicate_label_names_need_exact_mapping(self):
        def label(library, key):
            return {'library_id': library, 'library_label_ref': {'library_id': library, 'label_id': key}, 'name': 'Dinner', 'normalized_name': 'dinner'}
        self.source.labels_enabled = self.dest.labels_enabled = True
        self.source.associations = [label('source', 'src')]
        self.dest.labels = [label('destination', 'one'), label('destination', 'two')]
        options = {'favorites': 'omit', 'labels': 'preserve'}
        self.assertEqual(self.prepare(metadata_options=options)['items'][0]['metadata']['labels']['status'], 'conflict')
        options['label_mappings'] = [{'source': self.source.associations[0]['library_label_ref'], 'destination': self.dest.labels[1]['library_label_ref']}]
        plan = self.prepare(source_refs=[self.source.ref('0')], metadata_options=options)
        self.assertEqual(plan['items'][0]['metadata']['labels']['status'], 'preserve', plan)
        result = self.execute(plan)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.dest.label_writes, [self.dest.labels[1]['library_label_ref']])

    def test_builtin_destination_and_link_only_preserve_rights(self):
        doc = self.source.docs['0']
        doc.update(ingredients=[], steps=[], portions=None)
        doc['rights']['storage'] = 'link_only'; doc['source']['url'] = 'https://example.org/recipe'
        plan = self.prepare(source_refs=[self.source.ref('0')], destination_library_id='builtin')
        result = self.execute(plan)
        self.assertEqual(result['status'], 'complete', result)
        ref = result['items'][0]['destination_ref']
        saved = self.app.recipes.get(ref['recipe_id'])
        self.assertEqual(saved['rights']['storage'], 'link_only')
        self.assertEqual(saved['ingredients'], [])
        self.assertEqual(self.execute(plan)['items'][0]['destination_ref'], ref)

    def test_expiry_cleanup_pins_uncertain_and_retains_confirmed_mapping(self):
        plan = self.prepare(source_refs=[self.source.ref('0')])
        with mock.patch.object(migration, 'now', return_value=migration.now() + timedelta(hours=1)):
            self.assertEqual(self.execute(plan)['status'], 'needs_review')
        plan = self.prepare(source_refs=[self.source.ref('0')])
        self.dest.timeout = True; self.dest.reconcile_available = False
        self.execute(plan)
        with mock.patch.object(migration, 'now', return_value=migration.now() + timedelta(days=31)):
            self.assertEqual(self.execute(plan)['status'], 'uncertain')
            self.dest.reconcile_available = True
            self.assertEqual(self.execute(plan)['status'], 'complete')
            with self.assertRaises(RecipeError):
                migration.Migration(self.app).load(plan['plan_id'])
        self.assertEqual(self.prepare(source_refs=[self.source.ref('0')])['items'][0]['status'], 'already_mapped')

    def test_installed_adapters_validate_native_storage_before_any_write(self):
        engine = migration.Migration(self.app)
        for adapter_class in (fixtures.MealieAdapter, fixtures.RecipeSageAdapter):
            adapter = object.__new__(adapter_class)
            adapter.library_id = 'destination'
            self.app.recipe_library_adapters['destination'] = adapter
            doc = deepcopy(self.source.docs['0'])
            self.assertTrue(engine.storage_supported(doc, 'destination'))
            doc.update(ingredients=[], steps=[], portions=None)
            doc['rights']['storage'] = 'link_only'
            doc['source']['url'] = 'https://example.org/recipe'
            # Native link storage drops source ID/tags: preview must reject
            # this exact document rather than discover loss after dispatch.
            self.assertFalse(engine.storage_supported(doc, 'destination'))

    def test_v4_backup_atomic_failure_retry_and_unknown_schema(self):
        path = Path(self.temp.name) / 'old' / 'recipes.sqlite3'; path.parent.mkdir()
        fixtures.create_v3_bank(path, fixtures.full_recipe('v4', external_id='v4'))
        with closing(sqlite3.connect(path)) as connection:
            RecipeStore(path, 'Hus A')._migrate_v3_to_v4(connection); connection.commit()
        store = RecipeStore(path, 'Hus A')
        original = RecipeStore._migrate_v4_to_v5
        def fail(self, connection):
            original(self, connection)
            raise RecipeError('test rollback')
        with mock.patch.object(RecipeStore, '_migrate_v4_to_v5', fail):
            with self.assertRaisesRegex(RecipeError, 'rollback'): store.search('')
        backup = path.with_name('recipes-v4.backup.sqlite3')
        before = (backup.stat().st_ino, backup.read_bytes())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], '4')
            self.assertFalse(connection.execute("SELECT name FROM sqlite_master WHERE name='migration_plans'").fetchone())
        self.assertEqual(len(store.search('')), 1)
        self.assertEqual((backup.stat().st_ino, backup.read_bytes()), before)
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0], '5')
            connection.execute("UPDATE metadata SET value='6' WHERE key='schema_version'"); connection.commit()
        with self.assertRaisesRegex(RecipeError, 'newer'): store.search('')
