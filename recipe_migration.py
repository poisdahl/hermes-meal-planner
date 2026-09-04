"""Explicit bounded recipe copies. Frozen text stays in the private recipe bank."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets

from recipe_libraries import (RecipeLibraryError, RecipeLibraryDefiniteError,
                              validate_library_id, validate_library_recipe_ref,
                              validate_library_label_ref)
from recipes import normalize_recipe, source_key, RecipeError, MAX_LIBRARY_OPERATIONS

MAX_ITEMS = 20
MAX_PLANS = 100
MAX_MAPPINGS = 10_000
SCAN_LIMIT = 500
CONFIRMATION = "I confirm this exact recipe copy plan and its metadata choices."


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc)


def document(value):
    result = normalize_recipe(value)
    # Retrieval time is an observation, not source content or attribution.
    if result.get("external_snapshot"):
        result["external_snapshot"]["fetched_at"] = "1970-01-01T00:00:00+00:00"
    return result


def origin(doc):
    source = doc["source"]
    if source.get("publisher") and source.get("external_id"):
        return (source["kind"], source["publisher"], source["external_id"])
    return None  # URLs and names never establish migration identity.


def cleanup(connection):
    cutoff = (now() - timedelta(days=30)).isoformat()
    for row in connection.execute("SELECT plan_id FROM migration_plans WHERE created_at <= ?", (cutoff,)).fetchall():
        plan_id = row[0]
        operations = connection.execute(
            "SELECT 1 FROM library_operations WHERE idempotency_key LIKE ? "
            "AND status IN ('pending','uncertain') LIMIT 1", (f"mig:{plan_id}:%",)
        ).fetchone()
        progress = [json.loads(r[0]) for r in connection.execute("SELECT progress FROM migration_items WHERE plan_id=?", (plan_id,))]
        if operations or any(p.get("copy_status") in {"pending", "uncertain"} or p.get("metadata_status") == "pending" for p in progress):
            continue
        connection.execute("DELETE FROM migration_items WHERE plan_id=?", (plan_id,))
        connection.execute("DELETE FROM migration_plans WHERE plan_id=?", (plan_id,))
        connection.execute("DELETE FROM library_operations WHERE idempotency_key LIKE ? AND status='failed'", (f"mig:{plan_id}:%",))


class Migration:
    def __init__(self, app):
        self.app = app
        self.store = app.recipes

    def handle(self, request):
        action = request.get("action", "inspect")
        if action == "prepare":
            return self.prepare(request)
        plan = self.load(request.get("plan_id"))
        if action == "inspect":
            return self.report(plan)
        if action != "execute":
            raise RecipeError("migration action must be prepare, inspect or execute")
        if request.get("confirmation") != {"plan_digest": plan["digest"], "statement": CONFIRMATION}:
            raise RecipeError("confirm the unchanged migration plan digest and statement")
        expired = now() >= datetime.fromisoformat(plan["expires_at"])
        # An expired confirmation allows only reads/reconciliation of already-dispatched work.
        if not self.app._recipe_operations_recovered:
            self.store.recover_library_operations()
            self.app._recipe_operations_recovered = True
        for item in plan["items"]:
            self.execute_item(plan, item, expired)
        return self.report(plan)

    def get(self, reference, *, latest=False):
        ref = dict(reference)
        if latest:
            ref.pop("version", None)
        if ref["library_id"] == "builtin":
            result = self.store.get(ref["recipe_id"], ref.get("version"))
        else:
            result = self.app._external_library_get(ref)
        actual = validate_library_recipe_ref(result["library_recipe_ref"])
        if "version" not in actual:
            raise RecipeError("migration requires authoritative source and destination versions")
        if not latest and actual != reference:
            raise RecipeError("exact recipe reference changed")
        return result

    def scan(self, library, query="", filters=None, maximum=SCAN_LIMIT):
        """Enumerate a complete bounded selection; never silently truncate identity checks."""
        refs, seen, cursor = [], set(), None
        for page_number in range(20):
            if library == "builtin":
                if filters:
                    raise RecipeError("built-in migration filters support query only")
                rows = self.store.search(query, limit=50, offset=page_number * 50, include_archived=True)
                next_cursor = str(page_number + 1) if len(rows) == 50 else None
            else:
                page = self.app.recipe_library_adapters[library].search(query, filters or {}, cursor, 50)
                if not isinstance(page, dict) or not isinstance(page.get("recipes"), list) or len(page["recipes"]) > 50:
                    raise RecipeError("invalid migration search page")
                rows, next_cursor = page["recipes"], page.get("cursor")
                if next_cursor is not None and (not isinstance(next_cursor, str) or not 1 <= len(next_cursor) <= 500 or next_cursor in seen):
                    raise RecipeError("invalid migration search cursor")
            for row in rows:
                ref = validate_library_recipe_ref(row.get("library_recipe_ref"))
                if ref["library_id"] != library or "version" not in ref:
                    raise RecipeError("migration search needs exact versioned library refs")
                if any(old["recipe_id"] == ref["recipe_id"] for old in refs):
                    raise RecipeError("migration search repeated a recipe identity")
                refs.append(ref)
                if len(refs) > maximum:
                    raise RecipeError("migration selection exceeds its bounded scan; narrow the source selection")
            if next_cursor is None:
                return refs
            seen.add(next_cursor)
            cursor = next_cursor
        raise RecipeError("migration search exceeds the page budget")

    def destination_state(self, source_ref, doc, destination):
        with self.store._connection() as connection:
            mapped = connection.execute(
                "SELECT * FROM migration_mappings WHERE source_library=? AND source_id=? AND destination_library=?",
                (source_ref["library_id"], source_ref["recipe_id"], destination)).fetchone()
            pending = connection.execute(
                "SELECT operation_id FROM library_operations WHERE kind IN ('create','migration') AND library_id=? "
                "AND (source_identity IN (?,?) OR (kind='migration' AND json_extract(request_metadata, '$.origin_identity')=?)) "
                "AND status IN ('pending','uncertain') LIMIT 1",
                (destination, self.identity(source_ref), source_key(doc), digest(origin(doc)) if origin(doc) else None)).fetchone()
        if mapped:
            ref = json.loads(mapped["destination_ref"])
            current = self.get(ref, latest=True)
            if mapped["document_digest"] != digest(doc) or digest(document(current)) != digest(doc):
                return {"status": "conflict", "destination_ref": current["library_recipe_ref"]}
            return {"status": "already_mapped", "destination_ref": current["library_recipe_ref"]}
        if pending:
            return {"status": "unavailable", "reason": "unresolved_copy", "operation_id": pending[0]}
        matches = []
        for ref in self.scan(destination):
            current = self.get(ref)
            if origin(doc) is not None and origin(document(current)) == origin(doc):
                matches.append(current)
        if len(matches) > 1:
            return {"status": "conflict", "reason": "ambiguous_exact_origin"}
        if matches:
            current = matches[0]
            return {"status": "exact_existing" if digest(document(current)) == digest(doc) else "conflict",
                    "destination_ref": current["library_recipe_ref"]}
        return {"status": "create"}

    @staticmethod
    def identity(ref):
        return "migration:" + digest({k: ref[k] for k in ("library_id", "recipe_id")})

    def storage_supported(self, doc, destination):
        if destination == "builtin":
            return True
        # Both installed adapters construct native payloads without HTTP. Check
        # size limits and exact attribution/content before authorizing a create.
        adapter = self.app.recipe_library_adapters[destination]
        try:
            _payload, stored = adapter._native_payload(doc, {
                "operation_id": "libop:v1:" + secrets.token_urlsafe(18),
                "library_id": destination, "source_identity": "migration-preview",
                "snapshot_digest": digest(doc),
            })
            return digest(document(stored)) == digest(doc)
        except Exception:
            return False

    def metadata(self, source, destination, options, source_caps, dest_caps):
        result = {"favorites": {"status": "omitted"}, "labels": {"status": "omitted"}, "stages": []}
        if options["favorites"] != "omit":
            supported = source_caps["favorite_read"] and dest_caps["favorite_read"] and dest_caps["favorite_write_desired_state"]
            if not supported or not isinstance(source.get("is_favorite"), bool):
                result["favorites"] = {"status": "unsupported", "choice_required": "omit_or_stop"}
            else:
                result["favorites"] = {"status": "preserve", "is_favorite": source["is_favorite"]}
                result["stages"].append({"action": "set_favorite", "is_favorite": source["is_favorite"]})
        if options["labels"] != "omit":
            if not (source_caps["label_read"] and dest_caps["label_read"] and dest_caps["label_apply_existing"]):
                result["labels"] = {"status": "unsupported", "choice_required": "omit_or_stop"}
            else:
                labels = self.app._read_external_recipe_labels(self.app.recipe_library_adapters[source["library_recipe_ref"]["library_id"]], source["library_recipe_ref"])
                if len(labels) > 20:
                    raise RecipeError("migration supports at most 20 labels per recipe")
                destination_labels = self.app._read_external_labels(self.app.recipe_library_adapters[destination], destination)
                mappings = options.get("label_mappings", [])
                stages = []
                for label in labels:
                    exact = [m for m in mappings if m["source"] == label["library_label_ref"]]
                    found = [d for d in destination_labels if exact and d["library_label_ref"] == exact[0]["destination"]]
                    if len(exact) != 1 or len(found) != 1:
                        result["labels"] = {"status": "conflict", "reason": "exact_label_mapping_required"}
                        break
                    stages.append({"action": "set_label", "library_label_ref": found[0]["library_label_ref"], "present": True,
                                   "source_label_ref": label["library_label_ref"], "source_name": label["name"], "destination_name": found[0]["name"]})
                else:
                    result["labels"] = {"status": "preserve", "mappings": stages}
                    result["stages"].extend(stages)
        return result

    def prepare(self, request):
        source = validate_library_id(request.get("source_library_id"))
        destination = validate_library_id(request.get("destination_library_id"))
        if source == destination or any(x not in self.app.recipe_libraries for x in (source, destination)):
            raise RecipeError("migration needs distinct exact configured source and destination libraries")
        options = request.get("metadata_options")
        if not isinstance(options, dict) or set(options) - {"favorites", "labels", "label_mappings"} or any(options.get(k) not in {"preserve", "omit", "stop"} for k in ("favorites", "labels")):
            raise RecipeError("explicit favorites and labels preserve/omit/stop choices are required")
        options = deepcopy(options)
        mappings = options.get("label_mappings", [])
        if not isinstance(mappings, list) or len(mappings) > 20:
            raise RecipeError("label mappings must contain at most 20 exact pairs")
        for mapping in mappings:
            if not isinstance(mapping, dict) or set(mapping) != {"source", "destination"}:
                raise RecipeError("label mapping requires exact source and destination refs")
            mapping["source"] = validate_library_label_ref(mapping["source"])
            mapping["destination"] = validate_library_label_ref(mapping["destination"])
            if mapping["source"]["library_id"] != source or mapping["destination"]["library_id"] != destination:
                raise RecipeError("label mapping is bound to a different library")
        if len({canonical(m["source"]) for m in mappings}) != len(mappings):
            raise RecipeError("each source label requires exactly one destination mapping")
        selected = request.get("source_refs")
        if selected is not None:
            if request.get("query") or request.get("filters") or not isinstance(selected, list) or not 1 <= len(selected) <= MAX_ITEMS:
                raise RecipeError("provide one to 20 exact refs or a bounded query/filter selection")
            selected = [validate_library_recipe_ref(ref) for ref in selected]
        else:
            query, filters = request.get("query") or "", request.get("filters") or {}
            if not isinstance(query, str) or len(query) > 200 or not isinstance(filters, dict) or len(canonical(filters).encode()) > 16384:
                raise RecipeError("migration query/filter is too large")
            selected = self.scan(source, query, filters, MAX_ITEMS)
        if not selected or any(ref["library_id"] != source or "version" not in ref for ref in selected) or len({ref["recipe_id"] for ref in selected}) != len(selected):
            raise RecipeError("migration selection needs unique exact versioned source refs")
        items = []
        try:
            source_caps, dest_caps = self.app._library_capabilities(source), self.app._library_capabilities(destination)
        except Exception:
            source_caps = dest_caps = None
        for index, ref in enumerate(selected):
            frozen = {"source_ref": ref}
            preview = {"item_id": index, "source_ref": ref, "status": "unavailable"}
            try:
                current = self.get(ref)
                doc = document(current)
                frozen["document"] = doc
                preview.update(name=doc["name"], document_digest=digest(doc))
                if source_caps is None or dest_caps is None:
                    raise RecipeError("capabilities unavailable")
                meta = self.metadata(current, destination, options, source_caps, dest_caps)
                frozen["metadata"] = meta
                preview["metadata"] = {k: v for k, v in meta.items() if k != "stages"}
                if not dest_caps["create_from_discovery"] or dest_caps["read_only"] or not self.storage_supported(doc, destination):
                    preview.update(status="unsupported_rights", reason="destination_storage_unavailable")
                else:
                    preview.update(self.destination_state(ref, doc, destination))
                if any(meta[k]["status"] in {"unsupported", "conflict"} for k in ("favorites", "labels")) or "stop" in (options["favorites"], options["labels"]):
                    preview["metadata_blocked"] = True
            except Exception:
                preview.update(status="unavailable", reason="exact_source_or_destination_unavailable")
            frozen["preview"] = preview
            items.append(frozen)
        timestamp = now()
        plan_id = secrets.token_urlsafe(18)
        preview = {"plan_id": plan_id, "source_library_id": source, "destination_library_id": destination,
                   "metadata_options": options, "items": [item["preview"] for item in items],
                   "expires_at": (timestamp + timedelta(minutes=30)).isoformat(), "confirmation_statement": CONFIRMATION}
        plan_digest = digest(preview)
        if len(canonical(items).encode()) > 4 * 1024 * 1024:
            raise RecipeError("frozen migration selection is too large; select fewer recipes")
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT COUNT(*) FROM migration_plans").fetchone()[0] >= MAX_PLANS:
                raise RecipeError("migration plan capacity reached")
            connection.execute("INSERT INTO migration_plans VALUES(?,?,?,?,?)", (plan_id, plan_digest, canonical(preview), timestamp.isoformat(), preview["expires_at"]))
            for index, item in enumerate(items):
                connection.execute("INSERT INTO migration_items VALUES(?,?,?,?)", (plan_id, index, canonical(item), '{}'))
        return self.report(self.load(plan_id))

    def load(self, plan_id):
        if not isinstance(plan_id, str) or len(plan_id) > 80:
            raise RecipeError("exact migration plan_id is required")
        with self.store._connection() as connection:
            row = connection.execute("SELECT * FROM migration_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None:
                raise RecipeError("migration plan was not found or has expired")
            plan = dict(row)
            plan["preview"] = json.loads(plan["preview"])
            plan["items"] = [{"item_id": row["item_id"], "frozen": json.loads(row["frozen"]), "progress": json.loads(row["progress"])} for row in connection.execute("SELECT * FROM migration_items WHERE plan_id=? ORDER BY item_id", (plan_id,))]
            return plan

    def update(self, plan, item):
        with self.store._connection() as connection:
            connection.execute("UPDATE migration_items SET progress=? WHERE plan_id=? AND item_id=?", (canonical(item["progress"]), plan["plan_id"], item["item_id"]))

    def report(self, plan):
        result = deepcopy(plan["preview"])
        result["plan_digest"] = plan["digest"]
        result["items"] = [{**deepcopy(item["frozen"]["preview"]), **deepcopy(item["progress"])} for item in plan["items"]]
        statuses = [i.get("copy_status") for i in result["items"]]
        if "uncertain" in statuses or any(i.get("metadata_status") == "uncertain" for i in result["items"]):
            result["status"] = "uncertain"
        elif "needs_review" in statuses:
            result["status"] = "needs_review"
        elif all(s == "confirmed" and i.get("metadata_status") == "complete" for s, i in zip(statuses, result["items"])):
            result["status"] = "complete"
        else:
            result["status"] = "partial" if any(statuses) else "prepared"
        result["primary_library_changed"] = False
        return result

    def begin(self, plan, item):
        item["progress"]["copy_status"] = "pending"
        self.update(plan, item)
        frozen = item["frozen"]
        ref, doc = frozen["source_ref"], frozen["document"]
        destination = plan["preview"]["destination_library_id"]
        key = f"mig:{plan['plan_id']}:{item['item_id']}:create"
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM library_operations WHERE idempotency_key=?", (key,)).fetchone()
            if row:
                return self.store._operation(row)
            if connection.execute("SELECT 1 FROM library_connection_controls WHERE library_id=?", (destination,)).fetchone():
                raise RecipeError("destination connection is disabled")
            if connection.execute("SELECT 1 FROM library_operations WHERE kind IN ('create','migration') AND library_id=? "
                                  "AND (source_identity IN (?,?) OR (kind='migration' AND json_extract(request_metadata, '$.origin_identity')=?)) "
                                  "AND status IN ('pending','uncertain')", (destination, self.identity(ref), source_key(doc), digest(origin(doc)) if origin(doc) else None)).fetchone():
                raise RecipeError("an unresolved copy already reserves this source and destination")
            if connection.execute("SELECT COUNT(*) FROM library_operations").fetchone()[0] >= MAX_LIBRARY_OPERATIONS:
                raise RecipeError("recipe operation journal is full")
            if connection.execute("SELECT COUNT(*) FROM migration_mappings").fetchone()[0] + connection.execute("SELECT COUNT(*) FROM library_operations WHERE kind='migration' AND status IN ('pending','uncertain')").fetchone()[0] >= MAX_MAPPINGS:
                raise RecipeError("migration mapping capacity reached")
            operation_id, timestamp = f"libop:v1:{secrets.token_urlsafe(18)}", now().isoformat()
            metadata = {"status": "active", "plan_id": plan["plan_id"], "source_ref": ref, "destination_library_id": destination, "document_digest": digest(doc), "origin_identity": digest(origin(doc)) if origin(doc) else None}
            connection.execute("""INSERT INTO library_operations(operation_id,kind,library_id,request_digest,request_metadata,idempotency_key,status,source_identity,snapshot_digest,created_at,updated_at)
                                  VALUES(?,'migration',?,?,?,?,'pending',?,?,?,?)""", (operation_id, destination, digest(metadata), canonical(metadata), key, self.identity(ref), digest(doc), timestamp, timestamp))
            return self.store._operation(connection.execute("SELECT * FROM library_operations WHERE operation_id=?", (operation_id,)).fetchone())

    def finish(self, operation, status, ref=None):
        with self.store._connection() as connection:
            connection.execute("UPDATE library_operations SET status=?, result_metadata=?, provider_recipe_id=?, provider_version=?, updated_at=? WHERE operation_id=? AND status IN ('pending','uncertain')", (status, canonical(ref) if ref else None, ref["recipe_id"] if ref else None, ref.get("version") if ref else None, now().isoformat(), operation["operation_id"]))
        return self.store.library_operation_snapshot(operation["operation_id"])

    def save_mapping(self, plan, item, reference):
        ref, doc = item["frozen"]["source_ref"], item["frozen"]["document"]
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute("SELECT document_digest FROM migration_mappings WHERE source_library=? AND source_id=? AND destination_library=?", (ref["library_id"], ref["recipe_id"], reference["library_id"])).fetchone()
            if prior is not None and prior[0] != digest(doc):
                raise RecipeError("source mapping has conflicting content")
            if prior is None and connection.execute("SELECT COUNT(*) FROM migration_mappings").fetchone()[0] >= MAX_MAPPINGS:
                raise RecipeError("migration mapping capacity reached")
            connection.execute("INSERT INTO migration_mappings VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_library,source_id,destination_library) DO UPDATE SET destination_ref=excluded.destination_ref", (ref["library_id"], ref["recipe_id"], reference["library_id"], ref["version"], digest(doc), canonical(reference), now().isoformat()))
        item["progress"].update(copy_status="confirmed", destination_ref=reference, metadata_status="pending")
        self.update(plan, item)

    def execute_item(self, plan, item, expired):
        frozen, progress = item["frozen"], item["progress"]
        preview, destination = frozen["preview"], plan["preview"]["destination_library_id"]
        if progress.get("copy_status") == "confirmed":
            self.apply_metadata(plan, item, expired)
            return
        if progress.get("copy_status") in {"needs_review", "failed", "skipped"}:
            return
        if preview["status"] not in {"create", "already_mapped", "exact_existing"} or preview.get("metadata_blocked"):
            progress.update(copy_status="skipped", reason="preview_requires_review_or_explicit_omit")
            self.update(plan, item)
            return
        key = f"mig:{plan['plan_id']}:{item['item_id']}:create"
        operation = self.store.library_operation_for_idempotency(key)
        if operation and operation["status"] == "confirmed":
            self.save_mapping(plan, item, operation["library_recipe_ref"])
            self.apply_metadata(plan, item, expired)
            return
        if operation and operation["status"] == "failed":
            progress["copy_status"] = "failed"
            self.update(plan, item)
            return
        dispatched = operation and (operation["dispatched_at"] or operation["status"] == "uncertain")
        if not dispatched:
            if expired:
                progress.update(copy_status="needs_review", reason="confirmation_expired")
                if operation:
                    self.finish(operation, "failed")
                self.update(plan, item)
                return
            try:
                current = self.get(frozen["source_ref"], latest=True)
                if current["library_recipe_ref"] != frozen["source_ref"] or digest(document(current)) != preview["document_digest"]:
                    raise RecipeError("source drift")
                destination_caps = self.app._library_capabilities(destination)
                if destination_caps["read_only"] or not destination_caps["create_from_discovery"] or not self.storage_supported(frozen["document"], destination):
                    raise RecipeError("destination storage capability changed")
                meta = self.metadata(current, destination, plan["preview"]["metadata_options"], self.app._library_capabilities(frozen["source_ref"]["library_id"]), destination_caps)
                if meta != frozen["metadata"]:
                    raise RecipeError("source metadata drift")
                state = self.destination_state(frozen["source_ref"], frozen["document"], destination) if operation is None else self.destination_state_without_reservation(frozen, destination, operation)
                expected = {k: preview[k] for k in ("status", "destination_ref", "reason", "operation_id") if k in preview}
                if state != expected:
                    raise RecipeError("destination drift")
                if state["status"] in {"already_mapped", "exact_existing"}:
                    self.save_mapping(plan, item, state["destination_ref"])
                    self.apply_metadata(plan, item, expired)
                    return
                operation = self.begin(plan, item)
            except Exception:
                if operation:
                    self.finish(operation, "failed")
                progress.update(copy_status="needs_review", reason="source_destination_or_metadata_changed")
                self.update(plan, item)
                return
        try:
            if destination == "builtin":
                # One local transaction commits the new recipe and operation result.
                with self.store._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    saved = self.store._save(connection, frozen["document"], "active", None, "migration", migration_identity=self.identity(frozen["source_ref"]))
                    ref = saved["library_recipe_ref"]
                    connection.execute("UPDATE library_operations SET status='confirmed',result_metadata=?,provider_recipe_id=?,provider_version=?,updated_at=? WHERE operation_id=?", (canonical(ref), ref["recipe_id"], ref["version"], now().isoformat(), operation["operation_id"]))
            else:
                adapter = self.app.recipe_library_adapters[destination]
                outbound = self.app._outbound_library_snapshot(frozen["document"])
                if dispatched:
                    if not self.app._library_capabilities(destination)["reconcile_create"]:
                        raise RecipeError("exact reconciliation unavailable")
                    result = adapter.reconcile_create(outbound, self.app._outbound_library_operation(operation))
                    if result is None:
                        raise RecipeError("exact reconciliation unresolved")
                else:
                    claimed = self.store.claim_library_dispatch(operation["operation_id"])
                    if not claimed["claimed"]:
                        raise RecipeError("dispatch already claimed")
                    operation = claimed
                    result = adapter.create_from_snapshot(outbound, self.app._outbound_library_operation(operation))
                ref, returned = self.app._validated_library_create_result(result, frozen["document"], destination)
                if "version" not in ref or digest(document(returned)) != preview["document_digest"]:
                    raise RecipeError("semantic create readback differs from the frozen document")
                self.finish(operation, "confirmed", ref)
            self.save_mapping(plan, item, ref)
        except RecipeLibraryDefiniteError:
            self.finish(operation, "uncertain" if dispatched else "failed")
            progress["copy_status"] = "uncertain" if dispatched else "failed"
            self.update(plan, item)
            return
        except Exception:
            self.finish(operation, "uncertain")
            progress["copy_status"] = "uncertain"
            self.update(plan, item)
            return
        self.apply_metadata(plan, item, expired)

    def destination_state_without_reservation(self, frozen, destination, operation):
        # A pre-dispatch crash keeps its reservation, but still needs a fresh complete destination scan.
        matches = []
        for ref in self.scan(destination):
            current = self.get(ref)
            if origin(frozen["document"]) is not None and origin(document(current)) == origin(frozen["document"]):
                matches.append(current)
        return {"status": "create"} if not matches else {"status": "conflict"}

    def apply_metadata(self, plan, item, expired):
        progress = item["progress"]
        stages = item["frozen"]["metadata"]["stages"]
        completed = progress.setdefault("metadata_stages", {})
        for index, stage in enumerate(stages):
            index_key = str(index)
            if completed.get(index_key, {}).get("status") in {"confirmed", "failed"}:
                continue
            key = f"mig:{plan['plan_id']}:{item['item_id']}:meta:{index}"
            if progress["destination_ref"]["library_id"] == "builtin":
                with self.store._connection() as connection:
                    saved = connection.execute("SELECT response_json FROM idempotency WHERE key=? AND operation='set_favorite'", (key,)).fetchone()
                if saved:
                    completed[index_key] = {"status": "confirmed"}
                    continue
                operation = None
            else:
                operation = self.store.library_operation_for_idempotency(key)
            if expired and (operation is None or not operation.get("dispatched_at")):
                completed[index_key] = {"status": "failed", "reason": "confirmation_expired"}
                continue
            try:
                args = {k: v for k, v in stage.items() if k in {"action", "is_favorite", "library_label_ref", "present"}}
                result = self.app._recipes({**args, "library_recipe_ref": progress["destination_ref"], "idempotency_key": key})
                status = result.get("status", "confirmed" if isinstance(result.get("is_favorite"), bool) else "uncertain")
                completed[index_key] = {"status": status}
                if result.get("operation_id"):
                    completed[index_key]["operation_id"] = result["operation_id"]
            except Exception:
                completed[index_key] = {"status": "partial", "reason": "metadata_unavailable"}
            self.update(plan, item)
        statuses = [stage["status"] for stage in completed.values()]
        progress["metadata_status"] = "uncertain" if any(s in {"pending", "uncertain"} for s in statuses) else "partial" if any(s != "confirmed" for s in statuses) else "complete"
        if progress["metadata_status"] != "complete":
            progress["message"] = "recipe copied; metadata not fully applied"
        self.update(plan, item)
